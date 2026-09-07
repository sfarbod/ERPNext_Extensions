# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.1.4 — PM Clearance Return deadlock vs live PI outstanding."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import cint, flt, today

from erpnext_extensions.petty_management.services.draft_approval_guards import (
	assert_pending_not_editable,
)
from erpnext_extensions.petty_management.services.purchase_invoice_readiness import (
	get_purchase_invoice_readiness,
	validate_purchase_invoices_for_prepare,
)
from erpnext_extensions.petty_management.services.workflow_utils import (
	apply_pm_workflow,
	resolve_workflow_state_link,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as pm_ct
from erpnext_extensions.petty_management.tests.test_pm_pending_remark_edit_v506 import (
	REVIEWER_ROLE,
	_ensure_user,
)


def _wf_title(link: str | None) -> str:
	if not link:
		return ""
	return (frappe.db.get_value("Workflow State", link, "workflow_state_name") or link or "").strip()


def _existing_submitted_pi(min_outstanding: float = 1.0) -> str | None:
	rows = frappe.db.sql(
		"""
		select name
		from `tabPurchase Invoice`
		where docstatus = 1 and ifnull(outstanding_amount, 0) >= %s
		order by modified desc
		limit 1
		""",
		(min_outstanding,),
		as_dict=True,
	)
	return rows[0].name if rows else None


class TestPMClearanceOutstandingStaleV514(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		pm_ct._ensure_company_context()
		if not pm_ct.COMPANY:
			raise unittest.SkipTest("No Company on site")
		from erpnext_extensions.patches.post_model_sync.migrate_pm_workflow_v402 import (
			_rebuild_pm_clearance_workflow,
			_rebuild_pm_request_workflow,
			_seed_assignment_rules,
		)

		_rebuild_pm_request_workflow()
		_rebuild_pm_clearance_workflow()
		_seed_assignment_rules()
		frappe.db.commit()

		cls.mgr = "pm_v514_mgr@example.com"
		cls.fin = "pm_v514_fin@example.com"
		cls.holder = "pm_v514_holder@example.com"
		cls.reviewer = "pm_v514_rev@example.com"
		desk = ["Accounts User", "Employee", "Desk User"]
		_ensure_user(cls.mgr, ["Petty Management User", "Expense Approver", *desk])
		_ensure_user(cls.fin, ["Petty Management Accountant", "Petty Management User", *desk])
		_ensure_user(cls.holder, ["Petty Management User", *desk])
		_ensure_user(cls.reviewer, [REVIEWER_ROLE, *desk])

		settings = frappe.get_single("PM Settings")
		settings.db_set("require_named_manager_approver", 1, update_modified=False)
		settings.db_set("finance_manager", cls.fin, update_modified=False)
		settings.db_set("clearance_finance_review_role", REVIEWER_ROLE, update_modified=False)

	def _pending_finance_clearance_with_amounts(
		self, allocated: float, live_outstanding: float, state: str = "Pending Finance Review"
	) -> tuple[str, str]:
		pi_name = _existing_submitted_pi()
		if not pi_name:
			raise unittest.SkipTest("No submitted Purchase Invoice on site")

		original_outstanding = flt(frappe.db.get_value("Purchase Invoice", pi_name, "outstanding_amount"))
		emp = pm_ct._make_employee()
		frappe.db.set_value("Employee", emp, "expense_approver", self.mgr, update_modified=False)
		pm_ct._make_holder(emp)
		frappe.db.set_value(
			"Purchase Invoice", pi_name, "outstanding_amount", allocated, update_modified=False
		)
		cl = frappe.new_doc("PM Clearance")
		cl.company = pm_ct.COMPANY
		cl.employee = emp
		cl.transaction_date = today()
		pm_ct._append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi_name,
				"allocated_amount": allocated,
				"outstanding_amount": allocated,
			},
		)
		req, _pe = pm_ct._fund_pm_request(emp, allocated)
		cl.append(
			"request_allocations",
			{
				"funding_source_type": "PM Request",
				"pm_request": req,
				"allocated_amount": allocated,
			},
		)
		cl.flags.ignore_mandatory = True
		cl.insert(ignore_permissions=True)
		frappe.db.set_value(
			"PM Clearance",
			cl.name,
			{
				"workflow_state": resolve_workflow_state_link(state),
				"owner": self.holder,
				"manager_approver": self.mgr,
			},
			update_modified=False,
		)
		frappe.db.set_value(
			"Purchase Invoice", pi_name, "outstanding_amount", live_outstanding, update_modified=False
		)
		self.addCleanup(
			lambda: frappe.db.set_value(
				"Purchase Invoice",
				pi_name,
				"outstanding_amount",
				original_outstanding,
				update_modified=False,
			)
		)
		return cl.name, pi_name

	def test_live_vs_child_snapshot_after_pi_drop(self):
		name, pi = self._pending_finance_clearance_with_amounts(10_000.0, 1_200.0)
		doc = frappe.get_doc("PM Clearance", name)
		self.assertEqual(flt(doc.details[0].outstanding_amount), 10_000.0)
		self.assertEqual(flt(doc.details[0].allocated_amount), 10_000.0)
		self.assertEqual(flt(frappe.db.get_value("Purchase Invoice", pi, "outstanding_amount")), 1_200.0)
		readiness = get_purchase_invoice_readiness(doc)
		self.assertFalse(readiness["ready"])
		self.assertIn("over_allocated", readiness["lines"][0]["issues"])
		self.assertEqual(flt(readiness["lines"][0]["outstanding_amount"]), 1_200.0)

	def test_draft_save_still_blocks_over_allocation(self):
		pi_name = _existing_submitted_pi()
		if not pi_name:
			raise unittest.SkipTest("No submitted Purchase Invoice on site")
		original = flt(frappe.db.get_value("Purchase Invoice", pi_name, "outstanding_amount"))
		frappe.db.set_value("Purchase Invoice", pi_name, "outstanding_amount", 1_000.0, update_modified=False)
		self.addCleanup(
			lambda: frappe.db.set_value(
				"Purchase Invoice", pi_name, "outstanding_amount", original, update_modified=False
			)
		)
		emp = pm_ct._make_employee()
		pm_ct._make_holder(emp)
		cl = frappe.new_doc("PM Clearance")
		cl.company = pm_ct.COMPANY
		cl.employee = emp
		cl.transaction_date = today()
		pm_ct._append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi_name,
				"allocated_amount": 5_000.0,
			},
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_purchase_invoices_for_prepare(cl)
		self.assertIn("cannot exceed Purchase Invoice outstanding", str(ctx.exception))
		self.assertEqual(flt(cl.details[0].outstanding_amount), 1_000.0)

	def test_pending_manager_remark_save_allowed_when_over_allocated(self):
		name, _pi = self._pending_finance_clearance_with_amounts(
			10_000.0, 1_200.0, state="Pending Manager Approval"
		)
		doc = frappe.get_doc("PM Clearance", name)
		doc.remark = "v514 manager remark while over allocated"
		doc.save()
		out = doc.reload()
		self.assertEqual(out.remark, "v514 manager remark while over allocated")
		self.assertEqual(flt(out.details[0].outstanding_amount), 1_200.0)

	def test_pending_remark_save_allowed_when_over_allocated(self):
		name, _pi = self._pending_finance_clearance_with_amounts(10_000.0, 1_200.0)
		doc = frappe.get_doc("PM Clearance", name)
		doc.remark = "v514 remark while over allocated"
		doc.save()
		out = doc.reload()
		self.assertEqual(out.remark, "v514 remark while over allocated")
		self.assertEqual(flt(out.details[0].outstanding_amount), 1_200.0)
		self.assertEqual(flt(out.details[0].allocated_amount), 10_000.0)

	def test_illegal_financial_edit_while_pending_still_blocked(self):
		name, _pi = self._pending_finance_clearance_with_amounts(10_000.0, 1_200.0)
		doc = frappe.get_doc("PM Clearance", name)
		doc.details[0].allocated_amount = 1_200.0
		with self.assertRaises(frappe.ValidationError) as ctx:
			assert_pending_not_editable(doc)
		self.assertIn("Only Remarks", str(ctx.exception))
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_return_for_correction_not_blocked_by_over_allocation(self):
		name, _pi = self._pending_finance_clearance_with_amounts(10_000.0, 1_200.0)
		doc = frappe.get_doc("PM Clearance", name)
		# Administrator can write; gate under test is prepare over-allocation, not role ACL.
		frappe.set_user("Administrator")
		out = apply_pm_workflow(doc, "PM Return for Correction")
		self.assertEqual(_wf_title(out.workflow_state), "Draft")
		out = out.reload()
		self.assertEqual(flt(out.details[0].outstanding_amount), 1_200.0)

	def test_finance_approve_still_blocked_when_over_allocation(self):
		name, _pi = self._pending_finance_clearance_with_amounts(10_000.0, 1_200.0)
		doc = frappe.get_doc("PM Clearance", name)
		frappe.set_user(self.reviewer)
		with self.assertRaises(frappe.ValidationError) as ctx:
			apply_pm_workflow(doc, "PM Finance Approve")
		frappe.set_user("Administrator")
		self.assertIn("outstanding", str(ctx.exception).lower())
		self.assertEqual(
			_wf_title(frappe.db.get_value("PM Clearance", name, "workflow_state")),
			"Pending Finance Review",
		)

	def test_prepare_stamps_live_outstanding_on_gate(self):
		name, _pi = self._pending_finance_clearance_with_amounts(10_000.0, 1_200.0)
		frappe.db.set_value(
			"PM Clearance",
			name,
			"workflow_state",
			resolve_workflow_state_link("Draft"),
			update_modified=False,
		)
		doc = frappe.get_doc("PM Clearance", name)
		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_purchase_invoices_for_prepare(doc)
		self.assertIn("1200", str(ctx.exception).replace(",", ""))
		self.assertEqual(flt(doc.details[0].outstanding_amount), 1_200.0)

	def test_return_fix_resubmit_finance_approve(self):
		"""Return → requester fixes allocation → resubmit → manager → finance approve."""
		name, _pi = self._pending_finance_clearance_with_amounts(10_000.0, 1_200.0)
		frappe.set_user("Administrator")
		doc = apply_pm_workflow(frappe.get_doc("PM Clearance", name), "PM Return for Correction")
		self.assertEqual(_wf_title(doc.workflow_state), "Draft")

		doc = frappe.get_doc("PM Clearance", name)
		doc.details[0].allocated_amount = 1_200.0
		doc.request_allocations[0].allocated_amount = 1_200.0
		doc.save()
		doc = doc.reload()
		self.assertEqual(flt(doc.details[0].allocated_amount), 1_200.0)
		self.assertEqual(flt(doc.details[0].outstanding_amount), 1_200.0)

		doc = apply_pm_workflow(doc, "PM Submit Finance Review")
		self.assertEqual(_wf_title(doc.workflow_state), "Pending Manager Approval")

		frappe.set_user(self.mgr)
		doc = apply_pm_workflow(frappe.get_doc("PM Clearance", name), "PM Manager Approve")
		frappe.set_user("Administrator")
		self.assertEqual(_wf_title(doc.workflow_state), "Pending Finance Review")

		frappe.set_user(self.reviewer)
		doc = apply_pm_workflow(frappe.get_doc("PM Clearance", name), "PM Finance Approve")
		frappe.set_user("Administrator")
		# Clearance Finance Approve lands on Approved (docstatus 1).
		self.assertEqual(_wf_title(doc.workflow_state), "Approved")
		self.assertEqual(cint(doc.docstatus), 1)

	def test_pm_request_return_unaffected_by_clearance_flag(self):
		"""PM Request Return must not depend on Clearance over-allocation flag."""
		frappe.set_user("Administrator")
		emp = pm_ct._make_employee()
		frappe.db.set_value("Employee", emp, "expense_approver", self.mgr, update_modified=False)
		pm_ct._make_holder(emp)
		req = frappe.new_doc("PM Request")
		req.company = pm_ct.COMPANY
		req.employee = emp
		req.transaction_date = today()
		req.append("details", {"advance_amount": 5_000.0})
		req.insert(ignore_permissions=True)
		frappe.db.set_value(
			"PM Request",
			req.name,
			{
				"workflow_state": resolve_workflow_state_link("Pending Manager Approval"),
				"manager_approver": self.mgr,
			},
			update_modified=False,
		)
		# Flag must stay Clearance-scoped; Request Return still transitions.
		frappe.flags.pm_return_for_correction = False
		frappe.set_user(self.mgr)
		out = apply_pm_workflow(frappe.get_doc("PM Request", req.name), "PM Return for Correction")
		frappe.set_user("Administrator")
		self.assertEqual(_wf_title(out.workflow_state), "Draft")
		self.assertFalse(bool(getattr(frappe.flags, "pm_return_for_correction", False)))
