# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Unit / integration tests for Asset Depreciation Repair Campaign (v5.3.31)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import now_datetime

from erpnext_extensions.asset_usage_depreciation.services import depr_reset_rebuild_campaign as camp


class TestCampaignHelpers(unittest.TestCase):
	def test_clamp_chunk_size(self):
		self.assertEqual(camp._clamp_chunk_size(5), 10)
		self.assertEqual(camp._clamp_chunk_size(20), 20)
		self.assertEqual(camp._clamp_chunk_size(100), 25)

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
		frappe.db.set_value(camp.ITEM_DT, item, "status", "Queued")
		ok1 = camp._claim_item(item, "tok-a", "chunk-1")
		ok2 = camp._claim_item(item, "tok-b", "chunk-2")
		self.assertTrue(ok1)
		self.assertFalse(ok2)

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


if __name__ == "__main__":
	unittest.main()
