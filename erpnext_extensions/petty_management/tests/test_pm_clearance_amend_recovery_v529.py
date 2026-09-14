# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""v5.2.9: PM Clearance cancel/amend recovery and stale JE handling."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.utils import cint

from erpnext_extensions.petty_management.services.clearance_service import (
	prepare_amended_clearance,
)
from erpnext_extensions.petty_management.services.journal_entry_service import settle_petty_cash


class TestPrepareAmendedClearanceV529(unittest.TestCase):
	def test_amended_copy_resets_workflow_status_and_je(self):
		detail = SimpleNamespace(generated_doctype="Journal Entry", generated_document="JE-OLD")
		doc = SimpleNamespace(
			amended_from="CLR-OLD",
			docstatus=0,
			journal_entry="JE-OLD",
			workflow_state="Approved",
			status="Cancelled",
			details=[detail],
		)
		with patch(
			"erpnext_extensions.petty_management.services.clearance_action_policy.ensure_workflow_state_record",
			return_value="Draft",
		):
			prepare_amended_clearance(doc)
		self.assertIsNone(doc.journal_entry)
		self.assertEqual(doc.workflow_state, "Draft")
		self.assertEqual(doc.status, "Draft")
		self.assertIsNone(detail.generated_doctype)
		self.assertIsNone(detail.generated_document)

	def test_non_amended_doc_untouched(self):
		doc = SimpleNamespace(
			amended_from=None,
			docstatus=0,
			journal_entry="JE-1",
			workflow_state="Approved",
			status="Approved",
			details=[],
		)
		prepare_amended_clearance(doc)
		self.assertEqual(doc.journal_entry, "JE-1")
		self.assertEqual(doc.workflow_state, "Approved")


class TestSettleStaleJournalEntryV529(unittest.TestCase):
	def test_cancelled_linked_je_allows_recreation(self):
		"""Stale journal_entry pointing at cancelled JE must not block settle."""
		calls = {"created": 0}

		fake_doc = MagicMock()
		fake_doc.docstatus = 1
		fake_doc.status = "Approved"
		fake_doc.journal_entry = None
		fake_doc.details = []
		fake_doc.name = "CLR-1"
		fake_doc.holder = None
		fake_doc.employee = None
		fake_doc.company = "_T"
		fake_doc.check_permission = MagicMock()
		fake_doc.db_set = MagicMock()
		fake_doc.reload = MagicMock()

		fake_je = MagicMock()
		fake_je.name = "JE-NEW"
		fake_je.docstatus = 0

		with (
			patch("frappe.get_doc", return_value=fake_doc),
			patch("frappe.has_permission", return_value=True),
			patch(
				"erpnext_extensions.petty_management.services.clearance_action_policy.heal_inactive_settlement_reference",
				return_value=True,
			),
			patch(
				"erpnext_extensions.petty_management.services.clearance_action_policy.find_active_settlement_je",
				return_value=None,
			),
			patch(
				"erpnext_extensions.petty_management.services.journal_entry_service.clearance_is_approved",
				return_value=True,
			),
			patch(
				"erpnext_extensions.petty_management.services.journal_entry_service.validate_clearance"
			),
			patch(
				"erpnext_extensions.petty_management.services.purchase_invoice_readiness.validate_purchase_invoices_for_settlement"
			),
			patch(
				"erpnext_extensions.petty_management.services.journal_entry_service.create_clearance_journal_entry",
				side_effect=lambda d: (calls.__setitem__("created", 1) or fake_je),
			),
			patch(
				"erpnext_extensions.petty_management.services.clearance_action_policy.sync_clearance_lifecycle",
				return_value="Pending Journal Entry Submission",
			),
		):
			out = settle_petty_cash("CLR-1")

		self.assertEqual(calls["created"], 1)
		self.assertEqual(out["journal_entry"], "JE-NEW")

	def test_active_draft_je_still_idempotent(self):
		fake_doc = MagicMock()
		fake_doc.docstatus = 1
		fake_doc.status = "Pending Journal Entry Submission"
		fake_doc.journal_entry = "JE-DRAFT"
		fake_doc.name = "CLR-1"
		fake_doc.check_permission = MagicMock()
		fake_doc.db_set = MagicMock()
		fake_doc.reload = MagicMock()

		with (
			patch("frappe.get_doc", return_value=fake_doc),
			patch("frappe.has_permission", return_value=True),
			patch(
				"erpnext_extensions.petty_management.services.clearance_action_policy.heal_inactive_settlement_reference",
				return_value=False,
			),
			patch(
				"erpnext_extensions.petty_management.services.clearance_action_policy.find_active_settlement_je",
				return_value="JE-DRAFT",
			),
			patch(
				"erpnext_extensions.petty_management.services.clearance_action_policy.sync_clearance_lifecycle",
				return_value="Pending Journal Entry Submission",
			),
			patch(
				"erpnext_extensions.petty_management.services.journal_entry_service.create_clearance_journal_entry"
			) as create_je,
		):
			out = settle_petty_cash("CLR-1")

		create_je.assert_not_called()
		self.assertEqual(out["journal_entry"], "JE-DRAFT")
		self.assertEqual(cint(0), 0)


if __name__ == "__main__":
	unittest.main()
