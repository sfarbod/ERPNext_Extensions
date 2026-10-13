# Copyright (c) 2026, ERPNext Extensions contributors
"""Whitelisted API for secure Purchase Receipt #59476 billing refresh.

Mutations: POST only. Privileged roles only. Explicit PR allowlist required.
Client financial values are never authoritative.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint

from erpnext_extensions.iran_accounting.pr_billing_refresh.service import (
	MAX_ALLOWLIST,
	fingerprint_pr,
	refresh_allowlist,
	require_privileged_user,
)


@frappe.whitelist(methods=["GET", "POST"])
def get_purchase_receipt_billing_fingerprint(purchase_receipt: str) -> dict:
	"""Read-only fingerprint for before-state validation."""
	require_privileged_user()
	if not purchase_receipt:
		frappe.throw("purchase_receipt is required")
	pr = frappe.get_doc("Purchase Receipt", purchase_receipt)
	if pr.docstatus != 1:
		frappe.throw(f"{purchase_receipt} is not submitted")
	return {
		"purchase_receipt": pr.name,
		"fingerprint": fingerprint_pr(pr),
		"per_billed": pr.per_billed,
		"item_count": len(pr.items),
	}


@frappe.whitelist(methods=["GET", "POST"])
def dry_run_purchase_receipt_billing_refresh(purchase_receipts, fingerprints=None) -> dict:
	"""Dry-run allowlisted PR billing refresh (no DB mutation of diffs/SLE/GL)."""
	return refresh_allowlist(
		purchase_receipts,
		dry_run=True,
		fingerprints=fingerprints,
	)


@frappe.whitelist(methods=["POST"])
def refresh_purchase_receipt_billing(
	purchase_receipts,
	dry_run=1,
	fingerprints=None,
) -> dict:
	"""Allowlisted PR billing refresh via patched native update_billing_percentage.

	Parameters
	----------
	purchase_receipts:
	    Explicit list/JSON of Purchase Receipt names (max {max}).
	dry_run:
	    Default 1. Set 0 to mutate. Mutations require HTTP POST.
	fingerprints:
	    Optional {{pr_name: sha256}} map; mismatch aborts that PR.

	Security
	--------
	- Administrator or System Manager
	- Explicit allowlist only (no bulk scan endpoint)
	- Server recalculates expected diffs; client amounts are ignored
	- No arbitrary method execution; no direct SLE/GL writes
	- Creates RIVs only through native Core path; does not run RIV campaign
	""".format(
		max=MAX_ALLOWLIST
	)
	return refresh_allowlist(
		purchase_receipts,
		dry_run=bool(cint(dry_run)),
		fingerprints=fingerprints,
	)
