# Copyright (c) 2026, ERPNext Extensions contributors
"""Reject-only Manufacture rate selection during Core SLE rebuild / RIV.

Core ``UpdateEntriesAfter.get_produced_row_rate`` requires an ``is_finished_item``
absorber. Reject-only Manufacture has MAIN_PRODUCT_REJECT only (no MAIN_FG), so
Core retains the historical SLE ``incoming_rate`` (often 0). Iran SED may already
carry the absorbed-cost valuation from ``allocate_reject_only_absorbed_cost`` /
historical repair — this module surfaces that document rate into Core's produced-
row path so SLE, SABE ``update_bundle_rate``, Bin, and Stock Entry GL follow.

Also guards ``apply_integer_sabe_policy_to_sle``: when Iran SED sync has already
priced the SLE but SABE child rows remain at historical zero, seeding SABE from
the SLE prevents the integer SABE policy from clobbering document economics.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.domain.currency import is_irr_company
from erpnext_extensions.iran_accounting.scrap_costing import (
	CLASS_MAIN_PRODUCT_REJECT,
	_has_reject_only,
	classify_manufacture_outputs,
)

_CACHE_ATTR = "_iran_reject_only_riv_cache"


def _voucher_cache() -> dict[str, Any]:
	cache = getattr(frappe.local, _CACHE_ATTR, None)
	if cache is None:
		cache = {}
		setattr(frappe.local, _CACHE_ATTR, cache)
	return cache


def clear_reject_only_riv_cache() -> None:
	"""Drop per-request reject-only RIV classification cache (tests / long jobs)."""
	if hasattr(frappe.local, _CACHE_ATTR):
		delattr(frappe.local, _CACHE_ATTR)


def _load_reject_only_context(voucher_no: str) -> dict[str, Any] | None:
	"""Cached Manufacture context: reject-only flag + MAIN_PRODUCT_REJECT names."""
	cache = _voucher_cache()
	if voucher_no in cache:
		return cache[voucher_no]

	meta = frappe.db.get_value(
		"Stock Entry",
		voucher_no,
		["purpose", "company", "docstatus"],
		as_dict=True,
	)
	if not meta or meta.purpose != "Manufacture" or not is_irr_company(meta.company):
		cache[voucher_no] = None
		return None

	doc = frappe.get_doc("Stock Entry", voucher_no)
	if not _has_reject_only(doc):
		cache[voucher_no] = None
		return None

	classified = classify_manufacture_outputs(doc)
	rejects = classified.get(CLASS_MAIN_PRODUCT_REJECT) or []
	ctx = {
		"company": meta.company,
		"reject_names": {r.name for r in rejects if r.get("name")},
		"reject_by_name": {r.name: r for r in rejects if r.get("name")},
	}
	cache[voucher_no] = ctx
	return ctx


def get_iran_reject_only_produced_rate(sle) -> float | None:
	"""Return SED amount÷qty for reject-only MAIN_PRODUCT_REJECT during RIV.

	Returns ``None`` when Core behaviour must be left unchanged (not reject-only,
	not this SLE's detail row, intentional zero, or unvalued SED).
	"""
	if sle is None:
		return None
	voucher_type = sle.get("voucher_type") if hasattr(sle, "get") else getattr(sle, "voucher_type", None)
	voucher_no = sle.get("voucher_no") if hasattr(sle, "get") else getattr(sle, "voucher_no", None)
	detail_no = (
		sle.get("voucher_detail_no") if hasattr(sle, "get") else getattr(sle, "voucher_detail_no", None)
	)
	actual_qty = flt(sle.get("actual_qty") if hasattr(sle, "get") else getattr(sle, "actual_qty", 0))
	if voucher_type != "Stock Entry" or not voucher_no or not detail_no or actual_qty <= 0:
		return None

	ctx = _load_reject_only_context(voucher_no)
	if not ctx or detail_no not in ctx["reject_names"]:
		return None

	row = ctx["reject_by_name"].get(detail_no)
	if row is None:
		row = frappe.db.get_value(
			"Stock Entry Detail",
			detail_no,
			[
				"name",
				"amount",
				"valuation_rate",
				"basic_rate",
				"qty",
				"transfer_qty",
				"allow_zero_valuation_rate",
			],
			as_dict=True,
		)
	if not row:
		return None
	if cint(row.get("allow_zero_valuation_rate")):
		return None

	amount = flt(row.get("amount"))
	if abs(amount) <= 0.5:
		# Fall back to valuation_rate × qty when amount not yet mirrored.
		rate = flt(row.get("valuation_rate")) or flt(row.get("basic_rate"))
		if abs(rate) <= 0.5:
			return None
		return float(rate)

	return float(amount / actual_qty)


def should_seed_sabe_from_document_sle(sle, sabe_rows: list) -> bool:
	"""True when SLE already carries document value but SABE children are still zero.

	After Iran ``sync_irr_sle_from_stock_entry_row`` stamps SED onto a reject-only
	incoming SLE, Core may have left SABE at historical zero. Integer SABE policy
	must not overwrite the SLE with that zero aggregate.
	"""
	if sle is None or not sabe_rows:
		return False
	actual_qty = flt(sle.get("actual_qty") if hasattr(sle, "get") else getattr(sle, "actual_qty", 0))
	if actual_qty <= 0:
		return False
	sle_svd = flt(
		sle.get("stock_value_difference")
		if hasattr(sle, "get")
		else getattr(sle, "stock_value_difference", 0)
	)
	sle_rate = flt(
		sle.get("incoming_rate") if hasattr(sle, "get") else getattr(sle, "incoming_rate", 0)
	)
	if abs(sle_svd) <= 0.5 and abs(sle_rate) <= 0.5:
		return False

	sabe_svd = sum(flt(r.get("stock_value_difference")) for r in sabe_rows)
	sabe_rate = sum(
		abs(flt(r.get("incoming_rate")) or flt(r.get("outgoing_rate")) or 0.0) for r in sabe_rows
	)
	if abs(sabe_svd) > 0.5 or sabe_rate > 0.5:
		return False

	voucher_type = sle.get("voucher_type") if hasattr(sle, "get") else getattr(sle, "voucher_type", None)
	voucher_no = sle.get("voucher_no") if hasattr(sle, "get") else getattr(sle, "voucher_no", None)
	detail_no = (
		sle.get("voucher_detail_no") if hasattr(sle, "get") else getattr(sle, "voucher_detail_no", None)
	)
	if voucher_type != "Stock Entry" or not voucher_no or not detail_no:
		return False

	ctx = _load_reject_only_context(voucher_no)
	return bool(ctx and detail_no in ctx["reject_names"])


def reject_only_sed_sle_lag_identities(voucher_no: str) -> list[dict[str, Any]]:
	"""Item+WH identities where reject-only SED is valued but incoming SLE is still ~0.

	Used to force a fresh Item+WH RIV when a chronologically covering Completed RIV
	would otherwise skip repost after SED-only historical repair.
	"""
	ctx = _load_reject_only_context(voucher_no)
	if not ctx:
		return []

	lags: list[dict[str, Any]] = []
	seen: set[tuple[str, str]] = set()
	for detail_no in ctx["reject_names"]:
		row = ctx["reject_by_name"].get(detail_no) or frappe.db.get_value(
			"Stock Entry Detail",
			detail_no,
			["name", "item_code", "t_warehouse", "amount", "valuation_rate", "allow_zero_valuation_rate"],
			as_dict=True,
		)
		if not row or cint(row.get("allow_zero_valuation_rate")):
			continue
		amount = flt(row.get("amount"))
		rate = flt(row.get("valuation_rate"))
		if abs(amount) <= 0.5 and abs(rate) <= 0.5:
			continue
		sle = frappe.db.get_value(
			"Stock Ledger Entry",
			{
				"voucher_type": "Stock Entry",
				"voucher_no": voucher_no,
				"voucher_detail_no": detail_no,
				"is_cancelled": 0,
				"actual_qty": (">", 0),
			},
			[
				"name",
				"item_code",
				"warehouse",
				"incoming_rate",
				"stock_value_difference",
				"posting_date",
				"posting_time",
			],
			as_dict=True,
		)
		if not sle:
			continue
		if abs(flt(sle.incoming_rate)) > 0.5 and abs(flt(sle.stock_value_difference)) > 0.5:
			continue
		key = (sle.item_code, sle.warehouse)
		if key in seen:
			continue
		seen.add(key)
		lags.append(
			{
				"item_code": sle.item_code,
				"warehouse": sle.warehouse,
				"posting_date": sle.posting_date,
				"posting_time": sle.posting_time,
				"voucher_detail_no": detail_no,
				"sed_amount": amount,
				"sle_incoming_rate": flt(sle.incoming_rate),
				"sle_svd": flt(sle.stock_value_difference),
			}
		)
	return lags


def seed_sabe_rows_from_sle(sle, sabe_rows: list) -> None:
	"""Stamp SLE incoming economics onto in-memory SABE row dicts (single-batch safe).

	Multi-batch reject rows share the SLE rate; each row's SVD is rate×qty so the
	bundle total matches the SLE movement when quantities sum to ``actual_qty``.
	"""
	actual_qty = flt(sle.get("actual_qty") if hasattr(sle, "get") else getattr(sle, "actual_qty", 0))
	sle_svd = flt(
		sle.get("stock_value_difference")
		if hasattr(sle, "get")
		else getattr(sle, "stock_value_difference", 0)
	)
	sle_rate = flt(
		sle.get("incoming_rate") if hasattr(sle, "get") else getattr(sle, "incoming_rate", 0)
	)
	if abs(sle_rate) <= 0.5 and abs(actual_qty) > 1e-9:
		sle_rate = abs(sle_svd) / abs(actual_qty)

	def _row_get(row, key, default=None):
		if isinstance(row, dict):
			return row.get(key, default)
		if hasattr(row, "get"):
			return row.get(key, default)
		return getattr(row, key, default)

	def _row_set(row, key, value):
		if isinstance(row, dict):
			row[key] = value
		else:
			setattr(row, key, value)

	qty_sum = sum(flt(_row_get(r, "qty")) for r in sabe_rows)
	for row in sabe_rows:
		qty = flt(_row_get(row, "qty"))
		if abs(qty_sum - actual_qty) <= 1e-6 and len(sabe_rows) == 1:
			row_svd = sle_svd
			row_rate = sle_rate
		else:
			row_svd = float(sle_rate * qty)
			row_rate = sle_rate
		_row_set(row, "incoming_rate", float(row_rate))
		_row_set(row, "outgoing_rate", 0.0)
		_row_set(row, "stock_value_difference", float(row_svd))
