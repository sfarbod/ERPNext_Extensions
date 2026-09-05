# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Legacy flat inventory-account stock summary (diagnostic / export only).

v5.1.1: Account Levels under Case A (Item/Item Group) is the product Account
breakdown. This module remains for forensic helpers and export compatibility —
it is not a navigator axis.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.constants import (
	INVENTORY_ACCOUNT_SORTABLE_FIELDS,
)
from erpnext_extensions.iran_accounting.account_explorer.inventory_account_measures import (
	get_inventory_account_attribution,
)
from erpnext_extensions.iran_accounting.account_explorer.pagination import (
	paginate_stock_summary_rows,
	sort_rows,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec

INVENTORY_ACCOUNT_COLUMNS = [
	{"id": "display_code", "label": "Inventory Account", "fieldtype": "Data", "width": 140},
	{"id": "display_title", "label": "Account Name", "fieldtype": "Data", "width": 240},
	{"id": "inward_value", "label": "Inward Value", "fieldtype": "Currency", "width": 140},
	{"id": "outward_value", "label": "Outward Value", "fieldtype": "Currency", "width": 140},
	{"id": "debit_balance", "label": "Debit Balance", "fieldtype": "Currency", "width": 140},
	{"id": "credit_balance", "label": "Credit Balance", "fieldtype": "Currency", "width": 140},
]


def _account_display(account_name: str) -> tuple[str, str]:
	number = frappe.db.get_value("Account", account_name, "account_number") or ""
	title = frappe.db.get_value("Account", account_name, "account_name") or account_name
	code = str(number).strip() if number else account_name
	return code, title


def build_inventory_account_summary(spec: AccountExplorerQuerySpec) -> dict:
	"""Leaf inventory accounts with scoped SLE stock value (opening rolled into Inward)."""
	attr = get_inventory_account_attribution(spec)
	rows: list[dict] = []
	for account_name, measures in attr.rows_by_account.items():
		code, title = _account_display(account_name)
		rows.append(
			{
				"row_key": f"inventory_account:{account_name}",
				"inventory_account": account_name,
				"account": account_name,
				"display_code": code,
				"display_title": title,
				"warehouses": attr.warehouses_by_account.get(account_name) or [],
				"is_virtual_group": 0,
				"drill_down_enabled": 1,
				"is_group": 0,
				**measures,
			}
		)

	rows = sort_rows(rows, spec, INVENTORY_ACCOUNT_SORTABLE_FIELDS)
	result = paginate_stock_summary_rows(rows, spec, include_qty=False)
	warnings: list[str] = []
	if attr.unmapped_warehouses:
		warnings.append(
			_(
				"Inventory Account: {0} warehouse(s) have stock value but no resolvable "
				"inventory account (signed residual {1}). Set Warehouse Account or "
				"Company Default Inventory Account."
			).format(len(attr.unmapped_warehouses), flt(attr.unmapped_signed_value))
		)
	result["warnings"] = warnings
	result["unmapped_warehouses"] = attr.unmapped_warehouses
	result["unmapped_signed_value"] = attr.unmapped_signed_value
	result["axis_subtitle"] = _("Stock value by inventory account")
	return result
