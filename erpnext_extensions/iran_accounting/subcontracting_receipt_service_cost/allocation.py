# Copyright (c) 2026, ERPNext Extensions contributors
"""PO → SCO → SCR service-amount resolution and allocation rules.

Source of truth for the service charge is the Purchase Order service row
``base_amount`` (company currency), reached through:

	SCR Item.subcontracting_order_item
	  → SCO Item.purchase_order_item
	  → PO Item.base_amount

``last_purchase_rate``, item valuation rate, and unrelated PO grand totals are
never used.

Allocation model (ERPNext-aligned)
---------------------------------
For each submitted SCO finished-good row:

	intended_service_cost_per_qty = po_service_base_amount / sco_fg_qty

Every non-cancelled SCR against that SCO row uses the same per-unit rate. Partial
receipts therefore allocate ``qty × intended`` and the sum across SCRs cannot
exceed the PO service amount when total received qty ≤ SCO qty.

Fee-type labels are diagnostic only; both fixed-total (e.g. ETO cartons) and
explicit per-unit PO lines reduce to the same division once the PO service
``base_amount`` and SCO FG qty are known.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost import (
	SERVICE_COST_TOLERANCE,
)


class ScrServiceCostAllocationError(frappe.ValidationError):
	pass


def classify_fee_type(*, po_service_qty: float, fg_item_qty: float, sco_fg_qty: float) -> str:
	"""Return a diagnostic fee-type label (does not change the math)."""
	po_service_qty = flt(po_service_qty)
	fg_item_qty = flt(fg_item_qty) or flt(sco_fg_qty)
	sco_fg_qty = flt(sco_fg_qty)
	if po_service_qty > 0 and fg_item_qty > 0 and abs(po_service_qty - fg_item_qty) > SERVICE_COST_TOLERANCE:
		return "fixed_total_for_operation"
	if po_service_qty > 0 and sco_fg_qty > 0 and abs(po_service_qty - sco_fg_qty) <= SERVICE_COST_TOLERANCE:
		return "per_unit"
	return "po_amount_over_sco_fg_qty"


def intended_service_cost_per_qty(*, po_service_amount: float, sco_fg_qty: float) -> float:
	sco_fg_qty = flt(sco_fg_qty)
	if sco_fg_qty <= 0:
		raise ScrServiceCostAllocationError("SCO finished-good qty must be greater than zero")
	return flt(po_service_amount) / sco_fg_qty


def allocation_within_cap(
	*,
	po_service_amount: float,
	sibling_allocations: list[dict[str, Any]],
	tolerance: float = SERVICE_COST_TOLERANCE,
) -> dict[str, Any]:
	"""Ensure sum(qty × service_cost_per_qty) across siblings ≤ PO service amount."""
	po_service_amount = flt(po_service_amount)
	total = 0.0
	rows = []
	for row in sibling_allocations:
		qty = flt(row.get("qty"))
		svc = flt(row.get("service_cost_per_qty"))
		allocated = qty * svc
		total += allocated
		rows.append(
			{
				"subcontracting_receipt": row.get("subcontracting_receipt"),
				"subcontracting_receipt_item": row.get("subcontracting_receipt_item"),
				"qty": qty,
				"service_cost_per_qty": svc,
				"allocated_service_amount": allocated,
			}
		)
	over = total - po_service_amount
	return {
		"po_service_amount": po_service_amount,
		"total_allocated": total,
		"remaining": po_service_amount - total,
		"within_cap": over <= tolerance,
		"over_by": over if over > tolerance else 0.0,
		"rows": rows,
	}


def resolve_po_service_for_sco_item(sco_item_name: str) -> dict[str, Any]:
	"""Map one SCO FG row to its PO service amount and metadata."""
	sco_item = frappe.db.get_value(
		"Subcontracting Order Item",
		sco_item_name,
		[
			"name",
			"parent",
			"item_code",
			"qty",
			"purchase_order_item",
			"service_cost_per_qty",
			"rm_cost_per_qty",
			"additional_cost_per_qty",
			"subcontracting_conversion_factor",
		],
		as_dict=True,
	)
	if not sco_item:
		raise ScrServiceCostAllocationError(f"Subcontracting Order Item {sco_item_name} not found")

	sco = frappe.db.get_value(
		"Subcontracting Order",
		sco_item.parent,
		["name", "docstatus", "status", "purchase_order", "company", "supplier", "conversion_rate"],
		as_dict=True,
	)
	if not sco:
		raise ScrServiceCostAllocationError(f"Subcontracting Order {sco_item.parent} not found")
	if cint_docstatus(sco.docstatus) == 2:
		raise ScrServiceCostAllocationError(f"Subcontracting Order {sco.name} is cancelled")

	if not sco_item.purchase_order_item:
		raise ScrServiceCostAllocationError(
			f"SCO item {sco_item_name} has no purchase_order_item; cannot resolve PO service amount"
		)

	# Ambiguity: more than one SCO service row pointing at the same PO item for this SCO.
	service_rows = frappe.get_all(
		"Subcontracting Order Service Item",
		filters={"parent": sco.name, "purchase_order_item": sco_item.purchase_order_item},
		fields=["name", "item_code", "qty", "rate", "amount", "base_amount", "fg_item", "fg_item_qty"],
	)
	if not service_rows:
		raise ScrServiceCostAllocationError(
			f"No SCO service item on {sco.name} for purchase_order_item {sco_item.purchase_order_item}"
		)
	if len(service_rows) > 1:
		raise ScrServiceCostAllocationError(
			f"Ambiguous SCO service mapping on {sco.name}: "
			f"{len(service_rows)} service rows share purchase_order_item {sco_item.purchase_order_item}"
		)

	svc = service_rows[0]
	po_item = frappe.db.get_value(
		"Purchase Order Item",
		sco_item.purchase_order_item,
		[
			"name",
			"parent",
			"item_code",
			"qty",
			"rate",
			"amount",
			"base_rate",
			"base_amount",
			"fg_item",
			"fg_item_qty",
			"last_purchase_rate",
			"stock_uom_rate",
		],
		as_dict=True,
	)
	if not po_item:
		raise ScrServiceCostAllocationError(
			f"Purchase Order Item {sco_item.purchase_order_item} not found"
		)
	if po_item.parent != sco.purchase_order:
		raise ScrServiceCostAllocationError(
			f"PO item {po_item.name} belongs to {po_item.parent}, "
			f"but SCO {sco.name} links to {sco.purchase_order}"
		)

	# Prefer company-currency base_amount; fall back to amount.
	po_service_amount = flt(po_item.base_amount) or flt(po_item.amount)
	if po_service_amount <= 0:
		raise ScrServiceCostAllocationError(
			f"PO item {po_item.name} has non-positive service amount"
		)

	# Guard: never treat last_purchase_rate as the fixed total.
	lpr = flt(po_item.last_purchase_rate)
	if lpr and abs(lpr - flt(po_item.rate)) > SERVICE_COST_TOLERANCE:
		# Informative only — resolution still uses base_amount.
		pass

	intended = intended_service_cost_per_qty(
		po_service_amount=po_service_amount, sco_fg_qty=sco_item.qty
	)
	fee_type = classify_fee_type(
		po_service_qty=po_item.qty,
		fg_item_qty=po_item.fg_item_qty or svc.fg_item_qty,
		sco_fg_qty=sco_item.qty,
	)

	return {
		"subcontracting_order": sco.name,
		"subcontracting_order_docstatus": cint_docstatus(sco.docstatus),
		"purchase_order": sco.purchase_order,
		"company": sco.company,
		"supplier": sco.supplier,
		"sco_item": sco_item.name,
		"sco_item_code": sco_item.item_code,
		"sco_fg_qty": flt(sco_item.qty),
		"sco_service_cost_per_qty": flt(sco_item.service_cost_per_qty),
		"purchase_order_item": po_item.name,
		"po_service_item_code": po_item.item_code,
		"po_service_qty": flt(po_item.qty),
		"po_service_rate": flt(po_item.rate),
		"po_service_amount": po_service_amount,
		"po_last_purchase_rate": lpr,
		"po_fg_item": po_item.fg_item,
		"po_fg_item_qty": flt(po_item.fg_item_qty),
		"sco_service_item": svc.name,
		"fee_type": fee_type,
		"intended_service_cost_per_qty": intended,
		# Explicit rejection signal if someone tried to use LPR as amount.
		"rejected_last_purchase_rate_as_amount": True,
	}


def list_sibling_scr_items(sco_item_name: str, *, include_cancelled: bool = False) -> list[dict]:
	"""All SCR FG rows linked to the same SCO item (for allocation caps)."""
	filters: dict[str, Any] = {"subcontracting_order_item": sco_item_name}
	rows = frappe.get_all(
		"Subcontracting Receipt Item",
		filters=filters,
		fields=[
			"name",
			"parent",
			"item_code",
			"qty",
			"received_qty",
			"rate",
			"amount",
			"service_cost_per_qty",
			"rm_cost_per_qty",
			"additional_cost_per_qty",
			"subcontracting_order",
			"purchase_order",
			"purchase_order_item",
		],
		order_by="parent, idx",
	)
	out = []
	for row in rows:
		scr_meta = frappe.db.get_value(
			"Subcontracting Receipt",
			row.parent,
			["docstatus", "is_return", "status", "company"],
			as_dict=True,
		)
		if not scr_meta:
			continue
		ds = cint_docstatus(scr_meta.docstatus)
		if ds == 2 and not include_cancelled:
			continue
		out.append(
			{
				**row,
				"docstatus": ds,
				"is_return": cint_docstatus(scr_meta.is_return),
				"status": scr_meta.status,
				"company": scr_meta.company,
				"subcontracting_receipt": row.parent,
				"subcontracting_receipt_item": row.name,
			}
		)
	return out


def cint_docstatus(value) -> int:
	try:
		return int(value or 0)
	except (TypeError, ValueError):
		return 0
