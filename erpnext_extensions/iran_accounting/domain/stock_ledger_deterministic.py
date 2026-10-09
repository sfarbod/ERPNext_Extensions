# Copyright (c) 2026, ERPNext Extensions contributors
"""IRR balance qty / stock value / avg rate — valuation_rate rounding only."""

from __future__ import annotations

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.domain.currency import (
	get_company_currency,
	is_irr_company,
	round_currency,
	round_row_amount_financial,
)
from erpnext_extensions.iran_accounting.domain.ledger_rounding import (
	_get_entry_value,
	_set_entry_value,
	_zero_positive_opening_stock_reconciliation_outgoing_rate,
)
from erpnext_extensions.iran_accounting.domain.stock_reconciliation_sync import (
	_prior_warehouse_sle_before,
)


def carry_stock_value_into_moving_average(engine, sle) -> None:
	"""Make vanilla moving average start from the carried stock value.

	ERPNext adds ``qty × valuation_rate``. An integer rate, or a row rate
	stamped onto ``valuation_rate``, hides a remainder. The next receipt or
	manufacture then drops, inflates, or explodes the balance. Setting the
	in-memory rate to ``stock_value / qty`` makes that product equal the
	carried value. The persisted rate is still rounded afterward.
	"""
	if engine is None or sle is None:
		return
	company = getattr(engine, "company", None) or _get_entry_value(sle, "company")
	if not company or not is_irr_company(company):
		return
	wh = getattr(engine, "wh_data", None)
	if wh is None:
		return
	qty = flt(getattr(wh, "qty_after_transaction", 0))
	value = flt(getattr(wh, "stock_value", 0))
	if qty > 0 and value > 0:
		wh.valuation_rate = value / qty


def irr_avg_rate_from_balance(cumulative_value: float, cumulative_qty: float, currency: str) -> float:
	"""Avg rate (balance stock) = round(balance value / qty after transaction, IRR)."""
	if not flt(cumulative_qty) or not flt(cumulative_value):
		return 0.0
	return float(round_currency(flt(cumulative_value) / flt(cumulative_qty), currency))


def resolve_irr_balance_avg_rate(sle_doc_or_dict, company: str) -> float:
	"""Avg rate = round(stock_value / qty_after_transaction, IRR)."""
	currency = get_company_currency(company)
	qty_after = flt(_get_entry_value(sle_doc_or_dict, "qty_after_transaction"))
	if not qty_after:
		return 0.0

	stock_value = flt(_get_entry_value(sle_doc_or_dict, "stock_value"))
	if stock_value > 0:
		return irr_avg_rate_from_balance(stock_value, qty_after, currency)

	detail_no = _get_entry_value(sle_doc_or_dict, "voucher_detail_no")
	if _get_entry_value(sle_doc_or_dict, "voucher_type") == "Stock Reconciliation" and detail_no:
		row = frappe.db.get_value(
			"Stock Reconciliation Item",
			detail_no,
			["valuation_rate"],
			as_dict=True,
		)
		if row and row.valuation_rate not in (None, ""):
			return float(round_currency(row.valuation_rate, currency))
	return 0.0


_ACQUISITION_VOUCHER_TYPES = ("Purchase Receipt", "Purchase Invoice")


def authoritative_acquisition_incoming_rate(sle_doc_or_dict, currency: str | None = None) -> float | None:
	"""Document valuation rate for a positive acquisition, else None.

	None means this SLE is not an authoritative acquisition. Balance-derived
	incoming stays in force for transfers, manufacture, repack, returns,
	internal transfers, and zero-rate pending receipts.

	Only the receipt/invoice row ``valuation_rate`` is authoritative. An
	unlinked Purchase Invoice is not consulted. When Buying Settings value
	receipts from the invoice, ERPNext has already written that difference
	into the linked row's valuation rate.
	"""
	voucher_type = _get_entry_value(sle_doc_or_dict, "voucher_type")
	if voucher_type not in _ACQUISITION_VOUCHER_TYPES:
		return None
	actual_qty = flt(_get_entry_value(sle_doc_or_dict, "actual_qty"))
	if actual_qty <= 0:
		return None
	voucher_no = _get_entry_value(sle_doc_or_dict, "voucher_no")
	detail_no = _get_entry_value(sle_doc_or_dict, "voucher_detail_no")
	if not voucher_no or not detail_no:
		return None

	parent = frappe.db.get_value(
		voucher_type,
		voucher_no,
		["is_return", "is_internal_supplier"],
		as_dict=True,
	)
	if not parent or cint(parent.get("is_return")) or cint(parent.get("is_internal_supplier")):
		return None

	row = frappe.db.get_value(
		f"{voucher_type} Item",
		detail_no,
		["item_code", "valuation_rate"],
		as_dict=True,
	)
	item_code = _get_entry_value(sle_doc_or_dict, "item_code")
	row_item = row.get("item_code") if row else None
	if not row or (item_code and row_item != item_code):
		return None
	rate = flt(row.get("valuation_rate"))
	# Zero is the pending-invoice / zero-source contract, not an acquisition rate.
	if rate <= 0:
		return None
	if currency:
		rate = float(round_currency(rate, currency))
	return rate


def apply_authoritative_acquisition_incoming_rate(sle_doc_or_dict, company: str | None = None) -> bool:
	"""Set incoming_rate from the acquisition document. False when not applicable."""
	company = company or _get_entry_value(sle_doc_or_dict, "company")
	if not company or not is_irr_company(company):
		return False
	currency = get_company_currency(company)
	rate = authoritative_acquisition_incoming_rate(sle_doc_or_dict, currency)
	if not rate:
		return False
	_set_entry_value(sle_doc_or_dict, "incoming_rate", rate)
	return True


def align_authoritative_acquisition_movement(sle_doc_or_dict, company: str | None = None, engine=None) -> bool:
	"""Make the incoming movement exactly quantity times the document rate.

	Moving average then owns the warehouse average. It must not replace the
	acquisition movement with the previous average or with a rounding drift.
	"""
	company = company or _get_entry_value(sle_doc_or_dict, "company")
	if not company or not is_irr_company(company):
		return False
	currency = get_company_currency(company)
	rate = authoritative_acquisition_incoming_rate(sle_doc_or_dict, currency)
	actual_qty = flt(_get_entry_value(sle_doc_or_dict, "actual_qty"))
	if not rate or actual_qty <= 0:
		return False
	movement = round_currency(rate * actual_qty, currency)
	stock_value = flt(_get_entry_value(sle_doc_or_dict, "stock_value"))
	current_movement = flt(_get_entry_value(sle_doc_or_dict, "stock_value_difference"))
	previous_value = round_currency(stock_value - current_movement, currency)
	new_value = round_currency(previous_value + movement, currency)
	qty_after = flt(_get_entry_value(sle_doc_or_dict, "qty_after_transaction"))
	_set_entry_value(sle_doc_or_dict, "incoming_rate", rate)
	_set_entry_value(sle_doc_or_dict, "stock_value_difference", movement)
	_set_entry_value(sle_doc_or_dict, "stock_value", new_value)
	if qty_after:
		_set_entry_value(
			sle_doc_or_dict,
			"valuation_rate",
			irr_avg_rate_from_balance(new_value, qty_after, currency),
		)
	wh = getattr(engine, "wh_data", None) if engine is not None else None
	if wh is not None:
		wh.stock_value = new_value
		wh.prev_stock_value = new_value
		if qty_after:
			wh.valuation_rate = irr_avg_rate_from_balance(new_value, qty_after, currency)
	return True


def apply_irr_deterministic_sle_valuation(sle_doc_or_dict, company: str | None = None) -> None:
	"""Set valuation_rate from balance stock_value / qty_after_transaction (IRR round only).

	Does not modify stock_value, stock_value_difference, or row amounts — those come from sync/ERPNext.
	"""
	company = company or _get_entry_value(sle_doc_or_dict, "company")
	if not company or not is_irr_company(company):
		return

	currency = get_company_currency(company)
	qty_after = flt(_get_entry_value(sle_doc_or_dict, "qty_after_transaction"))
	stock_value = flt(_get_entry_value(sle_doc_or_dict, "stock_value"))
	movement = flt(_get_entry_value(sle_doc_or_dict, "stock_value_difference"))
	actual_qty = flt(_get_entry_value(sle_doc_or_dict, "actual_qty"))

	prev_qty = qty_after - actual_qty
	prev_value = stock_value - movement
	if _get_entry_value(sle_doc_or_dict, "voucher_type") == "Stock Reconciliation":
		prev_qty, prev_value = _prior_warehouse_sle_before(sle_doc_or_dict)

	if not qty_after:
		_set_entry_value(sle_doc_or_dict, "valuation_rate", 0.0)
	else:
		_set_entry_value(
			sle_doc_or_dict,
			"valuation_rate",
			resolve_irr_balance_avg_rate(sle_doc_or_dict, company),
		)

	_set_irr_incoming_rate_from_balance_before(sle_doc_or_dict, company, prev_qty, prev_value, currency)
	_zero_positive_opening_stock_reconciliation_outgoing_rate(sle_doc_or_dict)


def _set_irr_incoming_rate_from_balance_before(
	sle_doc_or_dict,
	company: str,
	prev_qty: float,
	prev_value: float,
	currency: str,
) -> None:
	actual_qty = flt(_get_entry_value(sle_doc_or_dict, "actual_qty"))
	movement = flt(_get_entry_value(sle_doc_or_dict, "stock_value_difference"))
	if actual_qty <= 0 and movement <= 0:
		return

	# The previous warehouse average is not the acquisition rate.
	if apply_authoritative_acquisition_incoming_rate(sle_doc_or_dict, company):
		return

	voucher_type = _get_entry_value(sle_doc_or_dict, "voucher_type")
	detail_no = _get_entry_value(sle_doc_or_dict, "voucher_detail_no")

	if prev_qty > 0 and prev_value > 0:
		incoming = irr_avg_rate_from_balance(prev_value, prev_qty, currency)
	elif voucher_type == "Stock Reconciliation" and detail_no:
		row = frappe.db.get_value(
			"Stock Reconciliation Item",
			detail_no,
			["qty", "valuation_rate", "amount"],
			as_dict=True,
		)
		if row and row.valuation_rate not in (None, ""):
			incoming = float(round_currency(row.valuation_rate, currency))
		elif row and flt(row.qty) and row.amount not in (None, ""):
			incoming = float(
				round_row_amount_financial(flt(row.qty), flt(row.amount) / flt(row.qty), currency)
			)
		else:
			incoming = 0.0
	else:
		incoming = 0.0

	if actual_qty > 0 and incoming:
		_set_entry_value(sle_doc_or_dict, "incoming_rate", incoming)
	elif movement > 0 and flt(_get_entry_value(sle_doc_or_dict, "qty_after_transaction")) > 0 and incoming:
		_set_entry_value(sle_doc_or_dict, "incoming_rate", incoming)
