# Copyright (c) 2026, ERPNext Extensions contributors

"""Manufacture-native residual valuation contract (v5.3.7+)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock.manufacture_native import (
	HEALTHY,
	LEGITIMATE,
	WAITING_UPSTREAM,
	apply_manufacture_valuation_to_row,
	reconstruct_manufacture_valuation,
)
from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import wrong_rate_bucket


def _detail(**kwargs):
	base = frappe._dict(
		{
			"name": "d1",
			"item_code": "FG",
			"qty": 10,
			"basic_rate": 100,
			"valuation_rate": 100,
			"amount": 1000,
			"basic_amount": 1000,
			"s_warehouse": None,
			"t_warehouse": "Stores",
			"is_fg": 1,
			"is_scrap": 0,
			"allow_zero": 0,
			"secondary_item_type": "",
			"valuation_type": "",
			"additional_cost": 0,
		}
	)
	base.update(kwargs)
	return base


class TestManufactureNativeContract(FrappeTestCase):
	def test_simple_fg_matches_consumed_svd(self):
		se = frappe._dict(
			name="STE-1",
			purpose="Manufacture",
			docstatus=1,
			company="X",
			work_order=None,
			job_card=None,
			posting_date="2026-01-01",
			posting_time="00:00:00",
			total_additional_costs=0,
		)
		src = _detail(
			name="s1",
			item_code="RM",
			qty=10,
			basic_rate=100,
			amount=1000,
			basic_amount=1000,
			s_warehouse="Paykar",
			t_warehouse=None,
			is_fg=0,
		)
		fg = _detail(name="f1", item_code="FG", qty=10, basic_rate=100, amount=1000, basic_amount=1000, is_fg=1)
		sle_src = frappe._dict(
			name="sle-s",
			voucher_detail_no="s1",
			item_code="RM",
			warehouse="Paykar",
			actual_qty=-10,
			incoming_rate=0,
			outgoing_rate=100,
			valuation_rate=100,
			stock_value_difference=-1000,
			stock_value=0,
		)
		sle_fg = frappe._dict(
			name="sle-f",
			voucher_detail_no="f1",
			item_code="FG",
			warehouse="Stores",
			actual_qty=10,
			incoming_rate=100,
			outgoing_rate=0,
			valuation_rate=100,
			stock_value_difference=1000,
			stock_value=1000,
		)
		with (
			patch("frappe.db.exists", return_value=True),
			patch("frappe.db.get_value", return_value=se),
			patch("frappe.db.sql", side_effect=[[src, fg], [sle_src, sle_fg]]),
		):
			ev = reconstruct_manufacture_valuation("STE-1")
		self.assertEqual(ev["classification"], HEALTHY)
		self.assertFalse(ev["eligible"])
		self.assertAlmostEqual(flt(ev["expected_target_rate"]), 100.0)

	def test_scrap_and_additional_cost_keep_basic_healthy(self):
		"""basic_rate excludes additional; SLE amount includes FG additional_cost."""
		se = frappe._dict(
			name="STE-2",
			purpose="Manufacture",
			docstatus=1,
			company="X",
			work_order=None,
			job_card=None,
			posting_date="2026-01-01",
			posting_time="00:00:00",
			total_additional_costs=200,
		)
		src = _detail(
			name="s1",
			item_code="RM",
			qty=10,
			basic_rate=100,
			amount=1000,
			basic_amount=1000,
			s_warehouse="Paykar",
			t_warehouse=None,
			is_fg=0,
		)
		scrap = _detail(
			name="c1",
			item_code="SCRAP",
			qty=1,
			basic_rate=50,
			amount=50,
			basic_amount=50,
			s_warehouse=None,
			t_warehouse="Scrap WH",
			is_fg=0,
			is_scrap=0,
			secondary_item_type="Scrap",
		)
		fg = _detail(
			name="f1",
			item_code="FG",
			qty=9,
			basic_rate=105.555555,  # (1000-50)/9
			amount=1150,  # basic + additional
			basic_amount=950,
			is_fg=1,
			additional_cost=200,
		)
		# Use exact ERPNext residual basic
		fg.basic_rate = (1000 - 50) / 9
		fg.basic_amount = 1000 - 50
		fg.amount = fg.basic_amount + 200
		sle_src = frappe._dict(
			name="sle-s",
			voucher_detail_no="s1",
			actual_qty=-10,
			outgoing_rate=100,
			incoming_rate=0,
			stock_value_difference=-1000,
			stock_value=0,
			valuation_rate=100,
			item_code="RM",
			warehouse="Paykar",
		)
		sle_scrap = frappe._dict(
			name="sle-c",
			voucher_detail_no="c1",
			actual_qty=1,
			incoming_rate=50,
			outgoing_rate=0,
			stock_value_difference=50,
			stock_value=50,
			valuation_rate=50,
			item_code="SCRAP",
			warehouse="Scrap WH",
		)
		sle_fg = frappe._dict(
			name="sle-f",
			voucher_detail_no="f1",
			actual_qty=9,
			incoming_rate=fg.amount / 9,
			outgoing_rate=0,
			stock_value_difference=fg.amount,
			stock_value=fg.amount,
			valuation_rate=fg.amount / 9,
			item_code="FG",
			warehouse="Stores",
		)
		with (
			patch("frappe.db.exists", return_value=True),
			patch("frappe.db.get_value", return_value=se),
			patch("frappe.db.sql", side_effect=[[src, scrap, fg], [sle_src, sle_scrap, sle_fg]]),
		):
			ev = reconstruct_manufacture_valuation("STE-2")
		self.assertEqual(ev["classification"], HEALTHY)
		self.assertAlmostEqual(flt(ev["expected_target_rate"]), flt(fg.basic_rate), places=4)

	def test_zero_source_is_waiting_upstream(self):
		se = frappe._dict(
			name="STE-3",
			purpose="Manufacture",
			docstatus=1,
			company="X",
			work_order="WO-1",
			job_card=None,
			posting_date="2026-01-01",
			posting_time="00:00:00",
			total_additional_costs=0,
		)
		src = _detail(
			name="s1",
			item_code="RM",
			qty=5,
			basic_rate=0,
			amount=0,
			basic_amount=0,
			s_warehouse="Paykar",
			t_warehouse=None,
			is_fg=0,
			allow_zero=0,
		)
		fg = _detail(name="f1", is_fg=1, basic_rate=0, amount=0, basic_amount=0)
		sle_src = frappe._dict(
			name="sle-s",
			voucher_detail_no="s1",
			actual_qty=-5,
			outgoing_rate=0,
			incoming_rate=0,
			stock_value_difference=0,
			stock_value=0,
			valuation_rate=0,
			item_code="RM",
			warehouse="Paykar",
		)
		sle_fg = frappe._dict(
			name="sle-f",
			voucher_detail_no="f1",
			actual_qty=5,
			incoming_rate=0,
			outgoing_rate=0,
			stock_value_difference=0,
			stock_value=0,
			valuation_rate=0,
			item_code="FG",
			warehouse="Stores",
		)
		with (
			patch("frappe.db.exists", return_value=True),
			patch("frappe.db.get_value", return_value=se),
			patch("frappe.db.sql", side_effect=[[src, fg], [sle_src, sle_fg]]),
		):
			ev = reconstruct_manufacture_valuation("STE-3")
		self.assertEqual(ev["classification"], WAITING_UPSTREAM)
		self.assertFalse(ev["eligible"])

	def test_kpi_bucket_marks_healthy_complete(self):
		row = apply_manufacture_valuation_to_row(
			{
				"purpose": "Manufacture",
				"voucher": "STE-1",
				"item": "FG",
			},
			cache={
				"mfg_native:STE-1": {
					"classification": HEALTHY,
					"eligible": False,
					"reason": "ok",
					"source_of_truth": "historical_manufacture_consumed_svd",
				}
			},
		)
		self.assertTrue(row.get("no_action_required"))
		self.assertEqual(wrong_rate_bucket(row), "complete")

	def test_kpi_bucket_marks_legitimate_complete(self):
		row = {
			"no_action_required": True,
			"manufacture_native": {"classification": LEGITIMATE},
			"planner_status": "MANUAL",
		}
		self.assertEqual(wrong_rate_bucket(row), "complete")


if __name__ == "__main__":
	unittest.main()
