# Copyright (c) 2026, ERPNext Extensions contributors
"""IRR-safe cost center allocation: split legs must sum to the pre-split amount."""

from __future__ import annotations

import copy

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision, is_irr_company
from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
	assert_irr_residual_round_off_masters,
	is_irr_inventory_valuation_residual_gl,
	is_irr_rate_rounding_residual_gl,
	stamp_irr_inventory_valuation_residual_masters,
	stamp_irr_residual_round_off_masters,
)

_SPLIT_FIELDS = (
	"debit",
	"credit",
	"debit_in_account_currency",
	"credit_in_account_currency",
)


def absorb_irr_cost_center_split_residual(gle_list: list, template: dict, precision: int) -> None:
	"""After percentage splits, restore exact totals per monetary field (IRR integer)."""
	if not gle_list:
		return
	target = gle_list[-1]
	for field in _SPLIT_FIELDS:
		original = flt(template.get(field), precision)
		if not original:
			continue
		split_sum = sum(flt(g.get(field), precision) for g in gle_list)
		residual = flt(original - split_sum, precision)
		if residual:
			target[field] = flt(flt(target.get(field)) + residual, precision)


def _original_distribute_gl_based_on_cost_center_allocation():
	"""Resolve ERPNext's unpatched distribute_gl (safe after iran monkey patch)."""
	import erpnext.accounts.general_ledger as gl

	# After `_patch_general_ledger`, module attribute points at our wrapper.
	# Calling that as "_orig" causes infinite recursion for non-IRR companies.
	orig = getattr(gl, "_iran_original_distribute_cc", None)
	if orig:
		return orig
	return gl.distribute_gl_based_on_cost_center_allocation


def distribute_gl_based_on_cost_center_allocation_irr(gl_map, precision=None, from_repost=False):
	"""Drop-in replacement for ERPNext distribute_gl with exact IRR splits.

	IRR residual legs are excluded from allocation and keep the residual destination
	masters (Stock Adjustment for inventory valuation; Round Off for financial).
	Never first allocation child / dimension fallback for residual cost center.
	"""
	from erpnext.accounts.general_ledger import (
		get_cost_center_allocation_data,
		validate_expense_against_budget,
	)

	if not gl_map:
		return []

	company = gl_map[0].company
	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	if not is_irr_company(company):
		return _original_distribute_gl_based_on_cost_center_allocation()(
			gl_map, precision=precision, from_repost=from_repost
		)

	if not precision:
		precision = get_currency_precision(company_currency)

	round_off_account = frappe.get_cached_value("Company", company, "round_off_account")
	stock_adjustment_account = frappe.get_cached_value(
		"Company", company, "stock_adjustment_account"
	)

	new_gl_map = []
	for d in gl_map:
		# Inventory valuation residual: never allocate; keep Stock Adjustment masters.
		# Must run before generic residual detection — inventory marker alone would
		# otherwise match is_irr_rate_rounding_residual_gl and be restamped onto
		# Financial Round Off (v5.5.45 policy violation).
		if is_irr_inventory_valuation_residual_gl(
			d, company=company, stock_adjustment_account=stock_adjustment_account
		):
			stamp_irr_inventory_valuation_residual_masters(d, company)
			new_gl_map.append(d)
			continue

		# Financial IRR Round Off residual: never allocate; Company Round Off masters.
		if is_irr_rate_rounding_residual_gl(d, company=company, round_off_account=round_off_account):
			stamp_irr_residual_round_off_masters(d, company)
			assert_irr_residual_round_off_masters(d, company)
			new_gl_map.append(d)
			continue

		cost_center = d.get("cost_center")
		cost_center_allocation = get_cost_center_allocation_data(
			company, gl_map[0]["posting_date"], cost_center
		)
		if not cost_center_allocation:
			new_gl_map.append(d)
			continue

		if not from_repost:
			validate_expense_against_budget(
				d, expense_amount=flt(d.debit, precision) - flt(d.credit, precision)
			)

		# Ordinary (non-IRR-residual) Round Off: ERPNext behaviour — single child, no split.
		if d.account == round_off_account:
			d.cost_center = cost_center_allocation[0][0]
			new_gl_map.append(d)
			continue

		gle_list = []
		for sub_cost_center, percentage in cost_center_allocation:
			gle = copy.deepcopy(d)
			gle.cost_center = sub_cost_center
			for field in _SPLIT_FIELDS:
				gle[field] = flt(flt(d.get(field)) * percentage / 100, precision)
			gle_list.append(gle)

		absorb_irr_cost_center_split_residual(gle_list, d, precision)
		new_gl_map.extend(gle_list)

	return new_gl_map
