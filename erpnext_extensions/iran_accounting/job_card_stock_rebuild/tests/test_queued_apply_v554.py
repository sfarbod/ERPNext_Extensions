# Copyright (c) 2026, ERPNext Extensions contributors
"""Queued Apply orchestration tests (v5.5.4) — QA01–QA10, single-flight, post-commit recovery."""

from __future__ import annotations

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.api import (
	scan_manufacture_reconciliation,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.queued_repair import (
	MODE_APPLY,
	MODE_DRY_RUN,
	_cache_set,
	_committed_key,
	_release_lock,
	_run_key,
	_update_run,
	apply_in_progress,
	get_active_repair,
	get_committed_evidence,
	get_manufacture_repair_apply_status,
	repair_in_progress,
	start_manufacture_repair_apply,
	start_manufacture_repair_dry_run,
)

JC = "PO-JOB08760"


def _plan_snapshot():
	scan = scan_manufacture_reconciliation(JC)
	plan = scan["plan"]
	return {
		"dispositions": [
			{
				"item_code": r["item_code"],
				"batch_no": r.get("batch_no") or "",
				"proposed_consumed": r.get("proposed_consumed") or 0,
				"proposed_scrap": r.get("proposed_scrap") or 0,
				"proposed_return": r.get("proposed_return") or 0,
				"proposed_still_in_wip": r.get("proposed_still_in_wip") or 0,
				"disposition": r.get("suggested_action"),
			}
			for r in scan["scan"]["rows"]
		],
		"merge_documents": plan.get("merge_documents") or [],
		"merge_material_issues": plan.get("merge_material_issues"),
		"stamp_mode": "HISTORICAL",
		"fingerprint": plan.get("fingerprint"),
	}


def _biz_snap():
	return {
		"se1": cint(frappe.db.count("Stock Entry", {"docstatus": 1})),
		"sle": cint(frappe.db.sql("select count(*) from `tabStock Ledger Entry`")[0][0]),
		"gl": cint(frappe.db.sql("select count(*) from `tabGL Entry`")[0][0]),
		"bin": cint(frappe.db.sql("select count(*) from `tabBin`")[0][0]),
		"batch": cint(frappe.db.sql("select count(*) from `tabBatch`")[0][0]),
		"a": cint(frappe.db.get_value("Stock Entry", "MAT-STE-2026-31725", "docstatus") or 0),
		"b": cint(frappe.db.get_value("Stock Entry", "MAT-STE-2026-31726", "docstatus") or 0),
		"mfg": cint(frappe.db.get_value("Stock Entry", "MAT-STE-2026-31724-1", "docstatus") or 0),
		"active_mfg": frappe.db.sql(
			"select name from `tabStock Entry` where job_card=%s and purpose='Manufacture' and docstatus=1",
			JC,
		),
		"temp": cint(
			frappe.db.sql(
				"select count(*) from `tabStock Entry` where name like 'TEMP-MR-%%' and docstatus < 2"
			)[0][0]
		),
	}


class TestQueuedApplyV554(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.jcsr_apply_now = True
		frappe.flags.jcsr_dry_run_now = True
		frappe.flags.jcsr_repair_now = False
		frappe.flags.jc_repair_fail_at = None
		frappe.flags.jcsr_fail_redis_after_commit = None
		if not frappe.db.exists("Job Card", JC):
			self.skipTest(f"missing {JC}")
		# Require pre-repair fixture
		mfg = cint(frappe.db.get_value("Stock Entry", "MAT-STE-2026-31724-1", "docstatus") or -1)
		if mfg != 1:
			self.skipTest("PO-JOB08760 not in pre-repair fixture (31724-1 not submitted)")
		_release_lock(JC)

	def tearDown(self):
		frappe.flags.jcsr_apply_now = False
		frappe.flags.jcsr_dry_run_now = False
		frappe.flags.jc_repair_fail_at = None
		frappe.flags.jcsr_fail_redis_after_commit = None
		_release_lock(JC)
		frappe.db.rollback()

	def test_single_flight_dry_blocks_apply(self):
		frappe.flags.jcsr_apply_now = False
		frappe.flags.jcsr_dry_run_now = False
		with patch("frappe.enqueue") as enq:
			enq.return_value = frappe._dict(id="job-dry")
			a = start_manufacture_repair_dry_run(JC, _plan_snapshot())
			self.assertFalse(a.get("already_running"))
			enq.return_value = frappe._dict(id="job-apply")
			b = start_manufacture_repair_apply(JC, _plan_snapshot(), confirm=1)
			self.assertTrue(b.get("already_running"))
			self.assertEqual(a["run_id"], b["run_id"])
			self.assertEqual(enq.call_count, 1)
		_release_lock(JC, a["run_id"])

	def test_single_flight_apply_blocks_dry(self):
		frappe.flags.jcsr_apply_now = False
		frappe.flags.jcsr_dry_run_now = False
		with patch("frappe.enqueue") as enq:
			enq.return_value = frappe._dict(id="job-apply")
			a = start_manufacture_repair_apply(JC, _plan_snapshot(), confirm=1)
			self.assertFalse(a.get("already_running"))
			enq.return_value = frappe._dict(id="job-dry")
			b = start_manufacture_repair_dry_run(JC, _plan_snapshot())
			self.assertTrue(b.get("already_running"))
			self.assertEqual(a["run_id"], b["run_id"])
			self.assertEqual(enq.call_count, 1)
		_release_lock(JC, a["run_id"])

	def test_apply_confirm_required(self):
		with self.assertRaises(frappe.ValidationError):
			start_manufacture_repair_apply(JC, _plan_snapshot(), confirm=0)

	def _qa_fail(self, fail_at: str):
		before = _biz_snap()
		frappe.flags.jc_repair_fail_at = fail_at
		frappe.flags.jcsr_apply_now = True
		started = start_manufacture_repair_apply(JC, _plan_snapshot(), confirm=1)
		st = get_manufacture_repair_apply_status(started["run_id"])
		self.assertEqual(st.get("status"), "FAILED", fail_at)
		self.assertIn("INJECTED_FAILURE", st.get("error") or "", fail_at)
		res = st.get("result") or {}
		self.assertFalse(res.get("mutated"), fail_at)
		self.assertFalse(res.get("committed"), fail_at)
		self.assertEqual(_biz_snap(), before, fail_at)
		self.assertFalse(repair_in_progress(JC), fail_at)
		frappe.flags.jc_repair_fail_at = None

	def test_qa01_preparing(self):
		self._qa_fail("preparing")

	def test_qa02_after_temp_bridge(self):
		self._qa_fail("after_temp_receipt_submit")

	def test_qa03_after_dependency_cancel(self):
		self._qa_fail("after_mfg_cancel")

	def test_qa04_after_canonical(self):
		self._qa_fail("after_canonical")

	def test_qa05_after_logistics(self):
		self._qa_fail("after_logistics_recreate")

	def test_qa06_during_valuation(self):
		self._qa_fail("during_valuation")

	def test_qa07_after_valuation(self):
		self._qa_fail("after_valuation")

	def test_qa08_during_verify(self):
		self._qa_fail("during_verify")

	def test_qa09_before_commit(self):
		self._qa_fail("before_commit")

	def test_qa10_post_commit_redis_failure(self):
		"""After commit, Redis status failure must recover COMMITTED (not rollback).

		Uses a patched engine that reports committed=True without mutating the
		fixture DB. Full end-to-end QA10 is covered in the Development release gate.
		"""
		import uuid

		from erpnext_extensions.iran_accounting.job_card_stock_rebuild import queued_repair as qr

		run_id = str(uuid.uuid4())
		compact = {
			"ok": True,
			"status": "APPLY_PASS",
			"committed": True,
			"mutated": True,
			"canonical_name": "MAT-STE-QA10-CANONICAL",
			"fingerprint": "fp-qa10",
		}
		qr._acquire_lock(JC, run_id)
		qr._cache_set(qr._active_key(JC), run_id, qr.LOCK_TTL)
		qr._cache_set(
			qr._run_key(run_id),
			{
				"run_id": run_id,
				"job_card": JC,
				"mode": MODE_APPLY,
				"status": "RUNNING",
				"phase": "COMMIT",
				"progress": 95,
				"requested_by": "Administrator",
				"created_at": "2026-01-01 00:00:00",
				"updated_at": "2026-01-01 00:00:00",
			},
			qr.STATUS_TTL,
		)

		fake_result = {
			"ok": True,
			"status": "APPLY_PASS",
			"committed": True,
			"mutated": True,
			"canonical_name": "MAT-STE-QA10-CANONICAL",
			"fingerprint": "fp-qa10",
			"valuation": {"ok": True, "count": 1, "skipped_count": 0},
			"verification": {"ok": True, "errors": []},
			"phase_timings": {"total_elapsed": 1.0, "phases": []},
			"recreated_logistics": [],
			"logistics_equivalence": [],
			"temporary_bridge": {},
			"blockers": [],
		}

		frappe.flags.jcsr_fail_redis_after_commit = True
		with patch(
			"erpnext_extensions.iran_accounting.job_card_stock_rebuild.queued_repair.run_repair",
			return_value=fake_result,
		):
			out = qr.execute_queued_apply(
				run_id, JC, _plan_snapshot(), user="Administrator", confirm=1
			)
		self.assertTrue(out.get("committed"), out)
		st = get_manufacture_repair_apply_status(run_id)
		self.assertEqual(st.get("status"), "COMMITTED", st)
		self.assertNotEqual(st.get("status"), "FAILED")
		ev = get_committed_evidence(JC)
		self.assertTrue(ev and ev.get("committed"), ev)
		# Must not claim rollback
		res = st.get("result") or {}
		self.assertTrue(res.get("committed"))
		self.assertFalse(repair_in_progress(JC))
		# Start Apply with same fingerprint + already_committed evidence
		frappe.flags.jcsr_fail_redis_after_commit = None
		frappe.flags.jcsr_apply_now = False
		plan = _plan_snapshot()
		plan["fingerprint"] = "fp-qa10"
		with patch.object(qr, "_db_apply_committed_state", return_value=compact):
			with patch("frappe.enqueue") as enq:
				enq.return_value = frappe._dict(id="nope")
				again = start_manufacture_repair_apply(JC, plan, confirm=1)
				self.assertTrue(again.get("already_committed"), again)
				self.assertEqual(enq.call_count, 0)
		qr._cache_delete(qr._committed_key(JC))
		frappe.flags.jcsr_fail_redis_after_commit = None
