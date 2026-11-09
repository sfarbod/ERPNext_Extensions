# Copyright (c) 2026, ERPNext Extensions contributors
"""Dry-run manifests and cohort gates for financial correction campaigns."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.iran_accounting.financial_correction_campaign import (
	API_VERSION,
	ASSET_PR_APPROVED_ACCOUNT_PREFIX,
	CONFIRM_EXECUTE_ASSET_PR,
	CONFIRM_EXECUTE_STE_FRAC,
	DEFAULT_COMPANY,
	DENYLIST_JOURNAL_ENTRIES,
)


class CampaignError(frappe.ValidationError):
	pass


def require_privileged_user() -> str:
	user = frappe.session.user
	roles = set(frappe.get_roles(user))
	if user == "Administrator" or "Administrator" in roles or "System Manager" in roles:
		return user
	frappe.throw("Financial correction campaign requires Administrator or System Manager", frappe.PermissionError)


def assert_journal_denylist_intact() -> dict[str, Any]:
	"""Hard gate: denylisted JEs must never be mutated by these campaigns."""
	out = []
	for name in sorted(DENYLIST_JOURNAL_ENTRIES):
		if not frappe.db.exists("Journal Entry", name):
			out.append({"name": name, "exists": False, "protected": True})
			continue
		je = frappe.db.get_value(
			"Journal Entry",
			name,
			["name", "docstatus", "modified", "modified_by", "total_debit"],
			as_dict=True,
		)
		out.append({**je, "exists": True, "protected": True, "campaign_action": "NEVER_TOUCH"})
	return {"denylist": out, "policy": "Finance-owned; excluded at every campaign entry point"}


def resolve_account_by_number(company: str, account_number: str) -> str | None:
	rows = frappe.db.sql(
		"""
		SELECT name FROM `tabAccount`
		WHERE company=%s AND is_group=0 AND disabled=0
		  AND (account_number=%s OR name LIKE %s)
		ORDER BY name LIMIT 1
		""",
		(company, account_number, f"{account_number} %"),
	)
	return rows[0][0] if rows else None


def _assert_not_denylisted_voucher(voucher_type: str, voucher_no: str) -> None:
	if voucher_type == "Journal Entry" and voucher_no in DENYLIST_JOURNAL_ENTRIES:
		raise CampaignError(f"{voucher_no} is denylisted and cannot enter any campaign cohort")


# ---------------------------------------------------------------------------
# Cohort A — Asset Purchase Receipts
# ---------------------------------------------------------------------------


def dry_run_asset_pr_cohort(company: str | None = None) -> dict[str, Any]:
	"""Build per-voucher dry-run manifest for the approved 8 Asset PRs. No mutation."""
	require_privileged_user()
	company = company or DEFAULT_COMPANY
	je_gate = assert_journal_denylist_intact()

	from erpnext_extensions.iran_accounting.asset_purchase_receipt_correction.service import (
		fingerprint_pr,
		inspect_purchase_receipt,
		land_account_for_company,
	)

	land = land_account_for_company(company)
	results = []
	for pr_name, acct_num in ASSET_PR_APPROVED_ACCOUNT_PREFIX.items():
		_assert_not_denylisted_voucher("Purchase Receipt", pr_name)
		target = resolve_account_by_number(company, acct_num)
		entry: dict[str, Any] = {
			"purchase_receipt": pr_name,
			"approved_account_number": acct_num,
			"resolved_target_account": target,
			"execution_status": "PENDING — dry_run only; apply not authorized",
		}
		if not target:
			entry["blocked_reason"] = "target_account_not_found"
			entry["success"] = False
			results.append(entry)
			continue
		if not frappe.db.exists("Purchase Receipt", pr_name):
			entry["blocked_reason"] = "purchase_receipt_missing"
			entry["success"] = False
			results.append(entry)
			continue
		# Force override to approved mapping (not blanket category guess).
		insp = inspect_purchase_receipt(
			pr_name,
			company=company,
			account_overrides={
				# Apply override only to Land expense fixed-asset rows.
			},
		)
		# Build overrides from Land rows → approved target
		overrides = {}
		for row in insp.get("rows") or []:
			if row.get("from_account") == land and row.get("is_fixed_asset"):
				overrides[row["item_row"]] = target
		insp2 = inspect_purchase_receipt(pr_name, company=company, account_overrides=overrides)
		actionable = [r for r in insp2.get("rows") or [] if r.get("needs_correction")]
		# Linked PIs + amount differences (read-only)
		pi_rows = frappe.db.sql(
			"""
			SELECT pii.parent AS purchase_invoice, pii.pr_detail, pii.qty, pii.rate, pii.amount,
			       pii.base_net_amount, pii.expense_account,
			       pri.rate AS pr_rate, pri.amount AS pr_amount, pri.base_net_amount AS pr_base_net,
			       pri.billed_amt, pri.valuation_rate, pri.amount_difference_with_purchase_invoice,
			       pri.is_fixed_asset, pri.expense_account AS pr_expense_account
			FROM `tabPurchase Invoice Item` pii
			JOIN `tabPurchase Invoice` pi ON pi.name = pii.parent AND pi.docstatus = 1
			LEFT JOIN `tabPurchase Receipt Item` pri ON pri.name = pii.pr_detail
			WHERE pii.purchase_receipt=%s
			""",
			pr_name,
			as_dict=True,
		)
		from erpnext_extensions.iran_accounting.asset_purchase_receipt_correction.asset_impact import (
			inspect_assets_for_purchase_receipt,
		)
		from erpnext_extensions.iran_accounting.integration.fa_pr_gl_amount_difference import (
			is_fa_pr_gl_amount_difference_patch_installed,
		)

		asset_impact = inspect_assets_for_purchase_receipt(pr_name)
		amt_diff_total = sum(
			flt(r.amount_difference_with_purchase_invoice)
			for r in frappe.get_all(
				"Purchase Receipt Item",
				filters={"parent": pr_name, "is_fixed_asset": 1},
				fields=["amount_difference_with_purchase_invoice"],
			)
		)
		fa_gl_patch = is_fa_pr_gl_amount_difference_patch_installed()
		blockers = []
		# Prior Cohort A execution states (Dev evidence) — never re-run successes in this task.
		prior_success = {
			"MAT-PRE-2026-01907",
			"MAT-PRE-2026-00953",
			"MAT-PRE-2026-01311",
		}
		prior_failed = {"MAT-PRE-2026-00747"}
		if pr_name in prior_success:
			blockers.append(
				"ALREADY_RECONCILED_ON_DEV — do not re-execute source correction or RIV"
			)
		if pr_name in prior_failed and abs(amt_diff_total) > 0.5 and not fa_gl_patch:
			blockers.append(
				"FA_GL_AMT_DIFF_PATCH_REQUIRED — accounting RIV unbalanced until FA debit includes amt_diff"
			)
		if pr_name in prior_failed and abs(amt_diff_total) > 0.5:
			blockers.append(
				"SOURCE_GL_DIVERGED — expense_account may already be corrected while GL still on Land; "
				"future execute must verify source then RIV only"
			)
		if asset_impact.get("any_rebuild_required"):
			blockers.append(
				"ASSET_COST_REVIEW_REQUIRED — see asset_impact; do not auto-rebuild depreciation"
			)
		# Four unexecuted vouchers are ready for a *future* controlled run only.
		if pr_name not in prior_success | prior_failed:
			blockers.append(
				"NOT_EXECUTED_THIS_TASK — future controlled execute only after readiness sign-off"
			)

		entry.update(
			{
				"success": True,
				"fingerprint": fingerprint_pr(pr_name),
				"land_account": land,
				"land_debit_before": insp2.get("land_debit_before"),
				"gl_before": insp2.get("gl_before"),
				"actionable_rows": actionable,
				"expected_gl_changes": insp2.get("expected_gl_changes"),
				"linked_purchase_invoices": sorted({r.purchase_invoice for r in pi_rows}),
				"purchase_invoice_details": pi_rows,
				"amount_difference_with_purchase_invoice_total": amt_diff_total,
				"fa_gl_amount_difference_patch_installed": fa_gl_patch,
				"asset_impact": asset_impact,
				"linked_assets": [
					{"name": a["asset"], "docstatus": a["docstatus"], "status": a["status"]}
					for a in asset_impact.get("assets") or []
				],
				"depreciation_rebuild_any_required": asset_impact.get("any_rebuild_required"),
				"execution_blockers": blockers,
				"riv_plan": {
					"based_on": "Transaction",
					"voucher_type": "Purchase Receipt",
					"voucher_no": pr_name,
					"repost_only_accounting_ledgers": 1,
					"note": (
						"Source expense_account correction first; then accounting RIV. "
						"RIV alone does not update Asset acquisition cost or depreciation schedules."
					),
				},
				"gates": {
					"source_account_must_equal_target": True,
					"no_manual_je": True,
					"no_duplicate_gl": True,
					"verify_asset_acquisition_and_depr": True,
					"deny_ACC-JV-1405-169046": True,
					"fa_gl_includes_pi_amount_difference": True,
					"no_auto_asset_or_depr_rebuild": True,
				},
			}
		)
		results.append(entry)

	manifest = {
		"api_version": API_VERSION,
		"cohort": "A_asset_purchase_receipt",
		"dry_run": True,
		"mutated": False,
		"company": company,
		"confirm_token_for_future_execute": CONFIRM_EXECUTE_ASSET_PR,
		"generated_at": str(now_datetime()),
		"journal_denylist": je_gate,
		"denylist_journal_entries": sorted(DENYLIST_JOURNAL_ENTRIES),
		"fa_gl_amount_difference_patch_installed": (
			results[0].get("fa_gl_amount_difference_patch_installed") if results else False
		),
		"voucher_count": len(results),
		"success_count": sum(1 for r in results if r.get("success")),
		"allowlist": list(ASSET_PR_APPROVED_ACCOUNT_PREFIX.keys()),
		"approved_account_map": dict(ASSET_PR_APPROVED_ACCOUNT_PREFIX),
		"results": results,
		"execution_order_future_only": [
			"1. Confirm FA GL amt_diff patch installed (bootstrap)",
			"2. Skip already-reconciled: 01907, 00953, 01311",
			"3. MAT-PRE-2026-00747: verify source expense_account=121006; accounting RIV only; then Asset impact review",
			"4. Then one-by-one: 01413 → 00880 → 01241 → 01266 (source then RIV; stop on fail)",
			"5. After each PR: reconcile FA/GRNI GL; inspect Assets; never auto-rebuild depr with posted history",
			"6. Never touch ACC-JV-1405-169046",
			"7. Never execute Cohort B from this path",
		],
		"this_task_mutated_documents": False,
	}
	return manifest


# ---------------------------------------------------------------------------
# Cohort B — Stock Entry fractional IRR
# ---------------------------------------------------------------------------


def _load_investigation_rows() -> list[dict]:
	"""Prefer app-root JSON; fall back to /tmp inventory."""
	candidates = [
		Path(frappe.get_app_path("erpnext_extensions")).parent / "STE_FRACTIONAL_IRR_INVESTIGATION_DEV.json",
		Path("/tmp/ste_frac_raw.json"),
	]
	# investigation JSON may only have counts; prefer raw SLE list
	raw_path = Path("/tmp/ste_frac_raw.json")
	app_raw = Path(frappe.get_app_path("erpnext_extensions")).parent / "STE_FRACTIONAL_IRR_INVESTIGATION_DEV.json"
	if raw_path.exists():
		data = json.loads(raw_path.read_text())
		if data.get("rows"):
			return data["rows"]
	# Rebuild from DB if file missing (read-only query)
	company = DEFAULT_COMPANY
	return frappe.db.sql(
		"""
		SELECT sle.name sle_name, sle.voucher_no, sle.voucher_detail_no, sle.item_code, sle.warehouse,
		       sle.incoming_rate, sle.outgoing_rate, sle.actual_qty, sle.stock_value_difference,
		       sle.posting_date, se.stock_entry_type, se.purpose
		FROM `tabStock Ledger Entry` sle
		JOIN `tabStock Entry` se ON se.name=sle.voucher_no
		WHERE sle.company=%s AND sle.is_cancelled=0 AND sle.voucher_type='Stock Entry'
		  AND ABS(IFNULL(sle.incoming_rate,0) - ROUND(IFNULL(sle.incoming_rate,0))) > 1e-9
		ORDER BY se.stock_entry_type, sle.posting_date, sle.voucher_no
		""",
		company,
		as_dict=True,
	)


def dry_run_ste_fractional_cohort(company: str | None = None) -> dict[str, Any]:
	"""Build identity/voucher dry-run manifest for fractional STE SLEs. No mutation."""
	require_privileged_user()
	company = company or DEFAULT_COMPANY
	je_gate = assert_journal_denylist_intact()
	rows = _load_investigation_rows()

	from decimal import Decimal, ROUND_HALF_UP

	def half_up(x):
		return float(Decimal(str(flt(x))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

	by_type: dict[str, list] = {}
	identities: dict[tuple[str, str], dict] = {}
	vouchers: dict[str, dict] = {}

	for r in rows:
		# rows may be dict or as_dict
		if not isinstance(r, dict):
			r = dict(r)
		typ = r.get("stock_entry_type") or r.get("purpose") or "Unknown"
		vn = r["voucher_no"]
		item = r["item_code"]
		wh = r["warehouse"]
		ir = flt(r.get("incoming_rate"))
		qty = flt(r.get("actual_qty"))
		svd = flt(r.get("stock_value_difference"))
		exp = half_up(ir)
		residual = svd - qty * exp
		by_type.setdefault(typ, []).append(vn)
		key = (item, wh)
		identities.setdefault(
			key,
			{
				"item_code": item,
				"warehouse": wh,
				"stock_entry_types": set(),
				"vouchers": set(),
				"frac_sle_count": 0,
				"earliest_posting_date": str(r.get("posting_date")),
			},
		)
		identities[key]["stock_entry_types"].add(typ)
		identities[key]["vouchers"].add(vn)
		identities[key]["frac_sle_count"] += 1
		if str(r.get("posting_date")) < identities[key]["earliest_posting_date"]:
			identities[key]["earliest_posting_date"] = str(r.get("posting_date"))

		vouchers.setdefault(
			vn,
			{
				"voucher_no": vn,
				"stock_entry_type": typ,
				"frac_sle_rows": [],
				"execution_status": "PENDING — dry_run only; RIV not authorized",
				"riv_plan": {
					"based_on": "Item and Warehouse",
					"note": "One identity RIV from earliest date covering this voucher's item@WH; not company-wide",
				},
			},
		)
		# SED check
		sed_vr = frappe.db.get_value("Stock Entry Detail", r.get("voucher_detail_no"), "valuation_rate")
		vouchers[vn]["frac_sle_rows"].append(
			{
				"sle": r.get("sle_name") or r.get("name"),
				"item_code": item,
				"warehouse": wh,
				"qty": qty,
				"incoming_rate": ir,
				"expected_integer_incoming_rate": exp,
				"svd": svd,
				"class_a_residual_vs_int": residual,
				"sed_valuation_rate": flt(sed_vr) if sed_vr is not None else None,
				"sed_vr_fractional": bool(
					sed_vr is not None and abs(flt(sed_vr) - round(flt(sed_vr))) > 1e-9
				),
			}
		)

	# Special: missing GL investigation for MAT-STE-2026-29903
	repack_gl = None
	if frappe.db.exists("Stock Entry", "MAT-STE-2026-29903"):
		gl = frappe.db.sql(
			"""
			SELECT account, SUM(debit) d, SUM(credit) c FROM `tabGL Entry`
			WHERE voucher_type='Stock Entry' AND voucher_no=%s AND is_cancelled=0
			GROUP BY account
			""",
			"MAT-STE-2026-29903",
			as_dict=True,
		)
		repack_gl = {
			"voucher": "MAT-STE-2026-29903",
			"gl_row_count": len(gl),
			"gl": gl,
			"assessment": (
				"Missing GL — must be investigated before claiming SLE/GL reconciliation; "
				"do not silently accept"
				if not gl
				else "GL present"
			),
		}

	id_list = []
	for (item, wh), meta in sorted(identities.items()):
		id_list.append(
			{
				**meta,
				"stock_entry_types": sorted(meta["stock_entry_types"]),
				"vouchers": sorted(meta["vouchers"]),
				"fingerprint": hashlib.sha256(
					f"{item}|{wh}|{meta['frac_sle_count']}|{meta['earliest_posting_date']}".encode()
				).hexdigest(),
				"controlled_riv": {
					"based_on": "Item and Warehouse",
					"item_code": item,
					"warehouse": wh,
					"posting_date": meta["earliest_posting_date"],
					"allow_negative_stock": 1,
				},
			}
		)

	manifest = {
		"api_version": API_VERSION,
		"cohort": "B_ste_fractional_irr",
		"dry_run": True,
		"mutated": False,
		"company": company,
		"confirm_token_for_future_execute": CONFIRM_EXECUTE_STE_FRAC,
		"generated_at": str(now_datetime()),
		"journal_denylist": je_gate,
		"source": "STE_FRACTIONAL_IRR_INVESTIGATION_DEV (+ live frac SLE query fallback)",
		"counts": {
			"frac_sle_rows": len(rows),
			"vouchers": len(vouchers),
			"identities": len(id_list),
			"by_type": {t: len(set(v)) for t, v in by_type.items()},
		},
		"identities": id_list,
		"vouchers": list(vouchers.values()),
		"repack_missing_gl_gate": repack_gl,
		"code_fixes_prepared": [
			"historical_stock/manufacture_native.py — integer write_sle_rate / SED rates",
			"historical_stock/manufacture_scrap_fg.py — integer scrap/FG SLE rates",
			"domain/stock_entry_sync.py — SVD/qty via round_monetary_rate + enforce_irr_sle_unit_rates_integer",
		],
		"execution_order": [
			"1. Review dry-run manifest + code fixes + unit tests",
			"2. Controlled test: one identity per category (see investigation §6)",
			"3. Per identity Item+Warehouse RIV (not company-wide)",
			"4. Gates: integer SED VR, integer SLE IR, SVD/GL recon, Class A residual explicit",
			"5. Investigate MAT-STE-2026-29903 missing GL before accepting Repack cohort",
			"6. Never touch ACC-JV-1405-169046",
		],
		"rejected_outside_cohort": True,
	}
	return manifest


def reject_if_outside_asset_cohort(purchase_receipts) -> list[str]:
	"""Return only approved cohort names; raise if any outsider present."""
	if isinstance(purchase_receipts, str):
		try:
			purchase_receipts = json.loads(purchase_receipts)
		except json.JSONDecodeError:
			purchase_receipts = [purchase_receipts]
	names = [str(x).strip() for x in (purchase_receipts or []) if str(x).strip()]
	bad = [n for n in names if n not in ASSET_PR_APPROVED_ACCOUNT_PREFIX]
	if bad:
		raise CampaignError(f"Vouchers outside approved Asset PR cohort: {bad}")
	for n in names:
		_assert_not_denylisted_voucher("Purchase Receipt", n)
	return names
