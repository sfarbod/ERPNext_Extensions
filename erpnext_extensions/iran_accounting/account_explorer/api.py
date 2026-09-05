# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import frappe

from erpnext_extensions.iran_accounting.account_explorer.constants import GL_DIMENSION_EXPAND_THRESHOLD
from erpnext_extensions.iran_accounting.account_explorer.currency_discovery import discover_company_currencies
from erpnext_extensions.iran_accounting.account_explorer.dimension_discovery import (
	get_default_dimension_field,
	get_default_dimension_type,
	get_discovered_dimensions,
)
from erpnext_extensions.iran_accounting.account_explorer.filter_axis_matrix import FILTER_AXIS_COMPATIBILITY
from erpnext_extensions.iran_accounting.account_explorer.party_sources import (
	get_enabled_party_sources,
	get_identifier_warnings,
)
from erpnext_extensions.iran_accounting.account_explorer.query_builder import (
	build_account_level_summary,
	get_default_level_sequence,
	get_enabled_levels,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec

SUMMARY_COLUMNS = [
	{"id": "display_code", "label": "Code", "fieldtype": "Data", "width": 120},
	{"id": "display_title", "label": "Title", "fieldtype": "Data", "width": 240},
	{"id": "period_debit", "label": "Debit Turnover", "fieldtype": "Currency", "width": 180},
	{"id": "period_credit", "label": "Credit Turnover", "fieldtype": "Currency", "width": 180},
	{"id": "debit_balance", "label": "Debit Balance", "fieldtype": "Currency", "width": 180},
	{"id": "credit_balance", "label": "Credit Balance", "fieldtype": "Currency", "width": 180},
	{"id": "opening_debit", "label": "Opening Debit", "fieldtype": "Currency", "width": 180},
	{"id": "opening_credit", "label": "Opening Credit", "fieldtype": "Currency", "width": 180},
]

PARTY_COLUMNS = [
	{"id": "party_type", "label": "Party Type", "fieldtype": "Data", "width": 120},
	{"id": "display_code", "label": "Party", "fieldtype": "Data", "width": 140},
	{"id": "display_title", "label": "Party Name", "fieldtype": "Data", "width": 220},
	{"id": "party_identifier", "label": "Identifier", "fieldtype": "Data", "width": 140},
	{"id": "period_debit", "label": "Debit Turnover", "fieldtype": "Currency", "width": 180},
	{"id": "period_credit", "label": "Credit Turnover", "fieldtype": "Currency", "width": 180},
	{"id": "debit_balance", "label": "Debit Balance", "fieldtype": "Currency", "width": 180},
	{"id": "credit_balance", "label": "Credit Balance", "fieldtype": "Currency", "width": 180},
]

DIMENSION_COLUMNS = [
	{"id": "display_code", "label": "Code", "fieldtype": "Data", "width": 140},
	{"id": "display_title", "label": "Title", "fieldtype": "Data", "width": 240},
	{"id": "period_debit", "label": "Debit Turnover", "fieldtype": "Currency", "width": 180},
	{"id": "period_credit", "label": "Credit Turnover", "fieldtype": "Currency", "width": 180},
	{"id": "debit_balance", "label": "Debit Balance", "fieldtype": "Currency", "width": 180},
	{"id": "credit_balance", "label": "Credit Balance", "fieldtype": "Currency", "width": 180},
]

UNIFIED_PARTY_COLUMNS = [
	{"id": "display_code", "label": "Code", "fieldtype": "Data", "width": 120},
	{"id": "display_title", "label": "Unified Name", "fieldtype": "Data", "width": 220},
	{"id": "member_count", "label": "Members", "fieldtype": "Int", "width": 90},
	{"id": "primary_member_label", "label": "Primary Member", "fieldtype": "Data", "width": 200},
	{"id": "identifier_summary", "label": "Identifier", "fieldtype": "Data", "width": 140},
	{"id": "period_debit", "label": "Debit Turnover", "fieldtype": "Currency", "width": 180},
	{"id": "period_credit", "label": "Credit Turnover", "fieldtype": "Currency", "width": 180},
	{"id": "debit_balance", "label": "Debit Balance", "fieldtype": "Currency", "width": 180},
	{"id": "credit_balance", "label": "Credit Balance", "fieldtype": "Currency", "width": 180},
]

VOUCHER_COLUMNS = [
	{"id": "posting_date", "label": "Posting Date", "fieldtype": "Date", "width": 110},
	{"id": "voucher_type", "label": "Voucher Type", "fieldtype": "Data", "width": 130},
	{"id": "voucher_no", "label": "Voucher No", "fieldtype": "Data", "width": 140},
	{"id": "party_type", "label": "Party Type", "fieldtype": "Data", "width": 110},
	{"id": "party_name", "label": "Party", "fieldtype": "Data", "width": 180},
	{"id": "voucher_title", "label": "Title", "fieldtype": "Data", "width": 180},
	{"id": "reference", "label": "Reference", "fieldtype": "Data", "width": 140},
	{"id": "scoped_debit", "label": "Scoped Debit", "fieldtype": "Currency", "width": 130},
	{"id": "scoped_credit", "label": "Scoped Credit", "fieldtype": "Currency", "width": 130},
	{"id": "scoped_net", "label": "Scoped Net", "fieldtype": "Currency", "width": 130},
	{"id": "full_voucher_debit", "label": "Full Voucher Debit", "fieldtype": "Currency", "width": 130},
	{"id": "full_voucher_credit", "label": "Full Voucher Credit", "fieldtype": "Currency", "width": 130},
]

GL_GROUP_BASE_COLUMNS = [
	{"id": "posting_date", "label": "Posting Date", "fieldtype": "Date", "width": 110},
	{"id": "account", "label": "Account", "fieldtype": "Link", "width": 140},
	{"id": "account_name", "label": "Account Name", "fieldtype": "Data", "width": 180},
	{"id": "party_type", "label": "Party Type", "fieldtype": "Data", "width": 110},
	{"id": "party_name", "label": "Party", "fieldtype": "Data", "width": 160},
]

GL_GROUP_COMPACT_COLUMN = {
	"id": "dimensions",
	"label": "",
	"label_key": "Accounting Dimension Details",
	"fieldtype": "Data",
	"width": 260,
	"column_kind": "dimensions_compact",
}

GL_GROUP_TAIL_COLUMNS = [
	{"id": "debit", "label": "Debit", "fieldtype": "Currency", "width": 120},
	{"id": "credit", "label": "Credit", "fieldtype": "Currency", "width": 120},
	{"id": "currency", "label": "Currency", "fieldtype": "Data", "width": 90},
	{"id": "remarks", "label": "Remarks", "fieldtype": "Data", "width": 220},
]


def build_gl_detail_columns(dimensions: list[dict] | None = None) -> list[dict]:
	dimensions = dimensions if dimensions is not None else get_discovered_dimensions()
	dimension_columns = [
		{
			"id": f"dim:{row['fieldname']}",
			"label": row["label"],
			"label_fa": row.get("label_fa"),
			"fieldtype": "Link",
			"width": 140,
			"dimension_fieldname": row["fieldname"],
			"column_kind": "dimension",
		}
		for row in dimensions
	]
	return [*GL_GROUP_BASE_COLUMNS, GL_GROUP_COMPACT_COLUMN, *dimension_columns, *GL_GROUP_TAIL_COLUMNS]

def _currency_columns_for_company(company: str | None = None) -> list[dict]:
	from erpnext_extensions.iran_accounting.account_explorer.currency_summary import build_currency_columns

	company_currency = None
	if company:
		company_currency = frappe.get_cached_value("Company", company, "default_currency")
	return build_currency_columns(company_currency)


# Static fallback for metadata when company is unknown (labels use generic Company code).
CURRENCY_COLUMNS = _currency_columns_for_company(None)


def _item_group_columns() -> list[dict]:
	from erpnext_extensions.iran_accounting.account_explorer.item_group_summary import ITEM_GROUP_COLUMNS

	return ITEM_GROUP_COLUMNS


def _item_columns() -> list[dict]:
	from erpnext_extensions.iran_accounting.account_explorer.item_summary import ITEM_COLUMNS

	return ITEM_COLUMNS


def get_metadata_enrichment(company: str | None = None) -> dict:
	"""Deferred metadata: currencies and other expensive discoveries.

	Called after the toolbar shell is already painted.
	"""
	company = company or frappe.defaults.get_user_default("Company") or frappe.defaults.get_global_default(
		"company"
	)
	currencies: list[str] = []
	if company:
		currencies = discover_company_currencies(company)
	return {
		"company": company,
		"currencies": currencies,
		"currency_columns": _currency_columns_for_company(company),
	}


def get_metadata() -> dict:
	settings = frappe.get_single("Iran Accounting Settings")
	levels = [
		{
			"sequence": int(row.sequence),
			"enabled": int(row.enabled),
			"code_length": int(row.code_length),
			"title": row.title,
			"title_fa": row.title_fa,
			"short_title": row.short_title,
			"drill_down_enabled": int(row.drill_down_enabled),
			"default_visible": int(row.default_visible),
			"default_sort_order": row.default_sort_order,
		}
		for row in get_enabled_levels()
	]
	party_sources = []
	for row in get_enabled_party_sources():
		warning = None
		if row.identifier_field:
			meta = frappe.get_meta(row.party_type)
			if not meta.has_field(row.identifier_field):
				warning = frappe._("Identifier field {0} is missing on {1}.").format(
					row.identifier_field, row.party_type
				)
		party_sources.append(
			{
				"party_type": row.party_type,
				"enabled": int(row.enabled),
				"sequence": int(row.sequence),
				"label": row.label or row.party_type,
				"label_fa": row.label_fa,
				"identifier_field": row.identifier_field,
				"identifier_warning": warning,
				"show_in_unified_party": int(row.show_in_unified_party or 0),
			}
		)
	dimensions = get_discovered_dimensions()
	level_children = [
		{**level, "nav_kind": "account_level"}
		for level in levels
		if level.get("sequence") is not None and "fieldname" not in level
	]
	dimension_children = [
		{
			"fieldname": row["fieldname"],
			"label": row["label"],
			"document_type": row.get("document_type"),
			"nav_kind": "dimension_type",
		}
		for row in dimensions
	]
	company = frappe.defaults.get_user_default("Company") or frappe.defaults.get_global_default("company")
	fiscal_year = None
	from_date = None
	to_date = None
	# Currencies are expensive (GL DISTINCT) — deferred via get_account_explorer_metadata_enrichment.
	currencies: list[str] = []
	if company:
		from erpnext_extensions.iran_accounting.account_explorer.query_spec import _resolve_fiscal_year

		fiscal_year, from_date, to_date = _resolve_fiscal_year(company, None, None, None)

	party_enabled = int(settings.party_analysis_enabled)
	dimension_enabled = int(settings.dimension_analysis_enabled)
	voucher_enabled = int(settings.voucher_analysis_enabled or 0)
	unified_party_enabled = int(settings.unified_party_enabled or 0)
	currency_enabled = int(settings.currency_analysis_enabled or 0)
	inventory_enabled = int(getattr(settings, "inventory_analysis_enabled", 1) or 0)
	if getattr(settings, "inventory_analysis_enabled", None) is None:
		inventory_enabled = 1
	saved_views_enabled = int(settings.saved_views_enabled or 0)
	export_enabled = int(settings.export_enabled or 0)
	from erpnext_extensions.iran_accounting.account_explorer.export import (
		normalize_export_background_threshold,
	)

	export_background_threshold = normalize_export_background_threshold(
		settings.export_background_threshold
	)
	diagnostics_enabled = int(settings.diagnostics_enabled or 0)
	datatable_enabled = int(settings.account_explorer_datatable_enabled or 0)
	axes = [
		{
			"id": "account_level",
			"label": "Account Levels",
			"enabled": 1,
			"children": level_children,
			"measure_family": "gl",
			"subtitle": (
				"CASE B: posted GL when Account is the starting axis. "
				"CASE A: with Item/Item Group/Warehouse filters — SLE-scoped stock "
				"breakdown (Item|Item Group → Account EQUAL Δ=0; reverse not required)"
			),
			"subtitle_fa": (
				"حالت B: دفتر کل وقتی محور حساب است. "
				"حالت A: با فیلتر کالا/گروه/انبار — تفکیک ارزش موجودی SLE "
				"(گروه کالا→حساب برابر؛ جهت معکوس الزامی نیست)"
			),
			"inventory_filter_mode": "sle_scoped_stock",
			"case_a_relation": "EQUAL",
			"case_b_relation": "RECONCILABLE",
			"stock_peer": ["item_group", "item"],
		},
		{
			"id": "party",
			"label": "Parties",
			"enabled": party_enabled,
			"measure_family": "gl",
			"inventory_filter_mode": "voucher_scoped_gl",
		},
		{
			"id": "unified_party",
			"label": "Unified Parties",
			# Backend axis remains available; UI nav tab is hidden in Account Explorer (v4.6.2).
			"enabled": 1 if party_enabled and unified_party_enabled else 0,
			"ui_nav": 0,
			"measure_family": "gl",
			"inventory_filter_mode": "voucher_scoped_gl",
		},
		{
			"id": "dimension",
			"label": "Dimensions",
			"enabled": dimension_enabled,
			"children": dimension_children,
			"measure_family": "gl",
			"inventory_filter_mode": "voucher_scoped_gl",
		},
		{
			"id": "currency",
			"label": "Currencies",
			"enabled": currency_enabled,
			"measure_family": "gl",
			"inventory_filter_mode": "voucher_scoped_gl",
		},
		{
			"id": "item_group",
			"label": "Item Groups",
			"label_fa": "گروه کالا",
			"enabled": inventory_enabled,
			"measure_family": "stock",
			"subtitle": "Stock value — peer of Item (not GL Account turnover)",
			"subtitle_fa": "ارزش موجودی — هم‌تراز کالا (نه گردش حساب دفتر کل)",
			"stock_peer": ["item"],
		},
		{
			"id": "item",
			"label": "Items",
			"label_fa": "کالا",
			"enabled": inventory_enabled,
			"measure_family": "stock",
			"subtitle": "Stock qty/value — peer of Item Group (not GL Account turnover)",
			"subtitle_fa": "مقدار/ارزش موجودی — هم‌تراز گروه کالا (نه گردش حساب دفتر کل)",
			"stock_peer": ["item_group"],
		},
		# Voucher/Documents must remain the last visible axis (v5.1.1 UX).
		{
			"id": "voucher",
			"label": "Vouchers",
			"enabled": voucher_enabled,
			"measure_family": "gl",
			"subtitle": (
				"Real GL vouchers — with inventory filters: only vouchers with scoped SLE"
			),
			"subtitle_fa": (
				"اسناد دفتر کل واقعی — با فیلتر موجودی: فقط اسناد دارای حرکت موجودی در محدوده"
			),
			"inventory_filter_mode": "voucher_scoped_gl",
		},
	]

	return {
		"enabled": int(settings.account_explorer_enabled),
		"party_analysis_enabled": party_enabled,
		"dimension_analysis_enabled": dimension_enabled,
		"voucher_analysis_enabled": voucher_enabled,
		"unified_party_enabled": unified_party_enabled,
		"currency_analysis_enabled": currency_enabled,
		"inventory_analysis_enabled": inventory_enabled,
		"saved_views_enabled": saved_views_enabled,
		"export_enabled": export_enabled,
		"export_background_threshold": export_background_threshold,
		"diagnostics_enabled": diagnostics_enabled,
		"account_explorer_datatable_enabled": datatable_enabled,
		"allow_gl_entry_navigation": int(settings.allow_gl_entry_navigation or 0),
		"voucher_print_format": settings.account_explorer_voucher_print_format or None,
		"show_print_voucher": int(getattr(settings, "show_print_voucher", 1) or 0),
		"show_print_gl": int(getattr(settings, "show_print_gl", 1) or 0),
		"voucher_gl_print_format": getattr(settings, "voucher_gl_print_format", None) or None,
		"voucher_gl_layout": getattr(settings, "voucher_gl_layout", None) or "Standard",
		"voucher_gl_page_layout": getattr(settings, "voucher_gl_page_layout", None) or "Auto",
		"voucher_gl_amount_scale": getattr(settings, "voucher_gl_amount_scale", None) or "Raw",
		"voucher_gl_print_language": getattr(settings, "voucher_gl_print_language", None) or "Persian",
		"default_amount_display_scale": getattr(settings, "default_amount_display_scale", None) or "Raw",
		"append_source_attachments": int(getattr(settings, "append_source_attachments", 0) or 0),
		"voucher_gl_auto_orientation": int(getattr(settings, "voucher_gl_auto_orientation", 1) or 0),
		"voucher_gl_show_logo": int(getattr(settings, "voucher_gl_show_logo", 1) or 0),
		"voucher_gl_show_letterhead": int(getattr(settings, "voucher_gl_show_letterhead", 0) or 0),
		"voucher_gl_show_amount_in_words": int(getattr(settings, "voucher_gl_show_amount_in_words", 1) or 0),
		"voucher_gl_show_signature_block": int(getattr(settings, "voucher_gl_show_signature_block", 1) or 0),
		"voucher_gl_hide_empty_columns": int(getattr(settings, "voucher_gl_hide_empty_columns", 1) or 0),
		"voucher_gl_combine_dimensions": int(getattr(settings, "voucher_gl_combine_dimensions", 1) or 0),
		"voucher_gl_show_account_hierarchy": int(getattr(settings, "voucher_gl_show_account_hierarchy", 1) or 0),
		"voucher_gl_hierarchy_start_level": int(getattr(settings, "voucher_gl_hierarchy_start_level", 2) or 2),
		"voucher_gl_show_party_breakdown": int(getattr(settings, "voucher_gl_show_party_breakdown", 1) or 0),
		"voucher_gl_show_dimension_breakdown": int(getattr(settings, "voucher_gl_show_dimension_breakdown", 1) or 0),
		"voucher_gl_show_group_subtotals": int(getattr(settings, "voucher_gl_show_group_subtotals", 1) or 0),
		"show_amount_scale_label": int(getattr(settings, "show_amount_scale_label", 1) or 0),
		"amount_scale_decimal_precision": int(getattr(settings, "amount_scale_decimal_precision", 2) or 2),
		"axes": axes,
		"levels": levels,
		"party_sources": party_sources,
		"dimensions": dimensions,
		"currencies": currencies,
		"default_dimension_field": get_default_dimension_field(),
		"default_dimension_type": get_default_dimension_type(),
		"currency_types": [
			{"value": "account_currency", "label": frappe._("Account Currency")},
			{"value": "transaction_currency", "label": frappe._("Transaction Currency")},
		],
		"configuration_warnings": get_identifier_warnings(),
		"defaults": {
			"document_scope": {
				"company": company,
				"fiscal_year": fiscal_year,
				"from_date": str(from_date) if from_date else None,
				"to_date": str(to_date) if to_date else None,
				"hide_zero_rows": int(settings.default_hide_zero_rows),
				"status": {
					"include_cancelled_entries": int(settings.default_include_cancelled),
					"include_opening_entries": int(settings.default_include_opening_entries),
					"include_period_closing_vouchers": int(settings.default_include_period_closing_vouchers),
					"include_default_finance_book_entries": 1,
				},
				"voucher": {},
				"accounting": {},
				"accounting_dimensions": {},
				"currency": {"currency_type": "account_currency", "currency": None},
				"inventory": {"item_group": None, "item": None, "warehouse": None},
			},
			"page_size": int(settings.default_page_size) or 50,
		},
		"columns": SUMMARY_COLUMNS,
		"party_columns": PARTY_COLUMNS,
		"unified_party_columns": UNIFIED_PARTY_COLUMNS,
		"dimension_columns": DIMENSION_COLUMNS,
		"currency_columns": CURRENCY_COLUMNS,
		"voucher_columns": VOUCHER_COLUMNS,
		"item_group_columns": _item_group_columns(),
		"item_columns": _item_columns(),
		"gl_group_columns": build_gl_detail_columns(dimensions),
		"filter_axis_matrix": FILTER_AXIS_COMPATIBILITY,
		"gl_dimension_expand_threshold": GL_DIMENSION_EXPAND_THRESHOLD,
		"metadata_cache_version": int(settings.metadata_cache_version or 1),
		"default_level_sequence": get_default_level_sequence(),
		"currencies_deferred": 1,
	}


def validate_document_scope(payload) -> dict:
	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
	return {
		"ok": True,
		"company": spec.company,
		"from_date": str(spec.from_date),
		"to_date": str(spec.to_date),
		"fiscal_year": spec.fiscal_year,
		"scoped_account_count": len(spec.included_account_names or []),
		"document_scope": _document_scope_response(spec),
	}


def get_account_summary(payload) -> dict:
	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
	if spec.view_axis != "account_level":
		frappe.throw(frappe._("Invalid axis for account summary."))
	if _should_use_prepared(spec, payload):
		from erpnext_extensions.iran_accounting.account_explorer.prepared_report import (
			resolve_prepared_or_enqueue,
		)

		return resolve_prepared_or_enqueue(
			spec,
			payload=payload,
			columns=SUMMARY_COLUMNS,
			response_builder=_summary_response,
		)
	result = build_account_level_summary(spec)
	return _summary_response(spec, SUMMARY_COLUMNS, result)


def get_party_summary(payload) -> dict:
	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
	if spec.view_axis != "party":
		frappe.throw(frappe._("Invalid axis for party summary."))
	from erpnext_extensions.iran_accounting.account_explorer.party_summary import build_party_summary

	result = build_party_summary(spec)
	return _summary_response(spec, PARTY_COLUMNS, result)


def get_unified_party_summary(payload) -> dict:
	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
	if spec.view_axis != "unified_party":
		frappe.throw(frappe._("Invalid axis for unified party summary."))
	from erpnext_extensions.iran_accounting.account_explorer.unified_party_summary import (
		build_unified_party_summary,
	)

	result = build_unified_party_summary(spec)
	return _summary_response(spec, UNIFIED_PARTY_COLUMNS, result)


def get_unified_party_member_breakdown(payload) -> dict:
	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
	from erpnext_extensions.iran_accounting.account_explorer.permissions import assert_unified_party_enabled

	assert_unified_party_enabled()
	if not spec.unified_party_scope.selected_unified_party:
		frappe.throw(frappe._("Unified Accounting Party is required for member breakdown."))
	from erpnext_extensions.iran_accounting.account_explorer.unified_party_summary import (
		build_unified_party_member_breakdown,
	)

	result = build_unified_party_member_breakdown(spec)
	return _summary_response(spec, PARTY_COLUMNS, result)


def get_unified_party_suggestions(payload) -> dict:
	from erpnext_extensions.iran_accounting.account_explorer.permissions import (
		assert_unified_party_suggestions_allowed,
	)
	from erpnext_extensions.iran_accounting.account_explorer.unified_party_suggestions import (
		build_unified_party_suggestions,
	)

	assert_unified_party_suggestions_allowed()
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	document_scope = data.get("document_scope") or data
	return build_unified_party_suggestions(
		company=document_scope.get("company"),
		limit=data.get("limit") or 50,
	)


def get_dimension_summary(payload) -> dict:
	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
	if spec.view_axis != "dimension":
		frappe.throw(frappe._("Invalid axis for dimension summary."))
	from erpnext_extensions.iran_accounting.account_explorer.dimension_summary import (
		build_dimension_summary,
	)

	result = build_dimension_summary(spec)
	return _summary_response(spec, DIMENSION_COLUMNS, result)


def get_currency_summary(payload) -> dict:
	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
	if spec.view_axis != "currency":
		frappe.throw(frappe._("Invalid axis for currency summary."))
	from erpnext_extensions.iran_accounting.account_explorer.currency_summary import build_currency_summary

	result = build_currency_summary(spec)
	return _summary_response(spec, _currency_columns_for_company(spec.company), result)


def get_voucher_summary(payload) -> dict:
	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
	if spec.view_axis != "voucher" or spec.detail_mode != "summary":
		frappe.throw(frappe._("Invalid axis for voucher summary."))
	if _should_use_prepared(spec, payload):
		from erpnext_extensions.iran_accounting.account_explorer.prepared_report import (
			resolve_prepared_or_enqueue,
		)

		return resolve_prepared_or_enqueue(
			spec,
			payload=payload,
			columns=VOUCHER_COLUMNS,
			response_builder=_voucher_response,
		)
	from erpnext_extensions.iran_accounting.account_explorer.voucher_summary import build_voucher_summary

	result = build_voucher_summary(spec)
	return _voucher_response(spec, VOUCHER_COLUMNS, result)


def get_item_group_summary(payload) -> dict:
	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
	if spec.view_axis != "item_group":
		frappe.throw(frappe._("Invalid axis for item group summary."))
	from erpnext_extensions.iran_accounting.account_explorer.item_group_summary import (
		ITEM_GROUP_COLUMNS,
		build_item_group_summary,
	)

	result = build_item_group_summary(spec)
	return _summary_response(spec, ITEM_GROUP_COLUMNS, result)


def get_item_summary(payload) -> dict:
	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
	if spec.view_axis != "item":
		frappe.throw(frappe._("Invalid axis for item summary."))
	from erpnext_extensions.iran_accounting.account_explorer.item_summary import ITEM_COLUMNS, build_item_summary

	result = build_item_summary(spec)
	return _summary_response(spec, ITEM_COLUMNS, result)


def get_inventory_account_summary(payload) -> dict:
	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
	if spec.view_axis != "inventory_account":
		frappe.throw(frappe._("Invalid axis for inventory account summary."))
	from erpnext_extensions.iran_accounting.account_explorer.inventory_account_summary import (
		INVENTORY_ACCOUNT_COLUMNS,
		build_inventory_account_summary,
	)

	result = build_inventory_account_summary(spec)
	return _summary_response(spec, INVENTORY_ACCOUNT_COLUMNS, result)


def get_grouped_gl_entries(payload) -> dict:
	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
	if spec.detail_mode != "grouped_gl":
		frappe.throw(frappe._("Invalid detail mode for grouped GL entries."))
	from erpnext_extensions.iran_accounting.account_explorer.voucher_gl import build_grouped_gl_entries

	result = build_grouped_gl_entries(spec)
	columns = build_gl_detail_columns(result.get("dimensions"))
	return _grouped_gl_response(spec, columns, result)


CONSTRUCTED_LEG_COLUMNS = [
	{"id": "posting_date", "label": "Posting Date", "fieldtype": "Date"},
	{"id": "account", "label": "Account", "fieldtype": "Link"},
	{"id": "debit", "label": "Debit", "fieldtype": "Currency"},
	{"id": "credit", "label": "Credit", "fieldtype": "Currency"},
	{"id": "against", "label": "Against", "fieldtype": "Data"},
	{"id": "item_code", "label": "Item", "fieldtype": "Link"},
	{"id": "item_group", "label": "Item Group", "fieldtype": "Link"},
	{"id": "warehouse", "label": "Warehouse", "fieldtype": "Link"},
	{"id": "voucher_type", "label": "Voucher Type", "fieldtype": "Data"},
	{"id": "voucher_no", "label": "Voucher", "fieldtype": "Dynamic Link"},
	{"id": "voucher_detail_no", "label": "Voucher Detail", "fieldtype": "Data"},
	{"id": "sle_names", "label": "SLE", "fieldtype": "Data"},
	{"id": "rule_tag", "label": "Rule", "fieldtype": "Data"},
	{"id": "native_source_path", "label": "Native Source", "fieldtype": "Data"},
]


def get_constructed_accounting_legs(payload) -> dict:
	"""Optional diagnostic only — NOT the Account summary engine (v5.1.1).

	Case A Account summary uses ``sle_scoped_stock`` (SLE → warehouse → account).
	This endpoint remains for forensic comparison of native pre-merge GL legs only.
	"""
	from frappe.utils import cint

	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
	from erpnext_extensions.iran_accounting.account_explorer.sle_scoped_account import (
		ACCOUNT_FACT_ENGINE_POSTED,
		select_account_fact_engine,
	)
	from erpnext_extensions.iran_accounting.account_explorer.stock_construction_replay import (
		construction_meta_dict,
		get_constructed_legs_cached,
	)

	if select_account_fact_engine(spec) == ACCOUNT_FACT_ENGINE_POSTED:
		frappe.throw(
			frappe._("Constructed accounting legs require Item / Item Group / Warehouse filters.")
		)

	construction = get_constructed_legs_cached(spec)
	data = frappe.parse_json(payload) if isinstance(payload, str) else (payload or {})
	account_filter = data.get("selected_account") or (
		spec.account_scope.selected_account if spec.account_scope else None
	)

	legs = construction.legs
	if account_filter:
		from erpnext_extensions.iran_accounting.account_explorer.account_hierarchy import (
			load_company_accounts,
		)

		accounts = load_company_accounts(spec.company)
		by_name = {a["name"]: a for a in accounts}
		allowed = {account_filter}
		root = by_name.get(account_filter)
		if root and cint(root.get("is_group")):
			for a in accounts:
				if a["lft"] >= root["lft"] and a["rgt"] <= root["rgt"]:
					allowed.add(a["name"])
		legs = [leg for leg in legs if leg.account in allowed]

	rows = []
	for leg in legs:
		d = leg.as_dict()
		d["sle_names"] = ", ".join(d.get("sle_names") or [])
		d["row_key"] = (
			f"{leg.voucher_type}:{leg.voucher_no}:{leg.account}:"
			f"{leg.debit}:{leg.credit}:{leg.voucher_detail_no}"
		)
		rows.append(d)

	currency = frappe.get_cached_value("Company", spec.company, "default_currency")
	return {
		"columns": CONSTRUCTED_LEG_COLUMNS,
		"currency": {"code": currency, "precision": frappe.defaults.get_global_default("currency_precision")},
		"document_scope": _document_scope_response(spec),
		"context": _analysis_context_response(spec),
		"rows": rows,
		"total_rows": len(rows),
		"detail_kind": "constructed_accounting_legs",
		"diagnostic_only": 1,
		"account_summary_engine": select_account_fact_engine(spec),
		**construction_meta_dict(construction),
	}


def get_voucher_navigation_target(payload) -> dict:
	from erpnext_extensions.iran_accounting.account_explorer.voucher_navigation import resolve_voucher_navigation

	return resolve_voucher_navigation(payload)


def render_voucher_gl_print(company=None, voucher_type=None, voucher_no=None, filters=None) -> str:
	"""Return full HTML for native print preview of voucher GL lines."""
	import json

	from erpnext_extensions.iran_accounting.account_explorer.voucher_gl_print import (
		render_voucher_gl_print_html,
	)

	merged = {}
	if filters:
		if isinstance(filters, str):
			merged.update(json.loads(filters))
		elif isinstance(filters, dict):
			merged.update(filters)
	if company:
		merged["company"] = company
	if voucher_type:
		merged["voucher_type"] = voucher_type
	if voucher_no:
		merged["voucher_no"] = voucher_no
	return render_voucher_gl_print_html(merged)


def get_account_scope_preview(payload) -> dict:
	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=False)
	if spec.requires_bounded_dates():
		spec.included_account_names = spec.included_account_names or []
	return {
		"account_scope": _analysis_context_response(spec)["account_scope"],
		"scoped_account_count": len(spec.included_account_names or []),
	}


def _summary_response(spec: AccountExplorerQuerySpec, columns, result: dict) -> dict:
	currency = frappe.get_cached_value("Company", spec.company, "default_currency")
	return {
		"columns": columns,
		"currency": {"code": currency, "precision": frappe.defaults.get_global_default("currency_precision")},
		"document_scope": _document_scope_response(spec),
		"context": _analysis_context_response(spec),
		**result,
	}


def _should_use_prepared(spec: AccountExplorerQuerySpec, payload) -> bool:
	from erpnext_extensions.iran_accounting.account_explorer.prepared_report import (
		axis_uses_prepared_results,
	)

	if getattr(frappe.flags, "ae_prepared_skip", False):
		return False
	data = payload
	if isinstance(payload, str):
		data = frappe.parse_json(payload) if payload else {}
	data = data or {}
	mode = (data.get("prepared_mode") or "").lower()
	if mode in {"live", "skip", "off", "0"}:
		return False
	return axis_uses_prepared_results(spec)


def _document_scope_response(spec: AccountExplorerQuerySpec) -> dict:
	ds = spec.document_scope
	return {
		"company": ds.company,
		"fiscal_year": ds.fiscal_year,
		"from_date": str(ds.from_date) if ds.from_date else None,
		"to_date": str(ds.to_date) if ds.to_date else None,
		"finance_book": ds.finance_book,
		"hide_zero_rows": int(ds.hide_zero_rows),
		"voucher": {
			"voucher_type": ds.voucher.voucher_type,
			"voucher_no": ds.voucher.voucher_no,
			"against_voucher_type": ds.voucher.against_voucher_type,
			"against_voucher_no": ds.voucher.against_voucher_no,
			"reference_no": ds.voucher.reference_no,
		},
		"accounting": {
			"account": ds.accounting.account,
			"party_type": ds.accounting.party_type,
			"party": ds.accounting.party,
		},
		"accounting_dimensions": ds.accounting_dimensions,
		"currency": {
			"currency_type": ds.currency.currency_type,
			"currency": ds.currency.currency,
		},
		"status": {
			"include_opening_entries": int(ds.status.include_opening_entries),
			"include_cancelled_entries": int(ds.status.include_cancelled_entries),
			"include_default_finance_book_entries": int(ds.status.include_default_finance_book_entries),
			"include_period_closing_vouchers": int(ds.status.include_period_closing_vouchers),
		},
		"inventory": {
			"item_group": ds.inventory.item_group if hasattr(ds, "inventory") else None,
			"item": ds.inventory.item if hasattr(ds, "inventory") else None,
			"warehouse": ds.inventory.warehouse if hasattr(ds, "inventory") else None,
		},
	}


def _analysis_context_response(spec: AccountExplorerQuerySpec) -> dict:
	return {
		"view_axis": spec.view_axis,
		"level_sequence": spec.level_sequence,
		"account_scope": {
			"mode": spec.account_scope.mode,
			"selected_account": spec.account_scope.selected_account,
			"virtual_row_key": spec.account_scope.virtual_row_key,
			"is_virtual_group": spec.account_scope.is_virtual_group,
			"level_sequence": spec.account_scope.level_sequence,
			"tree_root_account": spec.account_scope.tree_root_account,
		},
		"party_scope": {
			"party_type": spec.party_scope.party_type,
			"selected_party": spec.party_scope.selected_party,
		},
		"unified_party_scope": {
			"selected_unified_party": spec.unified_party_scope.selected_unified_party,
			"include_unmapped": int(spec.unified_party_scope.include_unmapped),
		},
		"dimension_scope": {
			"dimension_type": spec.dimension_scope.dimension_type,
			"selected_dimension_value": spec.dimension_scope.selected_dimension_value,
		},
		"voucher_scope": {
			"voucher_type": spec.voucher_scope.voucher_type,
			"voucher_no": spec.voucher_scope.voucher_no,
		},
		"item_group_scope": {
			"selected_item_group": getattr(spec.item_group_scope, "selected_item_group", None),
		},
		"item_scope": {
			"selected_item": getattr(spec.item_scope, "selected_item", None),
		},
		"detail_mode": spec.detail_mode,
	}


def _voucher_response(spec: AccountExplorerQuerySpec, columns, result: dict) -> dict:
	currency = frappe.get_cached_value("Company", spec.company, "default_currency")
	return {
		"columns": columns,
		"currency": {"code": currency, "precision": frappe.defaults.get_global_default("currency_precision")},
		"document_scope": _document_scope_response(spec),
		"context": _analysis_context_response(spec),
		**result,
	}


def _grouped_gl_response(spec: AccountExplorerQuerySpec, columns, result: dict) -> dict:
	currency = frappe.get_cached_value("Company", spec.company, "default_currency")
	return {
		"columns": columns,
		"currency": {"code": currency, "precision": frappe.defaults.get_global_default("currency_precision")},
		"document_scope": _document_scope_response(spec),
		"context": _analysis_context_response(spec),
		**result,
	}
