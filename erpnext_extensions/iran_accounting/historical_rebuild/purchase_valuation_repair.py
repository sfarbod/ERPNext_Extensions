# Copyright (c) 2026, ERPNext Extensions contributors
"""Minimal HRC repair for purchase valuation Categories A/B (v5.6.25).

Flow
----
dry_run → eligibility → (optional) PR billing/valuation refresh → RIV via
supported job path → verify SLE/GL/GRNI.

Does not write SLE/GL directly. Mutations go through:
- ``pr_billing_refresh.refresh_one`` (native ``update_billing_percentage`` + adjust)
- Core ``Repost Item Valuation`` / ``execute_reposting_entry``

Default is dry-run. Explicit ``execute=True`` required to mutate.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_detection import (
	dry_run_purchase_valuation_scan,
	expected_acquisition_amount,
	scan_purchase_grni_residuals,
	scan_purchase_receipt_valuation,
)
from erpnext_extensions.iran_accounting.historical_rebuild.integrity_types import (
	DEFAULT_VALUE_TOLERANCE,
	ROOT_CAUSE_CATEGORY_A,
	ROOT_CAUSE_CATEGORY_B,
)

TOL = DEFAULT_VALUE_TOLERANCE


def _bootstrap() -> None:
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as bootstrap_apply

	bootstrap_apply()


def _fingerprint_pr(pr_name: str) -> dict[str, Any]:
	rows = frappe.db.sql(
		"""
		select pri.name, pri.item_code, pri.warehouse, pri.qty, pri.valuation_rate,
		       pri.base_net_amount, pri.billed_amt,
		       pri.amount_difference_with_purchase_invoice,
		       pri.landed_cost_voucher_amount,
		       (select stock_value_difference from `tabStock Ledger Entry` sle
		          where sle.voucher_type='Purchase Receipt' and sle.voucher_no=%s
		            and sle.voucher_detail_no=pri.name and sle.is_cancelled=0
		          limit 1) as sle_svd
		from `tabPurchase Receipt Item` pri
		where pri.parent=%s
		""",
		(pr_name, pr_name),
		as_dict=True,
	) or []
	out = []
	for r in rows:
		exp = expected_acquisition_amount(r)
		out.append(
			{
				"item_row": r.name,
				"item_code": r.item_code,
				"warehouse": r.warehouse,
				"expected": exp,
				"sle_svd": flt(r.sle_svd),
				"difference": flt(r.sle_svd) - exp if r.sle_svd is not None else None,
				"valuation_rate": flt(r.valuation_rate),
				"pi_diff": flt(r.amount_difference_with_purchase_invoice),
				"lcv": flt(r.landed_cost_voucher_amount),
			}
		)
	return {"purchase_receipt": pr_name, "items": out, "at": str(now_datetime())}


def _grni_net(company: str, vouchers: list[str]) -> float:
	findings = scan_purchase_grni_residuals(company, voucher_nos=vouchers)
	if not findings:
		# Explicit zero when no residual rows
		from erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_detection import (
			_grni_accounts,
		)

		accounts = _grni_accounts(company)
		if not accounts or not vouchers:
			return 0.0
		ph_a = ", ".join(["%s"] * len(accounts))
		ph_v = ", ".join(["%s"] * len(vouchers))
		row = frappe.db.sql(
			f"""
			select coalesce(sum(debit)-sum(credit),0)
			from `tabGL Entry`
			where is_cancelled=0 and company=%s
			  and account in ({ph_a}) and voucher_no in ({ph_v})
			""",
			tuple([company, *accounts, *vouchers]),
		)
		return flt(row[0][0]) if row else 0.0
	return sum(flt(f.get("difference_amount") or f.get("actual_value") or 0) for f in findings)


def _related_vouchers(pr_name: str) -> list[str]:
	vouchers = [pr_name]
	vouchers.extend(
		frappe.db.sql(
			"""
			select distinct parent from `tabPurchase Invoice Item`
			where purchase_receipt=%s and docstatus=1
			""",
			pr_name,
			pluck=True,
		)
		or []
	)
	vouchers.extend(
		frappe.db.sql(
			"""
			select distinct parent from `tabLanded Cost Purchase Receipt`
			where receipt_document=%s and parenttype='Landed Cost Voucher'
			""",
			pr_name,
			pluck=True,
		)
		or []
	)
	return vouchers


def assess_repair(pr_name: str, company: str | None = None) -> dict[str, Any]:
	"""Eligibility + whether RIV alone is enough for one Purchase Receipt."""
	_bootstrap()
	if not frappe.db.exists("Purchase Receipt", pr_name):
		return {
			"purchase_receipt": pr_name,
			"eligible": False,
			"blocked_reason": "purchase_receipt_not_found",
		}
	company = company or frappe.db.get_value("Purchase Receipt", pr_name, "company")
	findings = scan_purchase_receipt_valuation(
		company, purchase_receipts=[pr_name], limit=20
	)
	fp = _fingerprint_pr(pr_name)
	related = _related_vouchers(pr_name)
	grni = _grni_net(company, related)

	sle_clean = all(
		i.get("difference") is None or abs(flt(i.get("difference"))) <= TOL
		for i in fp.get("items") or []
	)
	per_billed = flt(frappe.db.get_value("Purchase Receipt", pr_name, "per_billed") or 0)
	if per_billed >= 99.99:
		grni_ok = abs(grni) <= TOL
	else:
		expected_grni = -sum(flt(i.get("expected") or 0) for i in fp.get("items") or [])
		grni_ok = abs(grni - expected_grni) <= TOL
	if not findings and sle_clean and grni_ok:
		return {
			"purchase_receipt": pr_name,
			"company": company,
			"eligible": False,
			"blocked_reason": "no_discrepancy",
			"already_correct": True,
			"fingerprint": fp,
			"grni_net": grni,
			"related_vouchers": related,
			"riv_alone_sufficient": True,
			"recommended_action": "none",
		}

	categories = {f.get("root_cause_category") for f in findings}
	# Document VR / amount_difference may already encode expected economics while
	# SLE lags (Category A historical). RIV alone only helps when document
	# valuation_rate already equals expected/qty; otherwise refresh first.
	riv_alone = True
	for item in fp["items"]:
		exp = flt(item["expected"])
		qty = flt(
			frappe.db.get_value("Purchase Receipt Item", item["item_row"], "qty") or 0
		)
		vr = flt(item["valuation_rate"])
		if qty > 0 and abs(vr * qty - exp) > TOL:
			riv_alone = False
			break
		if item["sle_svd"] is not None and abs(flt(item["sle_svd"]) - exp) > TOL:
			# SLE wrong but VR matches expected → RIV with authoritative acquisition may fix
			pass

	action = "riv_only" if riv_alone else "adjust_incoming_rate_then_riv"
	return {
		"purchase_receipt": pr_name,
		"company": company,
		"eligible": True,
		"blocked_reason": None,
		"categories": sorted(c for c in categories if c),
		"findings": findings,
		"fingerprint": fp,
		"grni_net": grni,
		"related_vouchers": related,
		"riv_alone_sufficient": riv_alone,
		"recommended_action": action,
	}


def _execute_riv_via_job(riv_name: str) -> dict[str, Any]:
	"""Run one RIV through Frappe's job executor so ``before_job`` bootstrap fires."""
	_bootstrap()
	from frappe.utils.background_jobs import execute_job

	execute_job(
		site=frappe.local.site,
		method="erpnext.stock.doctype.repost_item_valuation.repost_item_valuation.execute_reposting_entry",
		event=None,
		job_name=f"hrc_purchase_valuation_riv:{riv_name}",
		kwargs={"name": riv_name},
		user=frappe.session.user,
		is_async=False,
	)
	status, err = frappe.db.get_value(
		"Repost Item Valuation", riv_name, ["status", "error_log"]
	) or (None, None)
	return {"riv": riv_name, "status": status, "error_log": (err or "")[:500]}


def _create_and_run_riv(item_code: str, warehouse: str, company: str, posting_date, posting_time) -> dict:
	doc = frappe.new_doc("Repost Item Valuation")
	doc.based_on = "Item and Warehouse"
	doc.item_code = item_code
	doc.warehouse = warehouse
	doc.company = company
	doc.posting_date = posting_date
	doc.posting_time = posting_time or "00:00:01"
	doc.allow_negative_stock = 1
	doc.flags.ignore_permissions = True
	doc.insert()
	doc.submit()
	frappe.db.commit()
	return _execute_riv_via_job(doc.name)


def repair_one(
	pr_name: str,
	*,
	execute: bool = False,
	company: str | None = None,
) -> dict[str, Any]:
	"""Dry-run (default) or execute repair for one Purchase Receipt."""
	_bootstrap()
	assessment = assess_repair(pr_name, company=company)
	result: dict[str, Any] = {
		"dry_run": not execute,
		"assessment": assessment,
		"steps": [],
		"status": "dry_run" if not execute else "pending",
	}

	if assessment.get("already_correct"):
		result["status"] = "idempotent_skip"
		return result

	if not assessment.get("eligible"):
		result["status"] = "blocked"
		result["blocked_reason"] = assessment.get("blocked_reason")
		return result

	if not execute:
		result["status"] = "dry_run"
		result["planned_action"] = assessment.get("recommended_action")
		return result

	# --- EXECUTE ---
	before = assessment["fingerprint"]
	company = assessment["company"]
	related = assessment["related_vouchers"]

	# Step 1: Core adjust_incoming_rate_for_pr recalculates valuation_rate from
	# base + PI diff + LCV and reposts SLE/GL. Required when amount_difference is
	# already correct but VR/SLE still lag (billing-refresh alone is a no-op then).
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import (
		adjust_incoming_rate_for_pr,
	)

	pr = frappe.get_doc("Purchase Receipt", pr_name)
	try:
		adjust_incoming_rate_for_pr(pr)
		frappe.db.commit()
		result["steps"].append(
			{"step": "adjust_incoming_rate_for_pr", "result": {"status": "applied"}}
		)
	except Exception as exc:
		frappe.db.rollback()
		result["steps"].append(
			{
				"step": "adjust_incoming_rate_for_pr",
				"result": {"status": "failed", "error": str(exc)[:800]},
			}
		)
		result["status"] = "blocked"
		result["blocked_reason"] = "adjust_incoming_rate_failed"
		result["fingerprint_after"] = _fingerprint_pr(pr_name)
		return result

	# Step 1b: RIV/adjust may refresh SLE while leaving stale PR GL (inventory ≠ SLE).
	# Regenerate via Core reverse + make_gl_entries(from_repost=True) — no direct GL writes.
	mid = _fingerprint_pr(pr_name)
	gl_stock = flt(
		frappe.db.sql(
			"""
			select coalesce(sum(gl.debit - gl.credit), 0)
			from `tabGL Entry` gl
			inner join `tabAccount` a on a.name=gl.account
			where gl.voucher_type='Purchase Receipt' and gl.voucher_no=%s
			  and gl.is_cancelled=0 and a.account_type='Stock'
			""",
			pr_name,
		)[0][0]
	)
	sle_total = sum(flt(i.get("sle_svd")) for i in mid.get("items") or [])
	if abs(gl_stock - sle_total) > TOL:
		try:
			from erpnext.accounts.general_ledger import make_reverse_gl_entries

			make_reverse_gl_entries(voucher_type="Purchase Receipt", voucher_no=pr_name)
			pr = frappe.get_doc("Purchase Receipt", pr_name)
			pr.flags.ignore_permissions = True
			pr.make_gl_entries(from_repost=True)
			frappe.db.commit()
			result["steps"].append(
				{
					"step": "regenerate_pr_gl",
					"result": {
						"status": "applied",
						"gl_stock_before": gl_stock,
						"sle_total": sle_total,
					},
				}
			)
		except Exception as exc:
			frappe.db.rollback()
			result["steps"].append(
				{
					"step": "regenerate_pr_gl",
					"result": {"status": "failed", "error": str(exc)[:800]},
				}
			)
			result["status"] = "blocked"
			result["blocked_reason"] = "gl_regenerate_failed"
			result["fingerprint_after"] = _fingerprint_pr(pr_name)
			return result
	else:
		result["steps"].append(
			{"step": "regenerate_pr_gl", "result": {"status": "skipped_gl_matches_sle"}}
		)

	# Step 2: run PR-anchored RIV only for this receipt's item/warehouse pairs.
	# Do not drain unrelated Queued RIVs for the same item_code (other PRs / backlog).
	started = str(now_datetime())
	riv_results = []
	for item in pr.items:
		riv_results.append(
			_create_and_run_riv(
				item.item_code,
				item.warehouse,
				company,
				pr.posting_date,
				pr.posting_time,
			)
		)
	# Also complete Queued RIVs created by adjust_incoming_rate_for_pr for these WH
	warehouses = list({i.warehouse for i in pr.items if i.warehouse})
	item_codes = list({i.item_code for i in pr.items if i.item_code})
	spawned = frappe.get_all(
		"Repost Item Valuation",
		filters={
			"item_code": ("in", item_codes),
			"warehouse": ("in", warehouses),
			"status": ("in", ["Queued", "In Progress"]),
			"docstatus": 1,
			"creation": (">=", started),
		},
		pluck="name",
		limit_page_length=50,
	)
	for name in spawned:
		riv_results.append(_execute_riv_via_job(name))
	result["steps"].append({"step": "riv", "results": riv_results})

	failed = [r for r in riv_results if r.get("status") not in {"Completed", "Skipped"}]
	after = _fingerprint_pr(pr_name)
	grni_after = _grni_net(company, related)
	mismatches = [
		i
		for i in after["items"]
		if i.get("difference") is not None and abs(flt(i["difference"])) > TOL
	]

	result["fingerprint_before"] = before
	result["fingerprint_after"] = after
	result["grni_net_after"] = grni_after

	if failed:
		result["status"] = "blocked"
		result["blocked_reason"] = "riv_failed"
		result["failed_rivs"] = failed
		return result

	# Fully billed: purchase GRNI on the closed voucher set must be ~0.
	# Unbilled / partial: outstanding GRNI ≈ −Σ expected acquisition (credit).
	# Do not treat legitimate unbilled GRNI as verification failure (FX rate-first
	# repairs such as MAT-PRE-2026-01642 leave open GRNI equal to base_net).
	per_billed = flt(frappe.db.get_value("Purchase Receipt", pr_name, "per_billed") or 0)
	if per_billed >= 99.99:
		grni_ok = abs(grni_after) <= TOL
		expected_grni = 0.0
	else:
		expected_grni = -sum(flt(i.get("expected") or 0) for i in after.get("items") or [])
		grni_ok = abs(grni_after - expected_grni) <= TOL
	result["expected_grni_after"] = expected_grni
	result["grni_ok"] = grni_ok

	if mismatches or not grni_ok:
		result["status"] = "blocked"
		result["blocked_reason"] = "verification_failed"
		result["remaining_mismatches"] = mismatches
		return result

	result["status"] = "repaired"
	result["reassess"] = assess_repair(pr_name, company=company)
	return result


def repair_from_scan(
	company: str,
	*,
	execute: bool = False,
	limit: int = 20,
	purchase_receipts: list[str] | None = None,
) -> dict[str, Any]:
	"""Scan then repair Category A/B findings (allowlisted PRs optional)."""
	_bootstrap()
	scan = dry_run_purchase_valuation_scan(
		company, purchase_receipts=purchase_receipts, limit=limit
	)
	prs = []
	seen = set()
	for f in scan.get("findings") or []:
		if f.get("voucher_type") == "Purchase Receipt" and f.get("voucher_no"):
			if f["voucher_no"] not in seen:
				seen.add(f["voucher_no"])
				prs.append(f["voucher_no"])
		cat = f.get("root_cause_category")
		if cat in (ROOT_CAUSE_CATEGORY_A, ROOT_CAUSE_CATEGORY_B) and f.get("voucher_no"):
			if f["voucher_no"] not in seen and f.get("voucher_type") == "Purchase Receipt":
				seen.add(f["voucher_no"])
				prs.append(f["voucher_no"])

	# Explicit allowlist wins (operator-controlled); otherwise use scan hits.
	if purchase_receipts:
		prs = list(purchase_receipts)

	results = []
	for pr in prs[: cint(limit) or 20]:
		results.append(repair_one(pr, execute=execute, company=company))

	return {
		"dry_run": not execute,
		"company": company,
		"scan_summary": {
			"finding_count": scan.get("finding_count"),
			"by_root_cause_category": scan.get("by_root_cause_category"),
			"bucket_counts": scan.get("bucket_counts"),
		},
		"repair_count": len(results),
		"results": results,
	}
