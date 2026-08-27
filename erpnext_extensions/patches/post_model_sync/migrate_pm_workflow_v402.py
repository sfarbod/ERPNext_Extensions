# Copyright (c) 2026, ERPNext Extensions contributors
"""v4.0.2: multi-level PM workflows, business status separation, Assignment Rules."""

from __future__ import annotations

import json

import frappe

from erpnext_extensions.petty_management.services.workflow_utils import (
	normalize_workflow_definition,
	realign_doctype_workflow_states,
	resolve_workflow_state_link,
)


def _wf(name: str) -> str:
	return resolve_workflow_state_link(name) or name


def _ensure_action(action: str) -> None:
	if frappe.db.exists("Workflow Action Master", action):
		return
	doc = frappe.new_doc("Workflow Action Master")
	doc.workflow_action_name = action
	doc.insert(ignore_permissions=True)


def _rebuild_pm_request_workflow() -> None:
	name = "PM Request Workflow"
	for title in (
		"Draft",
		"Pending Manager Approval",
		"Pending CEO Approval",
		"Pending Finance Approval",
		"Finance Approved",
		"Waiting for Payment",  # keep state master for legacy doc remaps
		"Rejected",
	):
		_wf(title)

	_ensure_action("PM Submit for Approval")
	_ensure_action("PM Approve")
	_ensure_action("PM Reject")
	_ensure_action("PM Return for Correction")
	_ensure_action("PM Manager Approve")
	_ensure_action("PM CEO Approve")
	_ensure_action("PM Finance Approve")

	if frappe.db.exists("Workflow", name):
		w = frappe.get_doc("Workflow", name)
	else:
		w = frappe.new_doc("Workflow")
		w.workflow_name = name
		w.document_type = "PM Request"

	w.is_active = 1
	w.workflow_state_field = "workflow_state"
	w.send_email_alert = 0
	w.states = []
	w.transitions = []

	for state, doc_status in (
		("Draft", "0"),
		("Pending Manager Approval", "0"),
		("Pending CEO Approval", "0"),
		("Pending Finance Approval", "0"),
		("Finance Approved", "1"),
		("Rejected", "1"),
	):
		w.append(
			"states",
			{"state": _wf(state), "doc_status": doc_status, "allow_edit": "All"},
		)

	# Submit: User role, no named-user condition
	w.append(
		"transitions",
		{
			"state": _wf("Draft"),
			"action": "PM Submit for Approval",
			"next_state": _wf("Pending Manager Approval"),
			"allowed": "Petty Management User",
			"allow_self_approval": 1,
		},
	)

	def _add(state, action, next_state, role, condition, self_ok):
		w.append(
			"transitions",
			{
				"state": _wf(state),
				"action": action,
				"next_state": _wf(next_state),
				"allowed": role,
				"allow_self_approval": 1 if self_ok else 0,
				"condition": condition,
			},
		)

	# v4.1.4: Manager/CEO capability = Petty Management User + stamped approver.
	# Finance capability = Petty Management Accountant + stamped finance_approver.
	# Never use Allowed Role = All. allow_self_approval=1 so stamped owners can approve.
	_add(
		"Pending Manager Approval",
		"PM Manager Approve",
		"Pending CEO Approval",
		"Petty Management User",
		"doc.manager_approver == frappe.session.user",
		True,
	)
	# v4.7.2: Pending* → Draft via Return (no Cancel/Amend); Reject only post-submit
	_add(
		"Pending Manager Approval",
		"PM Return for Correction",
		"Draft",
		"Petty Management User",
		"doc.manager_approver == frappe.session.user",
		True,
	)
	_add(
		"Pending CEO Approval",
		"PM CEO Approve",
		"Pending Finance Approval",
		"Petty Management User",
		"doc.ceo_approver == frappe.session.user",
		True,
	)
	_add(
		"Pending CEO Approval",
		"PM Return for Correction",
		"Draft",
		"Petty Management User",
		"doc.ceo_approver == frappe.session.user",
		True,
	)
	_add(
		"Pending Finance Approval",
		"PM Finance Approve",
		"Finance Approved",
		"Petty Management Accountant",
		"doc.finance_approver == frappe.session.user",
		True,
	)
	_add(
		"Pending Finance Approval",
		"PM Return for Correction",
		"Draft",
		"Petty Management Accountant",
		"doc.finance_approver == frappe.session.user",
		True,
	)
	# Reject from finance-approved terminal (still blocked by PE guards when funded)
	_add(
		"Finance Approved",
		"PM Reject",
		"Rejected",
		"Petty Management User",
		"doc.finance_approver == frappe.session.user or doc.manager_approver == frappe.session.user",
		True,
	)

	normalize_workflow_definition(w)
	if w.is_new():
		w.insert(ignore_permissions=True)
	else:
		w.save(ignore_permissions=True)


def _rebuild_pm_clearance_workflow() -> None:
	name = "PM Clearance Workflow"
	for title in (
		"Draft",
		"Pending Manager Approval",
		"Pending Finance Review",
		"Approved",
		"Rejected",
	):
		_wf(title)

	_ensure_action("PM Submit Finance Review")
	_ensure_action("PM Manager Approve")
	_ensure_action("PM Finance Approve")
	_ensure_action("PM Approve")
	_ensure_action("PM Reject")
	_ensure_action("PM Return for Correction")

	from erpnext_extensions.petty_management.services.clearance_finance_review import (
		DEFAULT_CLEARANCE_FINANCE_REVIEW_ROLE,
		get_clearance_finance_review_role,
	)

	review_role = get_clearance_finance_review_role() or DEFAULT_CLEARANCE_FINANCE_REVIEW_ROLE

	if frappe.db.exists("Workflow", name):
		w = frappe.get_doc("Workflow", name)
	else:
		w = frappe.new_doc("Workflow")
		w.workflow_name = name
		w.document_type = "PM Clearance"

	w.is_active = 1
	w.workflow_state_field = "workflow_state"
	w.send_email_alert = 0
	w.states = []
	w.transitions = []

	for state, doc_status, send_email in (
		("Draft", "0", 1),
		("Pending Manager Approval", "0", 1),
		("Pending Finance Review", "0", 0),
		("Approved", "1", 1),
		("Rejected", "1", 1),
	):
		w.append(
			"states",
			{
				"state": _wf(state),
				"doc_status": doc_status,
				"allow_edit": "All",
				"send_email": send_email,
			},
		)

	w.append(
		"transitions",
		{
			"state": _wf("Draft"),
			"action": "PM Submit Finance Review",
			"next_state": _wf("Pending Manager Approval"),
			"allowed": "Petty Management User",
			"allow_self_approval": 1,
		},
	)
	# v4.1.4: Clearance manager stage uses Petty Management User + stamp (same role model).
	w.append(
		"transitions",
		{
			"state": _wf("Pending Manager Approval"),
			"action": "PM Manager Approve",
			"next_state": _wf("Pending Finance Review"),
			"allowed": "Petty Management User",
			"allow_self_approval": 1,
			"condition": "doc.manager_approver == frappe.session.user",
		},
	)
	# v4.7.2: Pending* → Draft via Return (replace pre-submit Reject)
	w.append(
		"transitions",
		{
			"state": _wf("Pending Manager Approval"),
			"action": "PM Return for Correction",
			"next_state": _wf("Draft"),
			"allowed": "Petty Management User",
			"allow_self_approval": 1,
			"condition": "doc.manager_approver == frappe.session.user",
		},
	)
	w.append(
		"transitions",
		{
			"state": _wf("Pending Finance Review"),
			"action": "PM Finance Approve",
			"next_state": _wf("Approved"),
			"allowed": review_role,
			"allow_self_approval": 1,
		},
	)
	# Keep PM Approve as alias for finance for backward Desk habits
	w.append(
		"transitions",
		{
			"state": _wf("Pending Finance Review"),
			"action": "PM Approve",
			"next_state": _wf("Approved"),
			"allowed": review_role,
			"allow_self_approval": 1,
		},
	)
	w.append(
		"transitions",
		{
			"state": _wf("Pending Finance Review"),
			"action": "PM Return for Correction",
			"next_state": _wf("Draft"),
			"allowed": review_role,
			"allow_self_approval": 1,
		},
	)

	normalize_workflow_definition(w)
	if w.is_new():
		w.insert(ignore_permissions=True)
	else:
		w.save(ignore_permissions=True)


def _workflow_title(link: str | None) -> str:
	if not link:
		return ""
	return (frappe.db.get_value("Workflow State", link, "workflow_state_name") or link or "").strip()


def _migrate_pm_request_docs() -> dict:
	report = {"remapped": 0, "by_from": {}}
	pending = _wf("Pending Manager Approval")
	finance_approved = _wf("Finance Approved")
	rejected = _wf("Rejected")
	draft = _wf("Draft")
	cleared_titles = ("Approved", "Waiting for Payment", "Finance Approved")

	for row in frappe.get_all("PM Request", fields=["name", "workflow_state", "status", "payment_status", "is_closed"]):
		title = _workflow_title(row.workflow_state)
		new_ws = None
		new_status = None
		if title in ("Pending Approval", "Pending"):
			new_ws = pending
			new_status = "Pending Approval"
		elif title in cleared_titles:
			new_ws = finance_approved
			new_status = "Waiting for Payment"
		elif title == "Rejected":
			new_ws = rejected
			new_status = "Rejected"
		elif title == "Draft" or not title:
			new_ws = draft
			new_status = "Draft"

		if row.payment_status == "Partially Paid":
			new_status = "Partially Paid"
			if not new_ws or title in cleared_titles:
				new_ws = finance_approved
		if row.payment_status == "Paid":
			new_status = "Paid"
			if not new_ws or title in cleared_titles:
				new_ws = finance_approved
		if row.is_closed:
			new_status = "Closed"
			if title in cleared_titles:
				new_ws = finance_approved

		values = {}
		if new_ws and new_ws != row.workflow_state:
			values["workflow_state"] = new_ws
		if new_status and new_status != row.status:
			values["status"] = new_status
		if values:
			frappe.db.set_value("PM Request", row.name, values, update_modified=False)
			report["remapped"] += 1
			report["by_from"][title or "(blank)"] = report["by_from"].get(title or "(blank)", 0) + 1
	return report


def _migrate_pm_clearance_docs() -> dict:
	report = {"remapped": 0, "rewound_accounting": 0, "by_from": {}}
	approved = _wf("Approved")
	pending_mgr = _wf("Pending Manager Approval")
	pending_fin = _wf("Pending Finance Review")
	rejected = _wf("Rejected")
	draft = _wf("Draft")

	for row in frappe.get_all(
		"PM Clearance", fields=["name", "workflow_state", "status", "journal_entry", "docstatus"]
	):
		title = _workflow_title(row.workflow_state)
		new_ws = None
		new_status = (row.status or "").strip() or None
		je_ds = None
		if row.journal_entry and frappe.db.exists("Journal Entry", row.journal_entry):
			je_ds = frappe.db.get_value("Journal Entry", row.journal_entry, "docstatus")

		if title in ("Settled", "Pending Journal Entry Submission"):
			new_ws = approved
			report["rewound_accounting"] += 1
			if je_ds == 1:
				new_status = "Settled"
			elif je_ds == 0:
				new_status = "Pending Journal Entry Submission"
			else:
				new_status = "Approved"
		elif title == "Approved":
			new_ws = approved
			if je_ds == 1:
				new_status = "Settled"
			elif je_ds == 0:
				new_status = "Pending Journal Entry Submission"
			else:
				new_status = "Approved"
		elif title == "Pending Finance Review":
			new_ws = pending_fin
			new_status = "Pending Approval"
		elif title == "Pending Manager Approval":
			new_ws = pending_mgr
			new_status = "Pending Approval"
		elif title == "Rejected":
			new_ws = rejected
			new_status = "Rejected"
		elif title == "Draft" or not title:
			new_ws = draft
			new_status = "Draft"

		if row.docstatus == 2:
			new_status = "Cancelled"

		values = {}
		if new_ws and new_ws != row.workflow_state:
			values["workflow_state"] = new_ws
		if new_status and new_status != row.status:
			values["status"] = new_status
		if values:
			frappe.db.set_value("PM Clearance", row.name, values, update_modified=False)
			report["remapped"] += 1
			report["by_from"][title or "(blank)"] = report["by_from"].get(title or "(blank)", 0) + 1
	return report


def _ensure_assignment_rule(
	*,
	rule_name: str,
	document_type: str,
	assign_condition: str,
	unassign_condition: str,
	field: str,
	priority: int,
	description: str,
	close_condition: str | None = None,
) -> None:
	"""Create/update native Assignment Rule (Based on Field). Never creates ToDos here."""
	if frappe.db.exists("Assignment Rule", rule_name):
		doc = frappe.get_doc("Assignment Rule", rule_name)
	else:
		doc = frappe.new_doc("Assignment Rule")
		doc.name = rule_name

	doc.document_type = document_type
	doc.rule = "Based on Field"
	doc.field = field
	doc.assign_condition = assign_condition
	doc.unassign_condition = unassign_condition
	doc.close_condition = close_condition or ""
	doc.priority = priority
	doc.disabled = 0
	doc.description = description
	# Assignment Rule requires at least one assignment day
	if not doc.get("assignment_days"):
		for day in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
			doc.append("assignment_days", {"day": day})

	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)


def _seed_assignment_rules() -> list[str]:
	created = []
	# Bake canonical workflow_state link values into conditions (Assignment Rule eval uses bare fields).
	req_mgr = _wf("Pending Manager Approval")
	req_ceo = _wf("Pending CEO Approval")
	req_fin = _wf("Pending Finance Approval")
	req_finance_approved = _wf("Finance Approved")
	clr_mgr = _wf("Pending Manager Approval")
	clr_fin = _wf("Pending Finance Review")
	clr_appr = _wf("Approved")

	rules = [
		(
			"PM Request Manager Approval",
			"PM Request",
			f'workflow_state == "{req_mgr}"',
			f'workflow_state != "{req_mgr}"',
			"manager_approver",
			30,
			"PM Request {{ name }} awaiting manager approval",
			f'workflow_state in ("{req_finance_approved}", "{_wf("Rejected")}", "{_wf("Draft")}")',
		),
		(
			"PM Request CEO Approval",
			"PM Request",
			f'workflow_state == "{req_ceo}"',
			f'workflow_state != "{req_ceo}"',
			"ceo_approver",
			20,
			"PM Request {{ name }} awaiting CEO approval",
			f'workflow_state in ("{req_finance_approved}", "{_wf("Rejected")}", "{_wf("Draft")}")',
		),
		(
			"PM Request Finance Approval",
			"PM Request",
			f'workflow_state == "{req_fin}"',
			f'workflow_state != "{req_fin}"',
			"finance_approver",
			10,
			"PM Request {{ name }} awaiting finance approval",
			f'workflow_state in ("{req_finance_approved}", "{_wf("Rejected")}", "{_wf("Draft")}")',
		),
		(
			"PM Clearance Manager Approval",
			"PM Clearance",
			f'workflow_state == "{clr_mgr}"',
			f'workflow_state != "{clr_mgr}"',
			"manager_approver",
			20,
			"PM Clearance {{ name }} awaiting manager approval",
			f'workflow_state in ("{clr_appr}", "{_wf("Rejected")}", "{_wf("Draft")}")',
		),
		(
			"PM Clearance Finance Review",
			"PM Clearance",
			f'workflow_state == "{clr_fin}"',
			f'workflow_state != "{clr_fin}"',
			"finance_approver",
			10,
			"PM Clearance {{ name }} awaiting finance review",
			f'workflow_state in ("{clr_appr}", "{_wf("Rejected")}", "{_wf("Draft")}")',
		),
	]
	for args in rules:
		_ensure_assignment_rule(
			rule_name=args[0],
			document_type=args[1],
			assign_condition=args[2],
			unassign_condition=args[3],
			field=args[4],
			priority=args[5],
			description=args[6],
			close_condition=args[7],
		)
		created.append(args[0])
	if frappe.db.exists("Assignment Rule", "PM Clearance Finance Review"):
		frappe.db.set_value("Assignment Rule", "PM Clearance Finance Review", "disabled", 1, update_modified=False)
	return created


def _bulk_apply_pending_assignments() -> dict:
	"""Apply Assignment Rules only to pending approval documents (no Paid/Closed/Rejected)."""
	from frappe.automation.doctype.assignment_rule.assignment_rule import bulk_apply

	stats = {"request": 0, "clearance": 0}
	req_pending = [
		_wf("Pending Manager Approval"),
		_wf("Pending CEO Approval"),
		_wf("Pending Finance Approval"),
	]
	names = frappe.get_all(
		"PM Request",
		filters={"workflow_state": ("in", req_pending), "docstatus": ("in", (0, 1))},
		pluck="name",
	)
	for name in names:
		try:
			bulk_apply("PM Request", [name])
			stats["request"] += 1
		except Exception:
			frappe.log_error(frappe.get_traceback(), "PM v402 bulk_apply PM Request")

	clr_pending = [_wf("Pending Manager Approval"), _wf("Pending Finance Review")]
	names = frappe.get_all(
		"PM Clearance",
		filters={"workflow_state": ("in", clr_pending), "docstatus": ("in", (0, 1))},
		pluck="name",
	)
	for name in names:
		try:
			bulk_apply("PM Clearance", [name])
			stats["clearance"] += 1
		except Exception:
			frappe.log_error(frappe.get_traceback(), "PM v402 bulk_apply PM Clearance")
	return stats


def execute():
	frappe.flags.in_patch = True
	report = {"request_docs": {}, "clearance_docs": {}, "assignment_rules": [], "bulk_apply": {}}
	try:
		_rebuild_pm_request_workflow()
		_rebuild_pm_clearance_workflow()
		realign_doctype_workflow_states("PM Request")
		realign_doctype_workflow_states("PM Clearance")
		report["request_docs"] = _migrate_pm_request_docs()
		report["clearance_docs"] = _migrate_pm_clearance_docs()
		report["assignment_rules"] = _seed_assignment_rules()
	finally:
		frappe.flags.in_patch = False

	report["bulk_apply"] = _bulk_apply_pending_assignments()
	frappe.db.commit()
	frappe.cache().set_value("pm_workflow_v402_migration_report", report)
	print(json.dumps({"pm_workflow_v402": report}, indent=2, default=str))
