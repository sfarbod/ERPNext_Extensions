# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.17: Campaign status progress sync — scopes, read-only, invariants.

Does not fake document Pending by subtracting RIV counts.
"""

from __future__ import annotations

import unittest
from unittest import mock

from erpnext_extensions.iran_accounting.historical_rebuild import campaign as camp
from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	ITEM_BLOCKED,
	ITEM_COMPLETED,
	ITEM_FAILED,
	ITEM_PENDING,
	ITEM_SKIPPED,
	STATUS_PAUSED,
	STATUS_RUNNING,
)


def _fake_doc(name="HRC-00004"):
	doc = mock.Mock()
	doc.name = name
	doc.status = STATUS_RUNNING
	doc.execution_logs = []
	doc.current_voucher_type = "Repost Item Valuation"
	doc.current_voucher_no = "riv-current"
	doc.last_voucher_type = "ITEM-A"
	doc.last_voucher_no = "WH-1"
	doc.last_ordinal = 1
	doc.last_activity = None
	doc.started_at = None
	doc.completed_at = None
	doc.manifest_checksum = "abc123checksum"
	doc.reload = mock.Mock()
	return doc


def _sess(**kwargs):
	base = {
		"session_id": "20261025184803",
		"campaign": "HRC-00004",
		"status": "Running",
		"riv_completed": ["r1", "r2"],
		"riv_failed": [],
		"riv_created": ["r1", "r2", "r3"],
		"identities_done": ["ITEM-A|WH-1", "ITEM-B|WH-1"],
		"plan_size": 100,
		"started_at": "2026-10-25 18:48:03",
		"last_failure": None,
		"revision": 7,
		"current_riv": "r3",
		"current_item_code": "ITEM-C",
		"current_warehouse": "WH-1",
	}
	base.update(kwargs)
	return base


class TestComputeCountersInvariant(unittest.TestCase):
	def test_invariant_completed_plus_skipped_plus_pending(self):
		"""total = completed + pending + skipped + failed + blocked."""
		rows = [
			mock.Mock(item_status=ITEM_COMPLETED, c=10),
			mock.Mock(item_status=ITEM_SKIPPED, c=205),
			mock.Mock(item_status=ITEM_PENDING, c=16370),
			mock.Mock(item_status=ITEM_FAILED, c=0),
			mock.Mock(item_status=ITEM_BLOCKED, c=0),
		]
		# Mimic sql returning as_dict with .c attribute — use dict-like
		as_dicts = [
			{"item_status": ITEM_COMPLETED, "c": 10},
			{"item_status": ITEM_SKIPPED, "c": 205},
			{"item_status": ITEM_PENDING, "c": 16360},
			{"item_status": ITEM_FAILED, "c": 0},
			{"item_status": ITEM_BLOCKED, "c": 0},
		]
		# frappe.db.sql as_dict=True returns objects with attributes in real life;
		# our code uses r.item_status / r.c — use SimpleNamespace-like mocks.
		class R:
			def __init__(self, d):
				self.item_status = d["item_status"]
				self.c = d["c"]

		sql_calls = {"n": 0}

		def fake_sql(*args, **kwargs):
			sql_calls["n"] += 1
			# First call: item_status group; second: riv_names
			if sql_calls["n"] == 1:
				return [R(d) for d in as_dicts]
			return []

		with mock.patch.object(camp.frappe.db, "sql", side_effect=fake_sql):
			out = camp._compute_campaign_counters("HRC-00004")

		self.assertEqual(out["total"], 16575)
		self.assertEqual(out["completed"], 10)
		self.assertEqual(out["skipped_covered"], 205)
		self.assertEqual(out["pending"], 16360)
		self.assertEqual(
			out["total"],
			out["completed"] + out["pending"] + out["skipped_covered"] + out["failed"] + out["blocked"],
		)
		# Sequential RIVs are NOT in counters.riv_*
		self.assertEqual(out["riv_created"], 0)
		self.assertEqual(out["riv_completed"], 0)


class TestProgressViewSemantics(unittest.TestCase):
	def test_verified_is_completed_plus_skipped_not_riv_count(self):
		counters = {
			"total": 16575,
			"completed": 0,
			"skipped_covered": 205,
			"pending": 16370,
			"failed": 0,
			"blocked": 0,
			"riv_created": 0,
			"riv_completed": 0,
			"riv_queued": 0,
			"riv_in_progress": 0,
			"riv_failed": 0,
		}
		seq = {
			"session_riv_created": 43,
			"session_riv_completed": 42,
			"session_riv_failed": 0,
			"identities_done": 42,
			"plan_size": 5000,
			"session_status": "Running",
			"current_riv": "r43",
			"active_session_id": "20261025184803",
			"direct_verified": 69,
			"covered_verified": 136,
			"pending_verification": 16370,
		}
		prog = camp._build_progress_view(counters, seq)
		self.assertEqual(prog["documents"]["verified"], 205)
		self.assertNotEqual(prog["documents"]["verified"], 42)  # never RIV count
		self.assertEqual(prog["documents"]["pending"], 16370)
		self.assertTrue(prog["documents"]["invariant_ok"])
		self.assertEqual(prog["sequential_riv"]["created"], 43)
		self.assertEqual(prog["sequential_riv"]["completed"], 42)
		self.assertEqual(prog["manifest_linked_riv"]["created"], 0)
		self.assertEqual(prog["identities"]["verified"], 42)
		self.assertEqual(prog["identities"]["total"], 5000)
		self.assertAlmostEqual(prog["documents"]["progress_pct"], round(205 / 16575 * 100, 3))

	def test_no_fabricated_identity_total(self):
		counters = {
			"total": 10,
			"completed": 0,
			"skipped_covered": 2,
			"pending": 8,
			"failed": 0,
			"blocked": 0,
			"riv_created": 0,
			"riv_completed": 0,
			"riv_queued": 0,
			"riv_in_progress": 0,
			"riv_failed": 0,
		}
		seq = {
			"session_riv_created": 1,
			"session_riv_completed": 1,
			"session_riv_failed": 0,
			"identities_done": 1,
			"plan_size": None,
			"session_status": "Running",
			"current_riv": None,
			"active_session_id": "x",
			"direct_verified": 1,
			"covered_verified": 1,
			"pending_verification": 8,
		}
		prog = camp._build_progress_view(counters, seq)
		self.assertIsNone(prog["identities"]["total"])
		self.assertIsNone(prog["identities"]["progress_pct"])


class TestStatusReadOnlyAndScopes(unittest.TestCase):
	def test_status_uses_compute_not_refresh_persist(self):
		"""status() must not call _persist / _refresh (no Campaign write)."""
		fake = _fake_doc()
		counters = {
			"total": 16575,
			"completed": 0,
			"skipped_covered": 205,
			"pending": 16370,
			"failed": 0,
			"blocked": 0,
			"riv_created": 0,
			"riv_completed": 0,
			"riv_queued": 0,
			"riv_in_progress": 0,
			"riv_failed": 0,
		}
		persist = mock.Mock()
		refresh = mock.Mock(return_value=counters)

		seq_block = {
			"active_session_id": "20261025184803",
			"session_status": "Running",
			"session_riv_completed": 2,
			"session_riv_failed": 0,
			"session_riv_created": 3,
			"identities_done": 2,
			"plan_size": 100,
			"direct_verified": 69,
			"covered_verified": 136,
			"pending_verification": 16370,
			"current_riv": "r3",
			"current_identity": {"item_code": "ITEM-C", "warehouse": "WH-1"},
			"last_failure": None,
			"elapsed_seconds": 10.0,
			"avg_riv_seconds": 5.0,
			"estimated_remaining_seconds": 490.0,
			"revision": 7,
		}
		with mock.patch.object(camp, "_require_campaign", return_value=fake), mock.patch.object(
			camp, "_compute_campaign_counters", return_value=counters
		) as compute, mock.patch.object(
			camp, "_persist_campaign_counters", persist
		), mock.patch.object(
			camp, "_refresh_counters", refresh
		), mock.patch.object(
			camp, "_sequential_status_block", return_value=seq_block
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.get_all",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.db.sql",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.db.count",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.db.set_value"
		) as set_value, mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.db.commit"
		) as commit:
			out = camp.status("HRC-00004")

		compute.assert_called_once_with("HRC-00004")
		refresh.assert_not_called()
		persist.assert_not_called()
		set_value.assert_not_called()
		commit.assert_not_called()
		self.assertTrue(out["read_only"])
		self.assertEqual(out["manifest_checksum"], "abc123checksum")
		self.assertEqual(out["progress"]["documents"]["verified"], 205)
		self.assertEqual(out["progress"]["sequential_riv"]["completed"], 2)
		self.assertEqual(out["progress"]["manifest_linked_riv"]["created"], 0)
		self.assertIn("Verified Documents", out["text"])
		self.assertIn("Sequential RIV Completed", out["text"])
		self.assertIn("Manifest-linked RIV", out["text"])
		self.assertIn("counter_semantics", out)

	def test_session_and_campaign_riv_scopes_remain_separate(self):
		fake = _fake_doc()
		counters = {
			"total": 100,
			"completed": 0,
			"skipped_covered": 5,
			"pending": 95,
			"failed": 0,
			"blocked": 0,
			"riv_created": 0,
			"riv_completed": 0,
			"riv_queued": 0,
			"riv_in_progress": 0,
			"riv_failed": 0,
		}
		seq = _sess(
			riv_completed=["a"] * 42,
			riv_created=["a"] * 43,
			identities_done=["x"] * 42,
			plan_size=4000,
			direct_verified=2,
			covered_verified=3,
			pending_verification=95,
			session_riv_completed=42,  # ignored — block rebuilds from lists
		)
		# _sequential_status_block returns computed lens; patch the block directly
		seq_block = {
			"active_session_id": "20261025184803",
			"session_status": "Running",
			"session_riv_completed": 42,
			"session_riv_failed": 0,
			"session_riv_created": 43,
			"identities_done": 42,
			"plan_size": 4000,
			"direct_verified": 2,
			"covered_verified": 3,
			"pending_verification": 95,
			"current_riv": "r43",
			"current_identity": {"item_code": "I", "warehouse": "W"},
			"last_failure": None,
			"elapsed_seconds": 10,
			"avg_riv_seconds": 1,
			"estimated_remaining_seconds": 100,
		}
		with mock.patch.object(camp, "_require_campaign", return_value=fake), mock.patch.object(
			camp, "_compute_campaign_counters", return_value=counters
		), mock.patch.object(
			camp, "_sequential_status_block", return_value=seq_block
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.get_all",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.db.sql",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.db.count",
			return_value=0,
		):
			out = camp.status("HRC-00004")

		self.assertEqual(out["counters"]["riv_created"], 0)
		self.assertEqual(out["counters"]["riv_completed"], 0)
		self.assertEqual(out["sequential"]["session_riv_created"], 43)
		self.assertEqual(out["sequential"]["session_riv_completed"], 42)
		# Pending unchanged by RIV completion count
		self.assertEqual(out["counters"]["pending"], 95)
		self.assertEqual(out["progress"]["documents"]["verified"], 5)

	def test_previous_status_api_still_has_sequential_block(self):
		"""Regression vs v5.6.1 status shape."""
		fake = _fake_doc("HRC-00001")
		fake.status = STATUS_PAUSED
		with mock.patch.object(camp, "_require_campaign", return_value=fake), mock.patch.object(
			camp,
			"_compute_campaign_counters",
			return_value={
				"total": 10,
				"completed": 0,
				"skipped_covered": 0,
				"pending": 10,
				"failed": 0,
				"blocked": 0,
				"riv_created": 0,
				"riv_completed": 0,
				"riv_queued": 0,
				"riv_in_progress": 0,
				"riv_failed": 0,
			},
		), mock.patch.object(
			camp,
			"_sequential_status_block",
			return_value={
				"active_session_id": "s1",
				"session_status": "Paused",
				"session_riv_completed": 1,
				"session_riv_failed": 0,
				"session_riv_created": 1,
				"identities_done": 1,
				"plan_size": 10,
				"direct_verified": 0,
				"covered_verified": 0,
				"pending_verification": 10,
				"current_riv": None,
				"current_identity": None,
				"last_failure": None,
				"elapsed_seconds": None,
				"avg_riv_seconds": None,
				"estimated_remaining_seconds": None,
			},
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.get_all",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.db.sql",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.db.count",
			return_value=0,
		):
			out = camp.status("HRC-00001")
		self.assertIn("sequential", out)
		self.assertIn("counters", out)
		self.assertIn("progress", out)


class TestSequentialPlanSizePersisted(unittest.TestCase):
	def test_run_sequential_sets_plan_size_before_loop(self):
		from erpnext_extensions.iran_accounting.historical_rebuild import sequential_rebuild as sr

		saved = []

		def capture_save(payload):
			saved.append(dict(payload))
			payload["revision"] = int(payload.get("revision") or 0) + 1

		sess = {
			"session_id": "s1",
			"campaign": "HRC-X",
			"status": "Ready",
			"riv_created": [],
			"riv_completed": [],
			"riv_failed": [],
			"identities_done": [],
			"revision": 1,
		}
		idents = [mock.Mock(item_code="A", warehouse="W", posting_date="2024-01-01", posting_time="00:00:00")] * 3

		with mock.patch.object(sr, "_ensure_bootstrap"), mock.patch.object(
			sr, "_empty_rq"
		), mock.patch.object(sr, "get_seq_session", return_value=sess), mock.patch.object(
			sr, "_acquire_executor_lock"
		), mock.patch.object(
			sr, "_release_executor_lock"
		), mock.patch.object(
			sr, "chronological_identities", return_value=idents
		), mock.patch.object(
			sr, "clear_sequential_stop"
		), mock.patch.object(
			sr, "_save_seq", side_effect=capture_save
		), mock.patch.object(
			sr, "sync_campaign_execution_state"
		), mock.patch.object(
			sr, "_wait_until_riv_queue_clear", return_value={"stopped": True, "lock_lost": False}
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.frappe.db.get_value",
			return_value="Co",
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.frappe.db.commit"
		):
			out = sr.run_sequential("HRC-X", max_identities=1)

		self.assertTrue(saved)
		self.assertEqual(saved[0].get("plan_size"), 3)
		self.assertTrue(out.get("stopped_by_flag") or out.get("gates_ok") is False)


if __name__ == "__main__":
	unittest.main()
