# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Authoritative Job Card material transfer attribution (original + substitutes).

Aggregation rule (stock UOM via ``transfer_qty``)::

    effective_transferred_qty[requirement_item] = sum(
        ± transfer_qty
        for submitted Material Transfer for Manufacture rows on this Job Card
        where (original_item or item_code) == requirement_item
    )

- Forward transfers add; returns (``is_return``) subtract.
- Draft / cancelled Stock Entries are excluded (``docstatus == 1`` only).
- Attribution uses Stock Entry ``original_item``, not a global Item Alternative
  graph lookup — so unrelated Job Cards and unlinked substitutes never mix.
- Each Stock Entry Detail line is counted once (no double-count via JC rows).
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt


PURPOSE_MTFM = "Material Transfer for Manufacture"


def get_attributed_transferred_qty_by_item(job_card: str) -> dict[str, float]:
	"""Net stock-UOM qty credited to each requirement item code for one Job Card.

	Key = ``original_item or item_code`` on each submitted MTfM detail row.
	"""
	if not job_card:
		return {}

	rows = frappe.db.sql(
		"""
		select
			sed.item_code,
			sed.original_item,
			sed.transfer_qty,
			coalesce(se.is_return, 0) as is_return
		from `tabStock Entry Detail` sed
		inner join `tabStock Entry` se on se.name = sed.parent
		where se.job_card = %s
			and se.docstatus = 1
			and se.purpose = %s
		""",
		(job_card, PURPOSE_MTFM),
		as_dict=True,
	)
	return aggregate_transfer_rows(rows)


def aggregate_transfer_rows(rows: list[dict[str, Any]] | tuple) -> dict[str, float]:
	"""Pure aggregator used by production query and unit tests."""
	out: dict[str, float] = {}
	for row in rows or []:
		item_code = row.get("item_code")
		if not item_code:
			continue
		key = row.get("original_item") or item_code
		sign = -1.0 if flt(row.get("is_return")) else 1.0
		qty = sign * flt(row.get("transfer_qty"))
		out[key] = flt(out.get(key)) + qty
	# Clamp negatives from over-return to zero per item — excess return must not
	# invent credit for unrelated requirements.
	return {k: max(flt(v), 0.0) for k, v in out.items()}


def allocate_effective_qty_to_demand_rows(
	demand_rows: list[Any],
	attributed: dict[str, float],
) -> list[frappe._dict]:
	"""FIFO-allocate attributed qty across demand rows (required_qty > 0).

	For each demand row::

	    shortfall_qty = max(0, required_qty - available_pool_for_item)

	The pool is then reduced by ``required_qty`` (floored at 0) so sibling
	demand rows for the same item cannot both claim the same transfer qty.
	Over-transfer remains in the pool for later siblings and never creates a
	negative shortfall or credit for a different item.
	"""
	pool = {k: flt(v) for k, v in (attributed or {}).items()}
	results: list[frappe._dict] = []

	ordered = sorted(
		demand_rows or [],
		key=lambda r: (flt(getattr(r, "idx", 0) or 0), getattr(r, "name", "") or ""),
	)
	for row in ordered:
		required = flt(getattr(row, "required_qty", 0))
		if required <= 0:
			continue
		item_code = getattr(row, "item_code", None)
		available = flt(pool.get(item_code))
		shortfall = max(0.0, required - available)
		results.append(
			frappe._dict(
				{
					"name": getattr(row, "name", None),
					"idx": getattr(row, "idx", None),
					"item_code": item_code,
					"required_qty": required,
					"effective_transferred_qty": available,
					"shortfall_qty": shortfall,
					"source_warehouse": getattr(row, "source_warehouse", None),
					"stock_uom": getattr(row, "stock_uom", None),
					"uom": getattr(row, "uom", None),
				}
			)
		)
		pool[item_code] = max(0.0, available - required)

	return results


def get_demand_fulfillment(job_card_doc) -> list[frappe._dict]:
	"""Fulfillment rows for every Job Card Item with required_qty > 0."""
	attributed = get_attributed_transferred_qty_by_item(job_card_doc.name)
	return allocate_effective_qty_to_demand_rows(job_card_doc.get("items") or [], attributed)


def get_shortfall_rows(job_card_doc) -> list[frappe._dict]:
	"""Demand rows whose effective transferred qty is still below required."""
	return [row for row in get_demand_fulfillment(job_card_doc) if flt(row.shortfall_qty) > 0]


def materials_fully_transferred(job_card_doc) -> bool:
	"""True when every demand row is covered by original + substitute transfers."""
	if not (job_card_doc.get("items") or []):
		return True
	return not get_shortfall_rows(job_card_doc)


def get_material_transfer_status(job_card_doc) -> dict[str, Any]:
	"""Payload for Desk UI and whitelist API."""
	fulfillment = get_demand_fulfillment(job_card_doc)
	shortfalls = [row for row in fulfillment if flt(row.shortfall_qty) > 0]
	return {
		"fully_transferred": not shortfalls,
		"attributed_by_item": get_attributed_transferred_qty_by_item(job_card_doc.name),
		"fulfillment": fulfillment,
		"shortfalls": shortfalls,
	}
