# Copyright (c) 2026, ERPNext Extensions contributors
"""ETO subcontracting repost scope (v5.8.1).

Problem (docs/investigations/ETO_SCR_REPOST_PROPAGATION.md): after an ETO
Subcontracting Receipt's service cost is corrected, Repost Item Valuation
computes the new batch-wise outward rate in Core ``BatchNoValuation`` and
writes it to ``Serial and Batch Entry.incoming_rate`` / ``stock_value_difference``.
The integer SABE policy then picked the row's *stale* ``outgoing_rate`` (stamped
by an earlier integerization) as the raw rate and wrote the old rate back, so
the transfer, Manufacture consume, finished good and later issues never moved.

This module decides when the integer SABE policy must trust Core's fresh
outward rate instead of the stale ``outgoing_rate``. Scope is deliberately
limited to reposts started for ETO outsourcing:

* RIV *Transaction* on a Subcontracting Receipt whose Subcontracting Order
  bills an ETO service item; or
* RIV *Item and Warehouse* for an item that is the finished good of an ETO
  service item on any Subcontracting Order.

Every SLE processed inside such a repost (including dependants such as the
finished good and its issues) uses Core's fresh outward rate. Reposts for all
other items keep the existing behaviour unchanged.

ETO service items default to ``23000001`` and can be overridden with the
site config key ``iran_eto_service_items`` (list of item codes).
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt

DEFAULT_ETO_SERVICE_ITEMS = frozenset({"23000001"})

# IRR quantum: rates that round to the same integer are the same rate.
_RATE_EPS = 0.5

_CACHE_ATTR = "iran_eto_repost_scope_cache"


def get_eto_service_items() -> frozenset[str]:
	try:
		configured = frappe.conf.get("iran_eto_service_items")
	except Exception:
		configured = None
	if configured:
		return frozenset(str(code) for code in configured)
	return DEFAULT_ETO_SERVICE_ITEMS


def _get(obj, field, default=None):
	if obj is None:
		return default
	if hasattr(obj, "get"):
		value = obj.get(field)
		return default if value is None else value
	return getattr(obj, field, default)


def is_eto_subcontracting_receipt(scr_name: str | None) -> bool:
	"""True when any Subcontracting Order behind the SCR bills an ETO service item."""
	if not scr_name:
		return False
	orders = frappe.get_all(
		"Subcontracting Receipt Item",
		filters={"parent": scr_name},
		pluck="subcontracting_order",
	)
	orders = sorted({o for o in orders if o})
	if not orders:
		return False
	return bool(
		frappe.get_all(
			"Subcontracting Order Service Item",
			filters={
				"parent": ("in", orders),
				"item_code": ("in", sorted(get_eto_service_items())),
			},
			limit=1,
			pluck="name",
		)
	)


def is_eto_finished_good(item_code: str | None) -> bool:
	"""True when the item is the finished good of an ETO service item."""
	if not item_code:
		return False
	return bool(
		frappe.get_all(
			"Subcontracting Order Service Item",
			filters={
				"fg_item": item_code,
				"item_code": ("in", sorted(get_eto_service_items())),
				"docstatus": 1,
			},
			limit=1,
			pluck="name",
		)
	)


def is_eto_repost_doc(repost_doc: Any) -> bool:
	"""True when a Repost Item Valuation was started for ETO outsourcing."""
	if not repost_doc:
		return False
	based_on = _get(repost_doc, "based_on")
	if based_on == "Transaction":
		return _get(repost_doc, "voucher_type") == "Subcontracting Receipt" and is_eto_subcontracting_receipt(
			_get(repost_doc, "voucher_no")
		)
	if based_on == "Item and Warehouse":
		return is_eto_finished_good(_get(repost_doc, "item_code"))
	return False


def is_eto_repost_in_progress(engine: Any = None) -> bool:
	"""True inside a Repost Item Valuation run that is in ETO scope.

	Resolution is cached per repost document for the life of the request.
	"""
	try:
		if not frappe.flags.get("through_repost_item_valuation"):
			return False
	except Exception:
		return False

	if engine is None:
		engine = getattr(frappe.local, "iran_riv_update_entries_after", None)
	repost_doc = _get(engine, "repost_doc") if engine is not None else None
	if not repost_doc:
		return False

	key = _get(repost_doc, "name") or id(repost_doc)
	cache = getattr(frappe.local, _CACHE_ATTR, None)
	if cache is None:
		cache = {}
		setattr(frappe.local, _CACHE_ATTR, cache)
	if key not in cache:
		try:
			cache[key] = is_eto_repost_doc(repost_doc)
		except Exception:
			cache[key] = False
	return cache[key]


def should_align_empty_warehouse_sle_from_batch_sabe(sle: Any, engine: Any = None) -> bool:
	"""True when ETO repost must stamp batch SABE onto an empty-warehouse outward SLE.

	Default Iran policy keeps consume-to-zero warehouse MA residual as SLE
	authority (do not stamp batch SVD). For ETO finished goods that empties a
	quarantine warehouse, that residual is often understated relative to the
	batch layer (MAT-SCR-2026-00011 pattern): SABE stays at the batch rate while
	SLE/SED absorb the warehouse residual. During an ETO-scoped repost, prefer
	the batch SABE movement so Transfer / Manufacture carry the SCR rate.
	"""
	if not is_eto_repost_in_progress(engine):
		return False
	item_code = _get(sle, "item_code")
	if not is_eto_finished_good(item_code):
		return False
	if not _get(sle, "serial_and_batch_bundle"):
		return False
	actual_qty = flt(_get(sle, "actual_qty"))
	qty_after = flt(_get(sle, "qty_after_transaction"))
	if actual_qty >= 0 or abs(qty_after) > 1e-9:
		return False
	return True


def select_sabe_raw_rate(row: Any, *, prefer_core_outward_rate: bool = False) -> float:
	"""Raw rate the integer SABE policy localizes for one Serial and Batch Entry.

	Core ``update_serial_batch_no_valuation`` writes the fresh batch-wise outward
	rate into ``incoming_rate`` and never clears a prior ``outgoing_rate`` stamp.
	When the two disagree on an outward row, ``outgoing_rate`` is stale history —
	always prefer Core's ``incoming_rate`` (MCA/RIV transfer recovery depends on
	this; previously only ETO set ``prefer_core_outward_rate`` and Farvardin
	Material Transfers kept pre-MCA basic rates after a "Completed" RIV).

	``prefer_core_outward_rate`` is retained for call-site compatibility; the
	disagree→incoming rule applies regardless.
	"""
	qty = flt(_get(row, "qty"))
	incoming = flt(_get(row, "incoming_rate"))
	outgoing = flt(_get(row, "outgoing_rate"))
	if qty < 0 and outgoing:
		if incoming and abs(incoming - outgoing) >= _RATE_EPS:
			return incoming
		return outgoing
	return incoming or outgoing or 0.0
