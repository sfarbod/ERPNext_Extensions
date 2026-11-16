# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.50 — Staggered HRC RIV queue + conflict locks."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

import frappe

from erpnext_extensions.iran_accounting.historical_rebuild import staggered_riv as sr
from erpnext_extensions.iran_accounting.historical_rebuild import staggered_riv_locks as locks


class TestStaggeredLocksV5750(unittest.TestCase):
	def test_identity_and_scope_keys(self):
		self.assertEqual(locks.identity_key("A", "W"), "A|W")
		scopes = locks.scope_keys_for_identity("A", "W")
		self.assertTrue(any(s.endswith("iw:A|W") for s in scopes))
		self.assertTrue(any(s.endswith("item:A") for s in scopes))

	def test_voucher_overlap(self):
		self.assertTrue(locks.voucher_overlap({"SE/1", "SE/2"}, {"SE/2", "PR/9"}))
		self.assertFalse(locks.voucher_overlap({"SE/1"}, {"PR/9"}))
		self.assertFalse(locks.voucher_overlap(set(), {"SE/1"}))

	def test_acquire_and_release_scopes_atomic(self):
		scopes = [
			f"hrc_stagger:scope:test_iw:{frappe.generate_hash(length=8)}",
			f"hrc_stagger:scope:test_item:{frappe.generate_hash(length=8)}",
		]
		fake = {}

		class _FakeRedis:
			def eval(self, *_a, **_k):
				raise RuntimeError("force fallback")

			def set(self, key, value, nx=False, ex=None):
				if nx and key in fake:
					return False
				fake[key] = value
				return True

			def get(self, key):
				return fake.get(key)

			def delete(self, key):
				fake.pop(key, None)

		with mock.patch.object(locks, "_redis", return_value=_FakeRedis()):
			tok = locks.try_acquire_scopes(scopes, ttl_s=30)
			self.assertIsNotNone(tok)
			tok2 = locks.try_acquire_scopes(scopes, ttl_s=30)
			self.assertIsNone(tok2)
			n = locks.release_scopes(scopes, tok)
			self.assertGreaterEqual(n, 1)
			tok3 = locks.try_acquire_scopes(scopes, ttl_s=30)
			self.assertIsNotNone(tok3)
			locks.release_scopes(scopes, tok3)

	def test_conflict_blocks_when_scope_held(self):
		item = f"ITEM-{frappe.generate_hash(length=6)}"
		wh = f"WH-{frappe.generate_hash(length=6)}"
		with mock.patch.object(locks, "scopes_held", return_value=True), mock.patch.object(
			locks, "fetch_future_voucher_keys", return_value=set()
		):
			probe = locks.conflict_blocks_dispatch(
				item_code=item, warehouse=wh, posting_date="2026-01-01"
			)
		self.assertTrue(probe["blocked"])
		self.assertEqual(probe["reason"], "scope_held")

	def test_sle_voucher_overlap_blocks(self):
		item = f"ITEM-{frappe.generate_hash(length=6)}"
		wh = f"WH-{frappe.generate_hash(length=6)}"
		cand = {"Stock Entry/SE-1", "Purchase Receipt/PR-1"}
		with mock.patch.object(locks, "scopes_held", return_value=False), mock.patch.object(
			locks, "active_riv_voucher_union", return_value={"Stock Entry/SE-1"}
		):
			probe = locks.conflict_blocks_dispatch(
				item_code=item,
				warehouse=wh,
				posting_date="2026-01-01",
				candidate_vouchers=cand,
			)
		self.assertTrue(probe["blocked"])
		self.assertEqual(probe["reason"], "sle_voucher_overlap")


class TestStaggeredSessionLedgerV5750(unittest.TestCase):
	def setUp(self):
		self._tmpdir = tempfile.mkdtemp(prefix="hrc_stagger_test_")
		self._orig_session_dir = sr._session_dir

		def _fake_dir():
			os.makedirs(self._tmpdir, exist_ok=True)
			return self._tmpdir

		sr._session_dir = _fake_dir  # type: ignore

	def tearDown(self):
		sr._session_dir = self._orig_session_dir  # type: ignore

	def test_ledger_idempotent_roundtrip(self):
		sid = "20260101000000"
		ledger = {
			"entries": {
				"A|W": {
					"identity_key": "A|W",
					"riv_name": "riv1",
					"dispatch_status": "pending",
				}
			},
			"order": ["A|W"],
		}
		sr.save_ledger(sid, ledger)
		loaded = sr.load_ledger(sid)
		self.assertEqual(loaded["order"], ["A|W"])
		self.assertEqual(loaded["entries"]["A|W"]["riv_name"], "riv1")

	def test_session_revision_disk_wins(self):
		payload = {
			"session_id": "20260101000001",
			"campaign": "HRC-TEST",
			"status": "Starting",
			"revision": 0,
			"plan": [],
			"riv_created": 0,
		}
		sr._save_session(payload)
		disk1 = sr.load_session_from_file("20260101000001")
		disk1["status"] = "StageA"
		sr._save_session(disk1)
		disk = sr.load_session_from_file("20260101000001")
		self.assertEqual(disk["status"], "StageA")
		self.assertGreaterEqual(int(disk["revision"]), 2)

	def test_stage_a_skips_existing_ledger_entries(self):
		"""Second Stage A pass must not recreate ledgered RIVs."""
		campaign = "HRC-FAKE"
		sess = {
			"session_id": "20260101000002",
			"campaign": campaign,
			"company": "C",
			"status": "Starting",
			"plan": [
				{
					"item_code": "I1",
					"warehouse": "W1",
					"posting_date": "2026-01-01",
					"posting_time": "00:00:00",
				}
			],
			"plan_size": 1,
			"riv_created": 1,
			"revision": 1,
		}
		sr._save_session(sess)
		sr.save_ledger(
			sess["session_id"],
			{
				"entries": {
					"I1|W1": {
						"identity_key": "I1|W1",
						"item_code": "I1",
						"warehouse": "W1",
						"posting_date": "2026-01-01",
						"posting_time": "00:00:00",
						"riv_name": "EXISTING-RIV",
						"dispatch_status": "pending",
						"status": "created",
					}
				},
				"order": ["I1|W1"],
			},
		)

		with mock.patch.object(sr, "get_stagger_session", return_value=sess), mock.patch.object(
			sr, "assert_staggered_site_allowed", return_value={"ok": True}
		), mock.patch.object(
			sr, "sequential_still_active", return_value={"active": False}
		), mock.patch.object(
			sr, "acquire_campaign_execution_lock", return_value={"ok": True}
		), mock.patch.object(
			sr, "refresh_campaign_execution_lock", return_value=True
		), mock.patch.object(
			sr, "_assert_staggered_allowed", return_value={"ok": True}
		), mock.patch.object(
			frappe.db, "get_value", return_value="Queued"
		), mock.patch.object(
			sr, "_create_riv_for_identity"
		) as create_fn, mock.patch.object(
			frappe.db, "set_value"
		), mock.patch.object(
			frappe.db, "commit"
		):
			out = sr.stage_a_bulk_create(campaign, session_id=sess["session_id"])
		self.assertTrue(out["ok"])
		self.assertEqual(out["created"], 0)
		self.assertEqual(out["reused"], 1)
		create_fn.assert_not_called()


class TestStaggeredDispatchV5750(unittest.TestCase):
	def setUp(self):
		self._tmpdir = tempfile.mkdtemp(prefix="hrc_stagger_disp_")
		self._orig = sr._session_dir
		sr._session_dir = lambda: self._tmpdir  # type: ignore

	def tearDown(self):
		sr._session_dir = self._orig  # type: ignore

	def test_dispatch_respects_max_global_concurrent(self):
		sid = "20260101000003"
		sess = {
			"session_id": sid,
			"campaign": "HRC-FAKE",
			"company": "C",
			"status": "StageAComplete",
			"riv_created": 2,
			"riv_dispatched": 1,
			"riv_completed": 0,
			"riv_failed": 0,
			"revision": 1,
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
						"riv_name": "RIV-A",
						"dispatch_status": "dispatched",
						"riv_status": "In Progress",
					},
					"B|W": {
						"identity_key": "B|W",
						"item_code": "B",
						"warehouse": "W",
						"posting_date": "2026-01-01",
						"riv_name": "RIV-B",
						"dispatch_status": "pending",
					},
				},
				"order": ["A|W", "B|W"],
			},
		)

		def _db_get_value(doctype, name, field=None, **kwargs):
			if doctype == "Repost Item Valuation":
				status = "In Progress" if name == "RIV-A" else "Queued"
				if isinstance(field, (list, tuple)) or kwargs.get("as_dict"):
					return frappe._dict(status=status, error_log=None)
				return status
			return None

		with mock.patch.object(
			sr, "assert_staggered_site_allowed", return_value={"ok": True}
		), mock.patch.object(
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
			frappe.db, "get_value", side_effect=_db_get_value
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

	def test_dispatch_skips_conflict_and_picks_independent(self):
		sid = "20260101000004"
		sess = {
			"session_id": sid,
			"campaign": "HRC-FAKE",
			"company": "C",
			"status": "StageAComplete",
			"riv_created": 2,
			"riv_dispatched": 0,
			"riv_completed": 0,
			"riv_failed": 0,
			"revision": 1,
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
						"riv_name": "RIV-A",
						"dispatch_status": "pending",
					},
					"B|X": {
						"identity_key": "B|X",
						"item_code": "B",
						"warehouse": "X",
						"posting_date": "2026-01-01",
						"posting_time": "00:00:00",
						"riv_name": "RIV-B",
						"dispatch_status": "pending",
					},
				},
				"order": ["A|W", "B|X"],
			},
		)

		def _probe(**kwargs):
			if kwargs["item_code"] == "A":
				return {
					"blocked": True,
					"reason": "scope_held",
					"scopes": ["s1"],
					"vouchers": set(),
				}
			return {
				"blocked": False,
				"reason": None,
				"scopes": ["s2"],
				"vouchers": set(),
			}

		def _db_get_value(doctype, name, field=None, **kwargs):
			if doctype == "Repost Item Valuation":
				if isinstance(field, (list, tuple)) or kwargs.get("as_dict"):
					return frappe._dict(status="Queued", error_log=None)
				return "Queued"
			return None

		with mock.patch.object(
			sr, "assert_staggered_site_allowed", return_value={"ok": True}
		), mock.patch.object(
			sr, "sequential_still_active", return_value={"active": False}
		), mock.patch.object(
			sr, "refresh_campaign_execution_lock", return_value=True
		), mock.patch.object(
			sr, "get_stagger_session", return_value=sess
		), mock.patch.object(
			sr, "_refresh_dispatcher_lock", return_value=True
		), mock.patch.object(
			sr, "max_global_concurrent_riv", return_value=2
		), mock.patch.object(
			sr, "conflict_blocks_dispatch", side_effect=_probe
		), mock.patch.object(
			sr, "try_acquire_scopes", return_value="tok"
		), mock.patch.object(
			sr, "remember_riv_scopes"
		), mock.patch.object(
			frappe.db, "get_value", side_effect=_db_get_value
		), mock.patch.object(
			sr, "_enqueue_riv_long", return_value="job-B"
		) as enq, mock.patch.object(
			frappe.db, "set_value"
		), mock.patch.object(
			frappe.db, "commit"
		):
			out = sr.dispatch_once("HRC-FAKE", session_id=sid)
		self.assertEqual(out.get("action"), "dispatched")
		self.assertEqual(out.get("riv"), "RIV-B")
		enq.assert_called_once_with("RIV-B")


class TestCloneConfirmGateV5750(unittest.TestCase):
	def test_clone_requires_confirm(self):
		with mock.patch.object(sr, "assert_dev_only_clone"), self.assertRaises(Exception):
			sr.clone_campaign_manifest(source="HRC-00016", target="HRC-00017", confirm=None)


if __name__ == "__main__":
	unittest.main()
