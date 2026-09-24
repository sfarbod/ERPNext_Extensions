# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.5 — simple Historical Repair invariants (no extra repair modes)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

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

	def test_shared_paykar_bin_caps_to_equation_remainder(self):
		"""Bin over-count across open Job Cards must not force MANUAL."""
		from unittest.mock import patch

		from erpnext_extensions.iran_accounting.historical_stock.job_card_flow import (
			reconstruct_job_card_flow,
		)

		jc = SimpleNamespace(
			name="JC-OPEN-1",
			status="Work In Progress",
			work_order="WO-1",
			docstatus=0,
			for_quantity=100,
			total_completed_qty=0,
			company="C",
		)
		se = SimpleNamespace(
			name="STE-T",
			purpose="Material Transfer for Manufacture",
			is_return=0,
			work_order="WO-1",
			job_card="JC-OPEN-1",
		)
		detail = SimpleNamespace(
			item_code="RM1",
			qty=1000,
			s_warehouse="Approved",
			t_warehouse="انبار پایکار خط تولید",
			is_scrap_item=0,
			is_finished_item=0,
		)

		def gv(doctype, name=None, fieldname=None, as_dict=False, **kwargs):
			# signatures: get_value(dt, name, fields, as_dict=) OR get_value(dt, filters, fieldname)
			if doctype == "Job Card":
				return jc
			if doctype == "Bin":
				return 50000.0
			return None

		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.job_card_flow.frappe.db.get_value",
				side_effect=gv,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.job_card_flow._stock_entries_for_job_card",
				return_value=([se], True),
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.job_card_flow.frappe.db.sql",
				return_value=[detail],
			),
		):
			out = reconstruct_job_card_flow("JC-OPEN-1")
		self.assertTrue(out.get("shared_paykar_bin"))
		self.assertEqual(out["status"], JC_OPEN_VALID)
		self.assertAlmostEqual(out["remaining"], 1000.0)
		self.assertAlmostEqual(out["residual"], 0.0)


class TestManufactureWrongRateProvenance(unittest.TestCase):
	def test_manufacture_refuses_previous_healthy_exact(self):
		from unittest.mock import patch

		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

		raw = type(
			"R",
			(),
			{
				"name": "sed1",
				"parent": "STE-MFG-1",
				"item_code": "FG1",
				"qty": 10,
				"basic_rate": 0,
				"valuation_rate": 0,
				"amount": 0,
				"s_warehouse": None,
				"t_warehouse": "FG-WH",
				"batch_no": None,
				"purpose": "Manufacture",
				"posting_date": "2026-01-01",
				"posting_time": "10:00:00",
				"company": "C",
				"allow_zero_valuation_rate": 0,
				"is_finished_item": 1,
				"is_scrap_item": 0,
				"serial_and_batch_bundle": None,
			},
		)()

		def fake_classify(raw_row, _cache=None):
			return {
				"voucher": "STE-MFG-1",
				"voucher_detail": "sed1",
				"item": "FG1",
				"qty": 10,
				"current_rate": 0,
				"proposed_rate": 3107,
				"expected": 3107,
				"confidence": "EXACT",
				"eligible": True,
				"status": "RECONSTRUCTABLE",
				"source_of_truth": "previous_healthy_sle",
				"purpose": "Manufacture",
				"is_finished_item": 1,
				"flags": ["ZERO_BASIC_RATE"],
			}

		with (
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.wrong_rate._scan_se_flags",
				return_value=[raw],
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.wrong_rate._scan_sle_flags",
				return_value=[],
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.wrong_rate.classify_zero_row",
				side_effect=fake_classify,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.manufacture.preview_manufacture_voucher",
				return_value={
					"status": "HEALTHY",
					"eligible": False,
					"expected_target_rate": 0,
					"input_health": {"status": "healthy"},
				},
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.wrong_rate._paired_sle_rate",
				return_value=0,
			),
			patch(
				"erpnext_extensions.iran_accounting.historical_stock.wrong_rate._force_include_patient_zeros",
				lambda *a, **k: None,
			),
		):
			scan = scan_wrong_rates(voucher="STE-MFG-1", limit=10)
		rows = scan.get("rows") or []
		self.assertTrue(rows)
		row = rows[0]
		self.assertNotEqual(row.get("confidence"), "EXACT")
		self.assertFalse(row.get("eligible"))
		self.assertEqual(row.get("wrong_reason"), "MANUAL_MANUFACTURE_GENERIC_RATE")


if __name__ == "__main__":
	unittest.main()
