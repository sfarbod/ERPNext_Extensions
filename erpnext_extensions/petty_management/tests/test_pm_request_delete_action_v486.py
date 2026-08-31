# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.8.6 — PM Request Delete PM Request business action (Administrator only)."""

from __future__ import annotations

import unittest

import frappe
from frappe.exceptions import PermissionError, ValidationError
from frappe.utils import cint

from erpnext_extensions.petty_management.services.request_action_policy import (
	compute_pm_request_action_flags,
)
from erpnext_extensions.petty_management.services.request_lifecycle_eligibility import (
	user_may_execute_pm_request_delete,
)
from erpnext_extensions.petty_management.services.request_service import (
	cancel_pm_request,
	delete_pm_request,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.test_pm_request_cancel_delete import (
	_make_clearance,
	_set_clearance_status,
)
from erpnext_extensions.petty_management.tests.test_pm_request_multi_pe import (
	_create_funding_pe,
	_new_submitted_request,
	_require_site_ready,
	_sync_funding_fields,
)

ACCOUNTANT_USER = "pm_delete_v486_acct@example.com"
REQUESTER_USER = "pm_delete_v486_user@example.com"


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
		update_password(email, "pm_delete_v486_test")
	frappe.db.commit()


def _cancelled_clean(emp: str, amount: float = 12_000) -> str:
	req = _new_submitted_request(emp, amount)
	cancel_pm_request(req)
	frappe.db.commit()
	return req


def _flags(req: str, *, user: str | None = None) -> dict:
	if user:
		frappe.set_user(user)
	return compute_pm_request_action_flags(frappe.get_doc("PM Request", req))


def _link_counts(name: str) -> dict:
	return {
		"Comment": frappe.db.count(
			"Comment", {"reference_doctype": "PM Request", "reference_name": name}
		),
		"ToDo": frappe.db.count(
			"ToDo", {"reference_type": "PM Request", "reference_name": name}
		),
		"Communication": frappe.db.count(
			"Communication", {"reference_doctype": "PM Request", "reference_name": name}
		),
		"DynamicLink": frappe.db.count(
			"Dynamic Link", {"link_doctype": "PM Request", "link_name": name}
		),
	}


class TestPmRequestDeleteActionV486(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_require_site_ready(cls)
		_ensure_users()

	def setUp(self):
		frappe.set_user("Administrator")

	def test_only_administrator_may_delete(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _cancelled_clean(emp)
		doc = frappe.get_doc("PM Request", req)
		frappe.set_user("Administrator")
		self.assertTrue(user_may_execute_pm_request_delete(doc))
		frappe.set_user(ACCOUNTANT_USER)
		self.assertFalse(user_may_execute_pm_request_delete(doc))
		frappe.set_user(REQUESTER_USER)
		self.assertFalse(user_may_execute_pm_request_delete(doc))

	def test_cancelled_clean_admin_visible_and_deletes(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _cancelled_clean(emp, 14_000)
		self.assertEqual(cint(frappe.db.get_value("PM Request", req, "docstatus")), 2)
		flags = _flags(req, user="Administrator")
		self.assertTrue(flags["can_delete_pm_request"], msg=flags.get("delete_block_reason"))
		frappe.set_user("Administrator")
		delete_pm_request(req)
		frappe.db.commit()
		self.assertFalse(frappe.db.exists("PM Request", req))

	def test_accountant_delete_hidden_and_api_denied(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _cancelled_clean(emp, 13_000)
		flags = _flags(req, user=ACCOUNTANT_USER)
		self.assertFalse(flags["can_delete_pm_request"])
		frappe.set_user(ACCOUNTANT_USER)
		with self.assertRaises(PermissionError):
			delete_pm_request(req)

	def test_submitted_hidden(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 10_000)
		flags = _flags(req, user="Administrator")
		self.assertFalse(flags["can_delete_pm_request"])

	def test_cancelled_with_pe_history_hidden(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 10_000)
		pe = _create_funding_pe(req, 10_000)
		frappe.get_doc("Payment Entry", pe).cancel()
		_sync_funding_fields(req)
		cancel_pm_request(req)
		frappe.db.commit()
		flags = _flags(req, user="Administrator")
		self.assertFalse(flags["can_delete_pm_request"])

	def test_cancelled_with_clearance_history_hidden(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 15_000)
		pe = _create_funding_pe(req, 15_000)
		_sync_funding_fields(req)
		cl = _make_clearance(emp, req, 2_000, submit=False)
		_set_clearance_status(cl, "Rejected")
		frappe.get_doc("Payment Entry", pe).cancel()
		_sync_funding_fields(req)
		cancel_pm_request(req)
		frappe.db.commit()
		flags = _flags(req, user="Administrator")
		self.assertFalse(flags["can_delete_pm_request"])

	def test_requester_cannot_execute_delete_api(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _cancelled_clean(emp)
		frappe.set_user(REQUESTER_USER)
		with self.assertRaises(PermissionError):
			delete_pm_request(req)

	def test_cancel_then_delete_no_orphan_links(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 9_500)
		cancel_pm_request(req)
		frappe.db.commit()
		before = _link_counts(req)
		frappe.set_user("Administrator")
		delete_pm_request(req)
		frappe.db.commit()
		self.assertFalse(frappe.db.exists("PM Request", req))
		after = _link_counts(req)
		self.assertEqual(after["ToDo"], 0)
		self.assertEqual(after["Communication"], 0)
		self.assertEqual(after["DynamicLink"], 0)
		self.assertEqual(after["Comment"], before["Comment"])

	def test_api_blocked_when_funded_history(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 11_000)
		_create_funding_pe(req, 5_000)
		_sync_funding_fields(req)
		with self.assertRaises(ValidationError):
			cancel_pm_request(req)
		frappe.set_user("Administrator")
		with self.assertRaises(ValidationError):
			delete_pm_request(req)


if __name__ == "__main__":
	unittest.main()
