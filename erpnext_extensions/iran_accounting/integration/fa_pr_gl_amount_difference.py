# Copyright (c) 2026, ERPNext Extensions contributors
"""Fixed Asset Purchase Receipt GL: include PI amount difference on FA debit.

ERPNext 16.53.0 ``PurchaseReceipt.make_item_gl_entries`` computes FA inward debit as::

    base_net_amount + item_tax_amount + landed_cost_voucher_amount

while ``make_amount_difference_entry`` posts ``amount_difference_with_purchase_invoice``
onto Asset Received But Not Billed (GRNI). ``make_divisional_loss_gl_entry`` returns
early for fixed assets, so the map is unbalanced by ``amt_diff``.

When Buying Settings ``set_landed_cost_based_on_purchase_invoice_rate`` is enabled,
stock items already absorb the difference via SLE valuation. Fixed assets must debit
the invoice-aligned acquisition amount::

    FA debit = base_net + item_tax + LCV + amount_difference_with_purchase_invoice

``amount_difference`` is **not** present in base_net / tax / LCV — adding it once to
the FA debit only. GRNI rows from core are left untouched.

Install exactly once via ``install_fa_pr_gl_amount_difference_patch``.
"""

from __future__ import annotations

import logging

import frappe
from frappe.utils import cint, flt

logger = logging.getLogger(__name__)

_PATCH_ATTR = "_iran_fa_pr_gl_amt_diff"
_installed = False
_original_make_item_gl_entries = None


def invoice_based_valuation_enabled() -> bool:
	return cint(
		frappe.db.get_single_value(
			"Buying Settings", "set_landed_cost_based_on_purchase_invoice_rate"
		)
	)


def core_fa_stock_value_diff(item) -> float:
	"""Exact ERPNext 16.53.0 FA debit numerator (excludes amt_diff)."""
	return (
		flt(item.base_net_amount)
		+ flt(item.item_tax_amount)
		+ flt(item.landed_cost_voucher_amount)
	)


def corrected_fa_stock_value_diff(item, *, include_amount_difference: bool) -> float:
	"""FA debit with optional PI amount difference (no double-count of tax/LCV)."""
	value = core_fa_stock_value_diff(item)
	if include_amount_difference:
		value += flt(item.amount_difference_with_purchase_invoice)
	return value


def should_adjust_fa_item(item) -> bool:
	if not cint(getattr(item, "is_fixed_asset", 0)):
		return False
	if not invoice_based_valuation_enabled():
		return False
	return abs(flt(getattr(item, "amount_difference_with_purchase_invoice", 0))) > 1e-9


def _iter_doc_items(doc):
	items = getattr(doc, "items", None)
	if items is None and hasattr(doc, "get"):
		items = doc.get("items")
	return items or []


def adjust_fixed_asset_amount_difference_debits(doc, gl_entries, start_idx: int = 0) -> int:
	"""Mutate in-memory FA debit rows so they include amt_diff. Returns adjustments made."""
	if not invoice_based_valuation_enabled():
		return 0

	adjusted = 0
	for item in _iter_doc_items(doc):
		if not should_adjust_fa_item(item):
			continue

		core_debit = core_fa_stock_value_diff(item)
		target_debit = corrected_fa_stock_value_diff(item, include_amount_difference=True)
		if abs(target_debit - core_debit) <= 1e-9:
			continue

		expense_account = getattr(item, "expense_account", None)
		if not expense_account:
			continue

		matched = False
		for entry in gl_entries[start_idx:]:
			if entry.get("_iran_fa_amt_diff_adjusted"):
				continue
			if entry.get("account") != expense_account:
				continue
			if abs(flt(entry.get("credit") or 0)) > 1e-9:
				continue
			# Match the core FA inward debit (exclude already-adjusted / unrelated rows).
			if abs(flt(entry.get("debit") or 0) - core_debit) > 0.5:
				continue

			# Assign plain floats — avoid flt(..., precision) which depends on System Settings.
			entry["debit"] = float(target_debit)
			if entry.get("debit_in_account_currency") is not None:
				if abs(flt(entry.get("debit_in_account_currency")) - core_debit) <= 0.5:
					entry["debit_in_account_currency"] = float(target_debit)
			entry["_iran_fa_amt_diff_adjusted"] = True
			matched = True
			adjusted += 1
			break

		if not matched:
			logger.warning(
				"iran_accounting: FA amt_diff debit adjust skipped for %s item %s "
				"(expected core debit %s on %s not found)",
				getattr(doc, "name", "?"),
				getattr(item, "name", "?"),
				core_debit,
				expense_account,
			)

	return adjusted


def _wrapped_make_item_gl_entries(self, gl_entries, inventory_account_map=None):
	start = len(gl_entries)
	_original_make_item_gl_entries(self, gl_entries, inventory_account_map=inventory_account_map)
	adjust_fixed_asset_amount_difference_debits(self, gl_entries, start_idx=start)


def install_fa_pr_gl_amount_difference_patch() -> dict:
	"""Idempotent install on PurchaseReceipt.make_item_gl_entries."""
	global _installed, _original_make_item_gl_entries

	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import PurchaseReceipt

	current = PurchaseReceipt.make_item_gl_entries
	if getattr(current, _PATCH_ATTR, False):
		_installed = True
		return {"status": "already_installed", "patched": True}

	_original_make_item_gl_entries = current
	_wrapped_make_item_gl_entries._iran_original = current  # type: ignore[attr-defined]
	setattr(_wrapped_make_item_gl_entries, _PATCH_ATTR, True)
	PurchaseReceipt.make_item_gl_entries = _wrapped_make_item_gl_entries
	_installed = True
	logger.info("iran_accounting: installed FA PR GL amount-difference debit patch")
	return {"status": "installed", "patched": True}


def uninstall_fa_pr_gl_amount_difference_patch() -> None:
	"""Restore core method (tests)."""
	global _installed, _original_make_item_gl_entries
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import PurchaseReceipt

	if _original_make_item_gl_entries is not None:
		PurchaseReceipt.make_item_gl_entries = _original_make_item_gl_entries
	_original_make_item_gl_entries = None
	_installed = False


def is_fa_pr_gl_amount_difference_patch_installed() -> bool:
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import PurchaseReceipt

	return bool(getattr(PurchaseReceipt.make_item_gl_entries, _PATCH_ATTR, False))
