# Copyright (c) 2026, ERPNext Extensions contributors
"""Prep + server assertions for PM workflow v5.0.2 Playwright E2E."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.model.workflow import get_transitions
from frappe.utils import cint, today
from frappe.utils.password import update_password

from erpnext_extensions.e2e.e2e_fixture import e2e_run_context
from erpnext_extensions.petty_management.services.return_for_correction_service import (
	RETURN_TIMELINE_MARKER,
	count_return_timeline_comments,
)
from erpnext_extensions.petty_management.services.workflow_utils import (
	apply_pm_workflow,
	resolve_workflow_state_link,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm

PASSWORD = "pm_v502_e2e_1"
HOLDER = "pm_v502_holder_e2e@example.com"
MANAGER_GOOD = "pm_v502_mgr_good_e2e@example.com"
MANAGER_BAD = "pm_v502_mgr_bad_e2e@example.com"
REVIEWER = "pm_v502_rev_e2e@example.com"
FINANCE = "pm_v502_fin_e2e@example.com"
REVIEW_ROLE = "Petty Management Clearance Reviewer"


def _wf_title(link: str | None) -> str:
	if not link:
		return ""
	return (frappe.db.get_value("Workflow State", link, "workflow_state_name") or link or "").strip()


def _desk_roles() -> list[str]:
	return ["Accounts User", "Employee"]


def _ensure_role(role: str) -> None:
	if not frappe.db.exists("Role", role):
		frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)


def _ensure_user(email: str, roles: list[str]) -> str:
	for role in roles:
		_ensure_role(role)
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0][:30],
				"send_welcome_email": 0,
				"user_type": "System User",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	user.roles = []
	for role in roles:
		user.append("roles", {"role": role})
	user.enabled = 1
	user.save(ignore_permissions=True)
	update_password(email, PASSWORD)
	frappe.db.commit()
	return email


def _configure_settings() -> None:
	settings = frappe.get_single("PM Settings")
	settings.db_set("require_named_manager_approver", 1, update_modified=False)
	settings.db_set("ceo_approver", MANAGER_GOOD, update_modified=False)
	settings.db_set("finance_manager", FINANCE, update_modified=False)
	settings.db_set("finance_supervisor", FINANCE, update_modified=False)
	settings.db_set("clearance_finance_review_role", REVIEW_ROLE, update_modified=False)
	frappe.db.commit()


def _rebuild_workflows() -> None:
	from erpnext_extensions.patches.post_model_sync.migrate_pm_workflow_v402 import (
		_rebuild_pm_clearance_workflow,
		_rebuild_pm_request_workflow,
		_seed_assignment_rules,
	)

	_rebuild_pm_request_workflow()
	_rebuild_pm_clearance_workflow()
	_seed_assignment_rules()


def _new_request_draft(employee: str, owner: str | None = None) -> str:
	req = frappe.new_doc("PM Request")
	req.company = tpm.COMPANY
	req.employee = employee
	req.transaction_date = today()
	req.append("details", {"advance_amount": 1500, "description": "v502 e2e"})
	req.insert(ignore_permissions=True)
	frappe.db.set_value(
		"PM Request",
		req.name,
		"workflow_state",
		resolve_workflow_state_link("Draft"),
		update_modified=False,
	)
	if owner:
		frappe.db.set_value("PM Request", req.name, "owner", owner, update_modified=False)
	frappe.db.commit()
	return req.name


def _submit_request_pending_manager(req_name: str, manager: str) -> str:
	frappe.db.set_value("PM Request", req_name, "owner", HOLDER, update_modified=False)
	frappe.set_user(HOLDER)
	out = apply_pm_workflow(frappe.get_doc("PM Request", req_name), "PM Submit for Approval")
	frappe.set_user("Administrator")
	frappe.db.commit()
	return out.name


def _insert_draft_pi(amount: float = 1_000.0) -> str:
	pi = tpm._make_pi_outstanding(amount)
	try:
		pi.insert(ignore_permissions=True)
	except frappe.ValidationError as exc:
		raise unittest.SkipTest(f"Purchase Invoice insert unavailable: {exc}") from exc
	frappe.db.commit()
	return pi.name


def _new_clearance_draft(employee: str, pi_name: str, owner: str | None = None) -> str:
	cl = frappe.new_doc("PM Clearance")
	cl.company = tpm.COMPANY
	cl.employee = employee
	cl.transaction_date = today()
	tpm._append_pm_clearance_detail_row(
		cl,
		{
			"settlement_type": "Purchase Invoice",
			"purchase_invoice": pi_name,
			"allocated_amount": 1_000.0,
		},
	)
	cl.flags.ignore_mandatory = True
	cl.flags.ignore_validate = True
	try:
		cl.insert(ignore_permissions=True)
	except frappe.ValidationError as exc:
		raise unittest.SkipTest(f"PM Clearance insert unavailable: {exc}") from exc
	frappe.db.set_value(
		"PM Clearance",
		cl.name,
		"workflow_state",
		resolve_workflow_state_link("Draft"),
		update_modified=False,
	)
	if owner:
		frappe.db.set_value("PM Clearance", cl.name, "owner", owner, update_modified=False)
	frappe.db.commit()
	return cl.name


def _advance_clearance_pending_manager(cl_name: str, manager: str) -> None:
	stamp = frappe.get_doc("PM Clearance", cl_name)
	from erpnext_extensions.petty_management.services.approver_stamp_service import (
		stamp_pm_clearance_approvers,
	)

	stamp_pm_clearance_approvers(stamp)
	apply_pm_workflow(stamp, "PM Submit Finance Review")
	frappe.db.commit()


def _advance_clearance_pending_finance(cl_name: str, manager: str, reviewer: str) -> None:
	_advance_clearance_pending_manager(cl_name, manager)
	frappe.db.set_value(
		"PM Clearance",
		cl_name,
		{"manager_approver": manager, "finance_approver": reviewer},
		update_modified=False,
	)
	frappe.set_user(manager)
	apply_pm_workflow(frappe.get_doc("PM Clearance", cl_name), "PM Manager Approve")
	frappe.set_user("Administrator")
	frappe.db.commit()


def _snapshot_request(name: str) -> dict:
	row = frappe.db.get_value(
		"PM Request",
		name,
		[
			"name",
			"workflow_state",
			"docstatus",
			"manager_approver",
			"ceo_approver",
			"finance_approver",
			"owner",
		],
		as_dict=True,
	)
	row["workflow_title"] = _wf_title(row.get("workflow_state"))
	row["open_todos"] = frappe.get_all(
		"ToDo",
		filters={"reference_type": "PM Request", "reference_name": name, "status": "Open"},
		fields=["name", "allocated_to"],
	)
	row["return_comments"] = count_return_timeline_comments("PM Request", name)
	return row


def _snapshot_clearance(name: str) -> dict:
	row = frappe.db.get_value(
		"PM Clearance",
		name,
		["name", "workflow_state", "docstatus", "manager_approver", "finance_approver", "owner"],
		as_dict=True,
	)
	row["workflow_title"] = _wf_title(row.get("workflow_state"))
	row["open_todos"] = frappe.get_all(
		"ToDo",
		filters={"reference_type": "PM Clearance", "reference_name": name, "status": "Open"},
		pluck="allocated_to",
	)
	row["return_comments"] = count_return_timeline_comments("PM Clearance", name)
	return row


@frappe.whitelist()
def prepare_v502_fixtures() -> dict:
	frappe.set_user("Administrator")
	_rebuild_workflows()
	tpm._ensure_company_context()
	if not tpm.COMPANY:
		frappe.throw("No Company on site")

	_configure_settings()
	_ensure_user(HOLDER, ["Petty Management User", *_desk_roles()])
	_ensure_user(
		MANAGER_GOOD,
		["Petty Management User", "Expense Approver", *_desk_roles()],
	)
	_ensure_user(MANAGER_BAD, ["Expense Approver", *_desk_roles()])
	_ensure_user(REVIEWER, [REVIEW_ROLE, *_desk_roles()])
	_ensure_user(FINANCE, ["Petty Management Accountant", "Petty Management User", *_desk_roles()])

	emp_good = tpm._make_employee()
	frappe.db.set_value("Employee", emp_good, "expense_approver", MANAGER_GOOD, update_modified=False)
	frappe.db.set_value("Employee", emp_good, "user_id", HOLDER, update_modified=False)
	tpm._make_holder(emp_good)

	emp_bad = tpm._make_employee()
	frappe.db.set_value("Employee", emp_bad, "expense_approver", MANAGER_BAD, update_modified=False)
	tpm._make_holder(emp_bad)

	request_draft = _new_request_draft(emp_good, owner=HOLDER)
	request_pending = _new_request_draft(emp_good, owner=HOLDER)
	request_pending = _submit_request_pending_manager(request_pending, MANAGER_GOOD)
	request_pending_drift = _new_request_draft(emp_good, owner=HOLDER)
	request_pending_drift = _submit_request_pending_manager(request_pending_drift, MANAGER_GOOD)
	request_pending_atomicity = _new_request_draft(emp_good, owner=HOLDER)
	request_pending_atomicity = _submit_request_pending_manager(request_pending_atomicity, MANAGER_GOOD)
	request_invalid_draft = _new_request_draft(emp_bad, owner=HOLDER)

	clearance_pending_manager = None
	clearance_pending_finance = None
	clearance_pending_atomicity = None
	clearance_skipped = None
	try:
		pi_name = _insert_draft_pi()
		clearance_pending_manager = _new_clearance_draft(emp_good, pi_name, owner=HOLDER)
		_advance_clearance_pending_manager(clearance_pending_manager, MANAGER_GOOD)
		clearance_pending_finance = _new_clearance_draft(emp_good, pi_name, owner=HOLDER)
		_advance_clearance_pending_finance(clearance_pending_finance, MANAGER_GOOD, REVIEWER)
		clearance_pending_atomicity = _new_clearance_draft(emp_good, pi_name, owner=HOLDER)
		_advance_clearance_pending_finance(clearance_pending_atomicity, MANAGER_GOOD, REVIEWER)
	except unittest.SkipTest as exc:
		clearance_skipped = str(exc)

	frappe.db.commit()
	return {
		**e2e_run_context(),
		"password": PASSWORD,
		"users": {
			"holder": {"email": HOLDER, "password": PASSWORD},
			"manager_good": {"email": MANAGER_GOOD, "password": PASSWORD},
			"manager_bad": {"email": MANAGER_BAD, "password": PASSWORD},
			"reviewer": {"email": REVIEWER, "password": PASSWORD},
			"finance": {"email": FINANCE, "password": PASSWORD},
		},
		"request_draft": request_draft,
		"request_pending_manager": request_pending,
		"request_pending_drift": request_pending_drift,
		"request_pending_atomicity": request_pending_atomicity,
		"request_invalid_draft": request_invalid_draft,
		"clearance_pending_manager": clearance_pending_manager,
		"clearance_pending_finance": clearance_pending_finance,
		"clearance_pending_atomicity": clearance_pending_atomicity,
		"clearance_skipped": clearance_skipped,
		"snapshots": {
			"request_pending_manager": _snapshot_request(request_pending),
			"clearance_pending_manager": _snapshot_clearance(clearance_pending_manager)
			if clearance_pending_manager
			else None,
			"clearance_pending_finance": _snapshot_clearance(clearance_pending_finance)
			if clearance_pending_finance
			else None,
		},
	}


@frappe.whitelist()
def attempt_invalid_manager_submit(pm_request: str, holder_email: str) -> dict:
	doc = frappe.get_doc("PM Request", pm_request)
	open_todos_before = frappe.db.count(
		"ToDo", {"reference_type": "PM Request", "reference_name": pm_request, "status": "Open"}
	)
	try:
		from erpnext_extensions.petty_management.services.approver_stamp_service import (
			stamp_pm_request_approvers,
		)

		stamp_pm_request_approvers(doc)
		ok = False
		error = "Expected ValidationError but stamp succeeded"
	except frappe.ValidationError as exc:
		ok = True
		error = str(exc)
	except Exception as exc:
		ok = False
		error = f"{type(exc).__name__}: {exc}"
	finally:
		frappe.set_user("Administrator")
		frappe.db.rollback()

	open_todos_after = frappe.db.count(
		"ToDo", {"reference_type": "PM Request", "reference_name": pm_request, "status": "Open"}
	)
	snap = _snapshot_request(pm_request)
	return {
		"ok": ok,
		"error": error,
		"workflow_title": snap["workflow_title"],
		"open_todos_before": open_todos_before,
		"open_todos_after": open_todos_after,
		"validation_message_clear": bool(
			ok and ("Petty Management User" in error or "Workflow approver" in error)
		),
	}


@frappe.whitelist()
def run_role_drift_request(pm_request: str, manager_email: str) -> dict:
	user = frappe.get_doc("User", manager_email)
	user.roles = []
	user.append("roles", {"role": "Expense Approver"})
	user.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_cache(user=manager_email)

	frappe.set_user(manager_email)
	try:
		from erpnext_extensions.petty_management.workflow_hooks import apply_workflow as hooked_apply

		hooked_apply(
			frappe.get_doc("PM Request", pm_request).as_dict(),
			"PM Return for Correction",
		)
		ok = False
		error = "Expected ValidationError but workflow applied"
	except frappe.ValidationError as exc:
		ok = True
		error = str(exc)
	finally:
		frappe.set_user("Administrator")
		frappe.db.rollback()

	_ensure_user(
		manager_email,
		["Petty Management User", "Expense Approver", *_desk_roles()],
	)
	snap = _snapshot_request(pm_request)
	return {
		"ok": ok,
		"error": error,
		"workflow_title": snap["workflow_title"],
		"still_pending": snap["workflow_title"] == "Pending Manager Approval",
		"message_clear": bool(
			ok
			and (
				"no longer have permission" in error.lower()
				or "workflow approver cannot execute" in error.lower()
			)
		),
	}


@frappe.whitelist()
def run_atomicity_request(pm_request: str, manager_email: str) -> dict:
	before = _snapshot_request(pm_request)
	frappe.set_user(manager_email)
	try:
		with patch("frappe.desk.form.assign_to.add", side_effect=RuntimeError("inject assign failure")):
			apply_pm_workflow(
				frappe.get_doc("PM Request", pm_request), "PM Return for Correction"
			)
		ok = False
		error = "Expected RuntimeError but Return succeeded"
	except RuntimeError as exc:
		ok = True
		error = str(exc)
	finally:
		frappe.set_user("Administrator")
		frappe.db.rollback()

	after = _snapshot_request(pm_request)
	return {
		"ok": ok,
		"error": error,
		"before": before,
		"after": after,
		"workflow_unchanged": before["workflow_state"] == after["workflow_state"],
		"docstatus_unchanged": before["docstatus"] == after["docstatus"],
		"stamps_unchanged": before["manager_approver"] == after["manager_approver"],
		"todos_unchanged": len(before["open_todos"]) == len(after["open_todos"]),
		"no_timeline": after["return_comments"] == before["return_comments"],
	}


@frappe.whitelist()
def run_atomicity_clearance(pm_clearance: str, reviewer_email: str) -> dict:
	if not pm_clearance:
		return {"ok": False, "skipped": True, "reason": "no clearance fixture"}
	before = _snapshot_clearance(pm_clearance)
	frappe.set_user(reviewer_email)
	try:
		with patch("frappe.desk.form.assign_to.add", side_effect=RuntimeError("inject assign failure")):
			apply_pm_workflow(
				frappe.get_doc("PM Clearance", pm_clearance), "PM Return for Correction"
			)
		ok = False
		error = "Expected RuntimeError but Return succeeded"
	except RuntimeError as exc:
		ok = True
		error = str(exc)
	finally:
		frappe.set_user("Administrator")
		frappe.db.rollback()

	after = _snapshot_clearance(pm_clearance)
	return {
		"ok": ok,
		"error": error,
		"before": before,
		"after": after,
		"workflow_unchanged": before["workflow_state"] == after["workflow_state"],
		"docstatus_unchanged": before["docstatus"] == after["docstatus"],
		"stamps_unchanged": before["manager_approver"] == after["manager_approver"],
		"todos_unchanged": len(before["open_todos"]) == len(after["open_todos"]),
		"no_timeline": after["return_comments"] == before["return_comments"],
	}


@frappe.whitelist()
def get_request_snapshot(pm_request: str) -> dict:
	return _snapshot_request(pm_request)


@frappe.whitelist()
def get_clearance_snapshot(pm_clearance: str) -> dict:
	return _snapshot_clearance(pm_clearance)


@frappe.whitelist()
def apply_return_as_user(doctype: str, name: str, user: str) -> dict:
	frappe.set_user(user)
	try:
		out = apply_pm_workflow(frappe.get_doc(doctype, name), "PM Return for Correction")
		frappe.db.commit()
		return {
			"name": out.name,
			"workflow_title": _wf_title(out.workflow_state),
		}
	finally:
		frappe.set_user("Administrator")


@frappe.whitelist()
def workflow_actions(doctype: str, name: str, user: str) -> list[str]:
	frappe.set_user(user)
	try:
		doc = frappe.get_doc(doctype, name)
		return [t.get("action") for t in get_transitions(doc) if t.get("action")]
	finally:
		frappe.set_user("Administrator")


@frappe.whitelist()
def has_return_timeline_marker(doctype: str, name: str) -> bool:
	rows = frappe.get_all(
		"Comment",
		filters={"reference_doctype": doctype, "reference_name": name, "comment_type": "Info"},
		pluck="content",
	)
	return any(RETURN_TIMELINE_MARKER in (content or "") for content in rows)
