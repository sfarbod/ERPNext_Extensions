# Copyright (c) 2026, ERPNext Extensions contributors
"""Sales-return incoming_rate hierarchy (no selling-rate substitution).

Priority:
1. Original ``return_against`` outgoing valuation (Core ``get_rate_for_return``)
2. Valid Batch / stock ledger valuation (``_get_incoming_rate`` > 0)
3. Earliest valid Opening Stock valuation for the Item + Company
4. Explicit ``allow_zero_valuation_rate`` or a clear validation error

Core standalone-return fallback to ``item.rate`` (selling price) is stripped and
must never become inventory cost — including FOC rows where selling rate is 0
and paid rows where Core copied the price list rate into ``incoming_rate``.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.domain.currency import (
	is_irr_company,
	round_monetary_rate,
)

OPENING_SR_PURPOSE = "Opening Stock"


def _integer_irr_rate(rate: float, company: str | None, currency: str | None = None) -> float:
	rate = flt(rate)
	if not rate:
		return 0.0
	if company and is_irr_company(company):
		return float(round_monetary_rate(rate, currency or "IRR"))
	return rate


def get_valid_stock_valuation_rate(
	*,
	item_code: str,
	warehouse: str | None,
	company: str | None,
	posting_date=None,
	posting_time=None,
	qty: float | None = None,
	batch_no: str | None = None,
	serial_and_batch_bundle: str | None = None,
	voucher_type: str | None = None,
	voucher_no: str | None = None,
	voucher_detail_no: str | None = None,
	allow_zero_valuation: int | bool = 0,
) -> float:
	"""Priority 2: non-zero rate from batch/stock ledger valuation only."""
	if not item_code or not warehouse:
		return 0.0

	from erpnext.stock.utils import _get_incoming_rate

	has_batch_no = cint(frappe.get_cached_value("Item", item_code, "has_batch_no"))
	if has_batch_no:
		if not batch_no or not frappe.db.exists("Batch", batch_no):
			# Missing / unknown batch is not valid batch valuation evidence.
			return 0.0

	args = {
		"item_code": item_code,
		"warehouse": warehouse,
		"posting_date": posting_date,
		"posting_time": posting_time,
		"qty": qty,
		"batch_no": batch_no,
		"serial_and_batch_bundle": serial_and_batch_bundle,
		"company": company,
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"voucher_detail_no": voucher_detail_no,
		"allow_zero_valuation": allow_zero_valuation,
	}
	rate = flt(_get_incoming_rate(args, raise_error_if_no_rate=False, fallbacks=False))
	return _integer_irr_rate(rate, company)


def get_earliest_opening_stock_valuation_rate(
	*,
	item_code: str,
	company: str,
	warehouse: str | None = None,
	currency: str | None = None,
) -> float:
	"""Priority 3: earliest Opening Stock valuation for Item + Company.

	Preference order:
	1. Opening Stock Reconciliation / Opening Material Receipt in the same warehouse
	2. Earliest opening valuation for the Item in any warehouse of the Company
	"""
	if not item_code or not company:
		return 0.0

	candidates: list[dict] = []

	# Stock Reconciliation purpose = Opening Stock
	sr_rows = frappe.db.sql(
		"""
		SELECT sr.name AS voucher_no, sr.posting_date, sr.posting_time, sr.creation,
		       sri.warehouse, sri.valuation_rate AS rate, sri.qty
		FROM `tabStock Reconciliation` sr
		INNER JOIN `tabStock Reconciliation Item` sri ON sri.parent = sr.name
		WHERE sr.docstatus = 1
		  AND sr.company = %s
		  AND sr.purpose = %s
		  AND sri.item_code = %s
		  AND IFNULL(sri.valuation_rate, 0) > 0
		  AND IFNULL(sri.qty, 0) != 0
		ORDER BY sr.posting_date, sr.posting_time, sr.creation, sri.idx
		""",
		(company, OPENING_SR_PURPOSE, item_code),
		as_dict=True,
	)
	for row in sr_rows or []:
		candidates.append(
			{
				"posting_date": row.get("posting_date"),
				"posting_time": row.get("posting_time"),
				"creation": row.get("creation"),
				"warehouse": row.get("warehouse"),
				"rate": flt(row.get("rate")),
				"source": "Stock Reconciliation",
				"voucher_no": row.get("voucher_no"),
			}
		)

	# Stock Entry marked as opening Material Receipt
	se_rows = frappe.db.sql(
		"""
		SELECT se.name AS voucher_no, se.posting_date, se.posting_time, se.creation,
		       sed.t_warehouse AS warehouse,
		       IFNULL(NULLIF(sed.valuation_rate, 0), sed.basic_rate) AS rate,
		       sed.transfer_qty AS qty
		FROM `tabStock Entry` se
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.docstatus = 1
		  AND se.company = %s
		  AND se.is_opening = 'Yes'
		  AND sed.item_code = %s
		  AND IFNULL(sed.t_warehouse, '') != ''
		  AND IFNULL(NULLIF(sed.valuation_rate, 0), sed.basic_rate) > 0
		  AND IFNULL(sed.transfer_qty, sed.qty) != 0
		ORDER BY se.posting_date, se.posting_time, se.creation, sed.idx
		""",
		(company, item_code),
		as_dict=True,
	)
	for row in se_rows or []:
		candidates.append(
			{
				"posting_date": row.get("posting_date"),
				"posting_time": row.get("posting_time"),
				"creation": row.get("creation"),
				"warehouse": row.get("warehouse"),
				"rate": flt(row.get("rate")),
				"source": "Stock Entry",
				"voucher_no": row.get("voucher_no"),
			}
		)

	return select_opening_rate_from_candidates(
		candidates, warehouse=warehouse, company=company, currency=currency
	)


def select_opening_rate_from_candidates(
	candidates: list[dict],
	*,
	warehouse: str | None = None,
	company: str | None = None,
	currency: str | None = None,
) -> float:
	"""Pick preferred opening rate: same warehouse first, then earliest posting."""
	if not candidates:
		return 0.0

	def _sort_key(row: dict):
		return (
			0 if warehouse and row.get("warehouse") == warehouse else 1,
			row.get("posting_date") or "",
			str(row.get("posting_time") or ""),
			row.get("creation") or "",
		)

	ordered = sorted(candidates, key=_sort_key)
	return _integer_irr_rate(ordered[0]["rate"], company, currency)


def resolve_sales_return_incoming_rate(
	*,
	item_code: str,
	warehouse: str | None,
	company: str | None,
	posting_date=None,
	posting_time=None,
	qty: float | None = None,
	batch_no: str | None = None,
	serial_and_batch_bundle: str | None = None,
	voucher_type: str | None = None,
	voucher_no: str | None = None,
	voucher_detail_no: str | None = None,
	return_against: str | None = None,
	original_rate: float | None = None,
	allow_zero_valuation_rate: int | bool = 0,
	currency: str | None = None,
	throw_on_missing: bool = False,
) -> float:
	"""Resolve return incoming_rate using the mandated hierarchy.

	``original_rate`` is Priority 1 when ``return_against`` is set (Core result).
	Selling rate is intentionally not a parameter.
	"""
	# Priority 1 — original transaction
	if return_against and flt(original_rate):
		return _integer_irr_rate(original_rate, company, currency)

	# Priority 2 — valid batch / stock valuation
	stock_rate = get_valid_stock_valuation_rate(
		item_code=item_code,
		warehouse=warehouse,
		company=company,
		posting_date=posting_date,
		posting_time=posting_time,
		qty=qty,
		batch_no=batch_no,
		serial_and_batch_bundle=serial_and_batch_bundle,
		voucher_type=voucher_type,
		voucher_no=voucher_no,
		voucher_detail_no=voucher_detail_no,
		allow_zero_valuation=allow_zero_valuation_rate,
	)
	if stock_rate:
		return stock_rate

	# Priority 3 — earliest opening stock valuation
	opening_rate = get_earliest_opening_stock_valuation_rate(
		item_code=item_code,
		company=company or "",
		warehouse=warehouse,
		currency=currency,
	)
	if opening_rate:
		return opening_rate

	# Priority 4
	if cint(allow_zero_valuation_rate):
		return 0.0

	if throw_on_missing:
		frappe.throw(
			_(
				"Cannot determine inventory valuation for Item {0}"
				"{1} in Company {2}. No original return reference, no valid "
				"Batch stock valuation history, and no Opening Stock valuation "
				"rate were found. Enable 'Allow Zero Valuation Rate' only if "
				"zero-cost stock is intentional."
			).format(
				frappe.bold(item_code),
				(_(" / Batch {0}").format(frappe.bold(batch_no)) if batch_no else ""),
				frappe.bold(company or ""),
			),
			title=_("Missing Return Valuation Rate"),
		)
	return 0.0


def apply_sales_return_valuation_hierarchy(doc: Any, *, throw_on_missing: bool | None = None) -> None:
	"""Replace Core selling-rate / zero incoming_rate with the mandated hierarchy."""
	if not doc or not cint(doc.get("is_return")):
		return
	if doc.doctype not in ("Sales Invoice", "Delivery Note", "POS Invoice"):
		return
	if doc.doctype in ("Sales Invoice", "POS Invoice") and not cint(doc.get("update_stock")):
		return

	# Linked returns keep Core Priority-1 behaviour unless a row is still unvalued.
	return_against = doc.get("return_against")
	if throw_on_missing is None:
		throw_on_missing = getattr(doc, "_action", None) == "submit" or cint(doc.get("docstatus")) == 1

	items = list(doc.get("items") or []) + list(doc.get("packed_items") or [])
	for d in items:
		if not d.get("item_code") or not flt(d.get("qty") or d.get("stock_qty")):
			continue
		if not frappe.get_cached_value("Item", d.item_code, "is_stock_item"):
			continue

		qty = flt(d.get("stock_qty") or d.get("qty"))
		allow_zero = cint(d.get("allow_zero_valuation_rate"))

		if return_against and flt(d.get("incoming_rate")):
			# Priority 1 already populated by Core.
			continue

		d.incoming_rate = resolve_sales_return_incoming_rate(
			item_code=d.item_code,
			warehouse=d.get("warehouse"),
			company=doc.get("company"),
			posting_date=doc.get("posting_date") or doc.get("transaction_date"),
			posting_time=doc.get("posting_time"),
			qty=qty,
			batch_no=d.get("batch_no"),
			serial_and_batch_bundle=d.get("serial_and_batch_bundle"),
			voucher_type=doc.doctype,
			voucher_no=doc.get("name"),
			voucher_detail_no=d.get("name"),
			return_against=return_against,
			original_rate=d.get("incoming_rate") if return_against else None,
			allow_zero_valuation_rate=allow_zero,
			throw_on_missing=throw_on_missing and not allow_zero,
		)


# Backwards-compatible aliases used by the initial 5.7.22 patch surface.
def warehouse_valuation_rate_for_return(**kwargs):
	"""Deprecated alias — prefer opening-stock hierarchy helpers."""
	return get_earliest_opening_stock_valuation_rate(
		item_code=kwargs.get("item_code") or "",
		company=kwargs.get("company") or "",
		warehouse=kwargs.get("warehouse"),
		currency=kwargs.get("currency"),
	)


def resolve_standalone_return_incoming_rate(
	*,
	computed_rate: float | None = None,
	sales_rate: float | None = None,  # ignored — selling rate must not value stock
	item_code: str,
	warehouse: str | None,
	company: str | None,
	voucher_type: str,
	voucher_no: str | None,
	allow_zero_valuation_rate: int | bool = 0,
	currency: str | None = None,
	**kwargs,
) -> float:
	"""Deprecated alias wrapping :func:`resolve_sales_return_incoming_rate`."""
	_ = sales_rate
	return resolve_sales_return_incoming_rate(
		item_code=item_code,
		warehouse=warehouse,
		company=company,
		qty=kwargs.get("qty"),
		batch_no=kwargs.get("batch_no"),
		posting_date=kwargs.get("posting_date"),
		posting_time=kwargs.get("posting_time"),
		voucher_type=voucher_type,
		voucher_no=voucher_no,
		original_rate=computed_rate,
		return_against=kwargs.get("return_against"),
		allow_zero_valuation_rate=allow_zero_valuation_rate,
		currency=currency,
	)


def apply_warehouse_vr_fallback_to_standalone_return_items(doc: Any) -> None:
	"""Deprecated alias for :func:`apply_sales_return_valuation_hierarchy`."""
	apply_sales_return_valuation_hierarchy(doc, throw_on_missing=False)
