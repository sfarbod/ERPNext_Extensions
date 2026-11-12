# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Full Costing QA runner for v5.7.27 on development.localhost.

Creates/runs Farvardin & Ordibehesht 1405 calculations and lifecycle checks.
Does not modify source ERP transactions or Expense Account Dimension Rules.
"""

from __future__ import annotations

import json
import traceback
from decimal import Decimal
from pathlib import Path

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.fg_cost_allocation._cleanup_regen import inventory
from erpnext_extensions.iran_accounting.fg_cost_allocation.engine import (
	calculate_monthly,
	delete_monthly,
	set_applied_batch_share,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.gl_extract import (
	detect_overlapping_rules,
	load_enabled_rules,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.period import resolve_persian_month

COMPANY = "اسپاد فارمد دارو"
SETTINGS = COMPANY
OUT = Path("/tmp/costing_qa_v5727")
EVIDENCE = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/private/costing_qa_v5727"
)


def _snap(name: str) -> dict:
	doc = frappe.get_doc("Monthly FG Cost Calculation", name)
	rev = cint(doc.revision)
	filters = {"monthly_calculation": name, "revision": rev}
	return {
		"name": name,
		"persian_period": doc.persian_period,
		"status": doc.status,
		"revision": rev,
		"is_consistent": cint(doc.is_consistent),
		"total_identified_expenses": flt(doc.total_identified_expenses),
		"total_allocated_expenses": flt(doc.total_allocated_expenses),
		"unallocated_amount": flt(doc.unallocated_amount),
		"excluded_gl_amount": flt(doc.excluded_gl_amount),
		"gl_entry_count": cint(doc.gl_entry_count),
		"pool_count": cint(doc.pool_count),
		"batch_count": cint(doc.batch_count),
		"product_count": cint(doc.product_count),
		"rule_count": cint(doc.rule_count),
		"exception_count": cint(doc.exception_count),
		"missing_uom_count": cint(getattr(doc, "missing_uom_count", 0) or 0),
		"share_variance": flt(doc.share_variance),
		"over_allocated": cint(getattr(doc, "over_allocated", 0) or 0),
		"shares_are_provisional": cint(getattr(doc, "shares_are_provisional", 0) or 0),
		"detail_counts": {
			"expense_pool": frappe.db.count("MFG Cost Expense Pool", filters),
			"gl_line": frappe.db.count("MFG Cost GL Line", filters),
			"batch": frappe.db.count("MFG Cost Batch Allocation", filters),
			"batch_expense": frappe.db.count("MFG Cost Batch Expense Allocation", filters),
			"product": frappe.db.count("MFG Cost Product Allocation", filters),
			"product_expense": frappe.db.count("MFG Cost Product Expense Allocation", filters),
			"recon": frappe.db.count("MFG Cost Reconciliation Check", filters),
			"exception": frappe.db.count("MFG Cost Exception", filters),
		},
	}


def _ensure_monthly(year: int, month: int) -> str:
	existing = frappe.db.get_value(
		"Monthly FG Cost Calculation",
		{"company": COMPANY, "persian_year": year, "persian_month": month},
		"name",
	)
	if existing:
		return existing
	period = resolve_persian_month(COMPANY, year, month)
	doc = frappe.get_doc(
		{
			"doctype": "Monthly FG Cost Calculation",
			"company": COMPANY,
			"persian_year": year,
			"persian_month": month,
			"persian_period": f"{year:04d}-{month:02d}",
			"persian_month_name": period.persian_month_name,
			"start_date": period.start_date,
			"end_date": period.end_date,
			"period_key": period.period_key,
			"status": "Draft",
			"revision": 0,
			"retain_manual_overrides_default": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _source_fingerprint() -> dict:
	return {
		"GL Entry": frappe.db.count("GL Entry"),
		"Stock Ledger Entry": frappe.db.count("Stock Ledger Entry"),
		"Stock Entry": frappe.db.count("Stock Entry"),
		"Work Order": frappe.db.count("Work Order"),
		"Batch": frappe.db.count("Batch"),
		"Item": frappe.db.count("Item"),
		"UOM Conversion Detail": frappe.db.count("UOM Conversion Detail"),
		"Expense Account Dimension Rule": frappe.db.count("Expense Account Dimension Rule"),
		"FG Item Primary UOM": frappe.db.count("FG Item Primary UOM"),
	}


def _product_sample(name: str, rev: int, limit: int = 5) -> list[dict]:
	rows = frappe.get_all(
		"MFG Cost Product Allocation",
		filters={"monthly_calculation": name, "revision": rev},
		fields=[
			"name",
			"batch_no",
			"item_code",
			"actual_qty",
			"normalized_qty",
			"product_share",
			"product_share_pct",
			"material_consumption_value",
			"product_allocated_expense",
			"total_product_cost",
			"unit_cost_on_actual",
			"unit_cost_on_normalized",
			"selling_price",
			"gross_difference",
			"gross_margin_pct",
			"missing_uom_factor",
			"price_status",
		],
		limit_page_length=limit,
		order_by="total_product_cost desc",
	)
	return rows


def _bx_sample(name: str, rev: int, limit: int = 5) -> list[dict]:
	return frappe.get_all(
		"MFG Cost Batch Expense Allocation",
		filters={"monthly_calculation": name, "revision": rev},
		fields=[
			"name",
			"batch_no",
			"account",
			"department",
			"cost_center",
			"pool_monthly_net_expense",
			"calculated_batch_share",
			"applied_batch_share",
			"allocated_amount",
			"is_manual_override",
		],
		limit_page_length=limit,
		order_by="allocated_amount desc",
	)


def _compare_snaps(a: dict, b: dict) -> dict:
	keys = [
		"total_identified_expenses",
		"total_allocated_expenses",
		"unallocated_amount",
		"excluded_gl_amount",
		"gl_entry_count",
		"pool_count",
		"batch_count",
		"product_count",
		"detail_counts",
	]
	diff = {}
	for k in keys:
		if a.get(k) != b.get(k):
			diff[k] = {"first": a.get(k), "second": b.get(k)}
	return diff


def run_month(year: int, month: int) -> dict:
	period_key = f"{year:04d}-{month:02d}"
	src_before = _source_fingerprint()
	name = _ensure_monthly(year, month)
	period = resolve_persian_month(COMPANY, year, month)

	# First calculation
	calc1 = calculate_monthly(name, retain_overrides=0)
	snap1 = _snap(name)
	src_after_calc = _source_fingerprint()

	# Idempotent second calculation
	calc2 = calculate_monthly(name, retain_overrides=1)
	snap2 = _snap(name)
	idempotency_diff = _compare_snaps(snap1, snap2)

	# Applied Batch Share edit
	share_edit = {"ok": False}
	bx_rows = _bx_sample(name, snap2["revision"], limit=1)
	if bx_rows:
		bx = bx_rows[0]
		old_share = flt(bx.applied_batch_share)
		old_alloc = flt(bx.allocated_amount)
		new_share = max(old_share * 0.5, 0.0001) if old_share else 0.01
		try:
			edit_result = set_applied_batch_share(
				batch_expense_allocation=bx.name,
				applied_share=new_share,
				reason="QA Applied Batch Share edit test",
				commit=1,
			)
			bx_after = frappe.get_doc("MFG Cost Batch Expense Allocation", bx.name)
			parent_after = frappe.get_doc("Monthly FG Cost Calculation", name)
			from decimal import Decimal, ROUND_HALF_UP

			from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision

			currency = parent_after.currency or frappe.get_cached_value(
				"Company", parent_after.company, "default_currency"
			)
			precision = get_currency_precision(currency)
			q = Decimal("1") if precision <= 0 else Decimal("1").scaleb(-precision)
			expected = float(
				(
					Decimal(str(flt(bx_after.pool_monthly_net_expense)))
					* Decimal(str(flt(bx_after.applied_batch_share)))
				).quantize(q, rounding=ROUND_HALF_UP)
			)
			calc_share_unchanged = abs(
				flt(bx_after.calculated_batch_share) - flt(bx.calculated_batch_share)
			) < 1e-12
			share_edit = {
				"ok": abs(flt(bx_after.allocated_amount) - expected) < 0.01
				and calc_share_unchanged
				and flt(bx_after.applied_batch_share) == flt(new_share),
				"calculated_share_readonly": calc_share_unchanged,
				"bx": bx.name,
				"old_share": old_share,
				"new_share": flt(bx_after.applied_batch_share),
				"old_allocated": old_alloc,
				"new_allocated": flt(bx_after.allocated_amount),
				"expected_allocated": expected,
				"parent_total_allocated": flt(parent_after.total_allocated_expenses),
				"parent_status": parent_after.status,
				"engine_result_keys": list((edit_result or {}).keys())[:20],
			}
			# restore via recalculation for clean baseline after edit test
			calculate_monthly(name, retain_overrides=0)
			snap_after_restore = _snap(name)
		except Exception as e:
			share_edit = {"ok": False, "error": str(e), "trace": traceback.format_exc()[-1500:]}
			snap_after_restore = _snap(name)
			try:
				calculate_monthly(name, retain_overrides=0)
				snap_after_restore = _snap(name)
			except Exception:
				pass
	else:
		snap_after_restore = snap2

	# Invalid share boundary
	invalid_share = {"ok": True}
	bx_rows2 = _bx_sample(name, snap_after_restore["revision"], limit=1)
	if bx_rows2:
		try:
			set_applied_batch_share(
				batch_expense_allocation=bx_rows2[0].name,
				applied_share=-1,
				reason="QA negative share",
				commit=1,
			)
			invalid_share = {"ok": False, "error": "negative share was accepted"}
		except Exception as e:
			invalid_share = {"ok": True, "rejected": True, "message": str(e)[:300]}

	# Delete and recreate
	baseline_before_delete = _snap(name)
	src_before_delete = _source_fingerprint()
	deleted = delete_monthly(name)
	orphans = {
		dt: frappe.db.count(dt, {"monthly_calculation": name})
		for dt in [
			"MFG Cost Expense Pool",
			"MFG Cost GL Line",
			"MFG Cost Batch Allocation",
			"MFG Cost Batch Expense Allocation",
			"MFG Cost Product Allocation",
			"MFG Cost Product Expense Allocation",
			"MFG Cost Reconciliation Check",
			"MFG Cost Exception",
			"MFG Cost Calculation Revision",
		]
	}
	src_after_delete = _source_fingerprint()
	# recreate
	name2 = _ensure_monthly(year, month)
	calculate_monthly(name2, retain_overrides=0)
	snap_recreated = _snap(name2)
	recreate_diff = _compare_snaps(
		{k: baseline_before_delete[k] for k in baseline_before_delete if k != "name"},
		{k: snap_recreated[k] for k in snap_recreated if k != "name"},
	)
	# financial totals should match (revision may differ)
	financial_keys = [
		"total_identified_expenses",
		"total_allocated_expenses",
		"unallocated_amount",
		"excluded_gl_amount",
		"gl_entry_count",
		"pool_count",
		"batch_count",
		"product_count",
	]
	financial_match = {
		k: abs(flt(baseline_before_delete.get(k)) - flt(snap_recreated.get(k))) < 0.05
		for k in financial_keys
	}

	src_final = _source_fingerprint()

	return {
		"period": period_key,
		"period_dates": {
			"start": str(period.start_date),
			"end": str(period.end_date),
			"name": period.persian_month_name,
		},
		"monthly_name": name2,
		"first_run": snap1,
		"second_run": snap2,
		"idempotency_diff": idempotency_diff,
		"idempotent_financials": not any(
			k in idempotency_diff
			for k in [
				"total_identified_expenses",
				"total_allocated_expenses",
				"gl_entry_count",
				"pool_count",
			]
		),
		"share_edit": share_edit,
		"invalid_share": invalid_share,
		"delete_recreate": {
			"deleted": deleted,
			"orphans": orphans,
			"orphans_clean": all(v == 0 for v in orphans.values()),
			"source_unchanged_on_delete": src_before_delete == src_after_delete,
			"recreated": snap_recreated,
			"recreate_diff": recreate_diff,
			"financial_match": financial_match,
			"financial_match_ok": all(financial_match.values()),
		},
		"product_sample": _product_sample(name2, snap_recreated["revision"]),
		"bx_sample": _bx_sample(name2, snap_recreated["revision"]),
		"source_fingerprint": {
			"before": src_before,
			"after_calc": src_after_calc,
			"final": src_final,
			"unchanged": src_before == src_final,
		},
		"calc_messages": {"first": calc1, "second": calc2},
	}


def run_failure_cases() -> dict:
	out = {}
	# Overlap detection still works
	rules = load_enabled_rules(SETTINGS)
	from erpnext_extensions.iran_accounting.fg_cost_allocation.gl_extract import RuleSpec

	fake = list(rules[:1])
	if fake:
		a = fake[0]
		b = RuleSpec(
			name="QA-OVERLAP",
			account=a.account,
			department=a.department,
			cost_center="",
			enabled=1,
		)
		# a already has blank CC; inventing same account+dept blank CC is duplicate/overlap
		pairs = detect_overlapping_rules([a, b])
		out["overlap_detection"] = {"pairs": len(pairs), "ok": len(pairs) >= 1}

	# Missing selling price list — product selling price should be missing/0 without crashing
	settings = frappe.get_doc("Finished Goods Cost Allocation Settings", SETTINGS)
	out["selling_price_list"] = settings.get("selling_price_list") or ""

	# Consumable rejection for Incomplete
	from erpnext_extensions.iran_accounting.fg_cost_allocation.contract import (
		FGCostAllocationNotConsumable,
		assert_consumable,
	)

	cals = frappe.get_all(
		"Monthly FG Cost Calculation",
		filters={"company": COMPANY, "persian_year": 1405, "persian_month": ["in", [1, 2]]},
		fields=["name", "status", "is_consistent"],
	)
	consumable_checks = []
	for c in cals:
		try:
			assert_consumable(c.name)
			consumable_checks.append({"name": c.name, "consumable": True, "status": c.status})
		except FGCostAllocationNotConsumable as e:
			consumable_checks.append(
				{"name": c.name, "consumable": False, "status": c.status, "reason": str(e)[:200]}
			)
		except Exception as e:
			consumable_checks.append({"name": c.name, "error": str(e)[:200]})
	out["consumable_checks"] = consumable_checks
	return out


def main():
	OUT.mkdir(parents=True, exist_ok=True)
	report = {
		"version": __import__("erpnext_extensions").__version__,
		"site": frappe.local.site,
		"company": COMPANY,
		"inventory_start": inventory(),
		"rules": {
			"count": frappe.db.count("Expense Account Dimension Rule"),
			"enabled": frappe.db.count("Expense Account Dimension Rule", {"enabled": 1}),
			"blank_cc": frappe.db.sql(
				"SELECT COUNT(*) FROM `tabExpense Account Dimension Rule` WHERE IFNULL(cost_center,'')=''"
			)[0][0],
			"accounts": frappe.db.sql(
				"SELECT COUNT(DISTINCT account) FROM `tabExpense Account Dimension Rule`"
			)[0][0],
			"departments": frappe.db.sql(
				"SELECT COUNT(DISTINCT department) FROM `tabExpense Account Dimension Rule`"
			)[0][0],
			"overlaps": len(detect_overlapping_rules(load_enabled_rules(SETTINGS))),
		},
	}

	report["farvardin"] = run_month(1405, 1)
	report["ordibehesht"] = run_month(1405, 2)
	report["failure_cases"] = run_failure_cases()
	report["inventory_end"] = inventory()
	report["source_final"] = _source_fingerprint()

	path = OUT / "qa_report.json"
	path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
	EVIDENCE.mkdir(parents=True, exist_ok=True)
	(EVIDENCE / "qa_report.json").write_text(
		json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
	)
	print(json.dumps({"wrote": str(path), "evidence": str(EVIDENCE / "qa_report.json")}, indent=2))
	# compact summary
	summary = {
		"rules": report["rules"],
		"farvardin": {
			"name": report["farvardin"]["monthly_name"],
			"status": report["farvardin"]["first_run"]["status"],
			"identified": report["farvardin"]["first_run"]["total_identified_expenses"],
			"allocated": report["farvardin"]["first_run"]["total_allocated_expenses"],
			"pools": report["farvardin"]["first_run"]["pool_count"],
			"batches": report["farvardin"]["first_run"]["batch_count"],
			"products": report["farvardin"]["first_run"]["product_count"],
			"idempotent": report["farvardin"]["idempotent_financials"],
			"share_edit_ok": report["farvardin"]["share_edit"].get("ok"),
			"delete_recreate_ok": report["farvardin"]["delete_recreate"]["financial_match_ok"],
			"orphans_clean": report["farvardin"]["delete_recreate"]["orphans_clean"],
			"source_unchanged": report["farvardin"]["source_fingerprint"]["unchanged"],
		},
		"ordibehesht": {
			"name": report["ordibehesht"]["monthly_name"],
			"status": report["ordibehesht"]["first_run"]["status"],
			"identified": report["ordibehesht"]["first_run"]["total_identified_expenses"],
			"allocated": report["ordibehesht"]["first_run"]["total_allocated_expenses"],
			"pools": report["ordibehesht"]["first_run"]["pool_count"],
			"batches": report["ordibehesht"]["first_run"]["batch_count"],
			"products": report["ordibehesht"]["first_run"]["product_count"],
			"idempotent": report["ordibehesht"]["idempotent_financials"],
			"share_edit_ok": report["ordibehesht"]["share_edit"].get("ok"),
			"delete_recreate_ok": report["ordibehesht"]["delete_recreate"]["financial_match_ok"],
			"orphans_clean": report["ordibehesht"]["delete_recreate"]["orphans_clean"],
			"source_unchanged": report["ordibehesht"]["source_fingerprint"]["unchanged"],
		},
	}
	print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
	return report
