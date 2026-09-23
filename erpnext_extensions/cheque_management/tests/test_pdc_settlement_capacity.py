# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""Unit tests for :mod:`erpnext_extensions.cheque_management.pdc_settlement_capacity`."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import erpnext_extensions.cheque_management.pdc_settlement_capacity as cap


class TestSumEffectivePdcAllocationsToReference(unittest.TestCase):
	def test_sql_aggregate_returned_for_direct_reference(self):
		fake = MagicMock()
		fake.db.sql = MagicMock(return_value=[[17_500.5]])
		with patch.object(cap, "frappe", fake):
			v = cap.sum_effective_pdc_allocations_to_reference("Sales Invoice", "SINV-1")
		self.assertEqual(v, 17_500.5)
		q = fake.db.sql.call_args[0][0]
		self.assertIn("not exists", q)
		self.assertIn("`tabPDC Journal Reference`", q)

	def test_exclude_pdc_passed_as_sql_params(self):
		fake = MagicMock()
		fake.db.sql = MagicMock(return_value=[[0.0]])
		with patch.object(cap, "frappe", fake):
			cap.sum_effective_pdc_allocations_to_reference(
				"Purchase Invoice", "PINV-1", exclude_pdc="PDC-001"
			)
		params = fake.db.sql.call_args[0][1]
		self.assertEqual(params[-2], "PDC-001")
		self.assertEqual(params[-1], "PDC-001")

	def test_exclude_pdc_falls_back_to_frappe_flags(self):
		"""Workflow transitions may validate without threading exclude_pdc everywhere; flags must still exclude self."""
		fake = MagicMock()
		fake.flags = SimpleNamespace(pdc_settlement_exclude_pdc="PDC-FLAG")
		fake.db.sql = MagicMock(return_value=[[0.0]])
		with patch.object(cap, "frappe", fake):
			cap.sum_effective_pdc_allocations_to_reference("Sales Invoice", "SINV-1", exclude_pdc=None)
		params = fake.db.sql.call_args[0][1]
		self.assertEqual(params[-2], "PDC-FLAG")
		self.assertEqual(params[-1], "PDC-FLAG")


class TestGetInvoiceRemainingCapacity(unittest.TestCase):
	def test_sales_and_purchase_invoice_parity_full_ledger_no_pending(self):
		with (
			patch.object(cap, "get_invoice_ledger_outstanding", return_value=20_000.0),
			patch.object(cap, "sum_effective_pdc_direct_to_invoice", return_value=0.0),
			patch.object(cap, "sum_effective_pdc_via_pr_to_invoice", return_value=0.0),
		):
			self.assertEqual(cap.get_invoice_remaining_capacity("Sales Invoice", "S-1"), 20_000.0)
			self.assertEqual(cap.get_invoice_remaining_capacity("Purchase Invoice", "P-1"), 20_000.0)

	def test_after_register_je_capacity_tracks_payment_ledger_only(self):
		"""Pending PDC sum is zero once Register JE exists; remaining capacity follows PL outstanding."""
		with (
			patch.object(cap, "get_invoice_ledger_outstanding", return_value=0.0),
			patch.object(cap, "sum_effective_pdc_direct_to_invoice", return_value=0.0),
			patch.object(cap, "sum_effective_pdc_via_pr_to_invoice", return_value=0.0),
		):
			self.assertEqual(cap.get_invoice_remaining_capacity("Sales Invoice", "S-1"), 0.0)

	def test_second_pdc_respects_first_pending_reservation(self):
		with (
			patch.object(cap, "get_invoice_ledger_outstanding", return_value=20_000.0),
			patch.object(cap, "sum_effective_pdc_direct_to_invoice", return_value=20_000.0),
			patch.object(cap, "sum_effective_pdc_via_pr_to_invoice", return_value=0.0),
		):
			self.assertEqual(
				cap.get_invoice_remaining_capacity("Sales Invoice", "S-1", exclude_pdc="PDC-2"), 0.0
			)

	def test_validating_same_pdc_excludes_self_from_pending_sum(self):
		with (
			patch.object(cap, "get_invoice_ledger_outstanding", return_value=20_000.0),
			patch.object(cap, "sum_effective_pdc_direct_to_invoice", return_value=0.0),
			patch.object(cap, "sum_effective_pdc_via_pr_to_invoice", return_value=0.0),
		):
			self.assertEqual(
				cap.get_invoice_remaining_capacity("Sales Invoice", "S-1", exclude_pdc="PDC-SELF"),
				20_000.0,
			)


class TestGetPrRemainingCapacity(unittest.TestCase):
	def test_pr_uses_pr_coverage_helper_not_invoice_effective_sum(self):
		fake = MagicMock()
		fake.db.exists = MagicMock(return_value=True)
		fake.db.get_value = MagicMock(
			return_value=SimpleNamespace(grand_total=100.0, docstatus=1, workflow_state="Approved")
		)
		with (
			patch.object(cap, "frappe", fake),
			patch(
				"erpnext_extensions.cheque_management.pdc_payment_request_eligibility.is_payment_request_settlement_eligible",
				return_value=True,
			),
			patch.object(cap, "sum_payment_entry_allocations_to_payment_request", return_value=0.0),
			patch.object(cap, "sum_payment_request_pdc_coverage_allocations", return_value=30.0),
		):
			self.assertEqual(cap.get_pr_remaining_capacity("PR-1", exclude_pdc=None), 70.0)

	def test_pr_partial_pdc_blocks_over_allocation(self):
		"""PR 100, coverage 80 → remaining 20 (Draft/Registered/JE-settled all use coverage helper)."""
		fake = MagicMock()
		fake.db.exists = MagicMock(return_value=True)
		fake.db.get_value = MagicMock(
			return_value=SimpleNamespace(grand_total=100.0, docstatus=1, workflow_state="Approved")
		)
		with (
			patch.object(cap, "frappe", fake),
			patch(
				"erpnext_extensions.cheque_management.pdc_payment_request_eligibility.is_payment_request_settlement_eligible",
				return_value=True,
			),
			patch.object(cap, "sum_payment_entry_allocations_to_payment_request", return_value=0.0),
			patch.object(cap, "sum_payment_request_pdc_coverage_allocations", return_value=80.0),
		):
			self.assertEqual(cap.get_pr_remaining_capacity("PR-1", exclude_pdc="PDC-2"), 20.0)


class TestSumPaymentRequestPdcCoverage(unittest.TestCase):
	def test_sql_includes_draft_and_omits_register_je_exclusion(self):
		fake = MagicMock()
		fake.db.sql = MagicMock(return_value=[[80.0]])
		with patch.object(cap, "frappe", fake):
			v = cap.sum_payment_request_pdc_coverage_allocations("PR-1")
		self.assertEqual(v, 80.0)
		q = fake.db.sql.call_args[0][0].lower()
		self.assertIn("docstatus", q)
		self.assertIn("< 2", q)
		self.assertNotIn("not exists", q)
		self.assertNotIn("pdc journal reference", q)
		self.assertIn("cancelled", q)
