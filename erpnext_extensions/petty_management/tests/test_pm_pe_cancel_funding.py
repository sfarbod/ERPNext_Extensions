# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.4.6: Payment Entry cancellation — PM Request funding recalculation.

Run::

	bench --site development.localhost run-tests \
		--module erpnext_extensions.petty_management.tests.test_pm_pe_cancel_funding \
		--skip-before-tests
"""

from __future__ import annotations

import json
import pathlib
import unittest

import frappe
from frappe.utils import cint, flt, today

from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.test_pm_request_multi_pe import (
	_create_funding_pe,
	_new_submitted_request,
	_require_site_ready,
	_sync_funding_fields,
)

_APPROVAL_FIELDS = ("manager_approver", "ceo_approver", "finance_approver")


def _row(name: str) -> dict:
	"""Read PM Request funding + lifecycle fields from DB."""
	fields = [
		"docstatus",
		"workflow_state",
		"status",
		"payment_status",
		"total_paid_amount",
		"remaining_to_pay",
		"payment_entry",
		"total_requested_amount",
	]
	meta = frappe.get_meta("PM Request")
	for f in _APPROVAL_FIELDS:
		if meta.has_field(f):
			fields.append(f)
	return frappe.db.get_value("PM Request", name, fields, as_dict=True)


def _tail_audit_log(n: int = 200) -> list[dict]:
	"""Read the last *n* lines of the petty_management audit log."""
	log_path = pathlib.Path(frappe.get_site_path("logs", "petty_management.log"))
	if not log_path.exists():
		return []
	lines = log_path.read_text().strip().splitlines()[-n:]
	events: list[dict] = []
	for line in lines:
		try:
			payload = json.loads(line.split(" : ", 1)[-1])
			events.append(payload)
		except (json.JSONDecodeError, IndexError):
			pass
	return events


class TestPECancelFunding(unittest.TestCase):
	"""PE cancellation must only recalculate funding — never touch workflow or docstatus."""

	def setUp(self):
		frappe.set_user("Administrator")
		_require_site_ready(self)

	# ── Scenario 1: cancel one PE in multi-payment ──────────────────────

	def test_cancel_one_pe_partial_funding(self):
		"""Requested=100k, PE1=40k cancelled, PE2=60k submitted → Partially Paid."""
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 100_000)
		ws_before = frappe.db.get_value("PM Request", req, "workflow_state")

		pe1 = _create_funding_pe(req, 40_000)
		pe2 = _create_funding_pe(req, 60_000)
		_sync_funding_fields(req)

		frappe.get_doc("Payment Entry", pe1).cancel()
		frappe.db.commit()

		r = _row(req)
		self.assertEqual(r.docstatus, 1)
		self.assertEqual(r.workflow_state, ws_before)
		self.assertAlmostEqual(flt(r.total_paid_amount), 60_000, places=2)
		self.assertAlmostEqual(flt(r.remaining_to_pay), 40_000, places=2)
		self.assertEqual(r.payment_status, "Partially Paid")
		self.assertEqual(r.status, "Partially Paid")
		self.assertEqual(r.payment_entry, pe2)
		self.assertEqual(cint(frappe.db.get_value("Payment Entry", pe1, "docstatus")), 2)

	# ── Scenario 2: cancel ALL PEs ──────────────────────────────────────

	def test_cancel_all_pes_not_paid(self):
		"""Cancel every PE → Not Paid, pointer cleared, workflow unchanged."""
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 50_000)
		ws_before = frappe.db.get_value("PM Request", req, "workflow_state")

		pe1 = _create_funding_pe(req, 20_000)
		pe2 = _create_funding_pe(req, 30_000)
		_sync_funding_fields(req)

		frappe.get_doc("Payment Entry", pe1).cancel()
		frappe.db.commit()
		frappe.get_doc("Payment Entry", pe2).cancel()
		frappe.db.commit()

		r = _row(req)
		self.assertEqual(r.docstatus, 1)
		self.assertEqual(r.workflow_state, ws_before)
		self.assertAlmostEqual(flt(r.total_paid_amount), 0, places=2)
		self.assertAlmostEqual(flt(r.remaining_to_pay), 50_000, places=2)
		self.assertEqual(r.payment_status, "Not Paid")
		self.assertIn(r.status, ("Waiting for Payment", "Not Paid"))
		self.assertFalse(r.payment_entry)

	# ── Scenario 3: workflow_state unchanged ─────────────────────────────

	def test_workflow_unchanged_after_cancel(self):
		"""Explicit check that workflow_state is identical before and after cancel."""
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 10_000)
		ws_before = frappe.db.get_value("PM Request", req, "workflow_state")

		pe = _create_funding_pe(req, 10_000)
		_sync_funding_fields(req)

		frappe.get_doc("Payment Entry", pe).cancel()
		frappe.db.commit()

		ws_after = frappe.db.get_value("PM Request", req, "workflow_state")
		self.assertEqual(ws_after, ws_before)

	# ── Scenario 4: PM Request docstatus stays 1 ────────────────────────

	def test_pm_request_docstatus_unchanged(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 10_000)

		pe = _create_funding_pe(req, 10_000)
		_sync_funding_fields(req)

		frappe.get_doc("Payment Entry", pe).cancel()
		frappe.db.commit()

		self.assertEqual(frappe.db.get_value("PM Request", req, "docstatus"), 1)

	# ── Scenario 5: Create PE available again after cancel ───────────────

	def test_create_pe_available_after_cancel(self):
		"""After cancelling a PE, remaining_to_pay > 0 and a new PE can be created."""
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 30_000)

		pe1 = _create_funding_pe(req, 30_000)
		_sync_funding_fields(req)

		r_before = _row(req)
		self.assertEqual(r_before.payment_status, "Paid")

		frappe.get_doc("Payment Entry", pe1).cancel()
		frappe.db.commit()

		r_after = _row(req)
		self.assertAlmostEqual(flt(r_after.remaining_to_pay), 30_000, places=2)
		self.assertEqual(r_after.payment_status, "Not Paid")

		pe2 = _create_funding_pe(req, 15_000)
		_sync_funding_fields(req)
		r_final = _row(req)
		self.assertAlmostEqual(flt(r_final.total_paid_amount), 15_000, places=2)
		self.assertEqual(r_final.payment_status, "Partially Paid")
		self.assertEqual(r_final.payment_entry, pe2)

	# ── Scenario 6: allocation guard blocks invalid cancel ───────────────

	def test_allocation_guard_blocks_cancel(self):
		"""If PM Clearance allocations exceed post-cancel funded amount, cancel is blocked."""
		from erpnext_extensions.petty_management.tests.pm_cancel_qa_fixtures import (
			prepare_pi_for_clearance_fixture,
		)
		from erpnext_extensions.petty_management.tests.test_pm_request_multi_pe import (
			_close_pm_request,
		)

		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 100_000)
		pe1 = _create_funding_pe(req, 50_000)
		_sync_funding_fields(req)
		# v5.6.32: Partially Paid Requests must be Closed (or Paid) before Clearance.
		_close_pm_request(req, close_reason="Partial Approval")
		_sync_funding_fields(req)

		pi = tpm._make_pi_outstanding(40_000)
		prepare_pi_for_clearance_fixture(pi)
		cl = frappe.new_doc("PM Clearance")
		cl.company = tpm.COMPANY
		cl.employee = emp
		cl.transaction_date = today()
		cl.append(
			"details",
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi.name,
				"allocated_amount": 40_000,
				**tpm._pm_clearance_detail_policy_fields(),
			},
		)
		cl.append("request_allocations", {"pm_request": req, "allocated_amount": 40_000})
		cl.insert()
		cl.submit()
		tpm._approve_pm_clearance_for_reservation(cl.name)

		with self.assertRaises(frappe.ValidationError) as ctx:
			frappe.get_doc("Payment Entry", pe1).cancel()
		msg = str(ctx.exception).lower()
		self.assertIn("cannot cancel payment entry", msg)
		self.assertIn(pe1.lower(), msg)
		self.assertIn(req.lower(), msg)
		self.assertIn("reserved", msg)
		# Decision unchanged: still blocked when reserved > funded_after.
		self.assertNotIn("successfully", msg)

	# ── Scenario 6b: allocation ok when remaining funding covers reserved ─

	def test_cancel_allowed_when_remaining_covers_allocations(self):
		"""PE1+PE2 fund 100k; 40k reserved; cancel PE1 (50k) → remaining 50k ≥ reserved."""
		from erpnext_extensions.petty_management.tests.pm_cancel_qa_fixtures import (
			prepare_pi_for_clearance_fixture,
		)
		from erpnext_extensions.petty_management.tests.test_pm_request_multi_pe import (
			_close_pm_request,
		)

		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 100_000)
		ws_before = frappe.db.get_value("PM Request", req, "workflow_state")
		approvers_before = frappe.db.get_value(
			"PM Request", req, list(_APPROVAL_FIELDS), as_dict=True
		) if all(frappe.get_meta("PM Request").has_field(f) for f in _APPROVAL_FIELDS) else None

		pe1 = _create_funding_pe(req, 50_000)
		pe2 = _create_funding_pe(req, 50_000)
		_sync_funding_fields(req)
		# Fully funded → Paid; close not required, but keep Closed-or-Paid gate explicit.
		if frappe.db.get_value("PM Request", req, "payment_status") == "Partially Paid":
			_close_pm_request(req, close_reason="Partial Approval")
			_sync_funding_fields(req)

		pi = tpm._make_pi_outstanding(40_000)
		prepare_pi_for_clearance_fixture(pi)
		cl = frappe.new_doc("PM Clearance")
		cl.company = tpm.COMPANY
		cl.employee = emp
		cl.transaction_date = today()
		cl.append(
			"details",
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi.name,
				"allocated_amount": 40_000,
				**tpm._pm_clearance_detail_policy_fields(),
			},
		)
		cl.append("request_allocations", {"pm_request": req, "allocated_amount": 40_000})
		cl.insert()
		cl.submit()
		tpm._approve_pm_clearance_for_reservation(cl.name)

		frappe.get_doc("Payment Entry", pe1).cancel()
		frappe.db.commit()

		r = _row(req)
		self.assertEqual(r.docstatus, 1)
		self.assertEqual(r.workflow_state, ws_before)
		self.assertAlmostEqual(flt(r.total_paid_amount), 50_000, places=2)
		self.assertEqual(r.payment_status, "Partially Paid")
		self.assertEqual(r.payment_entry, pe2)
		self.assertEqual(cint(frappe.db.get_value("Payment Entry", pe1, "docstatus")), 2)
		if approvers_before:
			approvers_after = frappe.db.get_value(
				"PM Request", req, list(_APPROVAL_FIELDS), as_dict=True
			)
			for f in _APPROVAL_FIELDS:
				self.assertEqual(approvers_after.get(f), approvers_before.get(f))

	# ── Scenario 6c: Desk cancel-all ignore list includes PM Request ─────

	def test_desk_cancel_ignores_pm_request_link(self):
		"""get_submitted_linked_docs must omit PM Request when ignore list includes it."""
		from frappe.desk.form.linked_with import get_submitted_linked_docs

		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 10_000)
		pe = _create_funding_pe(req, 10_000)
		_sync_funding_fields(req)
		self.assertEqual(frappe.db.get_value("PM Request", req, "payment_entry"), pe)

		# Without ignore: PM Request appears as a submitted reverse link.
		raw = get_submitted_linked_docs("Payment Entry", pe, ignore_doctypes_on_cancel_all=[])
		raw_doctypes = {d.get("doctype") for d in (raw.get("docs") or [])}
		self.assertIn("PM Request", raw_doctypes)

		# With Desk ignore list (ERPNext defaults + PM Request): Request omitted.
		erpnext_defaults = [
			"Sales Invoice",
			"Purchase Invoice",
			"Journal Entry",
			"Repost Payment Ledger",
			"Repost Accounting Ledger",
			"Unreconcile Payment",
			"Unreconcile Payment Entries",
			"Bank Transaction",
			"PM Request",
		]
		ignored = get_submitted_linked_docs(
			"Payment Entry", pe, ignore_doctypes_on_cancel_all=erpnext_defaults
		)
		ignored_doctypes = {d.get("doctype") for d in (ignored.get("docs") or [])}
		self.assertNotIn("PM Request", ignored_doctypes)

		# Hook registration + client script must be additive.
		from erpnext_extensions import hooks as app_hooks

		pe_js = app_hooks.doctype_js.get("Payment Entry")
		self.assertTrue(pe_js)
		js_path = pe_js if isinstance(pe_js, str) else pe_js[0]
		self.assertIn("payment_entry_pm_request_cancel.js", js_path)
		js_full = frappe.get_app_path("erpnext_extensions", js_path)
		js_src = open(js_full).read()
		self.assertIn('push("PM Request")', js_src)
		self.assertIn("ignore_doctypes_on_cancel_all", js_src)
		self.assertIn(".includes(", js_src)
		# Additive only — must not assign a replacement SI/PI/JE array.
		self.assertNotIn('"Sales Invoice"', js_src)

	# ── Scenario 7: full cancel → business status = Waiting for Payment ──

	def test_cancel_single_pe_business_status(self):
		"""Requested=100k, PE1=100k, cancel PE1 → Not Paid, status=Waiting for Payment."""
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 100_000)
		ws_before = frappe.db.get_value("PM Request", req, "workflow_state")

		pe1 = _create_funding_pe(req, 100_000)
		_sync_funding_fields(req)
		r_paid = _row(req)
		self.assertEqual(r_paid.payment_status, "Paid")
		self.assertEqual(r_paid.status, "Paid")

		frappe.get_doc("Payment Entry", pe1).cancel()
		frappe.db.commit()

		r = _row(req)
		self.assertEqual(r.payment_status, "Not Paid")
		self.assertAlmostEqual(flt(r.total_paid_amount), 0, places=2)
		self.assertAlmostEqual(flt(r.remaining_to_pay), 100_000, places=2)
		self.assertIn(r.status, ("Waiting for Payment", "Not Paid"))
		self.assertEqual(r.workflow_state, ws_before)
		self.assertFalse(r.payment_entry)

	# ── Scenario 8: approval fields unchanged after PE cancel ────────────

	def test_approval_fields_unchanged_after_cancel(self):
		"""manager_approver, ceo_approver, finance_approver must survive PE cancel."""
		meta = frappe.get_meta("PM Request")
		available = [f for f in _APPROVAL_FIELDS if meta.has_field(f)]
		if not available:
			self.skipTest("No approver fields on PM Request")

		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 10_000)

		before = frappe.db.get_value("PM Request", req, available, as_dict=True)

		pe = _create_funding_pe(req, 10_000)
		_sync_funding_fields(req)
		frappe.get_doc("Payment Entry", pe).cancel()
		frappe.db.commit()

		after = frappe.db.get_value("PM Request", req, available, as_dict=True)
		for field in available:
			self.assertEqual(
				after.get(field), before.get(field),
				msg=f"{field} changed from {before.get(field)!r} to {after.get(field)!r}",
			)

	# ── Scenario 9: PE docstatus=2 after cancel ──────────────────────────

	def test_pe_docstatus_is_2_after_cancel(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 10_000)

		pe = _create_funding_pe(req, 10_000)
		frappe.get_doc("Payment Entry", pe).cancel()
		frappe.db.commit()

		self.assertEqual(cint(frappe.db.get_value("Payment Entry", pe, "docstatus")), 2)

	# ── Scenario 10: audit event emitted ─────────────────────────────────

	def test_audit_event_emitted_on_cancel(self):
		"""petty_audit must log pm_payment_entry_cancelled with request + PE references."""
		from unittest.mock import patch

		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 10_000)

		pe = _create_funding_pe(req, 10_000)

		captured: list[dict] = []
		_original = __import__(
			"erpnext_extensions.petty_management.petty_audit", fromlist=["log_event"]
		).log_event

		def _spy(event, **kw):
			captured.append({"event": event, **kw})
			_original(event, **kw)

		with patch("erpnext_extensions.petty_management.petty_audit.log_event", side_effect=_spy):
			frappe.get_doc("Payment Entry", pe).cancel()
			frappe.db.commit()

		cancel_events = [
			e for e in captured
			if e.get("event") == "pm_payment_entry_cancelled"
			and e.get("payment_entry") == pe
			and e.get("pm_request") == req
		]
		self.assertTrue(
			cancel_events,
			msg=f"No pm_payment_entry_cancelled audit event found for PE={pe}, req={req}",
		)
		evt = cancel_events[-1]
		self.assertEqual(evt["pm_request"], req)
		self.assertEqual(evt["payment_entry"], pe)


if __name__ == "__main__":
	unittest.main()
