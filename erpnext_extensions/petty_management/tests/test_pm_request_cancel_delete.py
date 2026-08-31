# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.6.8 — PM Request cancel / delete eligibility.

Cancel = open financial process. Delete = history (independent).

Run::

	bench --site development.localhost run-tests \\
		--module erpnext_extensions.petty_management.tests.test_pm_request_cancel_delete \\
		--skip-before-tests
"""

from __future__ import annotations

import unittest

import frappe
from frappe.exceptions import ValidationError
from frappe.utils import cint, today

from erpnext_extensions.petty_management.services.request_lifecycle_eligibility import (
	assert_pm_request_cancel_allowed,
	assert_pm_request_delete_allowed,
	get_pm_request_cancel_blockers,
	get_pm_request_delete_blockers,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.pm_cancel_qa_fixtures import (
	get_doctype_json_role_perm,
	prepare_pi_for_clearance_fixture,
)
from erpnext_extensions.petty_management.tests.test_pm_request_multi_pe import (
	_create_funding_pe,
	_new_submitted_request,
	_require_site_ready,
	_sync_funding_fields,
)


def _draft_pe(req: str, amount: float) -> str:
	from erpnext_extensions.petty_management.services import request_service as rs

	pe_name = rs.create_payment_entry(req, paid_amount=amount)
	pe = frappe.get_doc("Payment Entry", pe_name)
	if pe.docstatus != 0:
		frappe.throw(f"Expected draft PE, got docstatus={pe.docstatus}")
	return pe_name


def _make_clearance(emp: str, req: str, amount: float, *, submit: bool = False):
	pi = tpm._make_pi_outstanding(amount)
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
			"allocated_amount": amount,
			**tpm._pm_clearance_detail_policy_fields(),
		},
	)
	cl.append("request_allocations", {"pm_request": req, "allocated_amount": amount})
	cl.insert()
	if submit:
		cl.submit()
	return cl.name


def _set_clearance_status(name: str, status: str, docstatus: int | None = None):
	vals = {"status": status}
	if docstatus is not None:
		vals["docstatus"] = docstatus
	frappe.db.set_value("PM Clearance", name, vals, update_modified=False)


def _force_clearance_cancelled(name: str):
	"""Mark Clearance + child rows cancelled (Frappe back-link uses child docstatus)."""
	frappe.db.set_value(
		"PM Clearance", name, {"docstatus": 2, "status": "Cancelled"}, update_modified=False
	)
	for doctype in ("PM Clearance Request Allocation", "PM Clearance Detail"):
		if frappe.db.table_exists(doctype):
			frappe.db.sql(
				f"UPDATE `tab{doctype}` SET docstatus=2 WHERE parent=%s AND parenttype='PM Clearance'",
				(name,),
			)


def _funded_request(emp: str, amount: float) -> tuple[str, str]:
	req = _new_submitted_request(emp, amount)
	pe = _create_funding_pe(req, amount)
	_sync_funding_fields(req)
	return req, pe


def _cancel_pe(pe: str, req: str) -> None:
	frappe.get_doc("Payment Entry", pe).cancel()
	_sync_funding_fields(req)


def _require_journal_entry_field(testcase: unittest.TestCase) -> None:
	"""Skip when schema lacks journal_entry (production uses meta.has_field)."""
	if not frappe.get_meta("PM Request").has_field("journal_entry"):
		testcase.skipTest(
			'PM Request has no field journal_entry; '
			'production cancel/delete guards with meta.has_field("journal_entry")'
		)


def _stub_journal_entry(*, company: str, docstatus: int) -> str:
	"""Minimal submitted/cancelled JE row for Request-level link tests (no GL)."""
	name = f"_PM-TEST-JE-{frappe.generate_hash(length=8)}"
	now = frappe.utils.now()
	frappe.db.sql(
		"""
		INSERT INTO `tabJournal Entry`
			(name, creation, modified, modified_by, owner, docstatus, idx,
			 company, voucher_type, naming_series, posting_date, title,
			 total_debit, total_credit, difference, multi_currency)
		VALUES
			(%s, %s, %s, %s, %s, %s, 0,
			 %s, 'Journal Entry', 'ACC-JV-.YYYY.-', %s, %s,
			 0, 0, 0, 0)
		""",
		(
			name,
			now,
			now,
			"Administrator",
			"Administrator",
			int(docstatus),
			company,
			today(),
			name,
		),
	)
	return name


def _link_request_journal_entry(req: str, je: str | None) -> None:
	frappe.db.set_value("PM Request", req, "journal_entry", je, update_modified=False)
	frappe.db.commit()



def _assert_blocked_with(self, req: str, *needles: str):
	with self.assertRaises(ValidationError) as ctx:
		assert_pm_request_cancel_allowed(req)
	msg = str(ctx.exception).lower()
	for n in needles:
		self.assertIn(n.lower(), msg, msg=f"expected {n!r} in {msg!r}")


class TestPmRequestCancelEligibility(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_require_site_ready(self)

	# --- Payment Entry matrix ---

	def test_cancel_no_pe(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 10_000)
		ws = frappe.db.get_value("PM Request", req, "workflow_state")
		approvers = frappe.db.get_value(
			"PM Request", req, ["manager_approver", "ceo_approver", "finance_approver"], as_dict=True
		)
		version_count = frappe.db.count("Version", {"ref_doctype": "PM Request", "docname": req})
		assert_pm_request_cancel_allowed(req)
		frappe.get_doc("PM Request", req).cancel()
		frappe.db.commit()
		row = frappe.db.get_value(
			"PM Request", req, ["docstatus", "status", "workflow_state"], as_dict=True
		)
		self.assertEqual(cint(row.docstatus), 2)
		self.assertEqual(row.status, "Cancelled")
		self.assertEqual(row.workflow_state, ws)
		after = frappe.db.get_value(
			"PM Request", req, ["manager_approver", "ceo_approver", "finance_approver"], as_dict=True
		)
		self.assertEqual(after, approvers)
		self.assertGreaterEqual(
			frappe.db.count("Version", {"ref_doctype": "PM Request", "docname": req}),
			version_count,
		)

	def test_cancel_blocked_draft_pe(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 20_000)
		try:
			pe = _draft_pe(req, 5_000)
		except Exception:
			self.skipTest("Could not create draft PE (auto-submit?)")
		_assert_blocked_with(self, req, "draft", "payment entry")
		self.assertTrue(frappe.db.exists("Payment Entry", pe))

	def test_cancel_blocked_submitted_pe(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req, pe = _funded_request(emp, 15_000)
		_assert_blocked_with(self, req, "submitted", "payment entry")
		self.assertEqual(cint(frappe.db.get_value("Payment Entry", pe, "docstatus")), 1)

	def test_cancel_after_all_pe_cancelled(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req, pe = _funded_request(emp, 12_000)
		_cancel_pe(pe, req)
		self.assertEqual(cint(frappe.db.get_value("Payment Entry", pe, "docstatus")), 2)
		assert_pm_request_cancel_allowed(req)
		frappe.get_doc("PM Request", req).cancel()
		self.assertEqual(cint(frappe.db.get_value("PM Request", req, "docstatus")), 2)
		self.assertEqual(cint(frappe.db.get_value("Payment Entry", pe, "docstatus")), 2)

	def test_cancel_multi_pe_partial_blocks(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 100_000)
		pe1 = _create_funding_pe(req, 40_000)
		pe2 = _create_funding_pe(req, 60_000)
		_sync_funding_fields(req)
		frappe.get_doc("Payment Entry", pe1).cancel()
		_sync_funding_fields(req)
		_assert_blocked_with(self, req, "submitted", "payment entry")
		self.assertEqual(cint(frappe.db.get_value("Payment Entry", pe2, "docstatus")), 1)

	def test_cancel_multi_pe_all_cancelled_allows(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 50_000)
		pe1 = _create_funding_pe(req, 20_000)
		pe2 = _create_funding_pe(req, 30_000)
		_sync_funding_fields(req)
		frappe.get_doc("Payment Entry", pe1).cancel()
		frappe.get_doc("Payment Entry", pe2).cancel()
		_sync_funding_fields(req)
		assert_pm_request_cancel_allowed(req)
		frappe.get_doc("PM Request", req).cancel()
		self.assertEqual(cint(frappe.db.get_value("PM Request", req, "docstatus")), 2)

	def test_cancel_not_pointer_authoritative(self):
		"""Clear pointer while submitted PE remains → still blocked via PE list."""
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req, pe = _funded_request(emp, 10_000)
		frappe.db.set_value("PM Request", req, "payment_entry", None, update_modified=False)
		self.assertTrue(frappe.db.exists("Payment Entry", pe))
		blockers = get_pm_request_cancel_blockers(req)
		self.assertTrue(any("submitted" in b.lower() and "payment entry" in b.lower() for b in blockers))

	# --- Clearance matrix (open process, not reservation) ---
	# Clearance insert requires submitted PE; cancel PE only while Clearance is non-reserving
	# (Draft), then set the target open status for the cancel check.

	def _req_with_open_clearance_after_pe_cancel(self, status: str) -> str:
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req, pe = _funded_request(emp, 40_000)
		cl = _make_clearance(emp, req, 4_000, submit=False)
		_cancel_pe(pe, req)
		if status == "Draft":
			_set_clearance_status(cl, "Draft", docstatus=0)
		else:
			_set_clearance_status(cl, status, docstatus=1)
		return req

	def test_cancel_blocked_draft_clearance(self):
		req = self._req_with_open_clearance_after_pe_cancel("Draft")
		_assert_blocked_with(self, req, "draft", "clearance")

	def test_cancel_blocked_pending_manager_clearance(self):
		req = self._req_with_open_clearance_after_pe_cancel("Pending Approval")
		_assert_blocked_with(self, req, "pending", "clearance")

	def test_cancel_blocked_pending_finance_clearance(self):
		req = self._req_with_open_clearance_after_pe_cancel("Pending Finance Review")
		_assert_blocked_with(self, req, "pending finance", "clearance")

	def test_cancel_blocked_approved_clearance(self):
		req = self._req_with_open_clearance_after_pe_cancel("Approved")
		_assert_blocked_with(self, req, "approved", "clearance")

	def test_cancel_blocked_pending_je_clearance(self):
		req = self._req_with_open_clearance_after_pe_cancel("Pending Journal Entry Submission")
		_assert_blocked_with(self, req, "pending journal", "clearance")

	def test_cancel_blocked_settled_clearance(self):
		req = self._req_with_open_clearance_after_pe_cancel("Settled")
		_assert_blocked_with(self, req, "settled", "clearance")

	def test_cancel_allowed_with_rejected_clearance_only(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req, pe = _funded_request(emp, 25_000)
		cl = _make_clearance(emp, req, 5_000, submit=True)
		_set_clearance_status(cl, "Rejected")  # terminal → PE cancel allowed; Request cancel allowed
		_cancel_pe(pe, req)
		assert_pm_request_cancel_allowed(req)
		frappe.get_doc("PM Request", req).cancel()
		self.assertEqual(cint(frappe.db.get_value("PM Request", req, "docstatus")), 2)
		self.assertEqual(frappe.db.get_value("PM Clearance", cl, "status"), "Rejected")
		self.assertEqual(cint(frappe.db.get_value("Payment Entry", pe, "docstatus")), 2)

	def test_cancel_allowed_with_cancelled_clearance_only(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req, pe = _funded_request(emp, 25_000)
		cl = _make_clearance(emp, req, 5_000, submit=True)
		_force_clearance_cancelled(cl)
		_cancel_pe(pe, req)
		assert_pm_request_cancel_allowed(req)
		frappe.get_doc("PM Request", req).cancel()
		self.assertEqual(cint(frappe.db.get_value("PM Request", req, "docstatus")), 2)
		self.assertEqual(cint(frappe.db.get_value("PM Clearance", cl, "docstatus")), 2)
		self.assertEqual(cint(frappe.db.get_value("Payment Entry", pe, "docstatus")), 2)

	# --- Mixed ---

	def test_cancel_cancelled_pe_rejected_clearance_allows(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req, pe = _funded_request(emp, 22_000)
		cl = _make_clearance(emp, req, 3_000, submit=True)
		_set_clearance_status(cl, "Rejected")
		_cancel_pe(pe, req)
		assert_pm_request_cancel_allowed(req)
		self.assertEqual(get_pm_request_cancel_blockers(req), [])

	def test_cancel_cancelled_pe_draft_clearance_blocks(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req, pe = _funded_request(emp, 22_000)
		_make_clearance(emp, req, 3_000, submit=False)
		_cancel_pe(pe, req)
		_assert_blocked_with(self, req, "draft", "clearance")

	def test_cancel_submitted_pe_rejected_clearance_blocks(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req, pe = _funded_request(emp, 28_000)
		cl = _make_clearance(emp, req, 3_000, submit=True)
		_set_clearance_status(cl, "Rejected")
		_assert_blocked_with(self, req, "submitted", "payment entry")
		self.assertEqual(cint(frappe.db.get_value("Payment Entry", pe, "docstatus")), 1)
		self.assertEqual(frappe.db.get_value("PM Clearance", cl, "status"), "Rejected")


	# --- Request-level Journal Entry (open process = submitted JE only) ---

	def test_cancel_blocked_submitted_request_journal_entry(self):
		_require_journal_entry_field(self)
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 7_500)
		je = _stub_journal_entry(company=tpm.COMPANY, docstatus=1)
		_link_request_journal_entry(req, je)
		_assert_blocked_with(self, req, "journal")
		self.assertEqual(cint(frappe.db.get_value("Journal Entry", je, "docstatus")), 1)

	def test_cancel_allowed_cancelled_request_journal_entry(self):
		_require_journal_entry_field(self)
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 7_600)
		je = _stub_journal_entry(company=tpm.COMPANY, docstatus=1)
		_link_request_journal_entry(req, je)
		frappe.db.set_value("Journal Entry", je, "docstatus", 2, update_modified=False)
		frappe.db.commit()
		assert_pm_request_cancel_allowed(req)
		frappe.get_doc("PM Request", req).cancel()
		self.assertEqual(cint(frappe.db.get_value("PM Request", req, "docstatus")), 2)

	def test_cancel_permission_accountant_has_docperm(self):
		acct = get_doctype_json_role_perm("PM Request", "Petty Management Accountant")
		self.assertTrue(cint(acct.cancel))

	def test_cancel_permission_user_lacks_docperm(self):
		user_perm = get_doctype_json_role_perm("PM Request", "Petty Management User")
		self.assertFalse(cint(user_perm.cancel))


class TestPmRequestDeleteEligibility(unittest.TestCase):
	"""Delete remains history-based; unchanged by Cancel open-process rule."""

	def setUp(self):
		frappe.set_user("Administrator")
		_require_site_ready(self)

	def _cancelled_clean_request(self) -> str:
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 8_000)
		frappe.get_doc("PM Request", req).cancel()
		frappe.db.commit()
		return req

	def test_delete_clean_cancelled(self):
		req = self._cancelled_clean_request()
		assert_pm_request_delete_allowed(req)
		frappe.delete_doc("PM Request", req, force=0)
		self.assertFalse(frappe.db.exists("PM Request", req))

	def test_delete_blocked_cancelled_with_cancelled_pe(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 9_000)
		pe = _create_funding_pe(req, 9_000)
		frappe.get_doc("Payment Entry", pe).cancel()
		_sync_funding_fields(req)
		frappe.get_doc("PM Request", req).cancel()
		with self.assertRaises(ValidationError) as ctx:
			assert_pm_request_delete_allowed(req)
		self.assertIn("payment entry", str(ctx.exception).lower())

	def test_delete_blocked_cancelled_with_submitted_pe(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 9_500)
		_create_funding_pe(req, 9_500)
		_sync_funding_fields(req)
		with self.assertRaises(ValidationError) as ctx:
			assert_pm_request_delete_allowed(req)
		self.assertIn("submitted", str(ctx.exception).lower())

	def test_delete_blocked_cancelled_with_draft_pe(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 8_500)
		try:
			_draft_pe(req, 1_000)
		except Exception:
			self.skipTest("Could not create draft PE (auto-submit?)")
		frappe.db.set_value("PM Request", req, {"docstatus": 2, "status": "Cancelled"}, update_modified=False)
		frappe.db.commit()
		with self.assertRaises(ValidationError) as ctx:
			assert_pm_request_delete_allowed(req)
		self.assertIn("payment entry", str(ctx.exception).lower())

	def test_delete_blocked_cancelled_with_clearance(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 11_000)
		pe = _create_funding_pe(req, 11_000)
		_sync_funding_fields(req)
		cl = _make_clearance(emp, req, 3_000, submit=False)
		_set_clearance_status(cl, "Rejected")
		frappe.get_doc("Payment Entry", pe).cancel()
		_sync_funding_fields(req)
		frappe.get_doc("PM Request", req).cancel()
		with self.assertRaises(ValidationError) as ctx:
			assert_pm_request_delete_allowed(req)
		self.assertIn("clearance", str(ctx.exception).lower())

	def test_delete_blocked_cancelled_with_cancelled_clearance(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 11_000)
		pe = _create_funding_pe(req, 11_000)
		_sync_funding_fields(req)
		cl = _make_clearance(emp, req, 3_000, submit=True)
		_force_clearance_cancelled(cl)
		frappe.get_doc("Payment Entry", pe).cancel()
		_sync_funding_fields(req)
		frappe.get_doc("PM Request", req).cancel()
		with self.assertRaises(ValidationError) as ctx:
			assert_pm_request_delete_allowed(req)
		self.assertIn("clearance", str(ctx.exception).lower())

	def test_delete_blocked_submitted(self):
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 5_000)
		with self.assertRaises(ValidationError) as ctx:
			assert_pm_request_delete_allowed(req)
		self.assertIn("submitted", str(ctx.exception).lower())

	def test_delete_clean_draft(self):
		tpm._ensure_petty_account()
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = frappe.new_doc("PM Request")
		req.company = tpm.COMPANY
		req.employee = emp
		req.transaction_date = today()
		req.append("details", {"advance_amount": 1_000})
		req.insert()
		name = req.name
		assert_pm_request_delete_allowed(name)
		frappe.delete_doc("PM Request", name)
		self.assertFalse(frappe.db.exists("PM Request", name))

	def test_delete_draft_blocked_with_clearance(self):
		tpm._ensure_petty_account()
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		draft = frappe.new_doc("PM Request")
		draft.company = tpm.COMPANY
		draft.employee = emp
		draft.transaction_date = today()
		draft.append("details", {"advance_amount": 2_000})
		draft.insert()
		sib = _new_submitted_request(emp, 20_000)
		pe = _create_funding_pe(sib, 20_000)
		_sync_funding_fields(sib)
		cl = _make_clearance(emp, sib, 2_000, submit=False)
		frappe.db.set_value(
			"PM Clearance Request Allocation",
			{"parent": cl, "pm_request": sib},
			"pm_request",
			draft.name,
			update_modified=False,
		)
		frappe.db.commit()
		with self.assertRaises(ValidationError) as ctx:
			assert_pm_request_delete_allowed(draft.name)
		self.assertIn("clearance", str(ctx.exception).lower())
		frappe.get_doc("Payment Entry", pe).cancel()

	def test_delete_draft_blocked_with_pe(self):
		from erpnext_extensions.petty_management.services.funding_queries import (
			list_payment_entries_for_pm_request,
		)

		tpm._ensure_petty_account()
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 6_000)
		pe = _create_funding_pe(req, 6_000)
		frappe.db.set_value("PM Request", req, "docstatus", 0, update_modified=False)
		frappe.db.commit()
		self.assertTrue(list_payment_entries_for_pm_request(req))
		with self.assertRaises(ValidationError) as ctx:
			assert_pm_request_delete_allowed(req)
		self.assertIn("payment entry", str(ctx.exception).lower())
		self.assertTrue(frappe.db.exists("Payment Entry", pe))


	def test_delete_blocked_cancelled_with_linked_journal_entry(self):
		"""Delete remains history-based: existing JE on Request still blocks (unchanged)."""
		_require_journal_entry_field(self)
		emp = tpm._make_employee()
		tpm._make_holder(emp)
		req = _new_submitted_request(emp, 7_700)
		je = _stub_journal_entry(company=tpm.COMPANY, docstatus=2)
		_link_request_journal_entry(req, je)
		assert_pm_request_cancel_allowed(req)
		frappe.get_doc("PM Request", req).cancel()
		with self.assertRaises(ValidationError) as ctx:
			assert_pm_request_delete_allowed(req)
		self.assertIn("journal", str(ctx.exception).lower())
		self.assertTrue(frappe.db.exists("Journal Entry", je))

	def test_delete_permission_accountant_lacks_docperm(self):
		acct = get_doctype_json_role_perm("PM Request", "Petty Management Accountant")
		self.assertFalse(cint(acct.delete))
		self.assertTrue(cint(acct.cancel))


if __name__ == "__main__":
	unittest.main()
