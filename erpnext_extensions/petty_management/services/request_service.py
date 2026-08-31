from __future__ import annotations

import frappe
from frappe import _
from frappe.exceptions import QueryTimeoutError
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate, today

from erpnext_extensions.petty_management.services.business_status_service import (
	REQ_WAITING_FOR_PAYMENT,
	request_is_finance_cleared,
	sync_pm_request_business_status,
)

from erpnext_extensions.petty_management.services.holder_service import (
	sync_request_holder_fields,
	validate_petty_cash_account_company,
)
from erpnext_extensions.petty_management.utils import (
	employee_has_draft_pm_clearance,
	get_pm_settings,
)

_EPS = 1e-6


def workflow_state_title(doc: Document) -> str:
	if not getattr(doc, "workflow_state", None):
		return ""
	return (
		frappe.db.get_value("Workflow State", doc.workflow_state, "workflow_state_name")
		or doc.workflow_state
		or ""
	)


def reconcile_payment_entry_link(doc: Document) -> None:
	"""Clear stale Latest Payment Entry link (missing or cancelled PE)."""
	from erpnext_extensions.petty_management.services.funding_queries import resolve_latest_payment_entry

	latest = resolve_latest_payment_entry(doc.name)
	if latest != getattr(doc, "payment_entry", None):
		doc.payment_entry = latest
		return
	if not getattr(doc, "payment_entry", None):
		return
	if not frappe.db.exists("Payment Entry", doc.payment_entry):
		doc.payment_entry = None
		return
	ds = cint(frappe.db.get_value("Payment Entry", doc.payment_entry, "docstatus"))
	if ds == 2:
		doc.payment_entry = resolve_latest_payment_entry(doc.name)


def derive_payment_status(doc: Document) -> None:
	meta = frappe.get_meta("PM Request")
	if meta.has_field("total_paid_amount"):
		from erpnext_extensions.petty_management.services.funding_service import (
			derive_payment_status_from_totals,
		)

		if doc.total_paid_amount is None:
			from erpnext_extensions.petty_management.services.funding_queries import sum_submitted_pe_amount

			doc.total_paid_amount = sum_submitted_pe_amount(doc.name)
		derive_payment_status_from_totals(doc)
		return
	if not getattr(doc, "payment_entry", None):
		doc.payment_status = "Not Paid"
		return
	ds = cint(frappe.db.get_value("Payment Entry", doc.payment_entry, "docstatus"))
	doc.payment_status = "Paid" if ds == 1 else "Not Paid"


def validate_request(doc: Document) -> None:
	from erpnext_extensions.petty_management.services.draft_approval_guards import (
		assert_pending_not_editable,
	)

	assert_pending_not_editable(doc)
	reconcile_payment_entry_link(doc)
	meta = frappe.get_meta("PM Request")
	if meta.has_field("total_paid_amount"):
		from erpnext_extensions.petty_management.services.funding_service import (
			sync_pm_request_funding_fields,
		)

		sync_pm_request_funding_fields(doc)
	else:
		derive_payment_status(doc)

	holder = sync_request_holder_fields(doc)
	compute_totals(doc)
	sync_pm_request_business_status(doc)
	enforce_request_state_machine(doc)

	settings = get_pm_settings()
	if not doc.details:
		frappe.throw(_("Add at least one detail line"))
	if flt(doc.total_requested_amount) <= 0:
		frappe.throw(_("Total Requested Amount must be greater than zero"))
	if holder.is_blocked:
		frappe.throw(_("This petty cash holder is blocked"))
	if (
		settings
		and settings.block_new_request_if_pending_clearance
		and employee_has_draft_pm_clearance(doc.employee, doc.company)
	):
		frappe.throw(_("This employee has a pending PM Clearance; new requests are blocked by settings."))

	if holder.get("max_balance") is not None:
		limit = flt(holder.max_balance)
		projected = flt(doc.previous_balance) + flt(doc.total_requested_amount)
		allow_over = bool(settings and settings.allow_negative_balance)
		if not allow_over and projected > limit + 1e-6:
			frappe.throw(_("Advance would exceed max balance {0} (projected {1}).").format(limit, projected))

	validate_payment_accounts(doc)


def enforce_request_state_machine(doc: Document) -> None:
	"""Disallow impossible combinations (rejected + funded, paid without PE, etc.)."""
	meta = frappe.get_meta("PM Request")
	ws_title = workflow_state_title(doc)
	st = (doc.status or "").strip()
	rejected = ws_title == "Rejected" or st == "Rejected"

	if rejected:
		if doc.payment_entry:
			pe_ds = cint(frappe.db.get_value("Payment Entry", doc.payment_entry, "docstatus"))
			if pe_ds == 1:
				frappe.throw(
					_(
						"Rejected PM Request cannot have a submitted Payment Entry. Cancel the Payment Entry first."
					),
					title=_("Invalid state"),
				)
			if pe_ds == 0:
				frappe.throw(
					_(
						"Rejected PM Request cannot have a draft Payment Entry. Cancel the Payment Entry first."
					),
					title=_("Invalid state"),
				)

	if doc.payment_status == "Paid":
		paid = flt(getattr(doc, "total_paid_amount", None))
		if meta.has_field("total_paid_amount"):
			from erpnext_extensions.petty_management.services.funding_queries import sum_submitted_pe_amount

			paid = sum_submitted_pe_amount(doc.name)
		if paid + 1e-6 < flt(doc.total_requested_amount):
			frappe.throw(
				_("Payment Status Paid requires funded amount to cover the request."),
				title=_("Invalid state"),
			)
		if paid <= 0:
			frappe.throw(
				_("Payment Status cannot be Paid without submitted funding."), title=_("Invalid state")
			)
	elif doc.payment_status == "Partially Paid":
		if not meta.has_field("total_paid_amount"):
			frappe.throw(_("Partially Paid is not supported on this site."), title=_("Invalid state"))
		from erpnext_extensions.petty_management.services.funding_queries import sum_submitted_pe_amount

		paid = sum_submitted_pe_amount(doc.name)
		if paid <= 1e-6 or paid + 1e-6 >= flt(doc.total_requested_amount):
			frappe.throw(
				_("Payment Status Partially Paid does not match funded amount."), title=_("Invalid state")
			)

	if doc.payment_status in ("Paid", "Partially Paid") and not doc.payment_entry:
		from erpnext_extensions.petty_management.services.funding_queries import count_linked_payment_entries

		if count_linked_payment_entries(doc.name, docstatus=(1,)) <= 0:
			frappe.throw(
				_("Payment Status requires at least one submitted Payment Entry."), title=_("Invalid state")
			)

	if doc.payment_entry:
		pe_ds = cint(frappe.db.get_value("Payment Entry", doc.payment_entry, "docstatus"))
		if pe_ds == 1 and doc.payment_status == "Not Paid":
			frappe.throw(
				_("Payment Status must reflect submitted Payment Entries."), title=_("Invalid state")
			)


def compute_totals(doc: Document) -> None:
	total = 0.0
	for row in doc.details:
		total += flt(row.advance_amount)
	doc.total_requested_amount = total
	for row in doc.details:
		row.percent_of_total = (flt(row.advance_amount) / total * 100) if total else 0


def sync_request_status_from_workflow(doc: Document) -> None:
	"""Legacy alias — delegates to business status sync (v4.0.2)."""
	sync_pm_request_business_status(doc)


def validate_payment_accounts(doc: Document) -> None:
	validate_petty_cash_account_company(doc.petty_cash_account, doc.company)
	if not doc.employee_bank_account:
		return
	ba = frappe.db.get_value(
		"Bank Account",
		doc.employee_bank_account,
		["party_type", "party", "company"],
		as_dict=True,
	)
	if not ba:
		return
	if ba.get("company") and ba["company"] != doc.company:
		frappe.throw(_("Employee Bank Account must belong to the same company as this request"))
	if ba.get("party_type") == "Employee" and ba.get("party") and ba["party"] != doc.employee:
		frappe.throw(_("Employee Bank Account must be for this request's employee"))


def validate_request_cancel(doc: Document) -> None:
	"""v4.6.8 — cancel only when no open financial process (not payment_entry pointer)."""
	from erpnext_extensions.petty_management.services.request_lifecycle_eligibility import (
		assert_pm_request_cancel_allowed,
	)

	assert_pm_request_cancel_allowed(doc)


def request_ready_for_payment_entry(doc: Document) -> tuple[bool, str]:
	"""Single source of truth for funding eligibility."""
	from erpnext_extensions.petty_management.services.business_status_service import (
		REQUEST_PENDING_WORKFLOW_TITLES,
	)

	ws_early = workflow_state_title(doc)
	if ws_early in REQUEST_PENDING_WORKFLOW_TITLES:
		return False, _("Payment Entry is only available after finance approval.")
	if cint(doc.docstatus) != 1:
		return False, _("Submit the PM Request first.")
	reconcile_payment_entry_link(doc)

	ws_title = workflow_state_title(doc)
	if ws_title == "Rejected" or (doc.status or "").strip() == "Rejected":
		return False, _("This request was rejected.")
	if not request_is_finance_cleared(doc):
		return False, _("Payment Entry is only available after finance approval.")

	if cint(getattr(doc, "is_closed", 0)):
		return False, _("This PM Request is closed.")

	meta = frappe.get_meta("PM Request")
	if meta.has_field("remaining_to_pay"):
		from erpnext_extensions.petty_management.services.funding_queries import (
			has_draft_payment_entry,
			sum_submitted_pe_amount,
		)

		if has_draft_payment_entry(doc.name):
			return False, _("A draft Payment Entry exists. Submit or cancel it before creating another.")
		submitted = sum_submitted_pe_amount(doc.name)
		requested = flt(doc.total_requested_amount)
		remaining = max(0.0, requested - submitted)
		if remaining <= _EPS:
			return False, _(
				"This request has been fully funded. No additional Payment Entry is required."
			)
		return True, ""

	if doc.payment_status == "Paid":
		return False, _("This request is already funded.")
	if doc.payment_entry:
		ds = cint(frappe.db.get_value("Payment Entry", doc.payment_entry, "docstatus"))
		if ds == 0:
			return False, _(
				"A draft Payment Entry already exists. Submit or cancel it before creating another."
			)
		if ds == 1:
			return False, _("A submitted Payment Entry already exists.")
	return True, ""


def find_active_payment_entries_for_pm_request(
	pm_request: str, *, exclude_pe: str | None = None
) -> list[str]:
	"""Non-cancelled Payment Entries tied to this PM Request (reference_no and/or custom_pm_request)."""
	names: set[str] = set(
		frappe.db.sql(
			"""
			select name from `tabPayment Entry`
			where reference_no = %s and docstatus in (0, 1)
			""",
			pm_request,
			pluck=True,
		)
	)
	meta_pe = frappe.get_meta("Payment Entry")
	if meta_pe.has_field("custom_pm_request"):
		for row in frappe.db.sql(
			"""
			select name from `tabPayment Entry`
			where custom_pm_request = %s and docstatus in (0, 1)
			""",
			pm_request,
			pluck=True,
		):
			names.add(row)
	if exclude_pe:
		names.discard(exclude_pe)
	return sorted(names)


def assert_no_active_payment_entry_for_request(doc: Document) -> None:
	"""Raise if another active PE already funds this request."""
	linked = (getattr(doc, "payment_entry", None) or "").strip()
	if linked and frappe.db.exists("Payment Entry", linked):
		ds = cint(frappe.db.get_value("Payment Entry", linked, "docstatus"))
		if ds in (0, 1):
			frappe.throw(
				_("A Payment Entry already exists for this PM Request ({0}).").format(linked),
				title=_("Duplicate funding Payment Entry"),
			)
	for pe_name in find_active_payment_entries_for_pm_request(doc.name, exclude_pe=linked or None):
		frappe.throw(
			_("Another Payment Entry already exists for this PM Request ({0}).").format(pe_name),
			title=_("Duplicate funding Payment Entry"),
		)


def _throw_payment_entry_busy() -> None:
	frappe.throw(
		_("This PM Request is currently being processed. Please refresh and try again."),
		title=_("Please try again"),
	)


def create_payment_entry(pm_request: str, paid_amount: float | None = None) -> str:
	"""Create funding PE with a short row lock on PM Request (validate/build outside the lock)."""
	from erpnext_extensions.petty_management.services.request_api_guard import (
		get_pm_request_doc_for_write,
		get_pm_request_doc_for_write_lock,
	)

	doc = get_pm_request_doc_for_write(pm_request)
	if doc.docstatus != 1:
		frappe.throw(_("Please submit PM Request before creating Payment Entry."))
	if not frappe.has_permission("PM Request", "submit", doc=doc):
		frappe.throw(_("Not permitted to create Payment Entry"), frappe.PermissionError)

	settings = get_pm_settings()
	if not doc.employee:
		frappe.throw(_("Employee is required"))
	if not doc.petty_cash_account:
		frappe.throw(_("Petty Cash Account is missing"))

	ok, reason = request_ready_for_payment_entry(doc)
	if not ok:
		frappe.throw(reason, title=_("Cannot create Payment Entry"))

	paid_from = settings.default_bank_account if settings else None
	if not paid_from:
		frappe.throw(_("Please configure Default Bank Account in PM Settings."))

	from erpnext_extensions.petty_management.services.funding_queries import sum_submitted_pe_amount
	from erpnext_extensions.petty_management.services.funding_service import validate_new_pe_amount

	requested = flt(doc.total_requested_amount)
	if requested <= 0:
		frappe.throw(_("Total Requested Amount must be positive"))
	submitted = sum_submitted_pe_amount(doc.name)
	default_amount = max(0.0, requested - submitted)
	amount = flt(paid_amount) if paid_amount is not None else default_amount
	if amount <= 0:
		frappe.throw(_("Payment amount must be greater than zero."))
	validate_new_pe_amount(doc.name, amount)

	pe = _build_payment_entry(doc, paid_from, amount)

	try:
		doc_locked = get_pm_request_doc_for_write_lock(pm_request)
	except QueryTimeoutError:
		_throw_payment_entry_busy()

	try:
		ok, reason = request_ready_for_payment_entry(doc_locked)
		if not ok:
			frappe.throw(reason, title=_("Cannot create Payment Entry"))
		validate_new_pe_amount(doc_locked.name, amount)

		pe.insert(ignore_permissions=True)
		from erpnext_extensions.petty_management.services.narration_service import (
			apply_funding_payment_entry_remarks,
		)

		apply_funding_payment_entry_remarks(pe, doc_locked, amount)
		pe.db_set(
			{
				"remarks": pe.remarks,
				"custom_remarks": 1 if frappe.get_meta("Payment Entry").has_field("custom_remarks") else 0,
			},
			update_modified=False,
		)
		dupes = find_active_payment_entries_for_pm_request(doc_locked.name, exclude_pe=pe.name)
		if dupes and not frappe.get_meta("PM Request").has_field("total_paid_amount"):
			frappe.throw(
				_("Another Payment Entry already exists for this PM Request ({0}).").format(dupes[0]),
				title=_("Duplicate funding Payment Entry"),
			)

		from erpnext_extensions.petty_management.services.funding_service import (
			sync_pm_request_funding_fields,
		)

		doc_locked.payment_entry = pe.name
		sync_pm_request_funding_fields(doc_locked)
		frappe.db.commit()
	except QueryTimeoutError:
		frappe.db.rollback()
		_throw_payment_entry_busy()
	except Exception as e:
		frappe.db.rollback()
		frappe.throw(
			_("Payment Entry could not be created: {0}").format(str(e)), title=_("Payment Entry failed")
		)

	if settings and settings.auto_submit_payment_entry:
		try:
			apply_funding_payment_entry_remarks(pe, doc, amount)
			pe.db_set(
				{
					"remarks": pe.remarks,
					"custom_remarks": 1
					if frappe.get_meta("Payment Entry").has_field("custom_remarks")
					else 0,
				},
				update_modified=False,
			)
			pe.submit()
			from erpnext_extensions.petty_management.services.funding_service import (
				sync_pm_request_funding_fields,
			)

			sync_pm_request_funding_fields(doc.name)
		except Exception as e:
			frappe.db.rollback()
			frappe.throw(
				_("Payment Entry could not be submitted: {0}").format(str(e)), title=_("Payment Entry failed")
			)

	try:
		from erpnext_extensions.petty_management import petty_audit

		petty_audit.log_event(
			"pm_payment_entry_created",
			pm_request=doc.name,
			payment_entry=pe.name,
			holder=doc.holder,
			employee=doc.employee,
			amount=amount,
			company=doc.company,
			auto_submit=bool(settings and settings.auto_submit_payment_entry),
		)
	except Exception:
		pass
	return pe.name


def _build_payment_entry(doc: Document, paid_from: str, amount: float) -> Document:
	company_currency = frappe.db.get_value("Company", doc.company, "default_currency")

	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = "Pay"
	pe.company = doc.company
	pe.posting_date = doc.transaction_date or today()
	pe.party_type = "Employee"
	pe.party = doc.employee
	pe.paid_from = paid_from
	pe.paid_to = doc.petty_cash_account
	pe.paid_amount = amount
	pe.received_amount = amount
	pe.target_exchange_rate = 1
	pe.source_exchange_rate = 1
	if company_currency:
		pe.paid_to_account_currency = company_currency
		pe.paid_from_account_currency = company_currency
	pe.reference_no = doc.name
	pe.reference_date = getdate(doc.transaction_date) if doc.transaction_date else pe.posting_date

	meta_pe = frappe.get_meta("Payment Entry")
	if doc.employee_bank_account and meta_pe.has_field("party_bank_account"):
		pe.party_bank_account = doc.employee_bank_account

	from erpnext_extensions.petty_management.services.narration_service import (
		apply_funding_payment_entry_remarks,
	)

	apply_funding_payment_entry_remarks(pe, doc, amount)

	if meta_pe.has_field("custom_pm_request"):
		pe.custom_pm_request = doc.name
	if meta_pe.has_field("custom_pm_holder") and doc.holder:
		pe.custom_pm_holder = doc.holder
	if doc.project and meta_pe.has_field("project"):
		pe.project = doc.project
	return pe


def get_pm_request_action_flags_for_doc(doc: Document) -> dict:
	"""Desk UI flags for a PM Request document already loaded via request_api_guard."""
	from erpnext_extensions.petty_management.services.request_action_policy import (
		compute_pm_request_action_flags,
	)

	flags = compute_pm_request_action_flags(doc)
	flags["reason"] = flags.get("create_block_reason") or ""
	return flags


def get_pm_request_action_flags(pm_request: str) -> dict:
	"""Deprecated: use whitelisted API with request_api_guard. Kept for internal callers."""
	from erpnext_extensions.petty_management.services.request_api_guard import get_pm_request_doc_for_read

	doc = get_pm_request_doc_for_read(pm_request)
	return get_pm_request_action_flags_for_doc(doc)


def close_pm_request(
	pm_request: str,
	close_reason: str | None = None,
	close_reason_detail: str | None = None,
) -> None:
	from erpnext_extensions.petty_management.services.funding_service import close_pm_request as _close

	_close(pm_request, close_reason=close_reason, close_reason_detail=close_reason_detail)


def cancel_pm_request(pm_request: str) -> None:
	"""v4.8.5 — business Cancel PM Request (DocPerm-independent; reuses v4.6.8 eligibility)."""
	from erpnext_extensions.petty_management.services.request_api_guard import (
		get_pm_request_doc_for_write,
	)
	from erpnext_extensions.petty_management.services.request_lifecycle_eligibility import (
		assert_pm_request_cancel_allowed,
		cancel_pm_request_action_flags,
		user_may_execute_pm_request_cancel,
	)

	doc = get_pm_request_doc_for_write(pm_request)
	if not user_may_execute_pm_request_cancel(doc):
		frappe.throw(_("Not permitted to cancel this PM Request."), frappe.PermissionError)
	can_cancel, reason = cancel_pm_request_action_flags(doc)
	if not can_cancel:
		if reason:
			frappe.throw(reason, title=_("Cannot cancel PM Request"))
		frappe.throw(_("Cannot cancel PM Request."), title=_("Cannot cancel PM Request"))
	assert_pm_request_cancel_allowed(doc)

	doc.flags.ignore_permissions = True
	try:
		doc.cancel()
	finally:
		doc.flags.ignore_permissions = False


def delete_pm_request(pm_request: str) -> None:
	"""v4.8.6 — business Delete PM Request (DocPerm-independent; reuses v4.6.8 eligibility)."""
	from erpnext_extensions.petty_management.services.request_api_guard import (
		get_pm_request_doc_for_write,
	)
	from erpnext_extensions.petty_management.services.request_lifecycle_eligibility import (
		assert_pm_request_delete_allowed,
		delete_pm_request_action_flags,
		user_may_execute_pm_request_delete,
	)

	doc = get_pm_request_doc_for_write(pm_request)
	if not user_may_execute_pm_request_delete(doc):
		frappe.throw(_("Not permitted to delete this PM Request."), frappe.PermissionError)
	can_delete, reason = delete_pm_request_action_flags(doc)
	if not can_delete:
		if reason:
			frappe.throw(reason, title=_("Cannot delete PM Request"))
		frappe.throw(_("Cannot delete PM Request."), title=_("Cannot delete PM Request"))
	assert_pm_request_delete_allowed(doc)

	doc.flags.ignore_permissions = True
	try:
		doc.delete()
	finally:
		doc.flags.ignore_permissions = False
