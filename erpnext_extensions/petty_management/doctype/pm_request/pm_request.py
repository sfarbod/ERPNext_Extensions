# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.desk.reportview import get_match_cond
from frappe.model.document import Document
from frappe.model.naming import getseries
from frappe.utils import getdate, today

from erpnext_extensions.petty_management.services.holder_service import get_holder_context
from erpnext_extensions.petty_management.services.request_service import (
	create_payment_entry as create_payment_entry_service,
)
from erpnext_extensions.petty_management.services.request_lifecycle_eligibility import (
	apply_cancelled_business_status,
	assert_pm_request_delete_allowed,
)
from erpnext_extensions.petty_management.services.request_service import (
	get_pm_request_action_flags_for_doc,
	validate_request,
	validate_request_cancel,
)


class PMRequest(Document):
	"""Funds petty cash via Payment Entry; ties to PM Clearance only through PM Holder / same Petty Cash Account."""

	def autoname(self):
		if not self.employee:
			frappe.throw(_("Employee is required before naming"))
		d = getdate(self.transaction_date or today())
		emp_key = str(self.employee).replace(" ", "")[:40]
		prefix = f"REQ-{emp_key}-{d.year}-{d.month:02d}-"
		self.name = prefix + getseries(prefix, 5)

	def validate(self):
		validate_request(self)

	def before_submit(self):
		# v4.7.2: Finance Approve submits (docstatus 0→1). Stamps were set on
		# Draft → Pending Manager; re-validate / fill gaps without clearing.
		from erpnext_extensions.petty_management.services.approver_stamp_service import (
			ensure_pm_request_approver_stamps,
		)

		ensure_pm_request_approver_stamps(self)

	def before_cancel(self):
		validate_request_cancel(self)
		# Terminal Clearances (Rejected/Cancelled) are historical for Cancel, but Rejected
		# parents keep submitted child Link rows. After open-process eligibility passes,
		# ignore PM Clearance reverse-links for Frappe's cancel back-link check (ERPNext pattern).
		ignored = list(getattr(self, "ignore_linked_doctypes", None) or [])
		if "PM Clearance" not in ignored:
			ignored.append("PM Clearance")
		self.ignore_linked_doctypes = ignored

	def on_cancel(self):
		"""Business status → Cancelled; never rewrite workflow_state or approvers."""
		apply_cancelled_business_status(self)

	def on_trash(self):
		"""v4.6.8 delete eligibility — independent from cancel rules."""
		from erpnext_extensions.petty_management.services.draft_approval_guards import (
			assert_pending_not_deletable,
		)

		assert_pending_not_deletable(self)
		assert_pm_request_delete_allowed(self)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def get_employee_bank_account_query(
	doctype,
	txt,
	searchfield,
	start,
	page_len,
	filters,
	reference_doctype=None,
	ignore_user_permissions=False,
):
	"""Link search: only Bank Account rows for the selected Employee (excludes company / other parties)."""
	doctype = "Bank Account"
	if isinstance(filters, str):
		filters = json.loads(filters)
	filters = filters or {}
	employee = filters.get("employee")
	company = filters.get("company")

	conds = [
		"`tabBank Account`.party_type = %(party_type)s",
		"`tabBank Account`.docstatus != 2",
		"IFNULL(`tabBank Account`.disabled, 0) = 0",
	]
	values = {
		"party_type": "Employee",
		"txt": f"%{txt}%",
		"start": start,
		"page_len": page_len,
	}

	if employee:
		conds.append("`tabBank Account`.party = %(employee)s")
		values["employee"] = employee
	else:
		conds.append("1=0")

	if company:
		conds.append("`tabBank Account`.company = %(company)s")
		values["company"] = company

	where_sql = " AND ".join(conds)
	match_cond = get_match_cond(doctype)

	return frappe.db.sql(
		f"""
		SELECT `tabBank Account`.name, `tabBank Account`.account_name
		FROM `tabBank Account`
		WHERE {where_sql}
			AND `tabBank Account`.{searchfield} LIKE %(txt)s
			{match_cond}
		ORDER BY `tabBank Account`.name
		LIMIT %(page_len)s OFFSET %(start)s
		""",
		values,
	)


@frappe.whitelist()
def get_pm_request_holder_context(
	employee: str | None = None, company: str | None = None, posting_date=None
) -> dict:
	return get_holder_context(employee, company, posting_date=posting_date)


@frappe.whitelist()
def create_payment_entry(pm_request: str, paid_amount=None):
	from erpnext_extensions.petty_management.services.request_api_guard import (
		get_pm_request_doc_for_write,
		get_pm_request_response_version,
		notify_pm_request_funding_updated,
	)

	doc = get_pm_request_doc_for_write(pm_request)
	pa = None
	if paid_amount not in (None, ""):
		pa = float(paid_amount)
	pe_name = create_payment_entry_service(doc.name, paid_amount=pa)
	notify_pm_request_funding_updated(doc.name, "on_payment_entry_created")
	return {
		"payment_entry": pe_name,
		"response_version_id": get_pm_request_response_version(doc.name),
	}


@frappe.whitelist()
def close_pm_request(
	pm_request: str, close_reason: str | None = None, close_reason_detail: str | None = None
):
	from erpnext_extensions.petty_management.services.request_api_guard import (
		get_pm_request_doc_for_write,
		get_pm_request_response_version,
		notify_pm_request_funding_updated,
	)
	from erpnext_extensions.petty_management.services.request_service import close_pm_request as _close

	get_pm_request_doc_for_write(pm_request)
	_close(pm_request, close_reason=close_reason, close_reason_detail=close_reason_detail)
	notify_pm_request_funding_updated(pm_request, "on_pm_request_updated")
	return {"ok": True, "response_version_id": get_pm_request_response_version(pm_request)}


@frappe.whitelist()
def cancel_pm_request(pm_request: str):
	from erpnext_extensions.petty_management.services.request_api_guard import (
		get_pm_request_doc_for_write,
		get_pm_request_response_version,
		notify_pm_request_funding_updated,
	)
	from erpnext_extensions.petty_management.services.request_service import cancel_pm_request as _cancel

	get_pm_request_doc_for_write(pm_request)
	_cancel(pm_request)
	notify_pm_request_funding_updated(pm_request, "on_pm_request_cancelled")
	return {"ok": True, "response_version_id": get_pm_request_response_version(pm_request)}


@frappe.whitelist()
def get_pm_request_payment_entries(pm_request: str):
	from erpnext_extensions.petty_management.services.request_api_guard import (
		build_pm_request_payment_entries_payload,
	)

	return build_pm_request_payment_entries_payload(pm_request)


@frappe.whitelist()
def get_pm_request_action_flags(pm_request: str):
	from erpnext_extensions.petty_management.services.request_api_guard import (
		get_pm_request_doc_for_read,
		wrap_action_flags_with_version,
	)

	doc = get_pm_request_doc_for_read(pm_request)
	return wrap_action_flags_with_version(get_pm_request_action_flags_for_doc(doc), doc.name)
