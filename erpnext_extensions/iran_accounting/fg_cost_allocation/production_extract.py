# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Eligible Finished Goods Manufacture Stock Entry extraction.

Eligibility for NEW calculations is driven by Settings → Item Primary UOMs.
Item Group تزریقی is only the initial default population criterion, not a permanent filter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.fg_cost_allocation.eligibility import (
	ExclusionRow,
	summarize_exclusions,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.period import ResolvedPeriod


@dataclass
class ProductionRow:
	stock_entry: str
	stock_entry_detail: str
	work_order: str
	posting_date: Any
	item_code: str
	item_name: str
	batch_no: str
	qty: Decimal
	stock_uom: str
	uom: str
	item_group: str = ""


@dataclass
class ProductBucket:
	batch_no: str
	item_code: str
	item_name: str
	actual_qty: Decimal = Decimal("0")
	uom: str = ""
	work_orders: set[str] = field(default_factory=set)
	stock_entries: set[str] = field(default_factory=set)
	stock_entry_details: set[str] = field(default_factory=set)
	posting_dates: list = field(default_factory=list)
	normalization_factor: Decimal | None = None
	normalization_uom: str = ""
	normalized_qty: Decimal = Decimal("0")
	missing_uom_factor: bool = False
	exceptions: list[str] = field(default_factory=list)
	item_group: str = ""


def configured_item_codes(settings_name: str | None = None, company: str | None = None) -> set[str]:
	"""Items currently in Item Primary UOMs — sole eligibility source for new calculations."""
	if not settings_name and company:
		settings_name = frappe.db.get_value(
			"Finished Goods Cost Allocation Settings", {"company": company}, "name"
		)
	if not settings_name:
		return set()
	return set(
		frappe.get_all(
			"FG Item Primary UOM",
			filters={"parent": settings_name},
			pluck="item",
		)
	) - {None, ""}


def _fetch_manufacture_fg_candidates(company: str, period: ResolvedPeriod) -> list[dict]:
	return frappe.db.sql(
		"""
		SELECT
			se.name AS stock_entry,
			sed.name AS stock_entry_detail,
			se.work_order,
			se.posting_date,
			sed.item_code,
			i.item_name,
			i.item_group,
			sed.batch_no,
			sed.transfer_qty AS qty,
			sed.stock_uom,
			sed.uom
		FROM `tabStock Entry` se
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		INNER JOIN `tabWork Order` wo ON wo.name = se.work_order
		INNER JOIN `tabItem` i ON i.name = sed.item_code
		WHERE se.docstatus = 1
		  AND se.company = %(company)s
		  AND se.purpose = 'Manufacture'
		  AND se.stock_entry_type = 'Manufacture'
		  AND sed.is_finished_item = 1
		  AND IFNULL(sed.t_warehouse, '') != ''
		  AND IFNULL(sed.s_warehouse, '') = ''
		  AND sed.item_code = wo.production_item
		  AND se.posting_date BETWEEN %(start)s AND %(end)s
		ORDER BY se.posting_date, se.posting_time, se.name, sed.idx
		""",
		{
			"company": company,
			"start": str(period.start_date),
			"end": str(period.end_date),
		},
		as_dict=True,
	)


def _to_production_row(r: dict) -> ProductionRow:
	return ProductionRow(
		stock_entry=r.stock_entry,
		stock_entry_detail=r.stock_entry_detail,
		work_order=r.work_order or "",
		posting_date=r.posting_date,
		item_code=r.item_code,
		item_name=r.item_name or "",
		batch_no=(r.batch_no or "").strip(),
		qty=Decimal(str(flt(r.qty))),
		stock_uom=r.stock_uom or "",
		uom=r.uom or r.stock_uom or "",
		item_group=r.item_group or "",
	)


def fetch_eligible_fg_rows(
	company: str,
	period: ResolvedPeriod,
	*,
	settings_name: str | None = None,
) -> tuple[list[ProductionRow], list[ExclusionRow], dict]:
	"""Return (eligible, excluded, exclusion_summary) using Settings Item Primary UOMs."""
	allowed = configured_item_codes(settings_name=settings_name, company=company)
	candidates = _fetch_manufacture_fg_candidates(company, period)
	eligible: list[ProductionRow] = []
	excluded: list[ExclusionRow] = []

	for r in candidates:
		item = r.item_code
		if item in allowed:
			eligible.append(_to_production_row(r))
			continue
		excluded.append(
			ExclusionRow(
				stock_entry=r.stock_entry,
				stock_entry_detail=r.stock_entry_detail,
				work_order=r.work_order or "",
				item_code=item,
				item_name=r.item_name or "",
				item_group=r.item_group or "",
				batch_no=(r.batch_no or "").strip(),
				qty=Decimal(str(flt(r.qty))),
				reason="Item not in Costing Settings Item Primary UOMs",
			)
		)

	summary = summarize_exclusions(excluded)
	summary["configured_item_count"] = len(allowed)
	return eligible, excluded, summary


def consolidate_products(
	rows: list[ProductionRow],
) -> tuple[dict[tuple[str, str], ProductBucket], list[str]]:
	buckets: dict[tuple[str, str], ProductBucket] = {}
	exceptions: list[str] = []
	for row in rows:
		if not row.batch_no:
			exceptions.append(
				f"Missing batch on SED {row.stock_entry_detail} / SE {row.stock_entry} item {row.item_code}"
			)
			continue
		if row.qty < 0:
			exceptions.append(
				f"Negative qty {row.qty} on SED {row.stock_entry_detail} batch {row.batch_no}"
			)
			continue
		if row.qty == 0:
			exceptions.append(
				f"Zero qty on SED {row.stock_entry_detail} batch {row.batch_no} (excluded from share base)"
			)
			continue
		key = (row.batch_no, row.item_code)
		bucket = buckets.get(key)
		if not bucket:
			bucket = ProductBucket(
				batch_no=row.batch_no,
				item_code=row.item_code,
				item_name=row.item_name,
				uom=row.stock_uom or row.uom,
				item_group=row.item_group,
			)
			buckets[key] = bucket
		bucket.actual_qty += row.qty
		bucket.work_orders.add(row.work_order)
		bucket.stock_entries.add(row.stock_entry)
		bucket.stock_entry_details.add(row.stock_entry_detail)
		bucket.posting_dates.append(row.posting_date)
		if not bucket.uom:
			bucket.uom = row.stock_uom or row.uom
	return buckets, exceptions
