# Copyright (c) 2026, ERPNext Extensions contributors
"""Regression: Material Transfer when source/target warehouses share one stock account."""

from __future__ import annotations

import unittest

import frappe
from erpnext.accounts.general_ledger import process_gl_map
from frappe.utils import flt, random_string

from erpnext_extensions.iran_accounting.e2e_bootstrap import (
	enable_perpetual_inventory,
	ensure_test_item,
	fractional_uom,
	get_irr_company,
	get_warehouse,
	submit_material_receipt,
)
from erpnext_extensions.iran_accounting.stock_gl_consistency.debug_stock_entry_ledger_drift_api import (
	compute_gl_submit_pipeline_counts,
)
from erpnext_extensions.iran_accounting.validation import fetch_gl_rows, fetch_sle_rows, voucher_db_flags
from erpnext_extensions.iran_accounting.zero_value_transfer import (
	_erpnext_gl_merge_key,
	_should_skip_balanced_transfer_gl_pair,
)


def _create_warehouse_same_stock_account(company: str, stock_account: str) -> str:
	wh = frappe.new_doc("Warehouse")
	wh.warehouse_name = f"IA-SameAcc-{random_string(6)}"
	wh.company = company
	wh.account = stock_account
	wh.is_group = 0
	wh.insert(ignore_permissions=True)
	return wh.name


def _pipeline(doc) -> dict:
	return compute_gl_submit_pipeline_counts(doc)


def _assert_no_single_row_gl(doc, msg: str = ""):
	pipe = _pipeline(doc)
	final = pipe.get("FINAL_GL_COUNT", 0)
	self_msg = msg or doc.name
	self.assertIn(final, (0, 2), f"{self_msg}: FINAL_GL_COUNT must be 0 or >=2, got {final} ({pipe})")
	if final:
		self.assertFalse(pipe.get("WOULD_THROW_INCORRECT_GL_COUNT"), pipe)


def _assert_balanced_gl(doc):
	inv = doc.get_inventory_account_map()
	raw = doc.get_gl_entries(inv) or []
	processed = process_gl_map(raw, precision=doc.get_debit_field_precision())
	debit = sum(flt(r.debit) for r in processed)
	credit = sum(flt(r.credit) for r in processed)
	self.assertEqual(debit, credit)


class TestMaterialTransferSameStockAccount(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		from erpnext_extensions.iran_accounting.integration.bootstrap import apply

		apply()
		frappe.set_user("Administrator")
		cls.company = get_irr_company("ESPAD")
		enable_perpetual_inventory(cls.company)
		frappe.flags.iran_gate_defaults = True
		cls.wh_base = get_warehouse(cls.company)
		cls.stock_account = frappe.db.get_value("Warehouse", cls.wh_base, "account")
		# Warehouse.account may be empty; ERPNext then falls back to Company default inventory.
		if not cls.stock_account:
			cls.stock_account = frappe.get_cached_value(
				"Company", cls.company, "default_inventory_account"
			)
		if not cls.stock_account:
			raise unittest.SkipTest("No inventory stock account configured for company")
		cls.wh_same_a = _create_warehouse_same_stock_account(cls.company, cls.stock_account)
		cls.wh_same_b = _create_warehouse_same_stock_account(cls.company, cls.stock_account)
		cls.frac_uom = fractional_uom()
		cls.stock_adj = frappe.get_cached_value("Company", cls.company, "stock_adjustment_account")
		cls.round_off = frappe.get_cached_value("Company", cls.company, "round_off_account")

	def _assert_no_adj_roundoff_gl(self, voucher_no: str):
		for row in fetch_gl_rows("Stock Entry", voucher_no):
			acc = row.get("account")
			self.assertNotIn(acc, (self.stock_adj, self.round_off))

	def _sle_abs_total(self, voucher_no: str) -> float:
		rows = fetch_sle_rows("Stock Entry", voucher_no)
		return sum(abs(flt(r.get("stock_value_difference"))) for r in rows)

	def test_same_account_same_dimensions_zero_gl_and_submit(self):
		item = ensure_test_item(self.company, "IA-MTSAME-1")
		submit_material_receipt(self.company, item, qty=500_000, rate=8151, warehouse=self.wh_same_a)
		se = frappe.new_doc("Stock Entry")
		se.company = self.company
		se.stock_entry_type = "Material Transfer"
		se.purpose = "Material Transfer"
		se.set_stock_entry_type()
		se.append(
			"items",
			{
				"item_code": item,
				"qty": 1000,
				"basic_rate": 8151,
				"s_warehouse": self.wh_same_a,
				"t_warehouse": self.wh_same_b,
			},
		)
		se.insert()
		se.run_method("before_gl_preview")
		pipe = _pipeline(se)
		self.assertEqual(pipe.get("RAW_GL_COUNT"), 0)
		self.assertEqual(pipe.get("FINAL_GL_COUNT"), 0)
		self.assertFalse(pipe.get("WOULD_THROW_INCORRECT_GL_COUNT"))
		se.submit()
		flags = voucher_db_flags("Stock Entry", se.name, self.company)
		self.assertEqual(len(fetch_gl_rows("Stock Entry", se.name)), 0)
		self.assertGreater(self._sle_abs_total(se.name), 0)
		self._assert_no_adj_roundoff_gl(se.name)

	def test_different_stock_accounts_two_legs_submit(self):
		item = ensure_test_item(self.company, "IA-MTDIFF-1")
		# Resolve accounts the same way ERPNext does for GL (explicit warehouse account
		# or company default). Avoid false "different account" picks when base WH.account
		# is empty and falls back to the same default inventory account.
		default_inv = frappe.get_cached_value("Company", self.company, "default_inventory_account")

		def _resolved_account(warehouse: str) -> str | None:
			return frappe.db.get_value("Warehouse", warehouse, "account") or default_inv

		wh_other = None
		for row in frappe.get_all(
			"Warehouse",
			filters={"company": self.company, "is_group": 0},
			fields=["name", "account"],
			limit=100,
		):
			resolved = row.account or default_inv
			if resolved and resolved != self.stock_account:
				wh_other = row.name
				break
		if not wh_other:
			self.skipTest("No second stock account warehouse on site")
		self.assertNotEqual(
			_resolved_account(self.wh_base),
			_resolved_account(wh_other),
			"fixture warehouses must resolve to different inventory accounts",
		)
		submit_material_receipt(self.company, item, qty=100, rate=5000, warehouse=self.wh_base)
		se = frappe.new_doc("Stock Entry")
		se.company = self.company
		se.stock_entry_type = "Material Transfer"
		se.purpose = "Material Transfer"
		se.set_stock_entry_type()
		se.append(
			"items",
			{"item_code": item, "qty": 10, "basic_rate": 5000, "s_warehouse": self.wh_base, "t_warehouse": wh_other},
		)
		se.insert()
		pipe = _pipeline(se)
		self.assertGreaterEqual(pipe.get("RAW_GL_COUNT", 0), 2)
		self.assertGreaterEqual(pipe.get("FINAL_GL_COUNT", 0), 2)
		self.assertFalse(pipe.get("WOULD_THROW_INCORRECT_GL_COUNT"))
		se.submit()
		self.assertGreater(len(fetch_gl_rows("Stock Entry", se.name)), 0)

	def test_merge_key_differs_when_cost_center_differs(self):
		base = {
			"account": "111620 - Test",
			"company": "Co",
			"voucher_type": "Stock Entry",
			"voucher_no": "STE-T",
			"project": None,
			"party": None,
			"party_type": None,
			"voucher_detail_no": None,
			"against_voucher": None,
			"against_voucher_type": None,
			"finance_book": None,
			"advance_voucher_type": None,
			"advance_voucher_no": None,
			"credit": 0,
			"debit": 0,
		}
		credit = frappe._dict({**base, "cost_center": "CC-A", "credit": 100})
		debit = frappe._dict({**base, "cost_center": "CC-B", "debit": 100})
		self.assertNotEqual(_erpnext_gl_merge_key(credit), _erpnext_gl_merge_key(debit))

		class Doc:
			def get_debit_field_precision(self):
				return 0

		self.assertFalse(_should_skip_balanced_transfer_gl_pair(Doc(), credit, debit, 0))

	def test_merge_key_differs_when_facility_differs(self):
		from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
			get_accounting_dimensions,
		)

		if "facility" not in get_accounting_dimensions():
			self.skipTest("Facility not an accounting dimension on this site")
		base = {
			"account": "111620 - Test",
			"cost_center": "CC - T",
			"company": "Co",
			"voucher_type": "Stock Entry",
			"voucher_no": "STE-T",
			"party": None,
			"party_type": None,
			"voucher_detail_no": None,
			"project": None,
			"finance_book": None,
			"advance_voucher_type": None,
			"advance_voucher_no": None,
		}
		credit = frappe._dict({**base, "credit": 50, "debit": 0, "facility": "F1"})
		debit = frappe._dict({**base, "credit": 0, "debit": 50, "facility": "F2"})
		self.assertNotEqual(_erpnext_gl_merge_key(credit), _erpnext_gl_merge_key(debit))

	def test_multi_item_same_account_mat_ste_2026_03077_amounts(self):
		"""Sanitized reproduction of production row amounts (total 3,720,605,460 IRR)."""
		item = ensure_test_item(self.company, "IA-MT-03077")
		rows = [
			(96810.0, 8151.0, 789_098_310),
			(74550.0, 8151.0, 607_657_050),
			(235200.0, 8151.0, 1_917_115_200),
			(49900.0, 8151.0, 406_734_900),
		]
		total_qty = sum(q for q, _, _ in rows)
		submit_material_receipt(
			self.company, item, qty=total_qty, rate=8151, warehouse=self.wh_same_a
		)
		se = frappe.new_doc("Stock Entry")
		se.company = self.company
		se.stock_entry_type = "Material Transfer"
		se.purpose = "Material Transfer"
		se.set_stock_entry_type()
		for qty, rate, _exp in rows:
			se.append(
				"items",
				{
					"item_code": item,
					"qty": qty,
					"basic_rate": rate,
					"s_warehouse": self.wh_same_a,
					"t_warehouse": self.wh_same_b,
				},
			)
		se.insert()
		se.run_method("before_gl_preview")
		self.assertEqual(flt(se.total_outgoing_value), 3_720_605_460)
		self.assertEqual(flt(se.total_incoming_value), 3_720_605_460)
		self.assertEqual(flt(se.value_difference), 0)
		pipe = _pipeline(se)
		self.assertEqual(pipe.get("FINAL_GL_COUNT"), 0)
		self.assertFalse(pipe.get("WOULD_THROW_INCORRECT_GL_COUNT"))
		se.submit()
		out_mag = sum(
			abs(flt(r.stock_value_difference))
			for r in frappe.get_all(
				"Stock Ledger Entry",
				filters={"voucher_type": "Stock Entry", "voucher_no": se.name, "is_cancelled": 0},
				fields=["stock_value_difference"],
			)
		)
		# Outgoing + incoming legs
		self.assertEqual(out_mag, 2 * 3_720_605_460)
		self.assertEqual(len(fetch_gl_rows("Stock Entry", se.name)), 0)

	def test_seven_decimal_qty_same_account(self):
		if not self.frac_uom:
			self.skipTest("No fractional UOM")
		item = ensure_test_item(self.company, "IA-MTFRAC", stock_uom=self.frac_uom)
		submit_material_receipt(self.company, item, qty=100, rate=3333, warehouse=self.wh_same_a)
		qty = 3.1415926
		se = frappe.new_doc("Stock Entry")
		se.company = self.company
		se.stock_entry_type = "Material Transfer"
		se.purpose = "Material Transfer"
		se.set_stock_entry_type()
		se.append(
			"items",
			{
				"item_code": item,
				"qty": qty,
				"basic_rate": 3333,
				"s_warehouse": self.wh_same_a,
				"t_warehouse": self.wh_same_b,
				"uom": self.frac_uom,
				"stock_uom": self.frac_uom,
				"conversion_factor": 1,
			},
		)
		se.insert()
		pipe = _pipeline(se)
		self.assertEqual(pipe.get("FINAL_GL_COUNT"), 0)
		self.assertFalse(pipe.get("WOULD_THROW_INCORRECT_GL_COUNT"))

	def test_submit_cancel_leaves_no_active_gl(self):
		item = ensure_test_item(self.company, "IA-MTCXL")
		submit_material_receipt(self.company, item, qty=50, rate=1000, warehouse=self.wh_same_a)
		se = frappe.new_doc("Stock Entry")
		se.company = self.company
		se.stock_entry_type = "Material Transfer"
		se.purpose = "Material Transfer"
		se.set_stock_entry_type()
		se.append(
			"items",
			{
				"item_code": item,
				"qty": 5,
				"basic_rate": 1000,
				"s_warehouse": self.wh_same_a,
				"t_warehouse": self.wh_same_b,
			},
		)
		se.insert()
		se.submit()
		se.cancel()
		active_gl = fetch_gl_rows("Stock Entry", se.name)
		self.assertEqual(len(active_gl), 0)

	def test_amend_resubmit_same_account(self):
		item = ensure_test_item(self.company, "IA-MTAMD")
		submit_material_receipt(self.company, item, qty=20, rate=2000, warehouse=self.wh_same_a)
		se = frappe.new_doc("Stock Entry")
		se.company = self.company
		se.stock_entry_type = "Material Transfer"
		se.purpose = "Material Transfer"
		se.set_stock_entry_type()
		se.append(
			"items",
			{
				"item_code": item,
				"qty": 2,
				"basic_rate": 2000,
				"s_warehouse": self.wh_same_a,
				"t_warehouse": self.wh_same_b,
			},
		)
		se.insert()
		se.submit()
		se.cancel()
		amended = frappe.copy_doc(se)
		amended.docstatus = 0
		amended.amended_from = se.name
		if amended.meta.has_field("workflow_state"):
			amended.workflow_state = "Draft"
		amended.insert()
		# Site Stock Entry workflow may still block Draft→Submitted; disable briefly.
		frappe.db.sql(
			"update `tabWorkflow` set is_active=0 where document_type=%s and is_active=1",
			("Stock Entry",),
		)
		frappe.clear_cache()
		try:
			amended.reload()
			if amended.meta.has_field("workflow_state"):
				amended.db_set("workflow_state", None, update_modified=False)
			amended.submit()
		finally:
			frappe.db.sql(
				"update `tabWorkflow` set is_active=1 where name=%s",
				("Stock Entry Final Workflow - Manufacturing + Orchid v2",),
			)
			frappe.clear_cache()
		pipe = _pipeline(amended)
		self.assertEqual(pipe.get("FINAL_GL_COUNT"), 0)


if __name__ == "__main__":
	unittest.main()
