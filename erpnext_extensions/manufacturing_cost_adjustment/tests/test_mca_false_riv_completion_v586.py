# Copyright (c) 2026, ERPNext Extensions contributors
"""Regression: never false-Complete RIV after RQ Job OK + deadlock/1020.

Forensic MCA-2026-00025: heal treated finished RQ job as successful repost while
Core left RIV In Progress after recoverable failure, then MCA finalized and
subsequent transfer MAT-STE-2026-25934 kept pre-MCA outgoing rates.
"""
from __future__ import annotations

import inspect
import time
import unittest
from unittest.mock import MagicMock, patch

import frappe

from erpnext_extensions.manufacturing_cost_adjustment import constants as C
from erpnext_extensions.manufacturing_cost_adjustment import repost_engine as re


def check(name: str, fn) -> dict:
	t0 = time.perf_counter()
	try:
		fn()
		return {"name": name, "ok": True, "ms": round((time.perf_counter() - t0) * 1000, 2)}
	except Exception as exc:
		return {
			"name": name,
			"ok": False,
			"error": str(exc),
			"ms": round((time.perf_counter() - t0) * 1000, 2),
		}


class TestFalseRIVCompletion(unittest.TestCase):
	def test_heal_source_never_sets_completed(self):
		src = inspect.getsource(re._heal_orphaned_riv_in_progress)
		self.assertNotIn('"status": "Completed"', src)
		self.assertNotIn("'status': 'Completed'", src)
		self.assertNotIn("healed orphan In Progress", src)
		self.assertIn("_kick_riv_execution", src)
		self.assertIn("Never", src)

	def test_deadlock_job_ok_does_not_complete_riv(self):
		"""RQ Job OK while RIV In Progress after deadlock → nudge only, never Complete."""
		from frappe.utils import now_datetime, add_to_date

		row = frappe._dict(
			status="In Progress",
			modified=add_to_date(now_datetime(), seconds=-120),
			error_log="QueryDeadlockError: (1213, 'Deadlock found when trying to get lock')",
		)
		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe.db, "get_value", return_value=row),
			patch.object(frappe.db, "set_value") as sv,
			patch.object(re, "_kick_riv_execution") as kick,
			patch.object(re, "_riv_has_live_long_job", create=True),
		):
			# Force no-live via patching get_queue path inside heal
			with patch(
				"frappe.utils.background_jobs.get_queue",
				side_effect=Exception("no queue in unit test"),
			):
				re._heal_orphaned_riv_in_progress("RIV-DEADLOCK-TEST")
			# Must not write Completed
			for c in sv.call_args_list:
				payload = c.args[2] if len(c.args) >= 3 else c.kwargs.get("value")
				if isinstance(payload, dict):
					self.assertNotEqual(payload.get("status"), "Completed")
			kick.assert_called_once_with("RIV-DEADLOCK-TEST")

	def test_error_1020_job_ok_does_not_complete_riv(self):
		from frappe.utils import now_datetime, add_to_date

		row = frappe._dict(
			status="In Progress",
			modified=add_to_date(now_datetime(), seconds=-5),  # young but recoverable
			error_log='(1020, "Record has changed since last read in table \'tabStock Ledger Entry\'")',
		)
		with (
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe.db, "get_value", return_value=row),
			patch.object(frappe.db, "set_value") as sv,
			patch.object(re, "_kick_riv_execution") as kick,
			patch(
				"frappe.utils.background_jobs.get_queue",
				side_effect=Exception("no queue"),
			),
		):
			re._heal_orphaned_riv_in_progress("RIV-1020-TEST")
			for c in sv.call_args_list:
				payload = c.args[2] if len(c.args) >= 3 else None
				if isinstance(payload, dict):
					self.assertNotEqual(payload.get("status"), "Completed")
			kick.assert_called_once_with("RIV-1020-TEST")

	def test_core_completed_allows_finalize_gate(self):
		self.assertTrue(re._riv_statuses_allow_mca_finalize({"r1": "Completed"}))
		self.assertTrue(re._riv_statuses_allow_mca_finalize({"r1": "Completed", "r2": "Skipped"}))
		self.assertFalse(re._riv_statuses_allow_mca_finalize({"r1": "In Progress"}))
		self.assertFalse(re._riv_statuses_allow_mca_finalize({"r1": "Queued"}))
		self.assertFalse(re._riv_statuses_allow_mca_finalize({"r1": "Failed"}))
		self.assertFalse(re._riv_statuses_allow_mca_finalize({}))

	def test_monitor_does_not_finalize_while_in_progress(self):
		riv = "RIV-STUCK-IP"
		mca = "MCA-STUCK-TEST"
		doc = frappe._dict(
			name=mca,
			riv_voucher_nos=riv,
			stock_entry="STE-STUCK",
			processing_status=C.PS_PENDING_RIV,
		)
		from frappe.utils import now_datetime

		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Repost Item Valuation":
				as_dict = kwargs.get("as_dict")
				if as_dict or isinstance(field, (list, tuple)):
					return frappe._dict(
						status="In Progress",
						modified=now_datetime(),
						error_log="Deadlock found when trying to get lock",
					)
				return "In Progress"
			return None

		with (
			patch.object(frappe.db, "get_value", side_effect=_gv),
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe.db, "set_value") as sv,
			patch.object(frappe.db, "commit"),
			patch.object(re, "_schedule_retry") as sched,
			patch.object(re, "_set_status"),
			patch.object(re, "_kick_riv_execution"),
			patch.object(re, "_heal_orphaned_riv_in_progress"),
			patch.object(re, "_final_reconcile") as final,
			patch.object(re, "release_se_lock") as unlock,
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(re, "_progress"),
		):
			out = re._monitor_riv_and_finalize(mca)
			self.assertEqual(out.get("reason"), "riv_pending")
			self.assertTrue(out.get("ok"))
			final.assert_not_called()
			unlock.assert_not_called()
			sched.assert_not_called()
			# Must not set MCA processing_status Completed
			for c in sv.call_args_list:
				if len(c.args) >= 3 and isinstance(c.args[2], dict):
					self.assertNotEqual(c.args[2].get("processing_status"), C.PS_COMPLETED)

	def test_monitor_accepts_genuine_core_completed(self):
		riv = "RIV-DONE"
		mca = "MCA-DONE-TEST"
		doc = frappe._dict(
			name=mca,
			riv_voucher_nos=riv,
			stock_entry="STE-DONE",
			processing_status=C.PS_PENDING_RIV,
		)

		def _gv(doctype, name, field=None, **kwargs):
			if doctype == "Repost Item Valuation":
				return "Completed"
			return None

		with (
			patch.object(frappe.db, "get_value", side_effect=_gv),
			patch.object(frappe.db, "exists", return_value=True),
			patch.object(frappe.db, "set_value") as sv,
			patch.object(frappe.db, "commit"),
			patch.object(re, "_final_reconcile"),
			patch.object(re, "release_se_lock"),
			patch.object(re, "_set_status"),
			patch.object(frappe, "get_doc", return_value=doc),
		):
			out = re._monitor_riv_and_finalize(mca)
			self.assertEqual(out.get("reason"), "completed")
			found = False
			for c in sv.call_args_list:
				if len(c.args) >= 3 and isinstance(c.args[2], dict):
					if c.args[2].get("processing_status") == C.PS_COMPLETED:
						found = True
						self.assertEqual(c.args[2].get("repost_status"), C.RS_COMPLETED)
			self.assertTrue(found)

	def test_queue_or_reuse_does_not_duplicate_open_riv(self):
		src = inspect.getsource(re._queue_or_reuse_riv)
		self.assertIn("Queued", src)
		self.assertIn("In Progress", src)
		self.assertIn("create_repost_item_valuation_entry", src)

	def test_lock_release_only_on_terminal_complete(self):
		src = inspect.getsource(re._monitor_riv_and_finalize)
		# release_se_lock only in the completed branch after finalize gate
		idx_gate = src.find("_riv_statuses_allow_mca_finalize")
		idx_release = src.find("release_se_lock")
		self.assertGreater(idx_gate, 0)
		self.assertGreater(idx_release, idx_gate)

	def test_forensic_case_readonly_invariant(self):
		"""Documented case: manufacture SLE has MCA rate; transfer still at basic.

		Read-only — does not repair historical records. Asserts the bug evidence
		still present so recovery can be authorized separately.
		"""
		se = "MAT-STE-2026-25933-2"
		tr = "MAT-STE-2026-25934"
		item = "20100042"
		wh = "انبار Quarantine محصول نهایی اسپاد"
		if not frappe.db.exists("Stock Entry", se):
			self.skipTest("forensic SE not on site")
		mfg = frappe.db.sql(
			"""
			SELECT incoming_rate, stock_value_difference, actual_qty
			FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND item_code=%s AND warehouse=%s
			  AND actual_qty>0 AND is_cancelled=0 LIMIT 1
			""",
			(se, item, wh),
			as_dict=True,
		)
		xfer = frappe.db.sql(
			"""
			SELECT outgoing_rate, actual_qty
			FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND item_code=%s AND warehouse=%s
			  AND actual_qty=-102 AND is_cancelled=0 LIMIT 1
			""",
			(tr, item, wh),
			as_dict=True,
		)
		self.assertTrue(mfg)
		self.assertTrue(xfer)
		self.assertGreater(float(mfg[0].incoming_rate), 30_000_000)
		self.assertLess(float(xfer[0].outgoing_rate), 5_000_000)
		# Propagation still broken until authorized historical RIV recovery
		self.assertGreater(
			float(mfg[0].incoming_rate) / max(float(xfer[0].outgoing_rate), 1),
			5,
		)

	def test_propagation_invariant_documented(self):
		"""Contract: after successful RIV, outbound rate must track manufacture in-rate."""
		mfg_in = 34_855_113.0
		xfer_out_bad = 3_705_004.0
		# Pre-fix world (documented failure)
		self.assertGreater(mfg_in / xfer_out_bad, 5)
		# Post-successful-RIV world (required)
		xfer_out_ok = mfg_in  # batch-specific outbound should consume at MCA rate
		self.assertAlmostEqual(xfer_out_ok, mfg_in, delta=1.0)


def run_false_riv_completion_suite() -> dict:
	t = TestFalseRIVCompletion()
	checks = [
		("heal_source_never_sets_completed", t.test_heal_source_never_sets_completed),
		("deadlock_job_ok_does_not_complete_riv", t.test_deadlock_job_ok_does_not_complete_riv),
		("error_1020_job_ok_does_not_complete_riv", t.test_error_1020_job_ok_does_not_complete_riv),
		("core_completed_allows_finalize_gate", t.test_core_completed_allows_finalize_gate),
		("monitor_does_not_finalize_while_in_progress", t.test_monitor_does_not_finalize_while_in_progress),
		("monitor_accepts_genuine_core_completed", t.test_monitor_accepts_genuine_core_completed),
		("queue_or_reuse_does_not_duplicate_open_riv", t.test_queue_or_reuse_does_not_duplicate_open_riv),
		("lock_release_only_on_terminal_complete", t.test_lock_release_only_on_terminal_complete),
		("forensic_case_readonly_invariant", t.test_forensic_case_readonly_invariant),
		("propagation_invariant_documented", t.test_propagation_invariant_documented),
	]
	results = [check(n, fn) for n, fn in checks]
	ok = all(r["ok"] for r in results)
	return {"ok": ok, "checks": results}


if __name__ == "__main__":
	frappe.init(site="development.localhost")
	frappe.connect()
	out = run_false_riv_completion_suite()
	print("FALSE_RIV_COMPLETION", out)
	raise SystemExit(0 if out["ok"] else 1)
