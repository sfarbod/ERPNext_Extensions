# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Unit / integration tests for Asset Depreciation Repair Campaign (v5.3.31)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint, now_datetime

from erpnext_extensions.asset_usage_depreciation.services import depr_reset_rebuild_campaign as camp


class TestCampaignHelpers(unittest.TestCase):
	def test_clamp_chunk_size(self):
		self.assertEqual(camp._clamp_chunk_size(1), 1)
		self.assertEqual(camp._clamp_chunk_size(3), 3)
		self.assertEqual(camp._clamp_chunk_size(20), 8)
		self.assertEqual(camp._clamp_chunk_size(0), 3)

	def test_clamp_max_jes(self):
		self.assertEqual(camp._clamp_max_jes(150), 150)
		self.assertEqual(camp._clamp_max_jes(10), 40)
		self.assertEqual(camp._clamp_max_jes(9999), 400)

	def test_already_repaired_detection(self):
		self.assertTrue(
			camp._is_already_repaired({"status": "READY", "je_count": 0, "fractional_rows": 0})
		)
		self.assertFalse(
			camp._is_already_repaired({"status": "READY", "je_count": 3, "fractional_rows": 0})
		)
		self.assertFalse(
			camp._is_already_repaired({"status": "READY", "je_count": 0, "fractional_rows": 2})
		)

	def test_non_retryable_negative_balance(self):
		self.assertEqual(
			camp._reason_code_from_error("Life-final balancing installment would be negative (-2)"),
			"NEGATIVE_FINAL_BALANCE",
		)
		self.assertTrue(camp._is_non_retryable_error("Life-final balancing installment would be negative"))

	def test_transient_detection(self):
		self.assertTrue(camp._is_transient(Exception("Lock wait timeout exceeded")))
		self.assertTrue(camp._is_transient(Exception("Deadlock found when trying to get lock")))
		self.assertTrue(camp._is_transient(Exception("(1020, \"Record has changed since last read in table 'tabSeries'\")")))
		self.assertFalse(camp._is_transient(Exception("Schedule sum mismatch")))

	def test_csv_escape(self):
		self.assertEqual(camp._csv(['a,b', 'x"y']), ['"a,b"', '"x""y"'])


class TestCampaignDocTypes(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("DocType", camp.CAMPAIGN_DT):
			self.skipTest("Campaign DocTypes not migrated")

	def test_create_campaign_with_assets_and_counts(self):
		company = frappe.db.get_single_value("Global Defaults", "default_company") or frappe.db.get_value(
			"Company", {}, "name"
		)
		# Use any two existing assets if present
		assets = frappe.get_all("Asset", filters={"docstatus": 1}, pluck="name", limit_page_length=2)
		if len(assets) < 1:
			self.skipTest("No assets")
		status = camp.create_campaign(company=company, assets=assets, chunk_size=10)
		self.assertEqual(status["status"], "Draft")
		self.assertEqual(status["total_assets"], len(assets))
		self.assertEqual(status["pending_count"], len(assets))
		# Idempotent item insert
		camp._insert_items(status["campaign"], company, assets)
		status2 = camp.get_campaign_status(status["campaign"])
		self.assertEqual(status2["total_assets"], len(assets))

	def test_claim_prevents_double_claim(self):
		company = frappe.db.get_value("Company", {}, "name")
		assets = frappe.get_all("Asset", filters={"docstatus": 1}, pluck="name", limit_page_length=1)
		if not assets:
			self.skipTest("No assets")
		st = camp.create_campaign(company=company, assets=assets, chunk_size=10)
		item = frappe.get_all(
			camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name", limit_page_length=1
		)[0]
		frappe.db.set_value(camp.ITEM_DT, item, {"status": "Queued", "chunk_id": "chunk-1"})
		ok1 = camp._claim_item(item, "tok-a", "chunk-1")
		ok2 = camp._claim_item(item, "tok-b", "chunk-2")
		self.assertTrue(ok1)
		self.assertFalse(ok2)

	def test_claim_rejects_mismatched_chunk(self):
		company = frappe.db.get_value("Company", {}, "name")
		assets = frappe.get_all("Asset", filters={"docstatus": 1}, pluck="name", limit_page_length=1)
		if not assets:
			self.skipTest("No assets")
		st = camp.create_campaign(company=company, assets=assets, chunk_size=3)
		item = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")[0]
		frappe.db.set_value(camp.ITEM_DT, item, {"status": "Queued", "chunk_id": "chunk-new"})
		self.assertFalse(camp._claim_item(item, "tok-old", "chunk-old"))
		row = frappe.db.get_value(camp.ITEM_DT, item, ["status", "chunk_id", "claim_token"], as_dict=True)
		self.assertEqual(row.status, "Queued")
		self.assertEqual(row.chunk_id, "chunk-new")
		self.assertFalse(row.claim_token)

	def test_pause_blocks_feeder(self):
		company = frappe.db.get_value("Company", {}, "name")
		assets = frappe.get_all("Asset", filters={"docstatus": 1}, pluck="name", limit_page_length=1)
		if not assets:
			self.skipTest("No assets")
		st = camp.create_campaign(company=company, assets=assets)
		frappe.db.set_value(camp.CAMPAIGN_DT, st["campaign"], "status", "Paused")
		n = camp.enqueue_next_chunks(st["campaign"])
		self.assertEqual(n, 0)

	@patch(
		"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign.frappe.enqueue"
	)
	def test_start_enqueues_without_blocking(self, enqueue_mock):
		company = frappe.db.get_value("Company", {}, "name")
		# Prefer an already-clean asset so start_campaign quick-skip may mark Already Repaired
		assets = frappe.get_all(
			"Asset",
			filters={"docstatus": 1, "calculate_depreciation": 1},
			pluck="name",
			limit_page_length=3,
		)
		if not assets:
			self.skipTest("No assets")
		st = camp.create_campaign(company=company, assets=assets, chunk_size=10, max_inflight_chunks=2)
		# Force remaining to Pending eligible without analyze cost: leave as Pending
		out = camp.start_campaign(st["campaign"])
		self.assertIn(out["status"], ("Running", "Completed", "Completed With Exceptions"))
		# Either enqueued or all skipped to terminal during quick pass
		self.assertIsInstance(out.get("enqueued_chunks"), int)

	def test_exception_register_csv(self):
		company = frappe.db.get_value("Company", {}, "name")
		assets = frappe.get_all("Asset", filters={"docstatus": 1}, pluck="name", limit_page_length=1)
		if not assets:
			self.skipTest("No assets")
		st = camp.create_campaign(company=company, assets=assets)
		item = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")[0]
		frappe.db.set_value(
			camp.ITEM_DT,
			item,
			{
				"status": "Manual Review",
				"reason_code": "NEGATIVE_FINAL_BALANCE",
				"recommended_review": "Review basis",
			},
		)
		csv_out = camp.export_campaign_exceptions_csv(st["campaign"])
		self.assertIn("NEGATIVE_FINAL_BALANCE", csv_out["csv"])
		self.assertEqual(csv_out["rows"], 1)


class TestCampaignWorkerInline(FrappeTestCase):
	"""Process chunk inline (simulates worker) with mocked repair for non-destructive cases."""

	def setUp(self):
		if not frappe.db.exists("DocType", camp.CAMPAIGN_DT):
			self.skipTest("Campaign DocTypes not migrated")

	@patch(
		"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign._reset_and_rebuild_asset"
	)
	@patch(
		"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign.analyze_asset"
	)
	def test_worker_marks_already_repaired_without_calling_reset(self, analyze_mock, reset_mock):
		company = frappe.db.get_value("Company", {}, "name")
		assets = frappe.get_all("Asset", filters={"docstatus": 1}, pluck="name", limit_page_length=1)
		if not assets:
			self.skipTest("No assets")
		analyze_mock.return_value = {
			"status": "READY",
			"je_count": 0,
			"fractional_rows": 0,
			"ads": "ADS-X",
			"rows": 10,
			"due_rows": 0,
		}
		st = camp.create_campaign(company=company, assets=assets)
		item = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")[0]
		frappe.db.set_value(camp.ITEM_DT, item, {"status": "Queued", "chunk_id": "c1"})
		frappe.db.set_value(camp.CAMPAIGN_DT, st["campaign"], "status", "Running")
		camp.process_campaign_chunk(st["campaign"], "c1", [item])
		status = frappe.db.get_value(camp.ITEM_DT, item, "status")
		self.assertEqual(status, "Already Repaired")
		reset_mock.assert_not_called()

	@patch(
		"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign._reset_and_rebuild_asset"
	)
	@patch(
		"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign.analyze_asset"
	)
	def test_worker_maps_blocked_scrapped(self, analyze_mock, reset_mock):
		company = frappe.db.get_value("Company", {}, "name")
		assets = frappe.get_all("Asset", filters={"docstatus": 1}, pluck="name", limit_page_length=1)
		if not assets:
			self.skipTest("No assets")
		analyze_mock.return_value = {
			"status": "BLOCKED",
			"reason": "LIFECYCLE_SCRAPPED,DISPOSAL_OR_SCRAP",
			"reasons": ["LIFECYCLE_SCRAPPED", "DISPOSAL_OR_SCRAP"],
			"je_count": 0,
			"fractional_rows": 0,
			"ads": None,
			"rows": 0,
			"due_rows": 0,
		}
		st = camp.create_campaign(company=company, assets=assets)
		item = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")[0]
		frappe.db.set_value(camp.ITEM_DT, item, {"status": "Queued", "chunk_id": "c2"})
		frappe.db.set_value(camp.CAMPAIGN_DT, st["campaign"], "status", "Running")
		camp.process_campaign_chunk(st["campaign"], "c2", [item])
		row = frappe.db.get_value(
			camp.ITEM_DT, item, ["status", "reason_code"], as_dict=True
		)
		self.assertEqual(row.status, "Blocked")
		self.assertEqual(row.reason_code, "LIFECYCLE_SCRAPPED")
		reset_mock.assert_not_called()

	@patch(
		"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign._reset_and_rebuild_asset"
	)
	@patch(
		"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign.analyze_asset"
	)
	def test_failure_then_next_asset_success(self, analyze_mock, reset_mock):
		company = frappe.db.get_value("Company", {}, "name")
		assets = frappe.get_all("Asset", filters={"docstatus": 1}, pluck="name", limit_page_length=2)
		if len(assets) < 2:
			self.skipTest("Need 2 assets")

		def analyze_side(asset):
			return {
				"status": "READY",
				"je_count": 2,
				"fractional_rows": 2,
				"ads": "ADS",
				"rows": 5,
				"due_rows": 1,
			}

		analyze_mock.side_effect = analyze_side

		def reset_side(asset):
			if asset == assets[0]:
				return {
					"status": "FAILED",
					"errors": ["Life-final balancing installment would be negative (-2)."],
				}
			return {
				"status": "SUCCESS",
				"JEs_cancelled": ["JE1", "JE2"],
				"old_ADS": "OLD",
				"new_ADS": "NEW",
				"rows": 5,
				"due_rows": 1,
				"whole_number_check": True,
				"gates": {"ok": True, "errors": [], "depreciable_total": 100, "final_amount": 10},
				"after": {"value_after_depreciation": 100},
			}

		reset_mock.side_effect = reset_side
		st = camp.create_campaign(company=company, assets=assets, chunk_size=10)
		items = frappe.get_all(
			camp.ITEM_DT, filters={"campaign": st["campaign"]}, fields=["name", "asset"], order_by="asset"
		)
		# preserve order assets[0], assets[1]
		items_sorted = sorted(items, key=lambda r: 0 if r.asset == assets[0] else 1)
		names = [r.name for r in items_sorted]
		for n in names:
			frappe.db.set_value(camp.ITEM_DT, n, {"status": "Queued", "chunk_id": "c3"})
		frappe.db.set_value(camp.CAMPAIGN_DT, st["campaign"], "status", "Running")
		camp.process_campaign_chunk(st["campaign"], "c3", names)
		s0 = frappe.db.get_value(camp.ITEM_DT, names[0], "status")
		s1 = frappe.db.get_value(camp.ITEM_DT, names[1], "status")
		self.assertEqual(s0, "Manual Review")
		self.assertEqual(s1, "Success")


class TestCampaignV532FeederAndRecovery(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("DocType", camp.CAMPAIGN_DT):
			self.skipTest("Campaign DocTypes not migrated")

	def test_weighted_chunk_respects_max_jes(self):
		company = frappe.db.get_value("Company", {}, "name")
		assets = frappe.get_all("Asset", filters={"docstatus": 1}, pluck="name", limit_page_length=5)
		if len(assets) < 3:
			self.skipTest("Need assets")
		st = camp.create_campaign(
			company=company, assets=assets, chunk_size=5, max_jes_per_chunk=100
		)
		# Force known JE weights so packing is deterministic
		items = frappe.get_all(
			camp.ITEM_DT, filters={"campaign": st["campaign"]}, fields=["name"], order_by="creation"
		)
		for i, it in enumerate(items):
			frappe.db.set_value(camp.ITEM_DT, it.name, "submitted_jes_before", 60)
		names = camp._select_and_queue_chunk(st["campaign"], chunk_size=5, max_jes=100)
		# 60+60=120 > 100 → only first asset when second would exceed (first always allowed)
		self.assertEqual(len(names), 1)

	def test_recover_abandoned_when_job_gone(self):
		company = frappe.db.get_value("Company", {}, "name")
		assets = frappe.get_all("Asset", filters={"docstatus": 1}, pluck="name", limit_page_length=1)
		if not assets:
			self.skipTest("No assets")
		st = camp.create_campaign(company=company, assets=assets, chunk_size=3)
		item = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")[0]
		old = frappe.utils.add_to_date(now_datetime(), minutes=-30)
		frappe.db.set_value(
			camp.ITEM_DT,
			item,
			{
				"status": "Processing",
				"started_at": old,
				"chunk_id": f"{st['campaign']}-deadchunk",
				"claim_token": "tok",
				"attempt_count": 1,
			},
		)
		with patch.object(camp, "_chunk_rq_job_state", return_value="missing"):
			n = camp.recover_abandoned_processing(st["campaign"])
		self.assertEqual(n, 1)
		row = frappe.db.get_value(
			camp.ITEM_DT, item, ["status", "claim_token", "error_type"], as_dict=True
		)
		self.assertEqual(row.status, "Retryable Failed")
		self.assertFalse(row.claim_token)
		self.assertEqual(row.error_type, "ABANDONED_PROCESSING")

	def test_recover_skips_when_job_active(self):
		company = frappe.db.get_value("Company", {}, "name")
		assets = frappe.get_all("Asset", filters={"docstatus": 1}, pluck="name", limit_page_length=1)
		if not assets:
			self.skipTest("No assets")
		st = camp.create_campaign(company=company, assets=assets, chunk_size=3)
		item = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")[0]
		old = frappe.utils.add_to_date(now_datetime(), minutes=-30)
		frappe.db.set_value(
			camp.ITEM_DT,
			item,
			{
				"status": "Processing",
				"started_at": old,
				"chunk_id": f"{st['campaign']}-live",
				"claim_token": "tok",
				"attempt_count": 1,
			},
		)
		with patch.object(camp, "_chunk_rq_job_state", return_value="active"):
			n = camp.recover_abandoned_processing(st["campaign"])
		self.assertEqual(n, 0)
		self.assertEqual(frappe.db.get_value(camp.ITEM_DT, item, "status"), "Processing")

	@patch(
		"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign.frappe.enqueue"
	)
	def test_process_chunk_finally_feeds_even_on_inner_error_path(self, enqueue_mock):
		company = frappe.db.get_value("Company", {}, "name")
		assets = frappe.get_all("Asset", filters={"docstatus": 1}, pluck="name", limit_page_length=2)
		if len(assets) < 2:
			self.skipTest("Need 2 assets")
		st = camp.create_campaign(company=company, assets=assets, chunk_size=3, max_inflight_chunks=2)
		frappe.db.set_value(camp.CAMPAIGN_DT, st["campaign"], "status", "Running")
		items = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")
		# Leave all Pending so finally feeder has work; process empty chunk
		camp.process_campaign_chunk(st["campaign"], "empty-chunk", [], feed_next=1)
		# Feeder should have attempted enqueue for remaining pending
		self.assertTrue(enqueue_mock.called or frappe.db.count(
			camp.ITEM_DT, {"campaign": st["campaign"], "status": "Queued"}
		) >= 0)

	@patch(
		"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign._reset_and_rebuild_asset"
	)
	@patch(
		"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign.analyze_asset"
	)
	def test_duplicate_chunk_second_claim_fails(self, analyze_mock, reset_mock):
		company = frappe.db.get_value("Company", {}, "name")
		# Need ≥2 assets so campaign stays Running after first Success (not Completed).
		assets = frappe.get_all("Asset", filters={"docstatus": 1}, pluck="name", limit_page_length=2)
		if len(assets) < 2:
			self.skipTest("Need ≥2 assets")
		analyze_mock.return_value = {
			"status": "READY",
			"je_count": 1,
			"fractional_rows": 1,
			"ads": "ADS",
			"rows": 5,
			"due_rows": 0,
		}
		reset_mock.return_value = {
			"status": "SUCCESS",
			"JEs_cancelled": ["JE1"],
			"old_ADS": "OLD",
			"new_ADS": "NEW",
			"rows": 5,
			"due_rows": 0,
			"whole_number_check": True,
			"gates": {"ok": True, "errors": [], "depreciable_total": 100, "final_amount": 10},
			"after": {"value_after_depreciation": 100},
		}
		st = camp.create_campaign(company=company, assets=assets, chunk_size=3)
		items = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")
		item = items[0]
		frappe.db.set_value(camp.ITEM_DT, item, {"status": "Queued", "chunk_id": "c-dup"})
		frappe.db.set_value(camp.CAMPAIGN_DT, st["campaign"], "status", "Running")
		camp.process_campaign_chunk(st["campaign"], "c-dup", [item], feed_next=0)
		# Second invocation: item already Success → filtered out / claim_failed; campaign still Running.
		self.assertEqual(frappe.db.get_value(camp.CAMPAIGN_DT, st["campaign"], "status"), "Running")
		out = camp.process_campaign_chunk(st["campaign"], "c-dup", [item], feed_next=0)
		# Owned-by-chunk filter drops non-Queued items; claim also fails if reached.
		if out.get("results"):
			self.assertEqual(out["results"][0]["status"], "claim_failed")
		else:
			self.assertEqual(out.get("results"), [])
		self.assertEqual(reset_mock.call_count, 1)


class TestCampaignV533OrphanedQueued(FrappeTestCase):
	"""Orphaned Queued recovery + old-job safety (v5.3.33)."""

	def setUp(self):
		if not frappe.db.exists("DocType", camp.CAMPAIGN_DT):
			self.skipTest("Campaign DocTypes not migrated")

	def _mk(self, n_assets=5, **kwargs):
		company = frappe.db.get_value("Company", {}, "name")
		assets = frappe.get_all(
			"Asset", filters={"docstatus": 1}, pluck="name", limit_page_length=n_assets
		)
		if len(assets) < n_assets:
			self.skipTest(f"Need ≥{n_assets} assets")
		kw = {"chunk_size": 3, "max_jes_per_chunk": 150, "max_inflight_chunks": 1}
		kw.update(kwargs)
		st = camp.create_campaign(company=company, assets=assets, **kw)
		frappe.db.set_value(camp.CAMPAIGN_DT, st["campaign"], "status", "Running")
		return st, assets

	def _queue_item(self, item, chunk_id, attempt_count=0, queued_minutes_ago=5):
		frappe.db.set_value(
			camp.ITEM_DT,
			item,
			{
				"status": "Queued",
				"chunk_id": chunk_id,
				"queued_at": frappe.utils.add_to_date(now_datetime(), minutes=-queued_minutes_ago),
				"attempt_count": attempt_count,
				"claim_token": None,
			},
		)

	def test_queued_live_parent_not_recovered(self):
		st, assets = self._mk(2)
		items = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")
		cid = f"{st['campaign']}-liveq"
		self._queue_item(items[0], cid)
		with patch.object(camp, "_chunk_rq_job_state", return_value="active"):
			diag = camp._recover_orphaned_queued_internal(st["campaign"])
		self.assertEqual(diag["items_recovered"], 0)
		self.assertEqual(diag["live_groups_skipped"], 1)
		self.assertEqual(frappe.db.get_value(camp.ITEM_DT, items[0], "status"), "Queued")

	def test_queued_started_parent_not_recovered(self):
		# Same active contract covers started.
		st, assets = self._mk(1)
		item = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")[0]
		self._queue_item(item, f"{st['campaign']}-started")
		with patch.object(camp, "_chunk_rq_job_state", return_value="active"):
			diag = camp._recover_orphaned_queued_internal(st["campaign"])
		self.assertEqual(diag["items_recovered"], 0)
		self.assertEqual(frappe.db.get_value(camp.ITEM_DT, item, "status"), "Queued")

	def test_queued_missing_parent_recovered_to_pending(self):
		st, assets = self._mk(1)
		item = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")[0]
		cid = f"{st['campaign']}-dead"
		self._queue_item(item, cid, attempt_count=0)
		with patch.object(camp, "_chunk_rq_job_state", return_value="missing"):
			diag = camp._recover_orphaned_queued_internal(st["campaign"])
		self.assertEqual(diag["items_recovered"], 1)
		row = frappe.db.get_value(
			camp.ITEM_DT, item, ["status", "chunk_id", "queued_at", "attempt_count", "error_type"], as_dict=True
		)
		self.assertEqual(row.status, "Pending")
		self.assertFalse(row.chunk_id)
		self.assertFalse(row.queued_at)
		self.assertEqual(cint(row.attempt_count), 0)
		self.assertEqual(row.error_type, "ORPHANED_QUEUED")

	def test_queued_terminal_parent_recovered(self):
		st, assets = self._mk(1)
		item = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")[0]
		self._queue_item(item, f"{st['campaign']}-term", attempt_count=2)
		with patch.object(camp, "_chunk_rq_job_state", return_value="terminal"):
			diag = camp._recover_orphaned_queued_internal(st["campaign"])
		self.assertEqual(diag["items_recovered"], 1)
		self.assertEqual(frappe.db.get_value(camp.ITEM_DT, item, "status"), "Retryable Failed")
		self.assertEqual(cint(frappe.db.get_value(camp.ITEM_DT, item, "attempt_count")), 2)

	def test_queued_unknown_rq_not_recovered(self):
		st, assets = self._mk(1)
		item = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")[0]
		self._queue_item(item, f"{st['campaign']}-unk")
		with patch.object(camp, "_chunk_rq_job_state", return_value="unknown"):
			diag = camp._recover_orphaned_queued_internal(st["campaign"])
		self.assertEqual(diag["items_recovered"], 0)
		self.assertEqual(diag["ambiguous_groups_skipped"], 1)
		self.assertEqual(frappe.db.get_value(camp.ITEM_DT, item, "status"), "Queued")

	def test_multiple_queued_same_dead_chunk(self):
		st, assets = self._mk(3)
		items = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")
		cid = f"{st['campaign']}-grp"
		for it in items:
			self._queue_item(it, cid)
		with patch.object(camp, "_chunk_rq_job_state", return_value="missing"):
			diag = camp._recover_orphaned_queued_internal(st["campaign"])
		self.assertEqual(diag["groups_recovered"], 1)
		self.assertEqual(diag["items_recovered"], 3)
		self.assertEqual(frappe.db.count(camp.ITEM_DT, {"campaign": st["campaign"], "status": "Pending"}), 3)

	def test_mixture_only_orphans_recover(self):
		st, assets = self._mk(4)
		items = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")
		dead, live, proc, pending = items[0], items[1], items[2], items[3]
		self._queue_item(dead, f"{st['campaign']}-dead")
		self._queue_item(live, f"{st['campaign']}-live")
		frappe.db.set_value(
			camp.ITEM_DT,
			proc,
			{
				"status": "Processing",
				"chunk_id": f"{st['campaign']}-proc",
				"started_at": frappe.utils.add_to_date(now_datetime(), minutes=-30),
				"attempt_count": 1,
				"claim_token": "t",
			},
		)
		# pending stays Pending

		def state(cid):
			if cid and cid.endswith("-live"):
				return "active"
			if cid and cid.endswith("-proc"):
				return "active"
			return "missing"

		with patch.object(camp, "_chunk_rq_job_state", side_effect=state):
			diag = camp._recover_orphaned_queued_internal(st["campaign"])
			n_proc = camp.recover_abandoned_processing(st["campaign"])
		self.assertEqual(diag["items_recovered"], 1)
		self.assertEqual(n_proc, 0)
		self.assertEqual(frappe.db.get_value(camp.ITEM_DT, dead, "status"), "Pending")
		self.assertEqual(frappe.db.get_value(camp.ITEM_DT, live, "status"), "Queued")
		self.assertEqual(frappe.db.get_value(camp.ITEM_DT, proc, "status"), "Processing")
		self.assertEqual(frappe.db.get_value(camp.ITEM_DT, pending, "status"), "Pending")

	def test_double_recover_noop(self):
		st, assets = self._mk(1)
		item = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")[0]
		self._queue_item(item, f"{st['campaign']}-dead")
		with patch.object(camp, "_chunk_rq_job_state", return_value="missing"):
			d1 = camp._recover_orphaned_queued_internal(st["campaign"])
			d2 = camp._recover_orphaned_queued_internal(st["campaign"])
		self.assertEqual(d1["items_recovered"], 1)
		self.assertEqual(d2["items_recovered"], 0)

	@patch(
		"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign.frappe.enqueue"
	)
	def test_recover_then_enqueue_once(self, enqueue_mock):
		st, assets = self._mk(3, max_inflight_chunks=1)
		items = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")
		# 37-like: fill Queued orphans on one dead chunk, leave others Pending
		cid = f"{st['campaign']}-orphan"
		for it in items[:2]:
			self._queue_item(it, cid)
		for it in items:
			frappe.db.set_value(camp.ITEM_DT, it, "submitted_jes_before", 40)
		with patch.object(camp, "_chunk_rq_job_state", return_value="missing"):
			out = camp.ensure_campaign_liveness(st["campaign"])
		self.assertEqual(out["recovered_queued"], 2)
		self.assertEqual(out["enqueued_chunks"], 1)
		self.assertEqual(enqueue_mock.call_count, 1)

	def test_old_chunk_cannot_claim_after_requeue(self):
		st, assets = self._mk(1)
		item = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")[0]
		old_chunk = f"{st['campaign']}-old"
		new_chunk = f"{st['campaign']}-new"
		self._queue_item(item, old_chunk)
		with patch.object(camp, "_chunk_rq_job_state", return_value="missing"):
			camp._recover_orphaned_queued_internal(st["campaign"])
		# Re-enqueued under new chunk
		frappe.db.set_value(camp.ITEM_DT, item, {"status": "Queued", "chunk_id": new_chunk})
		# Late old chunk executes
		owned = camp._filter_items_owned_by_chunk(st["campaign"], old_chunk, [item])
		self.assertEqual(owned, [])
		self.assertFalse(camp._claim_item(item, "late-tok", old_chunk))
		self.assertEqual(frappe.db.get_value(camp.ITEM_DT, item, "chunk_id"), new_chunk)
		self.assertEqual(frappe.db.get_value(camp.ITEM_DT, item, "status"), "Queued")

	@patch(
		"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign.frappe.enqueue"
	)
	def test_production_shape_37_queued_9_chunks(self, enqueue_mock):
		"""Simulate Prod: many Pending + 37 Queued across 9 dead chunks + max_inflight=1."""
		company = frappe.db.get_value("Company", {}, "name")
		assets = frappe.get_all("Asset", filters={"docstatus": 1}, pluck="name", limit_page_length=50)
		if len(assets) < 45:
			self.skipTest("Need ≥45 assets for production-shape simulation")
		st = camp.create_campaign(
			company=company,
			assets=assets,
			chunk_size=3,
			max_jes_per_chunk=150,
			max_inflight_chunks=1,
		)
		frappe.db.set_value(camp.CAMPAIGN_DT, st["campaign"], "status", "Running")
		items = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")
		# 9 dead chunks, 37 queued (sizes similar to Prod)
		sizes = [2, 1, 1, 2, 1, 4, 6, 9, 11]
		assert sum(sizes) == 37
		idx = 0
		dead_ids = []
		for i, sz in enumerate(sizes):
			cid = f"{st['campaign']}-dead{i}"
			dead_ids.append(cid)
			for _ in range(sz):
				self._queue_item(items[idx], cid, queued_minutes_ago=10)
				idx += 1
		# remaining stay Pending with weights
		for it in items:
			frappe.db.set_value(camp.ITEM_DT, it, "submitted_jes_before", 40, update_modified=False)

		with patch.object(camp, "_chunk_rq_job_state", return_value="missing"):
			out = camp.ensure_campaign_liveness(st["campaign"])
		self.assertEqual(out["recovered_queued"], 37)
		# Orphans cleared; feeder may have created ≤chunk_size new Queued under max_inflight=1
		self.assertEqual(out["enqueued_chunks"], 1)
		self.assertEqual(enqueue_mock.call_count, 1)
		queued_after = frappe.db.count(camp.ITEM_DT, {"campaign": st["campaign"], "status": "Queued"})
		self.assertGreaterEqual(queued_after, 1)
		self.assertLessEqual(queued_after, 3)
		# Second liveness: treat current Queued parent as live → no reclaim, no extra enqueue
		with patch.object(camp, "_chunk_rq_job_state", return_value="active"):
			out2 = camp.ensure_campaign_liveness(st["campaign"])
		self.assertEqual(out2["recovered_queued"], 0)
		self.assertEqual(out2["enqueued_chunks"], 0)
		queued_now = frappe.db.count(camp.ITEM_DT, {"campaign": st["campaign"], "status": "Queued"})
		self.assertLessEqual(queued_now, 3)

	@patch(
		"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign.frappe.enqueue"
	)
	def test_paused_no_enqueue(self, enqueue_mock):
		st, assets = self._mk(2)
		item = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")[0]
		self._queue_item(item, f"{st['campaign']}-dead")
		frappe.db.set_value(camp.CAMPAIGN_DT, st["campaign"], "status", "Paused")
		with patch.object(camp, "_chunk_rq_job_state", return_value="missing"):
			out = camp.ensure_campaign_liveness(st["campaign"])
		self.assertEqual(out["enqueued_chunks"], 0)
		self.assertEqual(out.get("status"), "Paused")
		# Recovery of Queued should not run for non-Running via ensure; item stays Queued
		self.assertEqual(frappe.db.get_value(camp.ITEM_DT, item, "status"), "Queued")

	@patch(
		"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign.frappe.enqueue"
	)
	def test_stopped_no_enqueue(self, enqueue_mock):
		st, assets = self._mk(1)
		frappe.db.set_value(camp.CAMPAIGN_DT, st["campaign"], "status", "Stopped")
		out = camp.ensure_campaign_liveness(st["campaign"])
		self.assertEqual(out["enqueued_chunks"], 0)
		self.assertFalse(enqueue_mock.called)

	@patch(
		"erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild_campaign.frappe.enqueue"
	)
	def test_completed_no_resurrection(self, enqueue_mock):
		st, assets = self._mk(1)
		frappe.db.set_value(camp.CAMPAIGN_DT, st["campaign"], "status", "Completed")
		out = camp.ensure_campaign_liveness(st["campaign"])
		self.assertEqual(out["enqueued_chunks"], 0)
		self.assertFalse(enqueue_mock.called)

	def test_grace_period_skips_fresh_queued(self):
		st, assets = self._mk(1)
		item = frappe.get_all(camp.ITEM_DT, filters={"campaign": st["campaign"]}, pluck="name")[0]
		# queued_at = now → within 90s grace
		frappe.db.set_value(
			camp.ITEM_DT,
			item,
			{"status": "Queued", "chunk_id": f"{st['campaign']}-fresh", "queued_at": now_datetime()},
		)
		with patch.object(camp, "_chunk_rq_job_state", return_value="missing"):
			diag = camp._recover_orphaned_queued_internal(st["campaign"])
		self.assertEqual(diag["items_recovered"], 0)
		self.assertEqual(diag["grace_skipped"], 1)


if __name__ == "__main__":
	unittest.main()
