# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Push credit-allocation shares into MFG Cost Product Allocation Document Breakdown."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision


def _money(value, currency: str) -> float:
	precision = get_currency_precision(currency)
	q = Decimal("1") if precision <= 0 else Decimal("1").scaleb(-precision)
	return float(Decimal(str(value or 0)).quantize(q))


def _zero_breakdown_credit_fields(pa_name: str, currency: str):
	rows = frappe.get_all(
		"MFG Cost Product Allocation Document Breakdown",
		filters={"parent": pa_name},
		fields=["name", "labor_amount", "overhead_amount"],
	)
	for r in rows:
		frappe.db.set_value(
			"MFG Cost Product Allocation Document Breakdown",
			r.name,
			{
				"allocated_labor_share": 0,
				"allocated_overhead_share": 0,
				"unallocated_labor": _money(r.labor_amount, currency),
				"unallocated_overhead": _money(r.overhead_amount, currency),
			},
			update_modified=False,
		)
	frappe.db.set_value(
		"MFG Cost Product Allocation",
		pa_name,
		{
			"breakdown_total_allocated_labor_share": 0,
			"breakdown_total_allocated_overhead_share": 0,
			"breakdown_total_unallocated_labor": frappe.db.get_value(
				"MFG Cost Product Allocation", pa_name, "breakdown_total_labor"
			)
			or 0,
			"breakdown_total_unallocated_overhead": frappe.db.get_value(
				"MFG Cost Product Allocation", pa_name, "breakdown_total_overhead"
			)
			or 0,
		},
		update_modified=False,
	)


def clear_credit_shares_for_period(
	*,
	company: str,
	persian_year: int,
	persian_month: int,
	exclude_allocation: str | None = None,
):
	"""Reset PA breakdown credit fields for the period, then re-apply any other active allocation."""
	monthly = frappe.db.get_value(
		"Monthly FG Cost Calculation",
		{
			"company": company,
			"persian_year": cint(persian_year),
			"persian_month": cint(persian_month),
		},
		"name",
	)
	if not monthly:
		return
	currency = (
		frappe.db.get_value("Monthly FG Cost Calculation", monthly, "currency")
		or frappe.get_cached_value("Company", company, "default_currency")
		or "IRR"
	)
	pas = frappe.get_all(
		"MFG Cost Product Allocation",
		filters={"monthly_calculation": monthly},
		pluck="name",
	)
	for pa in pas:
		_zero_breakdown_credit_fields(pa, currency)

	# Re-apply another active Calculated/Submitted allocation for same period if present
	other = frappe.db.get_value(
		"MFG Cost Control Credit Allocation",
		{
			"company": company,
			"persian_year": cint(persian_year),
			"persian_month": cint(persian_month),
			"status": ("in", ["Calculated", "Submitted"]),
			"name": ("!=", exclude_allocation or ""),
			"docstatus": ("<", 2),
		},
		"name",
	)
	if other:
		apply_credit_shares_to_product_allocations(frappe.get_doc("MFG Cost Control Credit Allocation", other))


def apply_credit_shares_to_product_allocations(doc) -> dict:
	"""Map product_rows (by product_allocation + source_document) onto Document Breakdown rows."""
	currency = doc.currency or "IRR"
	# Aggregate allocated amounts by (product_allocation, source_document)
	by_key: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(
		lambda: {"labor": Decimal("0"), "overhead": Decimal("0")}
	)
	pa_names: set[str] = set()
	for row in doc.get("product_rows") or []:
		pa = (row.product_allocation or "").strip()
		src = (row.source_document or "").strip()
		if not pa:
			continue
		pa_names.add(pa)
		by_key[(pa, src)]["labor"] += Decimal(str(flt(row.labor_allocated)))
		by_key[(pa, src)]["overhead"] += Decimal(str(flt(row.overhead_allocated)))

	# Also zero all PAs for this monthly calculation first
	if doc.monthly_calculation:
		all_pas = frappe.get_all(
			"MFG Cost Product Allocation",
			filters={"monthly_calculation": doc.monthly_calculation},
			pluck="name",
		)
		for pa in all_pas:
			_zero_breakdown_credit_fields(pa, currency)
			pa_names.add(pa)

	updated = 0
	for pa in sorted(pa_names):
		if not frappe.db.exists("MFG Cost Product Allocation", pa):
			continue
		children = frappe.get_all(
			"MFG Cost Product Allocation Document Breakdown",
			filters={"parent": pa},
			fields=["name", "source_document", "labor_amount", "overhead_amount"],
			order_by="idx asc",
		)
		tot_lab = tot_oh = tot_unlab = tot_unoh = Decimal("0")
		for ch in children:
			src = (ch.source_document or "").strip()
			alloc = by_key.get((pa, src), {"labor": Decimal("0"), "overhead": Decimal("0")})
			# If source_document blank on product row, fall back to first/only child
			if not src and (pa, "") in by_key and len(children) == 1:
				alloc = by_key[(pa, "")]
			lab_share = alloc["labor"]
			oh_share = alloc["overhead"]
			unlab = Decimal(str(flt(ch.labor_amount))) - lab_share
			unoh = Decimal(str(flt(ch.overhead_amount))) - oh_share
			frappe.db.set_value(
				"MFG Cost Product Allocation Document Breakdown",
				ch.name,
				{
					"allocated_labor_share": _money(lab_share, currency),
					"allocated_overhead_share": _money(oh_share, currency),
					"unallocated_labor": _money(unlab, currency),
					"unallocated_overhead": _money(unoh, currency),
				},
				update_modified=False,
			)
			tot_lab += lab_share
			tot_oh += oh_share
			tot_unlab += unlab
			tot_unoh += unoh
			updated += 1
		frappe.db.set_value(
			"MFG Cost Product Allocation",
			pa,
			{
				"breakdown_total_allocated_labor_share": _money(tot_lab, currency),
				"breakdown_total_allocated_overhead_share": _money(tot_oh, currency),
				"breakdown_total_unallocated_labor": _money(tot_unlab, currency),
				"breakdown_total_unallocated_overhead": _money(tot_unoh, currency),
			},
			update_modified=False,
		)

	return {"product_allocations": len(pa_names), "rows_updated": updated}
