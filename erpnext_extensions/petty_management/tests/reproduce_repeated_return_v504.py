"""One-off reproduction for v5.0.4 repeated Return cycles. Run via bench execute."""

from __future__ import annotations

import frappe
from frappe.model.workflow import get_transitions
from frappe.utils import today

from erpnext_extensions.petty_management.permissions import has_pm_request_permission
from erpnext_extensions.petty_management.services.return_for_correction_service import (
	count_return_timeline_comments,
)
from erpnext_extensions.petty_management.services.workflow_utils import (
	apply_pm_workflow,
	resolve_workflow_state_link,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm


def _wf_title(link: str | None) -> str:
	if not link:
		return ""
	return (frappe.db.get_value("Workflow State", link, "workflow_state_name") or link or "").strip()


def _ensure_user(email: str, roles: list[str]) -> None:
	if not frappe.db.exists("User", email):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": email.split("@")[0][:30],
				"send_welcome_email": 0,
				"user_type": "System User",
			}
		).insert(ignore_permissions=True)
	user = frappe.get_doc("User", email)
	user.roles = []
	for role in roles:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role}).insert(ignore_permissions=True)
		user.append("roles", {"role": role})
	user.enabled = 1
	user.save(ignore_permissions=True)


def _snapshot_request(name: str, manager: str) -> dict:
	row = frappe.db.get_value(
		"PM Request",
		name,
		[
			"name",
			"workflow_state",
			"docstatus",
			"status",
			"manager_approver",
			"ceo_approver",
			"finance_approver",
			"owner",
			"modified",
		],
		as_dict=True,
	)
	row["workflow_title"] = _wf_title(row.get("workflow_state"))
	row["return_comments"] = count_return_timeline_comments("PM Request", name)
	row["todos"] = frappe.get_all(
		"ToDo",
		filters={"reference_type": "PM Request", "reference_name": name},
		fields=["name", "status", "allocated_to"],
	)
	if frappe.db.has_table("Workflow Action"):
		row["workflow_actions"] = frappe.get_all(
			"Workflow Action",
			filters={"reference_doctype": "PM Request", "reference_name": name},
			fields=["name", "status", "user"],
		)
	else:
		row["workflow_actions"] = []
	frappe.set_user(manager)
	doc = frappe.get_doc("PM Request", name)
	try:
		row["mgr_transitions"] = [t.get("action") for t in get_transitions(doc)]
	except Exception as exc:
		row["mgr_transitions"] = []
		row["mgr_transitions_error"] = f"{type(exc).__name__}: {exc}"
	row["mgr_has_permission"] = bool(frappe.has_permission("PM Request", "read", doc=doc))
	row["mgr_controller"] = bool(has_pm_request_permission(doc, "read", manager))
	frappe.set_user("Administrator")
	return row


def reproduce_request_manager_cycles(cycles: int = 3) -> dict:
	frappe.set_user("Administrator")
	from erpnext_extensions.patches.post_model_sync.migrate_pm_workflow_v402 import (
		_rebuild_pm_clearance_workflow,
		_rebuild_pm_request_workflow,
		_seed_assignment_rules,
	)

	_rebuild_pm_request_workflow()
	_rebuild_pm_clearance_workflow()
	_seed_assignment_rules()
	tpm._ensure_company_context()

	mgr = "pm_v504_mgr@example.com"
	holder = "pm_v504_holder@example.com"
	_ensure_user(mgr, ["Petty Management User", "Expense Approver", "Accounts User", "Desk User"])
	_ensure_user(
		holder,
		["Petty Management User", "Accounts User", "Desk User"],
	)
	_ensure_user(
		"pm_v504_fin@example.com",
		["Petty Management Accountant", "Petty Management User", "Accounts User", "Desk User"],
	)

	emp = tpm._make_employee()
	frappe.db.set_value("Employee", emp, {"expense_approver": mgr, "user_id": holder}, update_modified=False)
	tpm._make_holder(emp)
	settings = frappe.get_single("PM Settings")
	settings.db_set("ceo_approver", mgr, update_modified=False)
	settings.db_set("finance_manager", "pm_v504_fin@example.com", update_modified=False)
	settings.db_set("require_named_manager_approver", 1, update_modified=False)

	req = frappe.new_doc("PM Request")
	req.company = tpm.COMPANY
	req.employee = emp
	req.transaction_date = today()
	req.append("details", {"advance_amount": 1500, "description": "v504 repeat"})
	req.insert(ignore_permissions=True)
	frappe.db.set_value(
		"PM Request",
		req.name,
		{"workflow_state": resolve_workflow_state_link("Draft"), "owner": holder},
		update_modified=False,
	)
	name = req.name

	steps: list[dict] = []
	for cycle in range(1, cycles + 1):
		frappe.set_user(holder)
		apply_pm_workflow(frappe.get_doc("PM Request", name), "PM Submit for Approval")
		frappe.set_user("Administrator")
		steps.append({"cycle": cycle, "step": "after_submit", **_snapshot_request(name, mgr)})

		frappe.set_user(mgr)
		try:
			apply_pm_workflow(frappe.get_doc("PM Request", name), "PM Return for Correction")
			result = "ok"
			error = None
		except Exception as exc:
			result = "fail"
			error = f"{type(exc).__name__}: {exc}"
		frappe.set_user("Administrator")
		steps.append(
			{
				"cycle": cycle,
				"step": "after_return",
				"return_result": result,
				"return_error": error,
				**_snapshot_request(name, mgr),
			}
		)
		if result != "ok":
			break

	frappe.db.commit()
	return {"name": name, "steps": steps}


@frappe.whitelist()
def run():
	try:
		return {"ok": True, "result": reproduce_request_manager_cycles(3)}
	except Exception as exc:
		frappe.db.rollback()
		return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
