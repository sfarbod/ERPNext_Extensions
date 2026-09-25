# Copyright (c) 2026, ERPNext Extensions contributors
"""Safe SLE DB persistence after in-memory IRR adjustments in process_sle."""

from __future__ import annotations

import logging

import frappe
from frappe.model.document import Document
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.ledger_rounding import (
	SLE_MONETARY_FIELDS,
	_get_entry_value,
)

logger = logging.getLogger(__name__)

_SLE_PERSIST_FIELDS = SLE_MONETARY_FIELDS + ("qty_after_transaction",)

# Fields restored when out-of-scope RIV soft-skip rejects a newly computed SLE.
_SLE_RESTORE_FIELDS = (
	"incoming_rate",
	"outgoing_rate",
	"valuation_rate",
	"stock_value",
	"stock_value_difference",
	"qty_after_transaction",
)


def snapshot_sle_ledger_state(sle) -> dict | None:
	"""Capture pre-vanilla SLE economics for out-of-scope restore."""
	name = _get_entry_value(sle, "name")
	if not name:
		return None
	try:
		return frappe.db.get_value(
			"Stock Ledger Entry",
			name,
			list(_SLE_RESTORE_FIELDS),
			as_dict=True,
		)
	except Exception:
		return None


def _assign_sle_field(sle, field, val) -> None:
	"""Set an SLE field without assuming Document.set (dict rows may shadow it)."""
	setter = getattr(type(sle), "set", None)
	if callable(setter) and not isinstance(sle, dict):
		try:
			setter(sle, field, val)
			return
		except Exception:
			pass
	if hasattr(sle, "__setitem__"):
		sle[field] = val
		return
	setattr(sle, field, val)


def restore_out_of_scope_sle_ledger_state(engine, sle, pre) -> bool:
	"""Restore SLE + warehouse running state after out-of-scope soft-skip.

	Vanilla ``process_sle`` already mutated ``sle`` and ``engine.wh_data``. Put
	both back to the pre-vanilla snapshot so target-item RIV does not create new
	I1/I2 on unrelated FG cascades.
	"""
	if not pre:
		return False
	for field in _SLE_RESTORE_FIELDS:
		if field not in pre:
			continue
		_assign_sle_field(sle, field, pre.get(field))
	wh = getattr(engine, "wh_data", None) if engine is not None else None
	if wh is not None:
		wh.qty_after_transaction = flt(pre.get("qty_after_transaction"))
		wh.stock_value = flt(pre.get("stock_value"))
		wh.valuation_rate = flt(pre.get("valuation_rate"))
	return True


def persist_processed_sle_if_possible(sle) -> bool:
	"""Write IRR-rounded fields to DB only when ERPNext has already persisted the SLE row.

	``update_entries_after.process_sle`` usually sets ``sle.doctype`` and calls ``db_update`` before
	returning. On early return (e.g. negative-stock validation) ``sle`` may be a ``frappe._dict`` without
	``doctype`` — never call ``frappe.get_doc(sle)`` in that case.
	"""
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		assert_sle_valuation_integrity_after_sync,
	)

	assert_sle_valuation_integrity_after_sync(sle)

	name = _get_entry_value(sle, "name")
	doctype = _get_entry_value(sle, "doctype")

	if isinstance(sle, Document):
		if sle.doctype != "Stock Ledger Entry":
			return False
		if not sle.name or sle.is_new():
			return False
		sle.db_update()
		return True

	if doctype == "Stock Ledger Entry" and name:
		try:
			frappe.get_doc(sle).db_update()
			return True
		except (ValueError, frappe.ValidationError) as exc:
			logger.debug("persist_processed_sle_if_possible: get_doc(mapping) failed: %s", exc)
			# fall through to load-by-name

	if not name:
		return False
	if doctype and doctype != "Stock Ledger Entry":
		return False

	if not frappe.db.exists("Stock Ledger Entry", name):
		return False

	doc = frappe.get_doc("Stock Ledger Entry", name)
	for field in _SLE_PERSIST_FIELDS:
		val = _get_entry_value(sle, field)
		if val is not None and val != "":
			doc.set(field, val)
	doc.db_update()
	return True
