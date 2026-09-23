# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

import erpnext_extensions.cheque_management.pdc_settlement_summary as pss
from erpnext_extensions.cheque_management.pdc_settlement_summary import get_settlement_summary_for_reference


class TestPdcSettlementSummaryPaymentRequest(unittest.TestCase):
	"""``document_outstanding`` for Payment Request must not mirror stale DB ``outstanding_amount`` (often 0 on draft)."""

	def test_draft_pr_zero_db_outstanding_shows_full_unpaid(self):
		row = {
			"grand_total": 50000.0,
			"outstanding_amount": 0.0,
			"company": "Test Co",
			"currency": "SAR",
			"docstatus": 0,
			"workflow_state": "Draft",
		}
		meta = MagicMock()
		meta.has_field = lambda f: f in ("grand_total", "outstanding_amount")
		fake_db = type(
			"DB",
			(),
			{"exists": staticmethod(lambda dt, nm: True), "get_value": staticmethod(lambda *a, **k: row)},
		)()
		fake_frappe = type(
			"F",
			(),
			{
				"db": fake_db,
				"has_permission": staticmethod(lambda *a, **k: True),
				"get_meta": staticmethod(lambda dt: meta),
			},
		)()
		with (
			patch.object(pss, "frappe", fake_frappe),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.is_payment_request_settlement_eligible",
				return_value=False,
			),
		):
			s = pss.get_settlement_summary_for_reference("Payment Request", "ACC-PRQ-2026-00025")
		self.assertIsNotNone(s)
		self.assertEqual(s["document_outstanding"], 50000.0)
		self.assertEqual(s["ledger_outstanding"], 50000.0)
		self.assertEqual(s["remaining_balance"], 50000.0)
		self.assertEqual(s["payment_entry_amount"], 0.0)
		self.assertEqual(s["effective_pdc_amount_direct"], 0.0)

	def test_eligible_pr_zero_db_outstanding_matches_gt_minus_pe_pdc(self):
		row = {
			"grand_total": 100.0,
			"outstanding_amount": 0.0,
			"company": "Test Co",
			"currency": "SAR",
			"docstatus": 1,
			"workflow_state": "Approved",
		}
		meta = MagicMock()
		meta.has_field = lambda f: f in ("grand_total", "outstanding_amount")
		fake_db = type(
			"DB",
			(),
			{"exists": staticmethod(lambda dt, nm: True), "get_value": staticmethod(lambda *a, **k: row)},
		)()
		fake_frappe = type(
			"F",
			(),
			{
				"db": fake_db,
				"has_permission": staticmethod(lambda *a, **k: True),
				"get_meta": staticmethod(lambda dt: meta),
			},
		)()
		with (
			patch.object(pss, "frappe", fake_frappe),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.is_payment_request_settlement_eligible",
				return_value=True,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.sum_payment_entry_allocations_to_payment_request",
				return_value=30.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.sum_payment_request_pdc_coverage_allocations",
				return_value=20.0,
			),
		):
			s = pss.get_settlement_summary_for_reference("Payment Request", "PR-1")
		self.assertIsNotNone(s)
		self.assertEqual(s["document_outstanding"], 50.0)
		self.assertEqual(s["ledger_outstanding"], 50.0)
		self.assertEqual(s["remaining_balance"], 50.0)
		self.assertEqual(s["effective_pdc_amount"], 20.0)
		self.assertEqual(s["pdc_advance_applied_amount"], 0.0)

	def test_eligible_pr_keeps_coverage_after_register_je_semantics(self):
		"""Summary uses PR coverage helper (not JE-excluding invoice effective sum)."""
		row = {
			"grand_total": 100.0,
			"outstanding_amount": 0.0,
			"company": "Test Co",
			"currency": "SAR",
			"docstatus": 1,
			"workflow_state": "Approved",
		}
		meta = MagicMock()
		meta.has_field = lambda f: f in ("grand_total", "outstanding_amount", "workflow_state")
		fake_db = type(
			"DB",
			(),
			{"exists": staticmethod(lambda dt, nm: True), "get_value": staticmethod(lambda *a, **k: row)},
		)()
		fake_frappe = type(
			"F",
			(),
			{
				"db": fake_db,
				"has_permission": staticmethod(lambda *a, **k: True),
				"get_meta": staticmethod(lambda dt: meta),
			},
		)()
		with (
			patch.object(pss, "frappe", fake_frappe),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.is_payment_request_settlement_eligible",
				return_value=True,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.sum_payment_entry_allocations_to_payment_request",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.sum_payment_request_pdc_coverage_allocations",
				return_value=80.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.sum_effective_pdc_allocations_to_reference",
				return_value=0.0,  # would be 0 after Register JE — must not be used for PR
			),
		):
			s = pss.get_settlement_summary_for_reference("Payment Request", "PR-80")
		self.assertEqual(s["effective_pdc_amount"], 80.0)
		self.assertEqual(s["remaining_balance"], 20.0)


class TestPdcSettlementSummaryWorkflowStateOptional(unittest.TestCase):
	def test_sales_invoice_without_workflow_state_column_does_not_crash(self):
		"""Sales Invoice may not have workflow_state column; summary must not query it."""
		row = {
			"grand_total": 100.0,
			"outstanding_amount": 20.0,
			"company": "Test Co",
			"currency": "SAR",
			"docstatus": 1,
		}
		meta = MagicMock()
		meta.has_field = lambda f: f in ("grand_total", "outstanding_amount")  # workflow_state absent
		m_get_value = MagicMock(return_value=row)
		fake_db = type(
			"DB",
			(),
			{
				"exists": staticmethod(lambda dt, nm: True),
				"get_value": staticmethod(lambda *a, **k: m_get_value(*a, **k)),
			},
		)()
		fake_frappe = type(
			"F",
			(),
			{
				"db": fake_db,
				"has_permission": staticmethod(lambda *a, **k: True),
				"get_meta": staticmethod(lambda dt: meta),
			},
		)()
		with (
			patch.object(pss, "frappe", fake_frappe),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.sum_payment_entry_allocations_to_reference",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.sum_effective_pdc_allocations_to_reference",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.sum_effective_pdc_allocations_via_payment_request_to_invoice",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.get_invoice_ledger_outstanding",
				return_value=20.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.get_remaining_settlement_capacity",
				return_value=20.0,
			),
			patch.object(pss, "_sum_net_pdc_advance_applied_on_invoice", return_value=20000.0),
		):
			s = pss.get_settlement_summary_for_reference("Sales Invoice", "SINV-1")

		self.assertIsNotNone(s)
		self.assertEqual(s["reference_doctype"], "Sales Invoice")
		self.assertEqual(s["remaining_balance"], 20.0)
		self.assertEqual(s["pdc_advance_applied_amount"], 20000.0)
		# Ensure the get_value field list did not include workflow_state
		args = m_get_value.call_args[0]
		self.assertNotIn("workflow_state", args[2])


class TestPdcSettlementSummaryAdvanceAppliedLine(unittest.TestCase):
	def test_invoice_advance_applied_computed_from_application_jes_purchase_invoice(self):
		"""Net applied is driven by posted application JEs (credit - debit on advance-paid account)."""
		row = {
			"grand_total": 50000.0,
			"outstanding_amount": 30000.0,
			"company": "Test Co",
			"currency": "SAR",
			"docstatus": 1,
		}
		meta = MagicMock()
		meta.has_field = lambda f: f in ("grand_total", "outstanding_amount")

		def _sql(query, params=None, as_dict=False):
			q = " ".join((query or "").split())
			if "FROM `tabJournal Entry Account`" in q:
				return [{"dr": 0.0, "cr": 20000.0}]
			return []

		fake_db = type(
			"DB",
			(),
			{
				"exists": staticmethod(lambda dt, nm: True),
				"get_value": staticmethod(lambda *a, **k: row),
				"sql": staticmethod(lambda *a, **k: _sql(*a, **k)),
				"table_exists": staticmethod(lambda *a, **k: True),
			},
		)()
		fake_frappe = type(
			"F",
			(),
			{
				"db": fake_db,
				"has_permission": staticmethod(lambda *a, **k: True),
				"get_meta": staticmethod(lambda dt: meta),
			},
		)()
		with (
			patch.object(pss, "frappe", fake_frappe),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.sum_payment_entry_allocations_to_reference",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.sum_effective_pdc_allocations_to_reference",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.sum_effective_pdc_allocations_via_payment_request_to_invoice",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.get_invoice_ledger_outstanding",
				return_value=30000.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.get_remaining_settlement_capacity",
				return_value=30000.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.frappe.db.get_value",
				side_effect=lambda doctype, name, field: (
					"ADV-PAID" if (doctype == "Company" and field == "default_advance_paid_account") else None
				),
			),
		):
			s = get_settlement_summary_for_reference("Purchase Invoice", "PINV-1")

		self.assertIsNotNone(s)
		self.assertEqual(s["pdc_advance_applied_amount"], 20000.0)

	def test_invoice_advance_applied_computed_from_application_jes_sales_invoice(self):
		"""Net applied for SI is debit - credit on advance-received account."""
		row = {
			"grand_total": 50000.0,
			"outstanding_amount": 30000.0,
			"company": "Test Co",
			"currency": "SAR",
			"docstatus": 1,
		}
		meta = MagicMock()
		meta.has_field = lambda f: f in ("grand_total", "outstanding_amount")

		def _sql(query, params=None, as_dict=False):
			q = " ".join((query or "").split())
			if "FROM `tabJournal Entry Account`" in q:
				return [{"dr": 20000.0, "cr": 0.0}]
			return []

		fake_db = type(
			"DB",
			(),
			{
				"exists": staticmethod(lambda dt, nm: True),
				"get_value": staticmethod(lambda *a, **k: row),
				"sql": staticmethod(lambda *a, **k: _sql(*a, **k)),
				"table_exists": staticmethod(lambda *a, **k: True),
			},
		)()
		fake_frappe = type(
			"F",
			(),
			{
				"db": fake_db,
				"has_permission": staticmethod(lambda *a, **k: True),
				"get_meta": staticmethod(lambda dt: meta),
			},
		)()
		with (
			patch.object(pss, "frappe", fake_frappe),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.sum_payment_entry_allocations_to_reference",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.sum_effective_pdc_allocations_to_reference",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.sum_effective_pdc_allocations_via_payment_request_to_invoice",
				return_value=0.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.get_invoice_ledger_outstanding",
				return_value=30000.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.get_remaining_settlement_capacity",
				return_value=30000.0,
			),
			patch(
				"erpnext_extensions.cheque_management.pdc_settlement_summary.frappe.db.get_value",
				side_effect=lambda doctype, name, field: (
					"ADV-REC"
					if (doctype == "Company" and field == "default_advance_received_account")
					else None
				),
			),
		):
			s = get_settlement_summary_for_reference("Sales Invoice", "SINV-2")

		self.assertIsNotNone(s)
		self.assertEqual(s["pdc_advance_applied_amount"], 20000.0)
