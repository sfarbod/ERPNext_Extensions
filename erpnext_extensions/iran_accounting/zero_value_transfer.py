# Copyright (c) 2026, ERPNext Extensions contributors
"""Zero-value stock transfer GL building (Material Transfer / MTfM / Send to Subcontractor)."""

from __future__ import annotations

import frappe
from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import get_accounting_dimensions
from erpnext.accounts.general_ledger import get_merge_key, get_merge_properties, process_gl_map
from erpnext.stock.doctype.inventory_dimension.inventory_dimension import get_inventory_dimensions
from frappe import _
from frappe.utils import flt

from erpnext_extensions.iran_accounting.rounding import (
	get_company_currency,
	get_currency_precision,
	round_currency,
)

ZERO_VALUE_TRANSFER_STOCK_ENTRY_PURPOSES = (
	"Material Transfer",
	"Material Transfer for Manufacture",
	"Send to Subcontractor",
)


def absorb_gl_map_rounding_residual(
	gl_map, precision, debit_credit_diff=None, trx_cur_debit_credit_diff=None
):
	"""Adjust the largest GL leg to absorb sub-unit rounding without Stock Adjustment / Round Off."""
	if not gl_map:
		return

	from erpnext.accounts.general_ledger import get_debit_credit_difference

	if debit_credit_diff is None or trx_cur_debit_credit_diff is None:
		debit_credit_diff, trx_cur_debit_credit_diff = get_debit_credit_difference(gl_map, precision)

	if not debit_credit_diff and not trx_cur_debit_credit_diff:
		return

	def _absorb_priority(entry) -> tuple:
		account = entry.get("account") or ""
		account_type = frappe.get_cached_value("Account", account, "account_type") if account else None
		if account_type in ("Payable", "Receivable"):
			return (0, max(flt(entry.get("debit")), flt(entry.get("credit"))))
		if account_type == "Stock":
			return (1, max(flt(entry.get("debit")), flt(entry.get("credit"))))
		if account_type in ("Cost of Goods Sold", "Round Off", "Stock Adjustment"):
			return (9, 0)
		return (5, max(flt(entry.get("debit")), flt(entry.get("credit"))))

	def _pick_entry(entries):
		return max(entries, key=_absorb_priority)

	entry = _pick_entry(gl_map)
	if flt(debit_credit_diff, precision):
		if debit_credit_diff > 0:
			entry.credit = flt(flt(entry.credit) + debit_credit_diff, precision)
			entry.credit_in_account_currency = flt(
				flt(entry.get("credit_in_account_currency")) + trx_cur_debit_credit_diff, precision
			)
		else:
			entry.debit = flt(flt(entry.debit) - debit_credit_diff, precision)
			entry.debit_in_account_currency = flt(
				flt(entry.get("debit_in_account_currency")) - trx_cur_debit_credit_diff, precision
			)


def _refresh_zero_value_transfer_totals(self):
	if self.doctype != "Stock Entry":
		return
	if hasattr(self, "set_total_incoming_outgoing_value"):
		self.set_total_incoming_outgoing_value()


def _should_force_balanced_transfer_gl(self, precision):
	if self.doctype != "Stock Entry":
		return False

	if self.purpose not in ZERO_VALUE_TRANSFER_STOCK_ENTRY_PURPOSES:
		return False

	_refresh_zero_value_transfer_totals(self)
	incoming = flt(self.total_incoming_value, precision)
	outgoing = flt(self.total_outgoing_value, precision)
	if incoming != outgoing:
		return False

	return flt(getattr(self, "value_difference", 0), precision) == 0


def _get_transfer_expense_account(self, item_row, inventory_account_map, sle=None):
	if item_row.get("target_warehouse"):
		_target_wh_inv_dict = self.get_inventory_account_dict(
			item_row, inventory_account_map, warehouse_field="target_warehouse"
		)
		return _target_wh_inv_dict["account"]

	return item_row.expense_account


def _append_zero_value_transfer_inventory_gl(self, gl_list, amount, sle, inv_dict, item_row, precision):
	_ = precision
	amount = round_currency(amount, get_company_currency(self.company))
	if not amount:
		return

	project = sle.get("project") or item_row.project or self.get("project")
	base = {
		"account": inv_dict["account"],
		"cost_center": item_row.cost_center,
		"project": project,
		"remarks": self.get("remarks") or _("Accounting Entry for Stock"),
		"is_opening": item_row.get("is_opening") or self.get("is_opening") or "No",
	}
	if amount > 0:
		gl_list.append(
			self.get_gl_dict(
				{**base, "debit": amount, "against": inv_dict["account"]},
				inv_dict["account_currency"],
				item=item_row,
			)
		)
	else:
		gl_list.append(
			self.get_gl_dict(
				{**base, "credit": abs(amount), "against": inv_dict["account"]},
				inv_dict["account_currency"],
				item=item_row,
			)
		)


def _get_transfer_gl_aggregate_key(self, sle, item_row, inv_dict):
	project = sle.get("project") or item_row.get("project") or self.get("project")
	cost_center = item_row.cost_center

	key = [
		inv_dict.get("account"),
		inv_dict.get("account_currency"),
		sle.warehouse,
		project,
		cost_center,
	]

	for dimension in get_accounting_dimensions():
		key.append(item_row.get(dimension) or self.get(dimension))

	sle_meta = frappe.get_meta("Stock Ledger Entry")
	for dimension in get_inventory_dimensions():
		fieldname = dimension.fieldname
		if sle_meta.has_field(fieldname):
			key.append(sle.get(fieldname) or item_row.get(fieldname))

	return tuple(key)


def _get_balanced_stock_gl_amounts(self, group_amounts, precision, sle_rounding_diff):
	tolerance = 1.0 / (10**precision) if precision else 1.0
	if abs(flt(sle_rounding_diff, precision)) > tolerance:
		return {key: flt(amount, precision) for key, amount in group_amounts.items()}

	rounded = {key: flt(amount, precision) for key, amount in group_amounts.items()}
	rounding_residual = flt(sum(rounded.values()), precision)
	if not rounding_residual:
		return rounded

	key_to_adjust = max(group_amounts, key=lambda key: abs(group_amounts[key]))
	rounded[key_to_adjust] = flt(rounded[key_to_adjust] - rounding_residual, precision)
	return rounded


def _gl_amount_precision_for_entry(self, entry, company_precision):
	from frappe.model.meta import get_field_precision

	account = entry.get("account")
	if not account:
		return company_precision, company_precision

	account_currency = entry.get("account_currency") or frappe.db.get_value(
		"Account", account, "account_currency"
	)
	company_currency = get_company_currency(self.company)
	gl_field = frappe.get_meta("GL Entry").get_field("debit_in_account_currency")
	acct_precision = get_field_precision(gl_field, currency=account_currency or company_currency)
	return company_precision, acct_precision


def _is_effectively_zero_gl_entry(self, entry, company_precision):
	company_precision, acct_precision = _gl_amount_precision_for_entry(self, entry, company_precision)
	min_company = 1.0 / (10**company_precision) if company_precision else 1.0
	min_acct = 1.0 / (10**acct_precision) if acct_precision else 1.0

	for field, min_unit, prec in (
		("debit", min_company, company_precision),
		("credit", min_company, company_precision),
		("debit_in_account_currency", min_acct, acct_precision),
		("credit_in_account_currency", min_acct, acct_precision),
	):
		val = flt(entry.get(field), prec)
		if abs(val) >= min_unit:
			return False
	return True


def _is_adjustment_or_round_off_account(self, account, stock_adj, round_off):
	if not account:
		return False
	if account in (stock_adj, round_off):
		return True
	account_type = frappe.get_cached_value("Account", account, "account_type")
	return account_type in ("Stock Adjustment", "Round Off")


def _sanitize_zero_value_transfer_gl_list(self, gl_list, precision):
	if not gl_list:
		return gl_list

	stock_adj = frappe.get_cached_value("Company", self.company, "stock_adjustment_account")
	round_off = frappe.get_cached_value("Company", self.company, "round_off_account")

	sanitized = []
	for entry in gl_list:
		if _is_adjustment_or_round_off_account(self, entry.get("account"), stock_adj, round_off):
			if _is_effectively_zero_gl_entry(self, entry, precision):
				continue
		sanitized.append(entry)

	return sanitized


def finalize_zero_value_transfer_gl_map(self, gl_map, precision=None):
	if not gl_map:
		return gl_map

	precision = precision if precision is not None else self.get_debit_field_precision()
	if not _should_force_balanced_transfer_gl(self, precision):
		return gl_map

	gl_map = _sanitize_zero_value_transfer_gl_list(self, gl_map, precision)
	return _absorb_transfer_gl_residual(self, gl_map, precision)


def _absorb_transfer_gl_residual(self, gl_list, precision):
	if not gl_list:
		return gl_list
	absorb_gl_map_rounding_residual(gl_list, precision)
	return gl_list


def get_debit_field_precision_for_company(self):
	company_currency = get_company_currency(self.company) if getattr(self, "company", None) else None
	if company_currency:
		return get_currency_precision(company_currency)
	return self._iran_original_get_debit_field_precision()


def _erpnext_gl_merge_key(entry) -> tuple:
	"""Same merge semantics as ERPNext merge_similar_entries (pre-submit GL dict)."""
	return get_merge_key(entry, get_merge_properties(get_accounting_dimensions()))


def _should_skip_balanced_transfer_gl_pair(self, credit_entry, debit_entry, precision) -> bool:
	"""Skip both legs when merge would net to a single non-empty GL row (FINAL_GL_COUNT == 1)."""
	if _erpnext_gl_merge_key(credit_entry) != _erpnext_gl_merge_key(debit_entry):
		return False
	credit_amt = flt(credit_entry.credit, precision)
	debit_amt = flt(debit_entry.debit, precision)
	if credit_amt != debit_amt:
		return False
	return True


def _append_balanced_transfer_item_gl(self, gl_list, item_row, inventory_account_map, precision):
	"""Post one credit (source) + one debit (target) from item amount — avoids duplicate SLE aggregation."""
	from_wh = item_row.get("s_warehouse")
	to_wh = item_row.get("t_warehouse")
	if not from_wh or not to_wh:
		return False
	company_currency = get_company_currency(self.company)
	amount = round_currency(flt(item_row.get("amount")), company_currency)
	if not amount:
		return False
	out_inv = self.get_inventory_account_dict(item_row, inventory_account_map, warehouse_field="s_warehouse")
	in_inv = self.get_inventory_account_dict(item_row, inventory_account_map, warehouse_field="t_warehouse")
	if not out_inv.get("account") or not in_inv.get("account"):
		return False
	project = item_row.project or self.get("project")
	base = {
		"cost_center": item_row.cost_center,
		"project": project,
		"remarks": self.get("remarks") or _("Accounting Entry for Stock"),
		"is_opening": item_row.get("is_opening") or self.get("is_opening") or "No",
	}
	credit_entry = self.get_gl_dict(
		{
			**base,
			"account": out_inv["account"],
			"against": in_inv["account"],
			"credit": amount,
		},
		out_inv["account_currency"],
		item=item_row,
	)
	debit_entry = self.get_gl_dict(
		{
			**base,
			"account": in_inv["account"],
			"against": out_inv["account"],
			"debit": amount,
		},
		in_inv["account_currency"],
		item=item_row,
	)
	if _should_skip_balanced_transfer_gl_pair(self, credit_entry, debit_entry, precision):
		return True
	gl_list.append(credit_entry)
	gl_list.append(debit_entry)
	return True


def expected_balanced_transfer_gl_magnitude(doc, inventory_account_map=None) -> float:
	"""Σ row.amount for items whose source/target warehouses use different stock accounts."""
	if not inventory_account_map:
		inventory_account_map = doc.get_inventory_account_map()
	total = 0.0
	for row in doc.get("items") or []:
		if not (row.get("s_warehouse") and row.get("t_warehouse")):
			continue
		out_a = doc.get_inventory_account_dict(
			row, inventory_account_map, warehouse_field="s_warehouse"
		).get("account")
		in_a = doc.get_inventory_account_dict(
			row, inventory_account_map, warehouse_field="t_warehouse"
		).get("account")
		if out_a == in_a:
			continue
		total += flt(row.get("amount"))
	return flt(total)


def build_iran_balanced_transfer_gl(
	self, inventory_account_map=None, default_expense_account=None, default_cost_center=None
):
	"""Zero-value transfer inventory↔inventory GL (no expense-bridge doubling)."""
	if not inventory_account_map:
		inventory_account_map = self.get_inventory_account_map()

	sle_map = self.get_stock_ledger_details()
	voucher_details = self.get_voucher_details(default_expense_account, default_cost_center, sle_map)
	gl_list = []
	precision = self.get_debit_field_precision()

	for item_row in voucher_details:
		_append_balanced_transfer_item_gl(self, gl_list, item_row, inventory_account_map, precision)

	gl_map = process_gl_map(
		gl_list, precision=precision, from_repost=frappe.flags.through_repost_item_valuation
	)
	return finalize_zero_value_transfer_gl_map(self, gl_map, precision)


def resolve_original_stock_entry_get_gl_entries():
	"""Return the saved ERPNext StockEntry.get_gl_entries callable."""
	from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry

	original = getattr(StockEntry, "_iran_original_stock_entry_get_gl_entries", None)
	if not callable(original):
		frappe.throw(
			_(
				"iran_accounting Stock Entry GL patch is misconfigured: "
				"_iran_original_stock_entry_get_gl_entries is missing or not callable"
			),
			title=_("IRR GL Ownership"),
		)
	if getattr(original, "_iran_stock_entry_gl_wrapper", None):
		frappe.throw(
			_(
				"iran_accounting Stock Entry GL patch saved its own wrapper as the original "
				"(duplicate / circular patch installation)"
			),
			title=_("IRR GL Ownership"),
		)
	return original


def iran_stock_entry_get_gl_entries(
	self, inventory_account_map=None, default_expense_account=None, default_cost_center=None
):
	"""ERPNext-first SE GL: balanced transfer, normalized Manufacture, or Core.

	Manufacture / Repack on IRR companies use normalized GL generation
	(``manufacture_expense_account.build_normalized_manufacture_gl_entries``):
	inventory SLE legs + Additional Cost credits + a single Stock Adjustment
	residual. Stock Adjustment is never a manufacturing clearing account.
	"""
	_ = default_expense_account, default_cost_center
	precision = self.get_debit_field_precision()
	if _should_force_balanced_transfer_gl(self, precision):
		return build_iran_balanced_transfer_gl(self, inventory_account_map)

	from erpnext_extensions.iran_accounting.domain.manufacture_expense_account import (
		build_normalized_manufacture_gl_entries,
		uses_normalized_manufacture_gl,
	)

	if uses_normalized_manufacture_gl(self):
		return build_normalized_manufacture_gl_entries(self, inventory_account_map)

	original = resolve_original_stock_entry_get_gl_entries()
	# Core StockEntry.get_gl_entries(self, inventory_account_map) only.
	return original(self, inventory_account_map)


def get_gl_entries(self, inventory_account_map=None, default_expense_account=None, default_cost_center=None):
	"""Dispatch: Stock Reconciliation builder, else Stock Entry ERPNext-first wrapper."""
	if self.doctype == "Stock Reconciliation":
		from erpnext_extensions.iran_accounting.domain.stock_reconciliation_gl import (
			get_stock_reconciliation_gl_entries,
		)

		return get_stock_reconciliation_gl_entries(
			self, inventory_account_map, default_expense_account, default_cost_center
		)

	if self.doctype == "Stock Entry":
		return iran_stock_entry_get_gl_entries(
			self, inventory_account_map, default_expense_account, default_cost_center
		)

	from erpnext.controllers.stock_controller import StockController

	orig = getattr(StockController, "_iran_original_get_gl_entries", None)
	if orig:
		return orig(self, inventory_account_map, default_expense_account, default_cost_center)
	return StockController.get_gl_entries(
		self, inventory_account_map, default_expense_account, default_cost_center
	)


STOCK_CONTROLLER_METHODS = {
	"_refresh_zero_value_transfer_totals": _refresh_zero_value_transfer_totals,
	"_should_force_balanced_transfer_gl": _should_force_balanced_transfer_gl,
	"_get_transfer_expense_account": _get_transfer_expense_account,
	"_append_zero_value_transfer_inventory_gl": _append_zero_value_transfer_inventory_gl,
	"_get_transfer_gl_aggregate_key": _get_transfer_gl_aggregate_key,
	"_get_balanced_stock_gl_amounts": _get_balanced_stock_gl_amounts,
	"_gl_amount_precision_for_entry": _gl_amount_precision_for_entry,
	"_is_effectively_zero_gl_entry": _is_effectively_zero_gl_entry,
	"_is_adjustment_or_round_off_account": _is_adjustment_or_round_off_account,
	"_sanitize_zero_value_transfer_gl_list": _sanitize_zero_value_transfer_gl_list,
	"finalize_zero_value_transfer_gl_map": finalize_zero_value_transfer_gl_map,
	"_absorb_transfer_gl_residual": _absorb_transfer_gl_residual,
	"get_gl_entries": get_gl_entries,
	"get_debit_field_precision_for_company": get_debit_field_precision_for_company,
}
