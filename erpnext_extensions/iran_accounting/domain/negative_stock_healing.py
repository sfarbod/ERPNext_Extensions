# Copyright (c) 2026, ERPNext Extensions contributors
"""Incoming movements that reduce an existing negative warehouse/batch balance.

Do not enable global negative stock. Outgoing movements that create or worsen a
negative balance remain vanilla ERPNext Insufficient Stock / BatchNegativeStockError.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt


def is_negative_stock_healing_movement(previous_qty, actual_qty, new_qty=None) -> bool:
	"""True when an incoming SLE/SABB row reduces an already-negative running qty.

	previous_qty — running warehouse (or batch) qty *before* this movement,
	the same figure ERPNext uses inside ``validate_negative_stock``
	(``wh_data.qty_after_transaction``), never Bin.actual_qty.

	actual_qty — SLE ``actual_qty`` (or signed SABB entry qty). Must be incoming.

	new_qty — ``previous_qty + actual_qty`` unless the caller already computed it.

	Healing:
	    previous_qty < 0
	    and actual_qty > 0
	    and new_qty > previous_qty

	The result may still be negative. That is allowed. Making the balance *more*
	negative (actual_qty <= 0) is never healing.
	"""
	prev = flt(previous_qty)
	actual = flt(actual_qty)
	new = flt(new_qty) if new_qty is not None else prev + actual
	return prev < 0 and actual > 0 and new > prev


def previous_and_new_qty(previous_qty, actual_qty) -> tuple[float, float]:
	prev = flt(previous_qty)
	actual = flt(actual_qty)
	return prev, prev + actual


def _sle_actual_qty(sle) -> float:
	if sle is None:
		return 0.0
	if hasattr(sle, "get"):
		return flt(sle.get("actual_qty"))
	return flt(getattr(sle, "actual_qty", 0))


def make_validate_negative_stock_wrapper(original):
	"""Skip warehouse NegativeStockError only for a qualifying incoming healing SLE."""

	def validate_negative_stock(self, sle):
		from erpnext_extensions.iran_accounting.domain.currency import is_irr_company

		company = getattr(self, "company", None) or (
			sle.get("company") if hasattr(sle, "get") else getattr(sle, "company", None)
		)
		if company and is_irr_company(company):
			wh_data = getattr(self, "wh_data", None)
			previous_qty = flt(getattr(wh_data, "qty_after_transaction", 0) if wh_data is not None else 0)
			actual_qty = _sle_actual_qty(sle)
			new_qty = previous_qty + actual_qty
			if is_negative_stock_healing_movement(previous_qty, actual_qty, new_qty):
				return True
		return original(self, sle)

	validate_negative_stock._iran_negative_stock_healing = True
	validate_negative_stock._iran_original = original
	return validate_negative_stock


def _incoming_qty_for_batch(bundle, batch_no) -> float:
	total = 0.0
	for row in bundle.get("entries") or []:
		row_batch = row.get("batch_no") if hasattr(row, "get") else getattr(row, "batch_no", None)
		if row_batch == batch_no:
			total += flt(row.qty)
	return total


def inward_bundle_heals_existing_negative_batches(bundle) -> bool:
	"""True when every currently-negative target batch is reduced by this Inward SABB.

	``validate_batch_inventory`` throws *after* ``validate_negative_batch`` via
	``throw_error_message``. Skipping only the inner method is not enough.
	"""
	if getattr(bundle, "type_of_transaction", None) != "Inward":
		return False

	from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
		get_auto_batch_nos,
		get_available_batches_qty,
	)

	entries = bundle.get("entries") or []
	batches = []
	for row in entries:
		batch_no = row.get("batch_no") if hasattr(row, "get") else getattr(row, "batch_no", None)
		if batch_no:
			batches.append(batch_no)
	if not batches:
		return False

	available_batches = get_auto_batch_nos(
		frappe._dict(
			{
				"item_code": bundle.item_code,
				"warehouse": bundle.warehouse,
				"batch_no": batches,
				"consider_negative_batches": True,
			}
		)
	)
	if not available_batches:
		return False

	available_batches = get_available_batches_qty(available_batches)
	saw_negative = False
	for batch_no in batches:
		available = flt(available_batches.get(batch_no, 0))
		if available >= 0:
			continue
		saw_negative = True
		incoming = _incoming_qty_for_batch(bundle, batch_no)
		if not is_negative_stock_healing_movement(available, incoming, available + incoming):
			return False
	return saw_negative


def make_validate_negative_batch_wrapper(original):
	"""Skip batch negative-stock only for Inward rows that heal an existing deficit.

	Outward SABB shortages still raise BatchNegativeStockError.
	"""

	def validate_negative_batch(self, batch_no, available_qty):
		from erpnext_extensions.iran_accounting.domain.currency import is_irr_company

		company = getattr(self, "company", None)
		if (
			company
			and is_irr_company(company)
			and getattr(self, "type_of_transaction", None) == "Inward"
		):
			incoming = _incoming_qty_for_batch(self, batch_no)
			available = flt(available_qty)
			# validate_batch_inventory passes qty *before* this inward bundle.
			if is_negative_stock_healing_movement(available, incoming, available + incoming):
				return None
			# set_incoming_rate_for_outward_transaction passes qty *after* applying the row.
			previous_after = available - incoming
			if is_negative_stock_healing_movement(previous_after, incoming, available):
				return None
		return original(self, batch_no, available_qty)

	validate_negative_batch._iran_negative_stock_healing = True
	validate_negative_batch._iran_original = original
	return validate_negative_batch


def make_validate_batch_inventory_wrapper(original):
	"""Skip Inward ``validate_batch_inventory`` when it would reject a healing receipt.

	Vanilla still throws for Outward SABB and for Inward that does not reduce a
	pre-existing negative batch balance.
	"""

	def validate_batch_inventory(self):
		from erpnext_extensions.iran_accounting.domain.currency import is_irr_company

		company = getattr(self, "company", None)
		if company and is_irr_company(company) and inward_bundle_heals_existing_negative_batches(self):
			return None
		return original(self)

	validate_batch_inventory._iran_negative_stock_healing = True
	validate_batch_inventory._iran_original = original
	return validate_batch_inventory
