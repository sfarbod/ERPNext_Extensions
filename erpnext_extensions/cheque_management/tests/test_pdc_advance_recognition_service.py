from __future__ import annotations

import unittest
from contextlib import ExitStack, contextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import frappe

import erpnext_extensions.cheque_management.pdc_open_advance as pdc_adv
from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
	build_pdc_journal_entry_data,
)
from erpnext_extensions.cheque_management.pdc_allocation import ALLOCATION_MODE_ADVANCE
from erpnext_extensions.cheque_management.pdc_journal_entry_service import (
	create_and_submit_journal_entry_from_payload,
)
from erpnext_extensions.cheque_management.pdc_open_advance import get_pdc_open_advance_instrument
from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	CHEQUE_DIRECTION_PAYABLE,
	CHEQUE_DIRECTION_RECEIVABLE,
	WORKFLOW_DRAFT,
	WORKFLOW_ISSUED,
	WORKFLOW_REGISTERED,
)

POSTING = date(2026, 4, 2)


@contextmanager
def _no_filelock(*args, **kwargs):
	yield


class TestPDCAdvanceRecognitionService(unittest.TestCase):
	def test_recognition_payload_sets_flag_and_open_advance_reflects_it(self) -> None:
		"""End-to-end (stubbed) check: payload marker flips recognition_je_posted, Task 3 reports gross/open."""

		pdc_doc = SimpleNamespace(
			name="PDC-ADV-1",
			company="_TC",
			cheque_direction=CHEQUE_DIRECTION_RECEIVABLE,
			allocation_mode=ALLOCATION_MODE_ADVANCE,
			effective_stage_for_advance_recognition="register",
			recognition_je_posted=0,
			instrument_dead=0,
			instrument_dead_reason=None,
			cheque_amount=20000.0,
			cheque_no="CHK-A",
			cheque_due_date=None,
			account_paid_to="ACC-CIH",
			party_type="Customer",
			party="CUST-1",
			journal_references=[],
			reload=lambda: None,
			append=lambda field, row: pdc_doc.journal_references.append(row),
			flags=SimpleNamespace(),
			save=lambda **kw: None,
		)

		# Build recognition payload (uses company default advance received account).
		with (
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque._get_pdc_settings_for_company",
				return_value=SimpleNamespace(
					get=lambda k: {
						"default_cheques_in_hand_account": "ACC-CIH",
						"default_payable_cheque_account": "ACC-POOL",
					}.get(k)
				),
			),
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque._company_default_advance_received_account",
				return_value="ACC-ADV-REC",
			),
			patch(
				"erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque.frappe"
			) as mf,
		):
			mf._ = lambda s: s
			je_payload = build_pdc_journal_entry_data(pdc_doc, WORKFLOW_DRAFT, WORKFLOW_REGISTERED, POSTING)
		assert je_payload is not None
		self.assertEqual(int(je_payload.get("set_recognition_je_posted") or 0), 1)

		# Stub create_and_submit_journal_entry_from_payload to avoid DB; ensure flag flips.
		fake_je = SimpleNamespace(
			name="JV-1",
			append=lambda *a, **k: None,
			submit=lambda *a, **k: None,
			flags=SimpleNamespace(),
		)

		def _new_doc(dt):
			self.assertEqual(dt, "Journal Entry")
			# minimal fields assigned by service
			fake_je.accounts = []
			# Guardrail: our service must not call `save()` explicitly (submit does it).
			fake_je.save = lambda *a, **k: (_ for _ in ()).throw(AssertionError("Unexpected je.save() call"))
			return fake_je

		with ExitStack() as stack:
			stack.enter_context(
				patch("erpnext_extensions.cheque_management.pdc_journal_entry_service.filelock", _no_filelock)
			)
			stack.enter_context(
				patch(
					"erpnext_extensions.cheque_management.pdc_journal_entry_service.frappe.new_doc",
					side_effect=_new_doc,
				)
			)
			stack.enter_context(
				patch(
					"erpnext_extensions.cheque_management.pdc_journal_entry_service.frappe.utils.today",
					return_value="2026-04-02",
				)
			)
			stack.enter_context(
				patch(
					"erpnext_extensions.cheque_management.pdc_journal_entry_service.frappe.throw",
					side_effect=AssertionError,
				)
			)
			# Do not replace frappe.flags wholesale — it is a shared process object used by
			# ERPNext helpers (accounting dimensions / DocType cache). The JE service only
			# needs to set skip_pdc_allocation_capacity_validation_for_pdc on the real flags.
			stack.enter_context(
				patch(
					"erpnext_extensions.cheque_management.pdc_journal_entry_service.get_existing_journal_entry_for_transition",
					return_value=None,
				)
			)
			# The service calls pdc.reload internally.
			stack.enter_context(patch.object(pdc_doc, "reload", lambda: None))
			create_and_submit_journal_entry_from_payload(
				pdc_doc, je_payload, WORKFLOW_DRAFT, WORKFLOW_REGISTERED
			)
		self.assertEqual(int(pdc_doc.recognition_je_posted), 1)

		# Task 3 open advance should now show gross/open = cheque_amount (no applications).
		fake_db = SimpleNamespace(
			get_value=lambda *a, **k: {
				"name": "PDC-ADV-1",
				"allocation_mode": "advance",
				"cheque_amount": 20000.0,
				"recognition_je_posted": 1,
				"instrument_dead": 0,
				"instrument_dead_reason": None,
			},
			sql=lambda *a, **k: [],
			table_exists=lambda *a, **k: False,
		)
		fake_frappe = SimpleNamespace(
			db=fake_db, throw=lambda msg, *a, **k: (_ for _ in ()).throw(AssertionError(msg))
		)
		with (
			patch.object(pdc_adv, "frappe", fake_frappe),
			patch.object(pdc_adv, "_", lambda s: s),
		):
			out = get_pdc_open_advance_instrument("PDC-ADV-1")
		self.assertEqual(out["recognized_gross"], 20000.0)
		self.assertEqual(out["open_amount"], 20000.0)

	def test_duplicate_prevention_returns_existing_je(self) -> None:
		"""If transition is already posted, service returns existing JE and does not alter flags."""
		pdc_doc = SimpleNamespace(
			name="PDC-ADV-2",
			company="_TC",
			cheque_direction=CHEQUE_DIRECTION_PAYABLE,
			cheque_amount=1000.0,
			cheque_no=None,
			cheque_due_date=None,
			journal_references=[],
			reload=lambda: None,
		)
		payload = {
			"accounts": [{"account": "A", "debit_in_account_currency": 1.0}],
			"set_recognition_je_posted": 1,
		}
		with (
			patch("erpnext_extensions.cheque_management.pdc_journal_entry_service.filelock", _no_filelock),
			patch(
				"erpnext_extensions.cheque_management.pdc_journal_entry_service.get_existing_journal_entry_for_transition",
				return_value="JV-EXIST",
			),
		):
			je = create_and_submit_journal_entry_from_payload(
				pdc_doc, payload, WORKFLOW_REGISTERED, WORKFLOW_ISSUED
			)
		self.assertEqual(je, "JV-EXIST")
