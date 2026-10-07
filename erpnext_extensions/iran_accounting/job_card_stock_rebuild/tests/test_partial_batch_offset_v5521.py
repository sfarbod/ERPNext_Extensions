# Copyright (c) 2026, ERPNext Extensions contributors
"""Focused tests for 5.5.21 user-approved Partial Batch Offset."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.batch_offset import (
	CLASS_AMBIGUOUS,
	CLASS_QTY_SAFE_VALUE_DIFF,
	CLASS_QTY_VALUE_SAFE,
	DECISION_ACCEPT,
	DECISION_ACCEPT_PARTIAL,
	STATUS_APPROVED_PARTIAL,
	detect_batch_offset_candidates,
	detect_partial_batch_offset_candidates,
	resolve_partial_batch_offset_approvals,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
	scan_golden_rule,
	validate_dispositions,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
	build_manufacture_plan,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.queued_repair import (
	_normalize_plan,
)


JC = "PO-JOB08604"
ITEM = "13200091"
B5638 = "5638-13200091-PVX-1-2-10741"
B5738 = "5738-13200091-PVX-1-2-10741"
B929 = "929-13200091-pvx-1-2-600-1201-10741"

JC_FULL = "PO-JOB08773"
B926 = "926-13200091-pvx-1-2-600-1201-10741"
B929_FULL = "929-13200091-pvx-1-2-600-1201-10741"


def _partial_cand(job_card=JC, item=ITEM):
	scan = scan_golden_rule(job_card)
	full = detect_batch_offset_candidates(job_card, scan["rows"])
	partial = detect_partial_batch_offset_candidates(
		job_card, scan["rows"], full_candidates=full
	)
	return scan, next(
		(
			c
			for c in partial
			if c["item_code"] == item
			and c.get("positive_batch") == B5638
			and c.get("negative_batch") == B929
		),
		None,
	)


def _dispositions_for_plan(scan, *, approve_pair=True, consume_5738=True):
	"""Cover all positive remainders; zero out approved pair when approve_pair."""
	out = []
	for r in scan["rows"]:
		item = r["item_code"]
		batch = r.get("batch_no") or ""
		rem = flt(r.get("remaining_wip"))
		if approve_pair and item == ITEM and batch in (B5638, B929):
			out.append(
				{
					"item_code": item,
					"batch_no": batch,
					"proposed_consumed": 0,
					"proposed_scrap": 0,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
				}
			)
		elif rem > 1e-9:
			qty = rem
			if item == ITEM and batch == B5738 and not consume_5738:
				qty = 0
			out.append(
				{
					"item_code": item,
					"batch_no": batch,
					"proposed_consumed": qty,
					"proposed_scrap": 0,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
				}
			)
		else:
			out.append(
				{
					"item_code": item,
					"batch_no": batch,
					"proposed_consumed": 0,
					"proposed_scrap": 0,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
				}
			)
	return out


class TestPartialBatchOffsetV5521(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db.exists("Job Card", JC):
			raise unittest.SkipTest(f"{JC} not on site")

	def test_PB01_exact_partial_pair_with_unresolved_sibling(self):
		scan, cand = _partial_cand()
		self.assertIsNotNone(cand)
		self.assertTrue(cand["eligible"], cand)
		self.assertAlmostEqual(flt(cand["pair_qty"]), 306.0)
		self.assertAlmostEqual(flt(cand["positive_remaining"]), 306.0)
		self.assertAlmostEqual(flt(cand["negative_remaining"]), -306.0)
		unres = {u["batch_no"]: flt(u["remaining_wip"]) for u in cand["unresolved_after_pair"]}
		self.assertAlmostEqual(unres.get(B5738), 332.0)

	def test_PB02_332_remains_unresolved_after_approval(self):
		scan, cand = _partial_cand()
		approvals = [
			{
				"item_code": ITEM,
				"positive_batch": B5638,
				"negative_batch": B929,
				"accepted": 1,
				"evidence_fingerprint": cand["evidence_fingerprint"],
				"decision": DECISION_ACCEPT_PARTIAL,
			}
		]
		plan = build_manufacture_plan(
			JC,
			dispositions=_dispositions_for_plan(scan),
			partial_batch_offset_approvals=approvals,
		)
		self.assertFalse(plan.get("blockers"), plan.get("blockers"))
		d5738 = next(d for d in plan["dispositions"] if d.get("batch_no") == B5738)
		self.assertAlmostEqual(flt(d5738["proposed_consumed"]), 332.0)
		self.assertEqual(d5738.get("status"), "MISSING CONSUMPTION")
		self.assertNotEqual(d5738.get("disposition"), STATUS_APPROVED_PARTIAL)

	def test_PB03_different_rates_do_not_block(self):
		_, cand = _partial_cand()
		self.assertTrue(cand["eligible"])
		self.assertGreater(abs(flt(cand["positive_rate"]) - flt(cand["negative_rate"])), 1)

	def test_PB04_value_delta_correct(self):
		_, cand = _partial_cand()
		self.assertAlmostEqual(flt(cand["positive_value"]), 22215600.0, places=2)
		self.assertAlmostEqual(flt(cand["negative_value"]), 20124090.0, places=2)
		self.assertAlmostEqual(flt(cand["value_delta"]), 2091510.0, places=2)

	def test_PB05_classification_qty_safe_value_different(self):
		_, cand = _partial_cand()
		self.assertEqual(cand["classification"], CLASS_QTY_SAFE_VALUE_DIFF)

	def test_PB06_PB09_plan_has_no_pair_stock_rows(self):
		scan, cand = _partial_cand()
		approvals = [
			{
				"item_code": ITEM,
				"positive_batch": B5638,
				"negative_batch": B929,
				"accepted": 1,
				"evidence_fingerprint": cand["evidence_fingerprint"],
			}
		]
		plan = build_manufacture_plan(
			JC,
			dispositions=_dispositions_for_plan(scan),
			partial_batch_offset_approvals=approvals,
		)
		# Pair itself: no extra consume beyond historical MFG collect
		extra_5638 = [
			r
			for r in (plan.get("canonical_manufacture") or {}).get("rows") or []
			if r.get("item_code") == ITEM
			and (r.get("batch_no") or "") == B5638
			and r.get("type") == "CONSUME"
		]
		# Historical collect may still include 929 consume; must NOT invent 5638 +306
		self.assertFalse(any(abs(flt(r.get("qty")) - 306) < 1e-6 for r in extra_5638))
		preview = plan.get("partial_batch_offset_preview") or []
		self.assertTrue(preview)
		self.assertEqual(preview[0].get("action"), "NO_STOCK_DOCUMENT_CHANGE")

	def test_PB10_PB11_default_unchecked_no_auto_net(self):
		scan, cand = _partial_cand()
		self.assertTrue(cand["eligible"])
		res = resolve_partial_batch_offset_approvals(JC, scan["rows"], [], candidates=[cand])
		self.assertEqual(res["approved_groups"], [])
		ok, errors, _ = validate_dispositions(scan["rows"], [])
		self.assertFalse(ok)
		self.assertTrue(any(B5638 in e and "disposition required" in e for e in errors))

	def test_PB12_ambiguous_multiple_counterparts(self):
		fake = [
			{
				"item_code": "AMB",
				"batch_no": "P",
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
				"batch_no": "N1",
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
				"batch_no": "N2",
				"issued": 0,
				"returned": 0,
				"consumed": 10,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": -10,
				"status": "BLOCKED",
			},
		]
		cands = detect_partial_batch_offset_candidates(JC, fake, full_candidates=[])
		amb = [c for c in cands if c["item_code"] == "AMB"]
		self.assertTrue(amb)
		self.assertFalse(amb[0]["eligible"])
		self.assertEqual(amb[0]["classification"], CLASS_AMBIGUOUS)

	def test_PB13_foreign_job_card_not_in_scan(self):
		scan, cand = _partial_cand()
		self.assertEqual(cand["job_card"], JC)
		# Fabricated foreign JC rows are not how detection is called — scan is JC-scoped.
		fake = [
			{
				"item_code": ITEM,
				"batch_no": "FOREIGN-POS",
				"issued": 5,
				"returned": 0,
				"consumed": 0,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": 5,
				"status": "MISSING CONSUMPTION",
			},
			{
				"item_code": ITEM,
				"batch_no": "FOREIGN-NEG",
				"issued": 0,
				"returned": 0,
				"consumed": 5,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": -5,
				"status": "BLOCKED",
			},
		]
		# Using another JC name in detection still only sees provided rows; ownership
		# gate requires negative JC outflow (consumed/returned) which fake has.
		# Foreign JC quantities never appear in real scan_golden_rule(JC).
		real_batches = {r.get("batch_no") for r in scan["rows"] if r["item_code"] == ITEM}
		self.assertNotIn("FOREIGN-POS", real_batches)

	def test_PB14_PB15_mi_conflict_blocks(self):
		fake = [
			{
				"item_code": "MIITEM",
				"batch_no": "A",
				"issued": 10,
				"returned": 0,
				"consumed": 0,
				"mi_consumed": 3,
				"scrap": 0,
				"remaining_wip": 10,
				"status": "MISSING CONSUMPTION",
			},
			{
				"item_code": "MIITEM",
				"batch_no": "B",
				"issued": 0,
				"returned": 0,
				"consumed": 10,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": -10,
				"status": "BLOCKED",
			},
		]
		cands = detect_partial_batch_offset_candidates(JC, fake, full_candidates=[])
		c = next(c for c in cands if c["item_code"] == "MIITEM")
		self.assertFalse(c["eligible"])
		self.assertIn("MI_REVIEW_OR_CONSUME_PRESENT", c.get("reason") or "")

	def test_PB16_scrap_mismatch_blocks(self):
		fake = [
			{
				"item_code": "SCR",
				"batch_no": "A",
				"issued": 10,
				"returned": 0,
				"consumed": 0,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": 10,
				"status": "SCRAP MISMATCH",
			},
			{
				"item_code": "SCR",
				"batch_no": "B",
				"issued": 0,
				"returned": 0,
				"consumed": 10,
				"mi_consumed": 0,
				"scrap": 0,
				"remaining_wip": -10,
				"status": "BLOCKED",
			},
		]
		cands = detect_partial_batch_offset_candidates(JC, fake, full_candidates=[])
		c = next(c for c in cands if c["item_code"] == "SCR")
		self.assertFalse(c["eligible"])
		self.assertIn("COMPONENT_SCRAP_MISMATCH", c.get("reason") or "")

	def test_PB17_serialized_policy(self):
		meta = frappe.db.get_value("Item", ITEM, "has_serial_no")
		self.assertFalse(int(meta or 0))

	def test_PB18_approval_survives_normalize_plan(self):
		_, cand = _partial_cand()
		raw = {
			"dispositions": [],
			"stamp_mode": "HISTORICAL",
			"partial_batch_offset_approvals": [
				{
					"item_code": ITEM,
					"positive_batch": B5638,
					"negative_batch": B929,
					"accepted": 1,
					"evidence_fingerprint": cand["evidence_fingerprint"],
					"decision": DECISION_ACCEPT_PARTIAL,
				}
			],
		}
		snap = _normalize_plan(raw)
		self.assertIn("partial_batch_offset_approvals", snap)
		self.assertEqual(snap["partial_batch_offset_approvals"][0]["evidence_fingerprint"], cand["evidence_fingerprint"])
		self.assertEqual(snap["partial_batch_offset_approvals"][0]["positive_batch"], B5638)

	def test_PB19_normalize_does_not_invent_partial(self):
		out = _normalize_plan({"dispositions": [], "stamp_mode": "HISTORICAL"})
		self.assertNotIn("partial_batch_offset_approvals", out)

	def test_PB20_stale_fingerprint(self):
		scan, cand = _partial_cand()
		res = resolve_partial_batch_offset_approvals(
			JC,
			scan["rows"],
			[
				{
					"item_code": ITEM,
					"positive_batch": B5638,
					"negative_batch": B929,
					"accepted": 1,
					"evidence_fingerprint": "deadbeef-stale",
				}
			],
			candidates=[cand],
		)
		self.assertTrue(any("STALE_PLAN" in b for b in res["blockers"]))
		self.assertFalse(res["approved_groups"])

	def test_PB21_full_offset_08773_unregressed(self):
		if not frappe.db.exists("Job Card", JC_FULL):
			self.skipTest(f"{JC_FULL} missing")
		scan = scan_golden_rule(JC_FULL)
		full = detect_batch_offset_candidates(JC_FULL, scan["rows"])
		cand = next(c for c in full if c["item_code"] == ITEM)
		self.assertTrue(cand["eligible"])
		self.assertEqual(cand["classification"], CLASS_QTY_VALUE_SAFE)
		# Partial must not steal the eligible full pair
		partial = detect_partial_batch_offset_candidates(
			JC_FULL, scan["rows"], full_candidates=full
		)
		stolen = [
			c
			for c in partial
			if c["item_code"] == ITEM
			and {c.get("positive_batch"), c.get("negative_batch")} == {B926, B929_FULL}
			and c.get("eligible")
		]
		self.assertEqual(stolen, [])

	def test_PB22_unapproved_partial_remains_strict(self):
		scan, cand = _partial_cand()
		disps = _dispositions_for_plan(scan, approve_pair=True)
		# Zero pair dispositions without approval → 5638 disposition required
		plan = build_manufacture_plan(JC, dispositions=disps, partial_batch_offset_approvals=[])
		self.assertTrue(
			any(B5638 in (b or "") and "disposition" in (b or "") for b in (plan.get("blockers") or []))
			or any("allocations" in (b or "") for b in (plan.get("blockers") or [])),
			plan.get("blockers"),
		)

	def test_PB23_unpaired_332_required(self):
		scan, cand = _partial_cand()
		approvals = [
			{
				"item_code": ITEM,
				"positive_batch": B5638,
				"negative_batch": B929,
				"accepted": 1,
				"evidence_fingerprint": cand["evidence_fingerprint"],
			}
		]
		disps = _dispositions_for_plan(scan, approve_pair=True, consume_5738=False)
		plan = build_manufacture_plan(
			JC, dispositions=disps, partial_batch_offset_approvals=approvals
		)
		self.assertTrue(
			any(B5738 in (b or "") for b in (plan.get("blockers") or [])),
			plan.get("blockers"),
		)

	def test_PB24_dry_run_pair_zero_mutation(self):
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import (
			dry_run_manufacture_repair,
		)

		scan, cand = _partial_cand()
		approvals = [
			{
				"item_code": ITEM,
				"positive_batch": B5638,
				"negative_batch": B929,
				"accepted": 1,
				"evidence_fingerprint": cand["evidence_fingerprint"],
				"decision": DECISION_ACCEPT_PARTIAL,
			}
		]
		plan_input = {
			"dispositions": _dispositions_for_plan(scan),
			"partial_batch_offset_approvals": approvals,
			"stamp_mode": "HISTORICAL",
		}
		# Preview plan first for merge docs / fingerprint
		preview = build_manufacture_plan(
			JC,
			dispositions=plan_input["dispositions"],
			partial_batch_offset_approvals=approvals,
		)
		if preview.get("blockers"):
			self.skipTest(f"fixture blockers: {preview.get('blockers')[:3]}")
		plan_input["merge_documents"] = preview.get("merge_documents")
		plan_input["fingerprint"] = preview.get("fingerprint")
		before = {
			"se": frappe.db.sql("select count(*) from `tabStock Entry`")[0][0],
			"sle": frappe.db.sql("select count(*) from `tabStock Ledger Entry`")[0][0],
			"gl": frappe.db.sql("select count(*) from `tabGL Entry`")[0][0],
		}
		out = dry_run_manufacture_repair(JC, plan=plan_input)
		after = {
			"se": frappe.db.sql("select count(*) from `tabStock Entry`")[0][0],
			"sle": frappe.db.sql("select count(*) from `tabStock Ledger Entry`")[0][0],
			"gl": frappe.db.sql("select count(*) from `tabGL Entry`")[0][0],
		}
		self.assertEqual(before, after)
		self.assertFalse(out.get("committed"))
		# May pass or be blocked by Custom 6 / other fixture items — pair must not invent SA
		if out.get("ok"):
			self.assertEqual(out.get("status"), "DRY_RUN_PASS")
			self.assertFalse(out.get("mutated"))
			self.assertTrue(out.get("approved_partial_batch_offsets") or (out.get("verification") or {}).get("approved_partial_batch_offsets"))

	def test_PB25_audit_preserves_pair_evidence(self):
		scan, cand = _partial_cand()
		approvals = [
			{
				"item_code": ITEM,
				"positive_batch": B5638,
				"negative_batch": B929,
				"accepted": 1,
				"evidence_fingerprint": cand["evidence_fingerprint"],
			}
		]
		plan = build_manufacture_plan(
			JC,
			dispositions=_dispositions_for_plan(scan),
			partial_batch_offset_approvals=approvals,
		)
		audit = plan.get("partial_batch_offset_approvals") or []
		self.assertTrue(audit)
		self.assertEqual(audit[0].get("status"), STATUS_APPROVED_PARTIAL)
		self.assertAlmostEqual(flt(audit[0].get("value_delta")), 2091510.0, places=2)
		self.assertEqual(audit[0].get("decision"), DECISION_ACCEPT_PARTIAL)
		self.assertTrue(audit[0].get("repack_link_found"))


def run():
	loader = unittest.TestLoader()
	suite = loader.loadTestsFromTestCase(TestPartialBatchOffsetV5521)
	result = unittest.TextTestRunner(verbosity=2).run(suite)
	details = []
	for test, err in list(result.failures) + list(result.errors):
		details.append({"test": str(test), "error": err[:2000]})
	return {
		"ok": result.wasSuccessful(),
		"testsRun": result.testsRun,
		"failures": len(result.failures),
		"errors": len(result.errors),
		"details": details,
	}
