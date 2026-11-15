# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v5.7.39 — Debt Purchase Assignment Date is the authoritative assignment / JE posting date."""

from __future__ import annotations

import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import getdate, today

from erpnext_extensions.cheque_management.doctype.post_dated_cheque import post_dated_cheque as pdc_mod
from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	CHEQUE_DIRECTION_RECEIVABLE,
	WORKFLOW_ASSIGNED_DEBT_PURCHASE,
	WORKFLOW_BOUNCED,
	WORKFLOW_DRAFT,
	WORKFLOW_REGISTERED,
	WORKFLOW_SENT_TO_BANK,
	normalize_workflow_state_value,
)


class TestDebtPurchaseAssignmentDateUnit(unittest.TestCase):
	"""Unit: posting-date resolution and mandatory validation (no DB workflow)."""

	def _doc(self, **extra):
		base = {
			"cheque_direction": CHEQUE_DIRECTION_RECEIVABLE,
			"workflow_state": WORKFLOW_ASSIGNED_DEBT_PURCHASE,
			"cheque_due_date": date(2026, 12, 1),
			"received_date": date(2026, 1, 1),
			"debt_purchase_assignment_date": date(2026, 6, 15),
			"journal_references": [],
			"name": "PDC-UNIT-DP-DATE",
		}
		base.update(extra)
		return MagicMock(**base)

	def test_validate_empty_blocks(self):
		doc = pdc_mod.PostDatedCheque(
			{
				"doctype": "Post Dated Cheque",
				"cheque_direction": CHEQUE_DIRECTION_RECEIVABLE,
				"workflow_state": WORKFLOW_ASSIGNED_DEBT_PURCHASE,
			}
		)
		doc.debt_purchase_assignment_date = None
		with self.assertRaises(Exception) as ctx:
			doc._validate_debt_purchase_assignment_date()
		self.assertIn("Debt Purchase Assignment Date", str(ctx.exception))

	def test_validate_skips_when_not_assigned(self):
		doc = pdc_mod.PostDatedCheque(
			{
				"doctype": "Post Dated Cheque",
				"cheque_direction": CHEQUE_DIRECTION_RECEIVABLE,
				"workflow_state": WORKFLOW_REGISTERED,
			}
		)
		doc.debt_purchase_assignment_date = None
		doc._validate_debt_purchase_assignment_date()  # no throw

	def test_validate_invalid_date_raises(self):
		doc = pdc_mod.PostDatedCheque(
			{
				"doctype": "Post Dated Cheque",
				"cheque_direction": CHEQUE_DIRECTION_RECEIVABLE,
				"workflow_state": WORKFLOW_ASSIGNED_DEBT_PURCHASE,
			}
		)
		doc.debt_purchase_assignment_date = "not-a-date"
		with self.assertRaises(Exception) as ctx:
			doc._validate_debt_purchase_assignment_date()
		self.assertIn("invalid", str(ctx.exception).lower())


class TestDebtPurchaseAssignmentDateIntegration(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.company = frappe.db.get_value(
			"Company",
			{"name": ["not like", "\\_Test%"]},
			"name",
			order_by="creation asc",
		) or frappe.db.get_value("Company", {}, "name", order_by="creation asc")
		if not cls.company:
			raise unittest.SkipTest("No Company")
		cls.customer = frappe.db.get_value("Customer", {"disabled": 0}, "name", order_by="modified desc")
		if not cls.customer:
			raise unittest.SkipTest("No Customer")
		cls.bank_account = frappe.db.get_value(
			"Bank Account",
			{"company": cls.company, "disabled": 0, "is_company_account": 1},
			"name",
			order_by="modified desc",
		)
		if not cls.bank_account:
			raise unittest.SkipTest("No company Bank Account")
		from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
			_get_pdc_settings_for_company,
		)

		cls.settings = _get_pdc_settings_for_company(cls.company)
		if not cls.settings or not cls.settings.default_cheques_in_hand_account:
			raise unittest.SkipTest("PDC Settings missing")
		if not (cls.settings.default_debt_purchase_in_collection_account or "").strip():
			raise unittest.SkipTest("DPIC account missing")
		from erpnext_extensions.patches.post_model_sync.ensure_debt_purchase_pdc_workflow import (
			execute as sync_dp_workflow,
		)
		from erpnext_extensions.patches.post_model_sync.ensure_bounced_reassign_pdc_workflow import (
			execute as sync_bounce_reassign,
		)

		sync_dp_workflow()
		sync_bounce_reassign()
		frappe.db.commit()
		# Ensure new DocType field exists on this site.
		frappe.reload_doc("cheque_management", "doctype", "post_dated_cheque", force=True)
		frappe.db.commit()

	def _apply(self, doc, action: str):
		from frappe.model.workflow import apply_workflow

		return apply_workflow(doc, action)

	def _draft(self, *, due_days: int = 60, amount: float = 1111.0):
		from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
			get_default_party_accounts,
		)
		from erpnext_extensions.facility_management.facility_e2e_context import ensure_bank_master
		from frappe.utils import random_string

		acc = get_default_party_accounts("Customer", self.customer, self.company, "Receivable") or {}
		doc = frappe.new_doc("Post Dated Cheque")
		doc.cheque_direction = "Receivable"
		doc.company = self.company
		doc.party_type = "Customer"
		doc.party = self.customer
		doc.cheque_no = f"DP-DATE-{random_string(8)}"
		doc.cheque_due_date = getdate(today()) + timedelta(days=due_days)
		doc.cheque_amount = amount
		doc.received_date = getdate(today()) - timedelta(days=10)
		doc.drawer_bank_name = (
			frappe.db.get_value("Bank", {}, "name", order_by="modified desc") or ensure_bank_master()
		)
		doc.bank_account = self.bank_account
		doc.account_paid_to = acc.get("account_paid_to") or self.settings.default_cheques_in_hand_account
		doc.account_paid_from = acc.get("account_paid_from")
		doc.workflow_state = WORKFLOW_DRAFT
		doc.allocation_mode = "direct_settlement"
		doc.sayad_code = f"SAYAD-{doc.cheque_no}"[:32]
		doc.sayad_registered = 1
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return frappe.get_doc("Post Dated Cheque", doc.name)

	def test_registered_path_uses_field_not_due_or_today(self):
		historical = getdate(today()) - timedelta(days=45)
		doc = self._draft()
		due = getdate(doc.cheque_due_date)
		self.assertNotEqual(historical, due)
		self.assertNotEqual(historical, getdate(today()))

		doc = self._apply(doc, "Register Cheque")
		doc.reload()
		doc.debt_purchase_assignment_date = historical
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = frappe.get_doc("Post Dated Cheque", doc.name)

		doc = self._apply(doc, "Assign for Debt Purchase")
		doc.reload()
		self.assertEqual(
			normalize_workflow_state_value(doc.workflow_state), WORKFLOW_ASSIGNED_DEBT_PURCHASE
		)
		self.assertEqual(getdate(doc.debt_purchase_assignment_date), historical)

		refs = frappe.get_all(
			"PDC Journal Reference",
			filters={"parent": doc.name, "purpose": "Debt Purchase Assignment"},
			fields=["journal_entry", "posting_date"],
		)
		self.assertEqual(len(refs), 1, refs)
		self.assertEqual(getdate(refs[0].posting_date), historical)
		je_posting = frappe.db.get_value("Journal Entry", refs[0].journal_entry, "posting_date")
		self.assertEqual(getdate(je_posting), historical)

		# Idempotent: still exactly one assignment JE / JR
		existing = frappe.get_all(
			"PDC Journal Reference",
			filters={"parent": doc.name, "purpose": "Debt Purchase Assignment"},
			pluck="name",
		)
		self.assertEqual(len(existing), 1)

	def test_empty_assignment_date_blocks_registered_path(self):
		doc = self._draft()
		doc = self._apply(doc, "Register Cheque")
		doc.reload()
		doc.debt_purchase_assignment_date = None
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = frappe.get_doc("Post Dated Cheque", doc.name)
		with self.assertRaises(Exception) as ctx:
			self._apply(doc, "Assign for Debt Purchase")
		self.assertIn("Debt Purchase Assignment Date", str(ctx.exception))
		doc.reload()
		self.assertEqual(normalize_workflow_state_value(doc.workflow_state), WORKFLOW_REGISTERED)
		self.assertEqual(
			len(
				frappe.get_all(
					"PDC Journal Reference",
					filters={"parent": doc.name, "purpose": "Debt Purchase Assignment"},
				)
			),
			0,
		)

	def test_today_in_field_is_used_because_from_field(self):
		doc = self._draft()
		doc = self._apply(doc, "Register Cheque")
		doc.reload()
		doc.debt_purchase_assignment_date = today()
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = frappe.get_doc("Post Dated Cheque", doc.name)
		doc = self._apply(doc, "Assign for Debt Purchase")
		doc.reload()
		refs = frappe.get_all(
			"PDC Journal Reference",
			filters={"parent": doc.name, "purpose": "Debt Purchase Assignment"},
			fields=["posting_date"],
		)
		self.assertEqual(len(refs), 1)
		self.assertEqual(getdate(refs[0].posting_date), getdate(today()))

	def test_bounced_path_uses_same_field(self):
		# received_date is today-10 from _draft; keep later event dates in order.
		received = getdate(today()) - timedelta(days=10)
		sent = received + timedelta(days=1)
		bounced = sent + timedelta(days=1)
		assign = bounced + timedelta(days=1)
		self.assertNotEqual(assign, getdate(today()))

		doc = self._draft()
		doc = self._apply(doc, "Register Cheque")
		doc.reload()
		doc.sent_to_bank_date = sent
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = frappe.get_doc("Post Dated Cheque", doc.name)
		doc = self._apply(doc, "Send to Bank")
		doc.reload()
		doc.bounced_date = bounced
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = frappe.get_doc("Post Dated Cheque", doc.name)
		doc = self._apply(doc, "Bounce Cheque")
		doc.reload()
		self.assertEqual(normalize_workflow_state_value(doc.workflow_state), WORKFLOW_BOUNCED)

		doc.debt_purchase_assignment_date = assign
		doc.bank_account = self.bank_account
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = frappe.get_doc("Post Dated Cheque", doc.name)
		doc = self._apply(doc, "Assign for Debt Purchase")
		doc.reload()
		self.assertEqual(
			normalize_workflow_state_value(doc.workflow_state), WORKFLOW_ASSIGNED_DEBT_PURCHASE
		)
		refs = frappe.get_all(
			"PDC Journal Reference",
			filters={"parent": doc.name, "purpose": "Debt Purchase Assignment"},
			fields=["journal_entry", "posting_date", "pdc_transition_key"],
		)
		self.assertEqual(len(refs), 1, refs)
		self.assertEqual(getdate(refs[0].posting_date), assign)
		self.assertIn("Bounced|Assigned to Bank for Debt Purchase", refs[0].pdc_transition_key or "")


if __name__ == "__main__":
	unittest.main()
