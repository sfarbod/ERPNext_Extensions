# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.8.6 — PM Request Connections tab payload and dashboard hardening."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.petty_management.doctype.pm_request.pm_request_dashboard import (
	get_pm_request_connection_counts,
)
from erpnext_extensions.petty_management.journal_entry_hooks import (
	_notify_pm_requests_for_journal_entry,
)
from erpnext_extensions.petty_management.services.funding_queries import (
	list_payment_entries_for_pm_request,
	sum_submitted_pe_amount,
)
from erpnext_extensions.petty_management.services.request_api_guard import (
	get_pm_request_response_version,
)
from erpnext_extensions.petty_management.services.request_connections_service import (
	build_pm_request_connections_payload,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.test_pm_request_cancel_delete import _make_clearance
from erpnext_extensions.petty_management.tests.test_pm_request_multi_pe import (
	_create_funding_pe,
	_new_submitted_request,
	_require_site_ready,
	_sync_funding_fields,
)


class TestPmRequestConnectionsV486(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_require_site_ready(cls)

	def setUp(self):
		frappe.set_user("Administrator")

	def test_connections_lists_and_totals_match_funding(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 20_000)
		pe1 = _create_funding_pe(req, 8_000)
		pe2 = _create_funding_pe(req, 5_000)
		_sync_funding_fields(req)
		cl = _make_clearance(emp, req, 3_000, submit=True)
		_sync_funding_fields(req)

		doc = frappe.get_doc("PM Request", req)
		payload = build_pm_request_connections_payload(doc)
		summary = payload["summary"]

		self.assertEqual(len(payload["payment_entries"]), 2)
		pe_names = {row["payment_entry"] for row in payload["payment_entries"]}
		self.assertEqual(pe_names, {pe1, pe2})
		self.assertTrue(any(row["clearance"] == cl for row in payload["clearances"]))

		expected_paid = flt(sum_submitted_pe_amount(req))
		self.assertAlmostEqual(flt(summary["total_paid"]), expected_paid, places=2)
		self.assertAlmostEqual(
			flt(summary["remaining_to_pay"]),
			max(0.0, flt(doc.total_requested_amount) - expected_paid),
			places=2,
		)
		self.assertAlmostEqual(flt(summary["allocated_amount"]), flt(doc.allocated_amount), places=2)
		self.assertAlmostEqual(
			flt(summary["available_for_clearance"]), flt(doc.available_for_clearance), places=2
		)

		api_rows = list_payment_entries_for_pm_request(req)
		self.assertEqual(len(api_rows), len(payload["payment_entries"]))

	def test_connections_fetch_is_read_only(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 12_000)
		_create_funding_pe(req, 6_000)
		_sync_funding_fields(req)

		before = frappe.db.get_value("PM Request", req, ["modified"], as_dict=True)
		version_before = get_pm_request_response_version(req)
		set_value_calls: list[tuple] = []
		save_calls: list[str] = []
		original_set_value = frappe.db.set_value
		original_save = frappe.model.document.Document.save

		def track_set_value(*args, **kwargs):
			set_value_calls.append((args, kwargs))
			return original_set_value(*args, **kwargs)

		def track_save(self, *args, **kwargs):
			save_calls.append(self.doctype)
			return original_save(self, *args, **kwargs)

		def forbid_sync(*args, **kwargs):
			raise AssertionError("sync_pm_request_funding_fields must not run on Connections fetch")

		doc = frappe.get_doc("PM Request", req)
		with patch.object(frappe.db, "set_value", side_effect=track_set_value):
			with patch.object(frappe.model.document.Document, "save", track_save):
				with patch(
					"erpnext_extensions.petty_management.services.funding_service.sync_pm_request_funding_fields",
					side_effect=forbid_sync,
				):
					build_pm_request_connections_payload(doc)
					get_pm_request_connection_counts("PM Request", req)

		after = frappe.db.get_value("PM Request", req, ["modified"], as_dict=True)
		version_after = get_pm_request_response_version(req)

		pm_request_writes = [
			call
			for call in set_value_calls
			if call[0] and call[0][0] == "PM Request"
		]
		self.assertEqual(pm_request_writes, [])
		self.assertNotIn("PM Request", save_calls)
		self.assertEqual(before.modified, after.modified)
		self.assertEqual(version_before, version_after)

	def test_connections_uses_single_clearance_allocation_query(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 14_000)
		_create_funding_pe(req, 7_000)
		_sync_funding_fields(req)
		_make_clearance(emp, req, 2_500, submit=True)
		_sync_funding_fields(req)

		doc = frappe.get_doc("PM Request", req)
		allocation_queries: list[str] = []
		original_sql = frappe.db.sql

		def counting_sql(query, *args, **kwargs):
			if isinstance(query, str) and "tabPM Clearance Request Allocation" in query:
				allocation_queries.append(query)
			return original_sql(query, *args, **kwargs)

		with patch.object(frappe.db, "sql", side_effect=counting_sql):
			payload = build_pm_request_connections_payload(doc)

		self.assertEqual(len(allocation_queries), 1)
		self.assertEqual(len(payload["clearances"]), 1)
		self.assertGreaterEqual(len(payload["payment_entries"]), 1)

	def test_dashboard_counts_match_payload(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 16_000)
		_create_funding_pe(req, 6_000)
		_sync_funding_fields(req)
		cl = _make_clearance(emp, req, 2_000, submit=True)
		_sync_funding_fields(req)

		doc = frappe.get_doc("PM Request", req)
		payload = build_pm_request_connections_payload(doc)
		counts = get_pm_request_connection_counts("PM Request", req)
		found = {
			row["doctype"]: row
			for row in counts["count"]["internal_links_found"]
		}

		self.assertEqual(found["Payment Entry"]["count"], len(payload["payment_entries"]))
		self.assertEqual(found["PM Clearance"]["count"], len(payload["clearances"]))
		self.assertEqual(found["Journal Entry"]["count"], len(payload["journal_entries"]))
		self.assertEqual(
			set(found["Payment Entry"]["names"]),
			{row["payment_entry"] for row in payload["payment_entries"]},
		)
		self.assertIn(cl, found["PM Clearance"]["names"])

	def test_clearance_create_notifies_pm_request_dashboard(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 11_000)
		_create_funding_pe(req, 5_000)
		_sync_funding_fields(req)

		version_before = get_pm_request_response_version(req)
		_make_clearance(emp, req, 1_500, submit=False)
		version_after = get_pm_request_response_version(req)
		self.assertNotEqual(version_before, version_after)

	def test_journal_entry_event_notifies_pm_request_dashboard(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 13_000)
		_create_funding_pe(req, 6_500)
		_sync_funding_fields(req)
		cl = _make_clearance(emp, req, 2_000, submit=True)
		_sync_funding_fields(req)

		je_name = "PM-CONN-TEST-JE"
		if frappe.db.exists("Journal Entry", je_name):
			frappe.delete_doc("Journal Entry", je_name, force=True)
		frappe.db.set_value("PM Clearance", cl, "journal_entry", je_name, update_modified=False)
		frappe.db.commit()

		version_before = get_pm_request_response_version(req)
		_notify_pm_requests_for_journal_entry(je_name, "on_journal_entry_submitted")
		version_after = get_pm_request_response_version(req)
		self.assertNotEqual(version_before, version_after)


if __name__ == "__main__":
	unittest.main()
