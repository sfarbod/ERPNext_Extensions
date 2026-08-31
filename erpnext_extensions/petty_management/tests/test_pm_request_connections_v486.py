# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.8.6 — PM Request Connections tab payload."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.petty_management.services.funding_queries import (
	list_payment_entries_for_pm_request,
	sum_submitted_pe_amount,
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


if __name__ == "__main__":
	unittest.main()
