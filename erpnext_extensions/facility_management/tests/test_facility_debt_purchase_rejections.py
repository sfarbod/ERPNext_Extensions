# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Phase 8 rejection / stale-field tests for Debt Purchase Cheque repayments.

Policy for Bank Account + PDC stale combinations (approved behaviour):

* On ``validate`` / ``before_save``, ``FacilityRepayment._normalize_repayment_method_fields``
  **clears** the sibling field for the active method:
  - Bank Account → ``post_dated_cheque = None``
  - Debt Purchase Cheque → ``bank_account = None``
* After clearing, Bank Account mode can still throw if ``post_dated_cheque`` remains
  (defensive path when ``validate_bank_account_method_fields`` is called directly).
* Under normal Document validate the clear happens first, so dual fields are not stored.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe

from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	WORKFLOW_ASSIGNED_DEBT_PURCHASE,
	WORKFLOW_DEBT_PURCHASE_SETTLED,
	WORKFLOW_REGISTERED,
	WORKFLOW_RETURNED,
)
from erpnext_extensions.facility_management.facility_debt_purchase import (
	REPAYMENT_METHOD_BANK,
	REPAYMENT_METHOD_DEBT_PURCHASE,
	validate_bank_account_method_fields,
	validate_debt_purchase_cheque_repayment,
)


class _FakeRepaymentDoc:
	"""Minimal stand-in for FacilityRepayment normalization helpers."""

	def __init__(self, **kwargs):
		for k, v in kwargs.items():
			setattr(self, k, v)

	def _normalize_repayment_method_fields(self):
		from erpnext_extensions.facility_management.doctype.facility_repayment.facility_repayment import (
			FacilityRepayment,
		)

		FacilityRepayment._normalize_repayment_method_fields(self)


class TestDebtPurchaseRejectionValidations(unittest.TestCase):
	def _facility(self, *, company: str = "C", facility_type: str = "FT-DP"):
		return SimpleNamespace(name="FAC-1", company=company, facility_type=facility_type)

	def _repayment(self, **extra):
		base = dict(
			name="FREP-1",
			facility="FAC-1",
			company="C",
			posting_date="2026-07-01",
			principal_amount=900,
			profit_amount=100,
			penalty_amount=0,
			repayment_method=REPAYMENT_METHOD_DEBT_PURCHASE,
			post_dated_cheque="PDC-1",
			bank_account=None,
			currency="IRR",
		)
		base.update(extra)
		return SimpleNamespace(**base)

	def _pdc(self, **extra):
		base = dict(
			name="PDC-1",
			cheque_direction="Receivable",
			workflow_state=WORKFLOW_ASSIGNED_DEBT_PURCHASE,
			company="C",
			currency="IRR",
			cheque_amount=1000,
			debt_purchase_repayment=None,
		)
		base.update(extra)
		return SimpleNamespace(**base)

	def _run_validate(self, repayment, facility, pdc, *, is_dp: int = 1):
		with (
			patch(
				"erpnext_extensions.facility_management.facility_debt_purchase.frappe.get_doc",
				side_effect=lambda *a, **k: pdc if a and a[0] == "Post Dated Cheque" else facility,
			),
			patch(
				"erpnext_extensions.facility_management.facility_debt_purchase.frappe.db.get_value",
				side_effect=lambda *a, **k: (is_dp if a and a[0] == "Facility Type" else None),
			),
			patch(
				"erpnext_extensions.facility_management.facility_debt_purchase.frappe.db.sql",
				return_value=[],
			),
			patch(
				"erpnext_extensions.facility_management.facility_debt_purchase.resolve_debt_purchase_in_collection_account",
				return_value="DPIC",
			),
			patch(
				"erpnext_extensions.facility_management.facility_debt_purchase.frappe.get_cached_value",
				return_value="IRR",
			),
			patch(
				"erpnext_extensions.facility_management.facility_pdc_allocation.pdc_is_allocated_to_facility",
				return_value=True,
			),
		):
			return validate_debt_purchase_cheque_repayment(repayment, facility=facility)

	def test_non_debt_purchase_facility_type_rejected(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run_validate(self._repayment(), self._facility(facility_type="FT-NORMAL"), self._pdc(), is_dp=0)
		self.assertIn("Is Debt Purchase", str(ctx.exception))

	def test_wrong_state_registered_rejected(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run_validate(
				self._repayment(), self._facility(), self._pdc(workflow_state=WORKFLOW_REGISTERED)
			)
		self.assertIn(WORKFLOW_ASSIGNED_DEBT_PURCHASE, str(ctx.exception))

	def test_wrong_state_returned_rejected(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run_validate(
				self._repayment(), self._facility(), self._pdc(workflow_state=WORKFLOW_RETURNED)
			)
		self.assertIn(WORKFLOW_ASSIGNED_DEBT_PURCHASE, str(ctx.exception))

	def test_wrong_state_settled_rejected(self):
		"""Settled is rejected by the Assigned-state gate (message cites required state)."""
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run_validate(
				self._repayment(),
				self._facility(),
				self._pdc(workflow_state=WORKFLOW_DEBT_PURCHASE_SETTLED),
			)
		self.assertIn(WORKFLOW_ASSIGNED_DEBT_PURCHASE, str(ctx.exception))

	def test_company_mismatch_rejected(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run_validate(
				self._repayment(company="C"),
				self._facility(company="C"),
				self._pdc(company="OTHER-CO"),
			)
		self.assertIn("company", str(ctx.exception).lower())

	def test_currency_mismatch_rejected(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run_validate(
				self._repayment(currency="IRR"),
				self._facility(),
				self._pdc(currency="USD"),
			)
		self.assertIn("currency", str(ctx.exception).lower())

	def test_amount_mismatch_rejected(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run_validate(
				self._repayment(principal_amount=900, profit_amount=100),
				self._facility(),
				self._pdc(cheque_amount=999),
			)
		self.assertIn("must exactly equal", str(ctx.exception))

	def test_penalty_rejected(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			self._run_validate(self._repayment(penalty_amount=50), self._facility(), self._pdc())
		self.assertIn("Penalty", str(ctx.exception))


class TestStaleMethodFieldPolicy(unittest.TestCase):
	"""Document approved clear-on-normalize policy for dual method fields."""

	def test_bank_method_clears_post_dated_cheque(self):
		doc = _FakeRepaymentDoc(
			repayment_method=REPAYMENT_METHOD_BANK,
			post_dated_cheque="PDC-STALE",
			bank_account="BANK-1",
		)
		doc._normalize_repayment_method_fields()
		self.assertEqual(doc.repayment_method, REPAYMENT_METHOD_BANK)
		self.assertIsNone(doc.post_dated_cheque)
		self.assertEqual(doc.bank_account, "BANK-1")

	def test_debt_purchase_method_clears_bank_account(self):
		doc = _FakeRepaymentDoc(
			repayment_method=REPAYMENT_METHOD_DEBT_PURCHASE,
			post_dated_cheque="PDC-1",
			bank_account="BANK-STALE",
		)
		doc._normalize_repayment_method_fields()
		self.assertEqual(doc.repayment_method, REPAYMENT_METHOD_DEBT_PURCHASE)
		self.assertEqual(doc.post_dated_cheque, "PDC-1")
		self.assertIsNone(doc.bank_account)

	def test_bank_method_rejects_pdc_if_still_set(self):
		"""Defensive path when clear did not run (direct validator call)."""
		rep = SimpleNamespace(post_dated_cheque="PDC-1", bank_account="BANK", facility="FAC-1")
		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_bank_account_method_fields(rep)
		self.assertIn("must be empty", str(ctx.exception))

	def test_empty_method_normalizes_to_bank_and_clears_pdc(self):
		doc = _FakeRepaymentDoc(
			repayment_method=None,
			post_dated_cheque="PDC-STALE",
			bank_account="BANK-1",
		)
		doc._normalize_repayment_method_fields()
		self.assertEqual(doc.repayment_method, REPAYMENT_METHOD_BANK)
		self.assertIsNone(doc.post_dated_cheque)


if __name__ == "__main__":
	unittest.main()
