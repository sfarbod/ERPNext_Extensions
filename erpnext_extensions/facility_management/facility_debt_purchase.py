# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Debt Purchase Cheque settlement helpers for Facility Repayment.

Bank Account repayments never call this module for PDC side-effects.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt
from frappe.utils.synchronization import filelock

from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
	resolve_pdc_accounts_for_journal,
)
from erpnext_extensions.cheque_management.pdc_accounting_idempotency import (
	build_pdc_accounting_transition_key,
	normalize_cheque_direction_for_accounting_key,
)
from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	CHEQUE_DIRECTION_RECEIVABLE,
	WORKFLOW_ASSIGNED_DEBT_PURCHASE,
	WORKFLOW_DEBT_PURCHASE_SETTLED,
	normalize_workflow_state_value,
)
from erpnext_extensions.cheque_management.pdc_workflow_to_cheque_status import (
	map_workflow_state_to_cheque_status,
)
from erpnext_extensions.facility_management.facility_monetary import parse_facility_amount

REPAYMENT_METHOD_BANK = "Bank Account"
REPAYMENT_METHOD_DEBT_PURCHASE = "Debt Purchase Cheque"

PURPOSE_DEBT_PURCHASE_SETTLEMENT = "Debt Purchase Settlement"


def normalize_repayment_method(value: str | None) -> str:
	"""Empty/NULL legacy rows behave exactly as Bank Account."""
	s = (value or "").strip()
	if not s or s == REPAYMENT_METHOD_BANK:
		return REPAYMENT_METHOD_BANK
	return s


def is_debt_purchase_cheque_method(repayment) -> bool:
	return normalize_repayment_method(getattr(repayment, "repayment_method", None)) == REPAYMENT_METHOD_DEBT_PURCHASE


def resolve_debt_purchase_in_collection_account(pdc) -> str | None:
	acc = resolve_pdc_accounts_for_journal(pdc)
	return (acc.get("debt_purchase_in_collection") or "").strip() or None


def validate_bank_account_method_fields(repayment) -> None:
	if (getattr(repayment, "post_dated_cheque", None) or "").strip():
		frappe.throw(
			_("Post Dated Cheque must be empty when Repayment Method is Bank Account."),
			title=_("Repayment Method"),
		)
	if not (getattr(repayment, "bank_account", None) or "").strip():
		# Prerequisites / resolve_account also require bank; keep explicit for clarity.
		facility = frappe.get_doc("Facility", repayment.facility)
		from erpnext_extensions.facility_management.facility_settings_doc import (
			get_facility_settings_doc,
			resolve_account,
		)

		settings = get_facility_settings_doc(facility.company)
		if not resolve_account(
			"bank_account", repayment=repayment, facility=facility, settings=settings, required=False
		):
			frappe.throw(_("Bank Account is required for Bank Account repayments."), title=_("Repayment Method"))


def validate_debt_purchase_cheque_repayment(repayment, facility=None) -> dict[str, Any]:
	"""Server-side eligibility for Debt Purchase Cheque method. Returns PDC doc dict context."""
	pdc_name = (getattr(repayment, "post_dated_cheque", None) or "").strip()
	if not pdc_name:
		frappe.throw(
			_("Post Dated Cheque is required when Repayment Method is Debt Purchase Cheque."),
			title=_("Debt Purchase Cheque"),
		)

	facility = facility or frappe.get_doc("Facility", repayment.facility)
	ft = (facility.facility_type or "").strip()
	if not ft or not cint(frappe.db.get_value("Facility Type", ft, "is_debt_purchase")):
		frappe.throw(
			_("Facility Type must have Is Debt Purchase enabled for Debt Purchase Cheque repayments."),
			title=_("Debt Purchase Cheque"),
		)

	if flt(getattr(repayment, "penalty_amount", 0)):
		frappe.throw(
			_("Penalty Amount must be zero for Debt Purchase Cheque repayments (v1)."),
			title=_("Debt Purchase Cheque"),
		)

	pdc = frappe.get_doc("Post Dated Cheque", pdc_name)
	if (pdc.cheque_direction or "").strip() != CHEQUE_DIRECTION_RECEIVABLE:
		frappe.throw(_("Selected cheque must be Receivable."), title=_("Debt Purchase Cheque"))

	ws = normalize_workflow_state_value(pdc.workflow_state)
	if ws != WORKFLOW_ASSIGNED_DEBT_PURCHASE:
		frappe.throw(
			_(
				"Cheque must be in Workflow State {0}. Current state is {1}."
			).format(WORKFLOW_ASSIGNED_DEBT_PURCHASE, ws),
			title=_("Debt Purchase Cheque"),
		)

	if (pdc.company or "").strip() != (repayment.company or facility.company or "").strip():
		frappe.throw(_("Cheque company must match Facility Repayment company."), title=_("Debt Purchase Cheque"))

	# Currency: Facility uses company default; PDC may store currency — compare when both set.
	pdc_cur = (getattr(pdc, "currency", None) or "").strip()
	rep_cur = (getattr(repayment, "currency", None) or "").strip()
	if not rep_cur:
		rep_cur = (frappe.get_cached_value("Company", facility.company, "default_currency") or "").strip()
	if pdc_cur and rep_cur and pdc_cur != rep_cur:
		frappe.throw(
			_("Cheque currency ({0}) must match Facility Repayment currency ({1}).").format(pdc_cur, rep_cur),
			title=_("Debt Purchase Cheque"),
		)

	if (getattr(pdc, "debt_purchase_repayment", None) or "").strip():
		linked = pdc.debt_purchase_repayment
		if linked != repayment.name:
			frappe.throw(
				_("Cheque is already linked to Facility Repayment {0}.").format(linked),
				title=_("Debt Purchase Cheque"),
			)

	if ws == WORKFLOW_DEBT_PURCHASE_SETTLED:
		frappe.throw(_("Cheque has already been settled for Debt Purchase."), title=_("Debt Purchase Cheque"))

	other = frappe.db.sql(
		"""
		SELECT name FROM `tabFacility Repayment`
		WHERE post_dated_cheque = %s AND docstatus = 1 AND name != %s
		LIMIT 1
		""",
		(pdc_name, repayment.name or ""),
	)
	if other:
		frappe.throw(
			_("Cheque is already used on submitted Facility Repayment {0}.").format(other[0][0]),
			title=_("Debt Purchase Cheque"),
		)

	# New repayments: PDC must appear on Facility.debt_purchase_pdcs.
	from erpnext_extensions.facility_management.facility_pdc_allocation import (
		assert_repayment_pdc_allocated,
	)

	assert_repayment_pdc_allocated(repayment, facility)

	principal = parse_facility_amount(getattr(repayment, "principal_amount", 0))
	profit = parse_facility_amount(getattr(repayment, "profit_amount", 0))
	cheque_amt = parse_facility_amount(getattr(pdc, "cheque_amount", 0))
	if cheque_amt != principal + profit:
		frappe.throw(
			_(
				"Cheque amount ({0}) must exactly equal Principal ({1}) + Profit ({2})."
			).format(cheque_amt, principal, profit),
			title=_("Debt Purchase Cheque"),
		)

	dpic = resolve_debt_purchase_in_collection_account(pdc)
	if not dpic:
		frappe.throw(
			_(
				"Debt Purchase In Collection account is not configured. "
				"Set Default Debt Purchase In Collection Account in PDC Settings."
			),
			title=_("Debt Purchase Cheque"),
		)

	return {"pdc": pdc, "dpic_account": dpic, "principal": principal, "profit": profit}


def _settlement_transition_key(pdc) -> str:
	ch_dir = normalize_cheque_direction_for_accounting_key(getattr(pdc, "cheque_direction", None))
	return build_pdc_accounting_transition_key(
		pdc.name, ch_dir, WORKFLOW_ASSIGNED_DEBT_PURCHASE, WORKFLOW_DEBT_PURCHASE_SETTLED
	)


def append_pdc_settlement_journal_reference(pdc, repayment, je_name: str) -> None:
	"""Persist exactly one Debt Purchase Settlement PDC Journal Reference (idempotent by transition key)."""
	je_name = (je_name or "").strip()
	if not je_name:
		frappe.throw(_("Settlement Journal Entry is required for Debt Purchase Settlement reference."))

	key = _settlement_transition_key(pdc)
	existing = frappe.db.get_value(
		"PDC Journal Reference",
		{"parent": pdc.name, "parenttype": "Post Dated Cheque", "pdc_transition_key": key},
		"name",
	)
	if existing:
		return

	pdc.reload()
	# Re-check after reload (same transaction / concurrent caller under filelock).
	if any((r.pdc_transition_key or "") == key for r in (pdc.journal_references or [])):
		return

	pdc.append(
		"journal_references",
		{
			"journal_entry": je_name,
			"purpose": PURPOSE_DEBT_PURCHASE_SETTLEMENT,
			"amount": flt(pdc.cheque_amount),
			"pdc_transition_key": key,
			"posting_date": repayment.posting_date,
		},
	)
	pdc.flags.ignore_validate_update_after_submit = True
	pdc.flags.skip_pdc_accounting_orchestration = True
	pdc.save(ignore_permissions=True)


def remove_pdc_settlement_journal_reference(
	pdc, repayment, *, settlement_je: str | None = None
) -> None:
	"""Remove Debt Purchase Settlement Journal Reference only. Idempotent if already absent.

	Does not remove Assignment or any other PDC Journal Reference purposes.
	Matches by the Assigned → Settled transition key (primary). Falls back to purpose
	``Debt Purchase Settlement`` + settlement JE when the key is missing on a legacy row.
	"""
	key = _settlement_transition_key(pdc)
	# Prefer explicit JE (cancel clears ``repayment.journal_entry`` before restore).
	settlement_je = (settlement_je or getattr(repayment, "journal_entry", None) or "").strip()

	pdc.reload()
	to_remove = []
	for r in pdc.journal_references or []:
		row_key = (r.pdc_transition_key or "").strip()
		row_purpose = (r.purpose or "").strip()
		row_je = (r.journal_entry or "").strip()
		if row_key == key:
			to_remove.append(r)
			continue
		if row_purpose != PURPOSE_DEBT_PURCHASE_SETTLEMENT:
			continue
		if settlement_je and row_je == settlement_je:
			to_remove.append(r)

	if not to_remove:
		return

	for r in to_remove:
		pdc.remove(r)
	pdc.flags.ignore_validate_update_after_submit = True
	pdc.flags.skip_pdc_accounting_orchestration = True
	pdc.flags.ignore_links = True
	pdc.save(ignore_permissions=True)


def apply_pdc_debt_purchase_settled(pdc, repayment, je_name: str) -> None:
	"""Mark PDC Settled + persist settlement Journal Reference.

	Uses ``db_set`` for workflow_state because Debt Purchase Settled is Facility-owned and is
	intentionally absent from the Desk Workflow transition graph (Frappe Workflow would otherwise
	block Assigned → Settled). Custom state-machine validation is bypassed via the settlement flag.
	"""
	frappe.flags.in_debt_purchase_facility_settlement = True
	try:
		pdc.reload()
		mapped = map_workflow_state_to_cheque_status(
			CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_DEBT_PURCHASE_SETTLED
		)
		updates = {
			"workflow_state": WORKFLOW_DEBT_PURCHASE_SETTLED,
			"debt_purchase_facility": repayment.facility,
			"debt_purchase_repayment": repayment.name,
		}
		if mapped:
			updates["cheque_status"] = mapped
		frappe.db.set_value("Post Dated Cheque", pdc.name, updates, update_modified=True)
		append_pdc_settlement_journal_reference(pdc, repayment, je_name)
	finally:
		frappe.flags.in_debt_purchase_facility_settlement = False


def restore_pdc_after_debt_purchase_cancel(
	pdc, repayment, *, settlement_je: str | None = None
) -> None:
	"""Restore Assigned after settlement JE was cancelled. Uses ``db_set`` for workflow_state
	(symmetric with settle — Settled is not a Desk Workflow edge).

	Clears settlement links before child-table save so cancel does not hit CancelledLinkError
	(Facility Repayment is already docstatus=2 when ``on_cancel`` runs).
	"""
	frappe.flags.in_debt_purchase_facility_settlement = True
	try:
		pdc.reload()
		if normalize_workflow_state_value(pdc.workflow_state) != WORKFLOW_DEBT_PURCHASE_SETTLED:
			frappe.throw(
				_("Linked cheque is not in Debt Purchase Settled state."),
				title=_("Debt Purchase Cancel"),
			)
		if (pdc.debt_purchase_repayment or "").strip() != repayment.name:
			frappe.throw(
				_("Cheque is not settled by this Facility Repayment."),
				title=_("Debt Purchase Cancel"),
			)
		mapped = map_workflow_state_to_cheque_status(
			CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE
		)
		updates = {
			"workflow_state": WORKFLOW_ASSIGNED_DEBT_PURCHASE,
			"debt_purchase_facility": None,
			"debt_purchase_repayment": None,
		}
		if mapped:
			updates["cheque_status"] = mapped
		frappe.db.set_value("Post Dated Cheque", pdc.name, updates, update_modified=True)
		remove_pdc_settlement_journal_reference(pdc, repayment, settlement_je=settlement_je)
	finally:
		frappe.flags.in_debt_purchase_facility_settlement = False


def settle_debt_purchase_on_submit(repayment, je_name: str) -> None:
	"""Atomic PDC side-effects after Facility Repayment JE is submitted (same DB transaction)."""
	pdc_name = (repayment.post_dated_cheque or "").strip()
	lock_name = f"pdc_debt_purchase_settle_{pdc_name}"
	with filelock(lock_name, timeout=120):
		ctx = validate_debt_purchase_cheque_repayment(repayment)
		pdc = ctx["pdc"]
		pdc.reload()
		# Re-check uniqueness under lock
		validate_debt_purchase_cheque_repayment(repayment, facility=frappe.get_doc("Facility", repayment.facility))
		apply_pdc_debt_purchase_settled(pdc, repayment, je_name)


def cancel_debt_purchase_settlement(repayment, *, settlement_je: str | None = None) -> None:
	"""Cancel-order step 2+: assumes JE already cancelled successfully.

	``settlement_je`` is the JE name before ``repayment.journal_entry`` was cleared.
	"""
	pdc_name = (repayment.post_dated_cheque or "").strip()
	if not pdc_name:
		return
	lock_name = f"pdc_debt_purchase_settle_{pdc_name}"
	with filelock(lock_name, timeout=120):
		pdc = frappe.get_doc("Post Dated Cheque", pdc_name)
		restore_pdc_after_debt_purchase_cancel(pdc, repayment, settlement_je=settlement_je)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def debt_purchase_cheque_query(doctype, txt, searchfield, start, page_len, filters):
	"""Eligible Assigned DP cheques for Facility Repayment link field.

	When ``facility`` is provided, only PDCs allocated on that Facility's
	``debt_purchase_pdcs`` table are returned.
	"""
	filters = filters or {}
	company = filters.get("company")
	facility = (filters.get("facility") or "").strip()
	# JOIN placeholders appear before WHERE placeholders in the SQL string.
	join_sql = ""
	join_params: list[Any] = []
	if facility:
		join_sql = """
			INNER JOIN `tabFacility PDC Allocation` alloc
				ON alloc.post_dated_cheque = `tabPost Dated Cheque`.name
				AND alloc.parenttype = 'Facility'
				AND alloc.parent = %s
		"""
		join_params.append(facility)
	conds = [
		"`tabPost Dated Cheque`.cheque_direction = %s",
		"`tabPost Dated Cheque`.workflow_state = %s",
		"`tabPost Dated Cheque`.docstatus < 2",
		"(`tabPost Dated Cheque`.debt_purchase_repayment IS NULL OR `tabPost Dated Cheque`.debt_purchase_repayment = '')",
	]
	where_params: list[Any] = [CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE]
	if company:
		conds.append("`tabPost Dated Cheque`.company = %s")
		where_params.append(company)
	conds.append(f"`tabPost Dated Cheque`.{searchfield} LIKE %s")
	where_params.append(f"%{txt}%")
	params = join_params + where_params + [page_len, start]
	return frappe.db.sql(
		f"""
		SELECT `tabPost Dated Cheque`.name, `tabPost Dated Cheque`.cheque_no,
			`tabPost Dated Cheque`.cheque_amount, `tabPost Dated Cheque`.party
		FROM `tabPost Dated Cheque`
		{join_sql}
		WHERE {' AND '.join(conds)}
		ORDER BY `tabPost Dated Cheque`.modified DESC
		LIMIT %s OFFSET %s
		""",
		tuple(params),
	)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def facility_allocation_pdc_query(doctype, txt, searchfield, start, page_len, filters):
	"""PDCs selectable on Facility.debt_purchase_pdcs.

	- Assigned + not yet settled repayments (normal path).
	- Settled PDCs already linked to this Facility (historical documentation).
	"""
	filters = filters or {}
	company = filters.get("company")
	exclude_facility = (filters.get("facility") or "").strip()
	conds = [
		"`tabPost Dated Cheque`.cheque_direction = %s",
		"`tabPost Dated Cheque`.docstatus < 2",
		"""(
			(
				`tabPost Dated Cheque`.workflow_state = %s
				AND (`tabPost Dated Cheque`.debt_purchase_repayment IS NULL
					OR `tabPost Dated Cheque`.debt_purchase_repayment = '')
			)
			OR (
				`tabPost Dated Cheque`.workflow_state = %s
				AND %s != ''
				AND `tabPost Dated Cheque`.debt_purchase_facility = %s
			)
		)""",
		"""NOT EXISTS (
			SELECT 1 FROM `tabFacility PDC Allocation` alloc
			WHERE alloc.post_dated_cheque = `tabPost Dated Cheque`.name
			  AND alloc.parenttype = 'Facility'
			  AND IFNULL(alloc.parent, '') != %s
		)""",
	]
	params: list[Any] = [
		CHEQUE_DIRECTION_RECEIVABLE,
		WORKFLOW_ASSIGNED_DEBT_PURCHASE,
		WORKFLOW_DEBT_PURCHASE_SETTLED,
		exclude_facility or "",
		exclude_facility or "",
		exclude_facility or "",
	]
	if company:
		conds.append("`tabPost Dated Cheque`.company = %s")
		params.append(company)
	conds.append(f"`tabPost Dated Cheque`.{searchfield} LIKE %s")
	params.append(f"%{txt}%")
	return frappe.db.sql(
		f"""
		SELECT `tabPost Dated Cheque`.name, `tabPost Dated Cheque`.cheque_no,
			`tabPost Dated Cheque`.cheque_amount, `tabPost Dated Cheque`.party
		FROM `tabPost Dated Cheque`
		WHERE {' AND '.join(conds)}
		ORDER BY `tabPost Dated Cheque`.modified DESC
		LIMIT %s OFFSET %s
		""",
		tuple(params + [page_len, start]),
	)


__all__ = [
	"REPAYMENT_METHOD_BANK",
	"REPAYMENT_METHOD_DEBT_PURCHASE",
	"PURPOSE_DEBT_PURCHASE_SETTLEMENT",
	"normalize_repayment_method",
	"is_debt_purchase_cheque_method",
	"resolve_debt_purchase_in_collection_account",
	"validate_bank_account_method_fields",
	"validate_debt_purchase_cheque_repayment",
	"append_pdc_settlement_journal_reference",
	"remove_pdc_settlement_journal_reference",
	"settle_debt_purchase_on_submit",
	"cancel_debt_purchase_settlement",
	"debt_purchase_cheque_query",
	"facility_allocation_pdc_query",
]
