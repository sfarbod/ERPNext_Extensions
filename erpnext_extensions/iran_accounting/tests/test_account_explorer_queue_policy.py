# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""v5.1.5 queue isolation: export vs prepared must not share a starvation domain."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from erpnext_extensions.iran_accounting.account_explorer import export as export_mod
from erpnext_extensions.iran_accounting.account_explorer.queue_policy import (
	AE_EXPORT_QUEUE,
	AE_PREPARED_QUEUE,
	AE_PREPARED_TIMEOUT_SECONDS,
	assert_queues_isolated,
	export_enqueue_kwargs,
	prepared_enqueue_kwargs,
)


class TestAccountExplorerQueuePolicy(unittest.TestCase):
	def test_queues_are_isolated(self):
		assert_queues_isolated()
		self.assertNotEqual(AE_EXPORT_QUEUE, AE_PREPARED_QUEUE)
		self.assertEqual(AE_EXPORT_QUEUE, "long")
		self.assertEqual(AE_PREPARED_QUEUE, "short")

	def test_prepared_enqueue_kwargs_use_interactive_queue(self):
		kwargs = prepared_enqueue_kwargs(prepared_result_name="x", payload={})
		self.assertEqual(kwargs["queue"], AE_PREPARED_QUEUE)
		self.assertEqual(kwargs["timeout"], AE_PREPARED_TIMEOUT_SECONDS)
		self.assertTrue(kwargs["at_front"])

	def test_export_enqueue_kwargs_use_bulk_queue(self):
		kwargs = export_enqueue_kwargs(payload={}, file_format="csv", user="Administrator")
		self.assertEqual(kwargs["queue"], AE_EXPORT_QUEUE)

	def test_large_export_routes_to_export_queue(self):
		payload = {
			"document_scope": {
				"company": "_Test Company",
				"from_date": "2026-03-21",
				"to_date": "2027-03-20",
				"status": {
					"include_opening_entries": 1,
					"include_cancelled_entries": 0,
					"include_default_finance_book_entries": 1,
					"include_period_closing_vouchers": 0,
				},
			},
			"analysis_context": {
				"view_axis": "voucher",
				"detail_mode": "summary",
				"page": 1,
				"page_size": 50,
			},
		}
		fake_job = MagicMock()
		fake_job.id = "test-export-job"
		with patch(
			"erpnext_extensions.iran_accounting.account_explorer.export.assert_export_allowed"
		):
			with patch(
				"erpnext_extensions.iran_accounting.account_explorer.export.AccountExplorerQuerySpec_from_client"
			) as spec_mock:
				spec = MagicMock()
				spec.detail_mode = "summary"
				spec.view_axis = "voucher"
				spec_mock.return_value = spec
				with patch.object(export_mod, "_export_settings", return_value={
					"export_enabled": 1,
					"export_background_threshold": 5000,
					"server_page_size": 200,
				}):
					with patch.object(export_mod, "_probe_export_size", return_value=5001):
						with patch("frappe.enqueue", return_value=fake_job) as enqueue_mock:
							result = export_mod.export_account_explorer(
								payload, "csv", force_sync=False
							)
		self.assertEqual(result.get("queued"), 1)
		self.assertEqual(result.get("queue"), AE_EXPORT_QUEUE)
		self.assertEqual(result.get("job_id"), "test-export-job")
		self.assertEqual(enqueue_mock.call_args.kwargs.get("queue"), AE_EXPORT_QUEUE)
		self.assertNotEqual(enqueue_mock.call_args.kwargs.get("queue"), AE_PREPARED_QUEUE)

	def test_small_export_stays_sync_no_enqueue(self):
		payload = {
			"document_scope": {
				"company": "_Test Company",
				"from_date": "2026-03-21",
				"to_date": "2027-03-20",
				"status": {
					"include_opening_entries": 1,
					"include_cancelled_entries": 0,
					"include_default_finance_book_entries": 1,
					"include_period_closing_vouchers": 0,
				},
			},
			"analysis_context": {
				"view_axis": "account_level",
				"detail_mode": "summary",
				"page": 1,
				"page_size": 50,
			},
		}
		frappe.local.response = frappe._dict()
		with patch(
			"erpnext_extensions.iran_accounting.account_explorer.export.assert_export_allowed"
		):
			with patch(
				"erpnext_extensions.iran_accounting.account_explorer.export.AccountExplorerQuerySpec_from_client"
			) as spec_mock:
				spec = MagicMock()
				spec.detail_mode = "summary"
				spec.view_axis = "account_level"
				spec_mock.return_value = spec
				with patch.object(export_mod, "_export_settings", return_value={
					"export_enabled": 1,
					"export_background_threshold": 5000,
					"server_page_size": 200,
				}):
					with patch.object(export_mod, "_probe_export_size", return_value=10):
						with patch.object(
							export_mod, "collect_export_rows", return_value=([], {}, 10)
						):
							with patch.object(export_mod, "get_export_columns", return_value=[]):
								with patch.object(export_mod, "build_csv_content", return_value="a,b\n"):
									with patch("frappe.enqueue") as enqueue_mock:
										result = export_mod.export_account_explorer(
											payload, "csv", force_sync=False
										)
		self.assertNotEqual(result.get("queued"), 1)
		enqueue_mock.assert_not_called()

	def test_prepared_enqueue_calls_interactive_queue(self):
		from erpnext_extensions.iran_accounting.account_explorer import prepared_report as pr

		# Force non-inline path regardless of unittest harness flags.
		prev_in_test = frappe.flags.in_test
		prev_inline = getattr(frappe.flags, "ae_prepared_inline", False)
		frappe.flags.in_test = False
		frappe.flags.ae_prepared_inline = False
		try:
			with patch(
				"erpnext_extensions.iran_accounting.account_explorer.background_jobs.workers_available",
				return_value=True,
			):
				with patch.object(pr, "_delete_stale_rq_job"):
					with patch("frappe.enqueue") as enqueue_mock:
						with patch("frappe.db.commit"):
							pr._enqueue_or_inline("prepdoc1", {"x": 1})
			enqueue_mock.assert_called_once()
			kwargs = enqueue_mock.call_args.kwargs
			self.assertEqual(kwargs.get("queue"), AE_PREPARED_QUEUE)
			self.assertEqual(kwargs.get("timeout"), AE_PREPARED_TIMEOUT_SECONDS)
			self.assertTrue(kwargs.get("at_front"))
			self.assertNotEqual(kwargs.get("queue"), AE_EXPORT_QUEUE)
		finally:
			frappe.flags.in_test = prev_in_test
			frappe.flags.ae_prepared_inline = prev_inline

	def test_no_sync_wait_on_export_enqueue(self):
		payload = {
			"document_scope": {
				"company": "_Test Company",
				"from_date": "2026-03-21",
				"to_date": "2027-03-20",
				"status": {
					"include_opening_entries": 1,
					"include_cancelled_entries": 0,
					"include_default_finance_book_entries": 1,
					"include_period_closing_vouchers": 0,
				},
			},
			"analysis_context": {
				"view_axis": "voucher",
				"detail_mode": "summary",
				"page": 1,
				"page_size": 50,
			},
		}
		with patch(
			"erpnext_extensions.iran_accounting.account_explorer.export.assert_export_allowed"
		):
			with patch(
				"erpnext_extensions.iran_accounting.account_explorer.export.AccountExplorerQuerySpec_from_client"
			) as spec_mock:
				spec = MagicMock()
				spec.detail_mode = "summary"
				spec.view_axis = "voucher"
				spec_mock.return_value = spec
				with patch.object(export_mod, "_export_settings", return_value={
					"export_enabled": 1,
					"export_background_threshold": 5000,
					"server_page_size": 200,
				}):
					with patch.object(export_mod, "_probe_export_size", return_value=9000):
						with patch("frappe.enqueue", return_value=MagicMock(id="j1")) as enqueue_mock:
							export_mod.export_account_explorer(payload, "csv", force_sync=False)
		kwargs = enqueue_mock.call_args.kwargs
		self.assertNotIn("now", kwargs)
		args = enqueue_mock.call_args.args
		self.assertTrue(args)
		self.assertIn("run_account_explorer_export_job", args[0])
