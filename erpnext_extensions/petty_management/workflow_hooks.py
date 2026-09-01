"""Workflow integration for Petty Management."""

from __future__ import annotations

import frappe
from frappe.model.workflow import apply_workflow as _apply_workflow

_SUBMIT_FOR_APPROVAL_ACTIONS = frozenset(
	{
		"PM Submit for Approval",
		"PM Submit Finance Review",
	}
)


def _stamp_before_submit_for_approval(doctype: str, name: str, action: str) -> None:
	"""Stamp approvers when leaving Draft (before Pending* at docstatus 0)."""
	if action not in _SUBMIT_FOR_APPROVAL_ACTIONS or not name:
		return
	doc = frappe.get_doc(doctype, name)
	if doctype == "PM Request":
		from erpnext_extensions.petty_management.services.approver_stamp_service import (
			stamp_pm_request_approvers,
		)

		stamp_pm_request_approvers(doc)
		frappe.db.set_value(
			doctype,
			name,
			{
				"manager_approver": doc.manager_approver,
				"ceo_approver": doc.ceo_approver,
				"finance_approver": doc.finance_approver,
			},
			update_modified=False,
		)
	elif doctype == "PM Clearance":
		from erpnext_extensions.petty_management.services.approver_stamp_service import (
			stamp_pm_clearance_approvers,
		)

		stamp_pm_clearance_approvers(doc)
		frappe.db.set_value(
			doctype,
			name,
			{
				"manager_approver": doc.manager_approver,
				"finance_approver": doc.finance_approver,
			},
			update_modified=False,
		)


@frappe.whitelist()
def apply_workflow(doc, action):
	"""Guard PM workflow actions before standard apply; sync business status after.

	Also runs consecutive same-user Approve auto-skip (v4.1.4) so Desk and API
	share one path. Payment Entry must never call this.
	"""
	payload = frappe.parse_json(doc) if isinstance(doc, str) else doc
	doctype = None
	name = None
	if isinstance(payload, dict):
		doctype = payload.get("doctype")
		name = payload.get("name")

	action_s = (action or "").strip()
	from_state = None

	frappe.flags.in_pm_workflow_apply = True
	try:
		if doctype == "PM Clearance" and name:
			from erpnext_extensions.petty_management.services.clearance_action_policy import (
				validate_apply_workflow_action,
			)

			cl_doc = frappe.get_doc("PM Clearance", name)
			validate_apply_workflow_action(cl_doc, action_s)
			from_state = cl_doc.workflow_state

		if doctype == "PM Request" and name:
			from erpnext_extensions.petty_management.services.request_action_policy import (
				validate_pm_request_workflow_action,
			)
			from erpnext_extensions.petty_management.services.request_api_guard import (
				get_pm_request_doc_for_read,
			)

			req_doc = get_pm_request_doc_for_read(name)
			validate_pm_request_workflow_action(req_doc, action_s)
			from_state = req_doc.workflow_state

		if doctype in ("PM Request", "PM Clearance") and name and action_s:
			from erpnext_extensions.petty_management.services.workflow_approver_validation_service import (
				validate_acting_approver_can_read,
			)

			check_doc = frappe.get_doc(doctype, name)
			validate_acting_approver_can_read(check_doc, action_s)

		# v4.7.2: serialize Return — lock row, re-read state, reject if already Draft
		if (
			doctype in ("PM Request", "PM Clearance")
			and name
			and action_s == "PM Return for Correction"
		):
			from erpnext_extensions.petty_management.services.return_for_correction_service import (
				assert_return_allowed_under_lock,
				lock_pm_document_for_return,
			)

			locked = lock_pm_document_for_return(doctype, name)
			from_title_locked = assert_return_allowed_under_lock(doctype, locked)
			from_state = locked.get("workflow_state") or from_state
			# Keep locked title for timeline if from_state link was blank
			if not from_state:
				from_state = from_title_locked

		if doctype in ("PM Request", "PM Clearance") and name:
			_stamp_before_submit_for_approval(doctype, name, action_s)

		result = _apply_workflow(doc, action)

		from erpnext_extensions.petty_management.services.clearance_finance_review import (
			CLEARANCE_FINANCE_WORKFLOW_ACTIONS,
			stamp_clearance_finance_approver_after_act,
		)

		doctype = getattr(result, "doctype", None) or (
			result.get("doctype") if isinstance(result, dict) else None
		)
		name = getattr(result, "name", None) or (result.get("name") if isinstance(result, dict) else None)
		if doctype == "PM Clearance" and action_s in CLEARANCE_FINANCE_WORKFLOW_ACTIONS:
			doc_obj = result if hasattr(result, "reload") else frappe.get_doc(doctype, name)
			stamp_clearance_finance_approver_after_act(doc_obj, action_s)

		from erpnext_extensions.petty_management.services.auto_skip_approvals import (
			PM_AUTO_SKIP_APPROVE_ACTIONS,
			apply_consecutive_auto_approvals,
			refresh_pm_assignment_rules,
		)
		from erpnext_extensions.petty_management.services.return_for_correction_service import (
			PM_RETURN_FOR_CORRECTION,
			handle_return_for_correction,
		)

		doctype = getattr(result, "doctype", None) or (
			result.get("doctype") if isinstance(result, dict) else None
		)
		name = getattr(result, "name", None) or (result.get("name") if isinstance(result, dict) else None)
		if doctype in ("PM Request", "PM Clearance") and name and action_s == PM_RETURN_FOR_CORRECTION:
			doc_obj = result if hasattr(result, "reload") else frappe.get_doc(doctype, name)
			from_title = None
			if from_state:
				from_title = (
					frappe.db.get_value("Workflow State", from_state, "workflow_state_name") or from_state
				)
			reason = (
				getattr(frappe.flags, "pm_return_reason", None)
				or (frappe.form_dict.get("reason") if getattr(frappe, "form_dict", None) else None)
			)
			result = handle_return_for_correction(doc_obj, from_state=from_title, reason=reason)
		elif (
			doctype in ("PM Request", "PM Clearance")
			and name
			and action_s in PM_AUTO_SKIP_APPROVE_ACTIONS
		):
			doc_obj = result if hasattr(result, "reload") else frappe.get_doc(doctype, name)
			refresh_pm_assignment_rules(doc_obj)
			doc_obj = apply_consecutive_auto_approvals(doc_obj)
			result = doc_obj

		_sync_business_status_after_workflow(result)
		return result
	finally:
		frappe.flags.in_pm_workflow_apply = False


def _sync_business_status_after_workflow(result) -> None:
	"""Submitted workflow saves may skip validate; persist status from workflow/JE facts."""
	if not result:
		return
	doctype = getattr(result, "doctype", None) or (result.get("doctype") if isinstance(result, dict) else None)
	name = getattr(result, "name", None) or (result.get("name") if isinstance(result, dict) else None)
	if not doctype or not name:
		return
	if doctype == "PM Request":
		from erpnext_extensions.petty_management.services.business_status_service import (
			sync_pm_request_business_status,
		)

		doc = frappe.get_doc(doctype, name)
		status = sync_pm_request_business_status(doc)
		frappe.db.set_value(doctype, name, "status", status, update_modified=False)
		if hasattr(result, "status"):
			result.status = status
	elif doctype == "PM Clearance":
		from erpnext_extensions.petty_management.services.business_status_service import (
			sync_pm_clearance_business_status,
		)

		doc = frappe.get_doc(doctype, name)
		sync_pm_clearance_business_status(doc, persist=True)
		if hasattr(result, "status"):
			result.status = doc.status
