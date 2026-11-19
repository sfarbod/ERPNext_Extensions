# Copyright (c) 2026, ERPNext Extensions contributors
"""Whitelisted API for ETO SCR valuation propagation (v5.8.3)."""

from __future__ import annotations

import frappe
from frappe.utils import cint

from erpnext_extensions.iran_accounting.eto_scr_valuation_propagation import (
	API_VERSION,
	CONFIRM_APPLY,
	MAX_ALLOWLIST,
)
from erpnext_extensions.iran_accounting.eto_scr_valuation_propagation.service import (
	apply_propagation,
	build_valuation_chain,
	plan_propagation,
	process_allowlist,
	require_privileged_user,
)


@frappe.whitelist(methods=["GET", "POST"])
def get_eto_scr_valuation_propagation_api_status():
	"""Read-only capability probe."""
	require_privileged_user()
	return {
		"api_version": API_VERSION,
		"max_allowlist": MAX_ALLOWLIST,
		"confirm_apply": CONFIRM_APPLY,
		"capabilities": [
			"inspect_chain",
			"plan",
			"dry_run_allowlist",
			"apply_allowlist",
			"optional_service_cost_correction",
			"item_warehouse_stock_riv",
		],
		"restrictions": [
			"development_supported_apis_only",
			"no_sql_sle_gl_bin_batch_edits",
			"no_lcv_recreation",
			"no_duplicate_concurrent_riv",
			"dry_run_default",
			"explicit_confirm_token_required_to_mutate",
		],
		"related_investigation": "docs/investigations/ETO_SCR_REPOST_PROPAGATION.md",
		"depends_on": [
			"eto_scr_repost_scope (v5.8.1 SABE outward-rate selection)",
			"subcontracting_receipt_service_cost (optional SCR field correction)",
		],
	}


@frappe.whitelist(methods=["GET", "POST"])
def inspect_scr_eto_valuation_chain(subcontracting_receipt: str) -> dict:
	"""Read-only chronological SCR → Transfer → Manufacture valuation chain."""
	return build_valuation_chain(subcontracting_receipt)


@frappe.whitelist(methods=["GET", "POST"])
def plan_scr_eto_valuation_propagation(
	subcontracting_receipt: str,
	include_service_cost_correction=None,
) -> dict:
	"""Read-only dry-run plan for one SCR."""
	flag = None
	if include_service_cost_correction is not None and include_service_cost_correction != "":
		flag = bool(cint(include_service_cost_correction))
	return plan_propagation(subcontracting_receipt, include_service_cost_correction=flag)


@frappe.whitelist(methods=["GET", "POST"])
def dry_run_scr_eto_valuation_propagation(
	subcontracting_receipts,
	include_service_cost_correction=None,
) -> dict:
	"""Dry-run allowlisted propagation plans (no mutation)."""
	flag = None
	if include_service_cost_correction is not None and include_service_cost_correction != "":
		flag = bool(cint(include_service_cost_correction))
	return process_allowlist(
		subcontracting_receipts,
		dry_run=True,
		include_service_cost_correction=flag,
	)


@frappe.whitelist(methods=["POST"])
def propagate_scr_eto_valuation(
	subcontracting_receipts,
	dry_run=1,
	confirm=None,
	include_service_cost_correction=None,
	execute_riv=1,
) -> dict:
	"""Propagate corrected ETO SCR valuation through supported RIV.

	Mutations require dry_run=0 and confirm=APPLY_ETO_SCR_VALUATION_PROPAGATION.
	"""
	flag = None
	if include_service_cost_correction is not None and include_service_cost_correction != "":
		flag = bool(cint(include_service_cost_correction))
	return process_allowlist(
		subcontracting_receipts,
		dry_run=bool(cint(dry_run)),
		confirm=confirm,
		include_service_cost_correction=flag,
		execute_riv=bool(cint(execute_riv)),
	)
