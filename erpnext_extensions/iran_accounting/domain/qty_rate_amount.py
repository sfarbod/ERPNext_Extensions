# Copyright (c) 2026, ERPNext Extensions contributors
"""Row-level qty × rate monetary rounding (currency precision)."""

from __future__ import annotations

import logging

from frappe.utils import cint, flt

import erpnext_extensions.iran_accounting.domain.currency as rounding

logger = logging.getLogger(__name__)

PI_SI_ITEM_TX_FIELDS = ("rate", "amount", "net_rate", "net_amount")
PI_SI_ITEM_BASE_FIELDS = ("base_rate", "base_amount", "base_net_rate", "base_net_amount")
PO_ITEM_TX_FIELDS = ("rate", "amount", "net_rate", "net_amount")
PO_ITEM_BASE_FIELDS = ("base_rate", "base_amount", "base_net_rate", "base_net_amount")
PR_DN_ITEM_TX_FIELDS = ("rate", "amount")
PR_DN_ITEM_BASE_FIELDS = ("base_rate", "base_amount")
STE_ITEM_FIELDS = ("basic_rate", "basic_amount", "amount", "valuation_rate")

SR_RATE_FIELD = "valuation_rate"
SR_CURRENT_RATE_FIELD = "current_valuation_rate"


def _row_rate(row, rate_field: str = "rate"):
	rate = row.get(rate_field)
	if rate in (None, "") and rate_field != "rate":
		rate = row.get("rate")
	return rate


def compute_row_amount(row, currency: str, *, rate_field: str = "rate") -> float:
	"""Single source of truth: round(qty × rate, currency precision). Missing inputs → 0."""
	qty = flt(row.get("qty"))
	rate = _row_rate(row, rate_field)
	if not qty or rate in (None, ""):
		return 0.0
	return flt(rounding.round_row_amount(qty, rate, currency))


def normalize_stock_reconciliation_row(row, currency: str, *, purpose: str = "") -> None:
	"""Per-row financial amounts: rate-first ROUND_HALF_UP; difference = new − current."""
	_ = purpose
	qty = row.get("qty")
	rate = _row_rate(row, SR_RATE_FIELD)
	if qty not in (None, "") and rate not in (None, "") and row.get("amount") in (None, ""):
		logger.warning(
			"iran_accounting: Stock Reconciliation row %s had qty+rate but empty amount; forcing qty×rate",
			row.idx,
		)

	if rate not in (None, ""):
		row.valuation_rate = rounding.round_monetary_rate(rate, currency)
	current_rate = _row_rate(row, SR_CURRENT_RATE_FIELD)
	if current_rate not in (None, ""):
		row.current_valuation_rate = rounding.round_monetary_rate(current_rate, currency)

	row.amount = flt(rounding.round_row_amount(qty, row.get("valuation_rate"), currency))
	row.current_amount = flt(
		rounding.round_row_amount(
			row.current_qty,
			row.get("current_valuation_rate"),
			currency,
		)
	)
	row.quantity_difference = flt(row.qty) - flt(row.current_qty)
	row.amount_difference = flt(row.amount) - flt(row.current_amount)


def _sr_row_counts_toward_header_sum(row) -> bool:
	"""Never exclude a row from header aggregation when qty or rate is set."""
	return flt(row.qty) > 0 or flt(_row_rate(row, SR_RATE_FIELD) or 0) > 0


def sum_stock_reconciliation_row_amounts(doc, currency: str | None = None) -> float:
	"""Σ row.amount (gross per row); not used for header."""
	_ = currency
	total = 0.0
	for row in sorted(doc.get("items") or [], key=lambda r: cint(r.idx or 0)):
		if _sr_row_counts_toward_header_sum(row):
			total += flt(row.amount)
	return flt(total)


def sum_stock_reconciliation_amount_difference(doc) -> float:
	"""Σ row.amount_difference — Stock Reconciliation header and GL/SLE net total."""
	total = 0.0
	for row in sorted(doc.get("items") or [], key=lambda r: cint(r.idx or 0)):
		if _sr_row_counts_toward_header_sum(row):
			total += flt(row.amount_difference)
	return flt(total)


def sum_stock_reconciliation_header_total(doc) -> float:
	"""Header = Σ row.amount_difference (net), aligned with GL / SLE."""
	return sum_stock_reconciliation_amount_difference(doc)


def override_difference_amount(doc) -> None:
	"""Header = Σ amount_difference after row normalization (matches GL / Stock Ledger)."""
	if not doc.get("company"):
		return
	currency = rounding.get_company_currency(doc.company)
	erpnext_header = flt(doc.difference_amount)
	for row in sorted(doc.get("items") or [], key=lambda r: cint(r.idx or 0)):
		normalize_stock_reconciliation_row(row, currency)
	final = sum_stock_reconciliation_amount_difference(doc)
	doc.difference_amount = final
	_log_sr_header_debug(doc, final, erpnext_header)
	if final != erpnext_header:
		logger.info(
			"iran_accounting SR override_difference_amount NET_SUM=%s ERP_NEXT_HEADER=%s DELTA=%s voucher=%s",
			final,
			erpnext_header,
			final - erpnext_header,
			doc.name or "new",
		)


def _log_sr_header_debug(doc, sum_amount_difference: float, erpnext_header: float) -> None:
	gl_total = sle_total = None
	if doc.get("name") and doc.get("docstatus") == 1:
		try:
			import frappe

			from erpnext_extensions.iran_accounting.qty_rate_consistency import (
				_gl_stock_totals,
				_sle_value_diff_sum,
			)

			if frappe.db.exists("Stock Reconciliation", doc.name):
				gl = _gl_stock_totals("Stock Reconciliation", doc.name)
				gl_total = max(flt(gl.get("debit")), flt(gl.get("credit")))
				sle_total = flt(_sle_value_diff_sum("Stock Reconciliation", doc.name))
		except Exception:
			pass
	logger.info(
		"SR_HEADER_DEBUG voucher=%s sum_amount_difference=%s final_header=%s erpnext_header=%s gl_total=%s sle_total=%s",
		doc.name or "new",
		sum_amount_difference,
		flt(doc.difference_amount),
		erpnext_header,
		gl_total,
		sle_total,
	)


def compute_final_difference_amount(doc) -> None:
	override_difference_amount(doc)


def align_stock_reconciliation_row_amounts(doc) -> None:
	compute_final_difference_amount(doc)


def enforce_row_amounts(doc) -> None:
	"""Normalize row amounts before save/submit (IRR and FX use company currency rules)."""
	if not doc.get("company"):
		return
	doctype = doc.doctype
	if doctype == "Stock Reconciliation":
		compute_final_difference_amount(doc)
	elif doctype == "Stock Entry":
		align_stock_entry_item_amounts(doc)
	elif doctype == "Purchase Order":
		align_purchase_order_item_amounts(doc)
	elif doctype == "Purchase Invoice":
		align_purchase_invoice_item_amounts(doc)
	elif doctype == "Sales Invoice":
		align_sales_invoice_item_amounts(doc)
	elif doctype == "Purchase Receipt":
		align_purchase_receipt_item_amounts(doc)
	elif doctype == "Delivery Note":
		align_delivery_note_item_amounts(doc)


def compose_stock_entry_row_amount(row, currency: str) -> float:
	"""ERPNext capitalization model: basic_amount + additional_cost + landed_cost_voucher_amount."""
	return flt(
		rounding.round_currency(
			flt(row.get("basic_amount"))
			+ flt(row.get("additional_cost"))
			+ flt(row.get("landed_cost_voucher_amount")),
			currency,
		)
	)


def align_stock_entry_item_amounts(doc) -> None:
	"""IRR rate-first Stock Entry row alignment.

	Contract (IRR):
	1. basic_rate = ROUND_HALF_UP(raw_rate, 0)  — persist integer rate
	2. basic_amount = ROUND_HALF_UP(transfer_qty × integer basic_rate, 0)
	3. amount = ROUND_HALF_UP(basic_amount + additional_cost + LCV, 0)
	4. valuation_rate = ROUND_HALF_UP(amount / transfer_qty, 0)
	5. amount remains authoritative: residual = amount − valuation_rate × qty
	   (never force amount := valuation_rate × qty)

	additional_cost and landed_cost_voucher_amount are preserved (rounded to IRR).
	"""
	if not rounding.is_irr_company(doc.company):
		return
	currency = rounding.get_company_currency(doc.company)
	for row in doc.get("items") or []:
		transfer_qty = flt(
			row.get("transfer_qty") if row.get("transfer_qty") not in (None, "") else row.get("qty")
		)

		if row.get("additional_cost") not in (None, ""):
			row.additional_cost = rounding.round_currency(row.additional_cost, currency)
		if row.get("landed_cost_voucher_amount") not in (None, ""):
			row.landed_cost_voucher_amount = rounding.round_currency(
				row.landed_cost_voucher_amount, currency
			)

		if row.get("basic_rate") is not None:
			row.basic_rate = rounding.round_monetary_rate(row.basic_rate, currency)
			if transfer_qty:
				row.basic_amount = rounding.round_row_amount(transfer_qty, row.basic_rate, currency)
			else:
				row.basic_amount = rounding.round_currency(flt(row.get("basic_amount")), currency)
		elif row.get("basic_amount") is not None:
			row.basic_amount = rounding.round_currency(flt(row.get("basic_amount")), currency)

		# Never discard capitalized costs; compose amount from integer components.
		row.amount = compose_stock_entry_row_amount(row, currency)
		if transfer_qty and flt(row.amount):
			row.valuation_rate = rounding.integer_valuation_rate_from_amount(
				row.amount, transfer_qty, currency
			)
			# Residual is intentional when valuation_rate × qty ≠ amount; amount wins.
			_ = rounding.amount_rate_qty_residual(
				row.amount, transfer_qty, row.valuation_rate, currency
			)
		elif row.get("valuation_rate") is not None:
			row.valuation_rate = rounding.round_monetary_rate(row.valuation_rate, currency)


def _row_has_distributed_discount(row) -> bool:
	"""ERPNext additional-discount allocation leaves net_amount ≠ qty × net_rate."""
	return abs(flt(row.get("distributed_discount_amount"))) > 0


def _align_po_pi_si_row(row, company_currency: str, transaction_currency: str) -> None:
	qty = flt(row.qty)
	if row.get("rate") is not None:
		row.rate = rounding.round_monetary_rate(row.rate, transaction_currency)
		row.amount = rounding.round_row_amount(qty, row.rate, transaction_currency)
	if row.get("net_rate") is not None:
		row.net_rate = rounding.round_monetary_rate(row.net_rate, transaction_currency)
		# Document-level discount is applied onto net_amount (distributed_discount_amount).
		# Rebuilding net_amount from qty×net_rate would wipe that allocation and unbalance GL
		# (seen on Purchase Invoice ACC-PINV-2026-00327: Δ=105).
		if _row_has_distributed_discount(row) and row.get("net_amount") is not None:
			row.net_amount = rounding.round_currency(row.net_amount, transaction_currency)
		else:
			row.net_amount = rounding.round_row_amount(qty, row.net_rate, transaction_currency)
	for base_field, tx_field in (
		("base_rate", "rate"),
		("base_amount", "amount"),
		("base_net_rate", "net_rate"),
		("base_net_amount", "net_amount"),
	):
		if row.get(tx_field) is not None and row.get(base_field) is not None:
			if transaction_currency == company_currency:
				row.set(base_field, row.get(tx_field))
			else:
				br = row.get("base_rate") if base_field.endswith("rate") else None
				if base_field.endswith("amount"):
					if "net" in base_field and _row_has_distributed_discount(row):
						row.set(
							base_field,
							rounding.round_currency(row.get(base_field), company_currency),
						)
					else:
						src_rate = row.get("base_net_rate" if "net" in base_field else "base_rate")
						row.set(base_field, rounding.round_row_amount(qty, src_rate, company_currency))
				elif br is not None:
					row.set(base_field, rounding.round_monetary_rate(br, company_currency))


def align_purchase_order_item_amounts(doc) -> None:
	if not rounding.is_irr_company(doc.company):
		return
	ccy = rounding.get_company_currency(doc.company)
	tx = doc.currency or ccy
	for row in doc.get("items") or []:
		_align_po_pi_si_row(row, ccy, tx)


def align_purchase_invoice_item_amounts(doc) -> None:
	align_purchase_order_item_amounts(doc)


def align_sales_invoice_item_amounts(doc) -> None:
	align_purchase_order_item_amounts(doc)


def align_purchase_receipt_item_amounts(doc) -> None:
	"""Rate-first PR/DN alignment (IRR integer rates and amounts)."""
	if not rounding.is_irr_company(doc.company):
		return
	ccy = rounding.get_company_currency(doc.company)
	tx = doc.currency or ccy
	for row in doc.get("items") or []:
		qty = flt(row.qty)
		if row.get("rate") is not None:
			row.rate = rounding.round_monetary_rate(row.rate, tx)
			row.amount = rounding.round_row_amount(qty, row.rate, tx)
		if row.get("base_rate") is not None:
			row.base_rate = rounding.round_monetary_rate(row.base_rate, ccy)
			row.base_amount = rounding.round_row_amount(qty, row.base_rate, ccy)
		if row.get("valuation_rate") is not None:
			row.valuation_rate = rounding.round_monetary_rate(row.valuation_rate, ccy)


def align_delivery_note_item_amounts(doc) -> None:
	align_purchase_receipt_item_amounts(doc)


def row_qty_rate_check(
	qty,
	rate,
	stored_amount,
	currency: str,
	*,
	label: str = "amount",
) -> dict:
	raw = flt(qty) * flt(rate)
	expected = rounding.round_row_amount(qty, rate, currency)
	stored = flt(stored_amount) if stored_amount not in (None, "") else None
	residual = None if stored is None else flt(stored) - expected
	ok = True
	if stored is not None:
		ok = not rounding.amount_is_fractional(stored, currency) and flt(stored) == flt(expected)
	return {
		"qty": qty,
		"rate": rate,
		"currency": currency,
		"field": label,
		"raw_amount": raw,
		"expected_rounded_amount": expected,
		"stored_amount": stored,
		"residual": residual,
		"status": "PASS" if ok else "FAIL",
	}
