# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.exceptions import QueryTimeoutError
from frappe.model.document import Document
from frappe.utils import cint, getdate, today

from erpnext_extensions.petty_management.services.allocation_service import (
	get_pm_request_allocation_context as get_pm_request_allocation_context_service,
)
from erpnext_extensions.petty_management.services.allocation_service import (
	get_pm_request_paid_amount,
	pm_request_passes_clearance_filters,
	sum_prior_pm_request_allocations,
)
from erpnext_extensions.petty_management.services.allocation_service import (
	pm_request_query_for_pm_clearance as _pm_request_query_for_pm_clearance,
)
from erpnext_extensions.petty_management.services.clearance_lock_diagnostics import (
	log_pm_clearance_lock_diagnostics,
)
from erpnext_extensions.petty_management.services.clearance_naming import assign_pm_clearance_name
from erpnext_extensions.petty_management.services.clearance_service import (
	before_cancel_clearance,
	before_validate_clearance,
	clearance_is_approved,
	ensure_petty_cash_account_filled,
	normalize_settlement_types,
	on_cancel_clearance,
	on_submit_clearance,
	prepare_doc_for_je_preview,
	prune_empty_request_allocation_rows,
	sync_clearance_status_from_workflow,
	validate_and_stamp_pi_rows,
	validate_and_stamp_supplier_advance_rows,
	validate_clearance,
	validate_duplicate_settlement_targets,
	validate_request_allocations,
)
from erpnext_extensions.petty_management.services.constants import SETTLEMENT_PI, SETTLEMENT_SA
from erpnext_extensions.petty_management.services.holder_service import (
	clearance_petty_cash_account,
	get_holder_context,
	get_holder_petty_cash_account,
	sync_clearance_holder_fields,
)
from erpnext_extensions.petty_management.services.holder_service import (
	request_petty_cash_account as pm_request_petty_cash_from_holder,
)
from erpnext_extensions.petty_management.services.journal_entry_service import (
	build_clearance_je_accounts,
	create_clearance_journal_entry,
)
from erpnext_extensions.petty_management.services.journal_entry_service import (
	settle_petty_cash as settle_petty_cash_service,
)
from erpnext_extensions.petty_management.services.preview_service import (
	doc_for_preview,
)
from erpnext_extensions.petty_management.services.preview_service import (
	preview_pm_clearance_settlement as preview_pm_clearance_settlement_service,
)


class PMClearance(Document):
	"""Thin controller for PM Clearance settlement lifecycle."""

	def autoname(self):
		frappe.logger("pm_clearance").info(
			"PM Clearance autoname start employee=%s", (self.employee or "").strip()
		)
		assign_pm_clearance_name(self)
		frappe.logger("pm_clearance").info("PM Clearance autoname done name=%s", self.name)

	def insert(self, *args, **kwargs):
		if cint(self.docstatus) == 0:
			from erpnext_extensions.petty_management.services.clearance_service import (
				normalize_funding_allocation_rows,
			)

			normalize_funding_allocation_rows(self)
		try:
			return super().insert(*args, **kwargs)
		except QueryTimeoutError as exc:
			log_pm_clearance_lock_diagnostics(
				phase="insert",
				doc=self,
				last_sql=getattr(frappe.db, "last_query", None),
			)
			frappe.throw(
				_("PM Clearance could not be saved due to a database lock. Please refresh and try again."),
				title=_("Please try again"),
				exc=exc,
			)

	def db_insert(self, *args, **kwargs):
		frappe.logger("pm_clearance").info("PM Clearance parent db_insert name=%s", self.name)
		try:
			return super().db_insert(*args, **kwargs)
		except QueryTimeoutError:
			log_pm_clearance_lock_diagnostics(
				phase="db_insert_tabPM Clearance",
				doc=self,
				last_sql=getattr(frappe.db, "last_query", None),
			)
			raise

	def before_validate(self):
		before_validate_clearance(self)

	def validate(self):
		validate_clearance(self)

	def before_submit(self):
		# v4.7.2: Approve submits. Manager stamp set on Draft→Pending; do not
		# clear finance_approver (stamped after finance act).
		from erpnext_extensions.petty_management.services.approver_stamp_service import (
			ensure_pm_clearance_manager_stamp,
		)

		ensure_pm_clearance_manager_stamp(self)

	def on_submit(self):
		on_submit_clearance(self)

	def before_cancel(self):
		before_cancel_clearance(self)

	def on_cancel(self):
		on_cancel_clearance(self)

	def on_trash(self):
		from erpnext_extensions.petty_management.services.draft_approval_guards import (
			assert_pending_not_deletable,
		)

		assert_pending_not_deletable(self)

	def _ensure_petty_cash_account_filled(self):
		ensure_petty_cash_account_filled(self)

	def _normalize_settlement_types(self):
		normalize_settlement_types(self)

	def _sync_holder_and_pending(self):
		sync_clearance_holder_fields(self)

	def _validate_duplicate_settlement_targets(self):
		validate_duplicate_settlement_targets(self)

	def _validate_and_stamp_pi_rows(self):
		validate_and_stamp_pi_rows(self)

	def _validate_and_stamp_supplier_advance_rows(self):
		validate_and_stamp_supplier_advance_rows(self)

	def _calc_line_totals(self):
		from erpnext_extensions.petty_management.services.clearance_service import calc_line_totals

		calc_line_totals(self)

	def _calc_parent_totals(self):
		from erpnext_extensions.petty_management.services.clearance_service import calc_parent_totals

		calc_parent_totals(self)

	def _validate_request_allocations(self):
		validate_request_allocations(self)

	def _sync_clearance_status_from_workflow(self):
		sync_clearance_status_from_workflow(self)

	def _prune_empty_request_allocation_rows(self):
		prune_empty_request_allocation_rows(self)

	def _sync_funding_traceability_snapshot(self):
		sync_clearance_holder_fields(self)

	def _create_clearance_journal_entry(self):
		return create_clearance_journal_entry(self)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def pm_request_query_for_pm_clearance(doctype, txt, searchfield, start, page_len, filters):
	return _pm_request_query_for_pm_clearance(doctype, txt, searchfield, start, page_len, filters)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def pm_opening_advance_query_for_pm_clearance(doctype, txt, searchfield, start, page_len, filters):
	from erpnext_extensions.petty_management.services.opening_advance_service import (
		pm_opening_advance_query_for_pm_clearance as _fn,
	)

	return _fn(doctype, txt, searchfield, start, page_len, filters)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def purchase_invoice_query_for_pm_clearance(doctype, txt, searchfield, start, page_len, filters):
	from erpnext_extensions.petty_management.services.settlement_query import (
		purchase_invoice_query_for_pm_clearance as _fn,
	)

	return _fn(doctype, txt, searchfield, start, page_len, filters)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def purchase_order_query_for_pm_clearance(doctype, txt, searchfield, start, page_len, filters):
	from erpnext_extensions.petty_management.services.settlement_query import (
		purchase_order_query_for_pm_clearance as _fn,
	)

	return _fn(doctype, txt, searchfield, start, page_len, filters)


@frappe.whitelist()
def get_opening_advance_allocation_context(
	pm_opening_advance: str,
	pm_clearance: str | None = None,
	company: str | None = None,
	employee: str | None = None,
	holder: str | None = None,
	petty_cash_account: str | None = None,
) -> dict:
	from erpnext_extensions.petty_management.services.opening_advance_service import (
		get_opening_advance_allocation_context as _svc,
	)

	return _svc(
		pm_opening_advance=pm_opening_advance,
		pm_clearance=pm_clearance,
		company=company,
		employee=employee,
		holder=holder,
		petty_cash_account=petty_cash_account,
	)


@frappe.whitelist()
def get_pm_request_allocation_context(
	pm_request: str,
	pm_clearance: str | None = None,
	company: str | None = None,
	employee: str | None = None,
	holder: str | None = None,
	petty_cash_account: str | None = None,
) -> dict:
	"""Controller wrapper for PM Clearance grid calls.

	The service function is also whitelisted so direct service calls remain
	backward compatible, but PM Clearance JS should use this controller path.
	"""
	return get_pm_request_allocation_context_service(
		pm_request=pm_request,
		pm_clearance=pm_clearance,
		company=company,
		employee=employee,
		holder=holder,
		petty_cash_account=petty_cash_account,
	)


def _petty_cash_account_for_holder(holder_name: str | None) -> str:
	return get_holder_petty_cash_account(holder_name)


def _clearance_is_approved(doc: Document) -> bool:
	return clearance_is_approved(doc)


def _doc_for_preview(doc=None, pm_clearance: str | None = None) -> Document:
	return doc_for_preview(doc=doc, pm_clearance=pm_clearance)


def _prepare_doc_for_je_preview(dobj: Document) -> None:
	prepare_doc_for_je_preview(dobj)


@frappe.whitelist()
def get_pm_clearance_holder_context(
	employee: str | None = None, company: str | None = None, posting_date=None
) -> dict:
	return get_holder_context(employee, company, posting_date=posting_date)


@frappe.whitelist()
def preview_pm_clearance_settlement(doc=None, pm_clearance: str | None = None) -> dict:
	return preview_pm_clearance_settlement_service(doc=doc, pm_clearance=pm_clearance)


@frappe.whitelist()
def approve_pm_clearance_for_settlement(pm_clearance: str) -> dict:
	"""Apply Clearance Finance Approve via legitimate workflow (no raw docstatus write).

	Document must already be Pending Finance Review. PI readiness + role checks run
	through ``apply_pm_workflow`` / action policy as for Desk Finance Approve.
	"""
	from erpnext_extensions.petty_management.services.clearance_service import (
		approve_pm_clearance_for_reservation,
	)

	if not frappe.has_permission("PM Clearance", "write", doc=pm_clearance):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	approve_pm_clearance_for_reservation(pm_clearance)
	return get_pm_clearance_action_flags(pm_clearance)


@frappe.whitelist()
def settle_petty_cash(pm_clearance: str) -> dict[str, str]:
	return settle_petty_cash_service(pm_clearance)


@frappe.whitelist()
def get_pm_clearance_action_flags(pm_clearance: str) -> dict:
	from erpnext_extensions.petty_management.services.clearance_action_policy import (
		get_pm_clearance_action_flags as _flags,
	)

	return _flags(pm_clearance)


@frappe.whitelist()
def get_pm_clearance_pi_readiness(pm_clearance: str) -> dict:
	"""Desk banner / Finance UX: Draft vs submitted PI readiness (v4.1.5)."""
	from erpnext_extensions.petty_management.services.purchase_invoice_readiness import (
		get_purchase_invoice_readiness,
	)

	doc = frappe.get_doc("PM Clearance", pm_clearance)
	if not frappe.has_permission("PM Clearance", "read", doc=doc):
		frappe.throw(_("Not permitted"), frappe.PermissionError)
	return get_purchase_invoice_readiness(doc)
