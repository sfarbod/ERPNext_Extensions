# Copyright (c) 2026, ERPNext Extensions contributors
"""Dry-run detection for purchase valuation / LCV / GRNI / RIV defects (v5.6.25).

Categories
----------
A — Incorrect invoice-based Purchase Receipt valuation after PI.
B — Fractional Landed Cost rounding / purchase GRNI residual / transfer pair.
C — Failed / incomplete RIV preventing corrected purchase valuation propagation.

Unbilled Purchase Receipts are never classified as GRNI defects.
Partial billing compares only the billed portion.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.historical_rebuild.integrity_scanner import _finding
from erpnext_extensions.iran_accounting.historical_rebuild.integrity_types import (
	ANOMALY_LCV_ROUNDING,
	ANOMALY_PI_VALUATION,
	ANOMALY_PURCHASE_GRNI,
	ANOMALY_RIV_INCOMPLETE,
	ANOMALY_TRANSFER_PAIR,
	CONFIRMED_CORRUPTION,
	DEFAULT_VALUE_TOLERANCE,
	NEEDS_MANUAL_REVIEW,
	ROOT_CAUSE_CATEGORY_A,
	ROOT_CAUSE_CATEGORY_B,
	ROOT_CAUSE_CATEGORY_C,
	SUSPECTED_CORRUPTION,
)


def _grni_accounts(company: str) -> list[str]:
	"""Resolve purchase GRNI / stock received but not billed accounts for company."""
	names = frappe.db.sql(
		"""
		select name from `tabAccount`
		where company=%s and disabled=0
		  and (
		    account_type='Stock Received But Not Billed'
		    or name like '%%معلق خرید%%'
		    or name like '%%GRNI%%'
		    or name like '%%Stock Received But Not Billed%%'
		  )
		""",
		company,
		pluck=True,
	)
	return list(names or [])


def expected_acquisition_amount(row: dict) -> float:
	"""Base purchase + invoice price adjustment + landed cost (document fields)."""
	base = flt(row.get("base_net_amount") or row.get("base_amount") or 0)
	pi_diff = flt(row.get("amount_difference_with_purchase_invoice") or 0)
	lcv = flt(row.get("landed_cost_voucher_amount") or 0)
	return base + pi_diff + lcv


def classify_pr_item_discrepancy(row: dict, sle_svd: float | None, tol: float = DEFAULT_VALUE_TOLERANCE) -> dict | None:
	"""Classify a single PR item vs SLE without mutating data.

	Returns None when no actionable discrepancy (including unbilled rows).
	"""
	qty = flt(row.get("qty") or row.get("stock_qty") or 0)
	if qty <= 0:
		return None

	billed_amt = flt(row.get("billed_amt") or 0)
	base_net = flt(row.get("base_net_amount") or row.get("base_amount") or 0)
	per_billed = flt(row.get("per_billed") or 0)
	# Prefer explicit billed_amt; fall back to parent per_billed when present.
	fully_billed = billed_amt > 0 and abs(billed_amt - base_net - flt(row.get("amount_difference_with_purchase_invoice") or 0)) <= tol
	if per_billed >= 99.99:
		fully_billed = True

	expected = expected_acquisition_amount(row)
	vr = flt(row.get("valuation_rate") or 0)
	rate_times_qty = vr * qty if vr else None

	root = None
	anomaly = None
	classification = SUSPECTED_CORRUPTION
	notes = []
	lcv = flt(row.get("landed_cost_voucher_amount") or 0)
	pi_diff = flt(row.get("amount_difference_with_purchase_invoice") or 0)

	if billed_amt <= 0 and per_billed < 99.99:
		# Unbilled FX / rate-first shapes:
		# - Integer VR with SLE = qty×VR ≠ auth is the *correct* inventory side under
		#   v5.6.26 when Stock Adjustment carries the gap and GRNI stays at auth.
		# - Flag only when SLE still equals commercial auth via a fractional
		#   amount-preserving VR (pre-5.6.26 repair) or when integer VR SLE drifts.
		from erpnext_extensions.iran_accounting.domain.currency import (
			integer_valuation_rate_from_amount,
		)

		int_vr = (
			integer_valuation_rate_from_amount(expected, qty, "IRR") if expected and qty else None
		)
		inventory_at_integer = flt(int_vr) * qty if int_vr is not None else None
		fx_gap = (
			flt(inventory_at_integer) - expected
			if inventory_at_integer is not None
			else None
		)
		# Amount-preserving VR (qty×VR≈auth) while integer VR would leave a gap:
		# FX policy requires integer VR + Stock Adjustment (v5.6.26).
		if (
			sle_svd is not None
			and fx_gap is not None
			and abs(fx_gap) > tol
			and abs(flt(sle_svd) - expected) <= tol
			and rate_times_qty is not None
			and abs(rate_times_qty - expected) <= tol
			and int_vr is not None
			and abs(vr - flt(int_vr)) > 1e-9
		):
			return {
				"root_cause_category": ROOT_CAUSE_CATEGORY_B,
				"anomaly_type": ANOMALY_LCV_ROUNDING,
				"classification": CONFIRMED_CORRUPTION,
				"expected": inventory_at_integer,
				"actual": flt(sle_svd),
				"difference": flt(sle_svd) - flt(inventory_at_integer),
				"rate_times_qty": rate_times_qty,
				"commercial_auth": expected,
				"fx_rounding_gap": fx_gap,
				"fully_billed": False,
				"notes": ["unbilled_fx_fractional_vr_needs_integer_sa"],
				"repair_eligibility": "FX_INTEGER_VR_STOCK_ADJUSTMENT",
				"repair_risk": "MEDIUM",
				"recommended_repair_sequence": [
					"dry_run_impact_preview",
					"ensure_iran_monkey_patches",
					"align_fx_integer_valuation_rate",
					"repost_item_warehouse",
					"verify_sle_equals_qty_times_integer_vr",
					"verify_grni_equals_commercial_auth",
					"verify_stock_adjustment_equals_rounding_gap",
				],
			}
		# Integer VR already on document but SLE still at commercial auth (RIV lag).
		if (
			sle_svd is not None
			and fx_gap is not None
			and abs(fx_gap) > tol
			and int_vr is not None
			and abs(vr - flt(int_vr)) <= tol
			and abs(flt(sle_svd) - expected) <= tol
		):
			return {
				"root_cause_category": ROOT_CAUSE_CATEGORY_B,
				"anomaly_type": ANOMALY_LCV_ROUNDING,
				"classification": CONFIRMED_CORRUPTION,
				"expected": inventory_at_integer,
				"actual": flt(sle_svd),
				"difference": flt(sle_svd) - flt(inventory_at_integer),
				"rate_times_qty": rate_times_qty,
				"commercial_auth": expected,
				"fx_rounding_gap": fx_gap,
				"fully_billed": False,
				"notes": ["unbilled_fx_integer_vr_sle_lags_inventory"],
				"repair_eligibility": "FX_INTEGER_VR_STOCK_ADJUSTMENT",
				"repair_risk": "MEDIUM",
				"recommended_repair_sequence": [
					"dry_run_impact_preview",
					"ensure_iran_monkey_patches",
					"repost_item_warehouse",
					"verify_sle_equals_qty_times_integer_vr",
					"verify_grni_equals_commercial_auth",
					"verify_stock_adjustment_equals_rounding_gap",
				],
			}
		return None

	if sle_svd is not None and abs(flt(sle_svd) - expected) > tol:
		# Default: invoice-based valuation not reflected on SLE.
		root = ROOT_CAUSE_CATEGORY_A
		anomaly = ANOMALY_PI_VALUATION
		classification = CONFIRMED_CORRUPTION
		notes.append("sle_svd_ne_expected_acquisition")
		# Classic LCV bug: integer VR×qty matches inflated SLE, not expected amount.
		if (
			lcv
			and rate_times_qty is not None
			and abs(rate_times_qty - flt(sle_svd)) <= tol
			and abs(rate_times_qty - expected) > tol
		):
			root = ROOT_CAUSE_CATEGORY_B
			anomaly = ANOMALY_LCV_ROUNDING
			notes.append("integer_vr_times_qty_matches_inflated_sle")
		elif lcv and pi_diff == 0:
			root = ROOT_CAUSE_CATEGORY_B
			anomaly = ANOMALY_LCV_ROUNDING
			notes.append("lcv_present_sle_ne_expected")

	if root is None:
		return None

	return {
		"root_cause_category": root,
		"anomaly_type": anomaly,
		"classification": classification,
		"expected": expected,
		"actual": flt(sle_svd) if sle_svd is not None else None,
		"difference": (flt(sle_svd) - expected) if sle_svd is not None else None,
		"rate_times_qty": rate_times_qty,
		"fully_billed": fully_billed,
		"notes": notes,
		"repair_eligibility": "RIV_FROM_PR" if root == ROOT_CAUSE_CATEGORY_A else "RIV_AFTER_LCV_REALIGN",
		"repair_risk": "MEDIUM",
		"recommended_repair_sequence": [
			"dry_run_impact_preview",
			"ensure_iran_monkey_patches",
			"repost_item_valuation_from_pr",
			"verify_sle_gl_grni",
		],
	}


def scan_purchase_receipt_valuation(
	company: str,
	*,
	limit: int = 500,
	tol: float = DEFAULT_VALUE_TOLERANCE,
	purchase_receipts: list[str] | None = None,
) -> list[dict]:
	"""Category A/B dry-run over submitted Purchase Receipts."""
	filters = {"docstatus": 1, "company": company}
	if purchase_receipts:
		filters["name"] = ("in", purchase_receipts)

	prs = frappe.get_all(
		"Purchase Receipt",
		filters=filters,
		fields=["name", "per_billed", "status", "supplier"],
		order_by="posting_date desc, creation desc",
		limit_page_length=cint(limit) or 500,
	) or []

	findings: list[dict] = []
	for pr in prs:
		items = frappe.get_all(
			"Purchase Receipt Item",
			filters={"parent": pr.name},
			fields=[
				"name",
				"item_code",
				"warehouse",
				"qty",
				"stock_qty",
				"valuation_rate",
				"base_net_amount",
				"base_amount",
				"billed_amt",
				"amount_difference_with_purchase_invoice",
				"landed_cost_voucher_amount",
			],
		) or []
		for it in items:
			it = dict(it)
			it["per_billed"] = flt(pr.per_billed)
			sle = frappe.db.get_value(
				"Stock Ledger Entry",
				{
					"voucher_type": "Purchase Receipt",
					"voucher_no": pr.name,
					"voucher_detail_no": it["name"],
					"is_cancelled": 0,
				},
				["name", "stock_value_difference", "incoming_rate", "warehouse"],
				as_dict=True,
			)
			sle_svd = flt(sle.stock_value_difference) if sle else None
			classified = classify_pr_item_discrepancy(it, sle_svd, tol=tol)
			if not classified:
				continue
			related_pi = frappe.db.sql(
				"""
				select distinct parent from `tabPurchase Invoice Item`
				where purchase_receipt=%s and docstatus=1
				""",
				pr.name,
				pluck=True,
			)
			related_lcv = frappe.db.sql(
				"""
				select distinct parent from `tabLanded Cost Purchase Receipt`
				where receipt_document=%s and parenttype='Landed Cost Voucher'
				""",
				pr.name,
				pluck=True,
			)
			finding = _finding(
				company=company,
				item_code=it.get("item_code") or "",
				warehouse=(sle.warehouse if sle else it.get("warehouse")) or "",
				anomaly_type=classified["anomaly_type"],
				classification=classified["classification"],
				sle=sle.name if sle else None,
				voucher_type="Purchase Receipt",
				voucher_no=pr.name,
				expected=classified["expected"],
				actual=classified["actual"],
				difference=classified["difference"],
				recommendation=(
					"Repost Item+Warehouse from PR posting time with Iran patches; "
					"verify GRNI=0 when fully billed; do not balance via JE."
				),
			)
			finding.update(
				{
					"root_cause_category": classified["root_cause_category"],
					"related_purchase_invoices": list(related_pi or []),
					"related_landed_cost_vouchers": list(related_lcv or []),
					"repair_eligibility": classified["repair_eligibility"],
					"repair_risk": classified["repair_risk"],
					"recommended_repair_sequence": classified["recommended_repair_sequence"],
					"notes": classified["notes"],
					"rate_times_qty": classified.get("rate_times_qty"),
				}
			)
			findings.append(finding)
	return findings


def scan_purchase_grni_residuals(
	company: str,
	*,
	tol: float = DEFAULT_VALUE_TOLERANCE,
	voucher_nos: list[str] | None = None,
) -> list[dict]:
	"""Category B: non-zero purchase GRNI on a closed PR/PI/LCV set.

	When ``voucher_nos`` is omitted, only samples recent fully-billed PRs
	(avoids flagging open unbilled GRNI as defects).
	"""
	accounts = _grni_accounts(company)
	if not accounts:
		return []

	if voucher_nos is None:
		prs = frappe.get_all(
			"Purchase Receipt",
			filters={"docstatus": 1, "company": company, "per_billed": (">=", 99.99)},
			pluck="name",
			limit_page_length=200,
		) or []
		voucher_nos = []
		for pr in prs:
			voucher_nos.append(pr)
			voucher_nos.extend(
				frappe.db.sql(
					"""
					select distinct parent from `tabPurchase Invoice Item`
					where purchase_receipt=%s and docstatus=1
					""",
					pr,
					pluck=True,
				)
				or []
			)
			voucher_nos.extend(
				frappe.db.sql(
					"""
					select distinct parent from `tabLanded Cost Purchase Receipt`
					where receipt_document=%s and parenttype='Landed Cost Voucher'
					""",
					pr,
					pluck=True,
				)
				or []
			)

	if not voucher_nos:
		return []

	placeholders = ", ".join(["%s"] * len(voucher_nos))
	acc_ph = ", ".join(["%s"] * len(accounts))
	rows = frappe.db.sql(
		f"""
		select account, sum(debit) d, sum(credit) c, sum(debit)-sum(credit) net
		from `tabGL Entry`
		where is_cancelled=0 and company=%s
		  and account in ({acc_ph})
		  and voucher_no in ({placeholders})
		group by account
		having abs(sum(debit)-sum(credit)) > %s
		""",
		tuple([company, *accounts, *voucher_nos, tol]),
		as_dict=True,
	) or []

	out = []
	for r in rows:
		finding = _finding(
			company=company,
			item_code="",
			warehouse="",
			anomaly_type=ANOMALY_PURCHASE_GRNI,
			classification=CONFIRMED_CORRUPTION,
			expected=0.0,
			actual=flt(r.net),
			difference=flt(r.net),
			recommendation=(
				"Trace fully-billed PR/PI/LCV chain; repair acquisition SLE amount "
				"preservation; never park residual in purchase GRNI."
			),
		)
		finding.update(
			{
				"root_cause_category": ROOT_CAUSE_CATEGORY_B,
				"account": r.account,
				"related_vouchers": list(voucher_nos)[:50],
				"repair_eligibility": "AMOUNT_PRESERVING_RIV",
				"repair_risk": "HIGH",
			}
		)
		out.append(finding)
	return out


def scan_transfer_pair_imbalance(
	company: str,
	*,
	limit: int = 200,
	tol: float = DEFAULT_VALUE_TOLERANCE,
) -> list[dict]:
	"""Category B/C: Material Transfer source/dest SLE amounts must cancel."""
	rows = frappe.db.sql(
		"""
		select voucher_no, voucher_detail_no, item_code,
		       sum(case when actual_qty < 0 then stock_value_difference else 0 end) src_svd,
		       sum(case when actual_qty > 0 then stock_value_difference else 0 end) dst_svd
		from `tabStock Ledger Entry`
		where is_cancelled=0 and company=%s and voucher_type='Stock Entry'
		group by voucher_no, voucher_detail_no, item_code
		having abs(
		  sum(case when actual_qty < 0 then stock_value_difference else 0 end)
		  + sum(case when actual_qty > 0 then stock_value_difference else 0 end)
		) > %s
		   and sum(case when actual_qty < 0 then 1 else 0 end) > 0
		   and sum(case when actual_qty > 0 then 1 else 0 end) > 0
		limit %s
		""",
		(company, tol, cint(limit) or 200),
		as_dict=True,
	) or []
	out = []
	for r in rows:
		purpose = frappe.db.get_value("Stock Entry", r.voucher_no, "purpose")
		if str(purpose or "") not in (
			"Material Transfer",
			"Material Transfer for Manufacture",
			"Send to Subcontractor",
		):
			continue
		diff = flt(r.src_svd) + flt(r.dst_svd)
		finding = _finding(
			company=company,
			item_code=r.item_code or "",
			warehouse="",
			anomaly_type=ANOMALY_TRANSFER_PAIR,
			classification=CONFIRMED_CORRUPTION,
			voucher_type="Stock Entry",
			voucher_no=r.voucher_no,
			expected=0.0,
			actual=diff,
			difference=diff,
			recommendation=(
				"Repost with Iran transfer inward conservation "
				"(destination SVD = −source SVD)."
			),
		)
		finding.update(
			{
				"root_cause_category": ROOT_CAUSE_CATEGORY_B,
				"voucher_detail_no": r.voucher_detail_no,
				"src_svd": flt(r.src_svd),
				"dst_svd": flt(r.dst_svd),
				"purpose": purpose,
				"repair_eligibility": "RIV_WITH_TRANSFER_CONSERVATION",
				"repair_risk": "MEDIUM",
			}
		)
		out.append(finding)
	return out


def scan_incomplete_riv_for_items(
	company: str,
	*,
	item_codes: list[str] | None = None,
	limit: int = 200,
) -> list[dict]:
	"""Category C: Failed / Skipped / long-Queued RIV rows."""
	params: list[Any] = []
	item_clause = ""
	if item_codes:
		item_clause = " and item_code in ({})".format(", ".join(["%s"] * len(item_codes)))
		params.extend(item_codes)
	params.extend([cint(limit) or 200])
	rows = frappe.db.sql(
		f"""
		select name, item_code, warehouse, status, posting_date, modified, error_log
		from `tabRepost Item Valuation`
		where docstatus=1
		  and status in ('Failed', 'Skipped', 'Queued')
		  {item_clause}
		order by modified desc
		limit %s
		""",
		tuple(params),
		as_dict=True,
	) or []
	out = []
	for r in rows:
		finding = _finding(
			company=company,
			item_code=r.item_code or "",
			warehouse=r.warehouse or "",
			anomaly_type=ANOMALY_RIV_INCOMPLETE,
			classification=NEEDS_MANUAL_REVIEW if r.status == "Queued" else CONFIRMED_CORRUPTION,
			related_rivs=[f"{r.name}:{r.status}"],
			recommendation=(
				"Drain or execute RIV synchronously with Iran monkey patches; "
				"do not mark success without SLE/GL verification."
			),
		)
		finding.update(
			{
				"root_cause_category": ROOT_CAUSE_CATEGORY_C,
				"riv_status": r.status,
				"error_log": (r.error_log or "")[:500],
				"repair_eligibility": "EXECUTE_RIV",
				"repair_risk": "MEDIUM",
			}
		)
		out.append(finding)
	return out


def dry_run_purchase_valuation_scan(
	company: str,
	*,
	campaign: str | None = None,
	purchase_receipts: list[str] | None = None,
	limit: int = 500,
) -> dict[str, Any]:
	"""Impact preview: detect and classify without mutating accounting data."""
	buckets = {
		"category_a_b_pr": scan_purchase_receipt_valuation(
			company, limit=limit, purchase_receipts=purchase_receipts
		),
		"category_b_grni": scan_purchase_grni_residuals(company),
		"category_b_transfer_pair": scan_transfer_pair_imbalance(company, limit=limit),
		"category_c_riv": scan_incomplete_riv_for_items(company, limit=limit),
	}
	findings: list[dict] = []
	for rows in buckets.values():
		findings.extend(rows)

	by_cat: dict[str, int] = {}
	for f in findings:
		cat = f.get("root_cause_category") or "UNKNOWN"
		by_cat[cat] = by_cat.get(cat, 0) + 1

	return {
		"dry_run": True,
		"company": company,
		"campaign": campaign,
		"finding_count": len(findings),
		"by_root_cause_category": by_cat,
		"bucket_counts": {k: len(v) for k, v in buckets.items()},
		"findings": findings,
		"notes": [
			"Unbilled Purchase Receipts are excluded from GRNI/PI valuation defects.",
			"Repair uses standard RIV with Iran monkey patches; no manual JE balancing.",
			"Idempotent: re-scan after successful RIV should clear Category A/B findings.",
		],
	}
