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


def _fx_company_rate(tx_rate, conversion_rate, company_currency: str):
	"""ROUND_HALF_UP(rate × conversion_rate) via Decimal — no binary float product."""
	from decimal import Decimal

	if tx_rate in (None, "") or conversion_rate in (None, ""):
		return None
	product = Decimal(str(tx_rate)) * Decimal(str(conversion_rate))
	return rounding.round_monetary_rate(product, company_currency)


def invoice_has_distributed_discount(doc) -> bool:
	"""True when additional/distributed discount is present (Pattern B — not Pattern A)."""
	if abs(flt(doc.get("discount_amount"))) > 0:
		return True
	if abs(flt(doc.get("additional_discount_percentage"))) > 0:
		return True
	return any(
		abs(flt(row.get("distributed_discount_amount"))) > 0 for row in doc.get("items") or []
	)


def is_pattern_a_fx_purchase_invoice(doc) -> bool:
	"""FX Purchase Invoice on an IRR company with no additional/distributed discount."""
	if getattr(doc, "doctype", None) != "Purchase Invoice":
		return False
	if not rounding.is_irr_company(doc.company):
		return False
	company_currency = rounding.get_company_currency(doc.company)
	transaction_currency = doc.currency or company_currency
	if not transaction_currency or transaction_currency == company_currency:
		return False
	if invoice_has_distributed_discount(doc):
		return False
	return True


def _pattern_a_amounts_equal(actual, expected, company_currency: str) -> bool:
	"""Compare company-currency amounts via ROUND_HALF_UP — no binary float equality."""
	return rounding.round_currency(actual, company_currency) == rounding.round_currency(
		expected, company_currency
	)


def _pattern_a_total_category_base_tax(doc, company_currency: str):
	"""Σ Total / Valuation-and-Total tax bases — same semantics as header re-aggregation."""
	base_tax = 0.0
	for tax in doc.get("taxes") or []:
		if tax.get("category") not in ("Total", "Valuation and Total"):
			continue
		amt = flt(
			tax.get("base_tax_amount_after_discount_amount")
			if tax.get("base_tax_amount_after_discount_amount") not in (None, "")
			else tax.get("base_tax_amount")
		)
		if tax.get("add_deduct_tax") == "Deduct":
			base_tax -= amt
		else:
			base_tax += amt
	return rounding.round_currency(base_tax, company_currency)


def _raise_pattern_a_invariant_failure(doc, invariant: str, expected, actual) -> None:
	import frappe
	from frappe import _

	frappe.throw(
		_(
			"RATE-FIRST accounting invariant failed for Purchase Invoice {0}: {1}. "
			"Expected {2}, got {3}. Refusing to apply FX precision-loss override."
		).format(doc.get("name") or _("(new)"), invariant, expected, actual),
		title=_("RATE-FIRST Invariant Failed"),
	)


def validate_pattern_a_rate_first_invariants(doc) -> None:
	"""Fail-closed: Pattern A docs must already satisfy RATE-FIRST header/item invariants.

	Called only when ``is_pattern_a_fx_purchase_invoice(doc)`` is True, immediately before
	skipping ERPNext product-first ``make_precision_loss_gl_entry``. Does not repair the
	document and does not inspect in-progress GL (taxes / invoice rounding GLE may follow).
	"""
	company_currency = rounding.get_company_currency(doc.company)
	conversion_rate = doc.get("conversion_rate")
	items = doc.get("items") or []

	sum_base_amount = 0.0
	sum_base_net_amount = 0.0
	for row in items:
		qty = flt(row.get("qty"))
		if row.get("rate") is not None:
			expected_base_rate = _fx_company_rate(row.rate, conversion_rate, company_currency)
			if expected_base_rate is None:
				_raise_pattern_a_invariant_failure(
					doc,
					"item base_rate could not be derived from rate × conversion_rate",
					"derived RATE-FIRST base_rate",
					row.get("base_rate"),
				)
			if not _pattern_a_amounts_equal(row.get("base_rate"), expected_base_rate, company_currency):
				_raise_pattern_a_invariant_failure(
					doc,
					f"item row {cint(row.get('idx') or 0)} base_rate != ROUND_HALF_UP(rate × conversion_rate)",
					expected_base_rate,
					row.get("base_rate"),
				)
			expected_base_amount = rounding.round_row_amount(
				qty, expected_base_rate, company_currency
			)
			if not _pattern_a_amounts_equal(row.get("base_amount"), expected_base_amount, company_currency):
				_raise_pattern_a_invariant_failure(
					doc,
					f"item row {cint(row.get('idx') or 0)} base_amount != ROUND_HALF_UP(qty × base_rate)",
					expected_base_amount,
					row.get("base_amount"),
				)

		if row.get("net_rate") is not None and not _row_has_distributed_discount(row):
			expected_base_net_rate = _fx_company_rate(row.net_rate, conversion_rate, company_currency)
			if expected_base_net_rate is None:
				_raise_pattern_a_invariant_failure(
					doc,
					"item base_net_rate could not be derived from net_rate × conversion_rate",
					"derived RATE-FIRST base_net_rate",
					row.get("base_net_rate"),
				)
			if not _pattern_a_amounts_equal(
				row.get("base_net_rate"), expected_base_net_rate, company_currency
			):
				_raise_pattern_a_invariant_failure(
					doc,
					f"item row {cint(row.get('idx') or 0)} base_net_rate != ROUND_HALF_UP(net_rate × conversion_rate)",
					expected_base_net_rate,
					row.get("base_net_rate"),
				)
			expected_base_net_amount = rounding.round_row_amount(
				qty, expected_base_net_rate, company_currency
			)
			if not _pattern_a_amounts_equal(
				row.get("base_net_amount"), expected_base_net_amount, company_currency
			):
				_raise_pattern_a_invariant_failure(
					doc,
					f"item row {cint(row.get('idx') or 0)} base_net_amount != ROUND_HALF_UP(qty × base_net_rate)",
					expected_base_net_amount,
					row.get("base_net_amount"),
				)

		sum_base_amount += flt(row.get("base_amount"))
		sum_base_net_amount += flt(row.get("base_net_amount"))

	expected_base_total = rounding.round_currency(sum_base_amount, company_currency)
	if not _pattern_a_amounts_equal(doc.get("base_total"), expected_base_total, company_currency):
		_raise_pattern_a_invariant_failure(
			doc,
			"base_total does not equal the sum of aligned item base_amount values",
			expected_base_total,
			doc.get("base_total"),
		)

	expected_base_net_total = rounding.round_currency(sum_base_net_amount, company_currency)
	if not _pattern_a_amounts_equal(doc.get("base_net_total"), expected_base_net_total, company_currency):
		_raise_pattern_a_invariant_failure(
			doc,
			"base_net_total does not equal the sum of aligned item base_net_amount values",
			expected_base_net_total,
			doc.get("base_net_total"),
		)

	expected_base_tax = _pattern_a_total_category_base_tax(doc, company_currency)
	if doc.get("base_total_taxes_and_charges") is not None and not _pattern_a_amounts_equal(
		doc.get("base_total_taxes_and_charges"), expected_base_tax, company_currency
	):
		_raise_pattern_a_invariant_failure(
			doc,
			"base_total_taxes_and_charges does not equal Total-category base tax sum",
			expected_base_tax,
			doc.get("base_total_taxes_and_charges"),
		)

	expected_base_grand_total = rounding.round_currency(
		flt(expected_base_net_total) + flt(expected_base_tax), company_currency
	)
	if not _pattern_a_amounts_equal(
		doc.get("base_grand_total"), expected_base_grand_total, company_currency
	):
		_raise_pattern_a_invariant_failure(
			doc,
			"base_grand_total does not equal base_net_total + applicable Total-category base taxes",
			expected_base_grand_total,
			doc.get("base_grand_total"),
		)

	if not flt(doc.get("rounding_adjustment")):
		if flt(doc.get("base_rounding_adjustment")):
			_raise_pattern_a_invariant_failure(
				doc,
				"base_rounding_adjustment must be 0 when transaction rounding_adjustment is 0",
				0,
				doc.get("base_rounding_adjustment"),
			)
		if flt(doc.get("rounded_total")):
			if not _pattern_a_amounts_equal(
				doc.get("base_rounded_total"), expected_base_grand_total, company_currency
			):
				_raise_pattern_a_invariant_failure(
					doc,
					"base_rounded_total must equal base_grand_total when transaction rounding_adjustment is 0",
					expected_base_grand_total,
					doc.get("base_rounded_total"),
				)
	else:
		from decimal import Decimal

		expected_base_rounding = rounding.round_currency(
			Decimal(str(doc.rounding_adjustment)) * Decimal(str(doc.conversion_rate or 0)),
			company_currency,
		)
		if not _pattern_a_amounts_equal(
			doc.get("base_rounding_adjustment"), expected_base_rounding, company_currency
		):
			_raise_pattern_a_invariant_failure(
				doc,
				"base_rounding_adjustment != ROUND_HALF_UP(rounding_adjustment × conversion_rate)",
				expected_base_rounding,
				doc.get("base_rounding_adjustment"),
			)
		expected_base_rounded_total = rounding.round_currency(
			flt(expected_base_grand_total) + flt(expected_base_rounding), company_currency
		)
		if not _pattern_a_amounts_equal(
			doc.get("base_rounded_total"), expected_base_rounded_total, company_currency
		):
			_raise_pattern_a_invariant_failure(
				doc,
				"base_rounded_total != base_grand_total + base_rounding_adjustment",
				expected_base_rounded_total,
				doc.get("base_rounded_total"),
			)


def _align_po_pi_si_row(
	row, company_currency: str, transaction_currency: str, conversion_rate=None
) -> None:
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

	if transaction_currency == company_currency:
		for base_field, tx_field in (
			("base_rate", "rate"),
			("base_amount", "amount"),
			("base_net_rate", "net_rate"),
			("base_net_amount", "net_amount"),
		):
			if row.get(tx_field) is not None and row.get(base_field) is not None:
				row.set(base_field, row.get(tx_field))
		return

	use_conv = conversion_rate not in (None, "") and flt(conversion_rate)
	if row.get("rate") is not None:
		if use_conv:
			row.base_rate = _fx_company_rate(row.rate, conversion_rate, company_currency)
		elif row.get("base_rate") is not None:
			row.base_rate = rounding.round_monetary_rate(row.base_rate, company_currency)
		if row.get("base_rate") is not None:
			row.base_amount = rounding.round_row_amount(qty, row.base_rate, company_currency)

	if row.get("net_rate") is not None:
		if use_conv:
			row.base_net_rate = _fx_company_rate(row.net_rate, conversion_rate, company_currency)
		elif row.get("base_net_rate") is not None:
			row.base_net_rate = rounding.round_monetary_rate(row.base_net_rate, company_currency)
		if _row_has_distributed_discount(row) and row.get("base_net_amount") is not None:
			row.base_net_amount = rounding.round_currency(row.get("base_net_amount"), company_currency)
		elif row.get("base_net_rate") is not None:
			row.base_net_amount = rounding.round_row_amount(qty, row.base_net_rate, company_currency)


def align_purchase_order_item_amounts(doc) -> None:
	if not rounding.is_irr_company(doc.company):
		return
	ccy = rounding.get_company_currency(doc.company)
	tx = doc.currency or ccy
	conversion_rate = doc.get("conversion_rate")
	for row in doc.get("items") or []:
		_align_po_pi_si_row(row, ccy, tx, conversion_rate=conversion_rate)


def reaggregate_fx_purchase_invoice_base_totals(doc) -> None:
	"""Derive IRR header totals from aligned RATE-FIRST item bases (Pattern A only).

	Must not run on Sales Invoice, same-currency IRR invoices, submitted docs, or
	Pattern B additional-discount invoices.
	"""
	if cint(doc.get("docstatus")) == 1 and getattr(doc, "_action", None) not in ("save", "submit"):
		return
	if not is_pattern_a_fx_purchase_invoice(doc):
		return

	company_currency = rounding.get_company_currency(doc.company)
	items = doc.get("items") or []
	base_total = 0.0
	base_net_total = 0.0
	for row in items:
		base_total += flt(row.get("base_amount"))
		base_net_total += flt(row.get("base_net_amount"))

	doc.base_total = rounding.round_currency(base_total, company_currency)
	doc.base_net_total = rounding.round_currency(base_net_total, company_currency)

	base_tax = 0.0
	for tax in doc.get("taxes") or []:
		if tax.get("category") not in ("Total", "Valuation and Total"):
			continue
		amt = flt(
			tax.get("base_tax_amount_after_discount_amount")
			if tax.get("base_tax_amount_after_discount_amount") not in (None, "")
			else tax.get("base_tax_amount")
		)
		if tax.get("add_deduct_tax") == "Deduct":
			base_tax -= amt
		else:
			base_tax += amt
	doc.base_total_taxes_and_charges = rounding.round_currency(base_tax, company_currency)
	doc.base_grand_total = rounding.round_currency(
		flt(doc.base_net_total) + flt(doc.base_total_taxes_and_charges), company_currency
	)

	if not flt(doc.get("rounding_adjustment")):
		doc.base_rounding_adjustment = 0
		if flt(doc.get("rounded_total")):
			doc.base_rounded_total = doc.base_grand_total
		elif doc.get("base_rounded_total") is not None:
			doc.base_rounded_total = 0
	else:
		from decimal import Decimal

		raw_adj = Decimal(str(doc.rounding_adjustment)) * Decimal(str(doc.conversion_rate or 0))
		doc.base_rounding_adjustment = rounding.round_currency(raw_adj, company_currency)
		doc.base_rounded_total = rounding.round_currency(
			flt(doc.base_grand_total) + flt(doc.base_rounding_adjustment), company_currency
		)

	authoritative_base = flt(doc.get("base_rounded_total") or doc.base_grand_total)
	_update_payment_schedule_base(doc, authoritative_base, company_currency)
	_update_outstanding_after_rate_first(doc, company_currency)
	if callable(getattr(doc, "set_total_in_words", None)):
		doc.set_total_in_words()


def _update_payment_schedule_base(doc, authoritative_base, company_currency: str) -> None:
	rows = doc.get("payment_schedule") or []
	if not rows:
		return
	for d in rows:
		portion = flt(d.get("invoice_portion"))
		if portion:
			base_payment = rounding.round_currency(
				flt(authoritative_base) * portion / 100.0, company_currency
			)
			d.base_payment_amount = base_payment
			if d.get("base_outstanding") is not None or hasattr(d, "base_outstanding"):
				d.base_outstanding = base_payment
		elif len(rows) == 1:
			d.base_payment_amount = rounding.round_currency(authoritative_base, company_currency)
			if d.get("base_outstanding") is not None or hasattr(d, "base_outstanding"):
				d.base_outstanding = d.base_payment_amount


def _update_outstanding_after_rate_first(doc, company_currency: str) -> None:
	if doc.get("is_return") and doc.get("return_against") and not doc.get("update_outstanding_for_self"):
		return
	party_ccy = doc.get("party_account_currency")
	if party_ccy == doc.currency:
		total_to_pay = (
			flt(doc.get("rounded_total") or doc.get("grand_total"))
			- flt(doc.get("total_advance"))
			- flt(doc.get("write_off_amount"))
		)
		paid = flt(doc.get("paid_amount"))
		doc.outstanding_amount = rounding.round_currency(total_to_pay - paid, doc.currency)
	else:
		total_to_pay = (
			flt(doc.get("base_rounded_total") or doc.get("base_grand_total"))
			- flt(doc.get("total_advance"))
			- flt(doc.get("base_write_off_amount"))
		)
		paid = flt(doc.get("base_paid_amount"))
		doc.outstanding_amount = rounding.round_currency(total_to_pay - paid, company_currency)


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
