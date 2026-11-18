# Copyright (c) 2026, ERPNext Extensions contributors
"""Balance Subcontracting Receipt clearing GL under Iran integer SLE.

Root cause (Core × Iran)
------------------------
Core ``SubcontractingReceipt.make_item_gl_entries``:

1. Debits FG inventory from SLE ``stock_value_difference`` (SVD).
2. Credits ``expense_account`` with ``SVD − service_cost`` (embeds +Δ when
   SVD = amount + Δ).
3. Credits service expense with ``service_cost``.
4. Posts ``divisional_loss = amount − SVD`` (= −Δ) to Stock Adjustment **and**
   mirrors that signed amount on ``expense_account`` (another +Δ credit after
   sign toggle).

Shared clearing (e.g. 111421) therefore shows a **2Δ** net credit. Overall the
voucher still balances because that 2Δ clearing credit funds both INV's +Δ and
SA's +Δ debit. Forcing clearing to net zero while leaving Core's SA debit would
break voucher balance by 2Δ.

Correct Iran treatment when SVD = amount + Δ
-------------------------------------------
Inventory must stay at SLE SVD. Clearing must net to zero (RM debit =
document material credit). The single Δ residual is a **valuation gain**:

* Credit Stock Adjustment Δ (not debit).
* Align clearing debit/credit to document amounts, then **drop** the
  net-zero clearing row(s) via the established
  ``drop_net_zero_merged_gl_rows`` helper (same convention as Manufacture /
  Repack — economically a no-op must not persist).

When SVD < amount the residual is a valuation loss (debit SA |Δ|).

``SubcontractingReceipt.get_gl_entries`` runs ``process_gl_map`` before return,
so Iran's ``StockController.make_gl_entries`` often sees **merged** expense
rows. This module handles merged and unmerged maps. Idempotent under repost.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.currency import is_irr_company
from erpnext_extensions.iran_accounting.domain.manufacture_expense_account import (
	drop_net_zero_merged_gl_rows,
)

SCR_GL_BALANCE_MARKER = "_irr_scr_clearing_balance"
TOL = 0.51


def uses_scr_clearing_balance_gl(doc) -> bool:
	if not doc or getattr(doc, "doctype", None) != "Subcontracting Receipt":
		return False
	if int(doc.get("is_return") or 0):
		return False
	return bool(is_irr_company(doc.get("company")))


def _g(entry: Any, key: str, default=None):
	if hasattr(entry, "get"):
		val = entry.get(key, default)
	else:
		val = getattr(entry, key, default)
	return default if val is None else val


def _s(entry: Any, key: str, val) -> None:
	if hasattr(entry, "__setitem__"):
		entry[key] = val
	else:
		setattr(entry, key, val)


def _approx(a: float, b: float, tol: float = TOL) -> bool:
	return abs(flt(a) - flt(b)) <= tol


def _fg_sle_stock_value(doc, item) -> float | None:
	filters = {
		"voucher_type": doc.doctype,
		"voucher_no": doc.name,
		"voucher_detail_no": item.name,
		"is_cancelled": 0,
	}
	if item.warehouse:
		filters["warehouse"] = item.warehouse
	val = frappe.db.get_value("Stock Ledger Entry", filters, "stock_value_difference")
	if val is None:
		filters.pop("warehouse", None)
		filters["actual_qty"] = (">", 0)
		val = frappe.db.get_value("Stock Ledger Entry", filters, "stock_value_difference")
	return flt(val) if val is not None else None


def _amount_precision(item) -> int:
	precision_fn = getattr(item, "precision", None)
	if callable(precision_fn):
		try:
			return int(precision_fn("amount") or 0)
		except Exception:
			pass
	return 0


def _service_cost(item) -> float:
	return flt(flt(item.service_cost_per_qty) * flt(item.qty), _amount_precision(item))


def _fg_items(doc) -> list:
	return [
		item
		for item in (doc.get("items") or [])
		if item.get("bom")
		and not item.get("secondary_item_type")
		and not item.get("valuation_type")
		and flt(item.get("qty"))
		and flt(item.get("rate"))
		and item.get("expense_account")
	]


def _rm_amount_by_expense(doc, fg_items: list) -> dict[str, float]:
	totals: dict[str, float] = {}
	default_exp = fg_items[0].expense_account if fg_items else None
	for rm in doc.get("supplied_items") or []:
		exp = rm.get("expense_account") or default_exp
		if not exp:
			continue
		totals[exp] = totals.get(exp, 0.0) + flt(rm.get("amount"))
	return totals


def _stock_adjustment_account(doc) -> str | None:
	company = doc.get("company")
	if not company:
		return None
	return frappe.get_cached_value("Company", company, "stock_adjustment_account")


def _strip_markers(gl_entries: list) -> None:
	for e in gl_entries or []:
		if hasattr(e, "pop"):
			e.pop(SCR_GL_BALANCE_MARKER, None)
		elif hasattr(e, SCR_GL_BALANCE_MARKER):
			try:
				delattr(e, SCR_GL_BALANCE_MARKER)
			except Exception:
				pass


def _set_amount_fields(entry, *, debit: float = 0.0, credit: float = 0.0) -> None:
	_s(entry, "debit", flt(debit))
	_s(entry, "credit", flt(credit))
	_s(entry, "debit_in_account_currency", flt(debit))
	_s(entry, "credit_in_account_currency", flt(credit))
	if _g(entry, "debit_in_transaction_currency") is not None or _g(entry, "credit_in_transaction_currency") is not None:
		_s(entry, "debit_in_transaction_currency", flt(debit))
		_s(entry, "credit_in_transaction_currency", flt(credit))


def _rewrite_sa_legs(doc, gl_entries: list, delta_total: float) -> bool:
	"""Set SA net (debit−credit) = amount−SVD = −delta_total so voucher balances."""
	sa = _stock_adjustment_account(doc)
	if not sa:
		return False

	target_debit = flt(delta_total) * -1.0 if flt(delta_total) < 0 else 0.0
	target_credit = flt(delta_total) if flt(delta_total) > 0 else 0.0
	# When delta_total > 0 (SVD > amount): credit SA (valuation gain)
	# When delta_total < 0 (SVD < amount): debit SA (valuation loss)

	sa_entries = [e for e in gl_entries if _g(e, "account") == sa]
	cur_debit = sum(flt(_g(e, "debit")) for e in sa_entries)
	cur_credit = sum(flt(_g(e, "credit")) for e in sa_entries)
	if _approx(cur_debit, target_debit, tol=1e-6) and _approx(cur_credit, target_credit, tol=1e-6):
		return False

	# Drop existing SA legs for this voucher map; replace with one residual row
	for e in list(sa_entries):
		gl_entries.remove(e)

	if abs(target_debit) < 1e-9 and abs(target_credit) < 1e-9:
		return True

	template = None
	for e in gl_entries:
		template = e
		break
	if template is None and not sa_entries:
		return False
	src = sa_entries[0] if sa_entries else template

	row = frappe._dict({k: _g(src, k) for k in (src.keys() if hasattr(src, "keys") else [])})
	if not row:
		row = frappe._dict(src)
	_s(row, "account", sa)
	_set_amount_fields(row, debit=target_debit, credit=target_credit)
	# Against clearing / inventory for readability
	fg = _fg_items(doc)
	if fg:
		_s(row, "against", fg[0].expense_account)
	_s(row, SCR_GL_BALANCE_MARKER, 1)
	gl_entries.append(row)
	return True


def normalize_subcontracting_receipt_gl_entries(doc, gl_entries: list | None) -> list:
	"""Balance SCR clearing and align SA residual after Core get_gl_entries."""
	if gl_entries is None:
		return []
	if not uses_scr_clearing_balance_gl(doc):
		return gl_entries
	flags = getattr(doc, "flags", None)
	if flags is not None and flags.get("irr_scr_clearing_balanced"):
		return gl_entries
	if any(_g(e, SCR_GL_BALANCE_MARKER) for e in gl_entries):
		return gl_entries

	fg_items = _fg_items(doc)
	if not fg_items:
		return gl_entries

	doc_credit_by_exp: dict[str, float] = {}
	core_credit_by_exp: dict[str, float] = {}
	delta_by_exp: dict[str, float] = {}
	delta_total = 0.0
	for item in fg_items:
		svd = _fg_sle_stock_value(doc, item)
		if svd is None:
			continue
		amount = flt(item.amount, _amount_precision(item))
		service_cost = _service_cost(item)
		exp = item.expense_account
		delta = flt(svd) - amount
		doc_credit_by_exp[exp] = doc_credit_by_exp.get(exp, 0.0) + (amount - service_cost)
		core_credit_by_exp[exp] = core_credit_by_exp.get(exp, 0.0) + (flt(svd) - service_cost)
		delta_by_exp[exp] = delta_by_exp.get(exp, 0.0) + delta
		delta_total += delta

	if not doc_credit_by_exp:
		return gl_entries

	rm_debit_by_exp = _rm_amount_by_expense(doc, fg_items)
	touched = False
	marked = []

	for exp, doc_credit in doc_credit_by_exp.items():
		core_credit = core_credit_by_exp.get(exp, doc_credit)
		rm_debit = rm_debit_by_exp.get(exp, doc_credit)
		delta = delta_by_exp.get(exp, 0.0)

		cur_debit = sum(flt(_g(e, "debit")) for e in gl_entries if _g(e, "account") == exp)
		cur_credit = sum(flt(_g(e, "credit")) for e in gl_entries if _g(e, "account") == exp)
		clearing_ok = _approx(cur_debit, rm_debit, tol=1e-6) and _approx(cur_credit, doc_credit, tol=1e-6)

		if not clearing_ok:
			merged = [
				e
				for e in gl_entries
				if _g(e, "account") == exp and flt(_g(e, "debit")) > 0 and flt(_g(e, "credit")) > 0
			]
			if merged:
				entry = merged[0]
				_set_amount_fields(entry, debit=rm_debit, credit=doc_credit)
				_s(entry, SCR_GL_BALANCE_MARKER, 1)
				marked.append(entry)
				touched = True
				extras = [
					e
					for e in gl_entries
					if e is not entry
					and _g(e, "account") == exp
					and (
						(_approx(flt(_g(e, "credit")), abs(delta)) and flt(_g(e, "debit")) == 0)
						or (_approx(flt(_g(e, "debit")), abs(delta)) and flt(_g(e, "credit")) == 0)
						or (_approx(flt(_g(e, "debit")), -abs(delta)))
					)
				]
				for e in extras:
					gl_entries.remove(e)
			else:
				for entry in gl_entries:
					if _g(entry, "account") != exp:
						continue
					credit = flt(_g(entry, "credit"))
					debit = flt(_g(entry, "debit"))
					if credit > 0 and debit == 0 and _approx(credit, core_credit):
						_set_amount_fields(entry, debit=0.0, credit=doc_credit)
						_s(entry, SCR_GL_BALANCE_MARKER, 1)
						marked.append(entry)
						touched = True

				drop = []
				for entry in gl_entries:
					if _g(entry, "account") != exp:
						continue
					if _g(entry, SCR_GL_BALANCE_MARKER):
						continue
					credit = flt(_g(entry, "credit"))
					debit = flt(_g(entry, "debit"))
					if debit == 0 and credit > 0 and _approx(credit, abs(delta)) and not _approx(credit, doc_credit):
						drop.append(entry)
					elif credit == 0 and debit > 0 and _approx(debit, abs(delta)) and not _approx(debit, rm_debit):
						drop.append(entry)
					elif debit < 0 and _approx(debit, -abs(delta)):
						drop.append(entry)
				for e in drop:
					gl_entries.remove(e)
					touched = True

	if _rewrite_sa_legs(doc, gl_entries, delta_total):
		touched = True

	_strip_markers(gl_entries)

	# Balanced clearing is a no-op movement. Reuse Manufacture/Repack convention:
	# drop net-zero merged rows (debit==credit or both ~0). Also remove unmerged
	# one-sided clearing pairs that cancel across the same expense account.
	before_len = len(gl_entries)
	gl_entries = _drop_balanced_clearing_accounts(doc, gl_entries, list(doc_credit_by_exp.keys()))
	gl_entries = drop_net_zero_merged_gl_rows(doc, gl_entries)
	if len(gl_entries) != before_len:
		touched = True

	if touched and flags is not None:
		flags.irr_scr_clearing_balanced = True
	return gl_entries


def _drop_balanced_clearing_accounts(doc, gl_entries: list, clearing_accounts: list[str]) -> list:
	"""Remove all legs for SCR clearing accounts whose debit total equals credit total.

	Covers unmerged debit+credit pairs that ``drop_net_zero_merged_gl_rows`` would
	not see (each row is one-sided). Merged equal debit/credit rows are handled by
	``drop_net_zero_merged_gl_rows`` afterward.
	"""
	_ = doc
	if not gl_entries or not clearing_accounts:
		return gl_entries or []

	drop_accounts = set()
	for acc in clearing_accounts:
		debit = sum(flt(_g(e, "debit")) for e in gl_entries if _g(e, "account") == acc)
		credit = sum(flt(_g(e, "credit")) for e in gl_entries if _g(e, "account") == acc)
		if abs(debit - credit) <= TOL:
			drop_accounts.add(acc)

	if not drop_accounts:
		return gl_entries

	return [e for e in gl_entries if _g(e, "account") not in drop_accounts]


def expected_scr_sa_delta(doc) -> float:
	"""Expected Stock Adjustment net (debit − credit) = Σ(amount − SVD) = −ΣΔ."""
	if not uses_scr_clearing_balance_gl(doc):
		return 0.0
	total = 0.0
	for item in _fg_items(doc):
		svd = _fg_sle_stock_value(doc, item)
		if svd is None:
			continue
		total += flt(item.amount, _amount_precision(item)) - flt(svd)
	return flt(total)


def clearing_account_net(gl_entries: list, account: str) -> float:
	net = 0.0
	for e in gl_entries or []:
		if _g(e, "account") != account:
			continue
		net += flt(_g(e, "debit")) - flt(_g(e, "credit"))
	return flt(net)


def clearing_account_present(gl_entries: list, account: str) -> bool:
	return any(_g(e, "account") == account for e in (gl_entries or []))
