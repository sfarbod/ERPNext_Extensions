# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.24 FIX B — RIV-propagated MA must update IRR document economics.

Rate-first remains the submit-time contract. During RIV, when Core outgoing_rate
legitimately diverges from Stock Entry basic_rate on transfer / manufacture /
repack dependants, Iran must round that rate, recalculate document amounts, and
let sync_irr mirror the UPDATED document — never stale amount overwrite.
"""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest import mock

import frappe
from frappe.utils import cint, flt, nowdate

from erpnext_extensions.iran_accounting.domain.riv_rate_guard import (
	is_valued_source_zero_outgoing,
	irr_round_outgoing_rate,
	make_update_rate_on_stock_entry_wrapper,
	should_accept_riv_propagated_outgoing_rate,
)
from erpnext_extensions.iran_accounting.e2e_bootstrap import (
	apply_stock_entry_site_defaults,
	enable_perpetual_inventory,
	ensure_test_item,
	get_irr_company,
	get_second_warehouse,
	get_warehouse,
)
from erpnext_extensions.iran_accounting.tests.hardening.builders import (
	_cost_center,
	_uom,
)


def _run_item_warehouse_riv(company, item_code, warehouse, posting_date, posting_time="00:00:01"):
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import repost

	riv = frappe.new_doc("Repost Item Valuation")
	riv.company = company
	riv.based_on = "Item and Warehouse"
	riv.item_code = item_code
	riv.warehouse = warehouse
	riv.posting_date = posting_date
	riv.posting_time = posting_time
	riv.allow_negative_stock = 1
	riv.flags.ignore_permissions = True
	riv.insert(ignore_permissions=True)
	repost(riv)
	frappe.db.commit()
	riv.reload()
	return riv


class TestRivPropagatedRateUnit(unittest.TestCase):
	def test_helper_requires_through_riv(self):
		self.assertFalse(
			should_accept_riv_propagated_outgoing_rate(
				purpose="Material Transfer for Manufacture",
				actual_qty=-10,
				basic_rate=100,
				outgoing_rate=250,
				through_riv=False,
			)
		)

	def test_helper_accepts_mtfm_divergence_during_riv(self):
		self.assertTrue(
			should_accept_riv_propagated_outgoing_rate(
				purpose="Material Transfer for Manufacture",
				actual_qty=-10,
				basic_rate=100,
				outgoing_rate=250,
				through_riv=True,
			)
		)

	def test_helper_rejects_idempotent_matching_rates(self):
		self.assertFalse(
			should_accept_riv_propagated_outgoing_rate(
				purpose="Manufacture",
				actual_qty=-10,
				basic_rate=250,
				outgoing_rate=250.4,
				through_riv=True,
			)
		)

	def test_helper_rejects_incoming_qty(self):
		self.assertFalse(
			should_accept_riv_propagated_outgoing_rate(
				purpose="Material Transfer",
				actual_qty=10,
				basic_rate=100,
				outgoing_rate=250,
				through_riv=True,
			)
		)

	def test_valued_source_zero_still_manufacture_only(self):
		self.assertTrue(
			is_valued_source_zero_outgoing(
				actual_qty=-10,
				basic_rate=0,
				allow_zero_valuation_rate=0,
				outgoing_rate=250,
				purpose="Manufacture",
			)
		)
		self.assertFalse(
			is_valued_source_zero_outgoing(
				actual_qty=-10,
				basic_rate=0,
				allow_zero_valuation_rate=0,
				outgoing_rate=250,
				purpose="Material Transfer for Manufacture",
			)
		)

	def test_wrapper_accepts_propagated_rate_during_riv(self):
		calls = []

		def original(self, sle, outgoing_rate):
			calls.append(outgoing_rate)

		engine = mock.Mock()
		engine.company = "IRR-CO"
		engine.is_manufacture_entry_with_sabb = mock.Mock(return_value=False)
		engine.recalculate_amounts_in_stock_entry = mock.Mock()
		sle = mock.Mock()
		sle.voucher_detail_no = "row-1"
		sle.voucher_no = "STE-1"
		sle.dependant_sle_voucher_detail_no = "dep"
		sle.company = "IRR-CO"
		sle.actual_qty = -10
		sle.outgoing_rate = 0

		def _gv(doctype, name, fields=None, as_dict=False):
			if doctype == "Stock Entry Detail":
				return frappe._dict(
					name="row-1",
					basic_rate=100,
					allow_zero_valuation_rate=0,
					parent="STE-1",
				)
			if doctype == "Stock Entry":
				return "Material Transfer for Manufacture"
			return None

		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
				return_value=True,
			),
			mock.patch("frappe.db.get_value", side_effect=_gv),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.riv_rate_guard.persist_irr_contract_after_recalculate"
			) as persist,
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.riv_rate_guard.is_through_repost_item_valuation",
				return_value=True,
			),
		):
			wrapped = make_update_rate_on_stock_entry_wrapper(original)
			wrapped(engine, sle, 250.4)
			self.assertEqual(calls, [250.0])
			engine.recalculate_amounts_in_stock_entry.assert_called_once()
			persist.assert_called_once()
			self.assertEqual(sle.outgoing_rate, 250.0)

	def test_wrapper_still_skips_without_riv_flag(self):
		calls = []

		def original(self, sle, outgoing_rate):
			calls.append(("original", outgoing_rate))

		engine = mock.Mock()
		engine.company = "IRR-CO"
		engine.is_manufacture_entry_with_sabb = mock.Mock(return_value=False)
		engine.recalculate_amounts_in_stock_entry = mock.Mock()
		sle = mock.Mock()
		sle.voucher_detail_no = "row-1"
		sle.voucher_no = "STE-1"
		sle.dependant_sle_voucher_detail_no = "dep"
		sle.company = "IRR-CO"
		sle.actual_qty = -10
		sle.outgoing_rate = 0

		def _gv(doctype, name, fields=None, as_dict=False):
			if doctype == "Stock Entry Detail":
				return frappe._dict(
					name="row-1",
					basic_rate=100,
					allow_zero_valuation_rate=0,
					parent="STE-1",
				)
			if doctype == "Stock Entry":
				return "Material Transfer for Manufacture"
			return None

		with (
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
				return_value=True,
			),
			mock.patch("frappe.db.get_value", side_effect=_gv),
			mock.patch(
				"erpnext_extensions.iran_accounting.domain.riv_rate_guard.is_through_repost_item_valuation",
				return_value=False,
			),
		):
			wrapped = make_update_rate_on_stock_entry_wrapper(original)
			wrapped(engine, sle, 250.0)
			self.assertEqual(calls, [])
			engine.recalculate_amounts_in_stock_entry.assert_not_called()

	def test_irr_round_outgoing_rate(self):
		self.assertEqual(irr_round_outgoing_rate(250.4), 250.0)
		self.assertEqual(irr_round_outgoing_rate(249.6), 250.0)


class TestRivPropagatedRateChain(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		from erpnext_extensions.iran_accounting.integration.bootstrap import apply

		apply()
		frappe.set_user("Administrator")
		cls.company = get_irr_company("ESPAD")
		enable_perpetual_inventory(cls.company)
		cls.wh = get_warehouse(cls.company)
		cls.wh2 = get_second_warehouse(cls.company, cls.wh)

	def _finalize_se(self, se, posting_time):
		se.posting_date = nowdate()
		se.posting_time = posting_time
		se.set_posting_time = 1
		frappe.flags.iran_gate_defaults = True
		apply_stock_entry_site_defaults(se)
		se.insert(ignore_permissions=True)
		se.submit()
		frappe.db.commit()
		return se

	def _submit_receipt_at(self, item, qty, rate, warehouse, posting_time):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		se = make_stock_entry(
			item_code=item,
			qty=float(qty),
			rate=float(rate),
			target=warehouse,
			company=self.company,
			purpose="Material Receipt",
			do_not_save=True,
			do_not_submit=True,
		)
		return self._finalize_se(se, posting_time)

	def _make_mtfm(self, item, qty, from_wh, to_wh, posting_time):
		cc = _cost_center(self.company)
		se = frappe.new_doc("Stock Entry")
		se.company = self.company
		se.stock_entry_type = "Material Transfer for Manufacture"
		se.purpose = "Material Transfer for Manufacture"
		se.append(
			"items",
			{
				"item_code": item,
				"qty": float(qty),
				"transfer_qty": float(qty),
				"conversion_factor": 1,
				"uom": _uom(item),
				"s_warehouse": from_wh,
				"t_warehouse": to_wh,
				"cost_center": cc,
			},
		)
		return self._finalize_se(se, posting_time)

	def _make_manufacture(self, rm, fg, rm_wh, fg_wh, rm_qty, fg_qty, rm_rate, posting_time):
		cc = _cost_center(self.company)
		se = frappe.new_doc("Stock Entry")
		se.company = self.company
		se.stock_entry_type = "Manufacture"
		se.purpose = "Manufacture"
		se.append(
			"items",
			{
				"item_code": rm,
				"qty": float(rm_qty),
				"transfer_qty": float(rm_qty),
				"conversion_factor": 1,
				"uom": _uom(rm),
				"s_warehouse": rm_wh,
				"basic_rate": float(rm_rate),
				"cost_center": cc,
			},
		)
		se.append(
			"items",
			{
				"item_code": fg,
				"qty": float(fg_qty),
				"transfer_qty": float(fg_qty),
				"conversion_factor": 1,
				"uom": _uom(fg),
				"t_warehouse": fg_wh,
				"is_finished_item": 1,
				"cost_center": cc,
			},
		)
		return self._finalize_se(se, posting_time)

	def test_receipt_rate_change_propagates_through_mtfm_manufacture(self):
		"""Controlled 100→250 chain with Iran patches fully active."""
		import erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild as rebuild

		rebuild_calls = {"n": 0}
		_orig = rebuild.rebuild_affected_documents

		def _probe(*a, **k):
			rebuild_calls["n"] += 1
			return _orig(*a, **k)

		rebuild.rebuild_affected_documents = _probe
		try:
			rm = ensure_test_item(f"PROP-RM-{frappe.generate_hash(length=6)}", self.company)
			fg = ensure_test_item(f"PROP-FG-{frappe.generate_hash(length=6)}", self.company)
			rec = self._submit_receipt_at(rm, 10, 100, self.wh, "09:00:00")
			rec_name = rec.name
			tr = self._make_mtfm(rm, 10, self.wh, self.wh2, "10:00:00")
			mfg = self._make_manufacture(rm, fg, self.wh2, self.wh, 10, 5, 100, "11:00:00")

			# Change receipt economics to 250 (controlled setup only)
			for d in frappe.get_doc("Stock Entry", rec_name).items:
				frappe.db.set_value(
					"Stock Entry Detail",
					d.name,
					{
						"basic_rate": 250,
						"valuation_rate": 250,
						"basic_amount": 2500,
						"amount": 2500,
					},
					update_modified=False,
				)
			sle_name = frappe.db.get_value(
				"Stock Ledger Entry",
				{"voucher_no": rec_name, "item_code": rm, "is_cancelled": 0},
				"name",
			)
			frappe.db.set_value(
				"Stock Ledger Entry",
				sle_name,
				{
					"incoming_rate": 250,
					"valuation_rate": 250,
					"stock_value": 2500,
					"stock_value_difference": 2500,
				},
				update_modified=False,
			)
			posting_date = frappe.db.get_value("Stock Entry", rec_name, "posting_date")
			frappe.db.commit()

			riv = _run_item_warehouse_riv(self.company, rm, self.wh, posting_date)
			self.assertEqual(riv.status, "Completed")

			tr_row = frappe.db.get_value(
				"Stock Entry Detail",
				{"parent": tr.name},
				["basic_rate", "amount", "valuation_rate"],
				as_dict=True,
			)
			self.assertEqual(flt(tr_row.basic_rate), 250.0)
			self.assertEqual(flt(tr_row.amount), 2500.0)

			mfg_rows = frappe.db.get_all(
				"Stock Entry Detail",
				filters={"parent": mfg.name},
				fields=["item_code", "basic_rate", "amount", "is_finished_item"],
				order_by="idx",
			)
			consume = next(r for r in mfg_rows if not cint(r.is_finished_item))
			finished = next(r for r in mfg_rows if cint(r.is_finished_item))
			self.assertEqual(flt(consume.basic_rate), 250.0)
			self.assertEqual(flt(consume.amount), 2500.0)
			self.assertEqual(flt(finished.basic_rate), 500.0)
			self.assertEqual(flt(finished.amount), 2500.0)

			fg_sle = frappe.db.get_value(
				"Stock Ledger Entry",
				{"voucher_no": mfg.name, "item_code": fg, "is_cancelled": 0},
				["incoming_rate", "valuation_rate", "stock_value_difference"],
				as_dict=True,
			)
			self.assertEqual(flt(fg_sle.incoming_rate), 500.0)
			self.assertEqual(flt(fg_sle.valuation_rate), 500.0)
			self.assertEqual(flt(fg_sle.stock_value_difference), 2500.0)

			gl = frappe.db.sql(
				"""
				SELECT SUM(debit) d, SUM(credit) c FROM `tabGL Entry`
				WHERE voucher_no=%s AND is_cancelled=0
				""",
				mfg.name,
			)[0]
			self.assertEqual(flt(gl[0]), 2500.0)
			self.assertEqual(flt(gl[1]), 2500.0)
			self.assertEqual(rebuild_calls["n"], 0)

			# Idempotent second RIV
			riv2 = _run_item_warehouse_riv(self.company, rm, self.wh, posting_date)
			self.assertEqual(riv2.status, "Completed")
			fg_sle2 = frappe.db.get_value(
				"Stock Ledger Entry",
				{"voucher_no": mfg.name, "item_code": fg, "is_cancelled": 0},
				["incoming_rate", "valuation_rate", "stock_value_difference"],
				as_dict=True,
			)
			self.assertEqual(flt(fg_sle2.incoming_rate), 500.0)
			self.assertEqual(flt(fg_sle2.stock_value_difference), 2500.0)
		finally:
			rebuild.rebuild_affected_documents = _orig

	def test_repack_propagates_when_upstream_receipt_rate_changes(self):
		rm = ensure_test_item(f"PROP-RP-IN-{frappe.generate_hash(length=6)}", self.company)
		out = ensure_test_item(f"PROP-RP-OUT-{frappe.generate_hash(length=6)}", self.company)
		rec = self._submit_receipt_at(rm, 10, 100, self.wh, "09:00:00")
		rec_name = rec.name
		cc = _cost_center(self.company)
		se = frappe.new_doc("Stock Entry")
		se.company = self.company
		se.stock_entry_type = "Repack"
		se.purpose = "Repack"
		se.append(
			"items",
			{
				"item_code": rm,
				"qty": 10,
				"transfer_qty": 10,
				"conversion_factor": 1,
				"uom": _uom(rm),
				"s_warehouse": self.wh,
				"basic_rate": 100,
				"cost_center": cc,
			},
		)
		se.append(
			"items",
			{
				"item_code": out,
				"qty": 5,
				"transfer_qty": 5,
				"conversion_factor": 1,
				"uom": _uom(out),
				"t_warehouse": self.wh,
				"is_finished_item": 1,
				"cost_center": cc,
			},
		)
		self._finalize_se(se, "12:00:00")

		for d in frappe.get_doc("Stock Entry", rec_name).items:
			frappe.db.set_value(
				"Stock Entry Detail",
				d.name,
				{"basic_rate": 250, "valuation_rate": 250, "basic_amount": 2500, "amount": 2500},
				update_modified=False,
			)
		sle_name = frappe.db.get_value(
			"Stock Ledger Entry",
			{"voucher_no": rec_name, "item_code": rm, "is_cancelled": 0},
			"name",
		)
		frappe.db.set_value(
			"Stock Ledger Entry",
			sle_name,
			{
				"incoming_rate": 250,
				"valuation_rate": 250,
				"stock_value": 2500,
				"stock_value_difference": 2500,
			},
			update_modified=False,
		)
		posting_date = frappe.db.get_value("Stock Entry", rec_name, "posting_date")
		frappe.db.commit()
		riv = _run_item_warehouse_riv(self.company, rm, self.wh, posting_date)
		self.assertEqual(riv.status, "Completed")

		rows = frappe.db.get_all(
			"Stock Entry Detail",
			filters={"parent": se.name},
			fields=["item_code", "basic_rate", "amount", "is_finished_item"],
			order_by="idx",
		)
		consume = next(r for r in rows if not cint(r.is_finished_item))
		finished = next(r for r in rows if cint(r.is_finished_item))
		self.assertEqual(flt(consume.basic_rate), 250.0)
		self.assertEqual(flt(consume.amount), 2500.0)
		self.assertEqual(flt(finished.basic_rate), 500.0)
		self.assertEqual(flt(finished.amount), 2500.0)

		# SLE mirrors document amount
		for row in rows:
			sle = frappe.db.get_value(
				"Stock Ledger Entry",
				{"voucher_detail_no": frappe.db.get_value("Stock Entry Detail", {"parent": se.name, "item_code": row.item_code}, "name"), "is_cancelled": 0},
				["stock_value_difference", "actual_qty"],
				as_dict=True,
			)
			# resolve detail name properly
		for detail in frappe.get_all(
			"Stock Entry Detail",
			filters={"parent": se.name},
			fields=["name", "amount", "item_code"],
		):
			sle = frappe.db.get_value(
				"Stock Ledger Entry",
				{"voucher_detail_no": detail.name, "is_cancelled": 0},
				["stock_value_difference", "actual_qty"],
				as_dict=True,
			)
			self.assertIsNotNone(sle)
			self.assertAlmostEqual(abs(flt(sle.stock_value_difference)), abs(flt(detail.amount)), places=0)
