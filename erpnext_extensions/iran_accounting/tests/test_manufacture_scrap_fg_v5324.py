# Copyright (c) 2026, ERPNext Extensions contributors
"""Exploded manufacture scrap + FG reconstruction (v5.3.24)."""

from __future__ import annotations

from unittest.mock import patch

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock.manufacture_native import (
	EXACT,
	HEALTHY,
	LEGITIMATE,
	MANUAL,
	WAITING_UPSTREAM,
)
from erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg import (
	STRATEGY,
	analyze_manufacture_scrap_fg,
	repair_manufacture_scrap_fg,
)


def _d(**kwargs):
	base = frappe._dict(
		{
			"name": "d1",
			"idx": 1,
			"item_code": "RM",
			"qty": 10,
			"transfer_qty": 10,
			"basic_rate": 100,
			"valuation_rate": 100,
			"amount": 1000,
			"basic_amount": 1000,
			"s_warehouse": "Paykar",
			"t_warehouse": None,
			"is_fg": 0,
			"is_scrap": 0,
			"allow_zero": 0,
			"secondary_item_type": "",
			"valuation_type": "",
			"bom_secondary_item": None,
			"additional_cost": 0,
		}
	)
	base.update(kwargs)
	return base


def _sle(**kwargs):
	base = frappe._dict(
		{
			"name": "sle1",
			"voucher_detail_no": "d1",
			"item_code": "RM",
			"warehouse": "Paykar",
			"actual_qty": -10,
			"incoming_rate": 0,
			"outgoing_rate": 100,
			"valuation_rate": 100,
			"stock_value_difference": -1000,
			"stock_value": 0,
			"batch_no": None,
		}
	)
	base.update(kwargs)
	return base


def _se(**kwargs):
	base = frappe._dict(
		name="STE-X",
		purpose="Manufacture",
		docstatus=1,
		company="X",
		work_order="WO-1",
		job_card="JC-1",
		posting_date="2026-01-01",
		posting_time="12:00:00",
		total_additional_costs=0,
	)
	base.update(kwargs)
	return base


class TestManufactureScrapFg(unittest.TestCase):
	def _run(self, se, details, sles, native=None):
		by = {s.voucher_detail_no: s for s in sles}
		native = native or {
			"classification": HEALTHY,
			"eligible": False,
			"expected_target_rate": 100,
			"reason": "FG already matches consumed-SVD residual rate",
		}
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg._load_voucher",
				return_value=(se, details, by),
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg.reconstruct_manufacture_valuation",
				return_value=native,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg.frappe.db.exists",
				return_value=True,
			),
		):
			return analyze_manufacture_scrap_fg(se.name)

	def test_healthy_same_item_scrap_ratio_one(self):
		"""36933-class: scrap rate equals same-voucher consume rate."""
		src = _d(name="s1", item_code="RM", qty=100, basic_rate=50, basic_amount=5000, amount=5000)
		scrap = _d(
			name="c1",
			item_code="RM",
			qty=10,
			basic_rate=50,
			basic_amount=500,
			amount=500,
			s_warehouse=None,
			t_warehouse="Scrap WH",
			secondary_item_type="Scrap",
			valuation_type="Valuation Rate",
		)
		fg = _d(
			name="f1",
			item_code="FG",
			qty=90,
			basic_rate=50,
			basic_amount=4500,
			amount=4500,
			is_fg=1,
			s_warehouse=None,
			t_warehouse="FG WH",
		)
		sles = [
			_sle(name="ss", voucher_detail_no="s1", item_code="RM", actual_qty=-100, outgoing_rate=50, stock_value_difference=-5000),
			_sle(name="sc", voucher_detail_no="c1", item_code="RM", warehouse="Scrap WH", actual_qty=10, incoming_rate=50, outgoing_rate=0, stock_value_difference=500),
			_sle(name="sf", voucher_detail_no="f1", item_code="FG", warehouse="FG WH", actual_qty=90, incoming_rate=50, outgoing_rate=0, stock_value_difference=4500),
		]
		an = self._run(_se(name="STE-OK"), [src, scrap, fg], sles)
		self.assertEqual(an["classification"], HEALTHY)
		self.assertFalse(an["eligible"])
		self.assertFalse(an.get("exploded_count"))

	def test_healthy_high_warehouse_scrap_preserved(self):
		"""37090-class: scrap >> consume rate but FG already matches documented residual."""
		src = _d(name="s1", item_code="PK", qty=1200, basic_rate=148500, basic_amount=178200000, amount=178200000)
		other = _d(
			name="s2",
			item_code="RM",
			qty=10,
			basic_rate=500000,
			basic_amount=5000000,
			amount=5000000,
		)
		scrap = _d(
			name="c1",
			item_code="PK",
			qty=50,
			basic_rate=24550761,
			basic_amount=1227538050,
			amount=1227538050,
			s_warehouse=None,
			t_warehouse="Other WH",
			secondary_item_type="Scrap",
			valuation_type="Valuation Rate",
		)
		# consumed = 178200000 + 5000000 = 183200000; scrap = 1227538050 would exceed
		# Use a pool large enough that documented scrap still leaves positive FG.
		src2 = _d(
			name="s3",
			item_code="RM2",
			qty=100,
			basic_rate=60000000,
			basic_amount=6000000000,
			amount=6000000000,
		)
		consumed = 178200000 + 5000000 + 6000000000
		scrap_amt = 1227538050
		fg_qty = 1151
		fg_rate = (consumed - scrap_amt) / fg_qty
		fg = _d(
			name="f1",
			item_code="FG",
			qty=fg_qty,
			basic_rate=fg_rate,
			basic_amount=fg_rate * fg_qty,
			amount=fg_rate * fg_qty,
			is_fg=1,
			s_warehouse=None,
			t_warehouse="FG WH",
		)
		sles = [
			_sle(name="ss1", voucher_detail_no="s1", item_code="PK", actual_qty=-1200, outgoing_rate=148500, stock_value_difference=-178200000),
			_sle(name="ss2", voucher_detail_no="s2", item_code="RM", actual_qty=-10, outgoing_rate=500000, stock_value_difference=-5000000),
			_sle(name="ss3", voucher_detail_no="s3", item_code="RM2", actual_qty=-100, outgoing_rate=60000000, stock_value_difference=-6000000000),
			_sle(name="sc", voucher_detail_no="c1", item_code="PK", warehouse="Other WH", actual_qty=50, incoming_rate=24550761, outgoing_rate=0, stock_value_difference=1227538050),
			_sle(name="sf", voucher_detail_no="f1", item_code="FG", warehouse="FG WH", actual_qty=fg_qty, incoming_rate=fg_rate, outgoing_rate=0, stock_value_difference=fg_rate * fg_qty),
		]
		native = {
			"classification": HEALTHY,
			"eligible": False,
			"expected_target_rate": fg_rate,
			"reason": "FG already matches consumed-SVD residual rate",
		}
		an = self._run(_se(name="STE-37090"), [src, other, src2, scrap, fg], sles, native=native)
		self.assertEqual(an["classification"], HEALTHY)
		self.assertFalse(an["eligible"])
		self.assertEqual(an.get("exploded_count"), 1)

	def test_exploded_scrap_negative_fg_is_exact(self):
		"""Scrap exploded vs same-item consume and FG is negative → EXACT coupled repair."""
		src = _d(name="s1", item_code="RM", qty=100, basic_rate=10, basic_amount=1000, amount=1000)
		scrap = _d(
			name="c1",
			item_code="RM",
			qty=10,
			basic_rate=850000,
			basic_amount=8500000,
			amount=8500000,
			s_warehouse=None,
			t_warehouse="Scrap WH",
			secondary_item_type="Scrap",
			valuation_type="Valuation Rate",
		)
		fg = _d(
			name="f1",
			item_code="FG",
			qty=90,
			basic_rate=-83322.222,
			basic_amount=-7499000,
			amount=-7499000,
			is_fg=1,
			s_warehouse=None,
			t_warehouse="FG WH",
		)
		sles = [
			_sle(name="ss", voucher_detail_no="s1", item_code="RM", actual_qty=-100, outgoing_rate=10, stock_value_difference=-1000),
			_sle(name="sc", voucher_detail_no="c1", item_code="RM", warehouse="Scrap WH", actual_qty=10, incoming_rate=850000, outgoing_rate=0, stock_value_difference=8500000),
			_sle(name="sf", voucher_detail_no="f1", item_code="FG", warehouse="FG WH", actual_qty=90, incoming_rate=-83322.222, outgoing_rate=0, stock_value_difference=-7499000),
		]
		native = {
			"classification": MANUAL,
			"eligible": False,
			"expected_target_rate": None,
			"reason": "scrap/by-product value 8500000 exceeds consumption 1000",
		}
		an = self._run(_se(name="STE-BOOM"), [src, scrap, fg], sles, native=native)
		self.assertEqual(an["classification"], EXACT)
		self.assertTrue(an["eligible"])
		self.assertEqual(an["exploded_count"], 1)
		self.assertAlmostEqual(an["corrected_scrap_value"], 100.0)  # 10 * consume 10
		self.assertAlmostEqual(an["conservation"]["residual_check"], 0.0, places=6)
		self.assertAlmostEqual(an["fg_plan"]["expected_rate"], (1000 - 100) / 90)
		self.assertEqual(an["strategy"], STRATEGY)

	def test_multiple_source_items_conservation(self):
		src_a = _d(name="s1", item_code="A", qty=10, basic_rate=100, basic_amount=1000, amount=1000)
		src_b = _d(name="s2", item_code="B", qty=5, basic_rate=40, basic_amount=200, amount=200)
		scrap = _d(
			name="c1",
			item_code="A",
			qty=1,
			basic_rate=90000,
			basic_amount=90000,
			amount=90000,
			s_warehouse=None,
			t_warehouse="Scrap WH",
			secondary_item_type="Scrap",
			valuation_type="Valuation Rate",
		)
		fg = _d(
			name="f1",
			item_code="FG",
			qty=14,
			basic_rate=-6342.857,
			basic_amount=-88800,
			amount=-88800,
			is_fg=1,
			s_warehouse=None,
			t_warehouse="FG WH",
		)
		sles = [
			_sle(name="sa", voucher_detail_no="s1", item_code="A", actual_qty=-10, outgoing_rate=100, stock_value_difference=-1000),
			_sle(name="sb", voucher_detail_no="s2", item_code="B", actual_qty=-5, outgoing_rate=40, stock_value_difference=-200),
			_sle(name="sc", voucher_detail_no="c1", item_code="A", warehouse="Scrap WH", actual_qty=1, incoming_rate=90000, outgoing_rate=0, stock_value_difference=90000),
			_sle(name="sf", voucher_detail_no="f1", item_code="FG", warehouse="FG WH", actual_qty=14, incoming_rate=-6342.857, outgoing_rate=0, stock_value_difference=-88800),
		]
		native = {
			"classification": MANUAL,
			"eligible": False,
			"reason": "scrap/by-product value 90000 exceeds consumption 1200",
		}
		an = self._run(_se(name="STE-MS"), [src_a, src_b, scrap, fg], sles, native=native)
		self.assertEqual(an["classification"], EXACT)
		self.assertAlmostEqual(an["corrected_scrap_value"], 100.0)
		self.assertAlmostEqual(an["conservation"]["fg"], 1100.0)
		self.assertAlmostEqual(an["conservation"]["consumed"], 1200.0)

	def test_waiting_upstream_zero_source(self):
		src = _d(name="s1", item_code="RM", qty=10, basic_rate=0, basic_amount=0, amount=0)
		fg = _d(name="f1", item_code="FG", qty=10, basic_rate=0, is_fg=1, s_warehouse=None, t_warehouse="FG WH")
		sles = [
			_sle(name="ss", voucher_detail_no="s1", actual_qty=-10, outgoing_rate=0, stock_value_difference=0),
			_sle(name="sf", voucher_detail_no="f1", item_code="FG", warehouse="FG WH", actual_qty=10, incoming_rate=0, outgoing_rate=0, stock_value_difference=0),
		]
		an = self._run(_se(name="STE-WAIT"), [src, fg], sles, native={"classification": WAITING_UPSTREAM})
		self.assertEqual(an["classification"], WAITING_UPSTREAM)
		self.assertFalse(an["eligible"])

	def test_documented_zero_scrap_not_exploded(self):
		src = _d(name="s1", item_code="RM", qty=10, basic_rate=100, basic_amount=1000, amount=1000)
		scrap = _d(
			name="c1",
			item_code="RM",
			qty=1,
			basic_rate=0,
			basic_amount=0,
			amount=0,
			s_warehouse=None,
			t_warehouse="Scrap WH",
			secondary_item_type="Scrap",
			valuation_type="Valuation Rate",
		)
		fg = _d(
			name="f1",
			item_code="FG",
			qty=9,
			basic_rate=111.111111,
			basic_amount=1000,
			amount=1000,
			is_fg=1,
			s_warehouse=None,
			t_warehouse="FG WH",
		)
		sles = [
			_sle(name="ss", voucher_detail_no="s1", actual_qty=-10, outgoing_rate=100, stock_value_difference=-1000),
			_sle(name="sc", voucher_detail_no="c1", item_code="RM", warehouse="Scrap WH", actual_qty=1, incoming_rate=0, outgoing_rate=0, stock_value_difference=0),
			_sle(name="sf", voucher_detail_no="f1", item_code="FG", warehouse="FG WH", actual_qty=9, incoming_rate=111.111111, outgoing_rate=0, stock_value_difference=1000),
		]
		an = self._run(_se(name="STE-Z"), [src, scrap, fg], sles)
		self.assertEqual(an["classification"], HEALTHY)
		self.assertFalse(any(p.get("exploded") for p in an.get("scrap_plans") or []))

	def test_dry_run_does_not_write(self):
		src = _d(name="s1", item_code="RM", qty=100, basic_rate=10, basic_amount=1000, amount=1000)
		scrap = _d(
			name="c1",
			item_code="RM",
			qty=10,
			basic_rate=850000,
			basic_amount=8500000,
			amount=8500000,
			s_warehouse=None,
			t_warehouse="Scrap WH",
			secondary_item_type="Scrap",
			valuation_type="Valuation Rate",
		)
		fg = _d(
			name="f1",
			item_code="FG",
			qty=90,
			basic_rate=-83322,
			basic_amount=-7499000,
			amount=-7499000,
			is_fg=1,
			s_warehouse=None,
			t_warehouse="FG WH",
		)
		sles = [
			_sle(name="ss", voucher_detail_no="s1", actual_qty=-100, outgoing_rate=10, stock_value_difference=-1000),
			_sle(name="sc", voucher_detail_no="c1", warehouse="Scrap WH", actual_qty=10, incoming_rate=850000, outgoing_rate=0, stock_value_difference=8500000),
			_sle(name="sf", voucher_detail_no="f1", item_code="FG", warehouse="FG WH", actual_qty=90, incoming_rate=-83322, outgoing_rate=0, stock_value_difference=-7499000),
		]
		native = {"classification": MANUAL, "eligible": False, "reason": "scrap/by-product value 8500000 exceeds consumption 1000"}
		se = _se(name="STE-DR")
		by = {s.voucher_detail_no: s for s in sles}
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg._load_voucher",
				return_value=(se, [src, scrap, fg], by),
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg.reconstruct_manufacture_valuation",
				return_value=native,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg.frappe.db.exists",
				return_value=True,
			),
			patch("frappe.db.set_value") as sv,
		):
			out = repair_manufacture_scrap_fg("STE-DR", dry_run=True)
		self.assertTrue(out["ok"])
		self.assertTrue(out["dry_run"])
		sv.assert_not_called()

	def test_not_eligible_is_aborted(self):
		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg.analyze_manufacture_scrap_fg",
				return_value={"classification": HEALTHY, "eligible": False, "reason": "healthy"},
			),
		):
			out = repair_manufacture_scrap_fg("STE-H", dry_run=False)
		self.assertFalse(out["ok"])
		self.assertTrue(out["aborted"])
		self.assertEqual(out["reason"], "healthy")


def run_unit() -> dict:
	import unittest

	suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestManufactureScrapFg)
	from erpnext_extensions.iran_accounting.tests.test_repair_pipeline_v535 import TestRepairPipeline

	suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(TestRepairPipeline))
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	return {
		"ok": result.wasSuccessful(),
		"run": result.testsRun,
		"failures": [str(f[0]) for f in result.failures],
		"errors": [str(e[0]) for e in result.errors],
	}
