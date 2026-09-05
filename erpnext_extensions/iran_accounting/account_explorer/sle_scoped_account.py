# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""SLE-scoped Account measures under Item / Item Group / Warehouse filters.

Final Case A contract (v5.1.1, frozen):
  Item Group = Σ Item = Σ Account Level breakdown
  for the SAME scoped SLE → warehouse → inventory-account population.
  Directions Item|Item Group → Account: EQUAL (Δ=0).

  Account → Item / Item Group (Case B): discovery only — not this module.

Engine id: ``sle_scoped_stock`` (canonical Case A Account measure engine).

Not voucher-wide posted GL. Not construction replay. Not E3 scoped GL amounts.
Multi-item vouchers: only scoped-item SLE stock_value contributes.
"""

from __future__ import annotations

from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.inventory_account_measures import (
	get_inventory_account_attribution,
)
from erpnext_extensions.iran_accounting.account_explorer.inventory_scope import (
	has_inventory_document_filters,
)
from erpnext_extensions.iran_accounting.account_explorer.measures import measures_from_opening_period
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec

ACCOUNT_FACT_ENGINE_SLE_SCOPED = "sle_scoped_stock"
ACCOUNT_FACT_ENGINE_POSTED = "posted_gl"


def select_sle_scoped_account_engine(spec: AccountExplorerQuerySpec) -> bool:
	return has_inventory_document_filters(spec)


def select_account_fact_engine(spec: AccountExplorerQuerySpec) -> str:
	"""Canonical Account summary engine label for responses / fingerprints.

	- Inventory filters (Item / Item Group / Warehouse) → ``sle_scoped_stock``
	- Otherwise → ``posted_gl`` (E1/E2/E3 measure path)

	Never returns voucher_scoped_gl or stock_construction_replay for Account summary.
	"""
	if select_sle_scoped_account_engine(spec):
		return ACCOUNT_FACT_ENGINE_SLE_SCOPED
	return ACCOUNT_FACT_ENGINE_POSTED


def aggregate_sle_scoped_account_measures(
	spec: AccountExplorerQuerySpec, account_names: list[str] | None = None
) -> dict[str, dict]:
	"""Map scoped SLE stock value onto inventory accounts in Account measure shape.

	Display contract mirrors Item Group:
	  period_debit  = displayed inward (opening rolled into inward)
	  period_credit = period outward
	  opening_*     = 0  (opening already in period_debit, same as IG columns)
	  debit_balance / credit_balance = side presentation of signed closing
	"""
	attr = get_inventory_account_attribution(spec, account_names)
	result: dict[str, dict] = {}
	for account, stock in attr.rows_by_account.items():
		period_debit = flt(stock.get("inward_value"))
		period_credit = flt(stock.get("outward_value"))
		if not period_debit and not period_credit and not flt(stock.get("balance_value")):
			continue
		result[account] = measures_from_opening_period(0.0, 0.0, period_debit, period_credit)
	return result


def sle_scoped_meta(spec: AccountExplorerQuerySpec) -> dict:
	attr = get_inventory_account_attribution(spec)
	return {
		"account_fact_engine": ACCOUNT_FACT_ENGINE_SLE_SCOPED,
		"case_a_stock_breakdown": 1,
		"sle_scoped_attributed_inward": attr.attributed_inward,
		"sle_scoped_attributed_outward": attr.attributed_outward,
		"sle_scoped_attributed_balance": attr.attributed_signed_balance,
		"sle_scoped_unmapped_warehouses": len(attr.unmapped_warehouses),
		"sle_scoped_unmapped_signed_value": attr.unmapped_signed_value,
		# Identity: IG signed = Σ mapped Account signed + unmapped signed
		"sle_scoped_mapped_plus_unmapped_balance": (
			flt(attr.attributed_signed_balance) + flt(attr.unmapped_signed_value)
		),
	}
