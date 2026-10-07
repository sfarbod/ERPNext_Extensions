# Copyright (c) 2026, ERPNext Extensions contributors
"""MCC + R1 residual preservation tests for 5.5.22 consumption correction."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace import (
	DECISION_REPLACE,
	apply_consumption_targets_to_canonical,
	apply_replacements_to_canonical,
	detect_manufacture_batch_replace_candidates,
	resolve_manufacture_batch_replace_approvals,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
	scan_golden_rule,
)
from erpnext_extensions.iran_accounting.manufacture_output_contract import (
	R3_LEGITIMATE_BUSINESS_RESIDUAL,
	historical_repair_residual_evidence,
)


JC = "PO-JOB08631"
ITEM = "13200475"
WRONG = "502-13200475-pn3121"
CORRECT = "503-13200475-pn3121"
HIST_MFG = "MAT-STE-2026-29972-1"


def _dept_patch():
	return mock.patch(
		"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan.resolve_mtfm_department",
		return_value={"department": "D", "evidence": [], "ok": True},
	)


def _synthetic_pre_scan():
	"""Pre-repair Golden Rule shape for the canary (502 over-consumed / 503 missing)."""
	return [
		{
			"item_code": ITEM,
			"batch_no": WRONG,
			"issued": 0,
			"returned": 0,
			"consumed": 2961,
			"mi_consumed": 0,
			"scrap": 0,
			"remaining_wip": -2961,
			"status": "BLOCKED",
		},
		{
			"item_code": ITEM,
			"batch_no": CORRECT,
			"issued": 3127,
			"returned": 166,
			"consumed": 0,
			"mi_consumed": 0,
			"scrap": 0,
			"remaining_wip": 2961,
			"status": "MISSING CONSUMPTION",
		},
	]


class TestManufactureConsumptionCorrectionV5522(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db.exists("Job Card", JC):
			raise unittest.SkipTest(f"{JC} missing")

	def test_MCC01_negative_remaining_suggests_reduction_via_replace(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_mfg_rate",
			return_value=16471.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_issued_rate",
			return_value=16471.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._historical_mfg_consume_qty",
			side_effect=lambda jc, item, batch: 2961.0 if batch == WRONG else 0.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._wip_warehouse_for_mfg",
			return_value="WIP",
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._item_has_mi_conflict",
			return_value=False,
		):
			cands = detect_manufacture_batch_replace_candidates(JC, _synthetic_pre_scan())
		elig = [c for c in cands if c.get("eligible") and c["item_code"] == ITEM]
		self.assertEqual(len(elig), 1)
		self.assertAlmostEqual(flt(elig[0]["qty"]), 2961)
		self.assertEqual(elig[0]["wrong_batch"], WRONG)
		self.assertEqual(elig[0]["correct_batch"], CORRECT)

	def test_MCC02_full_reduction_removes_row(self):
		rows = [
			{"type": "CONSUME", "item_code": ITEM, "batch_no": WRONG, "qty": 2961, "s_warehouse": "WIP"},
			{"type": "MAIN_FG", "item_code": "FG", "qty": 1, "is_finished_item": 1},
		]
		with _dept_patch():
			out, blockers = apply_consumption_targets_to_canonical(
				rows,
				[{"item_code": ITEM, "batch_no": WRONG, "target_qty": 0, "rate": 1}],
				job_card=JC,
				wip_warehouse="WIP",
			)
		self.assertFalse(blockers)
		self.assertFalse(
			any(r.get("type") == "CONSUME" and r.get("batch_no") == WRONG for r in out)
		)

	def test_MCC03_partial_reduction_keeps_remainder(self):
		rows = [
			{"type": "CONSUME", "item_code": ITEM, "batch_no": WRONG, "qty": 1000, "s_warehouse": "WIP"},
		]
		with _dept_patch():
			out, blockers = apply_consumption_targets_to_canonical(
				rows,
				[{"item_code": ITEM, "batch_no": WRONG, "target_qty": 700, "rate": 10}],
				job_card=JC,
				wip_warehouse="WIP",
			)
		self.assertFalse(blockers)
		c = next(r for r in out if r.get("type") == "CONSUME")
		self.assertAlmostEqual(flt(c["qty"]), 700)

	def test_MCC04_add_missing_row(self):
		with _dept_patch():
			out, blockers = apply_consumption_targets_to_canonical(
				[],
				[{"item_code": ITEM, "batch_no": CORRECT, "target_qty": 2961, "rate": 16471}],
				job_card=JC,
				wip_warehouse="WIP",
			)
		self.assertFalse(blockers)
		self.assertAlmostEqual(flt(out[0]["qty"]), 2961)
		self.assertEqual(out[0]["batch_no"], CORRECT)

	def test_MCC05_increase_existing_consumption(self):
		rows = [
			{"type": "CONSUME", "item_code": ITEM, "batch_no": CORRECT, "qty": 500, "s_warehouse": "WIP"},
		]
		with _dept_patch():
			out, blockers = apply_consumption_targets_to_canonical(
				rows,
				[{"item_code": ITEM, "batch_no": CORRECT, "target_qty": 800, "rate": 10}],
				job_card=JC,
				wip_warehouse="WIP",
			)
		self.assertFalse(blockers)
		self.assertAlmostEqual(flt(out[0]["qty"]), 800)

	def test_MCC06_target_below_zero_blocks(self):
		with _dept_patch():
			_, blockers = apply_consumption_targets_to_canonical(
				[],
				[{"item_code": ITEM, "batch_no": WRONG, "target_qty": -1, "rate": 1}],
				job_card=JC,
			)
		self.assertTrue(any("target" in b and "< 0" in b for b in blockers))

	def test_MCC07_MCC08_canary_total_2961_not_5922(self):
		"""Post-Apply: active Manufacture must consume 503@2961 only (not 5922)."""
		mfg = frappe.db.get_value(
			"Stock Entry",
			{"job_card": JC, "purpose": "Manufacture", "docstatus": 1},
			"name",
		)
		self.assertTrue(mfg)
		rows = frappe.db.sql(
			"""
			select batch_no, qty
			from `tabStock Entry Detail`
			where parent=%s and item_code=%s
			  and ifnull(s_warehouse,'')!='' and ifnull(t_warehouse,'')=''
			""",
			(mfg, ITEM),
			as_dict=1,
		)
		by_b = {(r.batch_no or ""): flt(r.qty) for r in rows}
		self.assertNotIn(WRONG, by_b)
		self.assertAlmostEqual(by_b.get(CORRECT, 0), 2961)
		self.assertAlmostEqual(sum(by_b.values()), 2961)

	def test_MCC09_MCC10_MCC11_golden_rule_resolved(self):
		scan = scan_golden_rule(JC)
		item_rows = [r for r in scan["rows"] if r["item_code"] == ITEM]
		by_b = {r.get("batch_no"): r for r in item_rows}
		# 502 may disappear once consume=0 and no WIP activity left
		if WRONG in by_b:
			self.assertAlmostEqual(flt(by_b[WRONG]["remaining_wip"]), 0)
			self.assertAlmostEqual(flt(by_b[WRONG]["consumed"]), 0)
		self.assertIn(CORRECT, by_b)
		self.assertAlmostEqual(flt(by_b[CORRECT]["remaining_wip"]), 0)
		self.assertAlmostEqual(flt(by_b[CORRECT]["consumed"]), 2961)
		total_rem = sum(flt(r["remaining_wip"]) for r in item_rows)
		self.assertAlmostEqual(total_rem, 0)

	def test_MCC12_different_rates_use_per_batch_economics(self):
		rows = [
			{"type": "CONSUME", "item_code": ITEM, "batch_no": WRONG, "qty": 100, "s_warehouse": "WIP"},
		]
		group = {
			"item_code": ITEM,
			"wrong_batch": WRONG,
			"correct_batch": CORRECT,
			"qty": 100,
			"wrong_rate": 10,
			"correct_rate": 20,
			"s_warehouse": "WIP",
		}
		with _dept_patch():
			out, blockers = apply_replacements_to_canonical(
				rows, [group], job_card=JC, wip_warehouse="WIP"
			)
		self.assertFalse(blockers)
		c = next(r for r in out if r.get("batch_no") == CORRECT)
		self.assertAlmostEqual(flt(c["valuation_rate"]), 20)

	def test_MCC13_same_rate_preserves_item_economics(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_mfg_rate",
			return_value=16471.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_issued_rate",
			return_value=16471.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._historical_mfg_consume_qty",
			side_effect=lambda jc, item, batch: 2961.0 if batch == WRONG else 0.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._wip_warehouse_for_mfg",
			return_value="WIP",
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._item_has_mi_conflict",
			return_value=False,
		):
			cand = next(
				c
				for c in detect_manufacture_batch_replace_candidates(JC, _synthetic_pre_scan())
				if c.get("eligible")
			)
		self.assertAlmostEqual(flt(cand["value_delta"]), 0.0)
		self.assertAlmostEqual(flt(cand["wrong_rate"]), flt(cand["correct_rate"]))

	def test_MCC14_MCC15_explicit_approval_required(self):
		scan = _synthetic_pre_scan()
		with mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_mfg_rate",
			return_value=16471.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_issued_rate",
			return_value=16471.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._historical_mfg_consume_qty",
			side_effect=lambda jc, item, batch: 2961.0 if batch == WRONG else 0.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._wip_warehouse_for_mfg",
			return_value="WIP",
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._item_has_mi_conflict",
			return_value=False,
		):
			cands = detect_manufacture_batch_replace_candidates(JC, scan)
			resolved = resolve_manufacture_batch_replace_approvals(JC, scan, approvals=[], candidates=cands)
		self.assertFalse(resolved["approved_groups"])
		# Unaccepted approval must not apply
		raw = {
			"item_code": ITEM,
			"wrong_batch": WRONG,
			"correct_batch": CORRECT,
			"qty": 2961,
			"accepted": 0,
			"evidence_fingerprint": cands[0]["evidence_fingerprint"],
			"decision": DECISION_REPLACE,
		}
		resolved2 = resolve_manufacture_batch_replace_approvals(JC, scan, approvals=[raw], candidates=cands)
		self.assertFalse(resolved2["approved_groups"])

	def test_MCC16_MCC17_normalize_preserves_targets(self):
		rows = [
			{"type": "CONSUME", "item_code": ITEM, "batch_no": WRONG, "qty": 2961, "s_warehouse": "WIP"},
		]
		group = {
			"item_code": ITEM,
			"wrong_batch": WRONG,
			"correct_batch": CORRECT,
			"qty": 2961,
			"wrong_rate": 16471,
			"correct_rate": 16471,
			"s_warehouse": "WIP",
		}
		with _dept_patch():
			out, blockers = apply_replacements_to_canonical(
				rows, [group], job_card=JC, wip_warehouse="WIP"
			)
		self.assertFalse(blockers)
		by_b = {
			(r.get("batch_no") or ""): flt(r.get("qty"))
			for r in out
			if r.get("type") == "CONSUME"
		}
		self.assertNotIn(WRONG, by_b)
		self.assertAlmostEqual(by_b[CORRECT], 2961)

	def test_MCC18_stale_fingerprint_blocks(self):
		scan = _synthetic_pre_scan()
		with mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_mfg_rate",
			return_value=16471.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_issued_rate",
			return_value=16471.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._historical_mfg_consume_qty",
			side_effect=lambda jc, item, batch: 2961.0 if batch == WRONG else 0.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._wip_warehouse_for_mfg",
			return_value="WIP",
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._item_has_mi_conflict",
			return_value=False,
		):
			cands = detect_manufacture_batch_replace_candidates(JC, scan)
		raw = {
			"item_code": ITEM,
			"wrong_batch": WRONG,
			"correct_batch": CORRECT,
			"qty": 2961,
			"accepted": 1,
			"evidence_fingerprint": "deadbeef",
			"decision": DECISION_REPLACE,
		}
		resolved = resolve_manufacture_batch_replace_approvals(JC, scan, approvals=[raw], candidates=cands)
		self.assertTrue(any("STALE_PLAN" in b for b in resolved["blockers"]))

	def test_MCC22_job_card_tracking(self):
		row = frappe.db.get_value(
			"Job Card Item",
			{"parent": JC, "item_code": ITEM},
			["transferred_qty", "consumed_qty"],
			as_dict=1,
		)
		self.assertIsNotNone(row)
		self.assertAlmostEqual(flt(row.transferred_qty), 2961)
		self.assertAlmostEqual(flt(row.consumed_qty), 2961)

	def test_MCC27_full_batch_offset_not_auto_converted(self):
		# REPLACE decision string must stay distinct from Batch Offset decisions.
		self.assertEqual(DECISION_REPLACE, "REPLACE_MANUFACTURE_BATCH")
		self.assertNotIn("OFFSET", DECISION_REPLACE)


class TestR1HistoricalResidualV5522(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db.exists("Stock Entry", HIST_MFG):
			raise unittest.SkipTest(f"{HIST_MFG} missing")

	def test_R101_historical_898_gap(self):
		se = frappe.db.get_value(
			"Stock Entry",
			HIST_MFG,
			["total_outgoing_value", "total_incoming_value", "value_difference"],
			as_dict=1,
		)
		self.assertAlmostEqual(flt(se.value_difference), 898.0)
		self.assertAlmostEqual(
			flt(se.total_incoming_value) - flt(se.total_outgoing_value), 898.0
		)

	def test_R102_batch_correction_value_delta_zero(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_mfg_rate",
			return_value=16471.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_issued_rate",
			return_value=16471.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._historical_mfg_consume_qty",
			side_effect=lambda jc, item, batch: 2961.0 if batch == WRONG else 0.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._wip_warehouse_for_mfg",
			return_value="WIP",
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._item_has_mi_conflict",
			return_value=False,
		):
			cand = next(
				c
				for c in detect_manufacture_batch_replace_candidates(JC, _synthetic_pre_scan())
				if c.get("eligible")
			)
		self.assertAlmostEqual(flt(cand["value_delta"]), 0.0)

	def test_R103_classify_historical_residual_as_r3(self):
		from erpnext_extensions.iran_accounting.historical_stock import HISTORICAL_REPAIR_FLAG

		prev = frappe.flags.get(HISTORICAL_REPAIR_FLAG)
		frappe.flags[HISTORICAL_REPAIR_FLAG] = True
		try:
			probe = frappe._dict(
				flags=frappe._dict(
					jc_manufacture_repair=True,
					jc_repair_historical_mfg=HIST_MFG,
				)
			)
			ev = historical_repair_residual_evidence(probe)
			self.assertIsNotNone(ev)
			self.assertAlmostEqual(flt(ev["value_difference"]), 898.0)
			self.assertAlmostEqual(flt(ev["sa_net_credit"]), 898.0)
			self.assertTrue(ev.get("fingerprint"))
			self.assertEqual(R3_LEGITIMATE_BUSINESS_RESIDUAL, "R3_LEGITIMATE_BUSINESS_RESIDUAL")
		finally:
			frappe.flags[HISTORICAL_REPAIR_FLAG] = prev

	def test_R104_repaired_mfg_preserves_sa_credit_898(self):
		mfg = frappe.db.get_value(
			"Stock Entry",
			{"job_card": JC, "purpose": "Manufacture", "docstatus": 1},
			"name",
		)
		self.assertTrue(mfg)
		sa = frappe.db.sql(
			"""
			select sum(credit-debit) as net_credit
			from `tabGL Entry`
			where voucher_no=%s and is_cancelled=0
			  and account like '%%تعدیلات موجودی کالا%%'
			""",
			(mfg,),
		)
		self.assertAlmostEqual(flt(sa[0][0] if sa else 0), 898.0)

	def test_R105_evidence_requires_fingerprint(self):
		from erpnext_extensions.iran_accounting.historical_stock import HISTORICAL_REPAIR_FLAG

		prev = frappe.flags.get(HISTORICAL_REPAIR_FLAG)
		frappe.flags[HISTORICAL_REPAIR_FLAG] = True
		try:
			probe = frappe._dict(
				flags=frappe._dict(
					jc_manufacture_repair=True,
					jc_repair_historical_mfg=HIST_MFG,
				)
			)
			ev = historical_repair_residual_evidence(probe)
			self.assertTrue(ev and ev.get("fingerprint"))
		finally:
			frappe.flags[HISTORICAL_REPAIR_FLAG] = prev

	def test_R106_unexplained_gap_still_blocks_without_flags(self):
		probe = frappe._dict(flags=frappe._dict())
		self.assertIsNone(historical_repair_residual_evidence(probe))

	def test_R107_no_global_weaken_without_historical_mfg(self):
		from erpnext_extensions.iran_accounting.historical_stock import HISTORICAL_REPAIR_FLAG

		prev = frappe.flags.get(HISTORICAL_REPAIR_FLAG)
		frappe.flags[HISTORICAL_REPAIR_FLAG] = True
		try:
			probe = frappe._dict(
				flags=frappe._dict(jc_manufacture_repair=True)  # no historical mfg
			)
			self.assertIsNone(historical_repair_residual_evidence(probe))
		finally:
			frappe.flags[HISTORICAL_REPAIR_FLAG] = prev

	def test_R108_R109_no_arbitrary_sa_or_additional_cost_invented(self):
		# Historical residual SA equals the proven 898 — not an invented arbitrary gap.
		from erpnext_extensions.iran_accounting.historical_stock import HISTORICAL_REPAIR_FLAG

		prev = frappe.flags.get(HISTORICAL_REPAIR_FLAG)
		frappe.flags[HISTORICAL_REPAIR_FLAG] = True
		try:
			probe = frappe._dict(
				flags=frappe._dict(
					jc_manufacture_repair=True,
					jc_repair_historical_mfg=HIST_MFG,
				)
			)
			ev = historical_repair_residual_evidence(probe)
			self.assertAlmostEqual(flt(ev["additional_costs"]), 0.0)
			self.assertAlmostEqual(flt(ev["sa_net_credit"]), flt(ev["value_difference"]))
		finally:
			frappe.flags[HISTORICAL_REPAIR_FLAG] = prev

	def test_R110_gl_balanced_on_active_mfg(self):
		mfg = frappe.db.get_value(
			"Stock Entry",
			{"job_card": JC, "purpose": "Manufacture", "docstatus": 1},
			"name",
		)
		self.assertTrue(mfg)
		row = frappe.db.sql(
			"""
			select sum(debit) d, sum(credit) c
			from `tabGL Entry`
			where voucher_no=%s and is_cancelled=0
			""",
			(mfg,),
		)[0]
		self.assertAlmostEqual(flt(row[0]), flt(row[1]))
