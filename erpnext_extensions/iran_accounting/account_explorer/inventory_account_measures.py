# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Inventory Account attribution helper: stock valuation by inventory account from SLE.

Case A Account Levels (and legacy inventory_account helpers) share this path:

  scoped SLE (Item / Item Group / Warehouse)
  → Warehouse
  → ERPNext warehouse inventory account map
  → aggregate stock value by inventory account

Opening is rolled into Inward (same contract as Item / Item Group).
Identity: scoped stock signed value = Σ mapped Account + unmapped residual.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from erpnext.stock import get_warehouse_account_map
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec
from erpnext_extensions.iran_accounting.account_explorer.stock_measures import stock_row_from_buckets
from erpnext_extensions.iran_accounting.account_explorer.stock_opening import (
	_aggregate_stock_buckets,
)

__all__ = [
	"InventoryAccountAttribution",
	"get_inventory_account_attribution",
	"resolve_scoped_inventory_accounts",
]


@dataclass
class InventoryAccountAttribution:
	"""Per inventory-account stock measures + unmapped warehouse residual."""

	rows_by_account: dict[str, dict] = field(default_factory=dict)
	warehouses_by_account: dict[str, list[str]] = field(default_factory=dict)
	unmapped_warehouses: list[dict] = field(default_factory=list)
	unmapped_signed_value: float = 0.0
	attributed_inward: float = 0.0
	attributed_outward: float = 0.0
	attributed_signed_balance: float = 0.0


def resolve_scoped_inventory_accounts(spec: AccountExplorerQuerySpec) -> list[str]:
	attr = get_inventory_account_attribution(spec)
	return sorted(attr.rows_by_account.keys())


def get_inventory_account_attribution(
	spec: AccountExplorerQuerySpec, account_names: list[str] | None = None
) -> InventoryAccountAttribution:
	"""Attribute scoped SLE stock value to warehouse inventory accounts."""
	opening, period = _aggregate_stock_buckets(
		spec, group_col="warehouse", key_name="warehouse", join_item=True
	)
	wh_map = get_warehouse_account_map(spec.company)
	allowed = set(account_names) if account_names is not None else None

	buckets: dict[str, dict[str, float]] = defaultdict(
		lambda: {"opening_value": 0.0, "inward_value": 0.0, "outward_value": 0.0}
	)
	warehouses_by_account: dict[str, list[str]] = defaultdict(list)
	unmapped: list[dict] = []
	unmapped_signed = 0.0

	warehouses = set(opening.keys()) | set(period.keys())
	for warehouse in warehouses:
		op = opening.get(warehouse) or {}
		pe = period.get(warehouse) or {}
		opening_value = flt(op.get("opening_value"))
		inward_value = flt(pe.get("inward_value"))
		outward_value = flt(pe.get("outward_value"))
		signed = opening_value + inward_value - outward_value

		info = wh_map.get(warehouse)
		account = info.account if info else None
		if not account:
			unmapped.append(
				{
					"warehouse": warehouse,
					"opening_value": opening_value,
					"inward_value": inward_value,
					"outward_value": outward_value,
					"signed_balance": signed,
				}
			)
			unmapped_signed += signed
			continue
		if allowed is not None and account not in allowed:
			continue

		b = buckets[account]
		b["opening_value"] += opening_value
		b["inward_value"] += inward_value
		b["outward_value"] += outward_value
		warehouses_by_account[account].append(warehouse)

	rows: dict[str, dict] = {}
	attributed_inward = 0.0
	attributed_outward = 0.0
	attributed_signed = 0.0
	targets = allowed if allowed is not None else set(buckets.keys())
	for account in targets:
		b = buckets.get(account) or {
			"opening_value": 0.0,
			"inward_value": 0.0,
			"outward_value": 0.0,
		}
		measures = stock_row_from_buckets(
			opening_value=b["opening_value"],
			inward_value=b["inward_value"],
			outward_value=b["outward_value"],
			include_qty=False,
		)
		rows[account] = measures
		attributed_inward += flt(measures.get("inward_value"))
		attributed_outward += flt(measures.get("outward_value"))
		attributed_signed += flt(measures.get("balance_value"))

	return InventoryAccountAttribution(
		rows_by_account=rows,
		warehouses_by_account={k: sorted(set(v)) for k, v in warehouses_by_account.items()},
		unmapped_warehouses=unmapped,
		unmapped_signed_value=unmapped_signed,
		attributed_inward=attributed_inward,
		attributed_outward=attributed_outward,
		attributed_signed_balance=attributed_signed,
	)
