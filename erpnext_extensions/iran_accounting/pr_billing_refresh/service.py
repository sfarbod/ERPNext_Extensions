# Copyright (c) 2026, ERPNext Extensions contributors
"""Server-side Purchase Receipt billing refresh (#59476).

Mutations go only through patched native ``update_billing_percentage`` with
``adjust_incoming_rate=True``. No direct SLE/GL or PR item field writes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime

MAX_ALLOWLIST = 50
TOL = 1.0

PRIVILEGED_ROLES = ("Administrator", "System Manager")


class BillingRefreshError(frappe.ValidationError):
	pass


def require_privileged_user() -> str:
	user = frappe.session.user
	roles = set(frappe.get_roles(user))
	if user == "Administrator" or "Administrator" in roles or "System Manager" in roles:
		return user
	frappe.throw(
		"Purchase Receipt billing refresh requires Administrator or System Manager",
		frappe.PermissionError,
	)


def require_post_for_mutation(dry_run: bool) -> None:
	if dry_run:
		return
	req = getattr(frappe.local, "request", None)
	method = (getattr(req, "method", None) or "POST").upper()
	if method != "POST":
		frappe.throw("Mutating billing refresh requires HTTP POST", frappe.PermissionError)


def _parse_allowlist(purchase_receipts) -> list[str]:
	if purchase_receipts is None:
		raise BillingRefreshError("purchase_receipts allowlist is required")
	if isinstance(purchase_receipts, str):
		purchase_receipts = purchase_receipts.strip()
		if not purchase_receipts:
			raise BillingRefreshError("purchase_receipts allowlist is required")
		try:
			purchase_receipts = json.loads(purchase_receipts)
		except json.JSONDecodeError:
			purchase_receipts = [purchase_receipts]
	if not isinstance(purchase_receipts, (list, tuple)):
		raise BillingRefreshError("purchase_receipts must be a list of names")
	names = []
	seen = set()
	for raw in purchase_receipts:
		name = str(raw or "").strip()
		if not name:
			continue
		if name in seen:
			continue
		seen.add(name)
		names.append(name)
	if not names:
		raise BillingRefreshError("purchase_receipts allowlist is empty")
	if len(names) > MAX_ALLOWLIST:
		raise BillingRefreshError(f"allowlist exceeds max of {MAX_ALLOWLIST}")
	return names


def _parse_fingerprints(fingerprints) -> dict[str, str]:
	if fingerprints in (None, "", {}):
		return {}
	if isinstance(fingerprints, str):
		fingerprints = json.loads(fingerprints)
	if not isinstance(fingerprints, dict):
		raise BillingRefreshError("fingerprints must be a JSON object of {pr_name: sha256}")
	return {str(k): str(v) for k, v in fingerprints.items()}


def fingerprint_pr(pr) -> str:
	parts = [
		pr.name,
		str(flt(pr.per_billed)),
		str(flt(pr.discount_amount)),
		str(flt(pr.additional_discount_percentage)),
	]
	for item in pr.items:
		parts.append(
			"|".join(
				[
					item.name,
					str(flt(item.qty)),
					str(flt(item.rate)),
					str(flt(item.base_net_amount)),
					str(flt(item.billed_amt)),
					str(flt(item.amount_difference_with_purchase_invoice)),
					str(flt(item.item_tax_amount)),
					str(flt(item.landed_cost_voucher_amount)),
				]
			)
		)
	return hashlib.sha256("||".join(parts).encode()).hexdigest()


def _ensure_compat_patch() -> dict:
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as bootstrap_apply
	from erpnext_extensions.iran_accounting.integration.pr_pi_landed_cost_compat import (
		install_pr_pi_landed_cost_compat_patch,
	)

	bootstrap_apply()
	info = install_pr_pi_landed_cost_compat_patch()
	import erpnext.stock.doctype.purchase_receipt.purchase_receipt as pr_mod

	fn = pr_mod.update_billing_percentage
	# When Core already has #59476, compat wrap is skipped; native fn is still correct.
	marker = getattr(fn, "_iran_pr_pi_landed_cost_59476", None)
	return {
		"patch_info": info,
		"marker": bool(marker),
		"callable": fn,
		"module": getattr(fn, "__module__", None),
	}


def _capture_items(pr) -> list[dict]:
	rows = []
	for item in pr.items:
		sq = flt(item.stock_qty) or flt(item.qty) or 1.0
		diff = flt(item.amount_difference_with_purchase_invoice)
		implied_vr = (
			flt(item.base_net_amount)
			+ diff
			+ flt(item.item_tax_amount)
			+ flt(item.landed_cost_voucher_amount)
		) / sq
		rows.append(
			{
				"name": item.name,
				"item_code": item.item_code,
				"qty": flt(item.qty),
				"rate": flt(item.rate),
				"base_net_amount": flt(item.base_net_amount),
				"billed_amt": flt(item.billed_amt),
				"amount_difference_with_purchase_invoice": diff,
				"valuation_rate": flt(getattr(item, "valuation_rate", 0) or 0),
				"implied_valuation_rate": implied_vr,
				"warehouse": item.warehouse,
			}
		)
	return rows


def _server_expected_diffs(pr) -> list[dict]:
	"""Recalculate expected #59476 amount_difference server-side (never trust client)."""
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import (
		get_billed_qty_amount_against_purchase_order,
		get_billed_qty_amount_against_purchase_receipt,
	)
	from erpnext_extensions.iran_accounting.integration.pr_pi_landed_cost_compat import (
		_compute_adjusted_amt_59476,
	)

	billed_qty_amt = get_billed_qty_amount_against_purchase_receipt(pr)
	po_pool = {
		k: {"qty": flt(v.get("qty")), "amount": flt(v.get("amount"))}
		for k, v in get_billed_qty_amount_against_purchase_order(pr).items()
	}
	out = []
	for item in pr.items:
		precision = item.precision("amount") or 2
		bill_qty = None
		billed_amt = flt(item.billed_amt)
		if billed_qty_amt.get(item.name):
			bill_qty = flt(billed_qty_amt[item.name].get("qty"))
			billed_amt = flt(billed_qty_amt[item.name].get("amount"))
		elif item.purchase_order_item and po_pool.get(item.purchase_order_item):
			pool = po_pool[item.purchase_order_item]
			bill_qty = flt(item.qty) if flt(item.qty) < flt(pool["qty"]) else flt(pool["qty"])
			total = flt(pool["qty"])
			billed_amt = flt(pool["amount"] * (bill_qty / total)) if total else 0.0
			pool["qty"] -= bill_qty
			pool["amount"] -= billed_amt
		exp = (
			_compute_adjusted_amt_59476(
				billed_amt=billed_amt,
				qty=bill_qty,
				item_qty=item.qty,
				base_net_amount=item.base_net_amount,
				precision=precision,
			)
			if bill_qty
			else 0.0
		)
		cur = flt(item.amount_difference_with_purchase_invoice)
		out.append(
			{
				"name": item.name,
				"item_code": item.item_code,
				"current_diff": cur,
				"expected_diff": exp,
				"delta": exp - cur,
				"bill_qty": bill_qty,
				"bill_amt_used": billed_amt,
			}
		)
	return out


def _list_rivs_for_items(item_codes: list[str], created_after: str) -> list[dict]:
	if not item_codes:
		return []
	return frappe.get_all(
		"Repost Item Valuation",
		filters={
			"item_code": ("in", item_codes),
			"creation": (">=", created_after),
		},
		fields=["name", "status", "item_code", "warehouse", "creation", "error_log"],
		order_by="creation asc",
	)


def _validate_pr(pr_name: str):
	if not frappe.db.exists("Purchase Receipt", pr_name):
		raise BillingRefreshError(f"Purchase Receipt not found: {pr_name}")
	meta = frappe.db.get_value(
		"Purchase Receipt",
		pr_name,
		["name", "docstatus", "is_return", "status", "company"],
		as_dict=True,
	)
	if cint(meta.docstatus) != 1:
		raise BillingRefreshError(f"{pr_name} is not submitted (docstatus={meta.docstatus})")
	return meta


def _audit(event: str, payload: dict) -> None:
	frappe.logger("pr_billing_refresh").info({"event": event, **payload})
	# Durable trail without a new DocType
	try:
		frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Info",
				"reference_doctype": "Purchase Receipt",
				"reference_name": payload.get("purchase_receipt") or "Billing Refresh",
				"content": f"[pr_billing_refresh] {event}: {json.dumps(payload, default=str)[:1800]}",
			}
		).insert(ignore_permissions=True)
	except Exception:
		pass


def refresh_one(
	pr_name: str,
	*,
	dry_run: bool = True,
	expected_fingerprint: str | None = None,
) -> dict[str, Any]:
	"""Refresh one allowlisted PR via native patched billing update."""
	_validate_pr(pr_name)
	patch = _ensure_compat_patch()
	fn = patch["callable"]

	pr = frappe.get_doc("Purchase Receipt", pr_name)
	fp = fingerprint_pr(pr)
	if expected_fingerprint and expected_fingerprint != fp:
		return {
			"purchase_receipt": pr_name,
			"status": "fingerprint_mismatch",
			"dry_run": dry_run,
			"expected_fingerprint": expected_fingerprint,
			"actual_fingerprint": fp,
		}

	before_items = _capture_items(pr)
	expected = _server_expected_diffs(pr)
	expected_delta = sum(r["delta"] for r in expected)
	already_ok = abs(expected_delta) <= TOL

	base = {
		"purchase_receipt": pr_name,
		"dry_run": dry_run,
		"fingerprint": fp,
		"patch_marker": patch["marker"],
		"patch_info": patch["patch_info"],
		"before_items": before_items,
		"server_expected_items": expected,
		"server_expected_delta": expected_delta,
		"already_correct": already_ok,
	}

	if dry_run:
		_audit("dry_run", {"purchase_receipt": pr_name, "expected_delta": expected_delta, "user": frappe.session.user})
		return {**base, "status": "dry_run"}

	if already_ok:
		_audit("idempotent_skip", {"purchase_receipt": pr_name, "user": frappe.session.user})
		return {**base, "status": "idempotent_skip", "after_items": before_items, "rivs_created": []}

	started = str(now_datetime())
	item_codes = [i.item_code for i in pr.items]

	# Native path only — creates RIVs through Core adjust_incoming_rate_for_pr
	fn(pr, update_modified=True, adjust_incoming_rate=True)

	pr_after = frappe.get_doc("Purchase Receipt", pr_name)
	after_items = _capture_items(pr_after)
	rivs = _list_rivs_for_items(item_codes, started)
	after_fp = fingerprint_pr(pr_after)

	result = {
		**base,
		"status": "repaired",
		"after_items": after_items,
		"fingerprint_after": after_fp,
		"rivs_created": rivs,
		"repaired_at": started,
		"user": frappe.session.user,
	}
	_audit(
		"repaired",
		{
			"purchase_receipt": pr_name,
			"expected_delta": expected_delta,
			"fingerprint_before": fp,
			"fingerprint_after": after_fp,
			"riv_count": len(rivs),
			"user": frappe.session.user,
		},
	)
	return result


def refresh_allowlist(
	purchase_receipts,
	*,
	dry_run: bool = True,
	fingerprints=None,
) -> dict[str, Any]:
	"""Refresh an explicit allowlist of Purchase Receipts.

	Client-supplied financial expectations are ignored; all amounts are
	recalculated server-side.
	"""
	require_privileged_user()
	dry_run = bool(cint(dry_run)) if not isinstance(dry_run, bool) else dry_run
	require_post_for_mutation(dry_run)

	names = _parse_allowlist(purchase_receipts)
	fps = _parse_fingerprints(fingerprints)

	buying = frappe.get_single("Buying Settings")
	if not cint(buying.get("set_landed_cost_based_on_purchase_invoice_rate")):
		raise BillingRefreshError(
			"Buying Settings.set_landed_cost_based_on_purchase_invoice_rate must be enabled"
		)

	results = []
	# One transaction per PR for isolation. Later failures do not roll back
	# earlier committed repairs — those successes are reported explicitly.
	for name in names:
		try:
			if dry_run:
				results.append(refresh_one(name, dry_run=True, expected_fingerprint=fps.get(name)))
			else:
				res = refresh_one(name, dry_run=False, expected_fingerprint=fps.get(name))
				# Commit only when a mutating outcome actually wrote (or no-op skip).
				if res.get("status") in {"repaired", "idempotent_skip"}:
					frappe.db.commit()
				results.append(res)
		except Exception as e:
			frappe.db.rollback()
			err = {
				"purchase_receipt": name,
				"status": "failed",
				"error": str(e),
				"error_type": e.__class__.__name__,
			}
			_audit("failed", err)
			results.append(err)

	summary = {
		"dry_run": dry_run,
		"requested": len(names),
		"statuses": {},
		"server_expected_delta_total": 0.0,
		"riv_total": 0,
		"repaired_prs": [],
		"failed_prs": [],
		"fingerprint_mismatch_prs": [],
		"idempotent_skip_prs": [],
		"partial": False,
	}
	for r in results:
		st = r.get("status") or "unknown"
		pr_name = r.get("purchase_receipt")
		summary["statuses"][st] = summary["statuses"].get(st, 0) + 1
		summary["server_expected_delta_total"] += flt(r.get("server_expected_delta"))
		summary["riv_total"] += len(r.get("rivs_created") or [])
		if st == "repaired":
			summary["repaired_prs"].append(pr_name)
		elif st == "failed":
			summary["failed_prs"].append(pr_name)
		elif st == "fingerprint_mismatch":
			summary["fingerprint_mismatch_prs"].append(pr_name)
		elif st == "idempotent_skip":
			summary["idempotent_skip_prs"].append(pr_name)

	summary["partial"] = bool(summary["repaired_prs"]) and bool(
		summary["failed_prs"] or summary["fingerprint_mismatch_prs"]
	)
	ok = all(r.get("status") in {"dry_run", "repaired", "idempotent_skip"} for r in results)

	return {
		"ok": ok,
		"summary": summary,
		"results": results,
		"note": (
			"RIV documents are created by native adjust_incoming_rate_for_pr. "
			"This API does not execute the RIV campaign; hand off Queued RIVs to the operator. "
			"If summary.partial is true, some PRs were repaired and committed before a later "
			"failure — inspect repaired_prs / failed_prs before retrying."
		),
	}
