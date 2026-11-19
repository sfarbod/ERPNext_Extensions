# Copyright (c) 2026, ERPNext Extensions contributors
"""Whitelisted API for Subcontracting Receipt service-cost correction.

Mutations require HTTP POST, Administrator/System Manager, dry_run=0, and
confirm=APPLY_SCR_SERVICE_COST_CORRECTION. RIV is never implied by correction;
pass enqueue_riv=1 explicitly for a separate Transaction RIV on the SCR only.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint

from erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost import (
	API_VERSION,
	CONFIRM_APPLY,
	MAX_ALLOWLIST,
)
from erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.service import (
	discover_eligible_subcontracting_receipts,
	fingerprint_scr,
	inspect_subcontracting_receipt,
	process_allowlist,
	require_privileged_user,
)


@frappe.whitelist(methods=["GET", "POST"])
def get_scr_service_cost_correction_api_status(company=None):
	"""Read-only capability probe."""
	require_privileged_user()
	return {
		"api_version": API_VERSION,
		"max_allowlist": MAX_ALLOWLIST,
		"confirm_apply": CONFIRM_APPLY,
		"company": company,
		"capabilities": [
			"discover_eligible",
			"inspect",
			"dry_run",
			"apply_allowlist",
			"optional_enqueue_transaction_riv",
		],
		"restrictions": [
			"never_use_last_purchase_rate_as_service_amount",
			"preserve_rm_and_additional_cost",
			"no_sql_sle_gl_bin_edits",
			"no_automatic_eto_transfer_manufacture_fix",
			"dry_run_default",
			"explicit_confirm_token_required_to_mutate",
		],
		"related_investigation": "docs/investigations/ETO_SCR_REPOST_PROPAGATION.md",
	}


@frappe.whitelist(methods=["GET", "POST"])
def discover_eligible_scr_service_cost_corrections(
	company=None,
	purchase_order=None,
	subcontracting_order=None,
	limit=200,
):
	"""Read-only discovery of submitted SCRs that need service-cost correction."""
	return discover_eligible_subcontracting_receipts(
		company=company or None,
		purchase_order=purchase_order or None,
		subcontracting_order=subcontracting_order or None,
		limit=limit,
	)


@frappe.whitelist(methods=["GET", "POST"])
def inspect_scr_service_cost_correction(subcontracting_receipt: str, company=None):
	"""Read-only before/after proposal for one SCR."""
	return inspect_subcontracting_receipt(
		subcontracting_receipt, company=company or None
	)


@frappe.whitelist(methods=["GET", "POST"])
def get_scr_service_cost_fingerprint(subcontracting_receipt: str) -> dict:
	"""Read-only fingerprint for idempotency / before-state checks."""
	require_privileged_user()
	if not subcontracting_receipt:
		frappe.throw("subcontracting_receipt is required")
	return {
		"subcontracting_receipt": subcontracting_receipt,
		"fingerprint": fingerprint_scr(subcontracting_receipt),
	}


@frappe.whitelist(methods=["GET", "POST"])
def dry_run_scr_service_cost_correction(
	subcontracting_receipts,
	fingerprints=None,
	company=None,
):
	"""Dry-run allowlisted SCR service-cost correction (no mutation)."""
	return process_allowlist(
		subcontracting_receipts,
		dry_run=True,
		fingerprints=fingerprints,
		company=company or None,
		enqueue_riv=0,
	)


@frappe.whitelist(methods=["POST"])
def correct_subcontracting_receipt_service_cost(
	subcontracting_receipts,
	dry_run=1,
	confirm=None,
	fingerprints=None,
	company=None,
	enqueue_riv=0,
):
	"""Correct allowlisted SCR service costs from linked PO service amounts.

	Parameters
	----------
	subcontracting_receipts:
	    Explicit list/JSON of Subcontracting Receipt names.
	dry_run:
	    Default 1. Set 0 to mutate. Mutations require HTTP POST + confirm token.
	confirm:
	    Must equal ``APPLY_SCR_SERVICE_COST_CORRECTION`` when dry_run=0.
	fingerprints:
	    Optional ``{scr_name: fingerprint}`` map; mismatch rejects that SCR.
	company:
	    Optional company filter / validation.
	enqueue_riv:
	    If 1 on apply, enqueue Transaction RIV for the SCR only. Does not fix
	    ETO Transfer/Manufacture propagation by itself.
	"""
	return process_allowlist(
		subcontracting_receipts,
		dry_run=cint(dry_run),
		confirm=confirm,
		fingerprints=fingerprints,
		company=company or None,
		enqueue_riv=cint(enqueue_riv),
	)
