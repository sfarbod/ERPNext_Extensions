# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests — Historical Repair dashboard snapshot / worker / scan job lifecycle."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
	FRESHNESS_FRESH,
	FRESHNESS_NOT_SCANNED,
	FRESHNESS_STALE,
	PRIORITY_KPI_LABELS,
	_freshness_for,
	get_dashboard_summary,
	patch_metrics_snapshot,
	save_metrics_snapshot,
)
from erpnext_extensions.iran_accounting.historical_stock.scan_job import (
	cancel_scan_all_job,
	start_scan_all_job,
)


class TestMetricsSnapshot(unittest.TestCase):
	def test_priority_labels_cover_operational_kpis(self):
		for label in (
			"Integrity Score",
			"User Action Required",
			"Tool Limit",
			"Posting Order",
			"Wrong Rate",
			"I1 Negative Rate",
			"I4 Leftover",
		):
			self.assertIn(label, PRIORITY_KPI_LABELS)

	def test_freshness_not_scanned_without_timestamp(self):
		self.assertEqual(_freshness_for(None), FRESHNESS_NOT_SCANNED)

	def test_freshness_stale_after_threshold(self):
		self.assertEqual(_freshness_for("2000-01-01 00:00:00"), FRESHNESS_STALE)

	def test_save_and_load_roundtrip_via_cache(self):
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot._ensure_doctype",
			return_value=False,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot.frappe.db.exists",
			return_value=False,
		):
			saved = save_metrics_snapshot(
				company="TEST-CO",
				dashboard={"Posting Order": 10, "Integrity Score": 31},
				timing={"posting_order": 1.2},
				source="unit_test",
			)
			self.assertEqual(saved["dashboard"]["Posting Order"], 10)
			self.assertEqual(saved["freshness"], FRESHNESS_FRESH)
			patched = patch_metrics_snapshot("TEST-CO", {"Posting Order": 7}, source="incremental")
			self.assertEqual(patched["dashboard"]["Posting Order"], 7)
			self.assertEqual(patched["dashboard"]["Integrity Score"], 31)

	def test_dashboard_summary_uses_worker_probe(self):
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot.load_metrics_snapshot",
			return_value={
				"dashboard": {"Posting Order": 443},
				"scanned_at": "2000-01-01 00:00:00",
				"freshness": FRESHNESS_STALE,
				"source": "test",
			},
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot.worker_queue_status",
			return_value={"available": False, "message": "workers paused", "queue": "long"},
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.scan_job.get_active_scan_job",
			return_value=None,
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot.frappe.db.exists",
			return_value=False,
		):
			summary = get_dashboard_summary("TEST-CO")
		self.assertEqual(summary["dashboard"]["Posting Order"], 443)
		self.assertFalse(summary["worker"]["available"])
		self.assertEqual(summary["freshness"], FRESHNESS_STALE)


class TestScanJobWorkerGate(unittest.TestCase):
	def test_start_without_worker_returns_worker_unavailable(self):
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot.worker_queue_status",
			return_value={"available": False, "message": "paused", "queue": "long", "workers_for_queue": 0},
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.scan_job.frappe.cache"
		) as cache_fn, patch(
			"erpnext_extensions.iran_accounting.historical_stock.scan_job.frappe.enqueue"
		) as enqueue, patch(
			"erpnext_extensions.iran_accounting.historical_stock.scan_job.frappe.session"
		) as session, patch(
			"erpnext_extensions.iran_accounting.historical_stock.scan_job.frappe.generate_hash",
			return_value="ABCDEF1234",
		):
			session.user = "Administrator"
			store = {}

			class _Cache:
				def get_value(self, k):
					return store.get(k)

				def set_value(self, k, v, expires_in_sec=None):
					store[k] = v

				def delete_value(self, k):
					store.pop(k, None)

			cache_fn.return_value = _Cache()
			out = start_scan_all_job(company="TEST-CO")
			self.assertEqual(out["status"], "WORKER_UNAVAILABLE")
			self.assertFalse(out.get("enqueued"))
			enqueue.assert_not_called()

	def test_duplicate_active_job_is_deduplicated(self):
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot.worker_queue_status",
			return_value={"available": True, "message": "ok", "queue": "long", "workers_for_queue": 1},
		), patch(
			"erpnext_extensions.iran_accounting.historical_stock.scan_job.frappe.cache"
		) as cache_fn, patch(
			"erpnext_extensions.iran_accounting.historical_stock.scan_job.frappe.enqueue"
		) as enqueue, patch(
			"erpnext_extensions.iran_accounting.historical_stock.scan_job.frappe.session"
		) as session:
			session.user = "Administrator"
			store = {
				"hr_scan_all_active:TEST-CO": "HRS-EXISTING",
				"hr_scan_all:HRS-EXISTING": {
					"job_id": "HRS-EXISTING",
					"status": "RUNNING",
					"phase": "full_integrity_scan",
					"progress": 40,
					"company": "TEST-CO",
				},
			}

			class _Cache:
				def get_value(self, k):
					return store.get(k)

				def set_value(self, k, v, expires_in_sec=None):
					store[k] = v

				def delete_value(self, k):
					store.pop(k, None)

			cache_fn.return_value = _Cache()
			out = start_scan_all_job(company="TEST-CO")
			self.assertTrue(out.get("deduplicated"))
			self.assertEqual(out["job_id"], "HRS-EXISTING")
			enqueue.assert_not_called()

	def test_cancel_queued_job(self):
		store = {
			"hr_scan_all:HRS-1": {
				"job_id": "HRS-1",
				"status": "QUEUED",
				"company": "TEST-CO",
			},
			"hr_scan_all_active:TEST-CO": "HRS-1",
		}

		class _Cache:
			def get_value(self, k):
				return store.get(k)

			def set_value(self, k, v, expires_in_sec=None):
				store[k] = v

			def delete_value(self, k):
				store.pop(k, None)

		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.scan_job.frappe.cache",
			return_value=_Cache(),
		):
			out = cancel_scan_all_job("HRS-1")
		self.assertEqual(out["status"], "CANCELLED")
		self.assertEqual(store["hr_scan_all:HRS-1"]["status"], "CANCELLED")


if __name__ == "__main__":
	unittest.main()
