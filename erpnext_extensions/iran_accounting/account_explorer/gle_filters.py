# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import frappe
from frappe.utils import cstr

from erpnext_extensions.iran_accounting.account_explorer.dimension_discovery import validate_dimension_field
from erpnext_extensions.iran_accounting.account_explorer.party_sources import get_enabled_party_types
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec, DocumentScope
from erpnext_extensions.iran_accounting.account_explorer.unified_party_registry import get_unified_party_types


def normalize_filter_values(value) -> list[str]:
	if value is None or value == "":
		return []
	if isinstance(value, (list, tuple, set)):
		return [str(item) for item in value if item not in (None, "")]
	return [str(value)]


def apply_member_tuple_filter(query, gle, member_tuples: list[tuple[str, str]]):
	if not member_tuples:
		return query.where(gle.name == "")
	condition = None
	for party_type, party in member_tuples:
		part = (gle.party_type == party_type) & (gle.party == party)
		condition = part if condition is None else condition | part
	return query.where(condition)


def apply_document_scope_filters(query, gle, spec: AccountExplorerQuerySpec):
	document_scope = spec.document_scope
	query = query.where(gle.company == document_scope.company)

	if not document_scope.status.include_cancelled_entries:
		query = query.where(gle.is_cancelled == 0)

	if not document_scope.status.include_period_closing_vouchers:
		query = query.where(gle.voucher_type != "Period Closing Voucher")

	query = _apply_finance_book_filters(query, gle, document_scope)
	query = _apply_document_voucher_filters(query, gle, document_scope)
	query = _apply_document_accounting_filters(query, gle, document_scope)
	query = _apply_document_dimension_filters(query, gle, document_scope)
	query = _apply_document_currency_filters(query, gle, document_scope)
	return query


def _apply_document_voucher_filters(query, gle, document_scope: DocumentScope):
	voucher = document_scope.voucher
	if voucher.voucher_type:
		query = query.where(gle.voucher_type == voucher.voucher_type)
	if voucher.voucher_no:
		query = query.where(gle.voucher_no == voucher.voucher_no)
	if voucher.against_voucher_type:
		query = query.where(gle.against_voucher_type == voucher.against_voucher_type)
	if voucher.against_voucher_no:
		query = query.where(gle.against_voucher == voucher.against_voucher_no)
	if voucher.reference_no:
		gl_meta = frappe.get_meta("GL Entry")
		if gl_meta.has_field("bill_no"):
			query = query.where(gle.bill_no == voucher.reference_no)
	return query


def _apply_document_accounting_filters(query, gle, document_scope: DocumentScope):
	accounting = document_scope.accounting
	accounts = normalize_filter_values(accounting.account)
	if accounts:
		query = query.where(gle.account.isin(accounts))
	if accounting.party_type:
		query = query.where(gle.party_type == accounting.party_type)
	parties = normalize_filter_values(accounting.party)
	if parties:
		query = query.where(gle.party.isin(parties))
	return query


def _apply_document_dimension_filters(query, gle, document_scope: DocumentScope):
	for fieldname, value in (document_scope.accounting_dimensions or {}).items():
		if value is None or value == "":
			continue
		validate_dimension_field(fieldname)
		dim = gle[fieldname]
		values = normalize_filter_values(value)
		if not values:
			continue
		if len(values) == 1:
			query = query.where(dim == values[0])
		else:
			query = query.where(dim.isin(values))
	return query


def _apply_document_currency_filters(query, gle, document_scope: DocumentScope):
	currency = document_scope.currency
	if not currency.currency:
		return query
	if currency.currency_type == "transaction_currency":
		query = query.where(
			(gle.transaction_currency == currency.currency)
			| ((gle.transaction_currency.isnull() | (gle.transaction_currency == "")) & (gle.account_currency == currency.currency))
		)
	else:
		query = query.where(gle.account_currency == currency.currency)
	return query


def apply_analysis_scope_filters(
	query,
	gle,
	spec: AccountExplorerQuerySpec,
	*,
	party_types: list[str] | None = None,
	apply_default_party_types: bool = True,
):
	accounts = spec.included_account_names or []
	if accounts:
		query = query.where(gle.account.isin(accounts))
	else:
		from erpnext_extensions.iran_accounting.account_explorer.inventory_scope import (
			has_inventory_document_filters,
		)

		if not has_inventory_document_filters(spec):
			query = query.where(gle.name == "")

	if spec.resolved_member_tuples:
		query = apply_member_tuple_filter(query, gle, spec.resolved_member_tuples)
	else:
		effective_party_types = party_types
		if effective_party_types is None and apply_default_party_types:
			if spec.view_axis == "unified_party":
				effective_party_types = get_unified_party_types()
			elif spec.view_axis == "party":
				effective_party_types = get_enabled_party_types()
		if effective_party_types:
			query = query.where(gle.party_type.isin(effective_party_types))

		if spec.view_axis != "unified_party":
			if spec.party_scope.party_type:
				query = query.where(gle.party_type == spec.party_scope.party_type)
			if spec.party_scope.selected_party:
				query = query.where(gle.party == spec.party_scope.selected_party)

	dimension_type = spec.dimension_scope.dimension_type
	if dimension_type and spec.dimension_scope.selected_dimension_value is not None:
		validate_dimension_field(dimension_type)
		dim = gle[dimension_type]
		if spec.dimension_scope.selected_dimension_value == "":
			query = query.where((dim == "") | (dim.isnull()))
		else:
			query = query.where(dim == spec.dimension_scope.selected_dimension_value)

	if spec.voucher_scope.voucher_type:
		query = query.where(gle.voucher_type == spec.voucher_scope.voucher_type)
	if spec.voucher_scope.voucher_no:
		query = query.where(gle.voucher_no == spec.voucher_scope.voucher_no)

	return query


def apply_scoped_gle_filters(
	query,
	gle,
	spec: AccountExplorerQuerySpec,
	*,
	party_types: list[str] | None = None,
	apply_default_party_types: bool = True,
):
	query = apply_document_scope_filters(query, gle, spec)
	query = apply_analysis_scope_filters(
		query,
		gle,
		spec,
		party_types=party_types,
		apply_default_party_types=apply_default_party_types,
	)
	from erpnext_extensions.iran_accounting.account_explorer.inventory_scope import (
		apply_inventory_voucher_scope_to_gle,
	)

	query = apply_inventory_voucher_scope_to_gle(query, gle, spec)
	return query


def _has_nonempty_filter_value(value) -> bool:
	if value is None or value == "":
		return False
	if isinstance(value, (list, tuple, set)):
		return any(_has_nonempty_filter_value(item) for item in value)
	return True


def spec_has_advanced_gle_filters(spec: AccountExplorerQuerySpec) -> bool:
	"""True when QuerySpec carries WHERE predicates Trial Balance cannot express.

	Account Levels keep the ERPNext Trial Balance path only for ordinary framing
	(company / dates / fiscal year / finance book / default-book / PCV flags /
	hide-zero). Any analytical or document advanced filter must use
	``apply_scoped_gle_filters`` so Analysis Filter chips are never accounting
	no-ops.

	Triggers (non-exhaustive but intentional):
	- document accounting account / party / party_type
	- document accounting_dimensions (cost center, project, custom dims)
	- document currency
	- document voucher (type, no, against, reference)
	- analysis party_scope / voucher_scope / dimension_scope value
	- unified party selection or resolved member tuples
	- include_cancelled_entries (TB always excludes cancelled)
	"""
	document = spec.document_scope
	accounting = document.accounting
	if _has_nonempty_filter_value(accounting.account) or _has_nonempty_filter_value(accounting.party):
		return True
	if accounting.party_type:
		return True
	for value in (document.accounting_dimensions or {}).values():
		if _has_nonempty_filter_value(value):
			return True
	currency = document.currency
	if currency and _has_nonempty_filter_value(currency.currency):
		return True
	voucher = document.voucher
	if voucher and (
		_has_nonempty_filter_value(voucher.voucher_type)
		or _has_nonempty_filter_value(voucher.voucher_no)
		or _has_nonempty_filter_value(voucher.against_voucher_type)
		or _has_nonempty_filter_value(voucher.against_voucher_no)
		or _has_nonempty_filter_value(voucher.reference_no)
	):
		return True
	# Trial Balance hard-excludes cancelled; including them requires scoped GL.
	if document.status.include_cancelled_entries:
		return True
	if spec.party_scope.party_type or _has_nonempty_filter_value(spec.party_scope.selected_party):
		return True
	if _has_nonempty_filter_value(spec.unified_party_scope.selected_unified_party):
		return True
	if spec.resolved_member_tuples:
		return True
	if spec.dimension_scope.selected_dimension_value is not None:
		return True
	if _has_nonempty_filter_value(spec.voucher_scope.voucher_type) or _has_nonempty_filter_value(
		spec.voucher_scope.voucher_no
	):
		return True
	# Item / Item Group / Warehouse → Case A Account uses sle_scoped_stock
	# (short-circuited in get_account_wise_measures). Other GL axes still need
	# E3 scoped posted GL with SLE→voucher EXISTS.
	from erpnext_extensions.iran_accounting.account_explorer.inventory_scope import (
		has_inventory_document_filters,
	)

	if has_inventory_document_filters(spec):
		return True
	# Narrowed account_scope selected_account alone stays on TB (post-subset).
	# Compatibility fields projected into the shapes above must keep this list current.
	return False


def get_gl_entry_match_conditions() -> str:
	from frappe.desk.reportview import build_match_conditions

	conditions = build_match_conditions("GL Entry")
	return f" and ({conditions})" if conditions else ""


def apply_opening_period_filters(query, gle, spec: AccountExplorerQuerySpec):
	from erpnext_extensions.iran_accounting.account_explorer.opening_entry_policy import (
		apply_policy_opening_filters,
	)

	return apply_policy_opening_filters(query, gle, spec)


def apply_period_turnover_filters(query, gle, spec: AccountExplorerQuerySpec):
	from erpnext_extensions.iran_accounting.account_explorer.opening_entry_policy import (
		apply_policy_turnover_filters,
	)

	return apply_policy_turnover_filters(query, gle, spec)


def apply_opening_entry_filters(query, gle, spec: AccountExplorerQuerySpec):
	"""Turnover-bucket is_opening gate (date range applied separately by callers)."""
	from erpnext_extensions.iran_accounting.account_explorer.opening_entry_policy import (
		OpeningEntryPolicyMode,
		policy_from_spec,
	)

	if policy_from_spec(spec) == OpeningEntryPolicyMode.EXCLUDE_OPENING_FLAGGED:
		query = query.where(gle.is_opening == "No")
	return query


def opening_entries_excluded_warning() -> str | None:
	return frappe._("Opening entries are excluded from this voucher view.")


def collect_scope_warnings(spec: AccountExplorerQuerySpec) -> list[str]:
	warnings: list[str] = []
	if not spec.include_opening_entries:
		warning = opening_entries_excluded_warning()
		if warning:
			warnings.append(warning)
	return warnings


def get_currency_amount_fields(currency_type: str = "account_currency") -> tuple[str, str]:
	if currency_type == "transaction_currency":
		return "debit_in_transaction_currency", "credit_in_transaction_currency"
	return "debit_in_account_currency", "credit_in_account_currency"


def get_currency_group_field(currency_type: str = "account_currency") -> str:
	return "transaction_currency" if currency_type == "transaction_currency" else "account_currency"


def _apply_finance_book_filters(query, gle, document_scope: DocumentScope):
	company_fb = frappe.get_cached_value("Company", document_scope.company, "default_finance_book")

	if document_scope.status.include_default_finance_book_entries:
		if document_scope.finance_book:
			if company_fb and cstr(document_scope.finance_book) != cstr(company_fb):
				allowed = [document_scope.finance_book, company_fb, ""]
			else:
				allowed = [document_scope.finance_book, ""]
			query = query.where((gle.finance_book.isin(allowed)) | gle.finance_book.isnull())
		else:
			allowed = [company_fb, ""] if company_fb else [""]
			query = query.where((gle.finance_book.isin(allowed)) | gle.finance_book.isnull())
	else:
		if document_scope.finance_book:
			query = query.where((gle.finance_book.isin([document_scope.finance_book, ""])) | gle.finance_book.isnull())
		else:
			query = query.where((gle.finance_book == "") | gle.finance_book.isnull())
	return query
