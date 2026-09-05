# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Inventory Account forensic contract: transfers, multi-account footer, Account stays GL."""

from __future__ import annotations

import unittest
from datetime import timedelta

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.api import (
	get_account_summary,
	get_inventory_account_summary,
)
from erpnext_extensions.iran_accounting.account_explorer.inventory_account_measures import (
	get_inventory_account_attribution,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	build_payload,
	current_fiscal_year,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_inventory_axes import (
	_ensure_item_group,
	_ensure_stock_item,
	_insert_sle,
	enable_inventory_analysis,
	getdate_safe,
	require_inventory_company,
)


class TestInventoryAccountForensicContract(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.company = require_inventory_company(cls)
		enable_inventory_analysis()
		fy = current_fiscal_year(cls.company)
		if not fy:
			raise unittest.SkipTest(f"No fiscal year for {cls.company}")
		cls.fiscal_year, cls.fy_start, cls.fy_end = fy
		cls.from_date = str(getdate_safe(cls.fy_start) + timedelta(days=5))
		cls.to_date = str(getdate_safe(cls.fy_start) + timedelta(days=60))
		cls.mid = str(getdate_safe(cls.fy_start) + timedelta(days=20))

		suffix = frappe.generate_hash(length=6)
		cls.group = _ensure_item_group(f"AE Forensic Group {suffix}", "All Item Groups", is_group=0)
		# Dedicated warehouses so other tests cannot remount shared WH→account maps.
		cls.wh_a = f"AE Forensic WH A {suffix} - _TC"
		cls.wh_b = f"AE Forensic WH B {suffix} - _TC"
		for wh, parent in ((cls.wh_a, "All Warehouses - _TC"), (cls.wh_b, "All Warehouses - _TC")):
			if not frappe.db.exists("Warehouse", wh):
				doc = frappe.get_doc(
					{
						"doctype": "Warehouse",
						"warehouse_name": wh.replace(" - _TC", ""),
						"company": cls.company,
						"parent_warehouse": parent if frappe.db.exists("Warehouse", parent) else "",
						"is_group": 0,
					}
				)
				doc.insert(ignore_permissions=True)
				# ERPNext may append company abbr; use created name.
				if doc.name != wh:
					if wh == cls.wh_a:
						cls.wh_a = doc.name
					else:
						cls.wh_b = doc.name
		accounts = frappe.get_all(
			"Account",
			filters={"company": cls.company, "account_type": "Stock", "is_group": 0},
			pluck="name",
			limit=2,
		)
		if len(accounts) < 2:
			raise unittest.SkipTest("Need two stock accounts")
		cls.acc_a, cls.acc_b = accounts[0], accounts[1]
		frappe.db.set_value("Warehouse", cls.wh_a, "account", cls.acc_a, update_modified=False)
		frappe.db.set_value("Warehouse", cls.wh_b, "account", cls.acc_b, update_modified=False)
		frappe.db.commit()
		frappe.clear_cache()
		frappe.flags.pop("warehouse_account_map", None)

		cls.item = _ensure_stock_item(f"AE-FORENSIC-{suffix}", cls.group, cls.company, cls.wh_a)
		cls.suffix = suffix
		# Purchase 1000
		_insert_sle(
			company=cls.company,
			item_code=cls.item,
			warehouse=cls.wh_a,
			posting_date=cls.mid,
			actual_qty=10,
			stock_value_difference=1000,
			voucher_suffix=f"FOR-IN-{suffix}",
		)
		# Issue 200
		_insert_sle(
			company=cls.company,
			item_code=cls.item,
			warehouse=cls.wh_a,
			posting_date=cls.mid,
			actual_qty=-2,
			stock_value_difference=-200,
			voucher_suffix=f"FOR-OUT-{suffix}",
		)
		# Same-account transfer ±500 (both legs on wh_a → same inventory account)
		_insert_sle(
			company=cls.company,
			item_code=cls.item,
			warehouse=cls.wh_a,
			posting_date=cls.mid,
			actual_qty=-5,
			stock_value_difference=-500,
			voucher_suffix=f"FOR-SAME-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item,
			warehouse=cls.wh_a,
			posting_date=cls.mid,
			actual_qty=5,
			stock_value_difference=500,
			voucher_suffix=f"FOR-SAME-{suffix}-IN",
		)
		# Cross-account transfer 300
		_insert_sle(
			company=cls.company,
			item_code=cls.item,
			warehouse=cls.wh_a,
			posting_date=cls.mid,
			actual_qty=-3,
			stock_value_difference=-300,
			voucher_suffix=f"FOR-X-{suffix}",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item,
			warehouse=cls.wh_b,
			posting_date=cls.mid,
			actual_qty=3,
			stock_value_difference=300,
			voucher_suffix=f"FOR-X-{suffix}",
		)

	def _payload(self, inventory=None):
		payload = frappe.parse_json(
			build_payload(
				self.company,
				self.fiscal_year,
				self.from_date,
				self.to_date,
				analysis={"view_axis": "inventory_account", "page_size": 200},
				document={
					"hide_zero_rows": 0,
					"inventory": inventory or {"item": self.item},
				},
			)
		)
		payload["prepared_mode"] = "live"
		return payload

	def test_same_account_transfer_preserves_stock_inward_outward(self):
		"""Stock axis keeps gross inward/outward (unlike GL netting of same-account transfer)."""
		spec = AccountExplorerQuerySpec_from_client(self._payload(), require_dates=True)
		attr = get_inventory_account_attribution(spec)
		m = attr.rows_by_account.get(self.acc_a) or {}
		# Inward: 1000 + 500 (same-in) = 1500; Outward: 200 + 500 + 300 = 1000
		self.assertAlmostEqual(flt(m.get("inward_value")), 1500.0, places=2)
		self.assertAlmostEqual(flt(m.get("outward_value")), 1000.0, places=2)
		self.assertAlmostEqual(flt(m.get("balance_value")), 500.0, places=2)

	def test_cross_account_transfer_both_sides(self):
		spec = AccountExplorerQuerySpec_from_client(self._payload(), require_dates=True)
		attr = get_inventory_account_attribution(spec)
		ma = attr.rows_by_account.get(self.acc_a) or {}
		mb = attr.rows_by_account.get(self.acc_b) or {}
		self.assertAlmostEqual(flt(mb.get("inward_value")), 300.0, places=2)
		self.assertAlmostEqual(flt(mb.get("balance_value")), 300.0, places=2)
		self.assertAlmostEqual(flt(ma.get("balance_value")), 500.0, places=2)
		self.assertAlmostEqual(attr.attributed_signed_balance, 800.0, places=2)

	def test_inventory_account_columns_not_gl_turnover(self):
		result = get_inventory_account_summary(self._payload())
		labels = {c["id"]: c["label"] for c in result.get("columns") or []}
		self.assertEqual(labels.get("inward_value"), "Inward Value")
		self.assertEqual(labels.get("outward_value"), "Outward Value")
		self.assertNotIn("period_debit", labels)
		self.assertNotIn("Debit Turnover", labels.values())

	def test_account_axis_not_attributed(self):
		payload = frappe.parse_json(
			build_payload(
				self.company,
				self.fiscal_year,
				self.from_date,
				self.to_date,
				analysis={"view_axis": "account_level", "level_sequence": 1, "page_size": 200},
				document={"hide_zero_rows": 0, "inventory": {"item_group": self.group}},
			)
		)
		payload["prepared_mode"] = "live"
		ac = get_account_summary(payload)
		self.assertIsNone(ac.get("inventory_attribution"))
		labels = {c["id"]: c["label"] for c in ac.get("columns") or []}
		self.assertNotEqual(labels.get("period_debit"), "Attributed Stock Debit")

	def test_multi_account_footer_side_netting(self):
		item2 = _ensure_stock_item(
			f"AE-FORENSIC-OFF-{self.suffix}", self.group, self.company, self.wh_a
		)
		_insert_sle(
			company=self.company,
			item_code=item2,
			warehouse=self.wh_a,
			posting_date=self.mid,
			actual_qty=1,
			stock_value_difference=100,
			voucher_suffix=f"FOR-OFF-A-{self.suffix}",
		)
		_insert_sle(
			company=self.company,
			item_code=item2,
			warehouse=self.wh_b,
			posting_date=self.mid,
			actual_qty=-1,
			stock_value_difference=-30,
			voucher_suffix=f"FOR-OFF-B-{self.suffix}",
		)
		result = get_inventory_account_summary(self._payload({"item": item2}))
		totals = result.get("totals") or {}
		self.assertAlmostEqual(flt(totals.get("balance_value")), 70.0, places=2)
		self.assertAlmostEqual(flt(totals.get("debit_balance")), 70.0, places=2)
		self.assertAlmostEqual(flt(totals.get("credit_balance")), 0.0, places=2)
