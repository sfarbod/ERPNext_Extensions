# Copyright (c) 2026, ERPNext Extensions contributors
"""Focused tests for 5.5.22 Manufacture Batch Replacement repair."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.batch_offset import (
	CLASS_QTY_SAFE_VALUE_DIFF,
	CLASS_QTY_VALUE_SAFE,
	DECISION_ACCEPT,
	DECISION_ACCEPT_PARTIAL,
	detect_batch_offset_candidates,
	detect_partial_batch_offset_candidates,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
	scan_golden_rule,
	validate_dispositions,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace import (
	CLASS_AMBIGUOUS,
	CLASS_WRONG_MFG_BATCH,
	DECISION_REPLACE,
	STATUS_APPROVED_REPLACE,
	apply_replacements_to_canonical,
	detect_manufacture_batch_replace_candidates,
	resolve_manufacture_batch_replace_approvals,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
	build_manufacture_plan,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.queued_repair import (
	_normalize_plan,
)


JC = "PO-JOB08631"
ITEM = "13200475"
WRONG = "502-13200475-pn3121"
CORRECT = "503-13200475-pn3121"
QTY = 2961.0

JC_PARTIAL = "PO-JOB08604"
ITEM_PARTIAL = "13200091"
B5638 = "5638-13200091-PVX-1-2-10741"
B929 = "929-13200091-pvx-1-2-600-1201-10741"
B5738 = "5738-13200091-PVX-1-2-10741"

JC_FULL = "PO-JOB08773"


def _synthetic_pre_rows(item=ITEM, wrong=WRONG, correct=CORRECT, qty=QTY):
	"""Pre-repair Golden Rule shape (502 over-consume / 503 missing)."""
	return [
		{
			"item_code": item,
			"batch_no": wrong,
			"issued": 0,
			"returned": 0,
			"consumed": qty,
			"mi_consumed": 0,
			"scrap": 0,
			"remaining_wip": -qty,
			"status": "BLOCKED",
		},
		{
			"item_code": item,
			"batch_no": correct,
			"issued": 3127,
			"returned": 166,
			"consumed": 0,
			"mi_consumed": 0,
			"scrap": 0,
			"remaining_wip": qty,
			"status": "MISSING CONSUMPTION",
		},
	]


def _detect_synthetic(job_card=JC, item=ITEM, wrong=WRONG, correct=CORRECT, qty=QTY):
	rows = _synthetic_pre_rows(item, wrong, correct, qty)
	with mock.patch(
		"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_mfg_rate",
		return_value=16471.0,
	), mock.patch(
		"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_issued_rate",
		return_value=16471.0,
	), mock.patch(
		"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._historical_mfg_consume_qty",
		side_effect=lambda jc, it, batch: qty if batch == wrong else 0.0,
	), mock.patch(
		"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._wip_warehouse_for_mfg",
		return_value="WIP",
	), mock.patch(
		"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._item_has_mi_conflict",
		return_value=False,
	), mock.patch(
		"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._item_is_serialized",
		return_value=False,
	):
		cands = detect_manufacture_batch_replace_candidates(job_card, rows)
	cand = next(
		(
			c
			for c in cands
			if c["item_code"] == item
			and c.get("wrong_batch") == wrong
			and c.get("correct_batch") == correct
		),
		None,
	)
	return {"rows": rows}, cand


def _cand(job_card=JC, item=ITEM, wrong=WRONG, correct=CORRECT):
	"""Prefer live pre-repair candidate; fall back to synthetic after Apply."""
	scan = scan_golden_rule(job_card)
	cands = detect_manufacture_batch_replace_candidates(job_card, scan["rows"])
	cand = next(
		(
			c
			for c in cands
			if c["item_code"] == item
			and c.get("wrong_batch") == wrong
			and c.get("correct_batch") == correct
		),
		None,
	)
	if cand:
		return scan, cand
	return _detect_synthetic(job_card, item, wrong, correct)


def _zero_dispositions(scan):
	out = []
	for r in scan["rows"]:
		out.append(
			{
				"item_code": r["item_code"],
				"batch_no": r.get("batch_no") or "",
				"proposed_consumed": 0,
				"proposed_scrap": 0,
				"proposed_return": 0,
				"proposed_still_in_wip": 0,
			}
		)
	return out


def _approval(cand, **extra):
	return {
		"item_code": cand["item_code"],
		"wrong_batch": cand["wrong_batch"],
		"correct_batch": cand["correct_batch"],
		"qty": cand["qty"],
		"accepted": 1,
		"evidence_fingerprint": cand["evidence_fingerprint"],
		"decision": DECISION_REPLACE,
		**extra,
	}


class TestManufactureBatchReplaceV5522(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db.exists("Job Card", JC):
			raise unittest.SkipTest(f"{JC} not on site")

	# --- MBR01–MBR11 canary detection / plan ---

	def test_MBR01_detect_exact_wrong_batch_pattern(self):
		_, cand = _cand()
		self.assertIsNotNone(cand)
		self.assertTrue(cand["eligible"], cand)
		self.assertAlmostEqual(flt(cand["qty"]), QTY)
		self.assertEqual(cand["wrong_batch"], WRONG)
		self.assertEqual(cand["correct_batch"], CORRECT)
		self.assertEqual(cand["classification"], CLASS_WRONG_MFG_BATCH)

	def test_MBR02_default_unchecked(self):
		scan, cand = _cand()
		res = resolve_manufacture_batch_replace_approvals(JC, scan["rows"], [], candidates=[cand])
		self.assertEqual(res["approved_groups"], [])
		# Synthetic pre-repair rows still require disposition when unchecked.
		ok, errors, _ = validate_dispositions(scan["rows"], [])
		self.assertFalse(ok)
		self.assertTrue(any(CORRECT in e and "disposition required" in e for e in errors))

	def test_MBR03_user_can_approve(self):
		scan, cand = _cand()
		res = resolve_manufacture_batch_replace_approvals(
			JC, scan["rows"], [_approval(cand)], candidates=[cand]
		)
		self.assertEqual(len(res["approved_groups"]), 1)
		self.assertFalse(res["blockers"])
		self.assertIn((ITEM, WRONG), res["exempt_keys"])
		self.assertIn((ITEM, CORRECT), res["exempt_keys"])

	def test_MBR04_MBR05_MBR06_canonical_remove_add_net_qty(self):
		# Unit path: apply_replacements (works after Apply when live plan candidate is gone).
		_, cand = _cand()
		rows = [
			{"type": "CONSUME", "item_code": ITEM, "batch_no": WRONG, "qty": QTY, "s_warehouse": "WIP"},
		]
		with mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan.resolve_mtfm_department",
			return_value={"department": "D", "evidence": [], "ok": True},
		):
			out, blockers = apply_replacements_to_canonical(
				rows, [cand], job_card=JC, wip_warehouse="WIP"
			)
		self.assertFalse(blockers)
		by_batch = {
			(r.get("batch_no") or ""): flt(r.get("qty"))
			for r in out
			if r.get("type") == "CONSUME"
		}
		self.assertNotIn(WRONG, by_batch)
		self.assertAlmostEqual(by_batch.get(CORRECT, 0), QTY)
		self.assertAlmostEqual(sum(by_batch.values()), QTY)

	def test_MBR07_MBR08_correct_issued_rate_used(self):
		_, cand = _cand()
		self.assertAlmostEqual(flt(cand["correct_rate"]), 16471.0)
		self.assertAlmostEqual(flt(cand["wrong_rate"]), 16471.0)
		rows = [
			{"type": "CONSUME", "item_code": ITEM, "batch_no": WRONG, "qty": QTY, "s_warehouse": "WIP"},
		]
		with mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan.resolve_mtfm_department",
			return_value={"department": "D", "evidence": [], "ok": True},
		):
			out, blockers = apply_replacements_to_canonical(
				rows, [cand], job_card=JC, wip_warehouse="WIP"
			)
		self.assertFalse(blockers)
		row = next(r for r in out if (r.get("batch_no") or "") == CORRECT)
		self.assertEqual(row.get("rate_source"), "issue_transfer")
		self.assertAlmostEqual(flt(row.get("valuation_rate")), flt(cand["correct_rate"]))

	def test_MBR09_value_delta_in_preview(self):
		_, cand = _cand()
		self.assertAlmostEqual(flt(cand["value_delta"]), 0.0)
		self.assertEqual(cand["action_if_approved"], "CANONICAL_MANUFACTURE_BATCH_REPLACE")
		self.assertEqual(len(cand.get("batches") or []), 2)
		roles = {b["role"] for b in cand["batches"]}
		self.assertEqual(roles, {"REMOVE", "ADD"})

	def test_MBR10_no_double_consume(self):
		# Post-Apply active MFG must not double-consume.
		mfg = frappe.db.get_value(
			"Stock Entry",
			{"job_card": JC, "purpose": "Manufacture", "docstatus": 1},
			"name",
		)
		self.assertTrue(mfg)
		rows = frappe.db.sql(
			"""
			select batch_no, qty from `tabStock Entry Detail`
			where parent=%s and item_code=%s
			  and ifnull(s_warehouse,'')!='' and ifnull(t_warehouse,'')=''
			""",
			(mfg, ITEM),
			as_dict=1,
		)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].batch_no, CORRECT)
		self.assertAlmostEqual(flt(rows[0].qty), QTY)

	def test_MBR11_decision_code_and_status(self):
		scan, cand = _cand()
		self.assertEqual(cand["decision"], DECISION_REPLACE)
		self.assertEqual(cand["action_if_approved"], "CANONICAL_MANUFACTURE_BATCH_REPLACE")
		res = resolve_manufacture_batch_replace_approvals(
			JC, scan["rows"], [_approval(cand)], candidates=[cand]
		)
		self.assertEqual(res["audit"][0]["status"], STATUS_APPROVED_REPLACE)

	# --- MBR12–MBR18 safety ---

	def test_MBR12_qty_mismatch_blocks(self):
		scan, cand = _cand()
		res = resolve_manufacture_batch_replace_approvals(
			JC,
			scan["rows"],
			[_approval(cand, qty=QTY + 1)],
			candidates=[cand],
		)
		self.assertTrue(any("BATCH_REPLACEMENT_QTY_MISMATCH" in b for b in res["blockers"]))
		self.assertFalse(res["approved_groups"])

	def test_MBR13_insufficient_ownership_blocks(self):
		fake = [
			{
				"item_code": "OWN",
				"batch_no": "WRONG",
				"issued": 0,
				"returned": 0,
				"consumed": 10,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": -10,
				"status": "BLOCKED",
			},
			{
				"item_code": "OWN",
				"batch_no": "RIGHT",
				"issued": 5,
				"returned": 0,
				"consumed": 0,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": 10,
				"status": "MISSING CONSUMPTION",
			},
		]
		with mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._historical_mfg_consume_qty",
			side_effect=lambda jc, item, batch: 10.0 if batch == "WRONG" else 0.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_mfg_rate",
			return_value=100.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_issued_rate",
			return_value=100.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._wip_warehouse_for_mfg",
			return_value="WIP",
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._item_is_serialized",
			return_value=False,
		):
			cands = detect_manufacture_batch_replace_candidates(JC, fake)
		c = next(c for c in cands if c["item_code"] == "OWN")
		self.assertFalse(c["eligible"])
		self.assertIn("INSUFFICIENT_CORRECT_BATCH_OWNERSHIP", c.get("reason") or "")

	def test_MBR14_foreign_jc_not_in_scan(self):
		scan, cand = _cand()
		self.assertEqual(cand["job_card"], JC)
		real = {r.get("batch_no") for r in scan["rows"] if r["item_code"] == ITEM}
		self.assertNotIn("FOREIGN-BATCH", real)

	def test_MBR15_mi_blocker(self):
		fake = [
			{
				"item_code": "MIITEM",
				"batch_no": "W",
				"issued": 0,
				"returned": 0,
				"consumed": 10,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": -10,
				"status": "BLOCKED",
			},
			{
				"item_code": "MIITEM",
				"batch_no": "C",
				"issued": 10,
				"returned": 0,
				"consumed": 0,
				"mi_consumed": 2,
				"scrap": 0,
				"remaining_wip": 10,
				"status": "MISSING CONSUMPTION",
			},
		]
		with mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._historical_mfg_consume_qty",
			side_effect=lambda jc, item, batch: 10.0 if batch == "W" else 0.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_mfg_rate",
			return_value=100.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_issued_rate",
			return_value=100.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._wip_warehouse_for_mfg",
			return_value="WIP",
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._item_is_serialized",
			return_value=False,
		):
			cands = detect_manufacture_batch_replace_candidates(JC, fake)
		c = next(c for c in cands if c["item_code"] == "MIITEM")
		self.assertFalse(c["eligible"])
		self.assertIn("MI_BLOCKER", c.get("reason") or "")

	def test_MBR16_scrap_blocker(self):
		fake = [
			{
				"item_code": "SCR",
				"batch_no": "W",
				"issued": 0,
				"returned": 0,
				"consumed": 10,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": -10,
				"status": "BLOCKED",
			},
			{
				"item_code": "SCR",
				"batch_no": "C",
				"issued": 10,
				"returned": 0,
				"consumed": 0,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": 10,
				"status": "SCRAP MISMATCH",
			},
		]
		with mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._historical_mfg_consume_qty",
			side_effect=lambda jc, item, batch: 10.0 if batch == "W" else 0.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_mfg_rate",
			return_value=100.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_issued_rate",
			return_value=100.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._wip_warehouse_for_mfg",
			return_value="WIP",
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._item_is_serialized",
			return_value=False,
		):
			cands = detect_manufacture_batch_replace_candidates(JC, fake)
		c = next(c for c in cands if c["item_code"] == "SCR")
		self.assertFalse(c["eligible"])
		self.assertIn("SCRAP_PAIR_MISMATCH", c.get("reason") or "")

	def test_MBR17_serial_blocks(self):
		fake = [
			{
				"item_code": "SER",
				"batch_no": "W",
				"issued": 0,
				"returned": 0,
				"consumed": 1,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": -1,
				"status": "BLOCKED",
			},
			{
				"item_code": "SER",
				"batch_no": "C",
				"issued": 1,
				"returned": 0,
				"consumed": 0,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": 1,
				"status": "MISSING CONSUMPTION",
			},
		]
		with mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._historical_mfg_consume_qty",
			side_effect=lambda jc, item, batch: 1.0 if batch == "W" else 0.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_mfg_rate",
			return_value=100.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._batch_issued_rate",
			return_value=100.0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._wip_warehouse_for_mfg",
			return_value="WIP",
		), mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace._item_is_serialized",
			return_value=True,
		):
			cands = detect_manufacture_batch_replace_candidates(JC, fake)
		c = next(c for c in cands if c["item_code"] == "SER")
		self.assertFalse(c["eligible"])
		self.assertIn("SERIAL_IDENTITY_CONFLICT", c.get("reason") or "")

	def test_MBR18_ambiguous_replacement(self):
		fake = [
			{
				"item_code": "AMB",
				"batch_no": "W",
				"issued": 0,
				"returned": 0,
				"consumed": 10,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": -10,
				"status": "BLOCKED",
			},
			{
				"item_code": "AMB",
				"batch_no": "C1",
				"issued": 10,
				"returned": 0,
				"consumed": 0,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": 10,
				"status": "MISSING CONSUMPTION",
			},
			{
				"item_code": "AMB",
				"batch_no": "C2",
				"issued": 10,
				"returned": 0,
				"consumed": 0,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": 10,
				"status": "MISSING CONSUMPTION",
			},
		]
		cands = detect_manufacture_batch_replace_candidates(JC, fake)
		amb = [c for c in cands if c["item_code"] == "AMB"]
		self.assertTrue(amb)
		self.assertFalse(amb[0]["eligible"])
		self.assertEqual(amb[0]["classification"], CLASS_AMBIGUOUS)

	# --- MBR19–MBR21 queue / stale ---

	def test_MBR19_approval_survives_normalize(self):
		_, cand = _cand()
		raw = {
			"dispositions": [],
			"stamp_mode": "HISTORICAL",
			"manufacture_batch_replace_approvals": [_approval(cand)],
		}
		snap = _normalize_plan(raw)
		self.assertIn("manufacture_batch_replace_approvals", snap)
		self.assertEqual(
			snap["manufacture_batch_replace_approvals"][0]["evidence_fingerprint"],
			cand["evidence_fingerprint"],
		)
		self.assertEqual(snap["manufacture_batch_replace_approvals"][0]["wrong_batch"], WRONG)

	def test_MBR20_normalize_does_not_invent(self):
		out = _normalize_plan({"dispositions": [], "stamp_mode": "HISTORICAL"})
		self.assertNotIn("manufacture_batch_replace_approvals", out)

	def test_MBR21_stale_fingerprint(self):
		scan, cand = _cand()
		res = resolve_manufacture_batch_replace_approvals(
			JC,
			scan["rows"],
			[_approval(cand, evidence_fingerprint="deadbeef-stale")],
			candidates=[cand],
		)
		self.assertTrue(any("STALE_PLAN" in b for b in res["blockers"]))
		self.assertFalse(res["approved_groups"])

	def test_MBR22_apply_replacements_unit(self):
		rows = [
			{
				"type": "CONSUME",
				"item_code": ITEM,
				"batch_no": WRONG,
				"qty": QTY,
				"s_warehouse": "WIP",
				"valuation_rate": 10,
				"basic_rate": 10,
			},
			{
				"type": "MAIN_FG",
				"item_code": "FG",
				"batch_no": "FG1",
				"qty": 1,
				"is_finished_item": 1,
			},
		]
		groups = [
			{
				"item_code": ITEM,
				"wrong_batch": WRONG,
				"correct_batch": CORRECT,
				"qty": QTY,
				"correct_rate": 20,
				"s_warehouse": "WIP",
			}
		]
		with mock.patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan.resolve_mtfm_department",
			return_value={"department": "D", "evidence": [], "ok": True},
		):
			out, blockers = apply_replacements_to_canonical(rows, groups, job_card=JC, wip_warehouse="WIP")
		self.assertFalse(blockers)
		consume = [r for r in out if r.get("type") == "CONSUME" and r.get("item_code") == ITEM]
		self.assertEqual(len(consume), 1)
		self.assertEqual(consume[0]["batch_no"], CORRECT)
		self.assertAlmostEqual(flt(consume[0]["qty"]), QTY)
		self.assertAlmostEqual(flt(consume[0]["valuation_rate"]), 20)

	# --- MBR28–MBR29 regressions ---

	def test_MBR28_full_batch_offset_08773_unregressed(self):
		if not frappe.db.exists("Job Card", JC_FULL):
			self.skipTest(f"{JC_FULL} missing")
		scan = scan_golden_rule(JC_FULL)
		full = detect_batch_offset_candidates(JC_FULL, scan["rows"])
		cand = next(c for c in full if c["item_code"] == ITEM_PARTIAL)
		self.assertTrue(cand["eligible"])
		self.assertEqual(cand["classification"], CLASS_QTY_VALUE_SAFE)
		# Replace must NOT steal / auto-apply; for 08773 pattern wrong-batch
		# typically has JC issue so replace detector stays empty.
		repl = detect_manufacture_batch_replace_candidates(JC_FULL, scan["rows"])
		stolen = [c for c in repl if c["item_code"] == ITEM_PARTIAL and c.get("eligible")]
		self.assertEqual(stolen, [])

	def test_MBR29_partial_08604_still_eligible(self):
		if not frappe.db.exists("Job Card", JC_PARTIAL):
			self.skipTest(f"{JC_PARTIAL} missing")
		scan = scan_golden_rule(JC_PARTIAL)
		full = detect_batch_offset_candidates(JC_PARTIAL, scan["rows"])
		partial = detect_partial_batch_offset_candidates(
			JC_PARTIAL, scan["rows"], full_candidates=full
		)
		cand = next(
			c
			for c in partial
			if c.get("positive_batch") == B5638 and c.get("negative_batch") == B929
		)
		self.assertTrue(cand["eligible"])
		self.assertEqual(cand["classification"], CLASS_QTY_SAFE_VALUE_DIFF)
		# +332 sibling remains independently unresolved
		unres = {u["batch_no"]: flt(u["remaining_wip"]) for u in cand["unresolved_after_pair"]}
		self.assertAlmostEqual(unres.get(B5738), 332.0)
		# Replace may ALSO be eligible as an alternate action — default unchecked.
		# Approving partial must remain possible (mutual exclusion only when both approved).
		repl = detect_manufacture_batch_replace_candidates(JC_PARTIAL, scan["rows"])
		# Do not auto-convert: no replace approval without user decision.
		res = resolve_manufacture_batch_replace_approvals(JC_PARTIAL, scan["rows"], [], candidates=repl)
		self.assertEqual(res["approved_groups"], [])

	def test_MBR30_offset_and_replace_mutually_exclusive(self):
		scan, cand = _cand()
		# Simulate approving full offset keys overlapping replace
		res = resolve_manufacture_batch_replace_approvals(
			JC,
			scan["rows"],
			[_approval(cand)],
			candidates=[cand],
			offset_approved_keys={(ITEM, WRONG), (ITEM, CORRECT)},
		)
		self.assertTrue(any("conflicts with approved Batch Offset" in b for b in res["blockers"]))

	def test_MBR_not_exception_only(self):
		# REPLACE is a real canonical Manufacture mutation, not Batch Offset exception-only.
		_, cand = _cand()
		self.assertEqual(cand["action_if_approved"], "CANONICAL_MANUFACTURE_BATCH_REPLACE")
		self.assertNotEqual(cand["decision"], DECISION_ACCEPT)
		self.assertNotEqual(cand["decision"], DECISION_ACCEPT_PARTIAL)
		# Post-Apply: active MFG proves the repair path mutates stock documents.
		mfg = frappe.db.get_value(
			"Stock Entry",
			{"job_card": JC, "purpose": "Manufacture", "docstatus": 1},
			"name",
		)
		hist = frappe.db.get_value("Stock Entry", "MAT-STE-2026-29972-1", "docstatus")
		self.assertTrue(mfg)
		self.assertEqual(cint(hist), 2)
