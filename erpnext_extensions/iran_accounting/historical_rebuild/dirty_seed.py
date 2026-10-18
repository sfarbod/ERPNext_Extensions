# Copyright (c) 2026, ERPNext Extensions contributors
"""Seed Core RIVs for scanner-dirty identities with justified anchors (v5.6.1)."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from erpnext.controllers.stock_controller import create_repost_item_valuation_entry
from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
	execute_reposting_entry,
)

from erpnext_extensions.iran_accounting.historical_rebuild.constants import MAX_OUTSTANDING_RIV
from erpnext_extensions.iran_accounting.historical_rebuild.failure_registry import (
	is_blocked_identity,
)


KNOWN_MID_ANCHORS: dict[tuple[str, str], tuple[str, str]] = {
	# Validated sandbox/dev mid-anchor for 40942-class repair
	(
		"13200244",
		"انبار پایکار خط تولید اسپاد فارمد",
	): ("2026-08-22", "23:40:12"),
}


def _anchor_for(item_code: str, warehouse: str) -> tuple[str, str] | None:
	key = (item_code, warehouse)
	if key in KNOWN_MID_ANCHORS:
		return KNOWN_MID_ANCHORS[key]
	# Earliest continuity break if any
	row = frappe.db.sql(
		"""
		select posting_date, posting_time from (
		  select posting_date, posting_time, stock_value, stock_value_difference,
		         lag(stock_value) over (
		           order by posting_date, posting_time, creation
		         ) prev_sv
		  from `tabStock Ledger Entry`
		  where is_cancelled=0 and item_code=%s and warehouse=%s
		) t
		where prev_sv is not null
		  and abs(stock_value - (prev_sv + stock_value_difference)) > 1
		order by posting_date, posting_time
		limit 1
		""",
		(item_code, warehouse),
	)
	if row:
		# Start slightly before the break: previous SLE posting
		prev = frappe.db.sql(
			"""
			select posting_date, posting_time
			from `tabStock Ledger Entry`
			where is_cancelled=0 and item_code=%s and warehouse=%s
			  and (
			    posting_date < %s
			    or (posting_date=%s and posting_time < %s)
			  )
			order by posting_date desc, posting_time desc, creation desc
			limit 1
			""",
			(item_code, warehouse, row[0][0], row[0][0], row[0][1]),
		)
		if prev:
			return (str(prev[0][0]), str(prev[0][1]))
		return (str(row[0][0]), str(row[0][1]))
	# Fallback: earliest SLE
	earliest = frappe.db.sql(
		"""
		select posting_date, posting_time from `tabStock Ledger Entry`
		where is_cancelled=0 and item_code=%s and warehouse=%s
		order by posting_date, posting_time, creation
		limit 1
		""",
		(item_code, warehouse),
	)
	if earliest:
		return (str(earliest[0][0]), str(earliest[0][1]))
	return None


def seed_dirty_identities(
	*,
	limit: int = 20,
	execute_sync: bool = True,
	company: str | None = None,
) -> dict[str, Any]:
	"""Create/execute RIVs for cached dirty identities (dev Phase 3)."""
	dirty = frappe.cache().get_value("hrc_integrity_dirty_identities") or []
	if not dirty:
		frappe.throw("No cached dirty identities — run integrity scanner first")

	created = []
	skipped = []
	failed = []
	for pair in dirty[: cint(limit) or 20]:
		if isinstance(pair, (list, tuple)):
			item_code, warehouse = pair[0], pair[1]
		else:
			continue
		if is_blocked_identity(item_code, warehouse):
			skipped.append({"item_code": item_code, "warehouse": warehouse, "reason": "blocked"})
			continue
		outstanding = cint(
			frappe.db.count(
				"Repost Item Valuation",
				{"docstatus": 1, "status": ("in", ["Queued", "In Progress"])},
			)
		)
		if outstanding >= MAX_OUTSTANDING_RIV:
			skipped.append({"item_code": item_code, "warehouse": warehouse, "reason": "backpressure"})
			break
		# Skip if Completed cover from earliest already exists and chain clean — still seed for dirty
		anchor = _anchor_for(item_code, warehouse)
		if not anchor:
			skipped.append({"item_code": item_code, "warehouse": warehouse, "reason": "no_anchor"})
			continue
		# Avoid duplicate open/completed same exact anchor
		exists = frappe.db.exists(
			"Repost Item Valuation",
			{
				"item_code": item_code,
				"warehouse": warehouse,
				"posting_date": anchor[0],
				"posting_time": anchor[1],
				"docstatus": 1,
				"status": ("in", ["Queued", "In Progress", "Completed"]),
			},
		)
		if exists and not execute_sync:
			skipped.append(
				{"item_code": item_code, "warehouse": warehouse, "reason": f"exists:{exists}"}
			)
			continue
		try:
			create_repost_item_valuation_entry(
				frappe._dict(
					based_on="Item and Warehouse",
					item_code=item_code,
					warehouse=warehouse,
					posting_date=anchor[0],
					posting_time=anchor[1],
					allow_zero_rate=False,
					allow_negative_stock=True,
				)
			)
			name = frappe.db.get_value(
				"Repost Item Valuation",
				{
					"item_code": item_code,
					"warehouse": warehouse,
					"posting_date": anchor[0],
					"status": ("in", ["Queued", "In Progress"]),
				},
				"name",
				order_by="creation desc",
			)
			if not name:
				name = frappe.db.get_value(
					"Repost Item Valuation",
					{"item_code": item_code, "warehouse": warehouse},
					"name",
					order_by="creation desc",
				)
			status = None
			if name and execute_sync:
				execute_reposting_entry(name)
				frappe.db.commit()
				status = frappe.db.get_value("Repost Item Valuation", name, "status")
			created.append(
				{
					"riv": name,
					"item_code": item_code,
					"warehouse": warehouse,
					"anchor": anchor,
					"status": status,
				}
			)
			if status == "Failed":
				failed.append(name)
		except Exception as e:
			failed.append(str(e)[:200])
			frappe.db.rollback()
	return {
		"created": created,
		"skipped": skipped,
		"failed": failed,
		"created_count": len(created),
		"failed_count": len(failed),
		"at": str(now_datetime()),
	}
