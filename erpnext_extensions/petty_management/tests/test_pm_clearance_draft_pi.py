# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.1.5: Draft Purchase Invoice on PM Clearance — prepare allowed, Finance/Settle blocked."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import cint, flt, today

from erpnext_extensions.petty_management.services.clearance_finance_review import (
	DEFAULT_CLEARANCE_FINANCE_REVIEW_ROLE,
)
from erpnext_extensions.petty_management.services.settlement_query import (
	purchase_invoice_query_for_pm_clearance,
)
from erpnext_extensions.petty_management.services.workflow_utils import apply_pm_workflow
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm


def _wf_title(link: str | None) -> str:
	if not link:
		return ""
	return (frappe.db.get_value("Workflow State", link, "workflow_state_name") or link or "").strip()


def _ensure_user(email: str, roles: list[str]) -> str:
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0][:30],
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		)
		u.insert(ignore_permissions=True)
		u.new_password = "pm_sec_test_1"
		u.save(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	have = {r.role for r in user.roles}
	for role in roles:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)
		if role not in have:
			user.append("roles", {"role": role})
	user.save(ignore_permissions=True)
	frappe.db.commit()
	return email


def _open_workflow_actions(cl_name: str) -> list[dict]:
	actions = frappe.get_all(
		"Workflow Action",
		filters={
			"reference_doctype": "PM Clearance",
			"reference_name": cl_name,
			"status": "Open",
		},
		fields=["name", "status", "user", "workflow_state"],
	)
	for row in actions:
		row["permitted_roles"] = frappe.get_all(
			"Workflow Action Permitted Role",
			filters={"parent": row["name"]},
			pluck="role",
		)
	return actions


def _finance_assignment_todos(cl_name: str) -> list[dict]:
	return frappe.get_all(
		"ToDo",
		filters={
			"reference_type": "PM Clearance",
			"reference_name": cl_name,
			"status": "Open",
			"assignment_rule": "PM Clearance Finance Review",
		},
		fields=["name", "allocated_to", "assignment_rule"],
	)


def _open_finance_todos(cl_name: str) -> list[dict]:
	return _finance_assignment_todos(cl_name)

def _pi_names_in_lookup(txt: str = "") -> set[str]:
	rows = purchase_invoice_query_for_pm_clearance(
		"Purchase Invoice",
		txt,
		"name",
		0,
		50,
		{"company": tpm.COMPANY},
	)
	return {r[0] for r in rows}


class TestPMClearanceDraftPI(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		tpm._ensure_company_context()
		if not tpm.COMPANY:
			raise unittest.SkipTest("No Company")
		tpm._ensure_petty_account()
		from erpnext_extensions.patches.post_model_sync.migrate_pm_clearance_finance_role_queue_v453 import (
			execute as migrate_clearance_finance_role_queue_v453,
		)

		migrate_clearance_finance_role_queue_v453()
		cls.manager = _ensure_user(
			"pm_draft_pi_mgr@example.com",
			["Petty Management User", "Expense Approver", "Accounts User", "System Manager"],
		)
		cls.finance = _ensure_user(
			"pm_draft_pi_fin@example.com",
			[
				"Petty Management Accountant",
				DEFAULT_CLEARANCE_FINANCE_REVIEW_ROLE,
				"Accounts User",
				"System Manager",
			],
		)
		settings = frappe.get_single("PM Settings")
		settings.db_set("finance_manager", cls.finance, update_modified=False)
		settings.db_set("finance_supervisor", cls.finance, update_modified=False)
		settings.db_set(
			"clearance_finance_review_role", DEFAULT_CLEARANCE_FINANCE_REVIEW_ROLE, update_modified=False
		)
		settings.db_set("require_named_manager_approver", 1, update_modified=False)
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		self._created: list[tuple[str, str]] = []

	def tearDown(self):
		frappe.set_user("Administrator")
		for dt, name in reversed(self._created):
			try:
				if not frappe.db.exists(dt, name):
					continue
				doc = frappe.get_doc(dt, name)
				if cint(doc.docstatus) == 1:
					doc.cancel()
				elif cint(doc.docstatus) == 0:
					frappe.delete_doc(dt, name, force=1)
			except Exception:
				pass
		frappe.db.commit()

	def _track(self, dt: str, name: str) -> None:
		self._created.append((dt, name))

	def _funded_holder(self, amount: float = 50_000.0) -> tuple[str, str]:
		emp = tpm._make_employee()
		frappe.db.set_value("Employee", emp, "expense_approver", self.manager, update_modified=False)
		tpm._make_holder(emp)
		req, pe = tpm._fund_pm_request(emp, amount)
		self._track("Payment Entry", pe)
		self._track("PM Request", req)
		self._track("Employee", emp)
		# Finance-cleared request for allocation
		from erpnext_extensions.petty_management.services.workflow_utils import resolve_workflow_state_link

		fa = resolve_workflow_state_link("Finance Approved")
		frappe.db.set_value(
			"PM Request",
			req,
			{"workflow_state": fa, "status": "Paid", "payment_status": "Paid"},
			update_modified=False,
		)
		return emp, req

	def _insert_draft_pi(self, amount: float = 5_000.0) -> str:
		pi = tpm._make_pi_outstanding(amount)
		pi.insert(ignore_permissions=True)
		self._track("Purchase Invoice", pi.name)
		self.assertEqual(cint(pi.docstatus), 0)
		return pi.name

	def _insert_submitted_pi(self, amount: float = 5_000.0) -> str:
		name = self._insert_draft_pi(amount)
		pi = frappe.get_doc("Purchase Invoice", name)
		pi.submit()
		return name

	def _new_clearance_with_pi(self, emp: str, req: str, pi_name: str, allocated: float | None = None) -> str:
		pi = frappe.get_doc("Purchase Invoice", pi_name)
		alloc = flt(allocated)
		if alloc <= 0:
			if cint(pi.docstatus) == 1:
				alloc = flt(pi.outstanding_amount)
			else:
				alloc = flt(pi.grand_total or pi.rounded_total or 0)
		cl = frappe.new_doc("PM Clearance")
		cl.company = tpm.COMPANY
		cl.employee = emp
		cl.posting_date = today()
		cl.transaction_date = today()
		tpm._append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi_name,
				"allocated_amount": alloc,
			},
		)
		cl.append(
			"request_allocations",
			{
				"funding_source_type": "PM Request",
				"pm_request": req,
				"allocated_amount": alloc,
			},
		)
		cl.insert(ignore_permissions=True)
		self._track("PM Clearance", cl.name)
		return cl.name

	def _assert_finance_role_queue(self, cl_name: str) -> None:
		"""v4.5.3: role Workflow Action queue — no finance Assignment Rule ToDos."""
		actions = _open_workflow_actions(cl_name)
		self.assertTrue(actions, msg="Expected open Workflow Action for finance review")
		review_role = (
			frappe.db.get_value("PM Settings", "PM Settings", "clearance_finance_review_role")
			or DEFAULT_CLEARANCE_FINANCE_REVIEW_ROLE
		)
		self.assertTrue(
			any(review_role in (a.get("permitted_roles") or []) for a in actions),
			msg=f"Expected {review_role} on Workflow Action: {actions}",
		)
		self.assertFalse(_finance_assignment_todos(cl_name), msg="Finance Assignment Rule ToDos must be absent")
		from erpnext_extensions.petty_management.services.workflow_utils import get_allowed_workflow_actions

		frappe.set_user(self.finance)
		try:
			doc = frappe.get_doc("PM Clearance", cl_name)
			allowed = {t.get("action") for t in get_allowed_workflow_actions(doc) if t.get("action")}
			self.assertIn("PM Finance Approve", allowed, msg=f"Finance user actions: {allowed}")
		finally:
			frappe.set_user("Administrator")

	def _to_pending_finance(self, cl_name: str) -> None:
		cl = frappe.get_doc("PM Clearance", cl_name)
		frappe.db.set_value(
			"PM Clearance",
			cl_name,
			{"manager_approver": self.manager, "finance_approver": None},
			update_modified=False,
		)
		if _wf_title(cl.workflow_state) in ("", "Draft"):
			apply_pm_workflow(frappe.get_doc("PM Clearance", cl_name), "PM Submit Finance Review")
		frappe.set_user(self.manager)
		cl = frappe.get_doc("PM Clearance", cl_name)
		if _wf_title(cl.workflow_state) == "Pending Manager Approval":
			apply_pm_workflow(cl, "PM Manager Approve")
		frappe.set_user("Administrator")
		cl = frappe.get_doc("PM Clearance", cl_name)
		self.assertEqual(_wf_title(cl.workflow_state), "Pending Finance Review")
		self.assertEqual((cl.status or "").strip(), "Pending Approval")
		self.assertFalse((cl.finance_approver or "").strip())
		self._assert_finance_role_queue(cl_name)

	# --- Lookup ---

	def test_draft_pi_appears_in_lookup(self):
		pi = self._insert_draft_pi(3_333)
		names = _pi_names_in_lookup(pi)
		self.assertIn(pi, names)

	def test_submitted_pi_with_outstanding_in_lookup(self):
		pi = self._insert_submitted_pi(4_444)
		names = _pi_names_in_lookup(pi)
		self.assertIn(pi, names)

	def test_cancelled_pi_excluded_from_lookup(self):
		pi = self._insert_submitted_pi(2_222)
		doc = frappe.get_doc("Purchase Invoice", pi)
		doc.cancel()
		names = _pi_names_in_lookup(pi)
		self.assertNotIn(pi, names)

	def test_submitted_zero_outstanding_excluded(self):
		"""Submitted PI with zero outstanding remains non-allocatable."""
		pi_name = self._insert_submitted_pi(1_500)
		# Force outstanding to zero without full payment machinery where possible
		frappe.db.set_value("Purchase Invoice", pi_name, "outstanding_amount", 0, update_modified=False)
		names = _pi_names_in_lookup(pi_name)
		self.assertNotIn(pi_name, names)

	# --- Prepare path ---

	def test_draft_pi_can_save_clearance(self):
		emp, req = self._funded_holder()
		pi = self._insert_draft_pi(5_000)
		cl_name = self._new_clearance_with_pi(emp, req, pi)
		cl = frappe.get_doc("PM Clearance", cl_name)
		self.assertEqual(cint(cl.docstatus), 0)
		self.assertEqual(cl.details[0].purchase_invoice, pi)

	def test_draft_pi_clearance_submit_and_manager_approve(self):
		emp, req = self._funded_holder()
		pi = self._insert_draft_pi(6_000)
		cl_name = self._new_clearance_with_pi(emp, req, pi)
		self._to_pending_finance(cl_name)

	# --- Finance block ---

	def test_finance_approval_blocked_with_draft_pi(self):
		emp, req = self._funded_holder()
		pi = self._insert_draft_pi(7_000)
		cl_name = self._new_clearance_with_pi(emp, req, pi)
		self._to_pending_finance(cl_name)
		frappe.set_user(self.finance)
		with self.assertRaises(frappe.ValidationError) as ctx:
			apply_pm_workflow(frappe.get_doc("PM Clearance", cl_name), "PM Finance Approve")
		msg = str(ctx.exception)
		self.assertIn("not submitted", msg.lower())
		self.assertIn(pi, msg)
		frappe.set_user("Administrator")
		cl = frappe.get_doc("PM Clearance", cl_name)
		self.assertEqual(_wf_title(cl.workflow_state), "Pending Finance Review")
		self.assertEqual((cl.status or "").strip(), "Pending Approval")

	def test_finance_block_message_lists_pi_and_keeps_workflow_action(self):
		emp, req = self._funded_holder()
		pi = self._insert_draft_pi(8_000)
		cl_name = self._new_clearance_with_pi(emp, req, pi)
		self._to_pending_finance(cl_name)
		self._assert_finance_role_queue(cl_name)
		frappe.set_user(self.finance)
		with self.assertRaises(frappe.ValidationError) as ctx:
			apply_pm_workflow(frappe.get_doc("PM Clearance", cl_name), "PM Finance Approve")
		msg = str(ctx.exception)
		self.assertIn(pi, msg)
		self.assertIn("Cannot complete Finance Approval", msg)
		frappe.set_user("Administrator")
		self._assert_finance_role_queue(cl_name)
		cl = frappe.get_doc("PM Clearance", cl_name)
		self.assertEqual(_wf_title(cl.workflow_state), "Pending Finance Review")

	def test_multiple_pi_one_draft_blocks_finance(self):
		emp, req = self._funded_holder(80_000)
		pi_ok = self._insert_submitted_pi(3_000)
		pi_draft = self._insert_draft_pi(4_000)
		cl = frappe.new_doc("PM Clearance")
		cl.company = tpm.COMPANY
		cl.employee = emp
		cl.posting_date = today()
		cl.transaction_date = today()
		for pi_name, amt in ((pi_ok, 3_000), (pi_draft, 4_000)):
			tpm._append_pm_clearance_detail_row(
				cl,
				{
					"settlement_type": "Purchase Invoice",
					"purchase_invoice": pi_name,
					"allocated_amount": amt,
				},
			)
		cl.append(
			"request_allocations",
			{"funding_source_type": "PM Request", "pm_request": req, "allocated_amount": 7_000},
		)
		cl.insert(ignore_permissions=True)
		self._track("PM Clearance", cl.name)
		self._to_pending_finance(cl.name)
		frappe.set_user(self.finance)
		with self.assertRaises(frappe.ValidationError) as ctx:
			apply_pm_workflow(frappe.get_doc("PM Clearance", cl.name), "PM Finance Approve")
		self.assertIn(pi_draft, str(ctx.exception))

	def test_submit_draft_pi_then_finance_succeeds(self):
		emp, req = self._funded_holder()
		pi = self._insert_draft_pi(5_500)
		cl_name = self._new_clearance_with_pi(emp, req, pi)
		self._to_pending_finance(cl_name)
		frappe.set_user("Administrator")
		frappe.get_doc("Purchase Invoice", pi).submit()
		# Refresh snapshot fields that prepare may have stamped from grand_total
		pi_doc = frappe.get_doc("Purchase Invoice", pi)
		cl = frappe.get_doc("PM Clearance", cl_name)
		for row in cl.details:
			if row.purchase_invoice == pi:
				row.outstanding_amount = flt(pi_doc.outstanding_amount)
				if flt(row.allocated_amount) > flt(pi_doc.outstanding_amount):
					row.allocated_amount = flt(pi_doc.outstanding_amount)
		cl.save(ignore_permissions=True)
		frappe.set_user(self.finance)
		apply_pm_workflow(frappe.get_doc("PM Clearance", cl_name), "PM Finance Approve")
		cl = frappe.get_doc("PM Clearance", cl_name)
		self.assertEqual(_wf_title(cl.workflow_state), "Approved")
		self.assertEqual((cl.status or "").strip(), "Approved")

	# --- Settlement / preview ---

	def test_settle_and_preview_blocked_with_draft_pi(self):
		from erpnext_extensions.petty_management.services.journal_entry_service import settle_petty_cash
		from erpnext_extensions.petty_management.services.preview_service import preview_pm_clearance_settlement

		emp, req = self._funded_holder()
		pi = self._insert_draft_pi(4_500)
		cl_name = self._new_clearance_with_pi(emp, req, pi)
		self._to_pending_finance(cl_name)
		# Force Approved workflow without going through finance PI guard (bypass simulation)
		from erpnext_extensions.petty_management.services.workflow_utils import resolve_workflow_state_link

		approved = resolve_workflow_state_link("Approved")
		frappe.db.set_value(
			"PM Clearance",
			cl_name,
			{"workflow_state": approved, "status": "Approved"},
			update_modified=False,
		)
		with self.assertRaises(frappe.ValidationError):
			preview_pm_clearance_settlement(pm_clearance=cl_name)
		with self.assertRaises(frappe.ValidationError):
			settle_petty_cash(cl_name)

	def test_je_debit_line_blocked_for_draft_pi(self):
		from erpnext_extensions.petty_management.services.journal_entry_service import (
			build_purchase_invoice_debit_line,
		)

		emp, req = self._funded_holder()
		pi = self._insert_draft_pi(2_000)
		cl_name = self._new_clearance_with_pi(emp, req, pi)
		cl = frappe.get_doc("PM Clearance", cl_name)
		row = cl.details[0]
		with self.assertRaises(frappe.ValidationError):
			build_purchase_invoice_debit_line(row, flt(row.allocated_amount))

	# --- Amount drift ---

	def test_finance_blocks_over_allocation_and_does_not_rewrite_allocated(self):
		emp, req = self._funded_holder()
		pi = self._insert_draft_pi(10_000)
		cl_name = self._new_clearance_with_pi(emp, req, pi, allocated=10_000)
		self._to_pending_finance(cl_name)
		frappe.get_doc("Purchase Invoice", pi).submit()
		# Shrink outstanding below allocated without touching clearance allocated_amount
		frappe.db.set_value("Purchase Invoice", pi, "outstanding_amount", 1_000, update_modified=False)
		cl = frappe.get_doc("PM Clearance", cl_name)
		before_alloc = flt(cl.details[0].allocated_amount)
		frappe.set_user(self.finance)
		with self.assertRaises(frappe.ValidationError):
			apply_pm_workflow(frappe.get_doc("PM Clearance", cl_name), "PM Finance Approve")
		cl = frappe.get_doc("PM Clearance", cl_name)
		self.assertEqual(flt(cl.details[0].allocated_amount), before_alloc)

	def test_finance_blocks_supplier_drift(self):
		emp, req = self._funded_holder()
		pi = self._insert_draft_pi(3_000)
		cl_name = self._new_clearance_with_pi(emp, req, pi)
		self._to_pending_finance(cl_name)
		frappe.get_doc("Purchase Invoice", pi).submit()
		# Stamp a different supplier on the clearance row (stale snapshot)
		other = frappe.get_all("Supplier", filters={"name": ("!=", frappe.db.get_value("Purchase Invoice", pi, "supplier"))}, pluck="name", limit=1)
		if not other:
			self.skipTest("Need a second Supplier for drift test")
		frappe.db.set_value(
			"PM Clearance Detail",
			frappe.get_doc("PM Clearance", cl_name).details[0].name,
			"supplier",
			other[0],
			update_modified=False,
		)
		frappe.set_user(self.finance)
		with self.assertRaises(frappe.ValidationError) as ctx:
			apply_pm_workflow(frappe.get_doc("PM Clearance", cl_name), "PM Finance Approve")
		self.assertTrue("supplier" in str(ctx.exception).lower() or "changed" in str(ctx.exception).lower())

	def test_cancelled_pi_blocked_on_prepare(self):
		emp, req = self._funded_holder()
		pi = self._insert_submitted_pi(2_500)
		frappe.get_doc("Purchase Invoice", pi).cancel()
		with self.assertRaises(frappe.ValidationError):
			self._new_clearance_with_pi(emp, req, pi, allocated=2_500)

	def test_full_lifecycle_draft_then_submit_then_settle(self):
		from erpnext_extensions.petty_management.services.journal_entry_service import settle_petty_cash
		from erpnext_extensions.petty_management.services.preview_service import preview_pm_clearance_settlement

		emp, req = self._funded_holder()
		pi = self._insert_draft_pi(5_000)
		cl_name = self._new_clearance_with_pi(emp, req, pi)
		self._to_pending_finance(cl_name)
		self._assert_finance_role_queue(cl_name)
		frappe.set_user(self.finance)
		with self.assertRaises(frappe.ValidationError):
			apply_pm_workflow(frappe.get_doc("PM Clearance", cl_name), "PM Finance Approve")
		frappe.set_user("Administrator")
		self._assert_finance_role_queue(cl_name)
		frappe.get_doc("Purchase Invoice", pi).submit()
		pi_doc = frappe.get_doc("Purchase Invoice", pi)
		cl = frappe.get_doc("PM Clearance", cl_name)
		for row in cl.details:
			row.outstanding_amount = flt(pi_doc.outstanding_amount)
			if flt(row.allocated_amount) > flt(pi_doc.outstanding_amount):
				row.allocated_amount = flt(pi_doc.outstanding_amount)
		cl.save(ignore_permissions=True)
		frappe.set_user(self.finance)
		apply_pm_workflow(frappe.get_doc("PM Clearance", cl_name), "PM Finance Approve")
		cl = frappe.get_doc("PM Clearance", cl_name)
		self.assertEqual(_wf_title(cl.workflow_state), "Approved")
		preview = preview_pm_clearance_settlement(pm_clearance=cl_name)
		self.assertTrue(preview.get("is_balanced") or preview.get("accounts"))
		out = settle_petty_cash(cl_name)
		self.assertTrue(out.get("journal_entry"))
		self._track("Journal Entry", out["journal_entry"])
		cl.reload()
		self.assertIn(cl.status, ("Pending Journal Entry Submission", "Settled"))

	def test_approve_for_reservation_blocked_with_draft_pi(self):
		"""Bypass helper must use the same Finance PI readiness gate."""
		from erpnext_extensions.petty_management.services.allocation_service import (
			sum_prior_pm_request_allocations,
		)
		from erpnext_extensions.petty_management.services.clearance_service import (
			approve_pm_clearance_for_reservation,
		)
		from erpnext_extensions.petty_management.doctype.pm_clearance.pm_clearance import (
			approve_pm_clearance_for_settlement,
		)

		emp, req = self._funded_holder()
		pi = self._insert_draft_pi(6_000)
		cl_name = self._new_clearance_with_pi(emp, req, pi)
		self._to_pending_finance(cl_name)
		self._assert_finance_role_queue(cl_name)
		reserved_before = flt(sum_prior_pm_request_allocations(req, exclude_clearance_name=None))

		with self.assertRaises(frappe.ValidationError) as ctx1:
			approve_pm_clearance_for_reservation(cl_name)
		self.assertIn(pi, str(ctx1.exception))
		self.assertIn("Cannot complete Finance Approval", str(ctx1.exception))

		with self.assertRaises(frappe.ValidationError) as ctx2:
			approve_pm_clearance_for_settlement(cl_name)
		self.assertIn(pi, str(ctx2.exception))

		cl = frappe.get_doc("PM Clearance", cl_name)
		self.assertEqual(_wf_title(cl.workflow_state), "Pending Finance Review")
		self.assertEqual((cl.status or "").strip(), "Pending Approval")
		self._assert_finance_role_queue(cl_name)
		reserved_after = flt(sum_prior_pm_request_allocations(req, exclude_clearance_name=None))
		self.assertEqual(reserved_after, reserved_before)

		# After PI submit, Finance Approve via legitimate workflow (role queue user).
		frappe.get_doc("Purchase Invoice", pi).submit()
		pi_doc = frappe.get_doc("Purchase Invoice", pi)
		cl = frappe.get_doc("PM Clearance", cl_name)
		for row in cl.details:
			row.outstanding_amount = flt(pi_doc.outstanding_amount)
		cl.save(ignore_permissions=True)
		frappe.set_user(self.finance)
		approve_pm_clearance_for_reservation(cl_name)
		frappe.set_user("Administrator")
		cl = frappe.get_doc("PM Clearance", cl_name)
		self.assertEqual(_wf_title(cl.workflow_state), "Approved")
		self.assertEqual((cl.status or "").strip(), "Approved")
		self.assertEqual(cint(cl.docstatus), 1)
		self.assertGreater(
			flt(sum_prior_pm_request_allocations(req, exclude_clearance_name=None)),
			reserved_before,
		)
