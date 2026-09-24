# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.5 — simple Historical Repair invariants (no extra repair modes)."""

from __future__ import annotations

import unittest

from erpnext_extensions.iran_accounting.historical_stock.job_card_flow import (
	JC_BALANCED,
	JC_BROKEN,
	JC_OPEN_VALID,
	classify_job_card_equation,
	warehouse_is_paykar,
)
from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import leftover_ma_receipt_eligible
from erpnext_extensions.iran_accounting.historical_stock.simple_model import (
	FAMILY_MANUFACTURE_FLOW,
	FAMILY_VALUATION,
	PRIMARY_FAILED,
	PRIMARY_LEGITIMATE,
	PRIMARY_MANUAL,
	PRIMARY_READY,
	PRIMARY_REPAIRED,
	PRIMARY_WAITING,
	PRIMARY_STATES,
	ROOT_FAMILIES,
	annotate_row,
	lifecycle,
	primary_state,
	project_simple_kpis,
	root_family,
)


class TestPrimaryModel(unittest.TestCase):
	def test_six_states_five_families(self):
		self.assertEqual(len(PRIMARY_STATES), 6)
		self.assertEqual(len(ROOT_FAMILIES), 5)
		self.assertEqual(lifecycle(), ("SCAN", "ROOT_CAUSE", "DRY_RUN", "REPAIR", "REPOST", "VERIFY"))

	def test_status_mapping(self):
		self.assertEqual(primary_state({"status": "READY_LEFTOVER_MA"}), PRIMARY_READY)
		self.assertEqual(primary_state({"planner_status": "WAITING_PATIENT_ZERO"}), PRIMARY_WAITING)
		self.assertEqual(primary_state({"status": "NO_ACTION"}), PRIMARY_LEGITIMATE)
		self.assertEqual(primary_state({"leftover_ma_status": "NO_ACTION"}), PRIMARY_LEGITIMATE)
		self.assertEqual(primary_state({"status": "LEFTOVER_MA_REPAIRED"}), PRIMARY_REPAIRED)
		self.assertEqual(primary_state({"status": "FAILED_POSTCONDITION"}), PRIMARY_FAILED)
		self.assertEqual(primary_state({"status": "MANUAL_REVIEW"}), PRIMARY_MANUAL)

	def test_leftover_ma_is_valuation_not_a_workflow(self):
		row = annotate_row({"topic": "LEFTOVER_MA", "status": "READY_LEFTOVER_MA"})
		self.assertEqual(row["primary_state"], PRIMARY_READY)
		self.assertEqual(row["root_family"], FAMILY_VALUATION)
		self.assertEqual(row["reason_code"], "LEFTOVER_MA")

	def test_manufacture_family(self):
		self.assertEqual(root_family({"topic": "MANUFACTURE", "status": "MANUAL"}), FAMILY_MANUFACTURE_FLOW)

	def test_simple_kpis(self):
		dash = project_simple_kpis(
			{
				"Repairable": 4,
				"Wrong Rate MANUAL": 2,
				"Manual": 1,
				"Proven Legitimate Zero": 3,
				"Failed RIV Actionable": 5,
				"Wrong Rate WAITING": 7,
			}
		)
		self.assertEqual(dash["Ready to Repair"], 4)
		self.assertGreaterEqual(dash["Needs Review"], 2)
		self.assertGreaterEqual(dash["Legitimate / No Action"], 3)
		self.assertEqual(dash["Failed"], 5)
		self.assertEqual(dash["Blocked"], 7)


class TestLeftoverMaManufactureExclusion(unittest.TestCase):
	def test_material_receipt_on_consumables_is_eligible(self):
		ok, reason = leftover_ma_receipt_eligible(
			voucher_type="Stock Entry",
			purpose="Material Receipt",
			warehouse="انبار ملزومات مصرفی اسپاد",
		)
		self.assertTrue(ok)
		self.assertEqual(reason, "")

	def test_manufacture_output_is_blocked(self):
		ok, reason = leftover_ma_receipt_eligible(
			voucher_type="Stock Entry",
			purpose="Manufacture",
			warehouse="انبار قرنطینه",
		)
		self.assertFalse(ok)
		self.assertEqual(reason, "MANUFACTURE_FLOW")

	def test_mtfm_is_blocked(self):
		ok, reason = leftover_ma_receipt_eligible(
			voucher_type="Stock Entry",
			purpose="Material Transfer for Manufacture",
			warehouse="پایکار",
		)
		self.assertFalse(ok)
		self.assertEqual(reason, "MANUFACTURE_FLOW")

	def test_scrap_and_reject_warehouses_blocked(self):
		ok, reason = leftover_ma_receipt_eligible(
			voucher_type="Stock Entry",
			purpose="Material Receipt",
			warehouse="انبار Reject محصولات نیمه ساخته",
		)
		self.assertFalse(ok)
		self.assertEqual(reason, "MANUFACTURE_FLOW")
		ok, reason = leftover_ma_receipt_eligible(
			voucher_type="Stock Entry",
			purpose="Material Receipt",
			warehouse="انبار ضایعات اقلام اسپاد",
		)
		self.assertFalse(ok)
		self.assertEqual(reason, "MANUFACTURE_FLOW")

	def test_scrap_and_finished_flags_blocked(self):
		ok, reason = leftover_ma_receipt_eligible(
			voucher_type="Stock Entry",
			purpose="Material Receipt",
			warehouse="انبار ملزومات مصرفی اسپاد",
			is_scrap_item=1,
		)
		self.assertFalse(ok)
		ok, reason = leftover_ma_receipt_eligible(
			voucher_type="Stock Entry",
			purpose="Material Receipt",
			warehouse="انبار ملزومات مصرفی اسپاد",
			is_finished_item=1,
		)
		self.assertFalse(ok)

	def test_purchase_receipt_allowed(self):
		ok, reason = leftover_ma_receipt_eligible(
			voucher_type="Purchase Receipt",
			warehouse="Stores",
		)
		self.assertTrue(ok)


class TestJobCardEquation(unittest.TestCase):
	def test_completed_1000_equals_100_850_50(self):
		out = classify_job_card_equation(
			transferred=1000, returned=100, consumed=850, scrap=50, completed=True
		)
		self.assertEqual(out["status"], JC_BALANCED)
		self.assertEqual(out["residual"], 0)

	def test_open_valid_paykar_remainder(self):
		out = classify_job_card_equation(
			transferred=1000,
			returned=100,
			consumed=400,
			scrap=50,
			remaining=450,
			completed=False,
		)
		self.assertEqual(out["status"], JC_OPEN_VALID)
		self.assertEqual(out["remaining"], 450)

	def test_completed_imbalance_is_broken(self):
		out = classify_job_card_equation(
			transferred=1000, returned=0, consumed=850, scrap=50, completed=True
		)
		self.assertEqual(out["status"], JC_BROKEN)

	def test_open_over_consumption_is_broken(self):
		out = classify_job_card_equation(
			transferred=100, returned=0, consumed=150, scrap=0, remaining=0, completed=False
		)
		self.assertEqual(out["status"], JC_BROKEN)

	def test_unlinked_is_manual(self):
		out = classify_job_card_equation(
			transferred=1000, returned=100, consumed=850, scrap=50, completed=True, linked=False
		)
		self.assertEqual(out["status"], "MANUAL")

	def test_paykar_token(self):
		self.assertTrue(warehouse_is_paykar("انبار پایکار تولید"))
		self.assertFalse(warehouse_is_paykar("انبار ملزومات مصرفی اسپاد"))
