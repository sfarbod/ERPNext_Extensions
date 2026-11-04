# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Multi-PE PM Request + operational close — unit tests (architecture v2.1).

Run::

	bench --site development.localhost run-tests \\
		--module erpnext_extensions.petty_management.tests.test_pm_request_multi_pe \\
		--skip-before-tests
"""

from __future__ import annotations

import inspect
import pathlib
import unittest

import frappe
from frappe.exceptions import ValidationError
from frappe.utils import flt, today

from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm


def _require_site_ready(testcase: unittest.TestCase) -> None:
	tpm._ensure_company_context()
	if not tpm.COMPANY:
		testcase.skipTest("No Company")
	if not tpm.BANK_ACCOUNT:
		testcase.skipTest("No bank/cash for PM Settings")
	_ensure_pm_settings_bank()


def _ensure_pm_settings_bank() -> None:
	from erpnext_extensions.petty_management.utils import get_pm_settings

	settings = get_pm_settings()
	if not settings:
		return
	if not settings.default_bank_account and tpm.BANK_ACCOUNT:
		settings.default_bank_account = tpm.BANK_ACCOUNT
		settings.save(ignore_permissions=True)
		frappe.db.commit()


def _approve_pm_request(req_name: str) -> None:
	tpm._finance_clear_pm_request(req_name)


def _new_submitted_request(employee: str, amount: float) -> str:
	tpm._ensure_petty_account()
	req = frappe.new_doc("PM Request")
	req.company = tpm.COMPANY
	req.employee = employee
	req.transaction_date = today()
	req.append("details", {"advance_amount": amount})
	req.insert()
	req.submit()
	_approve_pm_request(req.name)
	return req.name


def _create_funding_pe(pm_request: str, paid_amount: float | None = None) -> str:
	from erpnext_extensions.petty_management.services import request_service as rs

	sig = inspect.signature(rs.create_payment_entry)
	if paid_amount is not None and "paid_amount" in sig.parameters:
		pe_name = rs.create_payment_entry(pm_request, paid_amount=paid_amount)
	else:
		pe_name = rs.create_payment_entry(pm_request)
	pe = frappe.get_doc("Payment Entry", pe_name)
	if paid_amount is not None and "paid_amount" not in sig.parameters:
		if abs(flt(pe.paid_amount) - flt(paid_amount)) > 1e-6:
			pe.paid_amount = paid_amount
			pe.received_amount = paid_amount
			pe.save()
	if pe.docstatus == 0:
		pe.submit()
	frappe.db.commit()
	return pe_name


def _close_pm_request(
	pm_request: str,
	close_reason: str | None = None,
	close_reason_detail: str | None = None,
) -> None:
	from erpnext_extensions.petty_management.services import request_service as rs

	if not hasattr(rs, "close_pm_request"):
		raise AssertionError("close_pm_request API not implemented")
	rs.close_pm_request(pm_request, close_reason=close_reason, close_reason_detail=close_reason_detail)
	frappe.db.commit()


def _sync_funding_fields(pm_request: str) -> None:
	from erpnext_extensions.petty_management.services import funding_service

	if not hasattr(funding_service, "sync_pm_request_funding_fields"):
		raise AssertionError("sync_pm_request_funding_fields not implemented")
	funding_service.sync_pm_request_funding_fields(pm_request)
	frappe.db.commit()


class TestMultiPeSchema(unittest.TestCase):
	"""DocType fields required by PM_REQUEST_MULTI_PE_ARCHITECTURE.md."""

	REQUIRED_FIELDS = (
		"total_paid_amount",
		"remaining_to_pay",
		"total_draft_pe_amount",
		"allocated_amount",
		"available_for_clearance",
		"is_closed",
		"closed_on",
		"closed_by",
		"close_reason",
		"close_reason_detail",
	)

	def test_pm_request_has_multi_pe_fields(self):
		meta = frappe.get_meta("PM Request")
		missing = [f for f in self.REQUIRED_FIELDS if not meta.has_field(f)]
		self.assertEqual(missing, [], msg=f"Missing PM Request fields: {missing}")

	def test_payment_status_supports_partially_paid(self):
		meta = frappe.get_meta("PM Request")
		opts = (meta.get_options("payment_status") or "").split("\n")
		self.assertIn("Partially Paid", opts)

	def test_payment_entry_field_label_latest(self):
		meta = frappe.get_meta("PM Request")
		label = meta.get_label("payment_entry")
		self.assertIn("Latest", label or "")


class TestMultiPeFunding(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_require_site_ready(self)

	def test_create_payment_entry_accepts_optional_paid_amount(self):
		sig = inspect.signature(
			__import__(
				"erpnext_extensions.petty_management.services.request_service",
				fromlist=["create_payment_entry"],
			).create_payment_entry
		)
		self.assertIn("paid_amount", sig.parameters)

	def test_two_submitted_pe_partial_funding(self):
		from erpnext_extensions.petty_management.services.allocation_service import (
			get_pm_request_available_amount,
			get_pm_request_paid_amount,
		)

		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 100_000)
		pe1 = _create_funding_pe(req, 40_000)
		pe2 = _create_funding_pe(req, 30_000)
		self.assertNotEqual(pe1, pe2)
		_sync_funding_fields(req)
		req_doc = frappe.get_doc("PM Request", req)
		self.assertAlmostEqual(flt(req_doc.total_paid_amount), 70_000, places=2)
		self.assertAlmostEqual(flt(req_doc.remaining_to_pay), 30_000, places=2)
		self.assertEqual(req_doc.payment_status, "Partially Paid")
		self.assertAlmostEqual(get_pm_request_paid_amount(req), 70_000, places=2)
		self.assertAlmostEqual(get_pm_request_available_amount(req), 70_000, places=2)

	def test_over_funding_blocked(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 50_000)
		_create_funding_pe(req, 40_000)
		with self.assertRaises(ValidationError):
			_create_funding_pe(req, 20_000)

	def test_funding_queries_sum_submitted_only(self):
		from erpnext_extensions.petty_management.services import funding_queries

		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 20_000)
		pe_name = _create_funding_pe(req, 8_000)
		draft = frappe.get_doc("Payment Entry", pe_name)
		if draft.docstatus == 1:
			# create a second draft without submitting
			from erpnext_extensions.petty_management.services.request_service import create_payment_entry

			try:
				d2 = create_payment_entry(req, paid_amount=5_000)
			except TypeError:
				d2 = create_payment_entry(req)
			draft2 = frappe.get_doc("Payment Entry", d2)
			self.assertEqual(draft2.docstatus, 0)
			submitted = flt(funding_queries.sum_submitted_pe_amount(req))
			draft_total = flt(funding_queries.sum_draft_pe_amount(req))
			self.assertAlmostEqual(submitted, 8_000, places=2)
			self.assertAlmostEqual(draft_total, flt(draft2.paid_amount), places=2)
		else:
			submitted = flt(funding_queries.sum_submitted_pe_amount(req))
			self.assertAlmostEqual(submitted, 8_000, places=2)


class TestClosePmRequest(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_require_site_ready(self)

	def test_close_requires_reason_when_remaining_positive(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 100_000)
		_create_funding_pe(req, 40_000)
		_sync_funding_fields(req)
		with self.assertRaises(ValidationError):
			_close_pm_request(req)

	def test_close_other_requires_detail(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 50_000)
		with self.assertRaises(ValidationError):
			_close_pm_request(req, close_reason="Other")

	def test_close_blocked_with_draft_pe(self):
		from erpnext_extensions.petty_management.services.request_service import create_payment_entry

		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 60_000)
		try:
			create_payment_entry(req, paid_amount=10_000)
		except TypeError:
			create_payment_entry(req)
		with self.assertRaises(ValidationError) as ctx:
			_close_pm_request(req, close_reason="Budget Limitation")
		self.assertIn("cannot close while draft", str(ctx.exception).lower())

	def test_draft_pe_blocks_create_second_pe(self):
		from erpnext_extensions.petty_management.services.request_service import create_payment_entry

		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 60_000)
		create_payment_entry(req, paid_amount=10_000)
		with self.assertRaises(ValidationError) as ctx:
			create_payment_entry(req, paid_amount=5_000)
		self.assertIn("draft", str(ctx.exception).lower())

	def test_close_fully_paid_without_reason_ok_balances_unchanged(self):
		from erpnext_extensions.petty_management.services.allocation_service import (
			get_pm_request_available_amount,
			get_pm_request_paid_amount,
		)

		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 10_000)
		_create_funding_pe(req, 10_000)
		_sync_funding_fields(req)
		before = {
			"paid": flt(get_pm_request_paid_amount(req)),
			"available": flt(get_pm_request_available_amount(req)),
			"payment_status": frappe.db.get_value("PM Request", req, "payment_status"),
			"workflow_state": frappe.db.get_value("PM Request", req, "workflow_state"),
		}
		_close_pm_request(req)
		after_doc = frappe.get_doc("PM Request", req)
		self.assertEqual(cint(after_doc.is_closed), 1)
		self.assertAlmostEqual(flt(after_doc.total_paid_amount), before["paid"], places=2)
		self.assertAlmostEqual(flt(get_pm_request_paid_amount(req)), before["paid"], places=2)
		self.assertAlmostEqual(flt(get_pm_request_available_amount(req)), before["available"], places=2)
		self.assertEqual(after_doc.payment_status, before["payment_status"])
		self.assertEqual(after_doc.workflow_state, before["workflow_state"])

	def test_close_blocks_future_pe(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 80_000)
		_create_funding_pe(req, 20_000)
		_sync_funding_fields(req)
		_close_pm_request(req, close_reason="Partial Approval")
		with self.assertRaises(ValidationError):
			_create_funding_pe(req, 10_000)


def cint(v):
	from frappe.utils import cint as _c

	return _c(v)


class TestPeCancelGuard(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_require_site_ready(self)

	def test_pe_cancel_blocked_when_allocations_exceed_remaining_funded(self):
		from erpnext_extensions.petty_management.services.allocation_service import (
			sum_prior_pm_request_allocations,
		)

		# Prefer existing holder with a live Employee — insert can trip broken Raven hooks.
		holder_row = frappe.db.sql(
			"""
			select h.employee, h.name from `tabPM Holder` h
			inner join `tabEmployee` e on e.name = h.employee and ifnull(e.status,'')='Active'
			where h.company=%s and ifnull(h.is_blocked,0)=0 limit 1
			""",
			(tpm.COMPANY,),
			as_dict=True,
		)
		if holder_row:
			emp = holder_row[0].employee
			if frappe.db.exists("User", "Administrator"):
				frappe.db.set_value(
					"Employee", emp, "expense_approver", "Administrator", update_modified=False
				)
		else:
			emp = tpm._make_employee()
			tpm._make_holder(emp)
		req = _new_submitted_request(emp, 100_000)
		pe1 = _create_funding_pe(req, 30_000)
		_sync_funding_fields(req)
		# v5.6.32: Partially Paid Requests must be Closed (or Paid) before Clearance.
		_close_pm_request(req, close_reason="Partial Approval")
		_sync_funding_fields(req)

		from erpnext_extensions.petty_management.tests.pm_cancel_qa_fixtures import (
			prepare_pi_for_clearance_fixture,
		)

		pi = tpm._make_pi_outstanding(25_000)
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
				"allocated_amount": 25_000,
				**tpm._pm_clearance_detail_policy_fields(),
			},
		)
		cl.append("request_allocations", {"pm_request": req, "allocated_amount": 25_000})
		cl.insert()
		cl.submit()
		tpm._approve_pm_clearance_for_reservation(cl.name)
		self.assertGreater(flt(sum_prior_pm_request_allocations(req, None)), 20_000)

		pe_doc = frappe.get_doc("Payment Entry", pe1)
		with self.assertRaises(ValidationError):
			pe_doc.cancel()


class TestReconciliationMultiPe(unittest.TestCase):
	def test_reconciliation_codes_registered(self):
		from erpnext_extensions.petty_management.services import reconciliation_service as rec

		codes = {c for c, _fn, _sev in getattr(rec, "_CHECKS", [])}
		for code in (
			"PAID_EXCEEDS_REQUESTED",
			"SUBMITTED_PLUS_DRAFT_EXCEEDS_REQUEST",
			"AVAILABLE_NEGATIVE",
		):
			self.assertIn(code, codes)


class TestRejectPmRequest(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_require_site_ready(self)

	def test_reject_blocked_when_submitted_pe_exists(self):
		from erpnext_extensions.petty_management.services.request_action_policy import (
			compute_pm_request_action_flags,
			validate_pm_request_workflow_action,
		)

		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 20_000)
		_create_funding_pe(req, 5_000)
		doc = frappe.get_doc("PM Request", req)
		flags = compute_pm_request_action_flags(doc)
		self.assertFalse(flags["can_reject"])
		with self.assertRaises(ValidationError):
			validate_pm_request_workflow_action(doc, "PM Reject")


class TestPaymentEntryListApi(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_require_site_ready(self)

	def test_list_payment_entries_returns_all_linked(self):
		from erpnext_extensions.petty_management.services.funding_queries import (
			list_payment_entries_for_pm_request,
		)

		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 30_000)
		pe1 = _create_funding_pe(req, 10_000)
		pe2 = _create_funding_pe(req, 8_000)
		rows = list_payment_entries_for_pm_request(req)
		names = {r["payment_entry"] for r in rows}
		self.assertIn(pe1, names)
		self.assertIn(pe2, names)
		self.assertEqual(len(rows), 2)
		for row in rows:
			self.assertIn(row["status"], ("Draft", "Submitted", "Cancelled"))

	def test_get_pm_request_payment_entries_whitelist_read_permission(self):
		from erpnext_extensions.petty_management.doctype.pm_request.pm_request import (
			get_pm_request_payment_entries,
		)

		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 15_000)
		pe = _create_funding_pe(req, 5_000)
		frappe.set_user("Administrator")
		payload = get_pm_request_payment_entries(req)
		self.assertIn("response_version_id", payload)
		rows = payload.get("payment_entries") or []
		self.assertIsInstance(rows, list)
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["payment_entry"], pe)

	def test_get_pm_request_payment_entries_guest_raises_permission_error(self):
		from erpnext_extensions.petty_management.doctype.pm_request.pm_request import (
			get_pm_request_payment_entries,
		)

		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 10_000)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			get_pm_request_payment_entries(req)
		frappe.set_user("Administrator")


class TestAllocationNoIsClosed(unittest.TestCase):
	def test_allocation_service_does_not_reference_is_closed(self):
		root = pathlib.Path(frappe.get_app_path("erpnext_extensions")) / "petty_management" / "services"
		text = (root / "allocation_service.py").read_text(encoding="utf-8")
		self.assertNotIn("is_closed", text)


if __name__ == "__main__":
	unittest.main()
