# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, getdate

from erpnext_extensions.iran_accounting.account_explorer.account_scope import resolve_account_scope
from erpnext_extensions.iran_accounting.account_explorer.constants import (
	CURRENCY_SORTABLE_FIELDS,
	DETAIL_MODES,
	DIMENSION_SORTABLE_FIELDS,
	GL_GROUP_SORTABLE_FIELDS,
	INVENTORY_ACCOUNT_SORTABLE_FIELDS,
	ITEM_GROUP_SORTABLE_FIELDS,
	ITEM_SORTABLE_FIELDS,
	PARTY_SORTABLE_FIELDS,
	SORTABLE_FIELDS,
	UNIFIED_PARTY_SORTABLE_FIELDS,
	VOUCHER_SORTABLE_FIELDS,
	VIEW_AXES,
)
from erpnext_extensions.iran_accounting.account_explorer.dimension_discovery import validate_dimension_field
from erpnext_extensions.iran_accounting.account_explorer.permissions import (
	assert_accounts_role,
	assert_company_allowed,
	assert_currency_analysis_enabled,
	assert_dimension_analysis_enabled,
	assert_feature_enabled,
	assert_inventory_analysis_enabled,
	assert_party_analysis_enabled,
	assert_unified_party_enabled,
	assert_voucher_analysis_enabled,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import (
	AccountExplorerQuerySpec,
	AccountScope,
	AccountingFilter,
	AnalysisContext,
	CurrencyFilter,
	DimensionScope,
	DocumentScope,
	InventoryFilter,
	ItemGroupScope,
	ItemScope,
	PaginationState,
	PartyScope,
	StatusFilter,
	UnifiedPartyScope,
	VoucherFilter,
	VoucherScope,
)


class AccountExplorerValidationError(frappe.ValidationError):
	pass


def _parse_json(value: Any) -> dict:
	if not value:
		return {}
	if isinstance(value, dict):
		return value
	if isinstance(value, str):
		return json.loads(value) if value.strip() else {}
	return {}


def _resolve_fiscal_year(
	company: str, fiscal_year: str | None, from_date, to_date
) -> tuple[str | None, Any, Any]:
	if fiscal_year:
		fy = frappe.get_cached_value(
			"Fiscal Year",
			fiscal_year,
			["year_start_date", "year_end_date"],
			as_dict=True,
		)
		if not fy:
			frappe.throw(_("Fiscal Year {0} does not exist").format(fiscal_year))
		start = getdate(fy.year_start_date)
		end = getdate(fy.year_end_date)
		return fiscal_year, from_date or start, to_date or end

	if from_date and to_date:
		return fiscal_year, getdate(from_date), getdate(to_date)

	current = frappe.db.sql(
		"""
		select fy.name, fy.year_start_date, fy.year_end_date
		from `tabFiscal Year` fy
		inner join `tabFiscal Year Company` fyc on fyc.parent = fy.name
		where fyc.company = %s and fy.disabled = 0
		  and %s between fy.year_start_date and fy.year_end_date
		order by fy.year_start_date desc
		limit 1
		""",
		(company, getdate()),
		as_dict=True,
	)
	if current:
		row = current[0]
		return row.name, getdate(row.year_start_date), getdate(row.year_end_date)
	return fiscal_year, from_date, to_date


def _load_settings_defaults() -> dict:
	settings = frappe.get_single("Iran Accounting Settings")
	return {
		"include_cancelled_entries": cint(settings.default_include_cancelled),
		"hide_zero_rows": cint(settings.default_hide_zero_rows),
		"page_size": cint(settings.default_page_size) or 50,
		"include_opening_entries": cint(settings.default_include_opening_entries),
		"include_period_closing_vouchers": cint(settings.default_include_period_closing_vouchers),
	}


def _coerce_filter_value(value):
	if value is None:
		return None
	if isinstance(value, list):
		return [item for item in value if item not in (None, "")]
	return value


def build_voucher_filter(raw: dict | None) -> VoucherFilter:
	raw = raw or {}
	return VoucherFilter(
		voucher_type=raw.get("voucher_type") or None,
		voucher_no=raw.get("voucher_no") or None,
		against_voucher_type=raw.get("against_voucher_type") or None,
		against_voucher_no=raw.get("against_voucher_no") or None,
		reference_no=raw.get("reference_no") or None,
	)


def build_accounting_filter(raw: dict | None) -> AccountingFilter:
	raw = raw or {}
	return AccountingFilter(
		account=_coerce_filter_value(raw.get("account")),
		party_type=raw.get("party_type") or None,
		party=_coerce_filter_value(raw.get("party")),
	)


def build_currency_filter(raw: dict | None) -> CurrencyFilter:
	raw = raw or {}
	currency_type = raw.get("currency_type") or "account_currency"
	if currency_type not in {"account_currency", "transaction_currency"}:
		raise AccountExplorerValidationError(_("Invalid currency type."))
	return CurrencyFilter(
		currency_type=currency_type,
		currency=raw.get("currency") or None,
	)


def build_status_filter(raw: dict | None, defaults: dict) -> StatusFilter:
	raw = raw or {}
	return StatusFilter(
		include_opening_entries=cint(raw.get("include_opening_entries", defaults["include_opening_entries"])),
		include_cancelled_entries=cint(raw.get("include_cancelled_entries", defaults["include_cancelled_entries"])),
		include_default_finance_book_entries=cint(
			raw.get(
				"include_default_finance_book_entries",
				raw.get("include_default_book_entries", 1),
			)
		),
		include_period_closing_vouchers=cint(
			raw.get("include_period_closing_vouchers", defaults["include_period_closing_vouchers"])
		),
	)


def build_inventory_filter(raw: dict | None) -> InventoryFilter:
	raw = raw or {}
	return InventoryFilter(
		item_group=_coerce_filter_value(raw.get("item_group")),
		item=_coerce_filter_value(raw.get("item")),
		warehouse=_coerce_filter_value(raw.get("warehouse")),
		inventory_account=_coerce_filter_value(raw.get("inventory_account")),
	)


def build_document_scope(raw: dict, defaults: dict) -> DocumentScope:
	if not raw:
		raise AccountExplorerValidationError(_("document_scope is required."))

	company = raw.get("company")
	if not company:
		raise AccountExplorerValidationError(_("Company is required."))

	fiscal_year = raw.get("fiscal_year")
	from_date = raw.get("from_date")
	to_date = raw.get("to_date")
	fiscal_year, from_date, to_date = _resolve_fiscal_year(company, fiscal_year, from_date, to_date)

	status_raw = raw.get("status") or {}
	if not status_raw and any(
		key in raw
		for key in (
			"include_opening_entries",
			"include_cancelled_entries",
			"include_default_book_entries",
			"include_default_finance_book_entries",
			"include_period_closing_vouchers",
		)
	):
		status_raw = {
			"include_opening_entries": raw.get("include_opening_entries"),
			"include_cancelled_entries": raw.get("include_cancelled_entries"),
			"include_default_finance_book_entries": raw.get(
				"include_default_finance_book_entries", raw.get("include_default_book_entries")
			),
			"include_period_closing_vouchers": raw.get("include_period_closing_vouchers"),
		}

	return DocumentScope(
		company=company,
		fiscal_year=fiscal_year,
		from_date=getdate(from_date) if from_date else None,
		to_date=getdate(to_date) if to_date else None,
		finance_book=raw.get("finance_book"),
		voucher=build_voucher_filter(raw.get("voucher")),
		accounting=build_accounting_filter(raw.get("accounting")),
		accounting_dimensions=dict(raw.get("accounting_dimensions") or {}),
		currency=build_currency_filter(raw.get("currency")),
		status=build_status_filter(status_raw, defaults),
		inventory=build_inventory_filter(raw.get("inventory")),
		hide_zero_rows=cint(raw.get("hide_zero_rows", defaults["hide_zero_rows"])),
	)


def build_account_scope(raw: dict) -> AccountScope:
	scope_raw = raw.get("account_scope") or {}
	return AccountScope(
		mode=scope_raw.get("mode") or "tree",
		selected_account=scope_raw.get("selected_account"),
		virtual_row_key=scope_raw.get("virtual_row_key"),
		is_virtual_group=cint(scope_raw.get("is_virtual_group")),
		level_sequence=cint(scope_raw.get("level_sequence")) or None,
		tree_root_account=scope_raw.get("tree_root_account") or scope_raw.get("selected_account"),
	)


def build_party_scope(raw: dict) -> PartyScope:
	scope_raw = raw.get("party_scope") or {}
	return PartyScope(
		party_type=scope_raw.get("party_type") or None,
		selected_party=scope_raw.get("selected_party") or None,
	)


def build_dimension_scope(raw: dict) -> DimensionScope:
	scope_raw = raw.get("dimension_scope") or {}
	dimension_type = scope_raw.get("dimension_type") or scope_raw.get("dimension_field") or None
	selected_dimension_value = scope_raw.get("selected_dimension_value")
	if selected_dimension_value is None and "selected_value" in scope_raw:
		selected_dimension_value = scope_raw.get("selected_value")
	return DimensionScope(
		dimension_type=dimension_type,
		selected_dimension_value=selected_dimension_value,
	)


def build_voucher_scope(raw: dict) -> VoucherScope:
	scope_raw = raw.get("voucher_scope") or {}
	return VoucherScope(
		voucher_type=scope_raw.get("voucher_type") or None,
		voucher_no=scope_raw.get("voucher_no") or None,
	)


def build_unified_party_scope(raw: dict) -> UnifiedPartyScope:
	scope_raw = raw.get("unified_party_scope") or {}
	return UnifiedPartyScope(
		selected_unified_party=scope_raw.get("selected_unified_party") or None,
		include_unmapped=cint(scope_raw.get("include_unmapped", 0)),
	)


def build_item_group_scope(raw: dict) -> ItemGroupScope:
	scope_raw = raw.get("item_group_scope") or {}
	return ItemGroupScope(
		selected_item_group=scope_raw.get("selected_item_group") or None,
	)


def build_item_scope(raw: dict) -> ItemScope:
	scope_raw = raw.get("item_scope") or {}
	return ItemScope(
		selected_item=scope_raw.get("selected_item") or None,
	)


def build_analysis_context(raw: dict, defaults: dict) -> AnalysisContext:
	view_axis = raw.get("view_axis") or "account_level"
	detail_mode = (raw.get("detail_mode") or "summary").lower()
	page = max(cint(raw.get("page") or 1), 1)
	page_size = cint(raw.get("page_size") or defaults["page_size"]) or 50
	page_size = min(
		page_size, cint(frappe.get_single_value("Iran Accounting Settings", "server_page_size")) or 200
	)
	level_sequence = raw.get("level_sequence")
	if level_sequence is not None:
		level_sequence = cint(level_sequence) or None

	return AnalysisContext(
		view_axis=view_axis,
		detail_mode=detail_mode,
		level_sequence=level_sequence,
		account_scope=build_account_scope(raw),
		party_scope=build_party_scope(raw),
		unified_party_scope=build_unified_party_scope(raw),
		dimension_scope=build_dimension_scope(raw),
		voucher_scope=build_voucher_scope(raw),
		item_group_scope=build_item_group_scope(raw),
		item_scope=build_item_scope(raw),
		pagination=PaginationState(
			page=page,
			page_size=page_size,
			sort_field=(raw.get("sort_field") or _default_sort_field(view_axis, detail_mode)),
			sort_order=(raw.get("sort_order") or "asc").lower(),
		),
	)


def _default_sort_field(view_axis: str, detail_mode: str = "summary") -> str:
	if detail_mode == "grouped_gl":
		return "account"
	if view_axis == "voucher":
		return "posting_date"
	if view_axis == "party":
		return "party_type"
	if view_axis == "unified_party":
		return "display_title"
	if view_axis == "currency":
		return "currency"
	if view_axis in {"item", "item_group", "inventory_account"}:
		return "display_code"
	return "display_code"


def _sortable_fields_for_axis(view_axis: str, detail_mode: str = "summary"):
	if detail_mode == "grouped_gl":
		return GL_GROUP_SORTABLE_FIELDS
	if view_axis == "party":
		return PARTY_SORTABLE_FIELDS
	if view_axis == "unified_party":
		return UNIFIED_PARTY_SORTABLE_FIELDS
	if view_axis == "dimension":
		return DIMENSION_SORTABLE_FIELDS
	if view_axis == "currency":
		return CURRENCY_SORTABLE_FIELDS
	if view_axis == "voucher":
		return VOUCHER_SORTABLE_FIELDS
	if view_axis == "item_group":
		return ITEM_GROUP_SORTABLE_FIELDS
	if view_axis == "item":
		return ITEM_SORTABLE_FIELDS
	if view_axis == "inventory_account":
		return INVENTORY_ACCOUNT_SORTABLE_FIELDS
	return SORTABLE_FIELDS


def AccountExplorerQuerySpec_from_client(
	payload: Any, *, require_dates: bool = True
) -> AccountExplorerQuerySpec:
	assert_accounts_role()
	data = _parse_json(payload)
	defaults = _load_settings_defaults()

	document_raw = data.get("document_scope")
	if document_raw is None and data.get("company"):
		document_raw = data
	if document_raw is None:
		raise AccountExplorerValidationError(_("document_scope is required."))

	document_scope = build_document_scope(document_raw, defaults)
	assert_company_allowed(document_scope.company)
	assert_feature_enabled()

	if require_dates and (not document_scope.from_date or not document_scope.to_date):
		raise AccountExplorerValidationError(
			_("From Date and To Date are required before running Account Explorer queries.")
		)

	if document_scope.from_date and document_scope.to_date and document_scope.from_date > document_scope.to_date:
		raise AccountExplorerValidationError(_("From Date cannot be greater than To Date"))

	analysis = build_analysis_context(data.get("analysis_context") or data, defaults)
	view_axis = analysis.view_axis
	detail_mode = analysis.detail_mode

	if view_axis not in VIEW_AXES:
		raise AccountExplorerValidationError(_("Invalid analysis axis."))
	if detail_mode not in DETAIL_MODES:
		raise AccountExplorerValidationError(_("Invalid detail mode."))
	if view_axis == "party":
		assert_party_analysis_enabled()
	if view_axis == "unified_party":
		assert_party_analysis_enabled()
		assert_unified_party_enabled()
	if view_axis == "dimension":
		assert_dimension_analysis_enabled()
		if not analysis.dimension_scope.dimension_type:
			raise AccountExplorerValidationError(_("Dimension type is required for dimension analysis."))
		validate_dimension_field(analysis.dimension_scope.dimension_type)
	if view_axis == "currency":
		assert_currency_analysis_enabled()
	if view_axis in {"item_group", "item", "inventory_account"}:
		assert_inventory_analysis_enabled()
	if view_axis == "voucher" or detail_mode == "grouped_gl":
		assert_voucher_analysis_enabled()
	if detail_mode == "grouped_gl":
		if not analysis.voucher_scope.voucher_type or not analysis.voucher_scope.voucher_no:
			raise AccountExplorerValidationError(
				_("Voucher type and voucher number are required for grouped GL detail.")
			)

	if analysis.pagination.sort_field not in _sortable_fields_for_axis(view_axis, detail_mode):
		raise AccountExplorerValidationError(_("Invalid sort field."))

	spec = AccountExplorerQuerySpec(
		document_scope=document_scope,
		analysis=analysis,
		presentation_currency=document_raw.get("presentation_currency") or "company",
	)

	spec.included_account_names = resolve_account_scope(spec)
	from erpnext_extensions.iran_accounting.account_explorer.inventory_account_scope import (
		apply_inventory_related_account_scope,
	)

	apply_inventory_related_account_scope(spec)
	if spec.unified_party_scope.selected_unified_party:
		from erpnext_extensions.iran_accounting.account_explorer.unified_party_registry import (
			get_member_tuples,
			resolve_uap_for_company,
		)

		uap = resolve_uap_for_company(spec.unified_party_scope.selected_unified_party, spec.company)
		if not uap:
			raise AccountExplorerValidationError(
				_("Unified Accounting Party {0} is not available for company {1}.").format(
					spec.unified_party_scope.selected_unified_party, spec.company
				)
			)
		spec.resolved_member_tuples = get_member_tuples(spec.unified_party_scope.selected_unified_party)
	return spec
