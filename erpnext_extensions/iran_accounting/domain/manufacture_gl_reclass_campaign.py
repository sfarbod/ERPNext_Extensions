# Copyright (c) 2026, ERPNext Extensions contributors
"""Scoped historical GL normalization campaign for six Manufacture vouchers.

Package: HRC-00012 (Manufacture GL normalization — not an RIV campaign).

Design
------
Rebuild GL from normalized Manufacture generation:

* Inventory legs from SLE only (WIP / FG / SFG / scrap / by / co-product).
* Additional Costs credited once to OH accounts (no clearing debit).
* Stock Adjustment = genuine residual only, at most one debit **or** credit row.

Does **not** move gross clearing turnover to COGM ``511009``.

Apply (after Finance approval only):
  ``apply_manufacture_gl_reclass(company, dry_run=False, confirm_apply=True)``
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.iran_accounting.domain.manufacture_expense_account import (
	SA_RESIDUAL_REMARK_TOKEN,
	assert_sa_is_residual_only,
	count_sa_gl_rows,
	manufacture_expected_sa_residual_net,
	stock_adjustment_account,
	summarize_gl_by_account,
)

PACKAGE_ID = "HRC-00012"
IDEMPOTENCY_PREFIX = "IRR_MFG_GL_NORM|"

HRC00012_MANUFACTURE_VOUCHERS: tuple[str, ...] = (
	"MAT-STE-2026-40332",
	"MAT-STE-2026-40149",
	"MAT-STE-2026-36826",
	"MAT-STE-2026-36928",
	"MAT-STE-2026-40902",
	"MAT-STE-2026-43589",
)


def _checksum_vouchers(vouchers: tuple[str, ...] | list[str]) -> str:
	payload = "|".join(sorted(vouchers))
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def discover_package(company: str) -> dict[str, Any]:
	rows = []
	for name in HRC00012_MANUFACTURE_VOUCHERS:
		if not frappe.db.exists("Stock Entry", name):
			frappe.throw(f"Allowlisted voucher missing: {name}", frappe.ValidationError)
		doc = frappe.db.get_value(
			"Stock Entry",
			name,
			["name", "company", "docstatus", "purpose", "stock_entry_type", "posting_date"],
			as_dict=True,
		)
		if doc.company != company:
			frappe.throw(
				f"{name} company={doc.company!r} does not match campaign company={company!r}",
				frappe.ValidationError,
			)
		if cint(doc.docstatus) != 1:
			frappe.throw(f"{name} is not submitted (docstatus={doc.docstatus})", frappe.ValidationError)
		if doc.purpose != "Manufacture":
			frappe.throw(f"{name} purpose={doc.purpose!r} is not Manufacture", frappe.ValidationError)
		rows.append(doc)
	checksum = _checksum_vouchers(HRC00012_MANUFACTURE_VOUCHERS)
	return {
		"package_id": PACKAGE_ID,
		"company": company,
		"vouchers": [r.name for r in rows],
		"manifest_checksum": checksum,
		"idempotency_key": f"{IDEMPOTENCY_PREFIX}{PACKAGE_ID}|checksum={checksum}",
		"count": len(rows),
	}


def _fetch_posted_gl(voucher_no: str, company: str) -> list[dict]:
	return frappe.db.sql(
		"""
		SELECT account, debit, credit, against, cost_center, remarks
		FROM `tabGL Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s
		  AND company=%s AND IFNULL(is_cancelled, 0)=0
		ORDER BY account, against, name
		""",
		(voucher_no, company),
		as_dict=True,
	)


def _fetch_sle_net(voucher_no: str) -> float:
	return float(
		frappe.db.sql(
			"""
			SELECT IFNULL(SUM(stock_value_difference), 0)
			FROM `tabStock Ledger Entry`
			WHERE voucher_type='Stock Entry' AND voucher_no=%s
			  AND IFNULL(is_cancelled, 0)=0
			""",
			(voucher_no,),
		)[0][0]
		or 0.0
	)


def _expected_gl_for_voucher(voucher_no: str) -> list:
	from erpnext.accounts.general_ledger import toggle_debit_credit_if_negative
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()
	se = frappe.get_doc("Stock Entry", voucher_no)
	gl = toggle_debit_credit_if_negative(se.get_gl_entries(se.get_inventory_account_map()))
	return gl


def _account_table(before: dict[str, dict], after: dict[str, dict]) -> list[dict]:
	accounts = sorted(set(before) | set(after))
	rows = []
	for account in accounts:
		b = before.get(account, {"debit": 0.0, "credit": 0.0, "net": 0.0})
		a = after.get(account, {"debit": 0.0, "credit": 0.0, "net": 0.0})
		rows.append(
			{
				"account": account,
				"before_debit": b["debit"],
				"before_credit": b["credit"],
				"before_net": b["net"],
				"after_debit": a["debit"],
				"after_credit": a["credit"],
				"after_net": a["net"],
			}
		)
	return rows


def preflight_manufacture_gl_reclass(
	company: str,
	*,
	vouchers: list[str] | None = None,
) -> dict[str, Any]:
	"""Read-only before/after GL preview for normalized Manufacture GL."""
	manifest = discover_package(company)
	sa = stock_adjustment_account(company)
	allow = set(manifest["vouchers"])
	selected = list(vouchers) if vouchers else list(manifest["vouchers"])
	for name in selected:
		if name not in allow:
			frappe.throw(
				f"{name} is outside HRC-00012 allowlist — refusing expansion",
				frappe.ValidationError,
			)

	per_voucher = []
	agg_before_sa = {"debit": 0.0, "credit": 0.0, "net": 0.0}
	agg_after_sa = {"debit": 0.0, "credit": 0.0, "net": 0.0}

	for name in selected:
		se = frappe.get_doc("Stock Entry", name)
		before_rows = _fetch_posted_gl(name, company)
		before = summarize_gl_by_account(before_rows)
		expected_rows = _expected_gl_for_voucher(name)
		after = summarize_gl_by_account(expected_rows)
		sa_check = assert_sa_is_residual_only(expected_rows, company)
		sle_net = _fetch_sle_net(name)
		add = flt(se.total_additional_costs)
		residual = manufacture_expected_sa_residual_net(se)
		tot_dr = sum(v["debit"] for v in after.values())
		tot_cr = sum(v["credit"] for v in after.values())
		b_sa = before.get(sa, {"debit": 0.0, "credit": 0.0, "net": 0.0})
		a_sa = after.get(sa, {"debit": 0.0, "credit": 0.0, "net": 0.0})
		sa_rows_before = sum(
			1 for r in before_rows if (r.get("account") or "") == sa and (flt(r.debit) + flt(r.credit)) >= 0.5
		)
		# Posted GL may consolidate; count non-zero SA rows from before summary sides.
		if abs(b_sa["debit"]) >= 0.5 and abs(b_sa["credit"]) >= 0.5:
			sa_rows_before_effective = 2
		elif abs(b_sa["debit"]) + abs(b_sa["credit"]) >= 0.5:
			sa_rows_before_effective = max(sa_rows_before, 1)
		else:
			sa_rows_before_effective = 0
		sa_rows_after = count_sa_gl_rows(expected_rows, company)
		for bucket, src in ((agg_before_sa, b_sa), (agg_after_sa, a_sa)):
			bucket["debit"] += src["debit"]
			bucket["credit"] += src["credit"]
			bucket["net"] += src["net"]

		oh = "511101 - سربار - E"
		wip = "111701 - کالای درجریان ساخت - E"
		cogm = "511009 - قیمت تمام شده کالای ساخته شده - E"
		per_voucher.append(
			{
				"voucher": name,
				"posting_date": str(se.posting_date),
				"sle_net": sle_net,
				"total_additional_costs": add,
				"expected_sa_residual_net": residual,
				"balanced_after": abs(tot_dr - tot_cr) < 0.5,
				"total_debit_after": tot_dr,
				"total_credit_after": tot_cr,
				"sa_rows_before": sa_rows_before_effective,
				"sa_rows_after": sa_rows_after,
				"sa_residual_only_after": bool(sa_check.get("ok")),
				"sa_check": sa_check,
				"has_cogm_clearing_after": abs(after.get(cogm, {}).get("debit", 0))
				+ abs(after.get(cogm, {}).get("credit", 0))
				>= 0.5,
				"accounts": _account_table(before, after),
				"stock_adjustment": {"before": b_sa, "after": a_sa},
				"overhead_511101": {
					"before": before.get(oh, {"debit": 0.0, "credit": 0.0, "net": 0.0}),
					"after": after.get(oh, {"debit": 0.0, "credit": 0.0, "net": 0.0}),
				},
				"wip_111701": {
					"before": before.get(wip, {"debit": 0.0, "credit": 0.0, "net": 0.0}),
					"after": after.get(wip, {"debit": 0.0, "credit": 0.0, "net": 0.0}),
				},
				"already_normalized": any(
					SA_RESIDUAL_REMARK_TOKEN in str(r.get("remarks") or "")
					or "IRR_MFG_NORMALIZED_GL" in str(r.get("remarks") or "")
					for r in before_rows
				),
			}
		)

	return {
		"package_id": PACKAGE_ID,
		"design": "normalized_manufacture_gl_residual_only_sa",
		"dry_run": True,
		"company": company,
		"stock_adjustment_account": sa,
		"manifest": manifest,
		"vouchers": per_voucher,
		"aggregate": {
			"stock_adjustment": {"before": agg_before_sa, "after": agg_after_sa},
			"sa_gross_turnover_before": agg_before_sa["debit"] + agg_before_sa["credit"],
			"sa_gross_turnover_after": agg_after_sa["debit"] + agg_after_sa["credit"],
			"sa_net_before": agg_before_sa["net"],
			"sa_net_after": agg_after_sa["net"],
		},
		"finance_approval_required": True,
		"apply_blocked_until_confirm": True,
		"notes": [
			"No historical write performed.",
			"Normalized GL: inventory SLE + OH credits + single SA residual.",
			"COGM 511009 is NOT used as a clearing bridge.",
			"Apply requires dry_run=False and confirm_apply=True.",
		],
	}


def apply_manufacture_gl_reclass(
	company: str,
	*,
	dry_run: bool = True,
	confirm_apply: bool = False,
	update_sed_expense_account: bool = False,
	vouchers: list[str] | None = None,
) -> dict[str, Any]:
	"""Apply GL-only rebuild. Default dry-run. SED expense rewrite off by default."""
	_ = update_sed_expense_account
	preview = preflight_manufacture_gl_reclass(company, vouchers=vouchers)
	if dry_run or not confirm_apply:
		preview["written"] = False
		preview["blocked_reason"] = "dry_run" if dry_run else "confirm_apply_required"
		return preview

	from erpnext_extensions.iran_accounting.manufacturing_dimension.repair import (
		rebuild_gl_only_for_stock_entry,
	)

	results = []
	for row in preview["vouchers"]:
		name = row["voucher"]
		se = frappe.get_doc("Stock Entry", name)
		rebuild = rebuild_gl_only_for_stock_entry(se)
		after_rows = _fetch_posted_gl(name, company)
		after = summarize_gl_by_account(after_rows)
		sa = preview["stock_adjustment_account"]
		results.append(
			{
				"voucher": name,
				"rebuild": rebuild,
				"stock_adjustment_after": after.get(sa),
				"sa_check": assert_sa_is_residual_only(after_rows, company),
				"idempotency": f"{IDEMPOTENCY_PREFIX}{PACKAGE_ID}|{name}",
			}
		)
	frappe.db.commit()
	return {
		**preview,
		"dry_run": False,
		"written": True,
		"confirm_apply": True,
		"applied_at": str(now_datetime()),
		"results": results,
		"finance_approval_required": False,
	}


def dump_preflight_json(company: str, path: str | None = None) -> str:
	report = preflight_manufacture_gl_reclass(company)
	text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
	if path:
		with open(path, "w", encoding="utf-8") as fh:
			fh.write(text)
	return text
