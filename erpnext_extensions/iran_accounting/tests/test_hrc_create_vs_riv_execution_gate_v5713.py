# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.13 — Campaign creation must ignore active RIVs; execution gates on identity overlap only."""

from __future__ import annotations

import unittest
from unittest import mock

import frappe

from erpnext_extensions.iran_accounting.historical_rebuild import api_service as svc
from erpnext_extensions.iran_accounting.historical_rebuild.discovery import manifest_checksum


def _seed_meta():
	return frappe._dict(
		company="C",
		posting_date="2026-01-01",
		posting_time="00:00:00",
		creation="2026-01-01 00:00:00",
	)


def _checksum_for_single_se(name: str = "SE1") -> str:
	return manifest_checksum(
		[
			{
				"doctype": "Stock Entry",
				"name": name,
				"posting_date": "2026-01-01",
				"posting_time": "00:00:00",
				"creation": "2026-01-01 00:00:00",
				"company": "C",
			}
		]
	)


class TestCollectIdentityRivOverlapsV5713(unittest.TestCase):
	def test_collects_queued_and_in_progress_with_details(self):
		rows = {
			("ITEM-A", "WH-A"): [
				frappe._dict(
					name="RIV-Q1",
					status="Queued",
					item_code="ITEM-A",
					warehouse="WH-A",
					posting_date="2026-01-01",
					modified="2026-01-02",
				)
			],
			("ITEM-B", "WH-B"): [
				frappe._dict(
					name="RIV-IP1",
					status="In Progress",
					item_code="ITEM-B",
					warehouse="WH-B",
					posting_date="2026-01-01",
					modified="2026-01-03",
				)
			],
		}

		def _get_all(doctype, filters=None, **kwargs):
			self.assertEqual(doctype, "Repost Item Valuation")
			key = (filters["item_code"], filters["warehouse"])
			return list(rows.get(key, []))

		with mock.patch("frappe.get_all", side_effect=_get_all):
			out = svc.collect_identity_riv_overlaps(
				[("ITEM-A", "WH-A"), ("ITEM-B", "WH-B"), ("ITEM-C", "WH-C")]
			)
		self.assertEqual(len(out), 2)
		by_riv = {r["riv"]: r for r in out}
		self.assertEqual(by_riv["RIV-Q1"]["status"], "Queued")
		self.assertEqual(by_riv["RIV-Q1"]["item_code"], "ITEM-A")
		self.assertEqual(by_riv["RIV-Q1"]["warehouse"], "WH-A")
		self.assertEqual(by_riv["RIV-Q1"]["identity"], {"item_code": "ITEM-A", "warehouse": "WH-A"})
		self.assertEqual(by_riv["RIV-IP1"]["status"], "In Progress")

	def test_unrelated_identities_ignored(self):
		with mock.patch("frappe.get_all", return_value=[]):
			out = svc.collect_identity_riv_overlaps([("ITEM-A", "WH-A")])
		self.assertEqual(out, [])


class TestCreateIgnoresActiveRivV5713(unittest.TestCase):
	"""Creation must succeed with Queued/In Progress RIVs and must not start work."""

	def _create_with_active_riv(self, riv_status: str):
		checksum = _checksum_for_single_se()
		created = {
			"campaign": "HRC-TEST-CREATE",
			"status": "Ready",
			"checksum": checksum,
			"total": 1,
			"identity_count": 1,
		}
		riv_calls = {"get_all": 0, "count": 0}

		def _guard_get_all(doctype, *a, **k):
			if doctype == "Repost Item Valuation":
				riv_calls["get_all"] += 1
				return [
					frappe._dict(
						name="RIV-ACTIVE",
						status=riv_status,
						item_code="ITEM1",
						warehouse="WH1",
					)
				]
			return []

		def _guard_count(*a, **k):
			riv_calls["count"] += 1
			return 99

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign.expand_identities_from_vouchers",
			return_value=[("ITEM1", "WH1")],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign.create_exact_voucher_campaign",
			return_value=created,
		) as create_m, mock.patch.object(
			svc, "require_repair", return_value=1
		), mock.patch.object(
			svc, "require_company", side_effect=lambda c: c
		), mock.patch.object(
			svc, "append_api_audit", return_value=None
		), mock.patch(
			"frappe.get_all", side_effect=_guard_get_all
		), mock.patch(
			"frappe.db.count", side_effect=_guard_count
		), mock.patch(
			"frappe.db.exists", return_value=False
		):
			out = svc.create_exact_campaign(
				company="C",
				vouchers=[{"voucher_type": "Stock Entry", "voucher_no": "SE1"}],
				identities_allowlist=[{"item_code": "ITEM1", "warehouse": "WH1"}],
				expected_checksum=checksum,
				confirm="CREATE_EXACT_CAMPAIGN",
			)
		create_m.assert_called_once()
		# Creation path must not consult RIV tables as a gate.
		self.assertEqual(riv_calls["get_all"], 0)
		self.assertEqual(riv_calls["count"], 0)
		self.assertTrue(out.get("ok"))
		self.assertEqual(out.get("campaign"), "HRC-TEST-CREATE")
		self.assertEqual(out.get("status"), "Ready")
		return out

	def test_create_succeeds_with_queued_target_riv(self):
		self._create_with_active_riv("Queued")

	def test_create_succeeds_with_in_progress_target_riv(self):
		self._create_with_active_riv("In Progress")

	def test_create_does_not_start_riv_or_rebuild(self):
		checksum = _checksum_for_single_se()
		created = {
			"campaign": "HRC-TEST-CREATE-2",
			"status": "Ready",
			"checksum": checksum,
			"total": 1,
			"identity_count": 1,
		}
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign.create_exact_voucher_campaign",
			return_value=created,
		), mock.patch.object(svc, "require_repair", return_value=1), mock.patch.object(
			svc, "require_company", side_effect=lambda c: c
		), mock.patch.object(svc, "append_api_audit", return_value=None), mock.patch(
			"frappe.enqueue"
		) as enq, mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.start_sequential_session"
		) as start_sess, mock.patch(
			"erpnext.controllers.stock_controller.create_repost_item_valuation_entry"
		) as create_riv:
			out = svc.create_exact_campaign(
				company="C",
				vouchers=[{"voucher_type": "Stock Entry", "voucher_no": "SE1"}],
				identities_allowlist=[{"item_code": "ITEM1", "warehouse": "WH1"}],
				expected_checksum=checksum,
				confirm="CREATE_EXACT_CAMPAIGN",
			)
		self.assertTrue(out.get("ok"))
		enq.assert_not_called()
		start_sess.assert_not_called()
		create_riv.assert_not_called()


class TestCreateStructuralGuardsV5713(unittest.TestCase):
	def test_checksum_mismatch_blocks_creation(self):
		from erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign import (
			create_exact_voucher_campaign,
		)

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign.expand_identities_from_vouchers",
			return_value=[("ITEM1", "WH1")],
		), mock.patch(
			"frappe.db.get_value",
			return_value=_seed_meta(),
		):
			with self.assertRaises(Exception) as ctx:
				create_exact_voucher_campaign(
					company="C",
					vouchers=[{"voucher_type": "Stock Entry", "voucher_no": "SE1"}],
					identities_allowlist=[{"item_code": "ITEM1", "warehouse": "WH1"}],
					expected_checksum="0" * 64,
				)
			self.assertIn("checksum", str(ctx.exception).lower())

	def test_duplicate_identities_in_allowlist_blocked(self):
		from erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign import (
			create_exact_voucher_campaign,
		)

		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.scoped_campaign.expand_identities_from_vouchers",
			return_value=[("ITEM1", "WH1")],
		), mock.patch(
			"frappe.db.get_value",
			return_value=_seed_meta(),
		):
			with self.assertRaises(Exception) as ctx:
				create_exact_voucher_campaign(
					company="C",
					vouchers=[{"voucher_type": "Stock Entry", "voucher_no": "SE1"}],
					identities_allowlist=[
						{"item_code": "ITEM1", "warehouse": "WH1"},
						{"item_code": "ITEM1", "warehouse": "WH1"},
					],
					expected_checksum=None,
				)
			self.assertIn("duplicate", str(ctx.exception).lower())


class TestExecutionIdentityRivGateV5713(unittest.TestCase):
	def _ready_doc(self, checksum: str):
		return frappe._dict(status="Ready", company="C", manifest_checksum=checksum)

	def test_preflight_blocks_only_identity_overlap_not_sitewide_count(self):
		checksum = _checksum_for_single_se()
		overlap = [
			{
				"riv": "RIV-Q1",
				"name": "RIV-Q1",
				"status": "Queued",
				"item_code": "ITEM1",
				"warehouse": "WH1",
				"identity": {"item_code": "ITEM1", "warehouse": "WH1"},
			}
		]
		with mock.patch.object(svc, "require_read", return_value=1), mock.patch(
			"frappe.db.exists", return_value=True
		), mock.patch(
			"frappe.get_doc", return_value=self._ready_doc(checksum)
		), mock.patch.object(
			svc, "is_scoped_campaign", return_value=True
		), mock.patch.object(
			svc, "load_scoped_allowlist", return_value=[("ITEM1", "WH1")]
		), mock.patch(
			"frappe.get_all",
			side_effect=[
				[
					frappe._dict(
						voucher_type="Stock Entry",
						voucher_no="SE1",
						posting_date="2026-01-01",
						posting_time="00:00:00",
						creation_ts="2026-01-01 00:00:00",
					)
				],
				[],  # other_running campaigns
			],
		), mock.patch(
			"frappe.db.get_value",
			side_effect=[
				_seed_meta(),  # live voucher meta for checksum
				1,  # docstatus
			],
		), mock.patch(
			"frappe.db.count", return_value=42  # many unrelated site-wide RIVs
		), mock.patch(
			"frappe.cache",
			return_value=mock.Mock(get_value=mock.Mock(return_value=None)),
		), mock.patch.object(
			svc, "collect_identity_riv_overlaps", return_value=overlap
		):
			pre = svc.preflight_campaign(campaign="HRC-GATE")
		self.assertEqual(pre["verdict"], "BLOCKED")
		self.assertIn("identity_riv_overlap", pre["reasons"])
		self.assertNotIn(
			True,
			[r.startswith("active_queued_riv_count:") for r in pre["reasons"]],
		)
		self.assertEqual(pre["active_queued_riv_count"], 42)
		self.assertEqual(pre["identity_riv_overlap"][0]["riv"], "RIV-Q1")

	def test_preflight_passes_with_unrelated_sitewide_rivs(self):
		checksum = _checksum_for_single_se()
		with mock.patch.object(svc, "require_read", return_value=1), mock.patch(
			"frappe.db.exists", return_value=True
		), mock.patch(
			"frappe.get_doc", return_value=self._ready_doc(checksum)
		), mock.patch.object(
			svc, "is_scoped_campaign", return_value=True
		), mock.patch.object(
			svc, "load_scoped_allowlist", return_value=[("ITEM1", "WH1")]
		), mock.patch(
			"frappe.get_all",
			side_effect=[
				[
					frappe._dict(
						voucher_type="Stock Entry",
						voucher_no="SE1",
						posting_date="2026-01-01",
						posting_time="00:00:00",
						creation_ts="2026-01-01 00:00:00",
					)
				],
				[],
			],
		), mock.patch(
			"frappe.db.get_value",
			side_effect=[_seed_meta(), 1],
		), mock.patch(
			"frappe.db.count", return_value=17
		), mock.patch(
			"frappe.cache",
			return_value=mock.Mock(get_value=mock.Mock(return_value=None)),
		), mock.patch.object(
			svc, "collect_identity_riv_overlaps", return_value=[]
		):
			pre = svc.preflight_campaign(campaign="HRC-GATE")
		self.assertEqual(pre["verdict"], "PASS")
		self.assertEqual(pre["active_queued_riv_count"], 17)
		self.assertEqual(pre["identity_riv_overlap"], [])

	def _start_blocked(self, riv_status: str):
		checksum = _checksum_for_single_se()
		overlap = [
			{
				"riv": f"RIV-{riv_status}",
				"name": f"RIV-{riv_status}",
				"status": riv_status,
				"item_code": "ITEM1",
				"warehouse": "WH1",
				"identity": {"item_code": "ITEM1", "warehouse": "WH1"},
			}
		]
		pre = {
			"ok": False,
			"verdict": "BLOCKED",
			"reasons": ["identity_riv_overlap"],
			"identity_riv_overlap": overlap,
			"manifest_checksum": checksum,
		}
		with mock.patch.object(svc, "require_repair", return_value=1), mock.patch.object(
			svc, "preflight_campaign", return_value=pre
		), mock.patch(
			"frappe.db.get_value", return_value="Ready"
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.start_sequential_session"
		) as start_sess:
			out = svc.start_sequential_campaign(
				campaign="HRC-GATE",
				expected_checksum=checksum,
				confirm="START_SEQUENTIAL",
			)
		start_sess.assert_not_called()
		self.assertFalse(out.get("ok"))
		self.assertTrue(out.get("blocked"))
		self.assertEqual(out.get("campaign_status"), "Ready")
		self.assertEqual(out["identity_riv_overlap"][0]["status"], riv_status)
		return out

	def test_start_blocked_when_overlapping_riv_queued(self):
		self._start_blocked("Queued")

	def test_start_blocked_when_overlapping_riv_in_progress(self):
		self._start_blocked("In Progress")

	def test_start_not_blocked_by_unrelated_riv(self):
		checksum = _checksum_for_single_se()
		pre = {
			"ok": True,
			"verdict": "PASS",
			"reasons": [],
			"identity_riv_overlap": [],
			"manifest_checksum": checksum,
		}
		sess = {"session_id": "SESS-1"}
		with mock.patch.object(svc, "require_repair", return_value=1), mock.patch.object(
			svc, "preflight_campaign", return_value=pre
		), mock.patch(
			"frappe.db.get_value", return_value=checksum
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.get_seq_session",
			return_value=None,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild._executor_lock_key",
			return_value="lock-key",
		), mock.patch(
			"frappe.cache",
			return_value=mock.Mock(get_value=mock.Mock(return_value=None)),
		), mock.patch.object(
			svc, "assert_execution_riv_ready", return_value={"ok": True, "blocked": False, "identity_riv_overlap": []}
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.start_sequential_session",
			return_value=sess,
		) as start_sess, mock.patch.object(
			svc, "append_api_audit", return_value=None
		), mock.patch(
			"frappe.enqueue"
		), mock.patch(
			"frappe.db.set_value"
		):
			out = svc.start_sequential_campaign(
				campaign="HRC-GATE",
				expected_checksum=checksum,
				confirm="START_SEQUENTIAL",
				async_run=1,
			)
		start_sess.assert_called_once_with("HRC-GATE")
		self.assertTrue(out.get("ok"))

	def test_start_succeeds_after_conflicts_clear(self):
		checksum = _checksum_for_single_se()
		pre = {
			"ok": True,
			"verdict": "PASS",
			"reasons": [],
			"identity_riv_overlap": [],
		}
		with mock.patch.object(svc, "require_repair", return_value=1), mock.patch.object(
			svc, "preflight_campaign", return_value=pre
		), mock.patch(
			"frappe.db.get_value", return_value=checksum
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.get_seq_session",
			return_value=None,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild._executor_lock_key",
			return_value="lock-key",
		), mock.patch(
			"frappe.cache",
			return_value=mock.Mock(get_value=mock.Mock(return_value=None)),
		), mock.patch.object(
			svc,
			"assert_execution_riv_ready",
			return_value={"ok": True, "blocked": False, "identity_riv_overlap": []},
		) as gate, mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.start_sequential_session",
			return_value={"session_id": "SESS-CLEAR"},
		), mock.patch.object(
			svc, "append_api_audit", return_value=None
		), mock.patch(
			"frappe.enqueue"
		), mock.patch(
			"frappe.db.set_value"
		):
			out = svc.start_sequential_campaign(
				campaign="HRC-GATE",
				expected_checksum=checksum,
				confirm="START_SEQUENTIAL",
			)
		gate.assert_called_once_with("HRC-GATE")
		self.assertTrue(out.get("ok"))

	def test_start_recheck_blocks_race_after_preflight_pass(self):
		checksum = _checksum_for_single_se()
		pre = {
			"ok": True,
			"verdict": "PASS",
			"reasons": [],
			"identity_riv_overlap": [],
		}
		late_overlap = [
			{
				"riv": "RIV-RACE",
				"status": "Queued",
				"item_code": "ITEM1",
				"warehouse": "WH1",
				"identity": {"item_code": "ITEM1", "warehouse": "WH1"},
			}
		]
		with mock.patch.object(svc, "require_repair", return_value=1), mock.patch.object(
			svc, "preflight_campaign", return_value=pre
		), mock.patch(
			"frappe.db.get_value",
			side_effect=[checksum, "Ready"],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.get_seq_session",
			return_value=None,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild._executor_lock_key",
			return_value="lock-key",
		), mock.patch(
			"frappe.cache",
			return_value=mock.Mock(get_value=mock.Mock(return_value=None)),
		), mock.patch.object(
			svc,
			"assert_execution_riv_ready",
			return_value={
				"ok": False,
				"blocked": True,
				"identity_riv_overlap": late_overlap,
				"reason": "identity_riv_overlap",
			},
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.start_sequential_session"
		) as start_sess:
			out = svc.start_sequential_campaign(
				campaign="HRC-GATE",
				expected_checksum=checksum,
				confirm="START_SEQUENTIAL",
			)
		start_sess.assert_not_called()
		self.assertTrue(out.get("blocked"))
		self.assertEqual(out.get("reason"), "identity_riv_overlap")
		self.assertEqual(out.get("campaign_status"), "Ready")
		self.assertEqual(out["identity_riv_overlap"][0]["riv"], "RIV-RACE")

	def test_checksum_mismatch_blocks_execution(self):
		pre = {
			"ok": True,
			"verdict": "PASS",
			"reasons": [],
			"identity_riv_overlap": [],
		}
		with mock.patch.object(svc, "require_repair", return_value=1), mock.patch.object(
			svc, "preflight_campaign", return_value=pre
		), mock.patch(
			"frappe.db.get_value", return_value="abcd" * 16
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				svc.start_sequential_campaign(
					campaign="HRC-GATE",
					expected_checksum="0" * 64,
					confirm="START_SEQUENTIAL",
				)
			self.assertIn("checksum", str(ctx.exception).lower())


if __name__ == "__main__":
	unittest.main()
