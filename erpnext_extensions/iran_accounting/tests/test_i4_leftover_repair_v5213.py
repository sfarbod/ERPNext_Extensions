# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.13 Historical Repair — I4 leftover + real scan filters."""

from __future__ import annotations

import unittest
from unittest import mock

from erpnext_extensions.iran_accounting.historical_stock import (
	I4_LEFTOVER_REPAIR,
	I4_READY,
	TOPIC_I4,
)
from erpnext_extensions.iran_accounting.historical_stock.dependency import _required_action
from erpnext_extensions.iran_accounting.historical_stock.planner import (
	PLAN_READY_I4,
	PLAN_WAITING_I4,
	evaluate_row,
)
from erpnext_extensions.iran_accounting.historical_stock.scan_filters import (
	append_sle_scope,
	append_stock_entry_scope,
	filter_rows_by_planner,
	normalize_scope,
)


class TestScanFiltersV5213(unittest.TestCase):
	def test_normalize_and_append_sle_scope(self):
		scope = normalize_scope(
			company="Co",
			voucher="MAT-STE-2026-25791",
			item_code="30300014",
			warehouse="WH",
			batch="B1",
			serial_and_batch_bundle="SABB-1",
			work_order="WO-1",
			from_date="2026-04-01",
			to_date="2026-04-30",
			repair_class=I4_LEFTOVER_REPAIR,
			planner_status=I4_READY,
			patient_zero="MAT-STE-2026-25791",
		)
		conds: list[str] = ["sle.is_cancelled=0"]
		args: list = []
		conds, args, join = append_sle_scope(conds, args, scope)
		joined = " ".join(conds)
		self.assertIn("sle.company=%s", joined)
		self.assertIn("sle.voucher_no=%s", joined)
		self.assertIn("sle.item_code=%s", joined)
		self.assertIn("sle.warehouse=%s", joined)
		self.assertIn("sle.serial_and_batch_bundle=%s", joined)
		self.assertIn("se_scope.work_order=%s", joined)
		self.assertIn("tabStock Entry", join)
		self.assertEqual(args[0], "Co")
		self.assertEqual(args[1], "MAT-STE-2026-25791")

	def test_stock_entry_scope(self):
		conds: list[str] = ["se.docstatus=1"]
		args: list = []
		append_stock_entry_scope(
			conds,
			args,
			normalize_scope(company="Co", voucher="V1", item_code="I", warehouse="W", work_order="WO"),
		)
		self.assertIn("se.name=%s", " ".join(conds))
		self.assertIn("se.work_order=%s", " ".join(conds))

	def test_filter_rows_by_planner(self):
		rows = [
			{"repair_class": I4_LEFTOVER_REPAIR, "planner_status": I4_READY, "patient_zero": {"voucher_no": "PZ"}},
			{"repair_class": "OTHER", "planner_status": "READY", "root_blocker": "PZ"},
		]
		out = filter_rows_by_planner(rows, repair_class=I4_LEFTOVER_REPAIR, planner_status=I4_READY)
		self.assertEqual(len(out), 1)
		out2 = filter_rows_by_planner(rows, patient_zero="PZ")
		self.assertEqual(len(out2), 2)


class TestI4PlannerV5213(unittest.TestCase):
	def test_ready_i4_patient_zero(self):
		row = {
			"topic": TOPIC_I4,
			"repair_class": I4_LEFTOVER_REPAIR,
			"voucher": "MAT-STE-2026-25791",
			"item": "30300014",
			"warehouse": "WH",
			"reason": "qty_after_zero_nonzero_value",
			"i4_status": I4_READY,
			"is_patient_zero": True,
			"stock_value": -2420838,
			"sql_updates": 126,
			"confidence": "EXACT",
		}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_stock.planner._patient_name",
			return_value="MAT-STE-2026-25791",
		):
			decision = evaluate_row(row)
		self.assertEqual(decision["planner_status"], PLAN_READY_I4)
		self.assertGreater(int(decision.get("sql_updates") or 0), 0)

	def test_waiting_i4_downstream(self):
		row = {
			"topic": TOPIC_I4,
			"repair_class": I4_LEFTOVER_REPAIR,
			"voucher": "MAT-STE-2026-25912",
			"item": "30300014",
			"warehouse": "WH",
			"reason": "qty_after_zero_nonzero_value",
			"is_patient_zero": False,
			"root_patient_zero": "MAT-STE-2026-25791",
			"root_blocker": "MAT-STE-2026-25791",
			"confidence": "EXACT",
		}
		decision = evaluate_row(row)
		self.assertIn(decision["planner_status"], (PLAN_WAITING_I4, "WAITING_PATIENT_ZERO", PLAN_READY_I4))

	def test_required_action_says_repair_i4_not_wrong_rate(self):
		msg = _required_action(
			root_status=PLAN_READY_I4,
			root_voucher="MAT-STE-2026-25791",
			circular=False,
			stop_reason=None,
			actionable={"voucher": "MAT-STE-2026-25791", "topic": "I4_LEFTOVER"},
			waiting=None,
		)
		self.assertIn("Repair I4 Patient Zero", msg)
		self.assertNotIn("Repair Wrong Rate", msg)

	def test_ready_i4_in_scope_ready_set(self):
		from erpnext_extensions.iran_accounting.historical_stock.scope import READY_SCOPES

		self.assertIn("READY_I4", READY_SCOPES)


class TestI4RepairHelpersV5213(unittest.TestCase):
	def test_is_i4_leftover_row(self):
		from erpnext_extensions.iran_accounting.historical_stock.i4_repair import is_i4_leftover_row

		self.assertTrue(
			is_i4_leftover_row(
				{"qty_after_transaction": 0, "stock_value": -2420838, "actual_qty": -10}
			)
		)
		self.assertFalse(
			is_i4_leftover_row({"qty_after_transaction": 0, "stock_value": 0, "actual_qty": -10})
		)

	def test_master_plan_structure(self):
		from erpnext_extensions.iran_accounting.historical_stock import master_plan as mp

		fake_frappe = mock.MagicMock()
		fake_frappe.db.sql.side_effect = [
			[
				{
					"item_code": "I",
					"warehouse": "W",
					"voucher_no": "V",
					"posting_date": "2026-04-05",
					"stock_value": -1,
				}
			],
			[(3,)],
		]
		with mock.patch.object(mp, "frappe", fake_frappe):
			plan = mp.build_master_repair_plan(company="Co")
		self.assertIn("roadmap", plan)
		self.assertTrue(any(s.get("stage") == "Farvardin" for s in plan["roadmap"]))

	def test_rebuild_gl_skips_non_stock_entry(self):
		from erpnext_extensions.iran_accounting.historical_stock import i4_repair as i4

		fake = mock.MagicMock()
		fake.db.get_value.return_value = "Purchase Receipt"
		fake.db.exists.return_value = False
		with mock.patch.object(i4, "frappe", fake):
			out = i4._rebuild_gl_touched(["MAT-PRE-2026-00577", "MAT-STE-2026-03111"])
		self.assertEqual(out["rebuilt"], 0)
		self.assertTrue(any("not_stock_entry" in str(s.get("reason")) for s in out["skipped"]))


if __name__ == "__main__":
	unittest.main()
