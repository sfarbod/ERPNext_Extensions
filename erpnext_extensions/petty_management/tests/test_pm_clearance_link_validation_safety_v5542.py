# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.42 safety — narrow Return link validation (cancelled PI only).

Proves ``PMClearance._validate_links`` allows cancelled Purchase Invoice during
corrective Return while still blocking missing / deleted / other cancelled Links.
"""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import today

from erpnext_extensions.petty_management.services.workflow_utils import (
	apply_pm_workflow,
	resolve_workflow_state_link,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as pm_ct
from erpnext_extensions.petty_management.tests.test_pm_clearance_cancelled_pi_return_v5542 import (
	_mark_pi_cancelled,
	_submit_dedicated_pi,
)
from erpnext_extensions.petty_management.tests.test_pm_pending_remark_edit_v506 import (
	REVIEWER_ROLE,
	_ensure_user,
)


def _wf_title(link: str | None) -> str:
	if not link:
		return ""
	return (frappe.db.get_value("Workflow State", link, "workflow_state_name") or link or "").strip()


class TestPmClearanceLinkValidationSafetyV5542(unittest.TestCase):
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

		cls.mgr = "pm_v5542_link_mgr@example.com"
		cls.fin = "pm_v5542_link_fin@example.com"
		cls.holder_u = "pm_v5542_link_holder@example.com"
		cls.reviewer = "pm_v5542_link_rev@example.com"
		desk = ["Accounts User", "Employee", "Desk User", "System Manager"]
		_ensure_user(cls.mgr, ["Petty Management User", "Expense Approver", *desk])
		_ensure_user(cls.fin, ["Petty Management Accountant", "Petty Management User", *desk])
		_ensure_user(cls.holder_u, ["Petty Management User", *desk])
		_ensure_user(cls.reviewer, [REVIEWER_ROLE, *desk])
		settings = frappe.get_single("PM Settings")
		settings.db_set("require_named_manager_approver", 1, update_modified=False)
		settings.db_set("finance_manager", cls.fin, update_modified=False)
		settings.db_set("clearance_finance_review_role", REVIEWER_ROLE, update_modified=False)

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.flags.pm_return_for_correction = False
		frappe.flags.in_pm_workflow_apply = False

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.flags.pm_return_for_correction = False
		frappe.flags.in_pm_workflow_apply = False
		frappe.db.rollback()

	def _pending(self, amount: float = 3_000.0):
		emp = pm_ct._make_employee()
		frappe.db.set_value("Employee", emp, "expense_approver", self.mgr, update_modified=False)
		frappe.db.set_value("Employee", emp, "user_id", self.holder_u, update_modified=False)
		# Force SQL + cache drop — set_value alone can no-op against stale Employee cache.
		frappe.db.sql(
			"update `tabEmployee` set expense_approver=%s, user_id=%s where name=%s",
			(self.mgr, self.holder_u, emp),
		)
		frappe.db.commit()
		frappe.clear_document_cache("Employee", emp)
		frappe.clear_cache(doctype="Employee")
		assert frappe.db.get_value("Employee", emp, "expense_approver") == self.mgr, emp
		pm_ct._make_holder(emp)
		pi = _submit_dedicated_pi(amount)
		req, _ = pm_ct._fund_pm_request(emp, amount)
		cl = frappe.new_doc("PM Clearance")
		cl.company = pm_ct.COMPANY
		cl.employee = emp
		cl.transaction_date = today()
		pm_ct._append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi,
				"allocated_amount": amount,
				"outstanding_amount": amount,
			},
		)
		cl.append(
			"request_allocations",
			{"funding_source_type": "PM Request", "pm_request": req, "allocated_amount": amount},
		)
		cl.flags.ignore_mandatory = True
		cl.insert(ignore_permissions=True)
		frappe.db.set_value(
			"PM Clearance",
			cl.name,
			{
				"owner": self.holder_u,
				"manager_approver": self.mgr,
				"workflow_state": resolve_workflow_state_link("Pending Finance Review"),
				"status": "Pending Approval",
			},
			update_modified=False,
		)
		frappe.db.commit()
		return cl.name, pi, req, emp

	def _return_as_reviewer(self, name: str):
		frappe.set_user(self.reviewer)
		return apply_pm_workflow(frappe.get_doc("PM Clearance", name), "PM Return for Correction")

	# A — cancelled PI Return ALLOWED
	def test_A_cancelled_pi_return_allowed(self):
		name, pi, *_ = self._pending()
		_mark_pi_cancelled(pi)
		frappe.db.commit()
		out = self._return_as_reviewer(name)
		self.assertEqual(_wf_title(out.workflow_state), "Draft")
		self.assertFalse(bool(getattr(frappe.flags, "pm_return_for_correction", False)))

	# B — nonexistent PI BLOCKED
	def test_B_nonexistent_pi_blocked(self):
		name, *_ = self._pending()
		frappe.db.sql(
			"update `tabPM Clearance Detail` set purchase_invoice=%s where parent=%s",
			("PI-DOES-NOT-EXIST-V5542", name),
		)
		frappe.db.commit()
		with self.assertRaises(frappe.LinkValidationError) as ctx:
			self._return_as_reviewer(name)
		self.assertIn("PI-DOES-NOT-EXIST-V5542", str(ctx.exception))

	# C — deleted/missing PI name BLOCKED (same as nonexistent in Link validation)
	def test_C_deleted_pi_blocked(self):
		name, *_ = self._pending()
		frappe.db.sql(
			"update `tabPM Clearance Detail` set purchase_invoice=%s where parent=%s",
			("ACC-PINV-DELETED-V5542-001", name),
		)
		frappe.db.commit()
		with self.assertRaises(frappe.LinkValidationError):
			self._return_as_reviewer(name)

	# D — nonexistent PM Request BLOCKED
	def test_D_nonexistent_pm_request_blocked(self):
		name, *_ = self._pending()
		frappe.db.sql(
			"update `tabPM Clearance Request Allocation` set pm_request=%s where parent=%s",
			("REQ-NOPE-V5542", name),
		)
		frappe.db.commit()
		with self.assertRaises(frappe.LinkValidationError) as ctx:
			self._return_as_reviewer(name)
		self.assertIn("REQ-NOPE-V5542", str(ctx.exception))

	# E — cancelled PM Request BLOCKED (not purchase_invoice)
	def test_E_cancelled_pm_request_blocked(self):
		name, _pi, req, _emp = self._pending()
		frappe.db.set_value("PM Request", req, "docstatus", 2, update_modified=False)
		frappe.db.commit()
		with self.assertRaises(frappe.CancelledLinkError) as ctx:
			self._return_as_reviewer(name)
		self.assertIn(req, str(ctx.exception))

	# F — nonexistent Employee BLOCKED
	def test_F_nonexistent_employee_blocked(self):
		name, *_ = self._pending()
		frappe.db.set_value(
			"PM Clearance", name, "employee", "HR-EMP-NOPE-V5542", update_modified=False
		)
		frappe.db.commit()
		with self.assertRaises(frappe.LinkValidationError):
			self._return_as_reviewer(name)

	# G — nonexistent Company BLOCKED
	def test_G_nonexistent_company_blocked(self):
		name, *_ = self._pending()
		frappe.db.set_value(
			"PM Clearance", name, "company", "NOPE-COMPANY-V5542", update_modified=False
		)
		frappe.db.commit()
		with self.assertRaises(frappe.LinkValidationError):
			self._return_as_reviewer(name)

	# H — nonexistent Holder BLOCKED
	def test_H_nonexistent_holder_blocked(self):
		name, *_ = self._pending()
		frappe.db.set_value(
			"PM Clearance", name, "holder", "HOLDER-NOPE-V5542", update_modified=False
		)
		frappe.db.commit()
		with self.assertRaises(frappe.LinkValidationError):
			self._return_as_reviewer(name)

	# I — invalid Account BLOCKED
	def test_I_invalid_account_blocked(self):
		name, *_ = self._pending()
		frappe.db.set_value(
			"PM Clearance", name, "petty_cash_account", "ACC-NOPE-V5542", update_modified=False
		)
		frappe.db.commit()
		with self.assertRaises(frappe.LinkValidationError):
			self._return_as_reviewer(name)

	# J — invalid child Cost Center BLOCKED
	def test_J_invalid_child_cost_center_blocked(self):
		name, *_ = self._pending()
		frappe.db.sql(
			"update `tabPM Clearance Detail` set cost_center=%s where parent=%s",
			("CC-NOPE-V5542", name),
		)
		frappe.db.commit()
		with self.assertRaises(frappe.LinkValidationError):
			self._return_as_reviewer(name)

	# K — invalid Dynamic Link BLOCKED
	def test_K_invalid_dynamic_link_blocked(self):
		name, *_ = self._pending()
		frappe.db.sql(
			"""
			update `tabPM Clearance Detail`
			set generated_doctype=%s, generated_document=%s
			where parent=%s
			""",
			("Purchase Invoice", "PI-DYN-NOPE-V5542", name),
		)
		frappe.db.commit()
		with self.assertRaises(frappe.LinkValidationError) as ctx:
			self._return_as_reviewer(name)
		self.assertIn("PI-DYN-NOPE-V5542", str(ctx.exception))

	# L — wrong Employee / Holder relationship BLOCKED
	def test_L_wrong_company_employee_relationship_blocked(self):
		name, _pi, _req, emp = self._pending()
		# Attach a different employee's Holder so Clearance holder≠employee relationship fails.
		other_emp = pm_ct._make_employee()
		frappe.db.set_value(
			"Employee", other_emp, "expense_approver", self.mgr, update_modified=False
		)
		other_holder = pm_ct._make_holder(other_emp)
		frappe.db.set_value("PM Clearance", name, "holder", other_holder, update_modified=False)
		frappe.db.commit()
		with self.assertRaises(Exception):
			self._return_as_reviewer(name)
		self.assertNotEqual(
			_wf_title(frappe.db.get_value("PM Clearance", name, "workflow_state")),
			"Draft",
		)

	# M — unauthorized Return BLOCKED
	def test_M_unauthorized_return_blocked(self):
		name, pi, *_ = self._pending()
		_mark_pi_cancelled(pi)
		frappe.db.commit()
		frappe.set_user(self.holder_u)
		with self.assertRaises(Exception):
			apply_pm_workflow(frappe.get_doc("PM Clearance", name), "PM Return for Correction")
		self.assertEqual(
			_wf_title(frappe.db.get_value("PM Clearance", name, "workflow_state")),
			"Pending Finance Review",
		)

	# N — invalid workflow transition BLOCKED
	def test_N_invalid_workflow_transition_blocked(self):
		name, *_ = self._pending()
		frappe.db.set_value(
			"PM Clearance",
			name,
			"workflow_state",
			resolve_workflow_state_link("Draft"),
			update_modified=False,
		)
		frappe.db.commit()
		frappe.set_user(self.reviewer)
		with self.assertRaises(Exception):
			apply_pm_workflow(frappe.get_doc("PM Clearance", name), "PM Return for Correction")

	# O — exception during Return restores flags
	def test_O_flag_cleanup_after_exception(self):
		name, *_ = self._pending()
		frappe.db.sql(
			"update `tabPM Clearance Detail` set purchase_invoice=%s where parent=%s",
			("PI-NOPE-FLAG-V5542", name),
		)
		frappe.db.commit()
		frappe.set_user(self.reviewer)
		with self.assertRaises(Exception):
			apply_pm_workflow(frappe.get_doc("PM Clearance", name), "PM Return for Correction")
		self.assertFalse(bool(getattr(frappe.flags, "pm_return_for_correction", False)))
		doc = frappe.get_doc("PM Clearance", name)
		self.assertFalse(bool(getattr(doc.flags, "ignore_links", False)))

	# Extra: missing Project (the proven ignore_links gap) BLOCKED under Return
	def test_missing_project_blocked_during_return(self):
		name, pi, *_ = self._pending()
		_mark_pi_cancelled(pi)
		frappe.db.set_value(
			"PM Clearance", name, "project", "PROJECT-NOPE-V5542", update_modified=False
		)
		frappe.db.commit()
		with self.assertRaises(frappe.LinkValidationError) as ctx:
			self._return_as_reviewer(name)
		self.assertIn("PROJECT-NOPE-V5542", str(ctx.exception))

	# Extra: ignore_links alone must NOT be used by production Return path
	def test_production_return_does_not_set_ignore_links(self):
		import inspect

		from erpnext_extensions.petty_management.doctype.pm_clearance.pm_clearance import (
			PMClearance,
		)

		save_src = inspect.getsource(PMClearance)
		# Narrow override must exist; broad ignore_links assignment must not.
		self.assertIn("def _validate_links", save_src)
		self.assertNotIn("self.flags.ignore_links = True", save_src)
