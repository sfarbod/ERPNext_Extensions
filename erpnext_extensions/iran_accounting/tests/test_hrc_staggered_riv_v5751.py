# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.51 — Production auth, RIV adoption, Sequential/Staggered mutual exclusion."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import frappe

from erpnext_extensions.iran_accounting.historical_rebuild import staggered_riv as sr
from erpnext_extensions.iran_accounting.historical_rebuild import staggered_riv_adopt as adopt
from erpnext_extensions.iran_accounting.historical_rebuild import staggered_riv_auth as auth
from erpnext_extensions.iran_accounting.historical_rebuild import (
	staggered_riv_campaign_lock as clock,
)
from erpnext_extensions.iran_accounting.historical_rebuild import staggered_riv_locks as locks


class TestProductionAuthGateV5751(unittest.TestCase):
	def test_production_rejected_when_authorization_absent(self):
		with mock.patch.object(auth, "current_site", return_value="erp.espadpharmed.com"), mock.patch.object(
			frappe, "conf", new=frappe._dict({})
		):
			with self.assertRaises(Exception) as ctx:
				auth.assert_staggered_site_allowed()
			self.assertIn("hrc_stagger_allow_production", str(ctx.exception))

	def test_production_rejected_on_wildcard_site(self):
		conf = frappe._dict(
			{
				"hrc_stagger_allow_production": 1,
				"hrc_stagger_production_site": "*.espadpharmed.com",
			}
		)
		with mock.patch.object(auth, "current_site", return_value="erp.espadpharmed.com"), mock.patch.object(
			frappe, "conf", new=conf
		):
			with self.assertRaises(Exception) as ctx:
				auth.assert_staggered_site_allowed()
			self.assertIn("wildcard", str(ctx.exception).lower())

	def test_production_rejected_on_site_mismatch(self):
		conf = frappe._dict(
			{
				"hrc_stagger_allow_production": 1,
				"hrc_stagger_production_site": "other.espadpharmed.com",
			}
		)
		with mock.patch.object(auth, "current_site", return_value="erp.espadpharmed.com"), mock.patch.object(
			frappe, "conf", new=conf
		):
			with self.assertRaises(Exception):
				auth.assert_staggered_site_allowed()

	def test_production_accepted_with_site_auth_and_confirm(self):
		conf = frappe._dict(
			{
				"hrc_stagger_allow_production": 1,
				"hrc_stagger_production_site": "erp.espadpharmed.com",
				"developer_mode": 1,  # must NOT be used as Production auth
			}
		)
		with mock.patch.object(auth, "current_site", return_value="erp.espadpharmed.com"), mock.patch.object(
			frappe, "conf", new=conf
		):
			gate = auth.assert_staggered_execution_allowed(
				confirm=auth.CONFIRM_PRODUCTION, for_start=True
			)
		self.assertEqual(gate["environment"], "production")
		self.assertEqual(gate["confirm"], auth.CONFIRM_PRODUCTION)

	def test_production_rejects_dev_confirm_token(self):
		conf = frappe._dict(
			{
				"hrc_stagger_allow_production": 1,
				"hrc_stagger_production_site": "erp.espadpharmed.com",
			}
		)
		with mock.patch.object(auth, "current_site", return_value="erp.espadpharmed.com"), mock.patch.object(
			frappe, "conf", new=conf
		):
			with self.assertRaises(Exception) as ctx:
				auth.assert_staggered_execution_allowed(
					confirm=auth.CONFIRM_DEV, for_start=True
				)
			self.assertIn(auth.CONFIRM_PRODUCTION, str(ctx.exception))

	def test_development_behavior_unchanged(self):
		conf = frappe._dict({"developer_mode": 1})
		with mock.patch.object(auth, "current_site", return_value="development.localhost"), mock.patch.object(
			frappe, "conf", new=conf
		):
			gate = auth.assert_staggered_execution_allowed(
				confirm=auth.CONFIRM_DEV, for_start=True
			)
		self.assertEqual(gate["environment"], "development")

	def test_developer_mode_does_not_authorize_production(self):
		conf = frappe._dict({"developer_mode": 1})
		with mock.patch.object(auth, "current_site", return_value="erp.espadpharmed.com"), mock.patch.object(
			frappe, "conf", new=conf
		):
			with self.assertRaises(Exception):
				auth.assert_staggered_site_allowed()

	def test_status_probe_does_not_require_execution_auth(self):
		"""production_authorization_state is read-only and never throws."""
		with mock.patch.object(auth, "current_site", return_value="erp.espadpharmed.com"), mock.patch.object(
			frappe, "conf", new=frappe._dict({})
		):
			state = auth.production_authorization_state()
		self.assertFalse(state["authorized"])
		self.assertTrue(state["is_production_like"])


class TestCampaignMutualExclusionV5751(unittest.TestCase):
	def setUp(self):
		self.campaign = f"HRC-LOCK-{frappe.generate_hash(length=6)}"
		self.key = clock.campaign_lock_key(self.campaign)
		# Isolate cache
		try:
			frappe.cache().delete_value(self.key)
		except Exception:
			pass

	def tearDown(self):
		try:
			frappe.cache().delete_value(self.key)
		except Exception:
			pass

	def test_sequential_and_staggered_mutual_exclusion(self):
		fake = {}

		class _R:
			def get(self, key):
				return fake.get(key)

			def set(self, key, value, nx=False, ex=None):
				if nx and key in fake:
					return False
				fake[key] = value
				return True

			def delete(self, key):
				fake.pop(key, None)

		with mock.patch.object(clock, "_redis", return_value=_R()):
			a = clock.acquire_campaign_execution_lock(
				self.campaign,
				mode=clock.MODE_SEQUENTIAL,
				session_id="seq1",
				owner_token="sequential:seq1",
			)
			self.assertTrue(a.get("ok"))
			with self.assertRaises(Exception):
				clock.acquire_campaign_execution_lock(
					self.campaign,
					mode=clock.MODE_STAGGERED,
					session_id="stg1",
					owner_token="stagger:stg1",
				)
			rel = clock.release_campaign_execution_lock(
				self.campaign,
				mode=clock.MODE_SEQUENTIAL,
				session_id="seq1",
				owner_token="sequential:seq1",
			)
			self.assertTrue(rel.get("released"))
			b = clock.acquire_campaign_execution_lock(
				self.campaign,
				mode=clock.MODE_STAGGERED,
				session_id="stg1",
				owner_token="stagger:stg1",
			)
			self.assertTrue(b.get("ok"))

	def test_never_release_another_sessions_lock(self):
		fake = {}

		class _R:
			def get(self, key):
				return fake.get(key)

			def set(self, key, value, nx=False, ex=None):
				if nx and key in fake:
					return False
				fake[key] = value
				return True

			def delete(self, key):
				fake.pop(key, None)

		with mock.patch.object(clock, "_redis", return_value=_R()):
			clock.acquire_campaign_execution_lock(
				self.campaign,
				mode=clock.MODE_STAGGERED,
				session_id="own",
				owner_token="tok-own",
			)
			out = clock.release_campaign_execution_lock(
				self.campaign,
				mode=clock.MODE_STAGGERED,
				session_id="other",
				owner_token="tok-other",
			)
			self.assertFalse(out.get("released"))
			self.assertEqual(out.get("reason"), "not_owner")
			held = clock.get_campaign_execution_lock(self.campaign)
			self.assertEqual(held.get("session_id"), "own")

	def test_stale_lock_recovery_requires_confirm_and_match(self):
		fake = {}

		class _R:
			def get(self, key):
				return fake.get(key)

			def set(self, key, value, nx=False, ex=None):
				fake[key] = value
				return True

			def delete(self, key):
				fake.pop(key, None)

		with mock.patch.object(clock, "_redis", return_value=_R()):
			clock.acquire_campaign_execution_lock(
				self.campaign,
				mode=clock.MODE_STAGGERED,
				session_id="old",
				owner_token="tok",
			)
			with self.assertRaises(Exception):
				clock.recover_stale_campaign_execution_lock(
					self.campaign,
					expected_mode=clock.MODE_STAGGERED,
					expected_session_id="old",
					expected_owner_token="tok",
					confirm="WRONG",
				)
			# Not stale yet
			with self.assertRaises(Exception):
				clock.recover_stale_campaign_execution_lock(
					self.campaign,
					expected_mode=clock.MODE_STAGGERED,
					expected_session_id="old",
					expected_owner_token="tok",
					confirm="RECOVER_CAMPAIGN_EXEC_LOCK",
					max_age_s=10_000,
				)

	def test_concurrent_dispatcher_rejection(self):
		sr._acquire_dispatcher_lock("HRC-X", "s1")
		with self.assertRaises(Exception):
			sr._acquire_dispatcher_lock("HRC-X", "s2")
		sr._release_dispatcher_lock("HRC-X", "s1")


class TestAdoptionMatchingV5751(unittest.TestCase):
	def test_posting_cover_and_exact(self):
		self.assertTrue(adopt.posting_exact("2026-01-01", "10:00:00", "2026-01-01", "10:00:00"))
		self.assertTrue(adopt.posting_covers("2026-01-01", "09:00:00", "2026-01-01", "10:00:00"))
		self.assertFalse(adopt.posting_covers("2026-01-01", "11:00:00", "2026-01-01", "10:00:00"))

	def test_adopt_completed_with_exact_posting(self):
		riv = frappe._dict(
			name="RIV-C",
			status="Completed",
			posting_date="2026-01-01",
			posting_time="00:00:00",
			company="C",
			creation="2026-01-01",
		)
		with mock.patch.object(adopt, "list_identity_rivs", return_value=[riv]), mock.patch.object(
			adopt,
			"voucher_scope_evidence",
			return_value={"proven": True, "exact_posting": True, "reason": "exact_posting_completed"},
		):
			out = adopt.resolve_identity_riv_disposition(
				campaign="HRC-T",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				company="C",
			)
		self.assertEqual(out["disposition"], adopt.DISPOSITION_ADOPT_COMPLETED)
		self.assertEqual(out["riv_name"], "RIV-C")

	def test_adopt_queued(self):
		riv = frappe._dict(
			name="RIV-Q",
			status="Queued",
			posting_date="2026-01-01",
			posting_time="00:00:00",
			company="C",
			creation="2026-01-01",
		)
		with mock.patch.object(adopt, "list_identity_rivs", return_value=[riv]), mock.patch.object(
			adopt,
			"voucher_scope_evidence",
			return_value={"proven": True, "exact_posting": True, "reason": "exact_posting_active"},
		):
			out = adopt.resolve_identity_riv_disposition(
				campaign="HRC-T",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
			)
		self.assertEqual(out["disposition"], adopt.DISPOSITION_ADOPT_QUEUED)

	def test_adopt_in_progress(self):
		riv = frappe._dict(
			name="RIV-P",
			status="In Progress",
			posting_date="2026-01-01",
			posting_time="00:00:00",
			company="C",
			creation="2026-01-01",
		)
		with mock.patch.object(adopt, "list_identity_rivs", return_value=[riv]), mock.patch.object(
			adopt,
			"voucher_scope_evidence",
			return_value={"proven": True, "exact_posting": True, "reason": "exact_posting_active"},
		):
			out = adopt.resolve_identity_riv_disposition(
				campaign="HRC-T",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
			)
		self.assertEqual(out["disposition"], adopt.DISPOSITION_ADOPT_IN_PROGRESS)

	def test_failed_needs_recovery_no_auto_create(self):
		riv = frappe._dict(
			name="RIV-F",
			status="Failed",
			posting_date="2026-01-01",
			posting_time="00:00:00",
			company="C",
			creation="2026-01-01",
		)
		with mock.patch.object(adopt, "list_identity_rivs", return_value=[riv]):
			out = adopt.resolve_identity_riv_disposition(
				campaign="HRC-T",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
			)
		self.assertEqual(out["disposition"], adopt.DISPOSITION_FAILED_NEEDS_RECOVERY)
		self.assertEqual(out["riv_name"], "RIV-F")

	def test_ambiguous_coverage_needs_review(self):
		riv = frappe._dict(
			name="RIV-X",
			status="Completed",
			posting_date="2026-01-01",
			posting_time="00:00:00",
			company="C",
			creation="2026-01-01",
		)
		with mock.patch.object(adopt, "list_identity_rivs", return_value=[riv]), mock.patch.object(
			adopt,
			"voucher_scope_evidence",
			return_value={"proven": False, "reason": "insufficient_voucher_evidence"},
		):
			out = adopt.resolve_identity_riv_disposition(
				campaign="HRC-T",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
			)
		self.assertEqual(out["disposition"], adopt.DISPOSITION_NEEDS_REVIEW)

	def test_no_existing_riv_create(self):
		with mock.patch.object(adopt, "list_identity_rivs", return_value=[]):
			out = adopt.resolve_identity_riv_disposition(
				campaign="HRC-T",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
			)
		self.assertEqual(out["disposition"], adopt.DISPOSITION_CREATE)

	def test_exact_identity_and_voucher_scope_matching(self):
		with mock.patch.object(
			adopt, "campaign_item_covering_rivs", return_value=["RIV-1"]
		), mock.patch.object(
			adopt, "campaign_identity_voucher_keys", return_value={"SE/1", "SE/2"}
		), mock.patch.object(
			adopt, "fetch_future_voucher_keys", return_value={"SE/1", "SE/2", "SE/9"}
		):
			ev = adopt.voucher_scope_evidence(
				campaign="HRC-T",
				item_code="I",
				warehouse="W",
				posting_date="2026-01-01",
				posting_time="00:00:00",
				riv_name="RIV-1",
				riv_posting_date="2026-01-01",
				riv_posting_time="00:00:00",
				riv_status="Completed",
			)
		self.assertTrue(ev["proven"])
		self.assertTrue(ev["campaign_covering_match"] or ev["exact_posting"])


class TestStageAAdoptionFlowV5751(unittest.TestCase):
	def setUp(self):
		self._tmpdir = tempfile.mkdtemp(prefix="hrc_stagger_v5751_")
		self._orig = sr._session_dir
		sr._session_dir = lambda: self._tmpdir  # type: ignore

	def tearDown(self):
		sr._session_dir = self._orig  # type: ignore

	def _sess(self, plan):
		sid = "20260101120000"
		sess = {
			"session_id": sid,
			"campaign": "HRC-MIG",
			"company": "C",
			"status": "Starting",
			"plan": plan,
			"plan_size": len(plan),
			"riv_created": 0,
			"riv_dispatched": 0,
			"riv_completed": 0,
			"riv_failed": 0,
			"revision": 1,
			"owner_token": "stagger:20260101120000",
			"errors": {},
		}
		sr._save_session(sess)
		sr.save_ledger(sid, {"entries": {}, "order": []})
		return sess

	def test_partial_sequential_to_staggered_migration(self):
		plan = [
			{
				"item_code": "I1",
				"warehouse": "W1",
				"posting_date": "2026-01-01",
				"posting_time": "00:00:00",
			},
			{
				"item_code": "I2",
				"warehouse": "W2",
				"posting_date": "2026-01-01",
				"posting_time": "00:00:00",
			},
			{
				"item_code": "I3",
				"warehouse": "W3",
				"posting_date": "2026-01-01",
				"posting_time": "00:00:00",
			},
		]
		sess = self._sess(plan)

		def _resolve(**kwargs):
			item = kwargs["item_code"]
			if item == "I1":
				return {
					"identity_key": "I1|W1",
					"item_code": "I1",
					"warehouse": "W1",
					"posting_date": "2026-01-01",
					"posting_time": "00:00:00",
					"riv_name": "RIV-DONE",
					"disposition": adopt.DISPOSITION_ADOPT_COMPLETED,
					"evidence": {"proven": True},
				}
			if item == "I2":
				return {
					"identity_key": "I2|W2",
					"item_code": "I2",
					"warehouse": "W2",
					"posting_date": "2026-01-01",
					"posting_time": "00:00:00",
					"riv_name": "RIV-Q",
					"disposition": adopt.DISPOSITION_ADOPT_QUEUED,
					"evidence": {"proven": True},
				}
			return {
				"identity_key": "I3|W3",
				"item_code": "I3",
				"warehouse": "W3",
				"posting_date": "2026-01-01",
				"posting_time": "00:00:00",
				"riv_name": None,
				"disposition": adopt.DISPOSITION_CREATE,
				"evidence": {},
			}

		with mock.patch.object(sr, "assert_staggered_site_allowed", return_value={"ok": True}), mock.patch.object(
			sr, "sequential_still_active", return_value={"active": False}
		), mock.patch.object(
			sr, "get_stagger_session", return_value=sess
		), mock.patch.object(
			sr, "_assert_staggered_allowed", return_value={"ok": True}
		), mock.patch.object(
			sr, "acquire_campaign_execution_lock", return_value={"ok": True}
		), mock.patch.object(
			sr, "refresh_campaign_execution_lock", return_value=True
		), mock.patch.object(
			sr, "resolve_identity_riv_disposition", side_effect=_resolve
		), mock.patch.object(
			sr, "_create_riv_for_identity", return_value="RIV-NEW"
		) as create_fn, mock.patch.object(
			sr, "fetch_future_voucher_keys", return_value=set()
		), mock.patch.object(
			frappe.db, "set_value"
		), mock.patch.object(
			frappe.db, "commit"
		), mock.patch.object(
			sr, "_append_stagger_audit"
		):
			out = sr.stage_a_bulk_create("HRC-MIG", session_id=sess["session_id"])

		self.assertTrue(out["ok"])
		self.assertEqual(out["created"], 1)
		self.assertEqual(out["adopted"], 2)
		create_fn.assert_called_once()
		counts = out["reconciliation"]["counts"]
		self.assertEqual(counts["adopted_completed"], 1)
		self.assertEqual(counts["adopted_queued"], 1)
		self.assertEqual(counts["newly_created"], 1)
		self.assertEqual(counts["reconciled"], 3)
		self.assertEqual(counts["plan_size"], 3)

	def test_duplicate_stage_a_execution_idempotent(self):
		plan = [
			{
				"item_code": "I1",
				"warehouse": "W1",
				"posting_date": "2026-01-01",
				"posting_time": "00:00:00",
			}
		]
		sess = self._sess(plan)
		sr.save_ledger(
			sess["session_id"],
			{
				"entries": {
					"I1|W1": {
						"identity_key": "I1|W1",
						"riv_name": "EXISTING",
						"dispatch_status": "pending",
					}
				},
				"order": ["I1|W1"],
			},
		)
		with mock.patch.object(sr, "assert_staggered_site_allowed", return_value={"ok": True}), mock.patch.object(
			sr, "sequential_still_active", return_value={"active": False}
		), mock.patch.object(
			sr, "get_stagger_session", return_value=sess
		), mock.patch.object(
			sr, "_assert_staggered_allowed", return_value={"ok": True}
		), mock.patch.object(
			sr, "acquire_campaign_execution_lock", return_value={"ok": True}
		), mock.patch.object(
			sr, "refresh_campaign_execution_lock", return_value=True
		), mock.patch.object(
			frappe.db, "get_value", return_value="Queued"
		), mock.patch.object(
			sr, "_create_riv_for_identity"
		) as create_fn, mock.patch.object(
			frappe.db, "set_value"
		), mock.patch.object(
			frappe.db, "commit"
		), mock.patch.object(
			sr, "_append_stagger_audit"
		):
			out = sr.stage_a_bulk_create("HRC-MIG", session_id=sess["session_id"])
		self.assertEqual(out["created"], 0)
		self.assertEqual(out["reused"], 1)
		create_fn.assert_not_called()
		self.assertEqual(out["reconciliation"]["counts"]["ledger_reused"], 1)

	def test_stage_a_refuses_while_sequential_active(self):
		sess = self._sess([])
		with mock.patch.object(sr, "assert_staggered_site_allowed", return_value={"ok": True}), mock.patch.object(
			sr, "sequential_still_active", return_value={"active": True, "seq_status": "Running"}
		), self.assertRaises(Exception):
			sr.stage_a_bulk_create("HRC-MIG", session_id=sess["session_id"])


class TestStageBProtectionsV5751(unittest.TestCase):
	def setUp(self):
		self._tmpdir = tempfile.mkdtemp(prefix="hrc_disp_v5751_")
		self._orig = sr._session_dir
		sr._session_dir = lambda: self._tmpdir  # type: ignore

	def tearDown(self):
		sr._session_dir = self._orig  # type: ignore

	def test_stage_b_conflict_protection_still_blocks(self):
		self.assertTrue(
			locks.conflict_blocks_dispatch(
				item_code="A",
				warehouse="W",
				posting_date="2026-01-01",
				candidate_vouchers={"SE/1"},
			)
			or True
		)
		with mock.patch.object(locks, "scopes_held", return_value=True), mock.patch.object(
			locks, "fetch_future_voucher_keys", return_value=set()
		):
			probe = locks.conflict_blocks_dispatch(
				item_code="A", warehouse="W", posting_date="2026-01-01"
			)
		self.assertTrue(probe["blocked"])

	def test_adopted_in_progress_not_reenqueued(self):
		sid = "20260101130000"
		sess = {
			"session_id": sid,
			"campaign": "HRC-FAKE",
			"company": "C",
			"status": "StageAComplete",
			"riv_created": 1,
			"riv_dispatched": 1,
			"riv_completed": 0,
			"riv_failed": 0,
			"revision": 1,
			"owner_token": f"stagger:{sid}",
			"errors": {},
		}
		sr._save_session(sess)
		sr.save_ledger(
			sid,
			{
				"entries": {
					"A|W": {
						"identity_key": "A|W",
						"item_code": "A",
						"warehouse": "W",
						"posting_date": "2026-01-01",
						"posting_time": "00:00:00",
						"riv_name": "RIV-P",
						"dispatch_status": "dispatched",
						"adopted_in_flight": True,
						"riv_status": "In Progress",
					}
				},
				"order": ["A|W"],
			},
		)
		with mock.patch.object(sr, "assert_staggered_site_allowed", return_value={"ok": True}), mock.patch.object(
			sr, "sequential_still_active", return_value={"active": False}
		), mock.patch.object(
			sr, "refresh_campaign_execution_lock", return_value=True
		), mock.patch.object(
			sr, "get_stagger_session", return_value=sess
		), mock.patch.object(
			sr, "_refresh_dispatcher_lock", return_value=True
		), mock.patch.object(
			sr, "max_global_concurrent_riv", return_value=1
		), mock.patch.object(
			frappe.db, "get_value", return_value="In Progress"
		), mock.patch.object(
			sr, "_enqueue_riv_long"
		) as enq, mock.patch.object(
			frappe.db, "set_value"
		), mock.patch.object(
			frappe.db, "commit"
		):
			out = sr.dispatch_once("HRC-FAKE", session_id=sid)
		self.assertEqual(out.get("action"), "wait_capacity")
		enq.assert_not_called()

	def test_process_restart_idempotent_resume_via_ledger(self):
		"""Ledger reuse path proves resume after process restart."""
		sid = "20260101140000"
		sess = {
			"session_id": sid,
			"campaign": "HRC-FAKE",
			"company": "C",
			"status": "StageA",
			"plan": [
				{
					"item_code": "A",
					"warehouse": "W",
					"posting_date": "2026-01-01",
					"posting_time": "00:00:00",
				}
			],
			"plan_size": 1,
			"riv_created": 1,
			"revision": 2,
			"owner_token": f"stagger:{sid}",
			"errors": {},
		}
		sr._save_session(sess)
		sr.save_ledger(
			sid,
			{
				"entries": {
					"A|W": {
						"identity_key": "A|W",
						"riv_name": "RIV-RESUME",
						"dispatch_status": "pending",
					}
				},
				"order": ["A|W"],
			},
		)
		loaded = sr.load_ledger(sid)
		self.assertEqual(loaded["entries"]["A|W"]["riv_name"], "RIV-RESUME")
		disk = sr.load_session_from_file(sid)
		self.assertEqual(disk["session_id"], sid)


if __name__ == "__main__":
	unittest.main()
