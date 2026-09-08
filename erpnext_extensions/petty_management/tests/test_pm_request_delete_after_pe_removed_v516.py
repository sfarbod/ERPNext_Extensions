# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.1.6 — delete after PE cancel+delete; cancelled banner must not say Submit first."""

from __future__ import annotations

import unittest

import frappe
from frappe.exceptions import PermissionError, ValidationError
from frappe.utils import cint

from erpnext_extensions.petty_management.services.request_action_policy import (
	MSG_REQUEST_CANCELLED,
	MSG_SUBMIT_FIRST,
	build_pm_request_business_status_presentation,
	compute_pm_request_action_flags,
)
from erpnext_extensions.petty_management.services.request_lifecycle_eligibility import (
	get_pm_request_delete_blockers,
	user_may_execute_pm_request_delete,
)
from erpnext_extensions.petty_management.services.request_service import (
	cancel_pm_request,
	delete_pm_request,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.test_pm_request_cancel_delete import (
	_link_request_journal_entry,
	_require_journal_entry_field,
	_stub_journal_entry,
)
from erpnext_extensions.petty_management.tests.test_pm_request_multi_pe import (
	_create_funding_pe,
	_new_submitted_request,
	_require_site_ready,
	_sync_funding_fields,
)

ACCOUNTANT_USER = "pm_delete_v516_acct@example.com"
REQUESTER_USER = "pm_delete_v516_user@example.com"


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
		update_password(email, "pm_delete_v516_test")
	frappe.db.commit()


def _cancel_and_delete_pe(pe_name: str) -> None:
	pe = frappe.get_doc("Payment Entry", pe_name)
	if cint(pe.docstatus) == 1:
		pe.cancel()
	if frappe.db.exists("Payment Entry", pe_name):
		frappe.delete_doc("Payment Entry", pe_name, force=1, ignore_permissions=True)
	frappe.db.commit()


def _flags(req: str, *, user: str = "Administrator") -> dict:
	frappe.set_user(user)
	return compute_pm_request_action_flags(frappe.get_doc("PM Request", req))


class TestPmRequestDeleteAfterPeRemovedV516(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_require_site_ready(cls)
		_ensure_users()

	def setUp(self):
		frappe.set_user("Administrator")

	def test_a_cancelled_request_pe_cancelled_and_deleted_delete_allowed(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 10_000)
		pe = _create_funding_pe(req, 10_000)
		_sync_funding_fields(req)
		_cancel_and_delete_pe(pe)
		_sync_funding_fields(req)
		self.assertFalse(frappe.db.exists("Payment Entry", pe))
		cancel_pm_request(req)
		frappe.db.commit()

		doc = frappe.get_doc("PM Request", req)
		self.assertEqual(cint(doc.docstatus), 2)
		self.assertEqual(doc.status, "Cancelled")
		self.assertEqual(get_pm_request_delete_blockers(doc), [])
		flags = _flags(req)
		self.assertTrue(flags["can_delete_pm_request"], msg=flags.get("delete_block_reason"))
		delete_pm_request(req)
		frappe.db.commit()
		self.assertFalse(frappe.db.exists("PM Request", req))

	def test_b_cancelled_request_with_submitted_pe_blocked(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 10_000)
		pe = _create_funding_pe(req, 10_000)
		_sync_funding_fields(req)
		self.assertEqual(cint(frappe.db.get_value("Payment Entry", pe, "docstatus")), 1)
		# Simulate cancelled Request while a submitted PE remains linked (delete policy only).
		frappe.db.set_value(
			"PM Request",
			req,
			{"docstatus": 2, "status": "Cancelled"},
			update_modified=False,
		)
		frappe.db.commit()
		blockers = get_pm_request_delete_blockers(frappe.get_doc("PM Request", req))
		self.assertTrue(blockers)
		self.assertTrue(any("Payment Entry" in b or "Submitted" in b for b in blockers))
		self.assertFalse(_flags(req)["can_delete_pm_request"])

	def test_c_cancelled_request_with_draft_pe_blocked(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 8_000)
		from erpnext_extensions.petty_management.services import request_service as rs

		pe_name = rs.create_payment_entry(req, paid_amount=8_000)
		self.assertEqual(cint(frappe.db.get_value("Payment Entry", pe_name, "docstatus")), 0)
		frappe.db.set_value(
			"PM Request",
			req,
			{"docstatus": 2, "status": "Cancelled"},
			update_modified=False,
		)
		frappe.db.commit()
		blockers = get_pm_request_delete_blockers(frappe.get_doc("PM Request", req))
		self.assertTrue(blockers)
		self.assertTrue(any("Draft" in b or "Payment Entry" in b for b in blockers))
		self.assertFalse(_flags(req)["can_delete_pm_request"])

	def test_d_cancelled_request_with_active_clearance_blocked(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 15_000)
		cancel_pm_request(req)
		frappe.db.commit()
		# Minimal Clearance + allocation row (avoid PI dimension fixtures).
		cl_name = f"_PM-TEST-CL-{frappe.generate_hash(length=8)}"
		now = frappe.utils.now()
		frappe.db.sql(
			"""
			INSERT INTO `tabPM Clearance`
				(name, creation, modified, modified_by, owner, docstatus, idx,
				 company, employee, transaction_date, status, workflow_state)
			VALUES
				(%s, %s, %s, 'Administrator', 'Administrator', 0, 0,
				 %s, %s, %s, 'Draft', 'Draft')
			""",
			(cl_name, now, now, tpm.COMPANY, emp, frappe.utils.today()),
		)
		child = frappe.generate_hash(length=10)
		frappe.db.sql(
			"""
			INSERT INTO `tabPM Clearance Request Allocation`
				(name, creation, modified, modified_by, owner, docstatus, idx,
				 parent, parenttype, parentfield, pm_request, allocated_amount, is_legacy_row)
			VALUES
				(%s, %s, %s, 'Administrator', 'Administrator', 0, 1,
				 %s, 'PM Clearance', 'request_allocations', %s, %s, 0)
			""",
			(child, now, now, cl_name, req, 1000),
		)
		frappe.db.commit()
		blockers = get_pm_request_delete_blockers(frappe.get_doc("PM Request", req))
		self.assertTrue(any("Clearance" in b for b in blockers))
		self.assertFalse(_flags(req)["can_delete_pm_request"])
		# cleanup stub
		frappe.db.sql("delete from `tabPM Clearance Request Allocation` where name=%s", (child,))
		frappe.db.sql("delete from `tabPM Clearance` where name=%s", (cl_name,))
		frappe.db.commit()

	def test_e_cancelled_request_with_active_je_blocked(self):
		_require_journal_entry_field(self)
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 9_000)
		cancel_pm_request(req)
		frappe.db.commit()
		je = _stub_journal_entry(company=tpm.COMPANY, docstatus=1)
		_link_request_journal_entry(req, je)
		blockers = get_pm_request_delete_blockers(frappe.get_doc("PM Request", req))
		self.assertTrue(any("Journal Entry" in b for b in blockers))
		self.assertFalse(_flags(req)["can_delete_pm_request"])

	def test_f_admin_delete_succeeds_when_eligible(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 7_000)
		pe = _create_funding_pe(req, 7_000)
		_cancel_and_delete_pe(pe)
		_sync_funding_fields(req)
		cancel_pm_request(req)
		frappe.db.commit()
		frappe.set_user("Administrator")
		delete_pm_request(req)
		frappe.db.commit()
		self.assertFalse(frappe.db.exists("PM Request", req))

	def test_g_accountant_delete_denied(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 6_000)
		cancel_pm_request(req)
		frappe.db.commit()
		doc = frappe.get_doc("PM Request", req)
		frappe.set_user(ACCOUNTANT_USER)
		self.assertFalse(user_may_execute_pm_request_delete(doc))
		with self.assertRaises(PermissionError):
			delete_pm_request(req)

	def test_h_cancelled_banner_not_submit_first(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 5_000)
		cancel_pm_request(req)
		frappe.db.commit()
		doc = frappe.get_doc("PM Request", req)
		pres = build_pm_request_business_status_presentation(doc)
		msgs = pres.get("ui_messages") or []
		self.assertNotIn(str(MSG_SUBMIT_FIRST), msgs)
		self.assertIn(str(MSG_REQUEST_CANCELLED), msgs)
		flags = compute_pm_request_action_flags(doc)
		self.assertNotIn(str(MSG_SUBMIT_FIRST), flags.get("ui_messages") or [])
		self.assertIn(str(MSG_REQUEST_CANCELLED), flags.get("ui_messages") or [])
		self.assertEqual(cint(doc.docstatus), 2)
		self.assertEqual(doc.status, "Cancelled")
