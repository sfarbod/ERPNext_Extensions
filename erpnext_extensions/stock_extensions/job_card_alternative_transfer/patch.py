# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Monkey-patch Job Card material completeness to credit substitute transfers."""

from __future__ import annotations

import frappe
from frappe.utils import flt

from erpnext_extensions.stock_extensions.job_card_alternative_transfer.attribution import (
	get_attributed_transferred_qty_by_item,
	get_shortfall_rows,
	materials_fully_transferred,
)

_ATTR_FLAG = "_ee_jc_alt_transfer_patched"
_ATTR_ORIG_GET_ROWS = "_ee_jc_alt_transfer_orig_get_rows_to_move"
_ATTR_ORIG_SHORTAGES = "_ee_jc_alt_transfer_orig_get_material_shortages"
_ATTR_ORIG_HAS_SHORT = "_ee_jc_alt_transfer_orig_has_short_transferred_material"


def apply_patch() -> None:
	"""Idempotently replace Job Card transfer-completeness methods.

	``get_material_shortages`` is always (re)bound to the ``@frappe.whitelist()``
	function so Desk ``frm.call({ method: "get_material_shortages", doc })``
	keeps working after code updates. Frappe checks membership in
	``frappe.whitelisted`` (function identity), not a boolean attribute.
	"""
	from erpnext.manufacturing.doctype.job_card.job_card import JobCard

	if not getattr(JobCard, _ATTR_FLAG, False):
		setattr(JobCard, _ATTR_ORIG_GET_ROWS, JobCard.get_rows_to_move)
		setattr(JobCard, _ATTR_ORIG_SHORTAGES, JobCard.get_material_shortages)
		setattr(JobCard, _ATTR_ORIG_HAS_SHORT, JobCard.has_short_transferred_material)

		JobCard.get_rows_to_move = get_rows_to_move
		JobCard.has_short_transferred_material = has_short_transferred_material
		JobCard.ee_materials_fully_transferred = materials_fully_transferred_method
		setattr(JobCard, _ATTR_FLAG, True)

	# Always rebind the whitelisted shortages method (safe / idempotent).
	JobCard.get_material_shortages = get_material_shortages
	_ensure_in_frappe_whitelist(get_material_shortages)


def _ensure_in_frappe_whitelist(fn) -> None:
	"""Register ``fn`` in Frappe's whitelist set if the decorator did not run yet."""
	if fn in frappe.whitelisted:
		return
	frappe.whitelisted.add(fn)
	frappe.allowed_http_methods_for_whitelisted_func[fn] = ["GET", "POST", "PUT", "DELETE"]


def materials_fully_transferred_method(self) -> bool:
	return materials_fully_transferred(self)


def get_rows_to_move(self):
	"""Return Job Card Item rows that still have an effective shortfall.

	Uses Stock Entry ``original_item`` attribution so original + substitute
	transfers (including mixed / multi-substitute) satisfy the demand row.
	"""
	shortfalls = {row.name for row in get_shortfall_rows(self)}
	return [row for row in (self.items or []) if row.name in shortfalls]


@frappe.whitelist()
def get_material_shortages(self):
	"""Warehouse shortage dialog using effective remaining qty to move.

	Read-only. Requires Job Card read permission (also enforced by
	``run_doc_method``). Never creates or submits Stock Entries.
	"""
	# Defense in depth beyond run_doc_method(check_permission=True).
	if hasattr(self, "check_permission"):
		self.check_permission("read")

	from erpnext.manufacturing.doctype.job_card.job_card import get_actual_qty_map

	rows_to_move = [row for row in self.get_rows_to_move() if row.source_warehouse]
	if not rows_to_move:
		return []

	shortfall_by_name = {s.name: flt(s.shortfall_qty) for s in get_shortfall_rows(self)}
	in_stock = get_actual_qty_map(rows_to_move)

	shortages = []
	for row in rows_to_move:
		key = (row.item_code, row.source_warehouse)
		qty_to_move = flt(shortfall_by_name.get(row.name))
		if qty_to_move <= 0:
			continue
		available = max(flt(in_stock.get(key)), 0)
		in_stock[key] = available - qty_to_move
		if available < qty_to_move:
			shortages.append(
				{
					"name": row.name,
					"item_code": row.item_code,
					"warehouse": row.source_warehouse,
					"qty_to_move": qty_to_move,
					"available_qty": available,
					"shortage_qty": qty_to_move - available,
					"uom": row.stock_uom or row.uom,
				}
			)
	return shortages


def has_short_transferred_material(self):
	"""True when any own WO material for this operation is under-transferred.

	Compares Work Order expected qty (scaled to Job Card ``for_quantity``)
	against Job Card–scoped Stock Entry attribution (original + substitutes).
	"""
	if not (self.work_order and flt(self.for_quantity)):
		return False

	wo_qty = self.get_work_order_qty_details().operation_qty
	if not wo_qty:
		return False

	wo_items = frappe.get_all(
		"Work Order Item",
		filters={"parent": self.work_order, "parenttype": "Work Order"},
		fields=["item_code", "required_qty", "operation", "operation_row_id"],
	)
	attributed = get_attributed_transferred_qty_by_item(self.name)

	for row in wo_items:
		if not self.is_own_material(row):
			continue
		expected = flt(row.required_qty) / wo_qty * flt(self.for_quantity)
		if flt(attributed.get(row.item_code), 6) < flt(expected, 6):
			return True
	return False
