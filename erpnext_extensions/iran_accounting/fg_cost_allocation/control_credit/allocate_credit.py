# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Allocate control-credit pools using MFG Cost Batch Expense Allocation methodology.

Reuses applied_batch_share / product_share from the existing monthly calculation
and the same money residual-to-largest policy as allocate_pools_to_batches.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision
from erpnext_extensions.iran_accounting.fg_cost_allocation.allocate import (
	_money,
	_share,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.control_credit.identify import (
	CreditIdentificationResult,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.document_breakdown import (
	build_document_breakdown,
)


@dataclass
class CreditBatchRow:
	batch_no: str
	batch_allocation: str
	applied_batch_share: Decimal
	calculated_batch_share: Decimal
	labor_allocated: Decimal
	overhead_allocated: Decimal
	residual_applied_labor: bool = False
	residual_applied_overhead: bool = False


@dataclass
class CreditProductRow:
	batch_no: str
	item_code: str
	product_allocation: str
	product_share: Decimal
	source_document_type: str
	source_document: str
	quantity: Decimal
	labor_allocated: Decimal
	overhead_allocated: Decimal


@dataclass
class CreditAllocationPlan:
	monthly_calculation: str
	revision: int
	currency: str
	batch_rows: list[CreditBatchRow]
	product_rows: list[CreditProductRow]
	labor_allocated_total: Decimal
	overhead_allocated_total: Decimal


def _load_monthly(company: str, persian_year: int, persian_month: int):
	name = frappe.db.get_value(
		"Monthly FG Cost Calculation",
		{
			"company": company,
			"persian_year": cint(persian_year),
			"persian_month": cint(persian_month),
		},
		"name",
	)
	if not name:
		frappe.throw(
			_(
				"Monthly FG Cost Calculation not found for {0} / {1}-{2:02d}. "
				"Credit allocation reuses that document's batch shares."
			).format(company, persian_year, persian_month)
		)
	return frappe.get_doc("Monthly FG Cost Calculation", name)


def _distribute_pool(
	pool_amount: Decimal,
	batches: list[dict],
	*,
	currency: str,
) -> list[tuple[dict, Decimal, bool]]:
	"""Mirror allocate_pools_to_batches residual policy for one synthetic pool."""
	precision = get_currency_precision(currency)
	eligible = [b for b in batches if Decimal(str(flt(b.normalized_qty or 0))) > 0]
	if not eligible or pool_amount == 0:
		return [(b, Decimal("0"), False) for b in batches]

	applied_sum = sum((Decimal(str(flt(b.applied_share))) for b in eligible), Decimal("0"))
	share_eps = Decimal("0.000001")
	shares_complete = abs(applied_sum - Decimal("1")) <= share_eps

	raw_amounts: list[tuple[dict, Decimal]] = []
	allocated_sum = Decimal("0")
	for b in eligible:
		raw = _money(pool_amount * Decimal(str(flt(b.applied_share))), precision)
		raw_amounts.append((b, raw))
		allocated_sum += raw
	diff = _money(pool_amount - allocated_sum, precision)
	residual_batch = None
	if shares_complete and eligible and diff != 0:
		residual_batch = max(
			eligible,
			key=lambda x: (Decimal(str(flt(x.normalized_qty or 0))), x.batch_no or ""),
		)

	out: list[tuple[dict, Decimal, bool]] = []
	seen = set()
	for b, amount in raw_amounts:
		residual = False
		final_amt = amount
		if residual_batch and b.batch_no == residual_batch.batch_no and diff != 0:
			final_amt = _money(amount + diff, precision)
			residual = True
		out.append((b, final_amt, residual))
		seen.add(b.batch_no)
	for b in batches:
		if b.batch_no not in seen:
			out.append((b, Decimal("0"), False))
	return out


def build_credit_allocation_plan(
	identification: CreditIdentificationResult,
	*,
	currency: str | None = None,
) -> CreditAllocationPlan:
	monthly = _load_monthly(
		identification.company, identification.persian_year, identification.persian_month
	)
	rev = cint(monthly.revision)
	ccy = currency or monthly.currency or "IRR"
	precision = get_currency_precision(ccy)

	batches = frappe.get_all(
		"MFG Cost Batch Allocation",
		filters={"monthly_calculation": monthly.name, "revision": rev},
		fields=[
			"name",
			"batch_no",
			"normalized_qty",
			"applied_share",
			"calculated_share",
		],
		order_by="batch_no asc",
	)
	if not batches:
		frappe.throw(
			_("No MFG Cost Batch Allocation rows for {0} revision {1}.").format(
				monthly.name, rev
			)
		)

	labor_pool = _money(identification.labor_eligible, precision)
	oh_pool = _money(identification.overhead_eligible, precision)

	labor_dist = {b.batch_no: (amt, res) for b, amt, res in _distribute_pool(labor_pool, batches, currency=ccy)}
	oh_dist = {b.batch_no: (amt, res) for b, amt, res in _distribute_pool(oh_pool, batches, currency=ccy)}

	batch_rows: list[CreditBatchRow] = []
	for b in batches:
		lab_amt, lab_res = labor_dist.get(b.batch_no, (Decimal("0"), False))
		oh_amt, oh_res = oh_dist.get(b.batch_no, (Decimal("0"), False))
		batch_rows.append(
			CreditBatchRow(
				batch_no=b.batch_no,
				batch_allocation=b.name,
				applied_batch_share=Decimal(str(flt(b.applied_share))),
				calculated_batch_share=Decimal(str(flt(b.calculated_share))),
				labor_allocated=lab_amt,
				overhead_allocated=oh_amt,
				residual_applied_labor=lab_res,
				residual_applied_overhead=oh_res,
			)
		)

	# Product + document rows using existing Product Allocation shares and breakdown qty split
	products = frappe.get_all(
		"MFG Cost Product Allocation",
		filters={"monthly_calculation": monthly.name, "revision": rev},
		fields=["name", "batch_no", "item_code", "product_share", "stock_entries", "stock_entry"],
		order_by="batch_no asc, item_code asc",
	)

	product_rows: list[CreditProductRow] = []
	import json

	from erpnext_extensions.iran_accounting.fg_cost_allocation.refresh_product_document_breakdown import (
		_fg_qty_by_stock_entry,
	)

	# Group products by batch for residual-safe product_share distribution
	by_batch: dict[str, list] = {}
	for p in products:
		by_batch.setdefault(p.batch_no, []).append(p)

	batch_labor = {r.batch_no: r.labor_allocated for r in batch_rows}
	batch_oh = {r.batch_no: r.overhead_allocated for r in batch_rows}

	for batch_no, prods in by_batch.items():
		b_lab = batch_labor.get(batch_no, Decimal("0"))
		b_oh = batch_oh.get(batch_no, Decimal("0"))
		eligible = [p for p in prods if Decimal(str(flt(p.product_share or 0))) > 0]
		# Distribute batch labor/oh by product_share (same as allocate_products)
		for pool_name, pool_amt in (("labor", b_lab), ("overhead", b_oh)):
			pass  # handled below per product with both amounts

		raw_lab: list[tuple[object, Decimal]] = []
		raw_oh: list[tuple[object, Decimal]] = []
		sum_lab = sum_oh = Decimal("0")
		for p in eligible:
			share = Decimal(str(flt(p.product_share)))
			la = _money(b_lab * share, precision)
			oa = _money(b_oh * share, precision)
			raw_lab.append((p, la))
			raw_oh.append((p, oa))
			sum_lab += la
			sum_oh += oa
		lab_diff = _money(b_lab - sum_lab, precision)
		oh_diff = _money(b_oh - sum_oh, precision)
		largest = (
			max(eligible, key=lambda x: (Decimal(str(flt(x.product_share))), x.item_code))
			if eligible
			else None
		)
		prod_lab = {}
		prod_oh = {}
		for p, la in raw_lab:
			final = la
			if largest and p.item_code == largest.item_code and lab_diff != 0:
				final = _money(la + lab_diff, precision)
			prod_lab[p.name] = final
		for p, oa in raw_oh:
			final = oa
			if largest and p.item_code == largest.item_code and oh_diff != 0:
				final = _money(oa + oh_diff, precision)
			prod_oh[p.name] = final

		for p in prods:
			try:
				ses = json.loads(p.stock_entries or "[]")
			except Exception:
				ses = []
			if not ses and p.stock_entry:
				ses = [p.stock_entry]
			se_qtys = _fg_qty_by_stock_entry(
				batch_no=p.batch_no, item_code=p.item_code, stock_entries=list(ses)
			)
			breakdown = build_document_breakdown(
				stock_entry_qtys=se_qtys,
				batch_overhead=prod_oh.get(p.name, Decimal("0")),
				batch_labor=prod_lab.get(p.name, Decimal("0")),
				product_share=Decimal("1"),  # already product-scaled
				currency=ccy,
			)
			if not breakdown.rows:
				product_rows.append(
					CreditProductRow(
						batch_no=p.batch_no,
						item_code=p.item_code,
						product_allocation=p.name,
						product_share=Decimal(str(flt(p.product_share))),
						source_document_type="",
						source_document="",
						quantity=Decimal("0"),
						labor_allocated=prod_lab.get(p.name, Decimal("0")),
						overhead_allocated=prod_oh.get(p.name, Decimal("0")),
					)
				)
				continue
			for row in breakdown.rows:
				product_rows.append(
					CreditProductRow(
						batch_no=p.batch_no,
						item_code=p.item_code,
						product_allocation=p.name,
						product_share=Decimal(str(flt(p.product_share))),
						source_document_type=row.source_document_type,
						source_document=row.source_document,
						quantity=row.quantity,
						labor_allocated=row.labor_amount,
						overhead_allocated=row.overhead_amount,
					)
				)

	return CreditAllocationPlan(
		monthly_calculation=monthly.name,
		revision=rev,
		currency=ccy,
		batch_rows=batch_rows,
		product_rows=product_rows,
		labor_allocated_total=_money(sum((r.labor_allocated for r in batch_rows), Decimal("0")), precision),
		overhead_allocated_total=_money(
			sum((r.overhead_allocated for r in batch_rows), Decimal("0")), precision
		),
	)
