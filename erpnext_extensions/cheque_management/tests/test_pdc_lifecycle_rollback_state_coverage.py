# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Isolated lifecycle rollback coverage for Endorsed, Bounce, ULA, Replaced, Payable.

Uses unittest.TestCase (not FrappeTestCase) to avoid ERPNext UOM fixture preload
collisions on development sites where ``tabSeries`` lags existing UOM Conversion Factor names.
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import getdate, random_string, today

from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
	_get_pdc_settings_for_company,
	get_default_party_accounts,
)
from erpnext_extensions.cheque_management.pdc_lifecycle_events import load_active_lifecycle_events
from erpnext_extensions.cheque_management.pdc_workflow_rollback import (
	get_rollback_target_states,
	rollback_workflow_state,
)
from erpnext_extensions.cheque_management.pdc_workflow_rollback_permission import user_may_rollback_pdc
from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	WORKFLOW_BOUNCED,
	WORKFLOW_CANCELLED,
	WORKFLOW_CLEARED,
	WORKFLOW_DRAFT,
	WORKFLOW_ENDORSED,
	WORKFLOW_ISSUED,
	WORKFLOW_REGISTERED,
	WORKFLOW_REPLACED,
	WORKFLOW_RETURNED,
	WORKFLOW_SENT_TO_BANK,
	WORKFLOW_UNDER_LEGAL_ACTION,
	normalize_workflow_state_value,
)
from erpnext_extensions.cheque_management.pdc_workflow_to_cheque_status import (
	map_workflow_state_to_cheque_status,
)
from erpnext_extensions.facility_management.facility_e2e_context import ensure_bank_master


def _pick_company() -> str:
	row = frappe.db.sql(
		"""
		SELECT c.name
		FROM `tabCompany` c
		WHERE c.name NOT LIKE '\\_Test%%'
		  AND EXISTS (SELECT 1 FROM `tabAccount` a WHERE a.company = c.name AND a.is_group = 0 LIMIT 1)
		ORDER BY c.creation ASC
		LIMIT 1
		""",
		as_list=True,
	)
	if row:
		return row[0][0]
	company = frappe.db.get_value("Company", {}, "name", order_by="creation asc")
	if not company:
		frappe.throw("No Company")
	return company


def _refs(pdc_name: str, *, purpose: str | None = None) -> list[dict]:
	filters: dict = {"parent": pdc_name, "parenttype": "Post Dated Cheque"}
	if purpose:
		filters["purpose"] = purpose
	return frappe.get_all(
		"PDC Journal Reference",
		filters=filters,
		fields=["name", "journal_entry", "purpose", "pdc_transition_key"],
		order_by="idx asc",
	)


def _je_docstatus(je: str) -> int:
	return int(frappe.db.get_value("Journal Entry", je, "docstatus") or 0)


class _PDCLifecycleRollbackFixture(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.company = _pick_company()
		cls.customer = frappe.db.get_value("Customer", {"disabled": 0}, "name", order_by="modified desc")
		if not cls.customer:
			raise unittest.SkipTest("No Customer")
		cls.bank_account = frappe.db.get_value(
			"Bank Account",
			{"company": cls.company, "disabled": 0},
			"name",
			order_by="modified desc",
		)
		if not cls.bank_account:
			raise unittest.SkipTest("No Bank Account")
		cls.settings = _get_pdc_settings_for_company(cls.company)
		if not cls.settings or not cls.settings.default_cheques_in_hand_account:
			raise unittest.SkipTest("PDC Settings / Cheques in Hand missing")
		# Endorsement requires allow_endorsement.
		if not int(getattr(cls.settings, "allow_endorsement", 0) or 0):
			cls.settings.allow_endorsement = 1
			cls.settings.save(ignore_permissions=True)
			frappe.db.commit()
		if not (cls.settings.default_protested_account or "").strip():
			raise unittest.SkipTest("PDC Settings Default Protested Account required for Bounce")

	def _apply(self, doc, action: str):
		from frappe.model.workflow import apply_workflow

		return apply_workflow(doc, action)

	def _new_receivable_draft(self, *, amount: float = 1000.0, prefix: str = "LC-RB"):
		acc = get_default_party_accounts("Customer", self.customer, self.company, "Receivable") or {}
		doc = frappe.new_doc("Post Dated Cheque")
		doc.cheque_direction = "Receivable"
		doc.company = self.company
		doc.party_type = "Customer"
		doc.party = self.customer
		doc.cheque_no = f"{prefix}-{random_string(8)}"
		doc.cheque_due_date = getdate(today()) + timedelta(days=30)
		doc.cheque_amount = amount
		doc.received_date = today()
		drawer_bank = frappe.db.get_value("Bank", {}, "name", order_by="modified desc") or ensure_bank_master()
		doc.drawer_bank_name = drawer_bank
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

	def _register(self, doc):
		doc = self._apply(doc, "Register Cheque")
		doc.reload()
		self.assertEqual(int(doc.docstatus or 0), 1)
		self.assertEqual(normalize_workflow_state_value(doc.workflow_state), WORKFLOW_REGISTERED)
		frappe.db.commit()
		return doc

	def _assert_status_matches(self, pdc_name: str):
		doc = frappe.get_doc("Post Dated Cheque", pdc_name)
		expected = map_workflow_state_to_cheque_status(doc.cheque_direction, doc.workflow_state)
		self.assertEqual(doc.cheque_status, expected)


class TestBounceAndMultiHopRollback(_PDCLifecycleRollbackFixture):
	def test_bounce_rollback_to_sent_preserves_receive_and_send(self):
		doc = self._register(self._new_receivable_draft(amount=1400.0, prefix="LC-BN"))
		receive_je = _refs(doc.name, purpose="Receive")[0]["journal_entry"]

		doc.sent_to_bank_date = today()
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = self._apply(frappe.get_doc("Post Dated Cheque", doc.name), "Send to Bank")
		doc.reload()
		self.assertEqual(normalize_workflow_state_value(doc.workflow_state), WORKFLOW_SENT_TO_BANK)
		send_refs = [
			r
			for r in _refs(doc.name)
			if r.get("pdc_transition_key") and "|Registered|Sent to Bank" in (r.get("pdc_transition_key") or "")
		]
		self.assertEqual(len(send_refs), 1, send_refs)
		send_je = send_refs[0]["journal_entry"]
		frappe.db.commit()

		doc.bounced_date = today()
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = self._apply(frappe.get_doc("Post Dated Cheque", doc.name), "Bounce Cheque")
		doc.reload()
		self.assertEqual(normalize_workflow_state_value(doc.workflow_state), WORKFLOW_BOUNCED)
		frappe.db.commit()

		bounce_refs = [
			r
			for r in _refs(doc.name)
			if r.get("pdc_transition_key") and "|Sent to Bank|Bounced" in (r.get("pdc_transition_key") or "")
		]
		self.assertEqual(len(bounce_refs), 1, bounce_refs)
		bounce_je = bounce_refs[0]["journal_entry"]

		targets = get_rollback_target_states(doc.name)
		self.assertIn(WORKFLOW_SENT_TO_BANK, targets)
		self.assertIn(WORKFLOW_REGISTERED, targets)
		self.assertIn(WORKFLOW_DRAFT, targets)

		out = rollback_workflow_state(doc.name, WORKFLOW_SENT_TO_BANK, "E2E bounce → Sent to Bank")
		frappe.db.commit()
		self.assertEqual(out.get("workflow_state"), WORKFLOW_SENT_TO_BANK)
		self.assertEqual(_je_docstatus(bounce_je), 2)
		self.assertEqual(_je_docstatus(send_je), 1)
		self.assertEqual(_je_docstatus(receive_je), 1)
		self.assertEqual(len(_refs(doc.name, purpose="Receive")), 1)
		self._assert_status_matches(doc.name)

		# Multi-hop from Sent to Draft undoes send + receive.
		out = rollback_workflow_state(doc.name, WORKFLOW_DRAFT, "E2E Sent → Draft after bounce undo")
		frappe.db.commit()
		self.assertEqual(out.get("workflow_state"), WORKFLOW_DRAFT)
		doc.reload()
		self.assertEqual(int(doc.docstatus or 0), 0)
		self.assertEqual(_je_docstatus(send_je), 2)
		self.assertEqual(_je_docstatus(receive_je), 2)
		self.assertEqual(get_rollback_target_states(doc.name), [])

	def test_bounce_rollback_je_failure_keeps_bounced(self):
		doc = self._register(self._new_receivable_draft(amount=950.0, prefix="LC-BF"))
		doc.sent_to_bank_date = today()
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = self._apply(frappe.get_doc("Post Dated Cheque", doc.name), "Send to Bank")
		doc.reload()
		doc.bounced_date = today()
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = self._apply(frappe.get_doc("Post Dated Cheque", doc.name), "Bounce Cheque")
		doc.reload()
		frappe.db.commit()
		bounce_je = [
			r["journal_entry"]
			for r in _refs(doc.name)
			if r.get("pdc_transition_key") and "|Sent to Bank|Bounced" in (r.get("pdc_transition_key") or "")
		][0]

		with patch(
			"erpnext_extensions.cheque_management.accounting_rollback.erpnext_accounting.cancel_journal_entry_voucher",
			side_effect=frappe.ValidationError("forced bounce JE cancel failure"),
		):
			with self.assertRaises(frappe.ValidationError):
				rollback_workflow_state(doc.name, WORKFLOW_SENT_TO_BANK, "must fail atomically")
		frappe.db.rollback()

		doc.reload()
		self.assertEqual(normalize_workflow_state_value(doc.workflow_state), WORKFLOW_BOUNCED)
		self.assertEqual(_je_docstatus(bounce_je), 1)
		self.assertTrue(
			any(
				"|Sent to Bank|Bounced" in (r.get("pdc_transition_key") or "")
				for r in _refs(doc.name)
			)
		)


class TestEndorsedAndULARollback(_PDCLifecycleRollbackFixture):
	def _ensure_endorsement_settlement_gl(self) -> str:
		"""Non-party Asset leaf — Payable settlement GLs require Party on JE rows."""
		parent = frappe.db.get_value(
			"Account",
			{"company": self.company, "is_group": 1, "root_type": "Asset"},
			"name",
			order_by="lft asc",
		)
		acc_name = "E2E Endorsement Settlement"
		exists = frappe.db.get_value("Account", {"company": self.company, "account_name": acc_name}, "name")
		if exists:
			return exists
		acc = frappe.new_doc("Account")
		acc.company = self.company
		acc.parent_account = parent
		acc.account_name = acc_name
		acc.is_group = 0
		acc.account_type = ""
		acc.insert(ignore_permissions=True)
		return acc.name

	def test_endorsed_rollback_to_registered_preserves_receive(self):
		# Distinct endorsee customer when available.
		endorsee = frappe.db.get_value(
			"Customer",
			{"disabled": 0, "name": ["!=", self.customer]},
			"name",
			order_by="modified desc",
		) or self.customer

		doc = self._register(self._new_receivable_draft(amount=1600.0, prefix="LC-EN"))
		receive_je = _refs(doc.name, purpose="Receive")[0]["journal_entry"]
		doc.holder_party_type = "Customer"
		doc.holder_party = endorsee
		doc.handover_date = today()
		doc.endorsement_settlement_account = self._ensure_endorsement_settlement_gl()
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		doc = self._apply(frappe.get_doc("Post Dated Cheque", doc.name), "Endorse")
		doc.reload()
		self.assertEqual(normalize_workflow_state_value(doc.workflow_state), WORKFLOW_ENDORSED)
		frappe.db.commit()

		endorse_refs = [
			r
			for r in _refs(doc.name)
			if r.get("pdc_transition_key") and "|Registered|Endorsed" in (r.get("pdc_transition_key") or "")
		]
		self.assertEqual(len(endorse_refs), 1, endorse_refs)
		endorse_je = endorse_refs[0]["journal_entry"]

		targets = get_rollback_target_states(doc.name)
		self.assertIn(WORKFLOW_REGISTERED, targets)
		self.assertIn(WORKFLOW_DRAFT, targets)

		out = rollback_workflow_state(doc.name, WORKFLOW_REGISTERED, "E2E Endorsed → Registered")
		frappe.db.commit()
		self.assertEqual(out.get("workflow_state"), WORKFLOW_REGISTERED)
		self.assertEqual(_je_docstatus(endorse_je), 2)
		self.assertEqual(_je_docstatus(receive_je), 1)
		self.assertEqual(len(_refs(doc.name, purpose="Receive")), 1)
		self._assert_status_matches(doc.name)
		active = load_active_lifecycle_events(doc.name)
		self.assertEqual(normalize_workflow_state_value(active[-1].get("to_state")), WORKFLOW_REGISTERED)

	def test_under_legal_action_rollback_to_bounced(self):
		doc = self._register(self._new_receivable_draft(amount=1100.0, prefix="LC-UL"))
		doc.sent_to_bank_date = today()
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = self._apply(frappe.get_doc("Post Dated Cheque", doc.name), "Send to Bank")
		doc.reload()
		doc.bounced_date = today()
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = self._apply(frappe.get_doc("Post Dated Cheque", doc.name), "Bounce Cheque")
		doc.reload()
		frappe.db.commit()
		bounce_je = [
			r["journal_entry"]
			for r in _refs(doc.name)
			if r.get("pdc_transition_key") and "|Sent to Bank|Bounced" in (r.get("pdc_transition_key") or "")
		][0]

		doc = self._apply(frappe.get_doc("Post Dated Cheque", doc.name), "Escalate Legal")
		doc.reload()
		self.assertEqual(
			normalize_workflow_state_value(doc.workflow_state), WORKFLOW_UNDER_LEGAL_ACTION
		)
		frappe.db.commit()

		targets = get_rollback_target_states(doc.name)
		self.assertIn(WORKFLOW_BOUNCED, targets)

		out = rollback_workflow_state(doc.name, WORKFLOW_BOUNCED, "E2E ULA → Bounced")
		frappe.db.commit()
		self.assertEqual(out.get("workflow_state"), WORKFLOW_BOUNCED)
		# ULA edge is workflow-only — bounce JE remains submitted.
		self.assertEqual(_je_docstatus(bounce_je), 1)
		self._assert_status_matches(doc.name)


class TestReturnedToReplacedRollback(_PDCLifecycleRollbackFixture):
	def test_returned_to_replaced_posts_je_and_rollback_preserves_return(self):
		"""Returned → Replaced is a valid Desk + JE path; rollback to Returned undoes only replace."""
		replacement = self._register(self._new_receivable_draft(amount=40.0, prefix="LC-RRX"))
		doc = self._register(self._new_receivable_draft(amount=1180.0, prefix="LC-RTR"))
		receive_je = _refs(doc.name, purpose="Receive")[0]["journal_entry"]

		doc.returned_date = today()
		doc.return_reason = "E2E return before replace"
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = self._apply(frappe.get_doc("Post Dated Cheque", doc.name), "Return Cheque")
		doc.reload()
		self.assertEqual(normalize_workflow_state_value(doc.workflow_state), WORKFLOW_RETURNED)
		frappe.db.commit()

		return_refs = [
			r
			for r in _refs(doc.name)
			if r.get("pdc_transition_key") and "|Registered|Returned" in (r.get("pdc_transition_key") or "")
		]
		self.assertEqual(len(return_refs), 1, return_refs)
		return_je = return_refs[0]["journal_entry"]
		self.assertEqual(_je_docstatus(return_je), 1)

		doc.replaced_by = replacement.name
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = self._apply(frappe.get_doc("Post Dated Cheque", doc.name), "Replace Cheque")
		doc.reload()
		self.assertEqual(normalize_workflow_state_value(doc.workflow_state), WORKFLOW_REPLACED)
		frappe.db.commit()

		replace_refs = [
			r
			for r in _refs(doc.name)
			if r.get("pdc_transition_key") and "|Returned|Replaced" in (r.get("pdc_transition_key") or "")
		]
		self.assertEqual(len(replace_refs), 1, replace_refs)
		replace_je = replace_refs[0]["journal_entry"]
		self.assertEqual(_je_docstatus(replace_je), 1)
		self.assertNotEqual(replace_je, return_je)
		self.assertNotEqual(replace_je, receive_je)

		targets = get_rollback_target_states(doc.name)
		self.assertIn(WORKFLOW_RETURNED, targets)
		self.assertIn(WORKFLOW_REGISTERED, targets)
		self.assertIn(WORKFLOW_DRAFT, targets)

		out = rollback_workflow_state(doc.name, WORKFLOW_RETURNED, "E2E Replaced → Returned")
		frappe.db.commit()
		self.assertEqual(out.get("workflow_state"), WORKFLOW_RETURNED)
		self.assertEqual(_je_docstatus(replace_je), 2)
		self.assertEqual(_je_docstatus(return_je), 1)
		self.assertEqual(_je_docstatus(receive_je), 1)
		self.assertEqual(len(_refs(doc.name, purpose="Receive")), 1)
		self.assertTrue(
			any("|Registered|Returned" in (r.get("pdc_transition_key") or "") for r in _refs(doc.name))
		)
		self.assertFalse(
			any("|Returned|Replaced" in (r.get("pdc_transition_key") or "") for r in _refs(doc.name))
		)
		self._assert_status_matches(doc.name)

		# Unrelated replacement PDC remains Registered / untouched.
		rep_doc = frappe.get_doc("Post Dated Cheque", replacement.name)
		self.assertEqual(normalize_workflow_state_value(rep_doc.workflow_state), WORKFLOW_REGISTERED)
		self.assertEqual(_je_docstatus(_refs(replacement.name, purpose="Receive")[0]["journal_entry"]), 1)


class TestTerminalAndPermissionRollback(_PDCLifecycleRollbackFixture):
	def test_replaced_exposes_history_targets_and_can_undo_replace(self):
		"""Replaced is Desk-terminal for forward actions but lifecycle rollback may undo replace."""
		replacement = self._register(self._new_receivable_draft(amount=50.0, prefix="LC-RX"))
		doc = self._register(self._new_receivable_draft(amount=1050.0, prefix="LC-RP"))
		receive_je = _refs(doc.name, purpose="Receive")[0]["journal_entry"]
		# Required by Post Dated Cheque validation when entering Replaced.
		doc.replaced_by = replacement.name
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = self._apply(frappe.get_doc("Post Dated Cheque", doc.name), "Replace Cheque")
		doc.reload()
		self.assertEqual(normalize_workflow_state_value(doc.workflow_state), WORKFLOW_REPLACED)
		frappe.db.commit()

		targets = get_rollback_target_states(doc.name)
		self.assertIn(WORKFLOW_REGISTERED, targets)

		replace_refs = [
			r
			for r in _refs(doc.name)
			if r.get("pdc_transition_key") and "|Registered|Replaced" in (r.get("pdc_transition_key") or "")
		]
		# Registered→Replaced is no_document; receive JE must remain.
		out = rollback_workflow_state(doc.name, WORKFLOW_REGISTERED, "E2E Replaced → Registered")
		frappe.db.commit()
		self.assertEqual(out.get("workflow_state"), WORKFLOW_REGISTERED)
		self.assertEqual(_je_docstatus(receive_je), 1)
		self.assertEqual(len(_refs(doc.name, purpose="Receive")), 1)
		for r in replace_refs:
			if r.get("journal_entry"):
				self.assertEqual(_je_docstatus(r["journal_entry"]), 2)
		self._assert_status_matches(doc.name)

	def test_cancelled_has_no_forward_desk_path_and_forced_state_blocks_or_is_empty(self):
		"""Cancelled is not a normal Desk destination; forced Settled-like gate is empty/invalid."""
		doc = self._register(self._new_receivable_draft(amount=800.0, prefix="LC-CN"))
		frappe.db.set_value(
			"Post Dated Cheque",
			doc.name,
			"workflow_state",
			WORKFLOW_CANCELLED,
			update_modified=False,
		)
		frappe.db.commit()
		# Lifecycle last_to (Registered) != current Cancelled → event_history returns [].
		self.assertEqual(get_rollback_target_states(doc.name), [])
		with self.assertRaises(Exception):
			rollback_workflow_state(doc.name, WORKFLOW_REGISTERED, "cancelled must not rollback")
		frappe.db.rollback()
		frappe.db.set_value(
			"Post Dated Cheque",
			doc.name,
			"workflow_state",
			WORKFLOW_REGISTERED,
			update_modified=False,
		)
		frappe.db.commit()

	def test_cleared_is_rollbackable_not_desk_forward(self):
		doc = self._register(self._new_receivable_draft(amount=1250.0, prefix="LC-CL"))
		receive_je = _refs(doc.name, purpose="Receive")[0]["journal_entry"]
		doc.cleared_date = today()
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = self._apply(frappe.get_doc("Post Dated Cheque", doc.name), "Clear Cheque")
		doc.reload()
		self.assertEqual(normalize_workflow_state_value(doc.workflow_state), WORKFLOW_CLEARED)
		frappe.db.commit()

		targets = get_rollback_target_states(doc.name)
		self.assertIn(WORKFLOW_REGISTERED, targets)
		clear_refs = [
			r
			for r in _refs(doc.name)
			if r.get("pdc_transition_key") and "|Registered|Cleared" in (r.get("pdc_transition_key") or "")
		]
		self.assertEqual(len(clear_refs), 1, clear_refs)
		clear_je = clear_refs[0]["journal_entry"]

		out = rollback_workflow_state(doc.name, WORKFLOW_REGISTERED, "E2E Cleared → Registered")
		frappe.db.commit()
		self.assertEqual(out.get("workflow_state"), WORKFLOW_REGISTERED)
		self.assertEqual(_je_docstatus(clear_je), 2)
		self.assertEqual(_je_docstatus(receive_je), 1)

	def test_unauthorized_user_denied_and_idempotent_reject_after_done(self):
		self.assertTrue(user_may_rollback_pdc())
		from erpnext_extensions.cheque_management.pdc_workflow_rollback import (
			get_pdc_rollback_target_states,
		)

		with patch(
			"erpnext_extensions.cheque_management.pdc_workflow_rollback.user_may_rollback_pdc_workflow",
			return_value=False,
		):
			with self.assertRaises(Exception):
				get_pdc_rollback_target_states("PDC-PERM-CHECK")

		doc = self._register(self._new_receivable_draft(amount=700.0, prefix="LC-ID"))
		# First rollback Registered → Draft succeeds.
		rollback_workflow_state(doc.name, WORKFLOW_DRAFT, "idempotency prep")
		frappe.db.commit()
		self.assertEqual(get_rollback_target_states(doc.name), [])
		with self.assertRaises(Exception):
			rollback_workflow_state(doc.name, WORKFLOW_DRAFT, "duplicate rollback must fail")
		frappe.db.rollback()


class TestPayableLifecycleRollback(_PDCLifecycleRollbackFixture):
	def _provision_leaf(self) -> str:
		import time

		start = (int(time.time() * 1000) % 900000) + 100000
		book = frappe.new_doc("Cheque Book")
		book.company = self.company
		book.bank_account = self.bank_account
		book.generation_mode = "prefix_plus_sequence"
		book.start_number = start
		book.end_number = start
		book.number_width = 6
		book.insert(ignore_permissions=True)
		book.generate_leaves()
		leaf = frappe.db.get_value("Cheque Leaf", {"cheque_book": book.name, "status": "Available"}, "name")
		if not leaf:
			raise unittest.SkipTest("Could not provision Cheque Leaf")
		return leaf

	def test_payable_cleared_to_issued_to_registered_preserves_unrelated(self):
		supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name", order_by="modified desc")
		if not supplier:
			raise unittest.SkipTest("No Supplier")
		pool = self.settings.default_payable_cheque_account
		if not pool:
			raise unittest.SkipTest("No default payable cheque account")
		acc = get_default_party_accounts("Supplier", supplier, self.company, "Payable") or {}
		ap = acc.get("account_paid_to") or acc.get("account_paid_from")
		if not ap:
			raise unittest.SkipTest("No supplier payable account")

		leaf = self._provision_leaf()
		cheque_no = frappe.db.get_value("Cheque Leaf", leaf, "cheque_number")
		doc = frappe.new_doc("Post Dated Cheque")
		doc.cheque_direction = "Payable"
		doc.company = self.company
		doc.party_type = "Supplier"
		doc.party = supplier
		doc.cheque_no = cheque_no
		doc.cheque_leaf = leaf
		doc.cheque_due_date = getdate(today()) + timedelta(days=20)
		doc.received_date = today()
		doc.cheque_amount = 1300
		doc.bank_account = self.bank_account
		doc.account_paid_from = pool
		doc.account_paid_to = ap
		doc.workflow_state = WORKFLOW_DRAFT
		doc.allocation_mode = "direct_settlement"
		doc.insert(ignore_permissions=True)
		frappe.db.commit()

		doc = self._apply(doc, "Register Cheque")
		doc.reload()
		reg_je = _refs(doc.name)[0]["journal_entry"]
		frappe.db.commit()

		doc.handover_date = today()
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = self._apply(frappe.get_doc("Post Dated Cheque", doc.name), "Issue Cheque")
		doc.reload()
		self.assertEqual(normalize_workflow_state_value(doc.workflow_state), WORKFLOW_ISSUED)
		frappe.db.commit()

		doc.cleared_date = today()
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc = self._apply(frappe.get_doc("Post Dated Cheque", doc.name), "Clear Cheque")
		doc.reload()
		self.assertEqual(normalize_workflow_state_value(doc.workflow_state), WORKFLOW_CLEARED)
		frappe.db.commit()
		clear_je = [
			r["journal_entry"]
			for r in _refs(doc.name)
			if r.get("pdc_transition_key") and "|Issued|Cleared" in (r.get("pdc_transition_key") or "")
		][0]

		targets = get_rollback_target_states(doc.name)
		self.assertIn(WORKFLOW_ISSUED, targets)
		self.assertIn(WORKFLOW_REGISTERED, targets)

		out = rollback_workflow_state(doc.name, WORKFLOW_ISSUED, "E2E Payable Cleared → Issued")
		frappe.db.commit()
		self.assertEqual(out.get("workflow_state"), WORKFLOW_ISSUED)
		self.assertEqual(_je_docstatus(clear_je), 2)
		self.assertEqual(_je_docstatus(reg_je), 1)

		out = rollback_workflow_state(doc.name, WORKFLOW_REGISTERED, "E2E Payable Issued → Registered")
		frappe.db.commit()
		self.assertEqual(out.get("workflow_state"), WORKFLOW_REGISTERED)
		self.assertEqual(_je_docstatus(reg_je), 1)
		self._assert_status_matches(doc.name)


if __name__ == "__main__":
	unittest.main()
