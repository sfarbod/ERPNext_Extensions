# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.8.5 — PM Request Cancel PM Request business action."""

from __future__ import annotations

import unittest

import frappe
from frappe.exceptions import PermissionError, ValidationError
from frappe.utils import cint

from erpnext_extensions.petty_management.services.request_action_policy import (
	compute_pm_request_action_flags,
)
from erpnext_extensions.petty_management.services.request_lifecycle_eligibility import (
	user_may_execute_pm_request_cancel,
)
from erpnext_extensions.petty_management.services.request_service import cancel_pm_request
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.test_pm_request_cancel_delete import (
	_link_request_journal_entry,
	_make_clearance,
	_require_journal_entry_field,
	_set_clearance_status,
	_stub_journal_entry,
)
from erpnext_extensions.petty_management.tests.test_pm_request_multi_pe import (
	_create_funding_pe,
	_new_submitted_request,
	_require_site_ready,
	_sync_funding_fields,
)

ACCOUNTANT_USER = "pm_cancel_v485_acct@example.com"
REQUESTER_USER = "pm_cancel_v485_user@example.com"


def _funded_request(emp: str, amount: float) -> tuple[str, str]:
	req = _new_submitted_request(emp, amount)
	pe = _create_funding_pe(req, amount)
	_sync_funding_fields(req)
	return req, pe


def _draft_pe(req: str, amount: float) -> str:
	from erpnext_extensions.petty_management.services import request_service as rs

	pe_name = rs.create_payment_entry(req, paid_amount=amount)
	pe = frappe.get_doc("Payment Entry", pe_name)
	if pe.docstatus != 0:
		frappe.throw(f"Expected draft PE, got docstatus={pe.docstatus}")
	return pe_name


def _ensure_users() -> None:
	from frappe.utils.password import update_password

	frappe.set_user("Administrator")
	for email, roles in (
		(ACCOUNTANT_USER, ("Petty Management Accountant", "Accounts User")),
		(REQUESTER_USER, ("Petty Management User", "Accounts User")),
	):
		if frappe.db.exists("User", email):
			frappe.delete_doc("User", email, force=True, ignore_permissions=True)
		doc = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0],
				"send_welcome_email": 0,
				"user_type": "System User",
				"enabled": 1,
			}
		)
		doc.insert(ignore_permissions=True)
		for role in roles:
			doc.append("roles", {"role": role})
		doc.save(ignore_permissions=True)
		update_password(email, "pm_cancel_v485_test")
	frappe.db.commit()


def _flags(req: str, *, user: str | None = None) -> dict:
	if user:
		frappe.set_user(user)
	return compute_pm_request_action_flags(frappe.get_doc("PM Request", req))


class TestPmRequestCancelActionV485(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_require_site_ready(cls)
		_ensure_users()

	def setUp(self):
		frappe.set_user("Administrator")

	def test_accountant_may_cancel_requester_may_not(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 12_000)
		doc = frappe.get_doc("PM Request", req)
		frappe.set_user(ACCOUNTANT_USER)
		self.assertTrue(user_may_execute_pm_request_cancel(doc))
		frappe.set_user(REQUESTER_USER)
		self.assertFalse(user_may_execute_pm_request_cancel(doc))

	def test_scenario_a_finance_approved_clean_visible_and_succeeds(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 15_000)
		self.assertEqual(
			frappe.db.get_value("PM Request", req, "payment_status"), "Not Paid"
		)
		ws = frappe.db.get_value("PM Request", req, "workflow_state")
		flags = _flags(req, user=ACCOUNTANT_USER)
		self.assertTrue(flags["can_cancel_pm_request"], msg=flags.get("cancel_block_reason"))
		frappe.set_user(ACCOUNTANT_USER)
		cancel_pm_request(req)
		frappe.db.commit()
		row = frappe.db.get_value(
			"PM Request", req, ["docstatus", "status", "workflow_state"], as_dict=True
		)
		self.assertEqual(cint(row.docstatus), 2)
		self.assertEqual(row.status, "Cancelled")
		self.assertEqual(row.workflow_state, ws)

	def test_scenario_b_submitted_pe_hidden(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 20_000)
		_create_funding_pe(req, 20_000)
		_sync_funding_fields(req)
		self.assertEqual(
			frappe.db.get_value("PM Request", req, "payment_status"), "Paid"
		)
		flags = _flags(req, user=ACCOUNTANT_USER)
		self.assertFalse(flags["can_cancel_pm_request"])

	def test_scenario_d_partially_paid_hidden(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 30_000)
		_create_funding_pe(req, 12_000)
		_sync_funding_fields(req)
		self.assertEqual(
			frappe.db.get_value("PM Request", req, "payment_status"), "Partially Paid"
		)
		flags = _flags(req, user=ACCOUNTANT_USER)
		self.assertFalse(flags["can_cancel_pm_request"])

	def test_scenario_c_draft_pe_hidden(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 20_000)
		try:
			_draft_pe(req, 5_000)
		except Exception:
			self.skipTest("Could not create draft PE")
		flags = _flags(req, user=ACCOUNTANT_USER)
		self.assertFalse(flags["can_cancel_pm_request"])

	def test_scenario_d_draft_clearance_hidden(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req, pe = _funded_request(emp, 40_000)
		cl = _make_clearance(emp, req, 4_000, submit=False)
		frappe.get_doc("Payment Entry", pe).cancel()
		_sync_funding_fields(req)
		_set_clearance_status(cl, "Draft", docstatus=0)
		flags = _flags(req, user=ACCOUNTANT_USER)
		self.assertFalse(flags["can_cancel_pm_request"])

	def test_scenario_e_submitted_clearance_hidden(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req, pe = _funded_request(emp, 40_000)
		cl = _make_clearance(emp, req, 4_000, submit=False)
		_set_clearance_status(cl, "Approved", docstatus=1)
		flags = _flags(req, user=ACCOUNTANT_USER)
		self.assertFalse(flags["can_cancel_pm_request"])
		self.assertTrue(frappe.db.exists("Payment Entry", pe))

	def test_scenario_f_journal_entry_hidden(self):
		_require_journal_entry_field(self)
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 7_500)
		je = _stub_journal_entry(company=tpm.COMPANY, docstatus=1)
		_link_request_journal_entry(req, je)
		flags = _flags(req, user=ACCOUNTANT_USER)
		self.assertFalse(flags["can_cancel_pm_request"])

	def test_scenario_k_draft_journal_entry_hidden(self):
		_require_journal_entry_field(self)
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 6_500)
		je = _stub_journal_entry(company=tpm.COMPANY, docstatus=0)
		_link_request_journal_entry(req, je)
		flags = _flags(req, user=ACCOUNTANT_USER)
		self.assertFalse(flags["can_cancel_pm_request"])

	def test_scenario_g_closed_hidden(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 9_000)
		frappe.db.set_value("PM Request", req, "is_closed", 1, update_modified=False)
		flags = _flags(req, user=ACCOUNTANT_USER)
		self.assertFalse(flags["can_cancel_pm_request"])

	def test_scenario_h_cancelled_hidden(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 8_000)
		frappe.get_doc("PM Request", req).cancel()
		flags = _flags(req, user=ACCOUNTANT_USER)
		self.assertFalse(flags["can_cancel_pm_request"])

	def test_requester_cannot_execute_cancel_api(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 11_000)
		frappe.set_user(REQUESTER_USER)
		with self.assertRaises(PermissionError):
			cancel_pm_request(req)

	def test_api_blocked_when_funded(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 18_000)
		_create_funding_pe(req, 10_000)
		_sync_funding_fields(req)
		frappe.set_user(ACCOUNTANT_USER)
		with self.assertRaises(ValidationError):
			cancel_pm_request(req)


if __name__ == "__main__":
	unittest.main()
