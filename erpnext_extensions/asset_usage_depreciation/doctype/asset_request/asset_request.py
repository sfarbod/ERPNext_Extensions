# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from erpnext_extensions.asset_usage_depreciation.constants import (
	STATUS_REJECTED,
	WF_STATE_APPROVED,
	WF_STATE_DRAFT,
	WF_STATE_REJECTED,
)
from erpnext_extensions.asset_usage_depreciation.services.manager_authorization import (
	sync_manager_access,
)
from erpnext_extensions.asset_usage_depreciation.services.request_service import (
	mark_approved,
	stamp_policy_and_approvers,
	validate_cancel,
	validate_request,
)


class AssetRequest(Document):
	def validate(self):
		if not self.workflow_state:
			self.workflow_state = WF_STATE_DRAFT
		if self.workflow_state not in (WF_STATE_DRAFT, None, "") or self.docstatus == 1:
			stamp_policy_and_approvers(self)
		validate_request(self)
		if self.workflow_state == WF_STATE_REJECTED and not (self.rejection_reason or "").strip():
			if self.has_value_changed("workflow_state"):
				frappe.throw(_("Rejection Reason is required."))

	def on_update(self):
		sync_manager_access(self)

	def after_insert(self):
		sync_manager_access(self)

	def before_submit(self):
		stamp_policy_and_approvers(self)
		if (self.workflow_state or WF_STATE_APPROVED) not in (WF_STATE_APPROVED,):
			# Direct submit without workflow: treat as Approved.
			self.workflow_state = WF_STATE_APPROVED
		mark_approved(self)

	def on_submit(self):
		from erpnext_extensions.asset_usage_depreciation.constants import FULFILLMENT_WAITING
		from erpnext_extensions.asset_usage_depreciation.services.fulfillment_service import (
			refresh_available_counts,
		)

		# Approval is not fulfillment: never create MR/AM and never reserve assets.
		self.fulfillment_status = FULFILLMENT_WAITING
		refresh_available_counts(self)
		self.db_set(
			{
				"status": self.status,
				"fulfillment_status": self.fulfillment_status,
				"issued_qty": self.issued_qty or 0,
				"purchase_qty": self.purchase_qty or 0,
				"available_asset_count": self.available_asset_count or 0,
				"material_request": None,
				"approved_on": self.approved_on,
				"approved_by": self.approved_by,
			},
			update_modified=False,
		)
		for row in self.get("items") or []:
			if row.name:
				row.db_update()

	def before_cancel(self):
		validate_cancel(self)

	def on_cancel(self):
		self.db_set("status", "Cancelled")


@frappe.whitelist()
def get_available_asset_count(
	company: str,
	requested_item_code: str | None = None,
	requested_asset_category: str | None = None,
	fulfilled_item_code: str | None = None,
	exclude_request: str | None = None,
) -> int:
	from erpnext_extensions.asset_usage_depreciation.services.availability import (
		get_available_asset_count as _count,
	)

	return _count(
		company,
		requested_item_code=requested_item_code,
		requested_asset_category=requested_asset_category,
		fulfilled_item_code=fulfilled_item_code,
		exclude_request=exclude_request,
	)


@frappe.whitelist()
def get_available_assets(
	company: str,
	requested_item_code: str | None = None,
	requested_asset_category: str | None = None,
	fulfilled_item_code: str | None = None,
	exclude_request: str | None = None,
) -> list[dict]:
	from erpnext_extensions.asset_usage_depreciation.services.availability import (
		get_available_assets as _list,
	)

	return _list(
		company,
		requested_item_code=requested_item_code,
		requested_asset_category=requested_asset_category,
		fulfilled_item_code=fulfilled_item_code,
		exclude_request=exclude_request,
	)


def _assert_fulfillment_rpc_allowed(doc, *, allow_fulfilled: bool = False) -> None:
	"""Privileged AM/MR insert must not be callable by employees or approvers."""
	from erpnext_extensions.asset_usage_depreciation.constants import (
		FULFILLMENT_FULFILLED,
		WF_STATE_APPROVED,
	)

	doc.check_permission("write")
	if int(doc.docstatus or 0) != 1:
		frappe.throw(_("Fulfillment can only run on a submitted Asset Request."))
	if (doc.workflow_state or "") != WF_STATE_APPROVED:
		frappe.throw(_("Fulfillment actions are only allowed after the request is Approved."))
	roles = set(frappe.get_roles())
	if not roles.intersection({"Asset Manager", "System Manager"}):
		frappe.throw(_("Not permitted to create fulfillment documents."), frappe.PermissionError)
	if not allow_fulfilled and (doc.fulfillment_status or "") == FULFILLMENT_FULFILLED:
		frappe.throw(_("This Asset Request is already fulfilled."))


def _save_submitted(doc) -> None:
	doc.flags.ignore_validate_update_after_submit = True
	doc.save()


@frappe.whitelist()
def check_availability(name: str) -> dict:
	from erpnext_extensions.asset_usage_depreciation.services.fulfillment_service import (
		check_availability as _check,
	)

	doc = frappe.get_doc("Asset Request", name)
	_assert_fulfillment_rpc_allowed(doc)
	result = _check(doc)
	try:
		doc.add_comment("Comment", _("Availability checked"))
	except Exception:
		pass
	_save_submitted(doc)
	return {"name": doc.name, "fulfillment_status": doc.fulfillment_status, **result}


@frappe.whitelist()
def reevaluate_fulfillment(name: str) -> dict:
	"""Compatibility alias for Check Availability. Does not create documents."""
	return check_availability(name)


@frappe.whitelist()
def get_pool_picker(name: str) -> dict:
	from erpnext_extensions.asset_usage_depreciation.services.fulfillment_service import (
		get_pool_picker_data,
	)

	doc = frappe.get_doc("Asset Request", name)
	_assert_fulfillment_rpc_allowed(doc)
	return get_pool_picker_data(doc)


@frappe.whitelist()
def issue_from_pool(name: str, selections=None, confirm_substitution: int = 0) -> dict:
	from erpnext_extensions.asset_usage_depreciation.services.fulfillment_service import (
		issue_from_pool as _issue,
	)

	doc = frappe.get_doc("Asset Request", name)
	_assert_fulfillment_rpc_allowed(doc)
	am = _issue(
		doc,
		selections=selections,
		confirm_substitution=confirm_substitution,
		auto_submit=0,
	)
	_save_submitted(doc)
	return {"asset_movement": am.name if am else None}


@frappe.whitelist()
def create_asset_movement(name: str, selections=None, confirm_substitution: int = 0) -> dict:
	"""Compatibility alias for Issue from Pool."""
	return issue_from_pool(
		name, selections=selections, confirm_substitution=confirm_substitution
	)


@frappe.whitelist()
def request_purchase(name: str) -> dict:
	from erpnext_extensions.asset_usage_depreciation.services.fulfillment_service import (
		request_purchase as _request,
	)

	doc = frappe.get_doc("Asset Request", name)
	_assert_fulfillment_rpc_allowed(doc)
	mr = _request(doc, auto_submit=0)
	_save_submitted(doc)
	return {"material_request": mr.name if mr else None}


@frappe.whitelist()
def create_material_request(name: str) -> dict:
	"""Compatibility alias for Request Purchase."""
	return request_purchase(name)
