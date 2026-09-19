# Copyright (c) 2026, ERPNext Extensions contributors
"""RIV valuation-scope helpers for integrity-guard gating.

ERPNext Moving Average identity is Item + Warehouse. Repost Item Valuation may
traverse multi-hop ``dependant_sle_voucher_detail_no`` chains that pull in other
items (Manufacture FG cascades). Legacy/converted SLE corruption on those
*other* items must not abort an otherwise valid target-item RIV.

Blocking scope (fail-closed):
  - Outside an Item-scoped RIV context → always block (normal submit / unknown).
  - Inside RIV → block only when the offending SLE's ``item_code`` is one of the
    RIV's *declared* target items (``Repost Item Valuation.item_code`` and/or
    ``items_to_be_repost``), not the expanded dependant closure.

Same-item / any-warehouse remains blocking so transfer chains for the target
item stay atomic. Different item codes are out-of-scope (logged, non-blocking).
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import cstr


def _entry_get(obj, field, default=None):
	if obj is None:
		return default
	if hasattr(obj, "get"):
		value = obj.get(field)
		return default if value is None else value
	return getattr(obj, field, default)


_FLAG_ANOMALIES = "iran_riv_out_of_scope_anomalies"
_LOGGER_NAME = "iran_accounting.riv_out_of_scope"
_FALLBACK_ANOMALIES: list[dict[str, Any]] = []
_FALLBACK_LOGGED: set[str] = set()


def _flags_get(name, default=None):
	try:
		return frappe.flags.get(name, default)
	except Exception:
		if name == _FLAG_ANOMALIES:
			return _FALLBACK_ANOMALIES if default is None else default
		if name == "iran_riv_out_of_scope_logged":
			return _FALLBACK_LOGGED if default is None else default
		return default


def _flags_set(name, value):
	try:
		frappe.flags[name] = value
	except Exception:
		if name == _FLAG_ANOMALIES:
			_FALLBACK_ANOMALIES.clear()
			_FALLBACK_ANOMALIES.extend(value or [])
		elif name == "iran_riv_out_of_scope_logged":
			_FALLBACK_LOGGED.clear()
			_FALLBACK_LOGGED.update(value or set())


def clear_out_of_scope_anomaly_buffers() -> None:
	"""Test helper: reset in-memory anomaly buffers."""
	_FALLBACK_ANOMALIES.clear()
	_FALLBACK_LOGGED.clear()
	try:
		frappe.flags[_FLAG_ANOMALIES] = []
		frappe.flags["iran_riv_out_of_scope_logged"] = set()
	except Exception:
		pass


def get_out_of_scope_anomalies() -> list[dict[str, Any]]:
	try:
		anomalies = frappe.flags.get(_FLAG_ANOMALIES)
		if isinstance(anomalies, list):
			return anomalies
	except Exception:
		pass
	return list(_FALLBACK_ANOMALIES)


def _entry_batch(sle) -> str | None:
	batch = _entry_get(sle, "batch_no")
	if batch:
		return cstr(batch)
	bundle = _entry_get(sle, "serial_and_batch_bundle")
	if not bundle:
		return None
	try:
		return frappe.db.get_value("Serial and Batch Entry", {"parent": bundle}, "batch_no")
	except Exception:
		return None


def get_riv_target_item_codes(engine) -> set[str] | None:
	"""Declared RIV target item codes, or ``None`` for fail-closed (no bypass).

	``None`` means the caller must not soften integrity errors (non-RIV paths,
	or RIV without a resolvable item target list).
	"""
	if engine is None:
		return None

	targets: set[str] = set()

	repost_doc = getattr(engine, "repost_doc", None)
	if repost_doc is not None:
		item = _entry_get(repost_doc, "item_code")
		if item:
			# Item-and-Warehouse RIV: declared target is only item_code.
			# Do NOT merge items_to_be_repost — ERPNext expands that list with
			# dependant_sle_voucher_detail_no hops (other items).
			targets.add(cstr(item))
		else:
			# Transaction-based RIV (no single item_code): use the JSON list.
			# Prefer a frozen declaration cached on flags for this RIV name.
			raw_items = _entry_get(repost_doc, "items_to_be_repost")
			riv_name = cstr(_entry_get(repost_doc, "name") or "")
			cached = None
			try:
				cache = frappe.flags.setdefault("iran_riv_declared_target_items", {})
				cached = cache.get(riv_name) if riv_name else None
			except Exception:
				cache = None
			if cached:
				targets.update(cached)
			elif raw_items:
				try:
					payload = json.loads(raw_items) if isinstance(raw_items, str) else raw_items
				except Exception:
					payload = None
				if isinstance(payload, list):
					for row in payload:
						if isinstance(row, dict) and row.get("item_code"):
							targets.add(cstr(row["item_code"]))
					if riv_name and cache is not None and targets:
						# Freeze first-seen list before dependant expansion mutates it.
						cache[riv_name] = set(targets)

	args = getattr(engine, "args", None)
	if args is not None and not targets:
		# Fallback only when repost_doc did not declare targets. Never use the
		# current args.item_code once dependant walking has started — that field
		# is the SLE under process, not the RIV declaration.
		items_to_be_repost = (
			args.get("items_to_be_repost") if hasattr(args, "get") else getattr(args, "items_to_be_repost", None)
		)
		# If the list has a single seed row matching a stable declaration, use it;
		# otherwise fail-closed (None) rather than treating the expanded closure
		# as targets.
		if isinstance(items_to_be_repost, list) and items_to_be_repost:
			seed = items_to_be_repost[0] if isinstance(items_to_be_repost[0], dict) else None
			if seed and seed.get("item_code") and len(items_to_be_repost) == 1:
				targets.add(cstr(seed["item_code"]))
			elif not getattr(engine, "distinct_item_and_warehouse", None):
				item = args.get("item_code") if hasattr(args, "get") else getattr(args, "item_code", None)
				if item:
					targets.add(cstr(item))

	if targets:
		return targets

	# In-progress RIV without item list → fail-closed.
	through_riv = False
	try:
		through_riv = bool(frappe.flags.get("through_repost_item_valuation"))
	except Exception:
		through_riv = False
	if repost_doc is not None or through_riv:
		return None

	return None


def get_riv_target_warehouses(engine) -> set[str]:
	"""Optional warehouse hints from the RIV declaration (logging / diagnostics)."""
	warehouses: set[str] = set()
	if engine is None:
		return warehouses

	repost_doc = getattr(engine, "repost_doc", None)
	if repost_doc is not None:
		wh = _entry_get(repost_doc, "warehouse")
		if wh:
			warehouses.add(cstr(wh))
		raw_items = _entry_get(repost_doc, "items_to_be_repost")
		if raw_items:
			try:
				payload = json.loads(raw_items) if isinstance(raw_items, str) else raw_items
			except Exception:
				payload = None
			if isinstance(payload, list):
				for row in payload:
					if isinstance(row, dict) and row.get("warehouse"):
						warehouses.add(cstr(row["warehouse"]))

	args = getattr(engine, "args", None)
	if args is not None:
		wh = args.get("warehouse") if hasattr(args, "get") else getattr(args, "warehouse", None)
		if wh:
			warehouses.add(cstr(wh))

	return warehouses


def is_sle_in_riv_blocking_scope(engine, sle) -> bool:
	"""True → integrity violation must abort the RIV / submit.

	Same target item (any warehouse) is blocking so transfer chains stay atomic.
	Different item codes reached only via dependant expansion are non-blocking.
	"""
	targets = get_riv_target_item_codes(engine)
	if targets is None:
		return True
	item_code = cstr(_entry_get(sle, "item_code") or "")
	if not item_code:
		return True
	return item_code in targets


def is_stock_entry_in_riv_blocking_scope(engine, doc) -> bool:
	"""SE-level asserts block when the voucher includes any RIV target item."""
	targets = get_riv_target_item_codes(engine)
	if targets is None:
		return True
	rows = []
	if hasattr(doc, "get"):
		rows = doc.get("items") or []
	elif getattr(doc, "items", None) is not None:
		rows = doc.items or []
	for row in rows:
		if cstr(_entry_get(row, "item_code") or "") in targets:
			return True
	return False


def _current_riv_name(engine) -> str | None:
	repost_doc = getattr(engine, "repost_doc", None) if engine is not None else None
	if repost_doc is not None and _entry_get(repost_doc, "name"):
		return cstr(_entry_get(repost_doc, "name"))
	if frappe.flags.get("through_repost_item_valuation"):
		try:
			return frappe.db.get_value(
				"Repost Item Valuation",
				{"status": "In Progress"},
				"name",
				order_by="modified desc",
			)
		except Exception:
			return None
	return None


def log_out_of_scope_integrity_anomaly(
	engine,
	*,
	sle=None,
	doc=None,
	exc: Exception | None = None,
	invariant: str | None = None,
	detail: str | None = None,
) -> dict[str, Any]:
	"""Record a non-blocking legacy anomaly for later historical repair."""
	targets = get_riv_target_item_codes(engine) or set()
	target_warehouses = get_riv_target_warehouses(engine)
	riv_name = _current_riv_name(engine)

	payload: dict[str, Any] = {
		"riv_name": riv_name,
		"target_items": sorted(targets),
		"target_warehouses": sorted(target_warehouses),
		"invariant": invariant,
		"detail": detail,
		"reason": (
			"offending item_code is outside declared RIV target item set; "
			"treated as unrelated legacy/converted valuation anomaly"
		),
	}

	if sle is not None:
		payload.update(
			{
				"offending_sle": _entry_get(sle, "name"),
				"offending_voucher_type": _entry_get(sle, "voucher_type"),
				"offending_voucher_no": _entry_get(sle, "voucher_no"),
				"offending_voucher_detail_no": _entry_get(sle, "voucher_detail_no"),
				"offending_item_code": _entry_get(sle, "item_code"),
				"offending_warehouse": _entry_get(sle, "warehouse"),
				"offending_batch_no": _entry_batch(sle),
				"actual_qty": _entry_get(sle, "actual_qty"),
				"incoming_rate": _entry_get(sle, "incoming_rate"),
				"valuation_rate": _entry_get(sle, "valuation_rate"),
				"stock_value_difference": _entry_get(sle, "stock_value_difference"),
			}
		)

	if doc is not None:
		payload.update(
			{
				"offending_voucher_type": _entry_get(doc, "doctype") or "Stock Entry",
				"offending_voucher_no": _entry_get(doc, "name"),
			}
		)

	if exc is not None and not invariant:
		msg = cstr(exc)
		if "I1" in msg:
			payload["invariant"] = payload.get("invariant") or "I1"
		elif "I2" in msg:
			payload["invariant"] = payload.get("invariant") or "I2"
		elif "I3" in msg:
			payload["invariant"] = payload.get("invariant") or "I3"
		elif "I4" in msg:
			payload["invariant"] = payload.get("invariant") or "I4"
		elif "I5" in msg:
			payload["invariant"] = payload.get("invariant") or "I5"
		payload["exception"] = msg[:2000]

	anomalies = None
	try:
		anomalies = frappe.flags.get(_FLAG_ANOMALIES)
	except Exception:
		anomalies = None
	if not isinstance(anomalies, list):
		anomalies = _FALLBACK_ANOMALIES
		try:
			frappe.flags[_FLAG_ANOMALIES] = anomalies
		except Exception:
			pass
	anomalies.append(payload)

	try:
		frappe.logger(_LOGGER_NAME).warning(
			"RIV out-of-scope integrity anomaly (non-blocking): %s",
			json.dumps(payload, default=str, ensure_ascii=False),
		)
	except Exception:
		pass

	# Deduped Error Log for desk audit (one per riv+voucher+item+invariant).
	dedupe_key = "|".join(
		[
			cstr(payload.get("riv_name") or ""),
			cstr(payload.get("offending_voucher_no") or ""),
			cstr(payload.get("offending_item_code") or ""),
			cstr(payload.get("invariant") or ""),
		]
	)
	try:
		seen = frappe.flags.setdefault("iran_riv_out_of_scope_logged", set())
	except Exception:
		seen = _FALLBACK_LOGGED
	if dedupe_key not in seen:
		seen.add(dedupe_key)
		message = json.dumps(payload, default=str, ensure_ascii=False, indent=2)
		title = "RIV out-of-scope integrity (non-blocking)"
		# Site file log survives RIV rollback (Error Log rows do not).
		try:
			from frappe.utils import get_site_path

			log_path = get_site_path("logs", "iran_riv_out_of_scope.log")
			with open(log_path, "a", encoding="utf-8") as fh:
				fh.write(f"{title}\n{message}\n---\n")
		except Exception:
			pass
		try:
			frappe.log_error(title=title, message=message)
			# Persist audit independently of a later repost() rollback.
			if not frappe.in_test:
				frappe.db.commit()
		except Exception:
			pass

	return payload


def run_integrity_assert_in_riv_scope(engine, sle, assert_fn, *, doc=None) -> None:
	"""Run ``assert_fn``; re-raise only when the SLE/doc is in blocking scope."""
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		ValuationIntegrityError,
	)

	try:
		assert_fn()
	except ValuationIntegrityError as exc:
		blocking = (
			is_stock_entry_in_riv_blocking_scope(engine, doc)
			if doc is not None and sle is None
			else is_sle_in_riv_blocking_scope(engine, sle)
		)
		if blocking:
			raise
		log_out_of_scope_integrity_anomaly(engine, sle=sle, doc=doc, exc=exc)
