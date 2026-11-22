# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Orchestrate MFG Cost Control Credit Allocation calculate / persist / cancel."""

from __future__ import annotations

from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision
from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.allocate_credit import (
	build_credit_allocation_plan,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.identify import (
	identify_control_credits,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.integrate_pa import (
	clear_credit_shares_for_period,
	apply_credit_shares_to_product_allocations,
)


def _money(value, currency: str) -> float:
	precision = get_currency_precision(currency)
	q = Decimal("1") if precision <= 0 else Decimal("1").scaleb(-precision)
	return float(Decimal(str(value or 0)).quantize(q))


def _assert_editable(doc):
	if cint(doc.docstatus) == 1 or doc.status == "Submitted":
		frappe.throw(_("Submitted Control Credit Allocation cannot be recalculated. Cancel or amend."))
	if doc.status == "Cancelled" or cint(doc.docstatus) == 2:
		frappe.throw(_("Cancelled Control Credit Allocation cannot be recalculated."))


def calculate_control_credit_allocation(name: str, *, commit: bool = True) -> dict:
	"""Identify credits, allocate with Batch Expense shares, persist children."""
	doc = frappe.get_doc("MFG Cost Control Credit Allocation", name)
	doc.check_permission("write")
	_assert_editable(doc)

	identification = identify_control_credits(
		company=doc.company,
		persian_year=cint(doc.persian_year),
		persian_month=cint(doc.persian_month),
		currency=doc.currency,
		exclude_allocation=doc.name,
	)
	# Block silent allocation when unresolved sources exist with non-zero amounts
	if identification.unresolved_count and (
		identification.labor_unresolved or identification.overhead_unresolved
	):
		# Still calculate, but mark document inconsistent and keep unresolved rows visible
		pass

	plan = build_credit_allocation_plan(identification, currency=doc.currency)
	ccy = plan.currency
	precision = get_currency_precision(ccy)

	doc.monthly_calculation = plan.monthly_calculation
	doc.costing_revision = plan.revision
	doc.period_start = identification.period_start
	doc.period_end = identification.period_end
	doc.overhead_account = identification.accounts.overhead_account
	doc.labor_account = identification.accounts.labor_account
	doc.currency = ccy

	doc.labor_period_credit = _money(identification.labor_period_credit, ccy)
	doc.overhead_period_credit = _money(identification.overhead_period_credit, ccy)
	doc.labor_consumed_by_product_allocation = _money(identification.labor_consumed_by_pa, ccy)
	doc.overhead_consumed_by_product_allocation = _money(identification.overhead_consumed_by_pa, ccy)
	doc.labor_eligible_credit = _money(identification.labor_eligible, ccy)
	doc.overhead_eligible_credit = _money(identification.overhead_eligible, ccy)
	doc.labor_unresolved_amount = _money(identification.labor_unresolved, ccy)
	doc.overhead_unresolved_amount = _money(identification.overhead_unresolved, ccy)
	doc.labor_allocated = _money(plan.labor_allocated_total, ccy)
	doc.overhead_allocated = _money(plan.overhead_allocated_total, ccy)
	# Remaining vs Monthly Credit Amount (allocation base), not Eligible Credit
	doc.labor_remaining = _money(
		Decimal(str(doc.labor_period_credit)) - Decimal(str(doc.labor_allocated)), ccy
	)
	doc.overhead_remaining = _money(
		Decimal(str(doc.overhead_period_credit)) - Decimal(str(doc.overhead_allocated)), ccy
	)
	doc.source_gl_count = identification.gl_entry_count
	doc.source_voucher_count = identification.voucher_count
	doc.unresolved_source_count = identification.unresolved_count
	doc.batch_row_count = len(plan.batch_rows)
	doc.product_row_count = len(plan.product_rows)
	doc.calculated_on = now_datetime()
	doc.calculated_by = frappe.session.user
	doc.source_identified_on = now_datetime()
	doc.status = "Calculated"
	doc.has_unresolved_sources = 1 if identification.unresolved_count else 0

	# Sources — keep classification visible; pool allocation is Monthly Credit (not line-gated).
	# current_allocated_amount records each line's contribution to the gross monthly base.
	# Classification (eligible / unresolved / PA-consumed / excluded) stays informational.
	doc.set("sources", [])
	for idx, s in enumerate(identification.sources, start=1):
		contrib = s.landed_cost_amount if (s.landed_cost_row or s.landed_cost_amount) else s.credit
		doc.append(
			"sources",
			{
				"gl_entry": s.gl_entry,
				"posting_date": s.posting_date,
				"account": s.account,
				"account_kind": s.account_kind,
				"voucher_type": s.voucher_type,
				"voucher_no": s.voucher_no,
				"cost_center": s.cost_center,
				"department": s.department,
				"debit": float(s.debit),
				"credit": float(s.credit),
				"landed_cost_row": s.landed_cost_row,
				"landed_cost_amount": float(s.landed_cost_amount),
				"eligible_amount": float(s.eligible_amount),
				"previously_allocated_amount": float(s.previously_allocated_amount),
				"current_allocated_amount": float(contrib),
				"remaining_amount": 0,
				"eligibility_status": s.eligibility_status,
				"exclusion_reason": s.exclusion_reason,
				"product_allocation": s.product_allocation,
				"description": s.description,
				"idx": idx,
			},
		)

	doc.set("batch_rows", [])
	for idx, b in enumerate(plan.batch_rows, start=1):
		doc.append(
			"batch_rows",
			{
				"batch_no": b.batch_no,
				"batch_allocation": b.batch_allocation,
				"applied_batch_share": float(b.applied_batch_share),
				"calculated_batch_share": float(b.calculated_batch_share),
				"labor_allocated": float(b.labor_allocated),
				"overhead_allocated": float(b.overhead_allocated),
				"total_allocated": float(_money(b.labor_allocated + b.overhead_allocated, ccy)),
				"residual_applied_labor": 1 if b.residual_applied_labor else 0,
				"residual_applied_overhead": 1 if b.residual_applied_overhead else 0,
				"idx": idx,
			},
		)

	doc.set("product_rows", [])
	for idx, p in enumerate(plan.product_rows, start=1):
		doc.append(
			"product_rows",
			{
				"batch_no": p.batch_no,
				"item_code": p.item_code,
				"product_allocation": p.product_allocation,
				"product_share": float(p.product_share),
				"source_document_type": p.source_document_type,
				"source_document": p.source_document,
				"quantity": float(p.quantity),
				"labor_allocated": float(p.labor_allocated),
				"overhead_allocated": float(p.overhead_allocated),
				"total_allocated": float(_money(p.labor_allocated + p.overhead_allocated, ccy)),
				"idx": idx,
			},
		)

	# Reconcile: allocated totals must equal Monthly Credit Amount (allocation base)
	lab_diff = abs(flt(doc.labor_allocated) - flt(doc.labor_period_credit))
	oh_diff = abs(flt(doc.overhead_allocated) - flt(doc.overhead_period_credit))
	tol = 0 if precision <= 0 else 10 ** (-precision)
	doc.reconciliation_difference_labor = lab_diff
	doc.reconciliation_difference_overhead = oh_diff
	doc.is_consistent = 1 if lab_diff <= tol and oh_diff <= tol else 0

	doc.flags.ignore_validate_update_after_submit = True
	doc.save(ignore_permissions=True)

	# Push shares into Product Allocation Document Breakdown (draft/calculated also refresh display)
	# Clears prior credit shares for the monthly calc first — prevents duplicate stacking on recalc.
	pa_result = apply_credit_shares_to_product_allocations(doc)
	over_alloc = _detect_control_over_allocation(doc)
	if over_alloc:
		_emit_over_allocation_warnings(doc, over_alloc)

	if commit:
		frappe.db.commit()

	return {
		"name": doc.name,
		"status": doc.status,
		"labor_period_credit": doc.labor_period_credit,
		"overhead_period_credit": doc.overhead_period_credit,
		"labor_eligible_credit": doc.labor_eligible_credit,
		"overhead_eligible_credit": doc.overhead_eligible_credit,
		"labor_allocated": doc.labor_allocated,
		"overhead_allocated": doc.overhead_allocated,
		"labor_remaining": doc.labor_remaining,
		"overhead_remaining": doc.overhead_remaining,
		"unresolved_source_count": doc.unresolved_source_count,
		"is_consistent": doc.is_consistent,
		"source_gl_count": doc.source_gl_count,
		"batch_row_count": doc.batch_row_count,
		"product_row_count": doc.product_row_count,
		"over_allocation_count": len(over_alloc),
		"product_allocations_updated": (pa_result or {}).get("product_allocations"),
	}


def _detect_control_over_allocation(doc) -> list[dict]:
	"""Find Document Breakdown rows where allocated share exceeds calculated control amount.

	Does not clamp negatives — returns evidence for user-visible warnings.
	"""
	currency = doc.currency or "IRR"
	issues: list[dict] = []
	for row in doc.get("product_rows") or []:
		pa = (row.product_allocation or "").strip()
		src = (row.source_document or "").strip()
		if not pa:
			continue
		children = frappe.get_all(
			"MFG Cost Product Allocation Document Breakdown",
			filters={"parent": pa, "source_document": src} if src else {"parent": pa},
			fields=[
				"name",
				"source_document",
				"labor_amount",
				"overhead_amount",
				"allocated_labor_share",
				"allocated_overhead_share",
				"unallocated_labor",
				"unallocated_overhead",
			],
		)
		for ch in children:
			lab_ctrl = flt(ch.labor_amount)
			oh_ctrl = flt(ch.overhead_amount)
			lab_alloc = flt(ch.allocated_labor_share)
			oh_alloc = flt(ch.allocated_overhead_share)
			if lab_alloc > lab_ctrl:
				issues.append(
					{
						"control_type": "Labor",
						"month": doc.persian_period,
						"account": doc.labor_account,
						"product_allocation": pa,
						"source_document": ch.source_document,
						"calculated_control": lab_ctrl,
						"monthly_credit_allocated_share": lab_alloc,
						"excess": _money(lab_alloc - lab_ctrl, currency),
						"unallocated": flt(ch.unallocated_labor),
					}
				)
			if oh_alloc > oh_ctrl:
				issues.append(
					{
						"control_type": "Overhead",
						"month": doc.persian_period,
						"account": doc.overhead_account,
						"product_allocation": pa,
						"source_document": ch.source_document,
						"calculated_control": oh_ctrl,
						"monthly_credit_allocated_share": oh_alloc,
						"excess": _money(oh_alloc - oh_ctrl, currency),
						"unallocated": flt(ch.unallocated_overhead),
					}
				)
	return issues


def _emit_over_allocation_warnings(doc, issues: list[dict]):
	"""Surface over-allocation without clamping unallocated to zero."""
	sample = issues[:8]
	lines = [
		_(
			"Control credit share exceeds calculated control amount on {0} Document Breakdown row(s)."
		).format(len(issues))
	]
	for i in sample:
		lines.append(
			_(
				"{control_type} | {month} | {account} | PA {pa} | doc {src}: "
				"control={ctrl} allocated_share={alloc} excess={excess} unallocated={unal}"
			).format(
				control_type=i["control_type"],
				month=i["month"],
				account=i["account"],
				pa=i["product_allocation"],
				src=i["source_document"] or "—",
				ctrl=i["calculated_control"],
				alloc=i["monthly_credit_allocated_share"],
				excess=i["excess"],
				unal=i["unallocated"],
			)
		)
	if len(issues) > len(sample):
		lines.append(_("…and {0} more.").format(len(issues) - len(sample)))
	frappe.msgprint(
		"<br>".join(lines),
		title=_("Control Over-Allocation"),
		indicator="orange",
		alert=True,
	)


def on_submit_control_credit(doc):
	doc.status = "Submitted"
	apply_credit_shares_to_product_allocations(doc)


def on_cancel_control_credit(doc):
	doc.status = "Cancelled"
	clear_credit_shares_for_period(
		company=doc.company,
		persian_year=cint(doc.persian_year),
		persian_month=cint(doc.persian_month),
		exclude_allocation=doc.name,
	)
