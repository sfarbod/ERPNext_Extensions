# Copyright (c) 2026, ERPNext Extensions contributors
"""Server-side Asset Purchase Receipt account correction.

Workflow per voucher
--------------------
1. Inspect PR item rows + linked Assets + current GL.
2. Resolve the correct fixed-asset account per row (never blanket Land→Computer).
3. Dry-run: report before/after mapping without mutation.
4. Apply (explicit confirm): ``db_set`` expense_account on PRI, then Transaction
   RIV with ``repost_only_accounting_ledgers=1``.
5. Verify GL: Land debit gone from the source PR; target FA debited; GRNI intact.

Safety
------
- Administrator / System Manager only.
- Explicit PR allowlist (no bulk auto-scan mutations).
- Per-PR isolation: one failure does not abort siblings.
- Idempotent: already-correct expense + Land-free GL → skipped.
- No SQL ledger edits; no compensating Journal Entries.
- Never cancel/amend/reverse ``ACC-JV-1405-169046`` (Finance-owned).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.iran_accounting.asset_purchase_receipt_correction import (
	API_VERSION,
	CONFIRM_APPLY,
	DEFAULT_COMPANY,
	FINANCE_OWNED_JOURNAL_ENTRIES,
	LAND_ACCOUNT_CODE_PREFIX,
)

MAX_ALLOWLIST = 50
PRIVILEGED_ROLES = ("Administrator", "System Manager")


class AssetPrCorrectionError(frappe.ValidationError):
	pass


def require_privileged_user() -> str:
	user = frappe.session.user
	roles = set(frappe.get_roles(user))
	if user == "Administrator" or "Administrator" in roles or "System Manager" in roles:
		return user
	frappe.throw(
		"Asset Purchase Receipt correction requires Administrator or System Manager",
		frappe.PermissionError,
	)


def require_post_for_mutation(dry_run: bool) -> None:
	if dry_run:
		return
	req = getattr(frappe.local, "request", None)
	method = (getattr(req, "method", None) or "POST").upper()
	if method != "POST":
		frappe.throw("Mutating Asset PR correction requires HTTP POST", frappe.PermissionError)


def _parse_allowlist(purchase_receipts) -> list[str]:
	if purchase_receipts is None:
		raise AssetPrCorrectionError("purchase_receipts allowlist is required")
	if isinstance(purchase_receipts, str):
		purchase_receipts = purchase_receipts.strip()
		if not purchase_receipts:
			raise AssetPrCorrectionError("purchase_receipts allowlist is required")
		try:
			purchase_receipts = json.loads(purchase_receipts)
		except json.JSONDecodeError:
			purchase_receipts = [purchase_receipts]
	if not isinstance(purchase_receipts, (list, tuple)):
		raise AssetPrCorrectionError("purchase_receipts must be a list of names")
	names: list[str] = []
	seen: set[str] = set()
	for raw in purchase_receipts:
		name = str(raw or "").strip()
		if not name or name in seen:
			continue
		seen.add(name)
		names.append(name)
	if not names:
		raise AssetPrCorrectionError("purchase_receipts allowlist is empty")
	if len(names) > MAX_ALLOWLIST:
		raise AssetPrCorrectionError(f"allowlist exceeds max of {MAX_ALLOWLIST}")
	return names


def _parse_overrides(account_overrides) -> dict[str, str]:
	"""Optional {purchase_receipt_item: target_account} map for explicit row targets."""
	if account_overrides in (None, "", {}):
		return {}
	if isinstance(account_overrides, str):
		account_overrides = json.loads(account_overrides)
	if not isinstance(account_overrides, dict):
		raise AssetPrCorrectionError("account_overrides must be a JSON object")
	return {str(k): str(v) for k, v in account_overrides.items() if k and v}


def land_account_for_company(company: str) -> str | None:
	rows = frappe.db.sql(
		"""
		SELECT name FROM `tabAccount`
		WHERE company=%s AND is_group=0 AND disabled=0
		  AND (account_number=%s OR name LIKE %s)
		ORDER BY name LIMIT 1
		""",
		(company, LAND_ACCOUNT_CODE_PREFIX, f"{LAND_ACCOUNT_CODE_PREFIX} %"),
	)
	return rows[0][0] if rows else None


def fa_account_for_category(asset_category: str | None, company: str) -> str | None:
	if not asset_category:
		return None
	return frappe.db.get_value(
		"Asset Category Account",
		{"parent": asset_category, "company_name": company},
		"fixed_asset_account",
	)


def fingerprint_pr(pr_name: str) -> str:
	"""Stable fingerprint of expense accounts + land GL debit for idempotency checks."""
	rows = frappe.db.sql(
		"""
		SELECT name, item_code, expense_account, asset_category, amount, is_fixed_asset
		FROM `tabPurchase Receipt Item`
		WHERE parent=%s ORDER BY idx, name
		""",
		pr_name,
		as_dict=True,
	)
	land = land_account_for_company(
		frappe.db.get_value("Purchase Receipt", pr_name, "company") or DEFAULT_COMPANY
	)
	land_debit = 0.0
	if land:
		land_debit = flt(
			frappe.db.sql(
				"""
				SELECT COALESCE(SUM(debit),0) FROM `tabGL Entry`
				WHERE voucher_type='Purchase Receipt' AND voucher_no=%s
				  AND account=%s AND is_cancelled=0
				""",
				(pr_name, land),
			)[0][0]
		)
	parts = [pr_name, f"land_debit={land_debit}"]
	for r in rows:
		parts.append(
			"|".join(
				[
					r.name,
					str(r.item_code or ""),
					str(cint(r.is_fixed_asset)),
					str(r.expense_account or ""),
					str(r.asset_category or ""),
					str(flt(r.amount)),
				]
			)
		)
	return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _linked_assets(pr_name: str, item_row: str, item_code: str) -> list[dict]:
	assets = frappe.db.sql(
		"""
		SELECT name, asset_name, asset_category, status, gross_purchase_amount,
		       purchase_receipt_item, item_code, docstatus
		FROM `tabAsset`
		WHERE purchase_receipt=%s AND docstatus<2
		  AND (purchase_receipt_item=%s OR item_code=%s)
		ORDER BY name
		""",
		(pr_name, item_row, item_code),
		as_dict=True,
	)
	exact = [a for a in assets if a.purchase_receipt_item == item_row]
	chosen = exact or assets
	return [
		{
			"name": a.name,
			"asset_name": a.asset_name,
			"asset_category": a.asset_category,
			"status": a.status,
			"gross_purchase_amount": flt(a.gross_purchase_amount),
			"purchase_receipt_item": a.purchase_receipt_item,
		}
		for a in chosen
	]


def resolve_target_account(
	*,
	company: str,
	land_account: str,
	pr_item_asset_category: str | None,
	item_code: str,
	assets: list[dict],
	override: str | None = None,
) -> tuple[str | None, str | None, str | None]:
	"""Return (to_account, mapping_source, blocked_reason)."""
	if override:
		if override == land_account:
			return None, None, "override_targets_land_forbidden"
		if not frappe.db.exists("Account", override):
			return None, None, "override_account_missing"
		return override, "explicit_override", None

	# Genuine Land category must never be auto-remapped.
	if pr_item_asset_category == "زمین":
		return None, None, "genuine_land_category_no_change"

	intended = fa_account_for_category(pr_item_asset_category, company)
	if intended and intended != land_account:
		return intended, "pr_item_asset_category", None

	for a in assets:
		fa = fa_account_for_category(a.get("asset_category"), company)
		if fa and fa != land_account:
			return fa, f"asset:{a['name']}:{a.get('asset_category')}", None

	item_cat = frappe.db.get_value("Item", item_code, "asset_category")
	if item_cat == "زمین":
		return None, None, "genuine_land_category_no_change"
	fa = fa_account_for_category(item_cat, company)
	if fa and fa != land_account:
		return fa, f"item:{item_code}:{item_cat}", None

	if pr_item_asset_category in (None, "NA") or intended == land_account:
		return None, None, "category_maps_to_land_needs_business_rule"
	return None, None, "no_safe_target_account"


def _pr_gl_summary(pr_name: str) -> list[dict]:
	rows = frappe.db.sql(
		"""
		SELECT account, SUM(debit) debit, SUM(credit) credit
		FROM `tabGL Entry`
		WHERE voucher_type='Purchase Receipt' AND voucher_no=%s AND is_cancelled=0
		GROUP BY account
		ORDER BY account
		""",
		pr_name,
		as_dict=True,
	)
	return [
		{"account": r.account, "debit": flt(r.debit), "credit": flt(r.credit)} for r in rows
	]


def _linked_purchase_invoices(pr_name: str) -> list[str]:
	return frappe.db.sql(
		"""
		SELECT DISTINCT parent FROM `tabPurchase Invoice Item`
		WHERE purchase_receipt=%s AND docstatus=1
		ORDER BY parent
		""",
		pr_name,
		pluck=True,
	)


def inspect_purchase_receipt(
	purchase_receipt: str,
	*,
	company: str | None = None,
	account_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
	"""Read-only inspection + proposed per-row account mapping for one PR."""
	require_privileged_user()
	if not purchase_receipt:
		raise AssetPrCorrectionError("purchase_receipt is required")
	if not frappe.db.exists("Purchase Receipt", purchase_receipt):
		raise AssetPrCorrectionError(f"Purchase Receipt {purchase_receipt} not found")

	pr = frappe.get_doc("Purchase Receipt", purchase_receipt)
	company = company or pr.company or DEFAULT_COMPANY
	if pr.company != company:
		raise AssetPrCorrectionError(
			f"{purchase_receipt} belongs to {pr.company}, expected {company}"
		)
	if pr.docstatus != 1:
		raise AssetPrCorrectionError(f"{purchase_receipt} is not submitted")

	land = land_account_for_company(company)
	if not land:
		raise AssetPrCorrectionError(f"Land account ({LAND_ACCOUNT_CODE_PREFIX}) not found")

	overrides = account_overrides or {}
	row_plans: list[dict] = []
	for item in pr.items:
		if not cint(item.is_fixed_asset):
			continue
		assets = _linked_assets(pr.name, item.name, item.item_code)
		override = overrides.get(item.name)
		to_account, source, blocked = resolve_target_account(
			company=company,
			land_account=land,
			pr_item_asset_category=item.asset_category,
			item_code=item.item_code,
			assets=assets,
			override=override,
		)
		from_account = item.expense_account
		needs = bool(
			from_account == land
			or (to_account and from_account and to_account != from_account and from_account == land)
		)
		# Also allow correcting non-land wrong FA when override / asset category demands it
		# and current expense equals land only for this campaign's primary scope.
		actionable = bool(needs and to_account and not blocked and to_account != from_account)
		already_correct = bool(to_account and from_account == to_account)
		row_plans.append(
			{
				"item_row": item.name,
				"idx": item.idx,
				"item_code": item.item_code,
				"item_name": item.item_name,
				"qty": flt(item.qty),
				"amount": flt(item.amount),
				"is_fixed_asset": 1,
				"pr_item_asset_category": item.asset_category,
				"from_account": from_account,
				"to_account": to_account,
				"mapping_source": source,
				"blocked_reason": blocked,
				"assets": assets,
				"needs_correction": actionable,
				"already_correct": already_correct,
				"idempotent_skip": already_correct,
			}
		)

	gl = _pr_gl_summary(pr.name)
	land_debit = sum(r["debit"] for r in gl if r["account"] == land)
	land_credit = sum(r["credit"] for r in gl if r["account"] == land)
	sle_count = cint(
		frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabStock Ledger Entry`
			WHERE voucher_type='Purchase Receipt' AND voucher_no=%s AND is_cancelled=0
			""",
			pr.name,
		)[0][0]
	)

	actionable_rows = [r for r in row_plans if r["needs_correction"]]
	blocked_rows = [r for r in row_plans if r["blocked_reason"] and r["from_account"] == land]
	expected_gl_changes = [
		{
			"item_row": r["item_row"],
			"item_code": r["item_code"],
			"amount": r["amount"],
			"remove_debit_from": r["from_account"],
			"add_debit_to": r["to_account"],
		}
		for r in actionable_rows
	]

	return {
		"api_version": API_VERSION,
		"purchase_receipt": pr.name,
		"company": company,
		"posting_date": str(pr.posting_date),
		"currency": pr.currency,
		"conversion_rate": flt(pr.conversion_rate),
		"land_account": land,
		"fingerprint": fingerprint_pr(pr.name),
		"sle_count": sle_count,
		"asset_pr_no_sle_expected": sle_count == 0,
		"linked_purchase_invoices": _linked_purchase_invoices(pr.name),
		"gl_before": gl,
		"land_debit_before": land_debit,
		"land_credit_before": land_credit,
		"rows": row_plans,
		"actionable_count": len(actionable_rows),
		"blocked_count": len(blocked_rows),
		"expected_gl_changes": expected_gl_changes,
		"status": (
			"clean"
			if land_debit == 0 and not actionable_rows
			else "needs_correction"
			if actionable_rows
			else "blocked_or_review"
		),
	}


def discover_land_posted_purchase_receipts(company: str | None = None) -> dict[str, Any]:
	"""Read-only discovery of PRs with live Land debit GL and/or Land expense_account."""
	require_privileged_user()
	company = company or DEFAULT_COMPANY
	land = land_account_for_company(company)
	if not land:
		raise AssetPrCorrectionError(f"Land account ({LAND_ACCOUNT_CODE_PREFIX}) not found")

	gl_prs = frappe.db.sql(
		"""
		SELECT gle.voucher_no AS purchase_receipt,
		       SUM(gle.debit) AS land_debit,
		       MIN(gle.posting_date) AS posting_date
		FROM `tabGL Entry` gle
		WHERE gle.company=%s AND gle.account=%s AND gle.is_cancelled=0
		  AND gle.voucher_type='Purchase Receipt' AND gle.debit>0
		GROUP BY gle.voucher_no
		ORDER BY posting_date, gle.voucher_no
		""",
		(company, land),
		as_dict=True,
	)
	expense_prs = frappe.db.sql(
		"""
		SELECT DISTINCT pri.parent AS purchase_receipt
		FROM `tabPurchase Receipt Item` pri
		JOIN `tabPurchase Receipt` pr ON pr.name=pri.parent
		WHERE pr.docstatus=1 AND pr.company=%s
		  AND pri.is_fixed_asset=1 AND pri.expense_account=%s
		ORDER BY pri.parent
		""",
		(company, land),
		pluck=True,
	)
	names = sorted({*(r.purchase_receipt for r in gl_prs), *expense_prs})
	inspections = []
	for name in names:
		try:
			inspections.append(inspect_purchase_receipt(name, company=company))
		except Exception as exc:
			inspections.append(
				{
					"purchase_receipt": name,
					"status": "inspect_error",
					"error": str(exc),
				}
			)

	return {
		"api_version": API_VERSION,
		"company": company,
		"land_account": land,
		"purchase_receipts": names,
		"count": len(names),
		"gl_land_debit_total": sum(flt(r.land_debit) for r in gl_prs),
		"inspections": inspections,
		"finance_owned_journal_entries_untouched": sorted(FINANCE_OWNED_JOURNAL_ENTRIES),
		"execution_status": "pending_per_voucher_apply",
		"note": (
			"Discovery/dry-run only. Do not treat this as completed correction. "
			"ACC-JV-1405-169046 is Finance-owned and must not be modified by this workflow."
		),
		"production_note": (
			"Production site must be targeted explicitly on the Production bench. "
			"This discovery never mutates data."
		),
	}


def _create_and_run_accounting_riv(pr_name: str, company: str) -> dict[str, Any]:
	"""Create Transaction RIV with accounting-only repost and execute synchronously.

	Uses Frappe's job executor so ``before_job`` / RIV bootstrap fires. Calling
	module-level ``repost`` as a DocType method is a no-op and leaves status Queued.
	"""
	pr = frappe.get_doc("Purchase Receipt", pr_name)
	riv = frappe.new_doc("Repost Item Valuation")
	riv.based_on = "Transaction"
	riv.voucher_type = "Purchase Receipt"
	riv.voucher_no = pr_name
	riv.company = company
	riv.posting_date = pr.posting_date
	riv.posting_time = pr.posting_time
	riv.allow_negative_stock = 1
	riv.repost_only_accounting_ledgers = 1
	riv.flags.ignore_permissions = True
	riv.insert()
	riv.submit()
	frappe.db.commit()

	from frappe.utils.background_jobs import execute_job

	execute_job(
		site=frappe.local.site,
		method="erpnext.stock.doctype.repost_item_valuation.repost_item_valuation.execute_reposting_entry",
		event=None,
		job_name=f"asset_pr_accounting_riv:{riv.name}",
		kwargs={"name": riv.name},
		user=frappe.session.user,
		is_async=False,
	)

	status, err = frappe.db.get_value(
		"Repost Item Valuation", riv.name, ["status", "error_log"]
	) or (None, None)
	return {
		"riv": riv.name,
		"status": status,
		"error": (err or "")[:2000],
	}


def apply_purchase_receipt_correction(
	purchase_receipt: str,
	*,
	dry_run: bool = True,
	confirm: str | None = None,
	fingerprint: str | None = None,
	account_overrides=None,
	company: str | None = None,
) -> dict[str, Any]:
	"""Dry-run or apply correction for a single Purchase Receipt."""
	require_privileged_user()
	require_post_for_mutation(dry_run)
	# Hard denylist — never route Finance-owned JEs through this workflow.
	if purchase_receipt in FINANCE_OWNED_JOURNAL_ENTRIES:
		raise AssetPrCorrectionError(
			f"{purchase_receipt} is Finance-owned and cannot enter Asset PR correction"
		)
	for _je in FINANCE_OWNED_JOURNAL_ENTRIES:
		# Read-only existence check keeps the denylist visible at every apply entry.
		frappe.db.exists("Journal Entry", _je)
	# Cohort A campaign allowlist (reject outsiders when map is loaded).
	try:
		from erpnext_extensions.iran_accounting.financial_correction_campaign import (
			ASSET_PR_APPROVED_ACCOUNT_PREFIX,
		)

		if purchase_receipt not in ASSET_PR_APPROVED_ACCOUNT_PREFIX:
			raise AssetPrCorrectionError(
				f"{purchase_receipt} is outside the Cohort A Asset PR allowlist"
			)
	except ImportError:
		pass
	overrides = _parse_overrides(account_overrides)
	before = inspect_purchase_receipt(
		purchase_receipt, company=company, account_overrides=overrides
	)

	if fingerprint and fingerprint != before["fingerprint"]:
		raise AssetPrCorrectionError(
			f"Fingerprint mismatch for {purchase_receipt}: "
			"document changed since dry-run; re-run dry_run"
		)

	result = {
		"api_version": API_VERSION,
		"purchase_receipt": purchase_receipt,
		"dry_run": bool(dry_run),
		"before": before,
		"applied_rows": [],
		"skipped_rows": [],
		"riv": None,
		"after": None,
		"success": False,
		"message": "",
		"executed_at": str(now_datetime()),
	}

	actionable = [r for r in before["rows"] if r["needs_correction"]]
	land_debit_before = flt(before.get("land_debit_before") or 0)
	# Source already corrected but Land still on GL → accounting RIV-only recovery.
	riv_only = (not actionable) and land_debit_before > 0

	if not actionable and not riv_only:
		result["success"] = True
		result["message"] = "idempotent_skip: no actionable Land expense rows and Land GL clear"
		result["after"] = before
		return result

	if dry_run:
		result["success"] = True
		result["message"] = "dry_run_ok_riv_only" if riv_only else "dry_run_ok"
		result["applied_rows"] = actionable
		result["riv_only"] = riv_only
		return result

	if confirm != CONFIRM_APPLY:
		raise AssetPrCorrectionError(
			f"confirm must equal {CONFIRM_APPLY!r} for mutation"
		)

	# Mutate source PRI expense_account only — never GL/SLE SQL.
	for row in actionable:
		frappe.db.set_value(
			"Purchase Receipt Item",
			row["item_row"],
			"expense_account",
			row["to_account"],
			update_modified=False,
		)
		result["applied_rows"].append(
			{
				"item_row": row["item_row"],
				"item_code": row["item_code"],
				"from_account": row["from_account"],
				"to_account": row["to_account"],
				"amount": row["amount"],
				"mapping_source": row["mapping_source"],
			}
		)
	if actionable:
		frappe.db.commit()

	riv_info = _create_and_run_accounting_riv(purchase_receipt, before["company"])
	result["riv"] = riv_info
	result["riv_only"] = riv_only
	frappe.db.commit()

	after = inspect_purchase_receipt(
		purchase_receipt, company=company, account_overrides=overrides
	)
	result["after"] = after
	land = before["land_account"]
	land_gone = flt(after.get("land_debit_before") or 0) == 0
	riv_ok = (riv_info.get("status") or "").lower() in ("completed", "skipped")
	result["success"] = bool(riv_ok and land_gone and after["actionable_count"] == 0)
	result["message"] = (
		("riv_only_corrected" if riv_only else "corrected")
		if result["success"]
		else f"partial_or_failed riv={riv_info.get('status')} land_debit={after.get('land_debit_before')}"
	)
	result["gl_after"] = after.get("gl_before")
	result["reconciliation"] = {
		"land_debit_cleared": land_gone,
		"land_account": land,
		"actionable_remaining": after["actionable_count"],
		"riv_status": riv_info.get("status"),
		"riv_only": riv_only,
		"gross_amounts_unchanged": all(
			flt(a["amount"]) == flt(b["amount"])
			for a, b in zip(
				[r for r in after["rows"] if r["item_row"] in {x["item_row"] for x in actionable}],
				actionable,
				strict=False,
			)
		)
		if actionable
		else True,
	}
	return result


def process_allowlist(
	purchase_receipts,
	*,
	dry_run: bool = True,
	confirm: str | None = None,
	fingerprints=None,
	account_overrides=None,
	company: str | None = None,
) -> dict[str, Any]:
	"""Process an explicit PR allowlist with per-voucher failure isolation."""
	require_privileged_user()
	require_post_for_mutation(dry_run)
	names = _parse_allowlist(purchase_receipts)
	fp_map: dict[str, str] = {}
	if fingerprints not in (None, "", {}):
		if isinstance(fingerprints, str):
			fingerprints = json.loads(fingerprints)
		if not isinstance(fingerprints, dict):
			raise AssetPrCorrectionError("fingerprints must be a JSON object")
		fp_map = {str(k): str(v) for k, v in fingerprints.items()}

	results = []
	for name in names:
		try:
			results.append(
				apply_purchase_receipt_correction(
					name,
					dry_run=dry_run,
					confirm=confirm,
					fingerprint=fp_map.get(name),
					account_overrides=account_overrides,
					company=company,
				)
			)
		except Exception as exc:
			frappe.log_error(
				title=f"Asset PR correction failed: {name}",
				message=frappe.get_traceback(),
			)
			results.append(
				{
					"api_version": API_VERSION,
					"purchase_receipt": name,
					"dry_run": bool(dry_run),
					"success": False,
					"message": str(exc),
					"error_type": exc.__class__.__name__,
				}
			)

	ok = [r for r in results if r.get("success")]
	failed = [r for r in results if not r.get("success")]
	return {
		"api_version": API_VERSION,
		"dry_run": bool(dry_run),
		"company": company or DEFAULT_COMPANY,
		"count": len(results),
		"success_count": len(ok),
		"failed_count": len(failed),
		"results": results,
		"execution_status": "dry_run_only" if dry_run else "apply_attempted",
		"finance_owned_journal_entries_untouched": sorted(FINANCE_OWNED_JOURNAL_ENTRIES),
		"note": (
			"Dry-run success is not completed correction. "
			"ACC-JV-1405-169046 remains Finance-owned and is never modified here."
		),
	}
