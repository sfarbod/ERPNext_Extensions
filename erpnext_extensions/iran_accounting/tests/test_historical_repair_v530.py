# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit + incident regression tests — Historical Repair Master Plan V2 (v5.3.0)."""

from __future__ import annotations

import math
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_EXACT,
	POISON_RATE,
	STATUS_RATE_REBUILD_COMPLETE,
	STATUS_RECONSTRUCTABLE,
	STATUS_VALUATION_POISON_DEPENDENCY,
)
from erpnext_extensions.iran_accounting.historical_stock.authoritative_rate import (
	is_authoritative_healthy_rate,
	is_poison_rate,
	manufacture_after_rates_healthy,
	rate_integrity_reason,
	reclassify_false_complete_row,
	refuse_false_rate_rebuild_complete,
)
from erpnext_extensions.iran_accounting.historical_stock.root_graph import (
	detect_dependency_cycle,
	group_root_vs_downstream,
)
from erpnext_extensions.iran_accounting.historical_stock.planner import (
	PLAN_RATE_REPAIR_COMPLETE,
	evaluate_row,
)


class TestAuthoritativeRate(unittest.TestCase):
	def test_poison_ceiling(self):
		self.assertTrue(is_poison_rate(POISON_RATE))
		self.assertTrue(is_poison_rate(POISON_RATE * 2))
		self.assertFalse(is_poison_rate(1e11))
		self.assertTrue(is_poison_rate(float("nan")))
		self.assertTrue(is_poison_rate(float("inf")))

	def test_negative_not_authoritative(self):
		self.assertFalse(is_authoritative_healthy_rate(-100))
		self.assertEqual(rate_integrity_reason(-5e9), "NEGATIVE_RATE")

	def test_refuse_false_complete_on_matched_poison(self):
		# Incident: SE+SLE both -5.8e9 marked RATE_REBUILD_COMPLETE.
		refusal = refuse_false_rate_rebuild_complete(
			current_rate=-5825110527.108651,
			expected_rate=-5825110527.108651,
			source="already_valued",
			status=STATUS_RATE_REBUILD_COMPLETE,
		)
		self.assertIsNotNone(refusal)
		self.assertTrue(refusal["false_complete"])
		self.assertIn(refusal["blocker"], ("NEGATIVE_RATE", "EXPLODED_RATE"))

	def test_true_complete_still_allowed(self):
		refusal = refuse_false_rate_rebuild_complete(
			current_rate=148500.0,
			expected_rate=148500.0,
			source="already_valued",
			status=STATUS_RATE_REBUILD_COMPLETE,
		)
		self.assertIsNone(refusal)

	def test_reclassify_false_complete_row(self):
		row = reclassify_false_complete_row(
			{
				"status": STATUS_RATE_REBUILD_COMPLETE,
				"source_of_truth": "already_valued",
				"current_rate": 1e15,
				"proposed_rate": 1e15,
			}
		)
		self.assertTrue(row.get("false_rate_rebuild_complete"))
		self.assertNotEqual(row.get("status"), STATUS_RATE_REBUILD_COMPLETE)
		self.assertEqual(row.get("source_of_truth"), "requires_authoritative_reconstruction")


class TestManufactureConfidenceV530(unittest.TestCase):
	def test_after_healthy_promotes_despite_fg_neg_before(self):
		changed = [
			{
				"item": "20100067",
				"before": {"basic_rate": -5e9, "amount": -5e9, "is_finished_item": 1},
				"after": {"basic_rate": 3944832.0, "amount": 3944832.0 * 356, "is_finished_item": 1},
			},
			{
				"item": "13200023",
				"before": {"basic_rate": 1.5e12, "amount": 1.5e12, "is_finished_item": 0},
				"after": {"basic_rate": 3505.0, "amount": 3505.0 * 25, "is_finished_item": 0},
			},
		]
		self.assertTrue(manufacture_after_rates_healthy(changed, fg_negative_before=True))

	def test_after_still_poison_refuses(self):
		changed = [
			{
				"item": "20100067",
				"before": {"basic_rate": -1, "amount": -1, "is_finished_item": 1},
				"after": {"basic_rate": -2, "amount": -2, "is_finished_item": 1},
			}
		]
		self.assertFalse(manufacture_after_rates_healthy(changed, fg_negative_before=True))

	def test_preview_manufacture_exact_on_healthy_after(self):
		from erpnext_extensions.iran_accounting.historical_stock.manufacture import (
			preview_manufacture_voucher,
		)

		class Row:
			def __init__(self, **kw):
				self.__dict__.update(kw)

		before_rows = [
			Row(
				idx=1,
				name="r1",
				item_code="FG",
				qty=10,
				basic_rate=-1000,
				basic_amount=-10000,
				valuation_rate=-1000,
				amount=-10000,
				additional_cost=0,
				is_finished_item=1,
				secondary_item_type=None,
				s_warehouse=None,
				t_warehouse="WH-FG",
			),
			Row(
				idx=2,
				name="r2",
				item_code="RM",
				qty=10,
				basic_rate=100,
				basic_amount=1000,
				valuation_rate=100,
				amount=1000,
				additional_cost=0,
				is_finished_item=0,
				secondary_item_type=None,
				s_warehouse="WH-RM",
				t_warehouse=None,
			),
		]

		class Doc:
			def __init__(self):
				self.items = [Row(**r.__dict__) for r in before_rows]

			def get(self, key, default=None):
				if key == "items":
					return self.items
				return default

		def fake_contract(doc):
			fg = doc.items[0]
			fg.basic_rate = 100
			fg.basic_amount = 1000
			fg.valuation_rate = 100
			fg.amount = 1000
			return True

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.manufacture.frappe"
		) as frappe_mod, patch(
			"erpnext_extensions.iran_accounting.historical_stock.manufacture.apply_iran_manufacture_output_contract",
			side_effect=fake_contract,
		):
			frappe_mod.get_doc.return_value = Doc()
			out = preview_manufacture_voucher("MAT-STE-TEST")
		self.assertTrue(out.get("fg_negative"))
		self.assertTrue(out.get("after_rates_healthy"))
		self.assertEqual(out.get("confidence"), CONFIDENCE_EXACT)
		self.assertEqual(out.get("status"), STATUS_RECONSTRUCTABLE)
		self.assertTrue(out.get("eligible"))

	def test_zero_rm_does_not_veto_healthy_after(self):
		"""Rule 3 — negative FG with healthy AFTER stays EXACT even if another RM row is zero."""
		from erpnext_extensions.iran_accounting.historical_stock.manufacture import (
			preview_manufacture_voucher,
		)

		class Row:
			def __init__(self, **kw):
				self.__dict__.update(kw)

		class Doc:
			def __init__(self):
				self.items = [
					Row(
						idx=1, name="fg", item_code="FG", qty=10, basic_rate=-50, basic_amount=-500,
						valuation_rate=-50, amount=-500, additional_cost=0, is_finished_item=1,
						secondary_item_type=None, s_warehouse=None, t_warehouse="WH-FG",
					),
					Row(
						idx=2, name="rm", item_code="RM", qty=1, basic_rate=0, basic_amount=0,
						valuation_rate=0, amount=0, additional_cost=0, is_finished_item=0,
						secondary_item_type=None, s_warehouse="WH-RM", t_warehouse=None,
					),
				]

			def get(self, key, default=None):
				return self.items if key == "items" else default

		def fake_contract(doc):
			doc.items[0].basic_rate = 200
			doc.items[0].basic_amount = 2000
			doc.items[0].valuation_rate = 200
			doc.items[0].amount = 2000
			return True

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.manufacture.frappe"
		) as frappe_mod, patch(
			"erpnext_extensions.iran_accounting.historical_stock.manufacture.apply_iran_manufacture_output_contract",
			side_effect=fake_contract,
		):
			frappe_mod.get_doc.return_value = Doc()
			out = preview_manufacture_voucher("MAT-STE-ZERO-RM")
		self.assertTrue(out.get("after_rates_healthy"))
		self.assertTrue(out.get("zero_rm_present"))
		self.assertEqual(out.get("confidence"), CONFIDENCE_EXACT)
		self.assertEqual(out.get("status"), STATUS_RECONSTRUCTABLE)


class TestSEToSLESync(unittest.TestCase):
	def test_sync_sle_from_se_detail_writes_svd(self):
		from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
			sync_sle_from_stock_entry_detail,
		)

		sle = SimpleNamespace(
			name="SLE1",
			voucher_detail_no="D1",
			item_code="FG",
			actual_qty=10,
			incoming_rate=-1e9,
			outgoing_rate=0,
			valuation_rate=-1e9,
			stock_value_difference=-1e10,
			amount=5000,
			basic_amount=5000,
			qty=10,
			basic_rate=500,
			se_valuation_rate=500,
		)
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild.frappe"
		) as frappe_mod:
			frappe_mod.db.sql.return_value = [sle]
			written = sync_sle_from_stock_entry_detail("STE-1")
		self.assertEqual(len(written), 1)
		frappe_mod.db.set_value.assert_called()
		args = frappe_mod.db.set_value.call_args
		values = args[0][2]
		self.assertAlmostEqual(values["incoming_rate"], 500.0)
		self.assertAlmostEqual(values["stock_value_difference"], 5000.0)


class TestPlannerFalseComplete(unittest.TestCase):
	@patch(
		"erpnext_extensions.iran_accounting.historical_stock.planner._write_counts",
		return_value={"sql": 1, "sle": 1, "se": 1, "sabb": 0, "sbe": 0, "bin": 0},
	)
	def test_evaluate_rate_refuses_poison_complete(self, _counts):
		row = {
			"topic": "WRONG_RATE",
			"voucher": "MAT-STE-POISON",
			"item": "20100067",
			"warehouse": "WH",
			"status": STATUS_RATE_REBUILD_COMPLETE,
			"source_of_truth": "already_valued",
			"current_rate": -5e9,
			"proposed_rate": -5e9,
			"confidence": CONFIDENCE_EXACT,
			"eligible": False,
		}
		decision = evaluate_row(row)
		# Must NOT be PLAN_RATE_REPAIR_COMPLETE.
		self.assertNotEqual(decision.get("planner_status"), PLAN_RATE_REPAIR_COMPLETE)
		self.assertTrue(decision.get("false_rate_rebuild_complete") or decision.get("blocked"))


class TestRootGraph(unittest.TestCase):
	def test_group_root_vs_downstream(self):
		rows = [
			{"voucher": "A", "item": "I", "warehouse": "W", "status": "READY"},
			{"voucher": "B", "item": "I", "warehouse": "W", "patient_zero": "A", "status": "WAITING"},
			{"voucher": "C", "item": "I2", "warehouse": "W", "patient_zero": {"voucher_no": "A"}, "status": "WAITING"},
		]
		g = group_root_vs_downstream(rows, repair_class="WRONG_RATE")
		self.assertGreaterEqual(g["root_count"], 1)
		self.assertGreaterEqual(g["downstream_count"], 1)

	def test_detect_cycle(self):
		rows = [
			{"voucher": "A", "patient_zero": "B"},
			{"voucher": "B", "patient_zero": "A"},
		]
		cycles = detect_dependency_cycle(rows)
		self.assertTrue(cycles)


class TestRIVPreflightUnit(unittest.TestCase):
	@patch("erpnext_extensions.iran_accounting.historical_stock.riv_preflight.frappe")
	@patch("erpnext_extensions.iran_accounting.historical_stock.riv_preflight.analyze_dependency_closure")
	@patch("erpnext_extensions.iran_accounting.historical_stock.riv_preflight._expected_gl_postable")
	def test_preflight_blocks_poison_closure(self, gl_state, closure, frappe_mod):
		from erpnext_extensions.iran_accounting.historical_stock.riv_preflight import (
			preview_repost_impact,
		)

		frappe_mod.db.sql.return_value = []
		closure.return_value = {
			"direct_vouchers": ["STE-1"],
			"stock_entries": ["STE-1"],
			"manufacture_vouchers": ["STE-1"],
			"items": ["13100134", "20100067"],
			"expanded_items": ["20100067"],
			"poison_vouchers": [
				{"voucher": "STE-1", "item": "20100067", "reason": "EXPLODED_RATE", "rate": 1e15}
			],
			"manufacture_with_poison_components": True,
			"counts": {"poison": 1},
		}
		gl_state.return_value = {"nonempty": True, "postable": True, "diff": 0, "allowance": 0.5}
		out = preview_repost_impact("13100134", "WH-PKG", posting_date="2026-05-26")
		self.assertFalse(out["eligible"])
		reason = out["reason"].lower()
		self.assertTrue("exploded" in reason or "poison" in reason or "unsafe" in reason)

	@patch("erpnext_extensions.iran_accounting.historical_stock.riv_preflight.frappe")
	@patch("erpnext_extensions.iran_accounting.historical_stock.riv_preflight.analyze_dependency_closure")
	@patch("erpnext_extensions.iran_accounting.historical_stock.riv_preflight._expected_gl_postable")
	def test_preflight_blocks_unbalanced_gl(self, gl_state, closure, frappe_mod):
		from erpnext_extensions.iran_accounting.historical_stock.riv_preflight import (
			preview_repost_impact,
		)

		frappe_mod.db.sql.return_value = []
		closure.return_value = {
			"direct_vouchers": ["STE-GL"],
			"stock_entries": ["STE-GL"],
			"manufacture_vouchers": [],
			"items": ["ITEM"],
			"expanded_items": [],
			"poison_vouchers": [],
			"manufacture_with_poison_components": False,
			"counts": {"poison": 0},
		}
		gl_state.return_value = {
			"nonempty": True,
			"postable": False,
			"diff": 1.0,
			"allowance": 0.5,
		}
		out = preview_repost_impact("ITEM", "WH")
		self.assertFalse(out["eligible"])
		self.assertTrue(out["gl_blockers"])


class TestGLMapStateAlignHook(unittest.TestCase):
	def test_gl_expected_map_state_source_includes_irr_align(self):
		import inspect
		from erpnext_extensions.iran_accounting.historical_stock.planner import (
			_gl_expected_map_state,
		)

		src = inspect.getsource(_gl_expected_map_state)
		self.assertIn("align_irr_gl_map_to_currency_precision", src)
		self.assertIn("irr_aligned", src)


class TestI1CycleResolution(unittest.TestCase):
	def test_cycle_helper_routes_to_manufacture(self):
		from erpnext_extensions.iran_accounting.historical_stock.i1_repair import (
			_i1_dependency_cycle_resolution,
		)

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.manufacture.preview_manufacture_voucher",
			return_value={
				"needs_repair": True,
				"eligible": True,
				"confidence": CONFIDENCE_EXACT,
				"status": STATUS_RECONSTRUCTABLE,
				"after_rates_healthy": True,
				"fg_negative": True,
			},
		):
			out = _i1_dependency_cycle_resolution(
				"MAT-STE-2026-24967",
				["20100067: MAT-STE-2026-24967 is negative_incoming_rate"],
				{},
			)
		self.assertTrue(out.get("dependency_cycle"))
		self.assertIn("Manufacture", out.get("required_action") or "")


class TestMasterPlanV2Shape(unittest.TestCase):
	@patch("erpnext_extensions.iran_accounting.historical_stock.master_plan._class_warehouse_placeholder")
	@patch("erpnext_extensions.iran_accounting.historical_stock.master_plan._class_sle_gl_drift")
	@patch("erpnext_extensions.iran_accounting.historical_stock.master_plan._class_gl")
	@patch("erpnext_extensions.iran_accounting.historical_stock.master_plan._class_riv")
	@patch("erpnext_extensions.iran_accounting.historical_stock.master_plan._class_wrong")
	@patch("erpnext_extensions.iran_accounting.historical_stock.master_plan._class_zero")
	@patch("erpnext_extensions.iran_accounting.historical_stock.master_plan._class_i4")
	@patch("erpnext_extensions.iran_accounting.historical_stock.master_plan._class_i1")
	@patch("erpnext_extensions.iran_accounting.historical_stock.master_plan._class_posting")
	@patch("erpnext_extensions.iran_accounting.historical_stock.master_plan.resolve_company", return_value="Co")
	def test_v2_fields(self, *_mocks):
		from erpnext_extensions.iran_accounting.historical_stock.master_plan import (
			build_master_repair_plan,
		)

		# Configure class mocks to return minimal dicts.
		import erpnext_extensions.iran_accounting.historical_stock.master_plan as mp

		def fake_class(name, prio):
			return {
				"repair_class": name,
				"priority": prio,
				"READY": 0,
				"WAITING": 0,
				"MANUAL": 0,
				"AMBIGUOUS": 0,
				"sample_rows": [],
			}

		mp._class_posting.return_value = fake_class("POSTING_ORDER", 3)
		mp._class_i1.return_value = fake_class("I1_NEGATIVE_RATE_REPAIR", 2)
		mp._class_i4.return_value = fake_class("I4_LEFTOVER_REPAIR", 6)
		mp._class_zero.return_value = fake_class("ZERO_RATE", 4)
		mp._class_wrong.return_value = fake_class("WRONG_RATE", 5)
		mp._class_riv.return_value = fake_class("FAILED_RIV", 9)
		mp._class_gl.return_value = fake_class("GL", 7)
		mp._class_sle_gl_drift.return_value = fake_class("SLE_GL_DRIFT", 8)
		mp._class_warehouse_placeholder.return_value = fake_class("WAREHOUSE_WIDE", 1)

		plan = build_master_repair_plan(company="Co")
		self.assertEqual(plan.get("version"), "5.3.0")
		self.assertEqual(plan.get("master_plan"), "V2")
		self.assertIn("root_cause_graph", plan)
		self.assertIn("phases", plan)
		self.assertEqual(len(plan.get("phases") or []), 8)
		self.assertTrue(plan.get("safety", {}).get("riv_preflight_required"))
		self.assertTrue(plan.get("safety", {}).get("false_rate_rebuild_complete_refused"))
		self.assertTrue(plan.get("safety", {}).get("zero_rate_purpose_first"))
		self.assertTrue(plan.get("safety", {}).get("material_receipt_no_invent_rate"))
		self.assertFalse(plan.get("safety", {}).get("legitimate_scrap_zero_no_action"))
		self.assertTrue(plan.get("safety", {}).get("matched_but_corrupt_detected"))


class TestScrapZeroBusinessRules(unittest.TestCase):
	def test_warehouse_token_match(self):
		from erpnext_extensions.iran_accounting.historical_stock.scrap_warehouse import (
			warehouse_matches_scrap_reject_waste,
		)

		self.assertTrue(warehouse_matches_scrap_reject_waste("انبار Reject مواد اولیه اسپاد"))
		self.assertTrue(warehouse_matches_scrap_reject_waste("انبار ضایعات اقلام اسپاد"))
		self.assertFalse(warehouse_matches_scrap_reject_waste("انبار محصولات هولد نیمه ساخته اسپاد"))

	def test_legitimate_scrap_zero_no_action(self):
		"""Legacy scrap-warehouse exemption removed — Manufacture scrap into scrap WH
		is no longer auto NO_ACTION solely because of warehouse name."""
		from erpnext_extensions.iran_accounting.historical_stock.zero_rate import classify_zero_row
		from erpnext_extensions.iran_accounting.historical_stock import NO_ACTION_REQUIRED

		row = {
			"name": "d1",
			"idx": 1,
			"parent": "MAT-STE-SCRAP-ZERO",
			"purpose": "Manufacture",
			"item_code": "13200400",
			"qty": 5,
			"basic_rate": 0,
			"valuation_rate": 0,
			"amount": 0,
			"s_warehouse": None,
			"t_warehouse": "انبار ضایعات اقلام اسپاد",
			"secondary_item_type": "Scrap",
			"is_scrap_item": 0,
			"allow_zero_valuation_rate": 0,
			"is_finished_item": 0,
			"company": "Espad",
			"posting_date": "2026-06-01",
			"posting_time": "10:00:00",
		}
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.scrap_warehouse.is_scrap_reject_waste_warehouse",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._version_rate",
			return_value=(0.0, 0.0),
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._batch_inward_rate",
			return_value=0.0,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._previous_healthy_sle_rate",
			return_value=0.0,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._source_transfer_sle_rate",
			return_value=0.0,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._patient_for_identity",
			return_value=None,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._identity_poisoned",
			return_value=False,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._finished_item",
			return_value="FG1",
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate.is_scrap_row",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate.is_product_reject",
			return_value=False,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._issued_rate_for_component",
			return_value=0.0,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate.frappe.get_doc",
			return_value=object(),
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.expected.attach_rate_analysis",
			side_effect=lambda d, *_a, **_k: d,
		):
			out = classify_zero_row(row)
		self.assertNotEqual(out.get("status"), NO_ACTION_REQUIRED)
		self.assertNotEqual(out.get("zero_class"), "LEGITIMATE_SCRAP_ZERO_RATE")
		self.assertTrue(out.get("scrap_warehouse_context"))

	def test_normal_incoming_zero_still_actionable(self):
		"""Material Receipt with document Version source remains reconstructable."""
		from erpnext_extensions.iran_accounting.historical_stock.zero_rate import classify_zero_row
		from erpnext_extensions.iran_accounting.historical_stock import (
			NO_ACTION_REQUIRED,
			STATUS_RECONSTRUCTABLE,
		)

		row = {
			"name": "d2",
			"idx": 1,
			"parent": "MAT-STE-ZERO-CORRUPT",
			"purpose": "Material Receipt",
			"item_code": "RM1",
			"qty": 10,
			"basic_rate": 0,
			"valuation_rate": 0,
			"amount": 0,
			"s_warehouse": None,
			"t_warehouse": "Stores - E",
			"secondary_item_type": None,
			"is_scrap_item": 0,
			"allow_zero_valuation_rate": 0,
			"is_finished_item": 0,
			"company": "Espad",
			"posting_date": "2026-06-01",
			"posting_time": "10:00:00",
			"batch_no": None,
			"serial_and_batch_bundle": None,
			"work_order": None,
			"job_card": None,
		}

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.scrap_warehouse.is_legitimate_scrap_zero_rate",
			return_value=False,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._version_rate",
			return_value=(1500.0, 15000.0),
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._batch_inward_rate",
			return_value=1500.0,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._previous_healthy_sle_rate",
			return_value=0.0,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._source_transfer_sle_rate",
			return_value=0.0,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._patient_for_identity",
			return_value=None,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.zero_rate._identity_poisoned",
			return_value=False,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.expected.attach_rate_analysis",
			side_effect=lambda d, *_a, **_k: d,
		):
			out = classify_zero_row(row)
		self.assertNotEqual(out.get("status"), NO_ACTION_REQUIRED)
		self.assertEqual(out.get("status"), STATUS_RECONSTRUCTABLE)
		self.assertTrue(out.get("eligible"))
		self.assertEqual(out.get("zero_reason"), "AUTHORITATIVE_SOURCE_EXISTS_BUT_RATE_IS_ZERO")


class TestMatchedButCorrupt(unittest.TestCase):
	def test_detect_matched_but_corrupt(self):
		from erpnext_extensions.iran_accounting.historical_stock.authoritative_rate import (
			detect_matched_but_corrupt,
		)

		hit = detect_matched_but_corrupt(
			se_rate=-5e9,
			sle_rate=-5e9,
			expected_rate=3944832.0,
		)
		self.assertIsNotNone(hit)
		self.assertTrue(hit["matched_but_corrupt"])
		self.assertEqual(hit["flag"], "MATCHED_BUT_CORRUPT")

	def test_healthy_match_not_flagged(self):
		from erpnext_extensions.iran_accounting.historical_stock.authoritative_rate import (
			detect_matched_but_corrupt,
		)

		self.assertIsNone(
			detect_matched_but_corrupt(se_rate=100.0, sle_rate=100.0, expected_rate=100.0)
		)


class TestInputMaterialScrapRole(unittest.TestCase):
	def test_input_material_vs_product_reject(self):
		from erpnext_extensions.iran_accounting.historical_stock.scrap_warehouse import (
			SCRAP_ROLE_INPUT_MATERIAL,
			SCRAP_ROLE_MANUFACTURE_BYPRODUCT,
			scrap_valuation_role,
		)

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.scrap_warehouse.is_scrap_row",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.scrap_warehouse.is_product_reject",
			return_value=False,
		):
			role = scrap_valuation_role(
				{"item_code": "13200400", "secondary_item_type": "Scrap", "t_warehouse": "WH"},
				finished_item="30300014",
			)
		self.assertEqual(role, SCRAP_ROLE_INPUT_MATERIAL)

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.scrap_warehouse.is_scrap_row",
			return_value=True,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.scrap_warehouse.is_product_reject",
			return_value=True,
		):
			role2 = scrap_valuation_role(
				{"item_code": "30300014", "secondary_item_type": "Scrap", "t_warehouse": "WH"},
				finished_item="30300014",
			)
		self.assertEqual(role2, SCRAP_ROLE_MANUFACTURE_BYPRODUCT)


class TestNegativeStockClassification(unittest.TestCase):
	def test_chronology_vs_user(self):
		from erpnext_extensions.iran_accounting.historical_stock.negative_stock_report import (
			HISTORICAL_NEGATIVE_REQUIRES_USER,
			POSTING_ORDER_REPAIRABLE,
			_classify_chain,
		)

		self.assertEqual(
			_classify_chain(
				chronology_candidate=True,
				bin_merely_stale=False,
				next_inbound=True,
				gap=30,
				qty_before=5,
				txn_qty=-10,
			),
			POSTING_ORDER_REPAIRABLE,
		)
		self.assertEqual(
			_classify_chain(
				chronology_candidate=False,
				bin_merely_stale=False,
				next_inbound=False,
				gap=None,
				qty_before=0,
				txn_qty=-5,
			),
			HISTORICAL_NEGATIVE_REQUIRES_USER,
		)


class TestZeroQtyNonzeroValueConstant(unittest.TestCase):
	def test_constant_present(self):
		from erpnext_extensions.iran_accounting.historical_stock import ZERO_QTY_NONZERO_VALUE

		self.assertEqual(ZERO_QTY_NONZERO_VALUE, "ZERO_QTY_NONZERO_VALUE")


if __name__ == "__main__":
	unittest.main()
