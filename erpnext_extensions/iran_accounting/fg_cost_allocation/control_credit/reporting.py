# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Read-only reporting helpers for Control Credit pools and batch/product details.

Uses persisted MFG Cost Control Credit Allocation documents and children only.
Does not recalculate GL credits or change allocation methodology.
"""

from __future__ import annotations

from frappe.utils import cint, flt


def _pool_status(doc_status: str, monthly_credit: float, allocated: float, remaining: float) -> str:
	"""Derive pool allocation status from Monthly Credit Amount (allocation base)."""
	if doc_status == "Cancelled":
		return "Cancelled"
	if doc_status == "Draft":
		return "Draft"
	# Calculated or Submitted — status keyed off Monthly Credit Amount, not Eligible
	if flt(monthly_credit) <= 0 and flt(allocated) <= 0:
		return "No Monthly Credit"
	if flt(remaining) <= 0 and flt(allocated) > 0:
		return "Fully Allocated"
	if flt(allocated) > 0 and flt(remaining) > 0:
		return "Partially Allocated"
	if doc_status == "Submitted":
		return "Submitted"
	return "Calculated"


def fetch_control_credit_pools(filters: dict | None = None) -> list[dict]:
	"""One row per (allocation document × Labor|Overhead) from persisted parent fields."""
	import frappe

	filters = filters or {}
	conds = ["docstatus < 2"]
	values: dict = {}
	if filters.get("company"):
		conds.append("company = %(company)s")
		values["company"] = filters["company"]
	if filters.get("persian_period"):
		conds.append("persian_period = %(persian_period)s")
		values["persian_period"] = filters["persian_period"]
	if filters.get("persian_year"):
		conds.append("persian_year = %(persian_year)s")
		values["persian_year"] = cint(filters["persian_year"])
	if filters.get("persian_month"):
		conds.append("persian_month = %(persian_month)s")
		values["persian_month"] = cint(filters["persian_month"])
	if filters.get("allocation_status"):
		# Filter on document status; derived pool status applied after
		conds.append("status = %(allocation_status)s")
		values["allocation_status"] = filters["allocation_status"]
	if filters.get("credit_allocation"):
		conds.append("name = %(credit_allocation)s")
		values["credit_allocation"] = filters["credit_allocation"]

	docs = frappe.db.sql(
		f"""
		SELECT name, company, persian_year, persian_month, persian_period, status, docstatus,
		       currency, labor_account, overhead_account,
		       labor_period_credit, overhead_period_credit,
		       labor_consumed_by_product_allocation, overhead_consumed_by_product_allocation,
		       labor_eligible_credit, overhead_eligible_credit,
		       labor_allocated, overhead_allocated,
		       labor_remaining, overhead_remaining,
		       labor_unresolved_amount, overhead_unresolved_amount,
		       source_gl_count, source_voucher_count
		FROM `tabMFG Cost Control Credit Allocation`
		WHERE {' AND '.join(conds)}
		ORDER BY persian_year, persian_month, company, name
		""",
		values,
		as_dict=True,
	)

	control_type = (filters.get("control_type") or "").strip().lower()
	control_account = (filters.get("control_account") or "").strip()
	derived_status_filter = (filters.get("pool_status") or "").strip()

	rows: list[dict] = []
	for d in docs:
		# Source counts by kind from persisted children
		kind_counts = frappe.db.sql(
			"""
			SELECT account_kind, COUNT(*) AS n
			FROM `tabMFG Cost Control Credit Source`
			WHERE parent = %s
			GROUP BY account_kind
			""",
			d.name,
			as_dict=True,
		)
		count_map = {r.account_kind: cint(r.n) for r in kind_counts}

		candidates = [
			{
				"control_type": "Labor",
				"control_account": d.labor_account,
				"period_credit": flt(d.labor_period_credit),
				"previously_allocated": flt(d.labor_consumed_by_product_allocation),
				"eligible_credit": flt(d.labor_eligible_credit),
				"allocated_amount": flt(d.labor_allocated),
				"remaining_amount": flt(d.labor_remaining),
				"unresolved_amount": flt(d.labor_unresolved_amount),
				"source_count": count_map.get("labor", 0),
			},
			{
				"control_type": "Overhead",
				"control_account": d.overhead_account,
				"period_credit": flt(d.overhead_period_credit),
				"previously_allocated": flt(d.overhead_consumed_by_product_allocation),
				"eligible_credit": flt(d.overhead_eligible_credit),
				"allocated_amount": flt(d.overhead_allocated),
				"remaining_amount": flt(d.overhead_remaining),
				"unresolved_amount": flt(d.overhead_unresolved_amount),
				"source_count": count_map.get("overhead", 0),
			},
		]
		for c in candidates:
			if control_type and c["control_type"].lower() != control_type:
				continue
			if control_account and c["control_account"] != control_account:
				continue
			pool_status = _pool_status(
				d.status, c["period_credit"], c["allocated_amount"], c["remaining_amount"]
			)
			if derived_status_filter and pool_status != derived_status_filter:
				continue
			rows.append(
				{
					"company": d.company,
					"persian_period": d.persian_period,
					"persian_year": d.persian_year,
					"persian_month": d.persian_month,
					"control_type": c["control_type"],
					"control_account": c["control_account"],
					"monthly_credit_amount": c["period_credit"],
					"previously_allocated": c["previously_allocated"],
					"eligible_credit": c["eligible_credit"],
					"allocated_amount": c["allocated_amount"],
					"remaining_amount": c["remaining_amount"],
					"unresolved_amount": c["unresolved_amount"],
					"document_status": d.status,
					"pool_status": pool_status,
					"source_count": c["source_count"],
					"source_voucher_count": cint(d.source_voucher_count),
					"credit_allocation": d.name,
					"currency": d.currency,
				}
			)
	return rows


def fetch_control_credit_batch_product_details(filters: dict | None = None) -> list[dict]:
	"""Persisted product_rows (and batch share) — one row per stored allocation target × control type with amount."""
	import frappe

	filters = filters or {}
	conds = ["p.docstatus < 2", "p.status != 'Cancelled'"]
	values: dict = {}
	if filters.get("company"):
		conds.append("p.company = %(company)s")
		values["company"] = filters["company"]
	if filters.get("persian_period"):
		conds.append("p.persian_period = %(persian_period)s")
		values["persian_period"] = filters["persian_period"]
	if filters.get("persian_year"):
		conds.append("p.persian_year = %(persian_year)s")
		values["persian_year"] = cint(filters["persian_year"])
	if filters.get("persian_month"):
		conds.append("p.persian_month = %(persian_month)s")
		values["persian_month"] = cint(filters["persian_month"])
	if filters.get("credit_allocation"):
		conds.append("p.name = %(credit_allocation)s")
		values["credit_allocation"] = filters["credit_allocation"]
	if filters.get("batch_no"):
		conds.append("pr.batch_no = %(batch_no)s")
		values["batch_no"] = filters["batch_no"]
	if filters.get("item_code"):
		conds.append("pr.item_code = %(item_code)s")
		values["item_code"] = filters["item_code"]
	if filters.get("product_allocation"):
		conds.append("pr.product_allocation = %(product_allocation)s")
		values["product_allocation"] = filters["product_allocation"]

	product_rows = frappe.db.sql(
		f"""
		SELECT p.name AS credit_allocation, p.company, p.persian_period, p.persian_year, p.persian_month,
		       p.status, p.currency, p.labor_account, p.overhead_account,
		       pr.batch_no, pr.item_code, pr.product_allocation, pr.product_share,
		       pr.source_document_type, pr.source_document, pr.quantity,
		       pr.labor_allocated, pr.overhead_allocated, pr.total_allocated,
		       br.applied_batch_share, br.calculated_batch_share, br.batch_allocation AS batch_alloc_name
		FROM `tabMFG Cost Control Credit Product Row` pr
		INNER JOIN `tabMFG Cost Control Credit Allocation` p ON p.name = pr.parent
		LEFT JOIN `tabMFG Cost Control Credit Batch Row` br
		  ON br.parent = p.name AND br.batch_no = pr.batch_no
		WHERE {' AND '.join(conds)}
		ORDER BY p.persian_year, p.persian_month, pr.batch_no, pr.item_code, pr.source_document
		""",
		values,
		as_dict=True,
	)

	control_type = (filters.get("control_type") or "").strip().lower()
	control_account = (filters.get("control_account") or "").strip()
	include_zero = cint(filters.get("include_zero_amounts"))

	out: list[dict] = []
	for r in product_rows:
		pairs = [
			(
				"Labor",
				r.labor_account,
				flt(r.labor_allocated),
			),
			(
				"Overhead",
				r.overhead_account,
				flt(r.overhead_allocated),
			),
		]
		for ctype, acct, amt in pairs:
			if control_type and ctype.lower() != control_type:
				continue
			if control_account and acct != control_account:
				continue
			if not include_zero and amt == 0:
				continue
			out.append(
				{
					"company": r.company,
					"persian_period": r.persian_period,
					"control_type": ctype,
					"control_account": acct,
					"credit_allocation": r.credit_allocation,
					"document_status": r.status,
					"batch_no": r.batch_no,
					"batch_allocation": r.batch_alloc_name,
					"item_code": r.item_code,
					"product_allocation": r.product_allocation,
					"allocation_basis": "Applied Batch Share × Product Share × Source Document Qty",
					"applied_batch_share": flt(r.applied_batch_share),
					"product_share": flt(r.product_share),
					"allocation_ratio": flt(r.applied_batch_share) * flt(r.product_share),
					"allocated_amount": amt,
					"source_document_type": r.source_document_type,
					"source_document": r.source_document,
					"quantity": flt(r.quantity),
					"currency": r.currency,
				}
			)
	return out
