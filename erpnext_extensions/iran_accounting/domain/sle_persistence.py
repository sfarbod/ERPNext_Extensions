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


def reseed_wh_data_from_chronological_previous(engine, sle, *, qty_tol: float = 1e-6) -> bool:
	"""Align ``engine.wh_data`` to the chronologically previous SLE when desynced.

	During Item+WH RIV, dependant expansion / soft-skip can leave ``wh_data`` at a
	future warehouse balance while the next SLE still expects the true previous
	layer (CASE A: MAT-STE-2026-40942 arrived with wh_qty≈15721 instead of 1).
	Consuming to the SLE's frozen qty_after=0 then yields a positive SVD (I3).

	Reseed only when this SLE is a consume-to-zero outward move and the running
	qty disagrees with the DB previous SLE on the same identity.
	"""
	if engine is None or sle is None:
		return False
	wh = getattr(engine, "wh_data", None)
	if wh is None:
		return False
	aq = flt(_get_entry_value(sle, "actual_qty"))
	qa = _get_entry_value(sle, "qty_after_transaction")
	if aq >= -qty_tol:
		return False
	if qa in (None, ""):
		return False
	if abs(flt(qa)) > qty_tol:
		return False

	item_code = _get_entry_value(sle, "item_code")
	warehouse = _get_entry_value(sle, "warehouse")
	name = _get_entry_value(sle, "name")
	if not item_code or not warehouse or not name:
		return False

	posting_datetime = _get_entry_value(sle, "posting_datetime")
	creation = _get_entry_value(sle, "creation")
	params: list = [item_code, warehouse, name]
	clauses = ["item_code=%s", "warehouse=%s", "is_cancelled=0", "name != %s"]
	if posting_datetime:
		clauses.append(
			"(posting_datetime < %s or (posting_datetime = %s and creation < %s))"
		)
		params.extend([posting_datetime, posting_datetime, creation or posting_datetime])
	elif creation:
		clauses.append("creation < %s")
		params.append(creation)

	row = frappe.db.sql(
		f"""
		select qty_after_transaction, stock_value, valuation_rate
		from `tabStock Ledger Entry`
		where {' and '.join(clauses)}
		order by posting_datetime desc, creation desc
		limit 1
		""",
		tuple(params),
		as_dict=True,
	)
	if not row:
		return False
	prev = row[0]
	expected_qty = flt(prev.qty_after_transaction)
	if abs(flt(wh.qty_after_transaction) - expected_qty) <= qty_tol:
		return False

	sv = flt(prev.stock_value)
	rate = flt(prev.valuation_rate)
	# Mid-chain RIV can persist a negative layer at qty>0. Reseeding that
	# poison into wh_data makes the next consume-to-zero absorb produce
	# positive outbound SVD (I3 / CASE A). Reconstruct a non-negative layer.
	if sv < 0:
		if rate > 0 and expected_qty > qty_tol:
			sv = rate * expected_qty
		else:
			sv = 0.0

	wh.qty_after_transaction = expected_qty
	wh.stock_value = sv
	wh.valuation_rate = rate
	wh.prev_stock_value = sv
	return True


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
		# Keep prev_stock_value aligned. Leaving a stale prev (e.g. from an earlier
		# SLE) makes the next SVD = stock_value - stale_prev and can flip sign (I3)
		# on a later consume-to-zero (CASE A / MAT-STE-2026-40942).
		wh.prev_stock_value = flt(pre.get("stock_value"))
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
