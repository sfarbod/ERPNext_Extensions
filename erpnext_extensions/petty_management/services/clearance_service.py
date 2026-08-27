from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate, today

from erpnext_extensions.petty_management.services.allocation_service import validate_request_allocations
from erpnext_extensions.petty_management.services.clearance_action_policy import (
	sync_clearance_lifecycle,
	sync_clearance_lifecycle_if_stale,
	validate_pm_clearance_workflow_change,
)
from erpnext_extensions.petty_management.services.constants import (
	EPSILON,
	FUNDING_SOURCE_OPENING_ADVANCE,
	FUNDING_SOURCE_PM_REQUEST,
	SETTLEMENT_PI,
	SETTLEMENT_SA,
)
from erpnext_extensions.petty_management.services.holder_service import (
	clearance_exclude_name_for_validation,
	clearance_petty_cash_account,
	get_holder,
	get_holder_balances,
	get_holder_petty_cash_account,
	sync_clearance_holder_fields,
)
from erpnext_extensions.petty_management.services.opening_advance_service import (
	allocation_row_funding_source_type,
)
from erpnext_extensions.petty_management.utils import get_pm_settings


def before_validate_clearance(doc: Document) -> None:
	if doc.docstatus == 0:
		normalize_funding_allocation_rows(doc)
		prune_empty_request_allocation_rows(doc)


def normalize_funding_allocation_rows(doc: Document) -> None:
	for row in doc.get("request_allocations") or []:
		if getattr(row, "is_legacy_row", 0):
			continue
		source_type = allocation_row_funding_source_type(row)
		if source_type == FUNDING_SOURCE_OPENING_ADVANCE:
			row.funding_source_type = FUNDING_SOURCE_OPENING_ADVANCE
			row.pm_request = None
		else:
			row.funding_source_type = FUNDING_SOURCE_PM_REQUEST
			row.pm_opening_advance = None


def validate_clearance(doc: Document) -> None:
	from erpnext_extensions.petty_management.services.draft_approval_guards import (
		assert_pending_not_editable,
	)

	assert_pending_not_editable(doc)
	doc.je_clearance_date = getdate(doc.transaction_date or today())
	sync_clearance_holder_fields(doc)
	ensure_petty_cash_account_filled(doc)
	normalize_settlement_types(doc)
	stamp_rows(doc)
	validate_details_not_empty(doc)
	validate_duplicate_settlement_targets(doc)
	validate_and_stamp_pi_rows(doc)
	validate_and_stamp_supplier_advance_rows(doc)
	calc_line_totals(doc)
	calc_parent_totals(doc)
	validate_request_allocations(doc)
	validate_pm_clearance_workflow_change(doc)
	sync_clearance_lifecycle_if_stale(doc)
	validate_clearance_policy(doc)


def _policy_available_for_clearance(doc: Document) -> tuple[float, float, float]:
	"""Holder-level available for policy; submitted clearances exclude their own reservation."""
	exclude = clearance_exclude_name_for_validation(doc)

	holder_name = (doc.holder or "").strip()
	if not holder_name and doc.employee and doc.company:
		h = get_holder(doc.employee, doc.company, required=False)
		holder_name = (h.name if h else "").strip()

	if holder_name:
		balances = get_holder_balances(
			holder_name,
			posting_date=getdate(doc.transaction_date or today()),
			exclude_clearance_name=exclude,
		)
		total_avail = balances.available_amount
		funded_avail = balances.funded_available_amount
		opening_avail = balances.opening_available_amount
	else:
		total_avail = flt(getattr(doc, "total_available", None) or getattr(doc, "pending_amount", None))
		funded_avail = flt(getattr(doc, "funded_available", 0))
		opening_avail = flt(getattr(doc, "opening_available", 0))

	if cint(doc.docstatus) == 1 and doc.get("request_allocations"):
		non_legacy = [r for r in doc.request_allocations if not getattr(r, "is_legacy_row", 0)]
		if non_legacy:
			# Row snapshots (just validated) use per-source SQL with exclude_clearance; use as floor when
			# holder aggregate paid SQL is stale under heavy sequential suites.
			row_headroom = sum(flt(r.available_amount) for r in non_legacy)
			if row_headroom > total_avail + EPSILON:
				total_avail = row_headroom
				if funded_avail + opening_avail < total_avail - EPSILON:
					funded_avail = max(funded_avail, row_headroom - opening_avail)

	doc.funded_available = funded_avail
	doc.opening_available = opening_avail
	doc.total_available = total_avail
	doc.pending_amount = total_avail
	return total_avail, funded_avail, opening_avail


def validate_clearance_policy(doc: Document) -> None:
	settings = get_pm_settings()
	if not doc.request_allocations:
		frappe.throw(_("Add at least one funding allocation line"))
	if flt(doc.total_expense_amount) <= 0:
		frappe.throw(_("Total settlement amount must be greater than zero"))

	allow_neg = bool(settings and settings.allow_negative_balance)
	total_avail, funded_avail, opening_avail = _policy_available_for_clearance(doc)
	if not allow_neg and flt(doc.total_expense_amount) > total_avail + EPSILON:
		frappe.throw(
			_("Clearance total {0} exceeds total available balance {1} (Funded {2} + Opening {3}).").format(
				doc.total_expense_amount,
				total_avail,
				funded_avail,
				opening_avail,
			)
		)

	for row in doc.details:
		if settings and settings.require_attachment and not row.proof:
			frappe.throw(_("Row {0}: attachment is required by PM Settings").format(row.idx))
		if settings and settings.require_bill_no and not row.bill_no:
			frappe.throw(_("Row {0}: bill number is required by PM Settings").format(row.idx))


def ensure_petty_cash_account_filled(doc: Document) -> None:
	if doc.holder and not (doc.petty_cash_account or "").strip():
		doc.petty_cash_account = get_holder_petty_cash_account(doc.holder)


def normalize_settlement_types(doc: Document) -> None:
	for row in doc.details:
		if not (getattr(row, "settlement_type", None) or "").strip():
			row.settlement_type = SETTLEMENT_PI
		st = (row.settlement_type or SETTLEMENT_PI).strip()
		if st == SETTLEMENT_PI:
			row.purchase_order = None
			row.supplier_advance_account = None
		else:
			row.purchase_invoice = None
			row.outstanding_amount = 0
			row.reference_doctype = None


def on_submit_clearance(doc: Document) -> None:
	refreshed = frappe.get_doc("PM Clearance", doc.name)
	sync_clearance_lifecycle(refreshed, persist=True)


def before_cancel_clearance(doc: Document) -> None:
	"""Accounting-safe cancel: never auto-cancel GL. User must cancel JE first."""
	if not doc.journal_entry:
		return
	je_ds = cint(frappe.db.get_value("Journal Entry", doc.journal_entry, "docstatus"))
	if je_ds == 1:
		frappe.throw(
			_("Cancel the settlement Journal Entry ({0}) before cancelling this clearance.").format(
				doc.journal_entry
			),
			title=_("Journal Entry submitted"),
		)


def on_cancel_clearance(doc: Document) -> None:
	frappe.db.set_value(
		"PM Clearance",
		doc.name,
		{"journal_entry": None, "purchase_invoice": None},
		update_modified=False,
	)
	doc.journal_entry = None
	doc.reload()
	doc.docstatus = 2
	from erpnext_extensions.petty_management.services.clearance_action_policy import (
		LIFECYCLE_CANCELLED,
		sync_clearance_lifecycle,
	)

	sync_clearance_lifecycle(doc, persist=True)
	frappe.db.set_value(
		"PM Clearance",
		doc.name,
		{"status": LIFECYCLE_CANCELLED},
		update_modified=False,
	)
	for row_name in frappe.get_all(
		"PM Clearance Detail",
		filters={"parent": doc.name, "parenttype": "PM Clearance"},
		pluck="name",
	):
		frappe.db.set_value(
			"PM Clearance Detail",
			row_name,
			{"generated_doctype": None, "generated_document": None},
			update_modified=False,
		)


def prune_empty_request_allocation_rows(doc: Document) -> None:
	for row in list(doc.get("request_allocations") or []):
		if getattr(row, "is_legacy_row", 0):
			continue
		has_req = bool((row.pm_request or "").strip())
		has_opening = bool((getattr(row, "pm_opening_advance", None) or "").strip())
		has_amt = flt(row.allocated_amount) != 0
		if not has_req and not has_opening and not has_amt:
			doc.remove(row)


def stamp_rows(doc: Document) -> None:
	for row in doc.details:
		if not row.created_by_user:
			row.created_by_user = frappe.session.user


def validate_details_not_empty(doc: Document) -> None:
	if not doc.details:
		frappe.throw(_("Add at least one settlement line"))


def validate_duplicate_settlement_targets(doc: Document) -> None:
	seen_pi = set()
	seen_po = set()
	for row in doc.details:
		st = (row.settlement_type or SETTLEMENT_PI).strip()
		if st == SETTLEMENT_PI:
			if not row.purchase_invoice:
				continue
			if row.purchase_invoice in seen_pi:
				frappe.throw(
					_("Purchase Invoice {0} cannot appear on more than one line.").format(
						row.purchase_invoice
					),
					title=_("Duplicate Purchase Invoice"),
				)
			seen_pi.add(row.purchase_invoice)
		elif st == SETTLEMENT_SA:
			if not row.purchase_order:
				continue
			if row.purchase_order in seen_po:
				frappe.throw(
					_("Purchase Order {0} cannot appear on more than one line.").format(row.purchase_order),
					title=_("Duplicate Purchase Order"),
				)
			seen_po.add(row.purchase_order)


def validate_and_stamp_pi_rows(doc: Document) -> None:
	"""Prepare-mode PI validation (v4.1.5): Draft + Submitted allowed; Cancelled blocked.

	Finance / Settle / Preview use ``purchase_invoice_readiness`` independently.
	"""
	from erpnext_extensions.petty_management.services.purchase_invoice_readiness import (
		validate_purchase_invoices_for_prepare,
	)

	validate_purchase_invoices_for_prepare(doc)


def validate_and_stamp_supplier_advance_rows(doc: Document) -> None:
	for row in doc.details:
		if (row.settlement_type or SETTLEMENT_PI).strip() != SETTLEMENT_SA:
			continue
		if not row.purchase_order:
			frappe.throw(_("Row {0}: Purchase Order is required for Supplier Advance.").format(row.idx))
		if not row.supplier_advance_account:
			frappe.throw(_("Row {0}: Supplier Advance Account is required.").format(row.idx))
		po = frappe.get_doc("Purchase Order", row.purchase_order)
		if po.docstatus != 1:
			frappe.throw(_("Row {0}: Purchase Order must be submitted.").format(row.idx))
		if po.company != doc.company:
			frappe.throw(_("Row {0}: Purchase Order belongs to another company.").format(row.idx))
		row.supplier = po.supplier
		acc_co, acc_type, acc_is_group = frappe.db.get_value(
			"Account",
			row.supplier_advance_account,
			["company", "account_type", "is_group"],
		) or (None, None, None)
		if acc_co and acc_co != doc.company:
			frappe.throw(
				_("Row {0}: Supplier Advance Account must belong to the clearance company.").format(row.idx)
			)
		if cint(acc_is_group):
			frappe.throw(
				_(
					"Row {0}: Supplier Advance Account {1} is a group account; select a ledger account."
				).format(row.idx, row.supplier_advance_account),
				title=_("Invalid Supplier Advance Account"),
			)
		# The settlement JE records the supplier advance against the Purchase Order with a
		# Supplier party. ERPNext only allows a party on a "Payable" account, and its
		# advance-against-order handling (self-referencing the JE) only reconciles correctly
		# for a Payable-type account. Any other type makes the JE unsubmittable and would
		# otherwise fail late with a cryptic accounting error, so guard it here.
		if (acc_type or "") != "Payable":
			frappe.throw(
				_(
					"Row {0}: Supplier Advance Account {1} must be an account with type 'Payable' "
					"(a supplier advance / advance-paid account). Its current type is '{2}'."
				).format(row.idx, row.supplier_advance_account, acc_type or _("<not set>")),
				title=_("Invalid Supplier Advance Account"),
			)
		if flt(row.allocated_amount) <= 0:
			frappe.throw(_("Row {0}: Allocated Amount must be greater than zero.").format(row.idx))


def calc_line_totals(doc: Document) -> None:
	for row in doc.details:
		row.amount_plus_tax = flt(row.allocated_amount)


def calc_parent_totals(doc: Document) -> None:
	total = sum(flt(row.allocated_amount) for row in doc.details)
	doc.total_expense_without_tax = 0
	doc.total_tax_amount = 0
	doc.total_expense_amount = total
	doc.total_petty_cash = total
	doc.remaining_amount = flt(getattr(doc, "total_available", None) or doc.pending_amount) - flt(
		doc.total_expense_amount
	)


def sync_clearance_status_from_workflow(doc: Document) -> None:
	"""Backward-compatible alias; prefer :func:`sync_clearance_lifecycle`."""
	sync_clearance_lifecycle(doc, persist=False)


def clearance_is_approved(doc: Document) -> bool:
	"""True when clearance is finance-approved for Settle (business ``status == Approved``)."""
	st = (getattr(doc, "status", None) or "").strip()
	if st in ("Rejected", "Cancelled", "Settled", "Pending Journal Entry Submission"):
		return False
	if st == "Approved":
		return True
	# Legacy: status still Pending Finance Review but workflow already Approved and no JE
	ws = (getattr(doc, "workflow_state", None) or "").strip()
	ws_title = (
		frappe.db.get_value("Workflow State", ws, "workflow_state_name") if ws else ""
	) or ws
	je = (getattr(doc, "journal_entry", None) or "").strip()
	if ws_title == "Approved" and not je and st in ("", "Pending Finance Review", "Pending Approval"):
		return True
	return False


def workflow_state_link_for_title(document_type: str, state_title: str) -> str | None:
	"""Resolve active workflow link field value for a human-readable state title."""
	wf_name = frappe.db.get_value("Workflow", {"document_type": document_type, "is_active": 1}, "name")
	if not wf_name:
		return None
	wf = frappe.get_doc("Workflow", wf_name)
	for s in wf.states:
		title = frappe.db.get_value("Workflow State", s.state, "workflow_state_name")
		if title == state_title:
			return s.state
	from erpnext_extensions.petty_management.services.workflow_utils import resolve_workflow_state_link

	return resolve_workflow_state_link(state_title)


def approve_pm_clearance_for_reservation(cl_name: str) -> None:
	"""Apply final Clearance Finance Approve via the legitimate workflow submit path.

	v4.7.2: MUST NOT write docstatus/workflow_state via db.set_value.
	Finance Approve is the only real Submit (Pending Finance Review 0 → Approved 1).

	Requires the document to already be in Pending Finance Review (or already Approved).
	"""
	from erpnext_extensions.petty_management.services.workflow_utils import apply_pm_workflow

	doc = frappe.get_doc("PM Clearance", cl_name)
	ws = (getattr(doc, "workflow_state", None) or "").strip()
	title = (
		frappe.db.get_value("Workflow State", ws, "workflow_state_name") if ws else ""
	) or ws

	if title == "Approved" and cint(doc.docstatus) == 1:
		return

	if title != "Pending Finance Review":
		frappe.throw(
			_(
				"Cannot approve PM Clearance {0} for reservation: expected Pending Finance Review, got {1}. "
				"Use the normal approval workflow (Manager → Finance Approve)."
			).format(cl_name, title or _("(blank)")),
			title=_("Finance Approve required"),
		)

	from erpnext_extensions.petty_management.services.purchase_invoice_readiness import (
		validate_purchase_invoices_for_finance_approval,
	)

	validate_purchase_invoices_for_finance_approval(doc)

	try:
		apply_pm_workflow(doc, "PM Finance Approve")
	except Exception:
		doc.reload()
		apply_pm_workflow(doc, "PM Approve")

	doc.reload()
	if cint(doc.docstatus) != 1:
		frappe.throw(
			_("Finance Approve did not submit PM Clearance {0} (docstatus={1}).").format(
				cl_name, doc.docstatus
			),
			title=_("Submit failed"),
		)


def prepare_doc_for_je_preview(doc: Document) -> None:
	sync_clearance_holder_fields(doc)
	ensure_petty_cash_account_filled(doc)
	normalize_settlement_types(doc)
	validate_duplicate_settlement_targets(doc)
	from erpnext_extensions.petty_management.services.purchase_invoice_readiness import (
		validate_purchase_invoices_for_settlement,
	)

	# Settlement readiness (blocks Draft PI) — independent of prepare-mode stamp.
	validate_purchase_invoices_for_settlement(doc)
	validate_and_stamp_supplier_advance_rows(doc)
	calc_line_totals(doc)
	calc_parent_totals(doc)
	validate_request_allocations(doc)
