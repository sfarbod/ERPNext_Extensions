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
from frappe import _
from frappe.utils import cstr, flt

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


# Stock vouchers whose GL residual comes from SLE→GL / valuation precision.
# These must use Company.stock_adjustment_account — never Company.round_off_account.
STOCK_VALUATION_VOUCHER_TYPES = frozenset(
	{
		"Stock Entry",
		"Purchase Receipt",
		"Delivery Note",
		"Stock Reconciliation",
	}
)

IRR_STOCK_VALUATION_RESIDUAL_REMARK = "IRR stock-valuation currency-precision residual"


def currency_precision_quantum(precision: int) -> float:
	"""One company-currency unit at the given decimal precision (precision 0 → 1)."""
	return 1.0 / (10**int(precision)) if int(precision) else 1.0


def is_stock_valuation_voucher_type(voucher_type: str | None) -> bool:
	return cstr(voucher_type) in STOCK_VALUATION_VOUCHER_TYPES


def absorb_stock_valuation_precision_residual(
	gl_map: list,
	debit_credit_diff: float,
	trx_cur_debit_credit_diff: float,
	precision: int,
	*,
	company: str | None = None,
) -> bool:
	"""Post a proven stock-valuation precision residual to Stock Adjustment.

	Returns True when absorbed. Does not call ``make_round_off_gle`` / Round Off.
	Only absorbs when ``|diff|`` is exactly one currency quantum (or less after
	flt to precision). Larger gaps return False so the caller can throw.
	"""
	if not gl_map:
		return False
	quantum = currency_precision_quantum(precision)
	diff = flt(debit_credit_diff, precision)
	if not diff or abs(diff) > quantum + 1e-9:
		return False

	company = company or (
		gl_map[0].get("company") if hasattr(gl_map[0], "get") else getattr(gl_map[0], "company", None)
	)
	if not company:
		return False
	adj_account = frappe.get_cached_value("Company", company, "stock_adjustment_account")
	if not adj_account:
		frappe.throw(
			_("Company {0} has no Stock Adjustment Account configured.").format(company)
		)

	currency = get_company_currency(company)
	template = gl_map[0]

	def _g(obj, key, default=None):
		if hasattr(obj, "get"):
			return obj.get(key, default)
		return getattr(obj, key, default)

	def _s(obj, key, val):
		if hasattr(obj, "set") and callable(getattr(type(obj), "set", None)) and not isinstance(obj, dict):
			try:
				obj.set(key, val)
				return
			except Exception:
				pass
		if hasattr(obj, "__setitem__"):
			obj[key] = val
		else:
			setattr(obj, key, val)

	target = next((e for e in gl_map if _g(e, "account") == adj_account), None)
	if target is None:
		cost_center = _g(template, "cost_center") or frappe.get_cached_value(
			"Company", company, "cost_center"
		)
		target = frappe._dict(
			{
				"account": adj_account,
				"cost_center": cost_center,
				"company": company,
				"posting_date": _g(template, "posting_date"),
				"voucher_type": _g(template, "voucher_type"),
				"voucher_no": _g(template, "voucher_no"),
				"remarks": IRR_STOCK_VALUATION_RESIDUAL_REMARK,
				"is_opening": _g(template, "is_opening") or "No",
				"debit": 0.0,
				"credit": 0.0,
				"debit_in_account_currency": 0.0,
				"credit_in_account_currency": 0.0,
				"debit_in_transaction_currency": 0.0,
				"credit_in_transaction_currency": 0.0,
				"project": _g(template, "project"),
			}
		)
		for key in (
			"department",
			"bank_dimension",
			"bank_account_dimension",
			"facility",
			"foreign_purchase_order",
		):
			if _g(template, key):
				target[key] = _g(template, key)
		gl_map.append(target)

	# debit_credit_diff > 0 ⇒ excess debit ⇒ add credit on Stock Adjustment.
	trx = flt(trx_cur_debit_credit_diff, precision)
	if diff > 0:
		_s(target, "credit", float(round_currency(flt(_g(target, "credit")) + diff, currency)))
		_s(
			target,
			"credit_in_account_currency",
			float(round_currency(flt(_g(target, "credit_in_account_currency")) + diff, currency)),
		)
		_s(
			target,
			"credit_in_transaction_currency",
			float(round_currency(flt(_g(target, "credit_in_transaction_currency")) + trx, currency)),
		)
	else:
		add = abs(diff)
		trx_add = abs(trx)
		_s(target, "debit", float(round_currency(flt(_g(target, "debit")) + add, currency)))
		_s(
			target,
			"debit_in_account_currency",
			float(round_currency(flt(_g(target, "debit_in_account_currency")) + add, currency)),
		)
		_s(
			target,
			"debit_in_transaction_currency",
			float(round_currency(flt(_g(target, "debit_in_transaction_currency")) + trx_add, currency)),
		)

	_quantize_entry(target, currency)
	# Drop zero legs after absorption.
	gl_map[:] = [
		e
		for e in gl_map
		if flt(_g(e, "debit"), precision) or flt(_g(e, "credit"), precision)
	]
	return True


def align_irr_gl_map_to_currency_precision(doc, gl_map: list | None) -> list | None:
	"""Quantize IRR GL legs and absorb a single-quantum residual into Stock Adjustment.

	Does not widen vanilla tolerance. Does not invent balances larger than one IRR
	quantum — larger gaps still fail closed in ERPNext validation.
	Never uses Company.round_off_account for stock-valuation residuals.
	"""
	if not gl_map or not doc or not getattr(doc, "company", None):
		return gl_map
	if not is_irr_company(doc.company):
		return gl_map

	currency = get_company_currency(doc.company)
	precision = get_currency_precision(currency)
	quantum = currency_precision_quantum(precision)

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

	absorb_stock_valuation_precision_residual(
		gl_map, net, net, precision, company=doc.company
	)
	return gl_map
