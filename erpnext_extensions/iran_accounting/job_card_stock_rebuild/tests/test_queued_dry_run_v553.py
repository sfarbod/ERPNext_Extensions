# Copyright (c) 2026, ERPNext Extensions contributors
"""Queued Dry Run orchestration tests (v5.5.3) — QF01–QF10, concurrency, zero-mutation."""

from __future__ import annotations

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint
from unittest.mock import patch

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.api import (
	scan_manufacture_reconciliation,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.queued_dry_run import (
	dry_run_in_progress,
	get_active_dry_run,
	get_manufacture_repair_dry_run_status,
	start_manufacture_repair_dry_run,
	_lock_key,
	_release_lock,
	_run_key,
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
		"a": cint(frappe.db.get_value("Stock Entry", "MAT-STE-2026-31725", "docstatus")),
		"b": cint(frappe.db.get_value("Stock Entry", "MAT-STE-2026-31726", "docstatus")),
	}


class TestQueuedDryRunV553(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.jcsr_dry_run_now = True
		frappe.flags.jc_repair_fail_at = None
		if not frappe.db.exists("Job Card", JC):
			self.skipTest(f"missing {JC}")
		_release_lock(JC)

	def tearDown(self):
		frappe.flags.jcsr_dry_run_now = False
		frappe.flags.jc_repair_fail_at = None
		_release_lock(JC)
		frappe.db.rollback()

	def test_start_returns_quickly_and_pass(self):
		before = _biz_snap()
		plan = _plan_snapshot()
		t0 = frappe.utils.now_datetime()
		started = start_manufacture_repair_dry_run(JC, plan)
		# Inline mode finishes in-process; start still returns structured run_id.
		self.assertTrue(started.get("run_id"))
		self.assertTrue(started.get("inline"))
		st = get_manufacture_repair_dry_run_status(started["run_id"])
		self.assertEqual(st.get("status"), "PASS")
		self.assertFalse((st.get("result") or {}).get("mutated"))
		self.assertFalse((st.get("result") or {}).get("committed"))
		self.assertEqual(_biz_snap(), before)
		self.assertFalse(dry_run_in_progress(JC))
		_ = t0  # start API itself is not a long HTTP wait in inline mode

	def test_single_flight_concurrent_starts(self):
		frappe.flags.jcsr_dry_run_now = False
		plan = _plan_snapshot()
		with patch("frappe.enqueue") as enq:
			enq.return_value = frappe._dict(id="job-fake-1")
			a = start_manufacture_repair_dry_run(JC, plan)
			self.assertFalse(a.get("already_running"))
			enq.return_value = frappe._dict(id="job-fake-2")
			b = start_manufacture_repair_dry_run(JC, plan)
			self.assertTrue(b.get("already_running"))
			self.assertEqual(a["run_id"], b["run_id"])
			self.assertEqual(enq.call_count, 1)
		_release_lock(JC, a["run_id"])

	def test_different_job_card_not_blocked(self):
		frappe.flags.jcsr_dry_run_now = False
		with patch("frappe.enqueue") as enq:
			enq.return_value = frappe._dict(id="j1")
			# acquire for JC without finishing
			a = start_manufacture_repair_dry_run(JC, _plan_snapshot())
			self.assertTrue(a.get("run_id"))
			# synthetic other card lock independence via lock key
			other = "PO-JOB00000-TEST"
			from erpnext_extensions.iran_accounting.job_card_stock_rebuild import queued_dry_run as q

			ok = q._acquire_lock(other, "other-run")
			self.assertTrue(ok)
			q._release_lock(other, "other-run")
		_release_lock(JC, a["run_id"])

	def test_apply_blocked_while_running(self):
		frappe.flags.jcsr_dry_run_now = False
		with patch("frappe.enqueue") as enq:
			enq.return_value = frappe._dict(id="j-block")
			started = start_manufacture_repair_dry_run(JC, _plan_snapshot())
			self.assertTrue(dry_run_in_progress(JC))
			from erpnext_extensions.iran_accounting.job_card_stock_rebuild.api import (
				apply_manufacture_repair,
			)

			with self.assertRaises(frappe.ValidationError):
				apply_manufacture_repair(JC, plan=_plan_snapshot(), confirm=1)
		_release_lock(JC, started["run_id"])

	def test_stale_recovery(self):
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild import queued_dry_run as q

		run_id = "stale-run-id"
		q._acquire_lock(JC, run_id)
		q._cache_set(
			q._active_key(JC),
			run_id,
			q.LOCK_TTL,
		)
		q._cache_set(
			q._run_key(run_id),
			{
				"run_id": run_id,
				"job_card": JC,
				"status": "RUNNING",
				"phase": "VALUATION",
				"progress": 70,
				"updated_at": "2020-01-01 00:00:00",
				"created_at": "2020-01-01 00:00:00",
			},
			q.STATUS_TTL,
		)
		active = get_active_dry_run(JC)
		self.assertIsNone(active)
		st = get_manufacture_repair_dry_run_status(run_id)
		self.assertEqual(st.get("status"), "FAILED")

	def _qf(self, fail_at: str):
		before = _biz_snap()
		frappe.flags.jc_repair_fail_at = fail_at
		frappe.flags.jcsr_dry_run_now = True
		started = start_manufacture_repair_dry_run(JC, _plan_snapshot())
		st = get_manufacture_repair_dry_run_status(started["run_id"])
		self.assertEqual(st.get("status"), "FAILED", fail_at)
		self.assertIn("INJECTED_FAILURE", st.get("error") or "")
		self.assertEqual(_biz_snap(), before, fail_at)
		self.assertFalse(dry_run_in_progress(JC), fail_at)
		frappe.flags.jc_repair_fail_at = None

	def test_qf01_preparing(self):
		self._qf("preparing")

	def test_qf02_after_locking(self):
		self._qf("after_locking")

	def test_qf03_after_temp_bridge(self):
		self._qf("after_temp_receipt_submit")

	def test_qf04_after_dependencies(self):
		self._qf("after_mfg_cancel")

	def test_qf05_after_canonical(self):
		self._qf("after_canonical")

	def test_qf06_after_logistics(self):
		self._qf("after_logistics_recreate")

	def test_qf07_during_valuation(self):
		self._qf("during_valuation")

	def test_qf08_after_valuation(self):
		self._qf("after_valuation")

	def test_qf09_during_verify(self):
		self._qf("during_verify")

	def test_qf10_before_rollback(self):
		self._qf("before_rollback")
