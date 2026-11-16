# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Adjusted gross cost for Asset disposal after Asset Value Adjustment (v5.7.46).

ERPNext core ``get_asset_disposal_values`` uses ``net_purchase_amount`` only.
AVA updates VAD and posts Dr Fixed Asset / Cr Revaluation Surplus (or the
reverse on decrease) but never changes ``net_purchase_amount``. Scrap/Sale JE
generation therefore derecognizes original cost and leaves revalued gross
uncleared.

Adjusted gross (authoritative for this app):

	net_purchase_amount
	+ sum(submitted AVA difference_amount)   # signed; draft/cancelled excluded
	+ additional_asset_cost                  # capitalized repairs; not in AVA

Scrap JE policy (authorized): **only** Cr Fixed Asset / Dr Accumulated
Depreciation for adjusted gross after disposal-day depreciation. Revaluation
surplus is left on historical AVA entries — not transferred in the Scrap JE.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, getdate


def get_submitted_ava_difference_rows(
	asset_name: str, finance_book: str | None = None
) -> list[dict]:
	"""Submitted AVA rows for an asset (draft/cancelled excluded).

	When ``finance_book`` is set, include rows whose Finance Book is empty
	(company default) or equals that book. When unset, include all submitted
	rows (typical single-book scrap).
	"""
	if not asset_name:
		return []

	rows = frappe.get_all(
		"Asset Value Adjustment",
		filters={"asset": asset_name, "docstatus": 1},
		fields=[
			"name",
			"date",
			"difference_amount",
			"difference_account",
			"finance_book",
			"journal_entry",
			"current_asset_value",
			"new_asset_value",
		],
		order_by="date asc, creation asc",
	)
	if finance_book:
		fb = cstr(finance_book)
		rows = [r for r in rows if not cstr(r.finance_book) or cstr(r.finance_book) == fb]
	return rows


def sum_submitted_ava_differences(asset_name: str, finance_book: str | None = None) -> float:
	"""Signed sum of submitted AVA difference amounts (up + down)."""
	return flt(
		sum(flt(r.difference_amount) for r in get_submitted_ava_difference_rows(asset_name, finance_book))
	)


def get_adjusted_gross_asset_value(asset, finance_book: str | None = None) -> dict:
	"""Return adjusted disposal gross and AVA breakdown.

	``additional_asset_cost`` is added once from the Asset master (ERPNext
	tracks capitalized repairs there separately from AVA).
	"""
	asset_name = asset if isinstance(asset, str) else asset.name
	if isinstance(asset, str):
		asset = frappe.get_doc("Asset", asset_name)

	original = flt(asset.net_purchase_amount)
	ava_rows = get_submitted_ava_difference_rows(asset_name, finance_book)
	ava_sum = flt(sum(flt(r.difference_amount) for r in ava_rows))
	additional = flt(getattr(asset, "additional_asset_cost", 0) or 0)
	gross = original + ava_sum + additional

	return {
		"original_purchase_cost": original,
		"ava_difference_total": ava_sum,
		"additional_asset_cost": additional,
		"gross_asset_value": gross,
		"ava_rows": ava_rows,
		"ava_names": [r.name for r in ava_rows],
	}


def get_adjusted_asset_disposal_values(asset, finance_book: str | None = None) -> dict:
	"""Drop-in replacement for core ``get_asset_disposal_values``."""
	if isinstance(asset, str):
		asset = frappe.get_doc("Asset", asset)

	adjusted = get_adjusted_gross_asset_value(asset, finance_book)
	return {
		"value_after_depreciation": asset.get_value_after_depreciation(finance_book),
		"gross_asset_value": adjusted["gross_asset_value"],
		# Extension metadata (ignored by core callers that only read the two keys)
		"original_purchase_cost": adjusted["original_purchase_cost"],
		"ava_difference_total": adjusted["ava_difference_total"],
		"additional_asset_cost": adjusted["additional_asset_cost"],
		"ava_names": adjusted["ava_names"],
	}


def _is_equity_leaf(account: str) -> bool:
	if not account or not frappe.db.exists("Account", account):
		return False
	row = frappe.db.get_value(
		"Account", account, ["root_type", "is_group", "disabled"], as_dict=True
	)
	return bool(row and row.root_type == "Equity" and not cint(row.is_group) and not cint(row.disabled))


def get_retained_earnings_account(company: str, surplus_account: str | None = None) -> str:
	"""Resolve Retained Earnings for surplus transfer (not disposal P&L).

	Preference order:
	1. Sibling leaf under the same parent as ``surplus_account`` whose number/name
	   matches retained earnings (Iranian COA ``311301``).
	2. Company equity leaf matching retained-earnings naming.
	"""
	company = company or frappe.defaults.get_user_default("Company")

	def _is_re_candidate(name: str, account_number: str | None = None) -> bool:
		blob = f"{account_number or ''} {name}".lower()
		if "311301" in blob.replace(" ", ""):
			return True
		# Iranian / English retained earnings — exclude surplus / prior-year adj
		if "تعدیل" in name or "سنواتی" in name:
			return False
		if "مازاد" in name and "تجدید" in name:
			return False
		if "revaluation" in blob and "surplus" in blob:
			return False
		if "انباشته" in name and "سود" in name:
			return True
		if "retained" in blob and "earning" in blob:
			return True
		return False

	if surplus_account and frappe.db.exists("Account", surplus_account):
		parent = frappe.db.get_value("Account", surplus_account, "parent_account")
		if parent:
			siblings = frappe.get_all(
				"Account",
				filters={
					"company": company,
					"parent_account": parent,
					"is_group": 0,
					"disabled": 0,
				},
				fields=["name", "account_number", "account_name"],
			)
			for s in siblings:
				if s.name == surplus_account:
					continue
				if _is_re_candidate(s.name, s.account_number):
					return s.name

	# Company-wide equity search
	equity = frappe.get_all(
		"Account",
		filters={"company": company, "root_type": "Equity", "is_group": 0, "disabled": 0},
		fields=["name", "account_number", "account_name"],
		order_by="account_number asc",
	)
	for a in equity:
		if _is_re_candidate(a.name, a.account_number):
			return a.name

	frappe.throw(
		_(
			"Cannot settle revaluation surplus on asset disposal: no Retained Earnings "
			"account found for Company {0}. Configure an Equity leaf such as "
			"311301 (سود / زیان انباشته) as the transfer destination."
		).format(frappe.bold(company))
	)


def get_revaluation_surplus_balances_for_asset(
	asset_name: str, finance_book: str | None = None
) -> dict[str, float]:
	"""Net AVA difference posted per difference_account (equity surplus only).

	Positive = credit balance to clear (Dr surplus / Cr RE on scrap).
	"""
	balances: dict[str, float] = defaultdict(float)
	for row in get_submitted_ava_difference_rows(asset_name, finance_book):
		acct = row.difference_account
		if not acct or not _is_equity_leaf(acct):
			continue
		balances[acct] += flt(row.difference_amount)
	return {k: flt(v) for k, v in balances.items() if abs(flt(v)) > 1e-9}


def build_revaluation_surplus_transfer_entries(
	asset,
	*,
	date,
	cost_center: str | None = None,
	finance_book: str | None = None,
) -> list[dict]:
	"""GL rows: Dr revaluation surplus / Cr retained earnings (per surplus account)."""
	if isinstance(asset, str):
		asset = frappe.get_doc("Asset", asset)

	balances = get_revaluation_surplus_balances_for_asset(asset.name, finance_book)
	if not balances:
		return []

	cc = cost_center or asset.cost_center or frappe.get_cached_value(
		"Company", asset.company, "depreciation_cost_center"
	)
	entries: list[dict] = []
	for surplus_account, amount in balances.items():
		# Net credit on surplus → debit surplus to clear; credit RE
		# Net debit (unusual) → reverse direction
		re_account = get_retained_earnings_account(asset.company, surplus_account)
		amt = abs(flt(amount))
		if amt < 1e-9:
			continue
		if flt(amount) > 0:
			debit_account, credit_account = surplus_account, re_account
		else:
			debit_account, credit_account = re_account, surplus_account

		entries.append(
			asset.get_gl_dict(
				{
					"account": debit_account,
					"debit": amt,
					"debit_in_account_currency": amt,
					"cost_center": cc,
					"posting_date": date,
				},
				item=asset,
			)
		)
		entries.append(
			asset.get_gl_dict(
				{
					"account": credit_account,
					"credit": amt,
					"credit_in_account_currency": amt,
					"cost_center": cc,
					"posting_date": date,
				},
				item=asset,
			)
		)
	return entries


def get_gl_entries_on_asset_disposal_adjusted(
	asset,
	selling_amount=0,
	finance_book=None,
	voucher_type=None,
	voucher_no=None,
	date=None,
	disposal_values: dict | None = None,
	*,
	include_surplus_transfer: bool = False,
):
	"""Like core disposal GL, but FA leg uses adjusted gross (not net_purchase)."""
	from erpnext.assets.doctype.asset.depreciation import (
		get_depreciation_accounts,
		get_disposal_account_and_cost_center,
		get_profit_gl_entries,
	)

	if not date:
		date = getdate()

	if isinstance(asset, str):
		asset = frappe.get_doc("Asset", asset)

	disposal_values = disposal_values or get_adjusted_asset_disposal_values(asset, finance_book)
	value_after_depreciation = flt(disposal_values["value_after_depreciation"])
	gross_asset_value = flt(disposal_values["gross_asset_value"])
	accumulated_depr_amount = gross_asset_value - value_after_depreciation

	fixed_asset_account, accumulated_depr_account, _ = get_depreciation_accounts(
		asset.asset_category, asset.company
	)
	disposal_account, depreciation_cost_center = get_disposal_account_and_cost_center(asset.company)
	depreciation_cost_center = asset.cost_center or depreciation_cost_center

	gl_entries = [
		asset.get_gl_dict(
			{
				"account": fixed_asset_account,
				"credit_in_account_currency": gross_asset_value,
				"credit": gross_asset_value,
				"cost_center": depreciation_cost_center,
				"posting_date": date,
			},
			item=asset,
		),
	]

	if accumulated_depr_amount:
		gl_entries.append(
			asset.get_gl_dict(
				{
					"account": accumulated_depr_account,
					"debit_in_account_currency": accumulated_depr_amount,
					"debit": accumulated_depr_amount,
					"cost_center": depreciation_cost_center,
					"posting_date": date,
				},
				item=asset,
			),
		)

	profit_amount = flt(selling_amount) - flt(value_after_depreciation)
	if profit_amount:
		get_profit_gl_entries(
			asset, profit_amount, gl_entries, disposal_account, depreciation_cost_center, date
		)

	if include_surplus_transfer:
		gl_entries.extend(
			build_revaluation_surplus_transfer_entries(
				asset,
				date=date,
				cost_center=depreciation_cost_center,
				finance_book=finance_book,
			)
		)

	if voucher_type and voucher_no:
		for entry in gl_entries:
			entry["voucher_type"] = voucher_type
			entry["voucher_no"] = voucher_no

	return gl_entries


def get_gl_entries_on_asset_regain_adjusted(
	asset,
	selling_amount=0,
	finance_book=None,
	voucher_type=None,
	voucher_no=None,
	date=None,
	disposal_values: dict | None = None,
):
	"""Symmetric regain path using adjusted gross for the FA debit."""
	from erpnext.assets.doctype.asset.depreciation import (
		get_depreciation_accounts,
		get_disposal_account_and_cost_center,
		get_profit_gl_entries,
	)

	if not date:
		date = getdate()
	if isinstance(asset, str):
		asset = frappe.get_doc("Asset", asset)

	disposal_values = disposal_values or get_adjusted_asset_disposal_values(asset, finance_book)
	value_after_depreciation = flt(disposal_values["value_after_depreciation"])
	gross_asset_value = flt(disposal_values["gross_asset_value"])
	accumulated_depr_amount = gross_asset_value - value_after_depreciation

	fixed_asset_account, accumulated_depr_account, _ = get_depreciation_accounts(
		asset.asset_category, asset.company
	)
	disposal_account, depreciation_cost_center = get_disposal_account_and_cost_center(asset.company)
	depreciation_cost_center = asset.cost_center or depreciation_cost_center

	gl_entries = [
		asset.get_gl_dict(
			{
				"account": fixed_asset_account,
				"debit_in_account_currency": gross_asset_value,
				"debit": gross_asset_value,
				"cost_center": depreciation_cost_center,
				"posting_date": date,
			},
			item=asset,
		),
		asset.get_gl_dict(
			{
				"account": accumulated_depr_account,
				"credit_in_account_currency": accumulated_depr_amount,
				"credit": accumulated_depr_amount,
				"cost_center": depreciation_cost_center,
				"posting_date": date,
			},
			item=asset,
		),
	]

	profit_amount = abs(flt(value_after_depreciation)) - abs(flt(selling_amount))
	if profit_amount:
		get_profit_gl_entries(
			asset, profit_amount, gl_entries, disposal_account, depreciation_cost_center, date
		)

	if voucher_type and voucher_no:
		for entry in gl_entries:
			entry["voucher_type"] = voucher_type
			entry["voucher_no"] = voucher_no

	return gl_entries
