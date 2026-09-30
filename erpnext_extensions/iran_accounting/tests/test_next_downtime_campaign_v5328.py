# Copyright (c) 2026, ERPNext Extensions contributors
"""Next-downtime campaign: CROSS_TIME, lanes, preflight, orchestrator (no DB writes)."""

from __future__ import annotations

import unittest

from erpnext_extensions.iran_accounting.domain.riv_rate_guard import is_valued_source_zero_outgoing
from erpnext_extensions.iran_accounting.historical_stock.i4_repair import is_precision_dust_inbound
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.categories import (
	AUTO_REPAIRABLE,
	LEGITIMATE,
	MANUAL_BUSINESS_EVIDENCE_REQUIRED,
	TECHNICAL_TOOL_GAP,
	WAITING_UPSTREAM,
	classify_lane,
)
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.cross_time import (
	CROSS_TIME_EXACT,
	INDEPENDENT_PRODUCTION_FLOWS,
	LEGITIMATE_ORDER,
	classify_cross_time,
	minimum_timestamp_shift,
	quantity_conserved,
	same_manufacturing_family,
)
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.failed_riv_buckets import (
	HISTORICAL_ONLY,
	SAFE_TO_RETRY,
	SUPERSEDED,
	classify_failed_riv_row,
)
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.orchestrator import (
	FROZEN_CANDIDATE_2_SHA256,
	backup_candidate_eligible,
	historical_repair_campaign,
	manual_gate,
	refuse_mutate_frozen_candidate,
)
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.preflight import (
	FULL_REPOST_BLOCKED,
	FULL_REPOST_READY,
	full_repost_preflight,
)
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.sabb import sabb_role
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.zero_reasons import (
	LEGITIMATE_FREE_RECEIPT,
	LEGITIMATE_ZERO_RECEIPT,
	LEFTOVER_MA,
	VALUED_SOURCE_ZERO_OUTGOING,
	classify_zero_rate,
)


class TestCrossTimePlanner(unittest.TestCase):
	def test_reconstructed_qty_conserved(self):
		self.assertTrue(quantity_conserved(1947, 1947, 0))
		self.assertFalse(quantity_conserved(100, 80, 0))

	def test_minimum_shift_is_plus_one_second(self):
		shift = minimum_timestamp_shift("2026-04-19 18:00:00", "2026-04-19 18:02:16")
		self.assertTrue(shift["ok"])
		self.assertEqual(shift["seconds"], 137)
		self.assertEqual(shift["new_outbound"], "2026-04-19 18:02:17")

	def test_shared_family_ready_is_exact(self):
		row = {
			"detection": "CROSS_TIME",
			"planner_status": "READY_BATCH_SCOPED_REPAIR",
			"status": "ELIGIBLE",
			"work_order": "WO-1",
			"outbound_work_order": "WO-1",
			"inbound_work_order": "WO-1",
			"inbound_qty": 10,
			"outbound_qty": 10,
			"outbound_datetime": "2026-04-19 18:00:00",
			"inbound_datetime": "2026-04-19 18:02:16",
		}
		got = classify_cross_time(row)
		self.assertEqual(got["class"], CROSS_TIME_EXACT)
		self.assertTrue(got["repair"])
		self.assertEqual(got["shift"]["seconds"], 137)

	def test_different_work_orders_are_independent_production_flows(self):
		row = {
			"detection": "CROSS_TIME",
			"planner_status": "READY_WAREHOUSE_REPLAY",
			"status": "CROSS_TIME_REPAIRABLE",
			"batch": "BATCH-X",
			"outbound_work_order": "WO-A",
			"inbound_work_order": "WO-B",
			"inbound_qty": 10,
			"outbound_qty": 10,
			"outbound_datetime": "2026-04-19 18:00:00",
			"inbound_datetime": "2026-04-19 18:02:16",
		}
		got = classify_cross_time(row)
		self.assertEqual(got["class"], INDEPENDENT_PRODUCTION_FLOWS)
		self.assertFalse(got["repair"])

	def test_same_time_no_repair_is_legitimate(self):
		got = classify_cross_time({"detection": "SAME_TIME", "status": "NO_REPAIR_NEEDED"})
		self.assertEqual(got["class"], LEGITIMATE_ORDER)

	def test_family_requires_shared_wo_or_job_card_not_batch_alone(self):
		self.assertTrue(same_manufacturing_family({"work_order": "W"}, {"work_order": "W"}))
		self.assertFalse(same_manufacturing_family({"work_order": "A"}, {"work_order": "B"}))
		self.assertFalse(
			same_manufacturing_family(
				{"work_order": "A", "batch": "B1"},
				{"work_order": "B", "batch": "B1"},
			)
		)
		self.assertTrue(same_manufacturing_family({"job_card": "JC-1"}, {"job_card": "JC-1"}))


class TestLanesAndSabb(unittest.TestCase):
	def test_precision_dust_is_legitimate(self):
		self.assertEqual(
			classify_lane(i4_reason="PRECISION_DUST_INBOUND"),
			LEGITIMATE,
		)

	def test_ready_exact_is_auto(self):
		self.assertEqual(
			classify_lane(planner_status="READY_WRONG_RATE", confidence="EXACT"),
			AUTO_REPAIRABLE,
		)

	def test_descendant_is_waiting(self):
		self.assertEqual(classify_lane(is_descendant=True), WAITING_UPSTREAM)

	def test_tool_limit_is_not_manual(self):
		self.assertEqual(
			classify_lane(manual_reason="MANUAL_TRANSFER_PROPAGATION", manual_lane="TOOL_LIMIT"),
			TECHNICAL_TOOL_GAP,
		)

	def test_disagreeing_sources_are_manual(self):
		self.assertEqual(
			classify_lane(has_authoritative_source=False, sources_disagree=True),
			MANUAL_BUSINESS_EVIDENCE_REQUIRED,
		)

	def test_sabb_paykar_is_descendant(self):
		self.assertEqual(
			sabb_role(
				warehouse="انبار پایکار خط تولید اسپاد فارمد",
				has_source_side_same_batch=True,
				source="batch_inward_sabb_rate",
				confidence="EXACT",
			),
			"DESCENDANT",
		)
		self.assertEqual(
			sabb_role(
				warehouse="انبار approved اقلام بسته بندی اولیه اسپاد",
				has_source_side_same_batch=False,
				source="batch_inward_sabb_rate",
				confidence="EXACT",
			),
			"ROOT",
		)


class TestZeroAndValuedSource(unittest.TestCase):
	def test_legitimate_zero_not_forced(self):
		self.assertEqual(
			classify_zero_rate({"zero_provenance": "ZP_PROVEN_LEGITIMATE_ZERO", "qty": 2, "stock_value": 0}),
			LEGITIMATE_ZERO_RECEIPT,
		)
		self.assertEqual(
			classify_zero_rate(
				{
					"allow_zero_valuation_rate": 1,
					"qty": 5,
					"valuation_rate": 0,
					"stock_value": 0,
					"purpose": "Material Receipt",
				}
			),
			LEGITIMATE_FREE_RECEIPT,
		)

	def test_valued_source_manufacture_only(self):
		self.assertEqual(
			classify_zero_rate(
				{
					"actual_qty": -2126,
					"valuation_rate": 0,
					"allow_zero_valuation_rate": 0,
					"source_rate": 354657,
					"purpose": "Manufacture",
				}
			),
			VALUED_SOURCE_ZERO_OUTGOING,
		)
		self.assertTrue(
			is_valued_source_zero_outgoing(
				actual_qty=-2126,
				basic_rate=0,
				allow_zero_valuation_rate=0,
				outgoing_rate=354657,
				purpose="Manufacture",
			)
		)
		self.assertFalse(
			is_valued_source_zero_outgoing(
				actual_qty=-10,
				basic_rate=0,
				allow_zero_valuation_rate=0,
				outgoing_rate=5005345,
				purpose="Material Transfer",
			)
		)

	def test_qty_value_rate0_is_leftover_ma_stamp(self):
		self.assertEqual(
			classify_zero_rate({"qty": 510, "stock_value": 2552725697, "valuation_rate": 0}),
			LEFTOVER_MA,
		)

	def test_i4_precision_dust_math(self):
		row = {
			"qty_after_transaction": 0,
			"stock_value": 486,
			"actual_qty": 5.3e-05,
			"incoming_rate": 9169811,
			"valuation_rate": 9169811,
		}
		self.assertTrue(is_precision_dust_inbound(row))


class TestFailedRivAndPreflight(unittest.TestCase):
	def test_failed_riv_buckets(self):
		self.assertEqual(classify_failed_riv_row({"riv_reconcile_status": "HISTORICAL_ONLY"}), HISTORICAL_ONLY)
		self.assertEqual(
			classify_failed_riv_row({"riv_reconcile_status": "SUPERSEDED_BY_SUCCESSFUL_REPAIR"}),
			SUPERSEDED,
		)
		self.assertEqual(
			classify_failed_riv_row({"riv_reconcile_status": "CURRENT_LEDGER_IMPACT", "error_class": "DEADLOCK"}),
			SAFE_TO_RETRY,
		)
		self.assertEqual(
			classify_failed_riv_row(
				{"riv_reconcile_status": "CURRENT_LEDGER_IMPACT", "error_class": "VALUATION_INTEGRITY"}
			),
			TECHNICAL_TOOL_GAP,
		)

	def test_preflight_blocks_known_poisons(self):
		blocked = full_repost_preflight({"i1": 1, "cross_time_exact": 1, "open_riv": 2})
		self.assertEqual(blocked["status"], FULL_REPOST_BLOCKED)
		self.assertFalse(blocked["ready"])
		self.assertGreaterEqual(len(blocked["blockers"]), 3)
		ready = full_repost_preflight({})
		self.assertEqual(ready["status"], FULL_REPOST_READY)

	def test_preflight_precision_dust_does_not_block(self):
		ready = full_repost_preflight(
			{
				"known_riv_poison_roots": [
					{"item": "A", "warehouse": "W", "status": "PRECISION_DUST"},
					{"item": "B", "warehouse": "W", "status": "LEGITIMATE"},
				],
				"manufacturing_dependency_roots": [],
			}
		)
		self.assertEqual(ready["status"], FULL_REPOST_READY)
		blocked = full_repost_preflight(
			{
				"known_riv_poison_roots": [
					{
						"item": "C",
						"warehouse": "W",
						"status": "TECHNICAL_TOOL_GAP",
						"riv_reproduce_reason": "opening poison",
					}
				]
			}
		)
		self.assertEqual(blocked["status"], FULL_REPOST_BLOCKED)
		self.assertEqual(blocked["normalized_snapshot"]["known_riv_poison_root"], 1)


class TestOrchestrator(unittest.TestCase):
	def test_manual_gate_stops_p1(self):
		gate = manual_gate(
			[
				{
					"category": MANUAL_BUSINESS_EVIDENCE_REQUIRED,
					"priority": "P1",
					"voucher": "STE-X",
				}
			]
		)
		self.assertTrue(gate["stop"])
		self.assertEqual(len(gate["blocking"]), 1)

	def test_p2_does_not_stop(self):
		gate = manual_gate(
			[{"category": MANUAL_BUSINESS_EVIDENCE_REQUIRED, "priority": "P2"}]
		)
		self.assertFalse(gate["stop"])

	def test_dry_run_campaign(self):
		out = historical_repair_campaign(
			source_sha256="abc",
			code_head="deadbeef",
			app_version="5.3.28",
			findings=[],
			snapshot={},
			apply=False,
		)
		self.assertTrue(out["ok"])
		self.assertEqual(out["phase"], "DRY_RUN")
		self.assertFalse(out["apply"])

	def test_resume_after_hard_gate_requires_fresh_restore(self):
		out = historical_repair_campaign(
			source_sha256="abc",
			code_head="deadbeef",
			app_version="5.3.28",
			resume_state={"hard_gate_failed": True},
			apply=True,
		)
		self.assertFalse(out["ok"])
		self.assertIn("fresh restore", out["reason"])

	def test_refuse_frozen_candidate_2(self):
		got = refuse_mutate_frozen_candidate(
			"20260927_051943-development_localhost-database.sql.gz",
			FROZEN_CANDIDATE_2_SHA256,
		)
		self.assertTrue(got["frozen"])
		self.assertFalse(got["allow_experiment"])
		blocked = historical_repair_campaign(
			source_sha256="abc",
			code_head="deadbeef",
			app_version="5.3.28",
			snapshot={"backup_sha256": FROZEN_CANDIDATE_2_SHA256},
			apply=True,
		)
		self.assertFalse(blocked["ok"])
		self.assertIn("immutable", blocked["reason"])

	def test_backup_eligibility(self):
		no = backup_candidate_eligible({})
		self.assertFalse(no["eligible"])
		yes = backup_candidate_eligible(
			{
				"phases_complete": True,
				"workers_settled": True,
				"open_riv": 0,
				"hard_gates_pass": True,
				"verify_complete": True,
				"db_committed": True,
			}
		)
		self.assertTrue(yes["eligible"])


def run_next_downtime_suite():
	suite = unittest.defaultTestLoader.loadTestsFromModule(
		__import__(
			"erpnext_extensions.iran_accounting.tests.test_next_downtime_campaign_v5328",
			fromlist=["*"],
		)
	)
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	return {
		"ok": result.wasSuccessful(),
		"tests": result.testsRun,
		"failures": len(result.failures),
		"errors": len(result.errors),
	}
