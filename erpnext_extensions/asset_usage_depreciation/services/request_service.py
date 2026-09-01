# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Validation, approver stamping, and status sync for Asset Request."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, getdate, now_datetime

from erpnext_extensions.asset_usage_depreciation.constants import (
	ACTIVE_REQUEST_STATUSES,
	ALLOC_CANCELLED,
	ALLOC_ISSUED,
	ALLOC_RECEIVED,
	ASSET_REQUEST_DOCTYPE,
	ASSET_REQUEST_ITEM_DOCTYPE,
	COMPANY_FIELD_AR_CEO_MIN_QTY,
	COMPANY_FIELD_AR_REQUIRE_CEO,
	COMPANY_FIELD_AR_REQUIRE_PLANNING,
	FULFILLMENT_FULFILLED,
	FULFILLMENT_ISSUED_FROM_POOL,
	FULFILLMENT_PURCHASE_REQUESTED,
	FULFILLMENT_WAITING,
	LINE_CANCELLED,
	LINE_CLOSED,
	METHOD_ISSUE,
	METHOD_MIXED,
	METHOD_PENDING,
	METHOD_PURCHASE,
	STATUS_APPROVED,
	STATUS_CANCELLED,
	STATUS_DRAFT,
	STATUS_FULFILLED,
	STATUS_PARTIALLY_FULFILLED,
	STATUS_PENDING_CEO,
	STATUS_PENDING_MANAGER,
	STATUS_PENDING_PLANNING,
	STATUS_REJECTED,
	WF_STATE_APPROVED,
	WF_STATE_CANCELLED,
	WF_STATE_DRAFT,
	WF_STATE_PENDING_CEO,
	WF_STATE_PENDING_MANAGER,
	WF_STATE_PENDING_PLANNING,
	WF_STATE_REJECTED,
)
from erpnext_extensions.asset_usage_depreciation.services.availability import (
	get_available_asset_count,
	get_settings,
)


WF_TO_STATUS = {
	WF_STATE_DRAFT: STATUS_DRAFT,
	WF_STATE_PENDING_MANAGER: STATUS_PENDING_MANAGER,
	WF_STATE_PENDING_PLANNING: STATUS_PENDING_PLANNING,
	WF_STATE_PENDING_CEO: STATUS_PENDING_CEO,
	WF_STATE_REJECTED: STATUS_REJECTED,
	WF_STATE_CANCELLED: STATUS_CANCELLED,
}

FULFILLMENT_STATUSES = (STATUS_APPROVED, STATUS_PARTIALLY_FULFILLED, STATUS_FULFILLED)


def _item_flags(item_code: str) -> dict:
	if not item_code:
		return {}
	return (
		frappe.db.get_value(
			"Item",
			item_code,
			["is_fixed_asset", "is_grouped_asset", "disabled", "item_name", "asset_category", "stock_uom"],
			as_dict=True,
		)
		or {}
	)


def _assert_fixed_asset_item(item_code: str, label: str, idx: int | None = None):
	flags = _item_flags(item_code)
	if not flags:
		frappe.throw(_("Row {0}: {1} {2} was not found.").format(idx or "", label, item_code))
	if cint(flags.get("disabled")):
		frappe.throw(_("Row {0}: {1} {2} is disabled.").format(idx or "", label, item_code))
	if not cint(flags.get("is_fixed_asset")):
		frappe.throw(
			_("Row {0}: {1} {2} must be a Fixed Asset item (Is Fixed Asset).").format(
				idx or "", label, item_code
			)
		)
	if cint(flags.get("is_grouped_asset")):
		frappe.throw(
			_("Row {0}: Grouped assets are not supported on Asset Request in v4.4.0.").format(idx or "")
		)
	return flags


def validate_request(doc) -> None:
	if not doc.items:
		frappe.throw(_("At least one requested asset item is required."))

	_validate_dates(doc)
	_validate_employee_company(doc)
	_validate_items(doc)
	from erpnext_extensions.asset_usage_depreciation.services.dimension_service import (
		apply_header_defaults_to_items,
		validate_dimension_companies,
	)

	# Empty item dimensions inherit from header so API and Desk behave the same.
	apply_header_defaults_to_items(doc, only_empty=True)
	validate_dimension_companies(doc)
	_sync_status_from_workflow(doc)

	settings = get_settings()
	if cint(settings.get("prevent_duplicate_active_requests")) and _is_leaving_draft(doc):
		_validate_duplicate_active(doc)


def _validate_dates(doc) -> None:
	if doc.transaction_date and doc.required_date:
		if getdate(doc.required_date) < getdate(doc.transaction_date):
			frappe.msgprint(
				_("Required Date is before Transaction Date."),
				indicator="orange",
				alert=True,
			)


def _validate_employee_company(doc) -> None:
	if not doc.employee or not doc.company:
		return
	emp_company = frappe.db.get_value("Employee", doc.employee, "company")
	if emp_company and emp_company != doc.company:
		frappe.throw(
			_("Employee {0} does not belong to company {1}.").format(doc.employee, doc.company)
		)


def _validate_items(doc) -> None:
	for row in doc.items:
		if cint(row.qty) < 1:
			frappe.throw(_("Row {0}: Quantity must be at least 1.").format(row.idx))

		req_flags = _assert_fixed_asset_item(row.requested_item_code, _("Requested Item"), row.idx)
		row.requested_item_name = row.requested_item_name or req_flags.get("item_name")
		row.requested_asset_category = row.requested_asset_category or req_flags.get("asset_category")
		row.uom = row.uom or req_flags.get("stock_uom")
		if not row.required_date:
			row.required_date = doc.required_date

		if not row.fulfilled_item_code:
			row.fulfilled_item_code = row.requested_item_code
		ful_flags = _assert_fixed_asset_item(row.fulfilled_item_code, _("Fulfilled Item"), row.idx)
		row.fulfilled_item_name = row.fulfilled_item_name or ful_flags.get("item_name")

		if row.fulfilled_purchase_item:
			_assert_fixed_asset_item(row.fulfilled_purchase_item, _("Fulfilled Purchase Item"), row.idx)
		else:
			row.fulfilled_purchase_item = row.fulfilled_item_code

		if row.fulfilled_item_code != row.requested_item_code and not (row.substitution_reason or "").strip():
			submitting = getattr(doc, "_action", None) == "submit" or cint(doc.docstatus) == 1
			if submitting or _is_leaving_draft(doc):
				frappe.throw(
					_(
						"Row {0}: Substitution Reason is required when Fulfilled Item "
						"differs from Requested Item."
					).format(row.idx)
				)

		row.available_qty = get_available_asset_count(
			doc.company,
			requested_item_code=row.requested_item_code,
			requested_asset_category=row.requested_asset_category,
			fulfilled_item_code=row.fulfilled_item_code,
			exclude_request=doc.name if not doc.is_new() else None,
		)


def _is_leaving_draft(doc) -> bool:
	state = doc.workflow_state or WF_STATE_DRAFT
	return state not in (WF_STATE_DRAFT, "", None) and state != WF_STATE_REJECTED


def _validate_duplicate_active(doc) -> None:
	if not doc.employee or not doc.company:
		return
	requested_items = {row.requested_item_code for row in doc.items if row.requested_item_code}
	if not requested_items:
		return

	others = frappe.get_all(
		ASSET_REQUEST_DOCTYPE,
		filters={
			"name": ("!=", doc.name or ""),
			"company": doc.company,
			"employee": doc.employee,
			"status": ("in", list(ACTIVE_REQUEST_STATUSES)),
			"docstatus": ("<", 2),
		},
		pluck="name",
	)
	if not others:
		return

	open_lines = frappe.get_all(
		ASSET_REQUEST_ITEM_DOCTYPE,
		filters={
			"parent": ("in", others),
			"requested_item_code": ("in", list(requested_items)),
			"line_status": ("not in", [LINE_CLOSED, LINE_CANCELLED]),
		},
		fields=["parent", "requested_item_code"],
	)
	if open_lines:
		hit = open_lines[0]
		frappe.throw(
			_(
				"An active Asset Request ({0}) already exists for {1} / {2}. "
				"Complete or cancel it before submitting a duplicate."
			).format(hit.parent, doc.employee, hit.requested_item_code)
		)


def stamp_policy_and_approvers(doc) -> None:
	"""Copy Company flags and named approvers. Frozen once the request leaves Draft."""
	before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	prev_state = (before.workflow_state or WF_STATE_DRAFT) if before else WF_STATE_DRAFT
	already_in_flow = prev_state not in (WF_STATE_DRAFT, "", None)

	if already_in_flow and before and before.manager_approver:
		if doc.manager_approver != before.manager_approver:
			doc.manager_approver = before.manager_approver
		return

	if already_in_flow and before and not before.manager_approver:
		# Legacy unstamped in-flight request: do not guess a manager.
		return

	if not doc.manager_approver:
		company = doc.company
		settings = get_settings()

		require_planning = cint(frappe.db.get_value("Company", company, COMPANY_FIELD_AR_REQUIRE_PLANNING))
		require_ceo_flag = cint(frappe.db.get_value("Company", company, COMPANY_FIELD_AR_REQUIRE_CEO))
		min_qty = cint(frappe.db.get_value("Company", company, COMPANY_FIELD_AR_CEO_MIN_QTY)) or 0
		total_qty = sum(cint(row.qty) for row in doc.items)
		require_ceo = 0
		if require_ceo_flag:
			require_ceo = 1 if min_qty <= 0 or total_qty >= min_qty else 0

		doc.require_planning_approval = require_planning
		doc.require_ceo_approval = require_ceo

		doc.manager_approver = _resolve_manager_user(doc.employee)
		if require_planning:
			doc.planning_approver = settings.get("planning_approver") or doc.planning_approver
		if require_ceo:
			doc.ceo_approver = settings.get("ceo_approver") or doc.ceo_approver

	if _is_submitting_for_approval(doc) and not _is_valid_stamped_manager(doc.manager_approver):
		from erpnext_extensions.asset_usage_depreciation.services.manager_authorization import (
			manager_resolution_message,
		)

		if doc.manager_approver:
			frappe.throw(
				_(
					"Cannot submit for approval: manager approver {0} is missing or disabled."
				).format(doc.manager_approver)
			)
		frappe.throw(manager_resolution_message(doc.employee))


def _is_submitting_for_approval(doc) -> bool:
	if (doc.workflow_state or "") != WF_STATE_PENDING_MANAGER:
		return False
	before = doc.get_doc_before_save() if hasattr(doc, "get_doc_before_save") else None
	if not before:
		return True
	prev = before.workflow_state or WF_STATE_DRAFT
	return prev in (WF_STATE_DRAFT, "", None)


def _is_valid_stamped_manager(user: str | None) -> bool:
	from erpnext_extensions.asset_usage_depreciation.services.manager_authorization import (
		is_valid_manager_user,
	)

	return is_valid_manager_user(user)


def _resolve_manager_user(employee: str | None) -> str | None:
	if not employee:
		return None
	if not frappe.db.exists("Employee", employee):
		return None
	reports_to = frappe.db.get_value("Employee", employee, "reports_to")
	if not reports_to or not frappe.db.exists("Employee", reports_to):
		return None
	user_id = frappe.db.get_value("Employee", reports_to, "user_id")
	if not user_id:
		return None
	from erpnext_extensions.asset_usage_depreciation.services.manager_authorization import (
		is_valid_manager_user,
	)

	return user_id if is_valid_manager_user(user_id) else None


def _sync_status_from_workflow(doc) -> None:
	state = doc.workflow_state or WF_STATE_DRAFT
	mapped = WF_TO_STATUS.get(state)
	if mapped:
		doc.status = mapped
		return
	if state == WF_STATE_APPROVED:
		if doc.status not in FULFILLMENT_STATUSES:
			doc.status = STATUS_APPROVED


def mark_approved(doc) -> None:
	doc.approved_on = now_datetime()
	doc.approved_by = frappe.session.user
	if doc.status not in FULFILLMENT_STATUSES:
		doc.status = STATUS_APPROVED
	doc.fulfillment_status = FULFILLMENT_WAITING


def validate_cancel(doc) -> None:
	blocking = []
	for row in doc.get("allocations") or []:
		if row.fulfillment_status == ALLOC_CANCELLED:
			continue
		if row.asset_movement and frappe.db.get_value("Asset Movement", row.asset_movement, "docstatus") == 1:
			blocking.append(row.asset_movement)
		if row.material_request:
			mr_status = frappe.db.get_value("Material Request", row.material_request, ["docstatus", "per_ordered"])
			if mr_status and cint(mr_status[0]) == 1 and (mr_status[1] or 0) > 0:
				blocking.append(row.material_request)
	if blocking:
		frappe.throw(
			_(
				"Cancel linked Asset Movement / Material Request documents first: {0}"
			).format(", ".join(sorted(set(blocking))))
		)


def refresh_header_fulfillment(doc) -> None:
	"""Recompute header qty/status from allocations. Does not save."""
	issued = 0
	purchase = 0
	open_units = 0
	total = sum(cint(row.qty) for row in doc.items)
	for alloc in doc.get("allocations") or []:
		if alloc.fulfillment_status == ALLOC_CANCELLED:
			continue
		if alloc.method == METHOD_ISSUE:
			issued += 1
			if alloc.fulfillment_status != ALLOC_ISSUED:
				open_units += 1
		elif alloc.method == METHOD_PURCHASE:
			purchase += 1
			if alloc.fulfillment_status not in (ALLOC_ISSUED, ALLOC_RECEIVED):
				open_units += 1

	doc.issued_qty = issued
	doc.purchase_qty = purchase
	done = issued + purchase
	has_am = any(
		a.asset_movement
		for a in (doc.get("allocations") or [])
		if a.fulfillment_status != ALLOC_CANCELLED
	)
	has_mr = any(
		a.material_request
		for a in (doc.get("allocations") or [])
		if a.fulfillment_status != ALLOC_CANCELLED
	) or bool(doc.material_request)

	# Workflow status stays Approved. Fulfillment is a separate lifecycle.
	if total > 0 and done >= total and open_units == 0:
		doc.fulfillment_status = FULFILLMENT_FULFILLED
	elif has_mr:
		doc.fulfillment_status = FULFILLMENT_PURCHASE_REQUESTED
	elif has_am:
		doc.fulfillment_status = FULFILLMENT_ISSUED_FROM_POOL
	else:
		doc.fulfillment_status = FULFILLMENT_WAITING

	_sync_item_line_status(doc)


def _sync_item_line_status(doc) -> None:
	from erpnext_extensions.asset_usage_depreciation.constants import (
		LINE_ISSUED,
		LINE_OPEN,
		LINE_PURCHASE_REQUESTED,
		LINE_RECEIVED,
		LINE_RESERVED,
	)

	by_item: dict[str, list] = {}
	for alloc in doc.get("allocations") or []:
		if alloc.fulfillment_status == ALLOC_CANCELLED:
			continue
		by_item.setdefault(alloc.asset_request_item, []).append(alloc)

	for row in doc.items:
		allocs = by_item.get(row.name) or []
		if not allocs:
			row.fulfillment_method = METHOD_PENDING
			row.line_status = LINE_OPEN
			continue
		methods = {a.method for a in allocs}
		if methods == {METHOD_ISSUE}:
			row.fulfillment_method = METHOD_ISSUE
		elif methods == {METHOD_PURCHASE}:
			row.fulfillment_method = METHOD_PURCHASE
		else:
			row.fulfillment_method = METHOD_MIXED

		statuses = {a.fulfillment_status for a in allocs}
		if statuses <= {ALLOC_ISSUED}:
			row.line_status = LINE_ISSUED
			if cint(row.qty) == 1 and allocs[0].allocated_asset:
				row.fulfilled_asset = allocs[0].allocated_asset
		elif statuses <= {ALLOC_RECEIVED, ALLOC_ISSUED}:
			row.line_status = LINE_RECEIVED
		elif METHOD_PURCHASE in methods:
			row.line_status = LINE_PURCHASE_REQUESTED
		else:
			row.line_status = LINE_RESERVED
