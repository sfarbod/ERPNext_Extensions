# Copyright (c) 2026, ERPNext Extensions contributors
"""Whitelisted API for Asset Purchase Receipt correction (5.7.18 local).

Mutations: POST only. Privileged roles only. Explicit PR allowlist required.
Never writes GL/SLE via SQL. Never creates compensating Journal Entries.
Never cancels/amends/reverses ACC-JV-1405-169046 (Finance-owned).
"""

from __future__ import annotations

import frappe
from frappe.utils import cint

from erpnext_extensions.iran_accounting.asset_purchase_receipt_correction import (
	API_VERSION,
	CONFIRM_APPLY,
	FINANCE_OWNED_JOURNAL_ENTRIES,
)
from erpnext_extensions.iran_accounting.asset_purchase_receipt_correction.service import (
	MAX_ALLOWLIST,
	discover_land_posted_purchase_receipts,
	fingerprint_pr,
	inspect_purchase_receipt,
	process_allowlist,
	require_privileged_user,
)


@frappe.whitelist(methods=["GET", "POST"])
def get_asset_pr_correction_api_status(company=None):
	"""Read-only capability probe."""
	require_privileged_user()
	return {
		"api_version": API_VERSION,
		"max_allowlist": MAX_ALLOWLIST,
		"confirm_apply": CONFIRM_APPLY,
		"company": company,
		"finance_owned_journal_entries_untouched": sorted(FINANCE_OWNED_JOURNAL_ENTRIES),
		"capabilities": [
			"discover_land_posted",
			"inspect",
			"dry_run",
			"apply_per_voucher",
		],
		"restrictions": [
			"no_sql_ledger_edits",
			"no_compensating_je_as_substitute",
			"no_blanket_land_to_computer",
			"per_row_account_resolution",
			"never_touch_ACC-JV-1405-169046",
			"dry_run_is_not_completed_correction",
		],
	}


@frappe.whitelist(methods=["GET", "POST"])
def discover_land_posted_asset_purchase_receipts(company=None):
	"""Read-only discovery of Land-posted Asset Purchase Receipts."""
	return discover_land_posted_purchase_receipts(company=company or None)


@frappe.whitelist(methods=["GET", "POST"])
def inspect_asset_purchase_receipt(purchase_receipt: str, company=None, account_overrides=None):
	"""Read-only per-PR inspection with proposed account mapping."""
	return inspect_purchase_receipt(
		purchase_receipt,
		company=company or None,
		account_overrides=account_overrides,
	)


@frappe.whitelist(methods=["GET", "POST"])
def get_asset_purchase_receipt_fingerprint(purchase_receipt: str) -> dict:
	"""Read-only fingerprint for before-state validation."""
	require_privileged_user()
	if not purchase_receipt:
		frappe.throw("purchase_receipt is required")
	return {
		"purchase_receipt": purchase_receipt,
		"fingerprint": fingerprint_pr(purchase_receipt),
	}


@frappe.whitelist(methods=["GET", "POST"])
def dry_run_asset_purchase_receipt_correction(
	purchase_receipts,
	fingerprints=None,
	account_overrides=None,
	company=None,
):
	"""Dry-run allowlisted Asset PR correction (no mutation). Not a completed correction."""
	return process_allowlist(
		purchase_receipts,
		dry_run=True,
		fingerprints=fingerprints,
		account_overrides=account_overrides,
		company=company or None,
	)


@frappe.whitelist(methods=["POST"])
def apply_asset_purchase_receipt_correction(
	purchase_receipts,
	dry_run=1,
	confirm=None,
	fingerprints=None,
	account_overrides=None,
	company=None,
):
	"""Allowlisted per-PR Asset account correction via source PRI + Transaction RIV.

	Parameters
	----------
	purchase_receipts:
	    Explicit list/JSON of Purchase Receipt names (max {max}).
	dry_run:
	    Default 1. Set 0 to mutate. Mutations require HTTP POST + confirm token.
	confirm:
	    Must equal ``{confirm}`` when dry_run=0.
	fingerprints:
	    Optional {{pr_name: sha256}} map; mismatch aborts that PR.
	account_overrides:
	    Optional {{purchase_receipt_item: target_account}} for explicit row targets.

	Never touches Finance-owned Journal Entry ACC-JV-1405-169046.
	""".format(
		max=MAX_ALLOWLIST,
		confirm=CONFIRM_APPLY,
	)
	return process_allowlist(
		purchase_receipts,
		dry_run=bool(cint(dry_run)),
		confirm=confirm,
		fingerprints=fingerprints,
		account_overrides=account_overrides,
		company=company or None,
	)
