# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v5.7.25 — Bounced → Sent to Bank / Assigned DP reassignment (unit)."""

from __future__ import annotations

import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from erpnext_extensions.cheque_management.pdc_accounting_idempotency import (
	CYCLEABLE_OPEN_OCCURRENCE_EDGES,
	is_cycleable_open_occurrence_edge,
)
from erpnext_extensions.cheque_management.pdc_journal_entry_service import _purpose_for_transition
from erpnext_extensions.cheque_management.pdc_transition_accounting_registry import (
	get_accounting_transition_spec,
)
from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	CHEQUE_DIRECTION_PAYABLE,
	CHEQUE_DIRECTION_RECEIVABLE,
	PDC_ACCOUNTING_JOURNAL_ENTRY,
	WORKFLOW_ASSIGNED_DEBT_PURCHASE,
	WORKFLOW_BOUNCED,
	WORKFLOW_DRAFT,
	WORKFLOW_REGISTERED,
	WORKFLOW_RETURNED,
	WORKFLOW_SENT_TO_BANK,
	get_pdc_accounting_decision,
	get_pdc_workflow_transition_validation_error,
	is_workflow_transition_allowed,
)


def _err(direction, prev, new):
	return get_pdc_workflow_transition_validation_error(direction, prev, new)


class TestBouncedReassignmentWorkflow(unittest.TestCase):
	def test_bounced_to_sent_to_bank_allowed(self):
		self.assertTrue(
			is_workflow_transition_allowed(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_BOUNCED, WORKFLOW_SENT_TO_BANK
			)
		)
		self.assertIsNone(_err(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_BOUNCED, WORKFLOW_SENT_TO_BANK))

	def test_bounced_to_assigned_dp_allowed(self):
		self.assertTrue(
			is_workflow_transition_allowed(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_BOUNCED, WORKFLOW_ASSIGNED_DEBT_PURCHASE
			)
		)
		self.assertIsNone(
			_err(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_BOUNCED, WORKFLOW_ASSIGNED_DEBT_PURCHASE)
		)

	def test_existing_bounced_exits_still_allowed(self):
		self.assertIsNone(_err(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_BOUNCED, WORKFLOW_RETURNED))

	def test_payable_cannot_reassign_from_bounced(self):
		self.assertIsNotNone(_err(CHEQUE_DIRECTION_PAYABLE, WORKFLOW_BOUNCED, WORKFLOW_SENT_TO_BANK))
		self.assertIsNotNone(
			_err(CHEQUE_DIRECTION_PAYABLE, WORKFLOW_BOUNCED, WORKFLOW_ASSIGNED_DEBT_PURCHASE)
		)

	def test_invalid_direct_api_edges_still_rejected(self):
		self.assertIsNotNone(_err(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_DRAFT, WORKFLOW_SENT_TO_BANK))
		self.assertIsNotNone(
			_err(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_BOUNCED, WORKFLOW_REGISTERED)
		)
		self.assertIsNotNone(
			_err(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_DRAFT, WORKFLOW_ASSIGNED_DEBT_PURCHASE)
		)

	def test_accounting_decisions_are_journal_entry(self):
		self.assertEqual(
			get_pdc_accounting_decision(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_BOUNCED, WORKFLOW_SENT_TO_BANK
			),
			PDC_ACCOUNTING_JOURNAL_ENTRY,
		)
		self.assertEqual(
			get_pdc_accounting_decision(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_BOUNCED, WORKFLOW_ASSIGNED_DEBT_PURCHASE
			),
			PDC_ACCOUNTING_JOURNAL_ENTRY,
		)

	def test_registry_specs(self):
		for to_state in (WORKFLOW_SENT_TO_BANK, WORKFLOW_ASSIGNED_DEBT_PURCHASE):
			spec = get_accounting_transition_spec(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_BOUNCED, to_state
			)
			self.assertIsNotNone(spec)
			self.assertTrue(spec.touches_party)

	def test_purposes(self):
		self.assertEqual(
			_purpose_for_transition(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_BOUNCED, WORKFLOW_SENT_TO_BANK
			),
			"Under Collection",
		)
		self.assertEqual(
			_purpose_for_transition(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_BOUNCED, WORKFLOW_ASSIGNED_DEBT_PURCHASE
			),
			"Debt Purchase Assignment",
		)

	def test_bounce_reassign_edges_are_cycleable(self):
		self.assertTrue(is_cycleable_open_occurrence_edge(WORKFLOW_BOUNCED, WORKFLOW_SENT_TO_BANK))
		self.assertTrue(
			is_cycleable_open_occurrence_edge(WORKFLOW_BOUNCED, WORKFLOW_ASSIGNED_DEBT_PURCHASE)
		)
		self.assertIn((WORKFLOW_SENT_TO_BANK, WORKFLOW_BOUNCED), CYCLEABLE_OPEN_OCCURRENCE_EDGES)
		self.assertIn(
			(WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_BOUNCED), CYCLEABLE_OPEN_OCCURRENCE_EDGES
		)

	def test_assigned_to_registered_return_allowed(self):
		self.assertTrue(
			is_workflow_transition_allowed(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_REGISTERED
			)
		)
		self.assertIsNone(
			_err(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_REGISTERED)
		)
		self.assertEqual(
			get_pdc_accounting_decision(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_REGISTERED
			),
			PDC_ACCOUNTING_JOURNAL_ENTRY,
		)
		self.assertEqual(
			_purpose_for_transition(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_REGISTERED
			),
			"Return from Debt Purchase",
		)
		self.assertTrue(
			is_cycleable_open_occurrence_edge(WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_REGISTERED)
		)
		self.assertTrue(
			is_cycleable_open_occurrence_edge(WORKFLOW_REGISTERED, WORKFLOW_ASSIGNED_DEBT_PURCHASE)
		)

	def test_assigned_to_party_returned_still_rejected(self):
		self.assertIsNotNone(
			_err(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_RETURNED)
		)


class TestBouncedReassignmentJeBuilders(unittest.TestCase):
	def _doc(self, **extra):
		base = dict(
			cheque_direction=CHEQUE_DIRECTION_RECEIVABLE,
			cheque_amount=1000,
			cheque_no="BN-1",
			cheque_purpose=None,
			party_type="Customer",
			party="CUST-1",
			company="C",
			account_paid_to="CIH",
			account_paid_from="AR",
			name="PDC-BN-1",
			bank_account=None,
			workflow_state=WORKFLOW_BOUNCED,
			cheque_status="Bounced",
			cheque_due_date=None,
			journal_references=[],
		)
		base.update(extra)
		return SimpleNamespace(**base)

	def _acc(self, **extra):
		acc = {
			"cheques_in_hand": "CIH",
			"debt_purchase_in_collection": "DPIC",
			"cheques_in_clearing": "CLR",
			"payable_cheque": None,
			"protested": "PROT",
			"endorsement_account": None,
		}
		acc.update(extra)
		return acc

	def _patch_build(self, acc):
		return (
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
			),
		)

	def test_bounced_to_sent_credits_protested_holding(self):
		from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
			build_pdc_journal_entry_data,
		)

		doc = self._doc()
		acc = self._acc()
		patches = self._patch_build(acc)
		with patches[0], patches[1], patches[2], patches[3], patches[4] as mf:
			mf._ = lambda s: s
			mf.db.get_value = lambda *a, **k: None
			payload = build_pdc_journal_entry_data(
				doc, WORKFLOW_BOUNCED, WORKFLOW_SENT_TO_BANK, date(2026, 7, 26)
			)
		self.assertIsNotNone(payload)
		accounts = payload["accounts"]
		self.assertEqual(accounts[0]["account"], "CLR")
		self.assertEqual(accounts[0]["debit_in_account_currency"], 1000)
		self.assertEqual(accounts[1]["account"], "PROT")
		self.assertEqual(accounts[1]["credit_in_account_currency"], 1000)
		self.assertEqual(accounts[0].get("party"), "CUST-1")
		self.assertEqual(accounts[1].get("party"), "CUST-1")

	def test_bounced_to_assigned_dp_credits_holding_debits_dpic(self):
		from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
			build_pdc_journal_entry_data,
		)

		doc = self._doc()
		acc = self._acc()
		patches = self._patch_build(acc)
		with patches[0], patches[1], patches[2], patches[3], patches[4] as mf:
			mf._ = lambda s: s
			mf.db.get_value = lambda *a, **k: None
			payload = build_pdc_journal_entry_data(
				doc, WORKFLOW_BOUNCED, WORKFLOW_ASSIGNED_DEBT_PURCHASE, date(2026, 7, 26)
			)
		self.assertIsNotNone(payload)
		accounts = payload["accounts"]
		self.assertEqual(accounts[0]["account"], "DPIC")
		self.assertEqual(accounts[0]["debit_in_account_currency"], 1000)
		self.assertEqual(accounts[1]["account"], "PROT")
		self.assertEqual(accounts[1]["credit_in_account_currency"], 1000)
		self.assertEqual(accounts[0].get("party"), "CUST-1")
		self.assertEqual(accounts[1].get("party"), "CUST-1")

	def test_holding_prefers_bounce_je_debit_account(self):
		from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
			build_pdc_journal_entry_data,
		)

		doc = self._doc(
			journal_references=[
				SimpleNamespace(
					pdc_transition_key="PDC-BN-1|Receivable|Sent to Bank|Bounced",
					journal_entry="JE-BOUNCE-1",
					purpose="Returned",
				)
			]
		)
		acc = self._acc()
		patches = self._patch_build(acc)
		with patches[0], patches[1], patches[2], patches[3], patches[4] as mf:
			mf._ = lambda s: s
			mf.db.get_value = lambda *a, **k: "CIH-FROM-BOUNCE"
			payload = build_pdc_journal_entry_data(
				doc, WORKFLOW_BOUNCED, WORKFLOW_ASSIGNED_DEBT_PURCHASE, date(2026, 7, 26)
			)
		self.assertEqual(payload["accounts"][1]["account"], "CIH-FROM-BOUNCE")

	def test_registered_to_assigned_still_credits_cih(self):
		"""Regression: normal DP assignment still credits CIH, not bounce holding."""
		from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
			build_pdc_journal_entry_data,
		)

		doc = self._doc(workflow_state=WORKFLOW_REGISTERED, journal_references=[])
		acc = self._acc()
		patches = self._patch_build(acc)
		with patches[0], patches[1], patches[2], patches[3], patches[4] as mf:
			mf._ = lambda s: s
			payload = build_pdc_journal_entry_data(
				doc, WORKFLOW_REGISTERED, WORKFLOW_ASSIGNED_DEBT_PURCHASE, date(2026, 7, 26)
			)
		self.assertEqual(payload["accounts"][0]["account"], "DPIC")
		self.assertEqual(payload["accounts"][1]["account"], "CIH")

	def test_return_from_assigned_debits_cih_credits_dpic(self):
		from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
			build_pdc_journal_entry_data,
		)

		doc = self._doc(
			workflow_state=WORKFLOW_ASSIGNED_DEBT_PURCHASE,
			journal_references=[
				SimpleNamespace(
					purpose="Debt Purchase Assignment",
					pdc_transition_key="PDC-BN-1|Receivable|Registered|Assigned to Bank for Debt Purchase",
					journal_entry="JE-ASSIGN-1",
				)
			],
		)
		acc = self._acc()
		patches = self._patch_build(acc)
		with patches[0], patches[1], patches[2], patches[3], patches[4] as mf:
			mf._ = lambda s: s
			mf.db.get_value = lambda *a, **k: "CIH"
			payload = build_pdc_journal_entry_data(
				doc, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_REGISTERED, date(2026, 7, 26)
			)
		self.assertIsNotNone(payload)
		accounts = payload["accounts"]
		self.assertEqual(accounts[0]["account"], "CIH")
		self.assertEqual(accounts[0]["debit_in_account_currency"], 1000)
		self.assertEqual(accounts[1]["account"], "DPIC")
		self.assertEqual(accounts[1]["credit_in_account_currency"], 1000)
		self.assertEqual(accounts[0].get("party"), "CUST-1")
		self.assertEqual(accounts[1].get("party"), "CUST-1")


if __name__ == "__main__":
	unittest.main()
