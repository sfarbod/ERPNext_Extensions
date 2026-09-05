# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Stock quantity/value measure helpers for Item / Item Group axes.

Presentation contract (v5.1.1): Opening is rolled into In / Inward.
Displayed fields never include separate Opening columns.

Item:
  in_qty / out_qty / balance_qty
  inward_value / outward_value
  debit_balance / credit_balance  (side presentation of signed closing value)

Item Group (value-only):
  inward_value / outward_value
  debit_balance / credit_balance

Signed closing value (internal / parity):
  balance_value = inward_value - outward_value

Side presentation matches Account Explorer ``finalize_measures``:
  balance_value >= 0 → debit_balance = balance_value, credit_balance = 0
  balance_value <  0 → debit_balance = 0, credit_balance = abs(balance_value)

Footer totals: sum signed nets first, then apply side presentation
(do not sum debit/credit columns across rows).

Proven vs ERPNext StockController: inventory GL posts
``debit = stock_value_difference`` on the warehouse inventory account
(asset-like). Positive stock closing → Debit Balance.
"""

from __future__ import annotations

from frappe.utils import flt

STOCK_VALUE_MEASURE_FIELDS = (
	"inward_value",
	"outward_value",
	"debit_balance",
	"credit_balance",
)

STOCK_QTY_MEASURE_FIELDS = (
	"in_qty",
	"out_qty",
	"balance_qty",
)

# Internal signed closing kept for parity / footer netting (not a grid column).
STOCK_SIGNED_VALUE_FIELD = "balance_value"

STOCK_ALL_MEASURE_FIELDS = STOCK_QTY_MEASURE_FIELDS + STOCK_VALUE_MEASURE_FIELDS


def zero_stock_value_measures() -> dict[str, float]:
	return {field: 0.0 for field in STOCK_VALUE_MEASURE_FIELDS}


def zero_stock_qty_measures() -> dict[str, float]:
	return {field: 0.0 for field in STOCK_QTY_MEASURE_FIELDS}


def apply_stock_side_balances(row: dict) -> dict:
	"""Map signed balance_value → debit_balance / credit_balance (never negative)."""
	signed = flt(row.get(STOCK_SIGNED_VALUE_FIELD))
	row[STOCK_SIGNED_VALUE_FIELD] = signed
	row["debit_balance"] = max(signed, 0.0)
	row["credit_balance"] = abs(min(signed, 0.0))
	return row


def finalize_stock_value_measures(row: dict) -> dict:
	"""Ensure balance_value = inward - outward, then side balances."""
	inward = flt(row.get("inward_value"))
	outward = flt(row.get("outward_value"))
	row["inward_value"] = inward
	row["outward_value"] = outward
	row[STOCK_SIGNED_VALUE_FIELD] = inward - outward
	row.pop("opening_value", None)
	row.pop("closing_value", None)
	return apply_stock_side_balances(row)


def finalize_stock_qty_measures(row: dict) -> dict:
	"""Ensure balance_qty = in - out. Opening must already be rolled into in_qty."""
	in_qty = flt(row.get("in_qty"))
	out_qty = flt(row.get("out_qty"))
	row["in_qty"] = in_qty
	row["out_qty"] = out_qty
	row["balance_qty"] = in_qty - out_qty
	row.pop("opening_qty", None)
	row.pop("closing_qty", None)
	return row


def finalize_stock_measures(row: dict, *, include_qty: bool = True) -> dict:
	finalize_stock_value_measures(row)
	if include_qty:
		finalize_stock_qty_measures(row)
	else:
		row.pop("opening_qty", None)
		row.pop("closing_qty", None)
		row.pop("in_qty", None)
		row.pop("out_qty", None)
		row.pop("balance_qty", None)
	return row


def stock_value_from_opening_period(
	opening_value: float, inward_value: float, outward_value: float
) -> dict:
	"""Roll opening into inward; outward is period-only."""
	row = {
		"inward_value": flt(opening_value) + flt(inward_value),
		"outward_value": flt(outward_value),
		STOCK_SIGNED_VALUE_FIELD: 0.0,
		"debit_balance": 0.0,
		"credit_balance": 0.0,
	}
	return finalize_stock_value_measures(row)


def stock_qty_from_opening_period(opening_qty: float, in_qty: float, out_qty: float) -> dict:
	"""Roll opening into in_qty; out_qty is period-only."""
	row = {
		"in_qty": flt(opening_qty) + flt(in_qty),
		"out_qty": flt(out_qty),
		"balance_qty": 0.0,
	}
	return finalize_stock_qty_measures(row)


def stock_row_from_buckets(
	*,
	opening_qty: float = 0.0,
	in_qty: float = 0.0,
	out_qty: float = 0.0,
	opening_value: float = 0.0,
	inward_value: float = 0.0,
	outward_value: float = 0.0,
	include_qty: bool = True,
) -> dict:
	"""Build display measures from internal opening + period buckets."""
	row = {
		**stock_value_from_opening_period(opening_value, inward_value, outward_value),
	}
	if include_qty:
		row.update(stock_qty_from_opening_period(opening_qty, in_qty, out_qty))
	return row


def row_has_stock_activity(row: dict) -> bool:
	fields = list(STOCK_VALUE_MEASURE_FIELDS) + [STOCK_SIGNED_VALUE_FIELD]
	if any(k in row for k in STOCK_QTY_MEASURE_FIELDS):
		fields = list(STOCK_QTY_MEASURE_FIELDS) + fields
	for field in fields:
		if flt(row.get(field)):
			return True
	return False


def add_stock_measures(target: dict, source: dict) -> None:
	"""Accumulate signed flows; side balances are recomputed after sum."""
	for field in ("inward_value", "outward_value", STOCK_SIGNED_VALUE_FIELD, *STOCK_QTY_MEASURE_FIELDS):
		if field in source or field in target:
			target[field] = flt(target.get(field)) + flt(source.get(field))


def sum_stock_measure_rows(rows: list[dict], *, include_qty: bool = True) -> dict:
	"""Footer contract: sum signed nets, then side-present (not Σ debit + Σ credit)."""
	total = {
		"inward_value": 0.0,
		"outward_value": 0.0,
		STOCK_SIGNED_VALUE_FIELD: 0.0,
	}
	if include_qty:
		total.update(zero_stock_qty_measures())
	for row in rows:
		add_stock_measures(total, row)
	# Prefer recomputing signed closing from summed flows when available.
	if "inward_value" in total:
		total[STOCK_SIGNED_VALUE_FIELD] = flt(total.get("inward_value")) - flt(total.get("outward_value"))
	return finalize_stock_measures(total, include_qty=include_qty)
