# Copyright (c) 2026, ERPNext Extensions contributors
"""Scrap / Reject / Waste warehouse helpers for Historical Repair (v5.3.0).

Warehouse Scrap/Reject/Waste is **contextual information only**.

v5.3.0 purpose-first rule: TRANSACTION SEMANTICS + AUTHORITATIVE SOURCE
determine Zero Rate repairability. Scrap/Reject warehouse name alone must
NOT exempt a zero-rate row from classification (especially Material Receipt).

``is_legitimate_scrap_zero_rate`` is retained for backward compatibility but
is no longer used as the primary Zero Rate decision branch.
"""

from __future__ import annotations

from functools import lru_cache

import frappe

from erpnext_extensions.iran_accounting.scrap_costing import (
	is_product_reject,
	is_scrap_row,
	secondary_item_type_of,
)

# Name tokens observed on this company + common English aliases.
# Matching is case-insensitive substring on Warehouse.name / warehouse_name.
SCRAP_WAREHOUSE_TOKENS = (
	"reject",
	"scrap",
	"waste",
	"ضایعات",  # waste/scrap
	"اسقاط",  # scrap/write-off
	"مرجوع",  # return/reject
)

# Explicit valuation roles (Rule 2) — do not merge these cases.
SCRAP_ROLE_ZERO_VALUED_WASTE = "ZERO_VALUED_WASTE_RECEIPT"
SCRAP_ROLE_INPUT_MATERIAL = "INPUT_MATERIAL_SCRAP"
SCRAP_ROLE_MANUFACTURE_BYPRODUCT = "MANUFACTURE_SCRAP_BYPRODUCT"
SCRAP_ROLE_CORRUPTED = "CORRUPTED_SCRAP_VALUATION"
SCRAP_ROLE_NOT_SCRAP = "NOT_SCRAP"

LEGITIMATE_SCRAP_ZERO_RATE = "LEGITIMATE_SCRAP_ZERO_RATE"
NO_ACTION_REQUIRED = "NO_ACTION_REQUIRED"

# Precise zero-rate reasons (Rule 6)
TRUE_ZERO_RATE_CORRUPTION = "TRUE_ZERO_RATE_CORRUPTION"
MISSING_SOURCE_RATE = "MISSING_SOURCE_RATE"
UPSTREAM_POISONED = "UPSTREAM_POISONED"
MANUFACTURE_DEPENDENCY = "MANUFACTURE_DEPENDENCY"
POSTING_ORDER_DEPENDENCY = "POSTING_ORDER_DEPENDENCY"
CONVERSION_ARTIFACT = "CONVERSION_ARTIFACT"
MANUAL_REVIEW_REASON = "MANUAL_REVIEW"


def _norm(text: str | None) -> str:
	return (text or "").strip().lower()


def warehouse_matches_scrap_reject_waste(warehouse: str | None) -> bool:
	"""True when the warehouse *name* indicates Scrap/Reject/Waste."""
	name = _norm(warehouse)
	if not name:
		return False
	return any(token in name for token in SCRAP_WAREHOUSE_TOKENS)


@lru_cache(maxsize=4)
def list_scrap_reject_waste_warehouses(company: str | None = None) -> tuple[str, ...]:
	"""Discover designated scrap/reject/waste warehouses from master data."""
	conds = ["IFNULL(disabled,0)=0", "IFNULL(is_group,0)=0"]
	args: list = []
	if company:
		conds.append("company=%s")
		args.append(company)
	token_or = " OR ".join(
		["LOWER(name) LIKE %s", "LOWER(IFNULL(warehouse_name,'')) LIKE %s"] * len(SCRAP_WAREHOUSE_TOKENS)
	)
	# Build LIKE args once per token for name + warehouse_name
	like_args: list = []
	like_clauses = []
	for token in SCRAP_WAREHOUSE_TOKENS:
		like_clauses.append("LOWER(name) LIKE %s")
		like_args.append(f"%{token.lower()}%")
		like_clauses.append("LOWER(IFNULL(warehouse_name,'')) LIKE %s")
		like_args.append(f"%{token.lower()}%")
	rows = frappe.db.sql(
		f"""
		SELECT name
		FROM `tabWarehouse`
		WHERE {" AND ".join(conds)}
		  AND ({" OR ".join(like_clauses)})
		ORDER BY name
		""",
		args + like_args,
		as_dict=True,
	)
	# Also include warehouses that historically receive scrap/secondary Scrap rows
	# even if the name token is missing (usage-backed discovery).
	usage = frappe.db.sql(
		"""
		SELECT DISTINCT sed.t_warehouse AS name
		FROM `tabStock Entry Detail` sed
		JOIN `tabStock Entry` se ON se.name = sed.parent
		WHERE se.docstatus=1
		  AND IFNULL(sed.t_warehouse,'')!=''
		  AND IFNULL(sed.s_warehouse,'')=''
		  AND (
		        IFNULL(sed.secondary_item_type,'')='Scrap'
		     OR IFNULL(sed.is_scrap_item,0)=1
		  )
		""",
		as_dict=True,
	)
	names = {r.name for r in rows if r.name}
	for r in usage:
		if warehouse_matches_scrap_reject_waste(r.name):
			names.add(r.name)
	return tuple(sorted(names))


def is_scrap_reject_waste_warehouse(warehouse: str | None, *, company: str | None = None) -> bool:
	if not warehouse:
		return False
	if warehouse_matches_scrap_reject_waste(warehouse):
		return True
	try:
		return warehouse in set(list_scrap_reject_waste_warehouses(company))
	except Exception:
		return warehouse_matches_scrap_reject_waste(warehouse)


def scrap_valuation_role(row, finished_item: str | None = None) -> str:
	"""Rule 2 — distinguish scrap valuation semantics. Never merge cases."""
	if not is_scrap_row(row) and not (
		secondary_item_type_of(row) == "Scrap" or row.get("is_scrap_item")
	):
		# Incoming to scrap warehouse without scrap markers still counts as waste receipt.
		if is_scrap_reject_waste_warehouse(row.get("t_warehouse")):
			return SCRAP_ROLE_ZERO_VALUED_WASTE
		return SCRAP_ROLE_NOT_SCRAP

	fg = finished_item
	if fg is None and row.get("parent"):
		try:
			fg = frappe.db.get_value(
				"Stock Entry Detail",
				{"parent": row.get("parent"), "is_finished_item": 1},
				"item_code",
			)
		except Exception:
			fg = None

	if is_product_reject(row, fg):
		return SCRAP_ROLE_MANUFACTURE_BYPRODUCT

	# Component / input-material scrap — retain source/input valuation.
	if row.get("s_warehouse") or (row.get("item_code") and row.get("item_code") != fg):
		return SCRAP_ROLE_INPUT_MATERIAL

	return SCRAP_ROLE_MANUFACTURE_BYPRODUCT


def is_legitimate_scrap_zero_rate(row, *, company: str | None = None) -> bool:
	"""Rule 1 — zero rate into Scrap/Reject/Waste warehouse is not corruption by itself.

	Requires: incoming (t_warehouse set, no s_warehouse), target is scrap/reject/waste,
	and absolute rate ≈ 0. Does not grant a global "all scrap is zero" pass.
	"""
	from frappe.utils import flt

	from erpnext_extensions.iran_accounting.historical_stock import RATE_EPS

	t_wh = row.get("t_warehouse")
	s_wh = row.get("s_warehouse")
	if not t_wh or s_wh:
		return False
	if not is_scrap_reject_waste_warehouse(t_wh, company=company):
		return False
	basic = abs(flt(row.get("basic_rate")))
	val = abs(flt(row.get("valuation_rate")))
	return basic <= RATE_EPS and val <= RATE_EPS


def clear_scrap_warehouse_cache() -> None:
	list_scrap_reject_waste_warehouses.cache_clear()
