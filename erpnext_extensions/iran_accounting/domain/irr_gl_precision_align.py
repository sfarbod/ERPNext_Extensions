# Copyright (c) 2026, ERPNext Extensions contributors
"""Align IRR GL maps to currency precision before vanilla debit/credit validation.

Vanilla ``process_debit_credit_difference`` uses Currency precision (0 for IRR) but a
hardcoded Stock Entry allowance of ``0.5``. When a GL map still carries fractional
stock-adjustment residual (e.g. ``0.48`` from unrounded SLE SVD sums) while inventory
legs round independently to integers, the residual rounds to ``0`` and the voucher
fails with ``Difference is 1.0`` — never reaching ``make_round_off_gle``.

Iran Accounting builds SLE/GL with IRR precision 0; this helper is defense-in-depth
for RIV/repost paths that may pass a pre-built fractional map into ``make_gl_entries``.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.currency import (
	get_company_currency,
	get_currency_precision,
	is_irr_company,
	round_currency,
)

_MONEY_FIELDS = (
	"debit",
	"credit",
	"debit_in_account_currency",
	"credit_in_account_currency",
	"debit_in_transaction_currency",
	"credit_in_transaction_currency",
)


def _quantize_entry(entry: Any, currency: str) -> None:
	for field in _MONEY_FIELDS:
		if entry.get(field) in (None, ""):
			continue
		entry[field] = float(round_currency(entry.get(field) or 0, currency))


def _net_debit(gl_map: list) -> float:
	return flt(sum(flt(e.get("debit")) - flt(e.get("credit")) for e in gl_map))


def align_irr_gl_map_to_currency_precision(doc, gl_map: list | None) -> list | None:
	"""Quantize IRR GL legs and absorb a single-quantum residual into Stock Adjustment.

	Does not widen vanilla tolerance. Does not invent balances larger than one IRR
	quantum — larger gaps still fail closed in ERPNext validation.
	"""
	if not gl_map or not doc or not getattr(doc, "company", None):
		return gl_map
	if not is_irr_company(doc.company):
		return gl_map

	currency = get_company_currency(doc.company)
	precision = get_currency_precision(currency)
	quantum = 1.0 / (10**precision) if precision else 1.0

	for entry in gl_map:
		_quantize_entry(entry, currency)

	# Drop legs that became all-zero after quantization (except keep structure for merge).
	gl_map[:] = [
		e
		for e in gl_map
		if flt(e.get("debit"), precision) or flt(e.get("credit"), precision)
	]

	net = flt(_net_debit(gl_map), precision)
	if not net:
		return gl_map

	if abs(net) > quantum + 1e-9:
		# Not a single-quantum IRR rounding artifact — leave for vanilla validation.
		return gl_map

	adj_account = frappe.get_cached_value("Company", doc.company, "stock_adjustment_account")
	if not adj_account:
		return gl_map

	# Prefer adjusting an existing Stock Adjustment leg; else append one.
	target = next((e for e in gl_map if e.get("account") == adj_account), None)
	template = gl_map[0]
	if target is None:
		target = frappe._dict(
			{
				"account": adj_account,
				"cost_center": template.get("cost_center")
				or frappe.get_cached_value("Company", doc.company, "cost_center"),
				"company": doc.company,
				"posting_date": template.get("posting_date") or doc.get("posting_date"),
				"voucher_type": doc.doctype,
				"voucher_no": doc.name,
				"remarks": template.get("remarks") or "IRR currency-precision alignment",
				"is_opening": template.get("is_opening") or "No",
				"debit": 0.0,
				"credit": 0.0,
				"debit_in_account_currency": 0.0,
				"credit_in_account_currency": 0.0,
				"debit_in_transaction_currency": 0.0,
				"credit_in_transaction_currency": 0.0,
				"project": template.get("project"),
			}
		)
		# Copy common dimensions from template when present.
		for key in (
			"department",
			"bank_dimension",
			"bank_account_dimension",
			"facility",
			"foreign_purchase_order",
		):
			if template.get(key):
				target[key] = template.get(key)
		gl_map.append(target)

	# net > 0 means excess debit → need more credit (or less debit) on adjustment.
	if net > 0:
		target["credit"] = float(round_currency(flt(target.get("credit")) + net, currency))
		target["credit_in_account_currency"] = float(
			round_currency(flt(target.get("credit_in_account_currency")) + net, currency)
		)
		target["credit_in_transaction_currency"] = float(
			round_currency(flt(target.get("credit_in_transaction_currency")) + net, currency)
		)
	else:
		add = abs(net)
		target["debit"] = float(round_currency(flt(target.get("debit")) + add, currency))
		target["debit_in_account_currency"] = float(
			round_currency(flt(target.get("debit_in_account_currency")) + add, currency)
		)
		target["debit_in_transaction_currency"] = float(
			round_currency(flt(target.get("debit_in_transaction_currency")) + add, currency)
		)

	_quantize_entry(target, currency)
	return gl_map
