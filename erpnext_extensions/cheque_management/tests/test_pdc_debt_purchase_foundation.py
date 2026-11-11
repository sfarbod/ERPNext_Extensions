# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Unit tests for Debt Purchase workflow edges and JE builders (no DB)."""

from __future__ import annotations

from datetime import date

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from erpnext_extensions.cheque_management.pdc_transition_accounting_registry import (
	get_accounting_transition_spec,
)
from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	CHEQUE_DIRECTION_PAYABLE,
	CHEQUE_DIRECTION_RECEIVABLE,
	PDC_ACCOUNTING_JOURNAL_ENTRY,
	PDC_ACCOUNTING_NO_DOCUMENT,
	PDC_VALIDATION_BOUNCED_REQUIRES_RECEIVABLE_SENT_TO_BANK,
	PDC_VALIDATION_DEBT_PURCHASE_ASSIGNED_NO_CLEAR,
	PDC_VALIDATION_DEBT_PURCHASE_ASSIGNED_NO_RETURN,
	PDC_VALIDATION_DEBT_PURCHASE_SETTLED_FACILITY_ONLY,
	PDC_VALIDATION_DEBT_PURCHASE_SETTLED_IS_TERMINAL,
	WORKFLOW_ASSIGNED_DEBT_PURCHASE,
	WORKFLOW_BOUNCED,
	WORKFLOW_CLEARED,
	WORKFLOW_DEBT_PURCHASE_SETTLED,
	WORKFLOW_REGISTERED,
	WORKFLOW_RETURNED,
	get_pdc_accounting_decision,
	get_pdc_workflow_transition_validation_error,
)
from erpnext_extensions.cheque_management.pdc_workflow_to_cheque_status import (
	CHEQUE_STATUS_DEBT_PURCHASE_IN_COLLECTION,
	CHEQUE_STATUS_DEBT_PURCHASE_SETTLED,
	map_workflow_state_to_cheque_status,
)
from erpnext_extensions.facility_management.facility_debt_purchase import (
	REPAYMENT_METHOD_BANK,
	REPAYMENT_METHOD_DEBT_PURCHASE,
	normalize_repayment_method,
)


def _err(direction, prev, new):
	return get_pdc_workflow_transition_validation_error(direction, prev, new)


class TestDebtPurchaseWorkflow(unittest.TestCase):
	def test_registered_to_assigned_allowed(self):
		self.assertIsNone(
			_err(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_REGISTERED, WORKFLOW_ASSIGNED_DEBT_PURCHASE)
		)

	def test_assigned_to_bounce_allowed(self):
		self.assertIsNone(
			_err(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_BOUNCED)
		)

	def test_assigned_to_returned_rejected(self):
		self.assertEqual(
			_err(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_RETURNED),
			PDC_VALIDATION_DEBT_PURCHASE_ASSIGNED_NO_RETURN,
		)

	def test_assigned_to_registered_return_allowed(self):
		self.assertIsNone(
			_err(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_REGISTERED)
		)

	def test_assigned_to_cleared_forbidden(self):
		self.assertEqual(
			_err(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_CLEARED),
			PDC_VALIDATION_DEBT_PURCHASE_ASSIGNED_NO_CLEAR,
		)

	def test_assigned_to_settled_facility_only(self):
		self.assertEqual(
			_err(
				CHEQUE_DIRECTION_RECEIVABLE,
				WORKFLOW_ASSIGNED_DEBT_PURCHASE,
				WORKFLOW_DEBT_PURCHASE_SETTLED,
			),
			PDC_VALIDATION_DEBT_PURCHASE_SETTLED_FACILITY_ONLY,
		)

	def test_settled_is_terminal(self):
		self.assertEqual(
			_err(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_DEBT_PURCHASE_SETTLED, WORKFLOW_ASSIGNED_DEBT_PURCHASE),
			PDC_VALIDATION_DEBT_PURCHASE_SETTLED_IS_TERMINAL,
		)

	def test_payable_cannot_assign(self):
		self.assertIsNotNone(
			_err(CHEQUE_DIRECTION_PAYABLE, WORKFLOW_REGISTERED, WORKFLOW_ASSIGNED_DEBT_PURCHASE)
		)

	def test_bounce_from_registered_still_rejected(self):
		"""Non-DP bounce rule unchanged: Registered → Bounced stays forbidden."""
		self.assertEqual(
			_err(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_REGISTERED, WORKFLOW_BOUNCED),
			PDC_VALIDATION_BOUNCED_REQUIRES_RECEIVABLE_SENT_TO_BANK,
		)

	def test_accounting_decisions(self):
		self.assertEqual(
			get_pdc_accounting_decision(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_REGISTERED, WORKFLOW_ASSIGNED_DEBT_PURCHASE
			),
			PDC_ACCOUNTING_JOURNAL_ENTRY,
		)
		self.assertEqual(
			get_pdc_accounting_decision(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_BOUNCED
			),
			PDC_ACCOUNTING_JOURNAL_ENTRY,
		)
		self.assertEqual(
			get_pdc_accounting_decision(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_REGISTERED
			),
			PDC_ACCOUNTING_JOURNAL_ENTRY,
		)
		self.assertEqual(
			get_pdc_accounting_decision(
				CHEQUE_DIRECTION_RECEIVABLE,
				WORKFLOW_ASSIGNED_DEBT_PURCHASE,
				WORKFLOW_DEBT_PURCHASE_SETTLED,
			),
			PDC_ACCOUNTING_NO_DOCUMENT,
		)

	def test_cheque_status_mapping(self):
		self.assertEqual(
			map_workflow_state_to_cheque_status(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE
			),
			CHEQUE_STATUS_DEBT_PURCHASE_IN_COLLECTION,
		)
		self.assertEqual(
			map_workflow_state_to_cheque_status(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_DEBT_PURCHASE_SETTLED
			),
			CHEQUE_STATUS_DEBT_PURCHASE_SETTLED,
		)

	def test_registry_specs(self):
		spec = get_accounting_transition_spec(
			CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_REGISTERED, WORKFLOW_ASSIGNED_DEBT_PURCHASE
		)
		self.assertIsNotNone(spec)
		self.assertTrue(spec.touches_party)
		spec2 = get_accounting_transition_spec(
			CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_BOUNCED
		)
		self.assertIsNotNone(spec2)
		self.assertTrue(spec2.touches_party)

	def test_repayment_method_legacy_default(self):
		self.assertEqual(normalize_repayment_method(None), REPAYMENT_METHOD_BANK)
		self.assertEqual(normalize_repayment_method(""), REPAYMENT_METHOD_BANK)
		self.assertEqual(normalize_repayment_method(REPAYMENT_METHOD_DEBT_PURCHASE), REPAYMENT_METHOD_DEBT_PURCHASE)


class TestDebtPurchaseJeBuilder(unittest.TestCase):
	def test_assign_builder_roles(self):
		from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
			build_pdc_journal_entry_data,
		)

		doc = SimpleNamespace(
			cheque_direction=CHEQUE_DIRECTION_RECEIVABLE,
			cheque_amount=1000,
			cheque_no="DP-1",
			cheque_purpose=None,
			party_type="Customer",
			party="CUST-1",
			company="C",
			account_paid_to=None,
			account_paid_from=None,
			name="PDC-DP-1",
			bank_account=None,
			workflow_state=WORKFLOW_REGISTERED,
			cheque_status="In Hand",
			cheque_due_date=None,
		)
		acc = {
			"cheques_in_hand": "CIH",
			"debt_purchase_in_collection": "DPIC",
			"cheques_in_clearing": None,
			"payable_cheque": None,
			"protested": None,
			"endorsement_account": None,
		}
		with (
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque.resolve_pdc_accounts_for_journal",
				return_value=acc,
			),
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque._get_pdc_settings_for_company",
				return_value=None,
			),
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque._pdc_bank_gl_account",
				return_value=None,
			),
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque._pdc_account_type",
				return_value=None,
			),
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque.frappe",
			) as mf,
		):
			mf._ = lambda s: s
			payload = build_pdc_journal_entry_data(doc, WORKFLOW_REGISTERED, WORKFLOW_ASSIGNED_DEBT_PURCHASE, date(2026, 7, 26))
		self.assertIsNotNone(payload)
		accounts = payload["accounts"]
		self.assertEqual(accounts[0]["account"], "DPIC")
		self.assertEqual(accounts[0]["debit_in_account_currency"], 1000)
		self.assertEqual(accounts[0].get("party"), "CUST-1")
		self.assertEqual(accounts[1]["account"], "CIH")
		self.assertEqual(accounts[1]["credit_in_account_currency"], 1000)
		self.assertEqual(accounts[1].get("party"), "CUST-1")

	def test_bounce_from_assigned_debits_protested_credits_dpic(self):
		from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
			build_pdc_journal_entry_data,
		)

		doc = SimpleNamespace(
			cheque_direction=CHEQUE_DIRECTION_RECEIVABLE,
			cheque_amount=1000,
			cheque_no="DP-1",
			cheque_purpose=None,
			party_type="Customer",
			party="CUST-1",
			company="C",
			account_paid_to="CIH",
			account_paid_from="AR",
			name="PDC-DP-1",
			bank_account=None,
			workflow_state=WORKFLOW_ASSIGNED_DEBT_PURCHASE,
			cheque_status="Debt Purchase In Collection",
			cheque_due_date=None,
		)
		acc = {
			"cheques_in_hand": "CIH",
			"debt_purchase_in_collection": "DPIC",
			"cheques_in_clearing": None,
			"payable_cheque": None,
			"protested": "PROT",
			"endorsement_account": None,
		}
		with (
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque.resolve_pdc_accounts_for_journal",
				return_value=acc,
			),
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque._get_pdc_settings_for_company",
				return_value=None,
			),
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque._pdc_bank_gl_account",
				return_value=None,
			),
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque._pdc_account_type",
				return_value=None,
			),
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque.frappe",
			) as mf,
		):
			mf._ = lambda s: s
			payload = build_pdc_journal_entry_data(doc, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_BOUNCED, date(2026, 7, 26))
		self.assertIsNotNone(payload)
		accounts = payload["accounts"]
		self.assertEqual(accounts[0]["account"], "PROT")
		self.assertEqual(accounts[0]["debit_in_account_currency"], 1000)
		self.assertEqual(accounts[0].get("party_type"), "Customer")
		self.assertEqual(accounts[0].get("party"), "CUST-1")
		self.assertEqual(accounts[1]["account"], "DPIC")
		self.assertEqual(accounts[1]["credit_in_account_currency"], 1000)
		self.assertEqual(accounts[1].get("party_type"), "Customer")
		self.assertEqual(accounts[1].get("party"), "CUST-1")
		self.assertNotIn("CIH", [a["account"] for a in accounts])

	def test_bounce_from_assigned_requires_protested_no_cih_fallback(self):
		from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
			build_pdc_journal_entry_data,
		)

		doc = SimpleNamespace(
			cheque_direction=CHEQUE_DIRECTION_RECEIVABLE,
			cheque_amount=1000,
			cheque_no="DP-1",
			cheque_purpose=None,
			party_type="Customer",
			party="CUST-1",
			company="C",
			account_paid_to="CIH",
			account_paid_from="AR",
			name="PDC-DP-1",
			bank_account=None,
			workflow_state=WORKFLOW_ASSIGNED_DEBT_PURCHASE,
			cheque_status="Debt Purchase In Collection",
			cheque_due_date=None,
		)
		acc = {
			"cheques_in_hand": "CIH",
			"debt_purchase_in_collection": "DPIC",
			"cheques_in_clearing": None,
			"payable_cheque": None,
			"protested": None,
			"endorsement_account": None,
		}
		with (
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque.resolve_pdc_accounts_for_journal",
				return_value=acc,
			),
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque._get_pdc_settings_for_company",
				return_value=None,
			),
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque._pdc_bank_gl_account",
				return_value=None,
			),
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque.frappe"
			) as mf,
		):
			mf._ = lambda s: s

			def _throw(msg, title=None):
				raise RuntimeError(str(msg))

			mf.throw = _throw
			with self.assertRaises(RuntimeError) as ctx:
				build_pdc_journal_entry_data(doc, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_BOUNCED, date(2026, 7, 26))
			self.assertIn("Protested", str(ctx.exception))

	def test_assign_missing_dpic_returns_none(self):
		from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
			build_pdc_journal_entry_data,
		)

		doc = SimpleNamespace(
			cheque_direction=CHEQUE_DIRECTION_RECEIVABLE,
			cheque_amount=1000,
			cheque_no="DP-1",
			cheque_purpose=None,
			party_type="Customer",
			party="CUST-1",
			company="C",
			account_paid_to="CIH",
			account_paid_from=None,
			name="PDC-DP-2",
			bank_account=None,
			workflow_state=WORKFLOW_REGISTERED,
			cheque_status="In Hand",
			cheque_due_date=None,
		)
		acc = {
			"cheques_in_hand": "CIH",
			"debt_purchase_in_collection": None,
			"cheques_in_clearing": None,
			"payable_cheque": None,
			"protested": None,
			"endorsement_account": None,
		}
		with (
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque.resolve_pdc_accounts_for_journal",
				return_value=acc,
			),
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque._get_pdc_settings_for_company",
				return_value=None,
			),
		):
			payload = build_pdc_journal_entry_data(doc, WORKFLOW_REGISTERED, WORKFLOW_ASSIGNED_DEBT_PURCHASE, date(2026, 7, 26))
			self.assertIsNone(payload)


if __name__ == "__main__":
	unittest.main()
