"""Re-test delete orphan cleanup + share edit after hardening. Farvardin only (fast)."""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision
from erpnext_extensions.iran_accounting.fg_cost_allocation.engine import (
	calculate_monthly,
	delete_monthly,
	set_applied_batch_share,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation._qa_full_v5727 import (
	_ensure_monthly,
	_snap,
	_source_fingerprint,
)

OUT = Path("/tmp/costing_qa_v5727/lifecycle_retest.json")
COMPANY = "اسپاد فارمد دارو"


def main():
	src_before = _source_fingerprint()
	name = _ensure_monthly(1405, 1)
	calculate_monthly(name, retain_overrides=0)
	snap1 = _snap(name)

	bx = frappe.get_all(
		"MFG Cost Batch Expense Allocation",
		filters={"monthly_calculation": name, "revision": snap1["revision"]},
		fields=[
			"name",
			"pool_monthly_net_expense",
			"applied_batch_share",
			"calculated_batch_share",
			"allocated_amount",
		],
		order_by="allocated_amount desc",
		limit_page_length=1,
	)[0]
	new_share = max(flt(bx.applied_batch_share) * 0.5, 0.0001)
	set_applied_batch_share(
		batch_expense_allocation=bx.name,
		applied_share=new_share,
		reason="lifecycle retest share edit",
		commit=1,
	)
	bx_after = frappe.get_doc("MFG Cost Batch Expense Allocation", bx.name)
	parent = frappe.get_doc("Monthly FG Cost Calculation", name)
	currency = parent.currency or frappe.get_cached_value("Company", parent.company, "default_currency")
	precision = get_currency_precision(currency)
	q = Decimal("1") if precision <= 0 else Decimal("1").scaleb(-precision)
	expected = float(
		(
			Decimal(str(flt(bx_after.pool_monthly_net_expense)))
			* Decimal(str(flt(bx_after.applied_batch_share)))
		).quantize(q, rounding=ROUND_HALF_UP)
	)
	share_ok = abs(flt(bx_after.allocated_amount) - expected) < 0.01
	calc_readonly = abs(flt(bx_after.calculated_batch_share) - flt(bx.calculated_batch_share)) < 1e-12

	# restore
	calculate_monthly(name, retain_overrides=0)
	baseline = _snap(name)
	src_before_del = _source_fingerprint()
	delete_monthly(name)
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
			"MFG Cost Account Dimension Summary",
		]
	}
	src_after_del = _source_fingerprint()
	name2 = _ensure_monthly(1405, 1)
	calculate_monthly(name2, retain_overrides=0)
	snap2 = _snap(name2)
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
		k: abs(flt(baseline.get(k)) - flt(snap2.get(k))) < 0.05 for k in financial_keys
	}
	src_final = _source_fingerprint()
	report = {
		"share_edit_ok": share_ok and calc_readonly,
		"share_edit": {
			"ok": share_ok,
			"calculated_share_readonly": calc_readonly,
			"expected": expected,
			"actual": flt(bx_after.allocated_amount),
			"new_share": flt(bx_after.applied_batch_share),
		},
		"orphans": orphans,
		"orphans_clean": all(v == 0 for v in orphans.values()),
		"source_unchanged_on_delete": src_before_del == src_after_del,
		"financial_match_ok": all(financial_match.values()),
		"financial_match": financial_match,
		"recreated": snap2,
		"source_unchanged": src_before == src_final,
		"rules": src_final["Expense Account Dimension Rule"],
		"monthly_name": name2,
	}
	OUT.parent.mkdir(parents=True, exist_ok=True)
	OUT.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
	print(json.dumps(report, indent=2, default=str))
	return report
