# Copyright (c) 2026, Farbod Siyahpoosh and contributors

"""Account Explorer whitelisted API entry points."""

from __future__ import annotations

import frappe


@frappe.whitelist()
def get_account_explorer_metadata():
	from erpnext_extensions.iran_accounting.account_explorer.api import get_metadata

	return get_metadata()


@frappe.whitelist()
def get_account_explorer_metadata_enrichment(company=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import get_metadata_enrichment

	return get_metadata_enrichment(company=company)


@frappe.whitelist()
def validate_document_scope(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import validate_document_scope as _validate

	return _validate(payload)


@frappe.whitelist()
def get_account_summary(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import get_account_summary as _summary

	return _summary(payload)


@frappe.whitelist()
def get_party_summary(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import get_party_summary as _summary

	return _summary(payload)


@frappe.whitelist()
def get_unified_party_summary(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import get_unified_party_summary as _summary

	return _summary(payload)


@frappe.whitelist()
def get_unified_party_member_breakdown(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import (
		get_unified_party_member_breakdown as _breakdown,
	)

	return _breakdown(payload)


@frappe.whitelist()
def get_unified_party_suggestions(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import get_unified_party_suggestions as _suggest

	return _suggest(payload)


@frappe.whitelist()
def get_currency_summary(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import get_currency_summary as _summary

	return _summary(payload)


@frappe.whitelist()
def get_dimension_summary(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import get_dimension_summary as _summary

	return _summary(payload)


@frappe.whitelist()
def get_voucher_summary(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import get_voucher_summary as _summary

	return _summary(payload)


@frappe.whitelist()
def get_item_group_summary(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import get_item_group_summary as _summary

	return _summary(payload)


@frappe.whitelist()
def get_item_summary(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import get_item_summary as _summary

	return _summary(payload)


@frappe.whitelist()
def get_inventory_account_summary(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import (
		get_inventory_account_summary as _summary,
	)

	return _summary(payload)


@frappe.whitelist()
def get_grouped_gl_entries(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import get_grouped_gl_entries as _entries

	return _entries(payload)


@frappe.whitelist()
def get_constructed_accounting_legs(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import (
		get_constructed_accounting_legs as _legs,
	)

	return _legs(payload)


@frappe.whitelist()
def get_voucher_navigation_target(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import get_voucher_navigation_target as _target

	return _target(payload)


@frappe.whitelist()
def render_voucher_gl_print(company=None, voucher_type=None, voucher_no=None, filters=None):
	"""One-click Print GL HTML (no intermediate report filter page)."""
	from erpnext_extensions.iran_accounting.account_explorer.api import render_voucher_gl_print as _render

	return _render(
		company=company,
		voucher_type=voucher_type,
		voucher_no=voucher_no,
		filters=filters,
	)


@frappe.whitelist()
def get_account_scope_preview(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.api import get_account_scope_preview as _preview

	return _preview(payload)


@frappe.whitelist()
def list_account_explorer_saved_views(company=None):
	from erpnext_extensions.iran_accounting.account_explorer.saved_views import list_saved_views

	return list_saved_views(company=company)


@frappe.whitelist()
def get_account_explorer_saved_view(name=None):
	from erpnext_extensions.iran_accounting.account_explorer.saved_views import get_saved_view

	return get_saved_view(name)


@frappe.whitelist()
def save_account_explorer_saved_view(payload=None):
	from erpnext_extensions.iran_accounting.account_explorer.saved_views import save_saved_view

	return save_saved_view(payload)


@frappe.whitelist()
def delete_account_explorer_saved_view(name=None):
	from erpnext_extensions.iran_accounting.account_explorer.saved_views import delete_saved_view

	return delete_saved_view(name)


@frappe.whitelist()
def export_account_explorer(payload=None, file_format="csv", force_sync=0):
	from frappe.utils import cint

	from erpnext_extensions.iran_accounting.account_explorer.export import export_account_explorer as _export

	return _export(payload, file_format, force_sync=bool(cint(force_sync)))


@frappe.whitelist()
def get_account_explorer_diagnostics(company=None):
	from erpnext_extensions.iran_accounting.account_explorer.diagnostics import run_account_explorer_diagnostics

	return run_account_explorer_diagnostics(company)


@frappe.whitelist()
def run_account_explorer_performance_benchmark(
	company=None, fiscal_year=None, from_date=None, to_date=None
):
	from erpnext_extensions.iran_accounting.account_explorer.performance_benchmark import (
		run_account_explorer_performance_benchmark as _benchmark,
	)

	return _benchmark(company, fiscal_year, from_date, to_date)
