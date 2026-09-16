# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests — async Scan All job enqueue / dedupe / status (v5.2.18)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from erpnext_extensions.iran_accounting.historical_stock import scan_job


class TestScanAllJob(unittest.TestCase):
	def setUp(self):
		self.store = {}

		class Cache:
			def get_value(self, key):
				return self_outer.store.get(key)

			def set_value(self, key, value, expires_in_sec=None):
				self_outer.store[key] = value

			def delete_value(self, key):
				self_outer.store.pop(key, None)

		self_outer = self
		self.cache = Cache()

	@patch("erpnext_extensions.iran_accounting.historical_stock.scan_job.frappe")
	def test_start_enqueues_long_queue(self, frappe):
		frappe.cache.return_value = self.cache
		frappe.session.user = "Administrator"
		frappe.generate_hash.return_value = "ABCDEF1234"
		frappe.enqueue = MagicMock()
		out = scan_job.start_scan_all_job(company="Co")
		self.assertEqual(out["status"], "QUEUED")
		self.assertTrue(out["job_id"].startswith("HRS-"))
		self.assertFalse(out["deduplicated"])
		kwargs = frappe.enqueue.call_args.kwargs
		self.assertEqual(kwargs.get("queue"), "long")
		self.assertEqual(kwargs.get("timeout"), 1800)

	@patch("erpnext_extensions.iran_accounting.historical_stock.scan_job.frappe")
	def test_dedupe_active_job(self, frappe):
		frappe.cache.return_value = self.cache
		frappe.session.user = "Administrator"
		frappe.generate_hash.return_value = "ABCDEF1234"
		frappe.enqueue = MagicMock()
		first = scan_job.start_scan_all_job(company="Co")
		second = scan_job.start_scan_all_job(company="Co")
		self.assertTrue(second.get("deduplicated"))
		self.assertEqual(second.get("job_id"), first.get("job_id"))
		self.assertEqual(frappe.enqueue.call_count, 1)

	@patch("erpnext_extensions.iran_accounting.historical_stock.scan_job.frappe")
	def test_run_job_completes(self, frappe):
		frappe.cache.return_value = self.cache
		frappe.session.user = "Administrator"
		frappe.generate_hash.return_value = "ZZZZZZZZZZ"
		frappe.enqueue = MagicMock()
		frappe.get_traceback.return_value = ""
		started = scan_job.start_scan_all_job(company="Co")
		job_id = started["job_id"]
		with patch(
			"erpnext_extensions.iran_accounting.historical_stock.scan.run_full_integrity_scan",
			return_value={"dashboard": {"Repairable": 1}, "timing": {"posting_order": 0.1}},
		):
			scan_job.run_scan_all_job(company="Co", scan_job_id=job_id, user="Administrator")
		status = scan_job.get_scan_all_job(job_id)
		self.assertEqual(status["status"], "COMPLETED")
		self.assertEqual(status["result"]["dashboard"]["Repairable"], 1)


if __name__ == "__main__":
	unittest.main()
