# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.6 — Journal Reference immutability: create/edit/delete + authorized system ops."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.exceptions import ValidationError

from erpnext_extensions.cheque_management import pdc_journal_reference_guard as guard
from erpnext_extensions.cheque_management.accounting_rollback import erpnext_accounting


def _clear_auth_flags():
	frappe.flags.pdc_jr_authorized_mutation = None
	frappe.flags.pdc_jr_authorized_removal = None  # legacy name must not authorize
	frappe.flags.in_pdc_workflow_rollback = None


class TestJournalReferenceAuthFlag(unittest.TestCase):
	def tearDown(self):
		_clear_auth_flags()

	def test_client_style_empty_auth_rejected(self):
		self.assertFalse(
			guard.journal_reference_removal_authorized(pdc_name="PDC-1", row_name="jr1")
		)
		self.assertFalse(guard.journal_reference_creation_authorized(pdc_name="PDC-1"))

	def test_mismatched_pdc_rejected(self):
		with guard.authorize_journal_reference_removal("PDC-A", rows=["jr1"]):
			self.assertFalse(
				guard.journal_reference_removal_authorized(pdc_name="PDC-B", row_name="jr1")
			)
		with guard.authorize_journal_reference_creation("PDC-A"):
			self.assertFalse(guard.journal_reference_creation_authorized(pdc_name="PDC-B"))

	def test_scoped_row_authorized(self):
		with guard.authorize_journal_reference_removal("PDC-A", rows=["jr1"], transition_keys=["k1"]):
			self.assertTrue(
				guard.journal_reference_removal_authorized(
					pdc_name="PDC-A", row_name="jr1", transition_key="k1"
				)
			)
			self.assertFalse(
				guard.journal_reference_removal_authorized(
					pdc_name="PDC-A", row_name="jr2", transition_key="k1"
				)
			)

	def test_unrelated_transition_key_rejected(self):
		with guard.authorize_journal_reference_removal("PDC-A", rows=["jr1"], transition_keys=["k1"]):
			self.assertFalse(
				guard.journal_reference_removal_authorized(
					pdc_name="PDC-A", row_name="jr1", transition_key="k-other"
				)
			)

	def test_broad_payload_without_allow_any_rejected(self):
		frappe.flags.pdc_jr_authorized_mutation = {
			"pdc": "PDC-A",
			"allow_remove": True,
			"allow_create": False,
			"rows": [],
			"transition_keys": [],
		}
		self.assertFalse(
			guard.journal_reference_removal_authorized(pdc_name="PDC-A", row_name="jr1")
		)

	def test_invalid_non_dict_payload_ignored(self):
		frappe.flags.pdc_jr_authorized_mutation = "PDC-A"
		self.assertFalse(
			guard.journal_reference_removal_authorized(pdc_name="PDC-A", row_name="jr1")
		)
		self.assertFalse(guard.journal_reference_creation_authorized(pdc_name="PDC-A"))

	def test_legacy_removal_flag_name_does_not_authorize(self):
		frappe.flags.pdc_jr_authorized_removal = {
			"pdc": "PDC-A",
			"rows": ["jr1"],
			"allow_any_row_on_pdc": True,
		}
		self.assertFalse(
			guard.journal_reference_removal_authorized(pdc_name="PDC-A", row_name="jr1")
		)

	def test_create_auth_cleared_after_success(self):
		with guard.authorize_journal_reference_creation("PDC-A", transition_keys=["k1"]):
			self.assertTrue(
				guard.journal_reference_creation_authorized(pdc_name="PDC-A", transition_key="k1")
			)
		self.assertFalse(guard.journal_reference_creation_authorized(pdc_name="PDC-A", transition_key="k1"))

	def test_create_auth_cleared_after_failure(self):
		try:
			with guard.authorize_journal_reference_creation("PDC-A"):
				self.assertTrue(guard.journal_reference_creation_authorized(pdc_name="PDC-A"))
				raise RuntimeError("boom")
		except RuntimeError:
			pass
		self.assertIsNone(getattr(frappe.flags, "pdc_jr_authorized_mutation", None))
		self.assertFalse(guard.journal_reference_creation_authorized(pdc_name="PDC-A"))

	def test_removal_auth_does_not_allow_create(self):
		with guard.authorize_journal_reference_removal("PDC-A", rows=["jr1"]):
			self.assertFalse(guard.journal_reference_creation_authorized(pdc_name="PDC-A"))

	def test_create_auth_does_not_allow_remove(self):
		with guard.authorize_journal_reference_creation("PDC-A"):
			self.assertFalse(
				guard.journal_reference_removal_authorized(pdc_name="PDC-A", row_name="jr1")
			)


class TestValidateJournalReferencesImmutable(unittest.TestCase):
	def tearDown(self):
		_clear_auth_flags()

	def _doc(self, name, rows, *, is_new=False):
		return SimpleNamespace(
			name=name,
			is_new=lambda: is_new,
			get=lambda field, default=None: rows if field == "journal_references" else default,
			journal_references=rows,
		)

	def test_deletion_by_normal_user_rejected(self):
		prev = [
			SimpleNamespace(
				name="jr1",
				journal_entry="JE-1",
				purpose="Receive",
				pdc_transition_key="PDC-1|Receivable|Draft|Registered",
				posting_date="2026-01-01",
				amount=100,
			)
		]
		doc = self._doc("PDC-1", [])
		with patch.object(frappe, "get_all", return_value=prev):
			with self.assertRaises(ValidationError) as ctx:
				guard.validate_journal_references_immutable(doc)
		self.assertIn("system-managed", str(ctx.exception).lower())

	def test_manual_append_rejected(self):
		prev = [
			SimpleNamespace(
				name="jr1",
				journal_entry="JE-1",
				purpose="Receive",
				pdc_transition_key="k1",
				posting_date="2026-01-01",
				amount=100,
			)
		]
		keep = SimpleNamespace(
			name="jr1",
			journal_entry="JE-1",
			purpose="Receive",
			pdc_transition_key="k1",
			posting_date="2026-01-01",
			amount=100,
		)
		keep.get = lambda f, d=None: getattr(keep, f, d)
		new_row = SimpleNamespace(
			name=None,
			__islocal=1,
			journal_entry="JE-NEW",
			purpose="Collected",
			pdc_transition_key="k-new",
			posting_date="2026-01-02",
			amount=100,
		)
		new_row.get = lambda f, d=None: getattr(new_row, f, d)
		doc = self._doc("PDC-1", [keep, new_row])
		with patch.object(frappe, "get_all", return_value=prev):
			with self.assertRaises(ValidationError) as ctx:
				guard.validate_journal_references_immutable(doc)
		self.assertIn("creation", str(ctx.exception).lower())

	def test_manual_append_on_new_document_rejected(self):
		new_row = SimpleNamespace(
			name=None,
			__islocal=1,
			journal_entry="JE-NEW",
			purpose="Receive",
			pdc_transition_key="k",
			posting_date="2026-01-01",
			amount=1,
			parent="PDC-NEW",
		)
		new_row.get = lambda f, d=None: getattr(new_row, f, d)
		doc = self._doc("PDC-NEW", [new_row], is_new=True)
		with self.assertRaises(ValidationError):
			guard.validate_journal_references_immutable(doc)

	def test_system_append_authorized(self):
		prev = [
			SimpleNamespace(
				name="jr1",
				journal_entry="JE-1",
				purpose="Receive",
				pdc_transition_key="k1",
				posting_date="2026-01-01",
				amount=100,
			)
		]
		keep = SimpleNamespace(
			name="jr1",
			journal_entry="JE-1",
			purpose="Receive",
			pdc_transition_key="k1",
			posting_date="2026-01-01",
			amount=100,
		)
		keep.get = lambda f, d=None: getattr(keep, f, d)
		new_row = SimpleNamespace(
			name=None,
			__islocal=1,
			journal_entry="JE-2",
			purpose="Debt Purchase Assignment",
			pdc_transition_key="k2",
			posting_date="2026-01-02",
			amount=100,
		)
		new_row.get = lambda f, d=None: getattr(new_row, f, d)
		doc = self._doc("PDC-1", [keep, new_row])
		with patch.object(frappe, "get_all", return_value=prev):
			with guard.authorize_journal_reference_creation("PDC-1", transition_keys=["k2"]):
				guard.validate_journal_references_immutable(doc)

	def test_modification_of_journal_entry_rejected(self):
		prev = [
			SimpleNamespace(
				name="jr1",
				journal_entry="JE-1",
				purpose="Receive",
				pdc_transition_key="k",
				posting_date="2026-01-01",
				amount=100,
			)
		]
		row = SimpleNamespace(
			name="jr1",
			journal_entry="JE-HACKED",
			purpose="Receive",
			pdc_transition_key="k",
			posting_date="2026-01-01",
			amount=100,
		)
		row.get = lambda f, d=None: getattr(row, f, d)
		doc = self._doc("PDC-1", [row])
		with patch.object(frappe, "get_all", return_value=prev):
			with self.assertRaises(ValidationError):
				guard.validate_journal_references_immutable(doc)

	def test_modification_of_transition_key_rejected(self):
		prev = [
			SimpleNamespace(
				name="jr1",
				journal_entry="JE-1",
				purpose="Receive",
				pdc_transition_key="k1",
				posting_date="2026-01-01",
				amount=100,
			)
		]
		row = SimpleNamespace(
			name="jr1",
			journal_entry="JE-1",
			purpose="Receive",
			pdc_transition_key="k-hacked",
			posting_date="2026-01-01",
			amount=100,
		)
		row.get = lambda f, d=None: getattr(row, f, d)
		doc = self._doc("PDC-1", [row])
		with patch.object(frappe, "get_all", return_value=prev):
			with self.assertRaises(ValidationError):
				guard.validate_journal_references_immutable(doc)

	def test_modification_of_purpose_rejected(self):
		prev = [
			SimpleNamespace(
				name="jr1",
				journal_entry="JE-1",
				purpose="Receive",
				pdc_transition_key="k1",
				posting_date="2026-01-01",
				amount=100,
			)
		]
		row = SimpleNamespace(
			name="jr1",
			journal_entry="JE-1",
			purpose="Collected",
			pdc_transition_key="k1",
			posting_date="2026-01-01",
			amount=100,
		)
		row.get = lambda f, d=None: getattr(row, f, d)
		doc = self._doc("PDC-1", [row])
		with patch.object(frappe, "get_all", return_value=prev):
			with self.assertRaises(ValidationError):
				guard.validate_journal_references_immutable(doc)

	def test_authorized_rollback_can_remove_intended_reference(self):
		prev = [
			SimpleNamespace(
				name="jr-assign",
				journal_entry="JE-A",
				purpose="Debt Purchase Assignment",
				pdc_transition_key="k-assign",
				posting_date="2026-01-01",
				amount=100,
			),
			SimpleNamespace(
				name="jr-settle",
				journal_entry="JE-S",
				purpose="Debt Purchase Settlement",
				pdc_transition_key="k-settle",
				posting_date="2026-01-02",
				amount=100,
			),
		]
		keep = SimpleNamespace(
			name="jr-assign",
			journal_entry="JE-A",
			purpose="Debt Purchase Assignment",
			pdc_transition_key="k-assign",
			posting_date="2026-01-01",
			amount=100,
		)
		keep.get = lambda f, d=None: getattr(keep, f, d)
		doc = self._doc("PDC-1", [keep])
		with patch.object(frappe, "get_all", return_value=prev):
			with guard.authorize_journal_reference_removal(
				"PDC-1", rows=["jr-settle"], transition_keys=["k-settle"]
			):
				guard.validate_journal_references_immutable(doc)

	def test_authorized_rollback_cannot_remove_unrelated_reference(self):
		prev = [
			SimpleNamespace(
				name="jr-assign",
				journal_entry="JE-A",
				purpose="Debt Purchase Assignment",
				pdc_transition_key="k-assign",
				posting_date="2026-01-01",
				amount=100,
			),
			SimpleNamespace(
				name="jr-settle",
				journal_entry="JE-S",
				purpose="Debt Purchase Settlement",
				pdc_transition_key="k-settle",
				posting_date="2026-01-02",
				amount=100,
			),
		]
		keep = SimpleNamespace(
			name="jr-settle",
			journal_entry="JE-S",
			purpose="Debt Purchase Settlement",
			pdc_transition_key="k-settle",
			posting_date="2026-01-02",
			amount=100,
		)
		keep.get = lambda f, d=None: getattr(keep, f, d)
		doc = self._doc("PDC-1", [keep])
		with patch.object(frappe, "get_all", return_value=prev):
			with guard.authorize_journal_reference_removal(
				"PDC-1", rows=["jr-settle"], transition_keys=["k-settle"]
			):
				with self.assertRaises(ValidationError):
					guard.validate_journal_references_immutable(doc)

	def test_assignment_remains_when_settlement_rolled_back(self):
		prev = [
			SimpleNamespace(
				name="jr-assign",
				journal_entry="JE-A",
				purpose="Debt Purchase Assignment",
				pdc_transition_key="k-assign",
				posting_date="2026-01-01",
				amount=100,
			),
			SimpleNamespace(
				name="jr-settle",
				journal_entry="JE-S",
				purpose="Debt Purchase Settlement",
				pdc_transition_key="k-settle",
				posting_date="2026-01-02",
				amount=100,
			),
		]
		keep = SimpleNamespace(
			name="jr-assign",
			journal_entry="JE-A",
			purpose="Debt Purchase Assignment",
			pdc_transition_key="k-assign",
			posting_date="2026-01-01",
			amount=100,
		)
		keep.get = lambda f, d=None: getattr(keep, f, d)
		doc = self._doc("PDC-1", [keep])
		with patch.object(frappe, "get_all", return_value=prev):
			with guard.authorize_journal_reference_removal(
				"PDC-1", rows=["jr-settle"], transition_keys=["k-settle"]
			):
				guard.validate_journal_references_immutable(doc)
		self.assertEqual(keep.name, "jr-assign")

	def test_facility_settlement_create_authorized(self):
		"""Facility Debt Purchase settlement append is a create, not a remove."""
		prev = [
			SimpleNamespace(
				name="jr-assign",
				journal_entry="JE-A",
				purpose="Debt Purchase Assignment",
				pdc_transition_key="k-assign",
				posting_date="2026-01-01",
				amount=100,
			)
		]
		keep = SimpleNamespace(
			name="jr-assign",
			journal_entry="JE-A",
			purpose="Debt Purchase Assignment",
			pdc_transition_key="k-assign",
			posting_date="2026-01-01",
			amount=100,
		)
		keep.get = lambda f, d=None: getattr(keep, f, d)
		settle = SimpleNamespace(
			name=None,
			__islocal=1,
			journal_entry="JE-S",
			purpose="Debt Purchase Settlement",
			pdc_transition_key="k-settle",
			posting_date="2026-01-02",
			amount=100,
		)
		settle.get = lambda f, d=None: getattr(settle, f, d)
		doc = self._doc("PDC-1", [keep, settle])
		with patch.object(frappe, "get_all", return_value=prev):
			with guard.authorize_journal_reference_creation("PDC-1", transition_keys=["k-settle"]):
				guard.validate_journal_references_immutable(doc)


class TestChildTrashAndFieldEdit(unittest.TestCase):
	def tearDown(self):
		_clear_auth_flags()

	def test_on_trash_rejected_without_auth(self):
		row = SimpleNamespace(parent="PDC-1", name="jr1", pdc_transition_key="k1")
		with self.assertRaises(ValidationError):
			guard.reject_unauthorized_journal_reference_trash(row)

	def test_on_trash_allowed_with_auth(self):
		row = SimpleNamespace(parent="PDC-1", name="jr1", pdc_transition_key="k1")
		with guard.authorize_journal_reference_removal("PDC-1", rows=["jr1"], transition_keys=["k1"]):
			guard.reject_unauthorized_journal_reference_trash(row)

	def test_insert_rejected_without_auth(self):
		row = SimpleNamespace(parent="PDC-1", name="new", pdc_transition_key="k1")
		with self.assertRaises(ValidationError):
			guard.reject_unauthorized_journal_reference_insert(row)

	def test_insert_allowed_with_create_auth(self):
		row = SimpleNamespace(parent="PDC-1", name="new", pdc_transition_key="k1")
		with guard.authorize_journal_reference_creation("PDC-1", transition_keys=["k1"]):
			guard.reject_unauthorized_journal_reference_insert(row)

	def test_field_edit_rejected(self):
		row = SimpleNamespace(
			name="jr1",
			is_new=lambda: False,
			journal_entry="JE-NEW",
			purpose="Receive",
			pdc_transition_key="k",
			posting_date="2026-01-01",
			amount=1,
		)
		row.get = lambda f, d=None: getattr(row, f, d)
		with patch.object(
			frappe.db,
			"get_value",
			return_value={
				"journal_entry": "JE-OLD",
				"purpose": "Receive",
				"pdc_transition_key": "k",
				"posting_date": "2026-01-01",
				"amount": 1,
			},
		):
			with self.assertRaises(ValidationError):
				guard.reject_unauthorized_journal_reference_field_edit(row)


class TestRollbackHelperUsesAuth(unittest.TestCase):
	def tearDown(self):
		_clear_auth_flags()

	def test_rollback_journal_reference_row_sets_auth(self):
		seen = {}

		def fake_delete(doctype, name, **kwargs):
			auth = getattr(frappe.flags, "pdc_jr_authorized_mutation", None)
			seen["auth"] = dict(auth) if isinstance(auth, dict) else auth
			seen["name"] = name

		with patch.object(frappe.db, "exists", return_value=True), patch.object(
			frappe.db,
			"get_value",
			return_value=SimpleNamespace(parent="PDC-1", pdc_transition_key="k1"),
		), patch.object(frappe, "delete_doc", side_effect=fake_delete):
			erpnext_accounting.rollback_journal_reference_row("jr1")
		self.assertEqual(seen["name"], "jr1")
		self.assertEqual(seen["auth"]["pdc"], "PDC-1")
		self.assertTrue(seen["auth"]["allow_remove"])
		self.assertFalse(seen["auth"]["allow_create"])
		self.assertIn("jr1", seen["auth"]["rows"])
		self.assertIsNone(getattr(frappe.flags, "pdc_jr_authorized_mutation", None))

	def test_cancel_failure_leaves_reference(self):
		step = SimpleNamespace(journal_entry="JE-1", journal_reference_row="jr1", transition_key="k1")
		with patch.object(
			erpnext_accounting,
			"resolve_journal_reference_row_for_rollback",
			return_value="jr1",
		), patch.object(
			erpnext_accounting, "rollback_journal_reference_row"
		) as remove_mock, patch.object(
			erpnext_accounting,
			"cancel_journal_entry_voucher",
			side_effect=RuntimeError("simulated cancel failure"),
		):
			from erpnext_extensions.cheque_management.accounting_rollback.transitions import (
				JournalEntryTransitionHandler,
			)

			handler = JournalEntryTransitionHandler()
			with self.assertRaises(RuntimeError):
				handler.rollback_accounting(step, SimpleNamespace(name="PDC-1"), dry_run=False)
			remove_mock.assert_called_once_with("jr1")
		self.assertIsNone(getattr(frappe.flags, "pdc_jr_authorized_mutation", None))


class TestSystemCreateWiring(unittest.TestCase):
	def test_journal_entry_service_imports_create_auth(self):
		import inspect

		from erpnext_extensions.cheque_management import pdc_journal_entry_service as jes

		src = inspect.getsource(jes)
		self.assertIn("authorize_journal_reference_creation", src)

	def test_facility_settlement_append_imports_create_auth(self):
		import inspect

		from erpnext_extensions.facility_management import facility_debt_purchase as fdp

		src = inspect.getsource(fdp.append_pdc_settlement_journal_reference)
		self.assertIn("authorize_journal_reference_creation", src)

	def test_post_dated_cheque_validate_imports_guard(self):
		import inspect

		from erpnext_extensions.cheque_management.doctype.post_dated_cheque import post_dated_cheque as mod

		src = inspect.getsource(mod.PostDatedCheque.validate)
		self.assertIn("validate_journal_references_immutable", src)

	def test_child_doctype_has_before_insert(self):
		import inspect

		from erpnext_extensions.cheque_management.doctype.pdc_journal_reference.pdc_journal_reference import (
			PDCJournalReference,
		)

		src = inspect.getsource(PDCJournalReference)
		self.assertIn("before_insert", src)
		self.assertIn("reject_unauthorized_journal_reference_insert", src)


if __name__ == "__main__":
	unittest.main()
