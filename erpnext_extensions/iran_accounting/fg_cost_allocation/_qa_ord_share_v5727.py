"""Ordibehesht Applied Batch Share edit verification after rounding fix."""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision
from erpnext_extensions.iran_accounting.fg_cost_allocation.engine import (
	calculate_monthly,
	set_applied_batch_share,
)


def main():
	name = frappe.db.get_value(
		"Monthly FG Cost Calculation",
		{"company": "اسپاد فارمد دارو", "persian_year": 1405, "persian_month": 2},
		"name",
	)
	if not name:
		return {"ok": False, "error": "Ordibehesht monthly missing"}
	# ensure clean calculated baseline
	calculate_monthly(name, retain_overrides=0)
	rev = frappe.db.get_value("Monthly FG Cost Calculation", name, "revision")
	bx = frappe.get_all(
		"MFG Cost Batch Expense Allocation",
		filters={"monthly_calculation": name, "revision": rev},
		fields=[
			"name",
			"pool_monthly_net_expense",
			"applied_batch_share",
			"calculated_batch_share",
			"allocated_amount",
			"batch_no",
		],
		order_by="allocated_amount desc",
		limit_page_length=1,
	)[0]
	old_calc = flt(bx.calculated_batch_share)
	new_share = max(flt(bx.applied_batch_share) * 0.5, 0.0001)
	set_applied_batch_share(
		batch_expense_allocation=bx.name,
		applied_share=new_share,
		reason="Ordibehesht QA share edit",
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
	# other batches not rebalanced: pick another bx row same pool if any
	other = frappe.get_all(
		"MFG Cost Batch Expense Allocation",
		filters={"monthly_calculation": name, "revision": rev, "batch_no": ["!=", bx.batch_no]},
		fields=["name", "applied_batch_share", "calculated_batch_share"],
		limit_page_length=1,
	)
	# restore
	calculate_monthly(name, retain_overrides=0)
	parent2 = frappe.get_doc("Monthly FG Cost Calculation", name)
	out = {
		"monthly": name,
		"share_edit_ok": abs(flt(bx_after.allocated_amount) - expected) < 0.01,
		"calculated_readonly": abs(flt(bx_after.calculated_batch_share) - old_calc) < 1e-12,
		"expected": expected,
		"actual": flt(bx_after.allocated_amount),
		"parent_status_after_edit": parent.status,
		"parent_total_after_edit": flt(parent.total_allocated_expenses),
		"restored_status": parent2.status,
		"restored_consistent": parent2.is_consistent,
		"other_batch_sample": other[0] if other else None,
	}
	Path("/tmp/costing_qa_v5727/ord_share.json").write_text(
		json.dumps(out, indent=2, default=str), encoding="utf-8"
	)
	print(json.dumps(out, indent=2, default=str))
	return out
