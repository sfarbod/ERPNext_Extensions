# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.11 — Return for Correction vs PM Request availability deadlock."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt, today

from erpnext_extensions.petty_management.services.allocation_service import (
	get_pm_request_allocation_context,
	stamp_allocation_snapshot,
	validate_request_allocations,
)
from erpnext_extensions.petty_management.services.clearance_service import validate_clearance
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


class TestPmClearanceReturnFundingAvailabilityV5211(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		pm_ct._ensure_company_context()
		if not pm_ct.COMPANY:
			raise unittest.SkipTest("No Company on site")
		from erpnext_extensions.patches.post_model_sync.migrate_pm_workflow_v402 import (
			_rebuild_pm_clearance_workflow,
			_seed_assignment_rules,
		)

		_rebuild_pm_clearance_workflow()
		_seed_assignment_rules()
		frappe.db.commit()

		cls.mgr = "pm_v5211_mgr@example.com"
		cls.fin = "pm_v5211_fin@example.com"
		cls.holder = "pm_v5211_holder@example.com"
		cls.reviewer = "pm_v5211_rev@example.com"
		desk = ["Accounts User", "Employee", "Desk User"]
		_ensure_user(cls.mgr, ["Petty Management User", "Expense Approver", *desk])
		_ensure_user(cls.fin, ["Petty Management Accountant", "Petty Management User", *desk])
		_ensure_user(cls.holder, ["Petty Management User", *desk])
		_ensure_user(cls.reviewer, [REVIEWER_ROLE, *desk])

		settings = frappe.get_single("PM Settings")
		settings.db_set("require_named_manager_approver", 1, update_modified=False)
		settings.db_set("finance_manager", cls.fin, update_modified=False)
		settings.db_set("clearance_finance_review_role", REVIEWER_ROLE, update_modified=False)
		settings.db_set("allow_negative_balance", 0, update_modified=False)

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.flags.pm_return_for_correction = False
		frappe.flags.in_pm_workflow_apply = False

	def _pending_over_allocated_vs_request(self, funded: float = 10_000.0, reserved: float = 4_000.0):
		"""Pending Finance clearance allocating ``funded`` while ``reserved`` is already settled."""
		frappe.set_user("Administrator")
		pi_name = _existing_submitted_pi()
		if not pi_name:
			raise unittest.SkipTest("No submitted Purchase Invoice on site")
		original_outstanding = flt(frappe.db.get_value("Purchase Invoice", pi_name, "outstanding_amount"))
		emp = pm_ct._make_employee()
		frappe.db.set_value("Employee", emp, "expense_approver", self.mgr, update_modified=False)
		pm_ct._make_holder(emp)

		frappe.db.set_value(
			"Purchase Invoice", pi_name, "outstanding_amount", funded, update_modified=False
		)
		req, _pe = pm_ct._fund_pm_request(emp, funded)

		# Settled prior reservation against the same request
		cl_settled = frappe.new_doc("PM Clearance")
		cl_settled.company = pm_ct.COMPANY
		cl_settled.employee = emp
		cl_settled.transaction_date = today()
		pm_ct._append_pm_clearance_detail_row(
			cl_settled,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi_name,
				"allocated_amount": reserved,
				"outstanding_amount": reserved,
			},
		)
		cl_settled.append(
			"request_allocations",
			{
				"funding_source_type": "PM Request",
				"pm_request": req,
				"allocated_amount": reserved,
			},
		)
		cl_settled.flags.ignore_mandatory = True
		cl_settled.insert(ignore_permissions=True)
		frappe.db.set_value(
			"PM Clearance",
			cl_settled.name,
			{
				"workflow_state": resolve_workflow_state_link("Approved"),
				"status": "Settled",
				"docstatus": 1,
			},
			update_modified=False,
		)

		# Pending clearance initially valid headroom, then force over-allocation
		headroom = funded - reserved
		cl = frappe.new_doc("PM Clearance")
		cl.company = pm_ct.COMPANY
		cl.employee = emp
		cl.transaction_date = today()
		pm_ct._append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi_name,
				"allocated_amount": headroom,
				"outstanding_amount": funded,
			},
		)
		cl.append(
			"request_allocations",
			{
				"funding_source_type": "PM Request",
				"pm_request": req,
				"allocated_amount": headroom,
			},
		)
		cl.flags.ignore_mandatory = True
		cl.insert(ignore_permissions=True)
		frappe.db.set_value(
			"PM Clearance",
			cl.name,
			{
				"workflow_state": resolve_workflow_state_link("Pending Finance Review"),
				"owner": self.holder,
				"manager_approver": self.mgr,
				"total_expense_amount": funded,
			},
			update_modified=False,
		)
		# Force over-allocation on child + parent totals
		frappe.db.sql(
			"""
			update `tabPM Clearance Detail`
			set allocated_amount=%s, outstanding_amount=%s
			where parent=%s and parenttype='PM Clearance'
			""",
			(funded, funded, cl.name),
		)
		frappe.db.sql(
			"""
			update `tabPM Clearance Request Allocation`
			set allocated_amount=%s
			where parent=%s and parenttype='PM Clearance' and parentfield='request_allocations'
			""",
			(funded, cl.name),
		)
		frappe.db.set_value(
			"Purchase Invoice", pi_name, "outstanding_amount", funded, update_modified=False
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
		frappe.db.commit()
		ctx = get_pm_request_allocation_context(req, pm_clearance=cl.name)
		self.assertLess(flt(ctx["available_amount"]), funded)
		return cl.name, req, funded, flt(ctx["available_amount"])

	def test_a_return_succeeds_when_over_allocated_vs_request(self):
		name, req, funded, available = self._pending_over_allocated_vs_request()
		doc = frappe.get_doc("PM Clearance", name)
		frappe.set_user("Administrator")
		out = apply_pm_workflow(doc, "PM Return for Correction")
		self.assertEqual(out.name, name)
		self.assertEqual(_wf_title(out.workflow_state), "Draft")
		out = out.reload()
		self.assertEqual(flt(out.request_allocations[0].available_amount), available)
		self.assertEqual(flt(out.request_allocations[0].allocated_amount), funded)
		self.assertEqual(out.request_allocations[0].pm_request, req)

	def test_b_finance_approve_blocked_when_over_allocated(self):
		name, _req, _funded, available = self._pending_over_allocated_vs_request()
		doc = frappe.get_doc("PM Clearance", name)
		# Administrator can write; gate under test is funding availability, not role ACL.
		frappe.set_user("Administrator")
		with self.assertRaises(frappe.ValidationError) as ctx:
			apply_pm_workflow(doc, "PM Finance Approve")
		msg = str(ctx.exception)
		self.assertIn("exceeds available", msg)
		self.assertIn(str(flt(available)).rstrip("0").rstrip("."), msg.replace(",", ""))
		self.assertEqual(
			_wf_title(frappe.db.get_value("PM Clearance", name, "workflow_state")),
			"Pending Finance Review",
		)

	def test_c_draft_save_blocked_when_over_allocated(self):
		name, _req, funded, available = self._pending_over_allocated_vs_request()
		frappe.db.set_value(
			"PM Clearance",
			name,
			"workflow_state",
			resolve_workflow_state_link("Draft"),
			update_modified=False,
		)
		doc = frappe.get_doc("PM Clearance", name)
		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_clearance(doc)
		self.assertIn("exceeds available PM Request balance", str(ctx.exception))
		self.assertIn("6000", str(ctx.exception).replace(",", ""))

	def test_d_e_after_return_correct_resubmit_approve(self):
		name, req, funded, available = self._pending_over_allocated_vs_request()
		frappe.set_user("Administrator")
		doc = apply_pm_workflow(frappe.get_doc("PM Clearance", name), "PM Return for Correction")
		self.assertEqual(_wf_title(doc.workflow_state), "Draft")

		doc = frappe.get_doc("PM Clearance", name)
		doc.details[0].allocated_amount = available
		doc.request_allocations[0].allocated_amount = available
		doc.save()
		doc = doc.reload()
		self.assertEqual(flt(doc.details[0].allocated_amount), available)
		self.assertEqual(flt(doc.request_allocations[0].available_amount), available)

		doc = apply_pm_workflow(doc, "PM Submit Finance Review")
		self.assertEqual(_wf_title(doc.workflow_state), "Pending Manager Approval")
		frappe.set_user(self.mgr)
		doc = apply_pm_workflow(frappe.get_doc("PM Clearance", name), "PM Manager Approve")
		frappe.set_user("Administrator")
		self.assertEqual(_wf_title(doc.workflow_state), "Pending Finance Review")
		# Gate under test is allocation availability; use Admin write to reach Finance Approve validate.
		doc = apply_pm_workflow(frappe.get_doc("PM Clearance", name), "PM Finance Approve")
		self.assertEqual(_wf_title(doc.workflow_state), "Approved")
		self.assertEqual(doc.name, name)

	def test_f_return_with_valid_allocation_still_succeeds(self):
		name, _req, funded, available = self._pending_over_allocated_vs_request()
		# Fix allocation while still pending via db (pending lock would block save)
		frappe.db.sql(
			"update `tabPM Clearance Detail` set allocated_amount=%s where parent=%s",
			(available, name),
		)
		frappe.db.sql(
			"""
			update `tabPM Clearance Request Allocation`
			set allocated_amount=%s where parent=%s
			""",
			(available, name),
		)
		frappe.db.set_value(
			"PM Clearance", name, "total_expense_amount", available, update_modified=False
		)
		frappe.db.commit()
		out = apply_pm_workflow(frappe.get_doc("PM Clearance", name), "PM Return for Correction")
		self.assertEqual(_wf_title(out.workflow_state), "Draft")

	def test_g_missing_request_still_blocked_on_return(self):
		name, _req, funded, available = self._pending_over_allocated_vs_request()
		frappe.db.sql(
			"""
			update `tabPM Clearance Request Allocation`
			set pm_request=null where parent=%s
			""",
			(name,),
		)
		frappe.db.commit()
		frappe.flags.pm_return_for_correction = True
		frappe.flags.in_pm_workflow_apply = True
		try:
			doc = frappe.get_doc("PM Clearance", name)
			with self.assertRaises(frappe.ValidationError):
				validate_request_allocations(doc)
		finally:
			frappe.flags.pm_return_for_correction = False
			frappe.flags.in_pm_workflow_apply = False

	def test_h_i_multiple_clearances_availability_math(self):
		name, req, funded, available = self._pending_over_allocated_vs_request(10_000, 4_000)
		ctx = get_pm_request_allocation_context(req, pm_clearance=name)
		self.assertEqual(flt(ctx["paid_amount"]), 10_000)
		self.assertEqual(flt(ctx["previously_allocated_amount"]), 4_000)
		self.assertEqual(flt(ctx["available_amount"]), 6_000)
		# Cancelled clearances must not reserve — insert cancelled sibling
		cl_c = frappe.new_doc("PM Clearance")
		cl_c.company = pm_ct.COMPANY
		emp = frappe.db.get_value("PM Clearance", name, "employee")
		cl_c.employee = emp
		cl_c.transaction_date = today()
		pi = frappe.db.get_value(
			"PM Clearance Detail", {"parent": name}, "purchase_invoice"
		)
		pm_ct._append_pm_clearance_detail_row(
			cl_c,
			{"settlement_type": "Purchase Invoice", "purchase_invoice": pi, "allocated_amount": 1_000},
		)
		cl_c.append(
			"request_allocations",
			{"funding_source_type": "PM Request", "pm_request": req, "allocated_amount": 1_000},
		)
		cl_c.flags.ignore_mandatory = True
		cl_c.insert(ignore_permissions=True)
		frappe.db.set_value(
			"PM Clearance",
			cl_c.name,
			{"docstatus": 2, "status": "Cancelled"},
			update_modified=False,
		)
		frappe.db.commit()
		ctx2 = get_pm_request_allocation_context(req, pm_clearance=name)
		self.assertEqual(flt(ctx2["available_amount"]), 6_000)
