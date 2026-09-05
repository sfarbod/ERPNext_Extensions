# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Filter × axis compatibility matrix for Account Explorer (v5.1.1).

Final asymmetric product contract (FROZEN):

CASE A — filter source is Item / Item Group / Warehouse:
  - Item / Item Group: SLE stock measures (shared bucket function).
  - Account Levels: SAME scoped SLE → warehouse → inventory account
    breakdown. Item|Item Group → Account: EQUAL (Δ=0).
  - Party / Dimension / Currency / Voucher: posted tabGL of vouchers with
    scoped SLE (EXISTS). No stock-value equality promise.

CASE B — filter / starting axis is Account (no stock Item/IG filter):
  - Account is posted General Ledger (E1/E2/E3).
  - Account → Item / Item Group: discovery only; reverse equality NOT required.

No Inventory Account nav tab.
No voucher-wide GL as Case A Account measures.
No construction-replay Account summary.
"""

from __future__ import annotations

STOCK_FAMILY_AXES = frozenset({"item", "item_group"})

GL_FAMILY_AXES = frozenset(
	{
		"account_level",
		"party",
		"unified_party",
		"dimension",
		"currency",
		"voucher",
	}
)

INVENTORY_FILTER_KEYS = frozenset({"item", "item_group", "warehouse"})

GL_FILTER_KEYS = frozenset(
	{
		"account",
		"party",
		"unified_party",
		"currency",
		"voucher",
		"dimension",
	}
)

_CASE_A_ACCOUNT_BEHAVIOR = (
	"CASE A: Account Levels = SLE-scoped stock breakdown "
	"(scoped SLE → warehouse → ERPNext inventory account). "
	"Same stock population as Item / Item Group. "
	"Item Group → Account EQUAL (Δ=0). Item → Account EQUAL (Δ=0). "
	"Does not include other Items/Groups on the same account, JE, or "
	"company-wide GL. Same-account transfers keep SLE gross Inward+Outward."
)

_CASE_B_ACCOUNT_BEHAVIOR = (
	"CASE B: When Account is the starting axis without Item/Item Group "
	"filters, measures are posted General Ledger. Related Items / Item "
	"Groups are discovery only — reverse equality is NOT mandatory."
)

_VOUCHER_SCOPED_GL_BEHAVIOR = (
	"Party / Dimension / Currency / Voucher: REAL tabGL rows whose "
	"(voucher_type, voucher_no) appear in the scoped SLE population "
	"(SQL EXISTS). No stock-value equality vs Item Group."
)

FILTER_AXIS_COMPATIBILITY = {
	"item": {
		"affects": sorted(STOCK_FAMILY_AXES | GL_FAMILY_AXES),
		"no_effect": [],
		"behavior": (
			"Restricts SLE population by item_code. "
			"Item / Item Group re-aggregate scoped stock value. "
			+ _CASE_A_ACCOUNT_BEHAVIOR
			+ " "
			+ _VOUCHER_SCOPED_GL_BEHAVIOR
		),
		"account_relation": "EQUAL",
	},
	"item_group": {
		"affects": sorted(STOCK_FAMILY_AXES | GL_FAMILY_AXES),
		"no_effect": [],
		"behavior": (
			"Expands to leaf item groups → item codes → SLE. "
			"Parent groups are filter-only; Item Group axis rows remain leaf-only. "
			+ _CASE_A_ACCOUNT_BEHAVIOR
			+ " "
			+ _VOUCHER_SCOPED_GL_BEHAVIOR
		),
		"account_relation": "EQUAL",
	},
	"warehouse": {
		"affects": sorted(STOCK_FAMILY_AXES | GL_FAMILY_AXES),
		"no_effect": [],
		"behavior": (
			"Restricts SLE by warehouse (not inferred from GL). "
			+ _CASE_A_ACCOUNT_BEHAVIOR
			+ " "
			+ _VOUCHER_SCOPED_GL_BEHAVIOR
		),
		"account_relation": "EQUAL",
	},
	"account": {
		"affects": sorted(GL_FAMILY_AXES | STOCK_FAMILY_AXES),
		"no_effect": [],
		"behavior": (
			_CASE_B_ACCOUNT_BEHAVIOR
			+ " Scopes GL axes by account tree. On stock axes, may restrict "
			"SLE to vouchers that post to the selected account (discovery)."
		),
		"stock_relation": "RECONCILABLE",
	},
	"party": {
		"affects": sorted(GL_FAMILY_AXES | STOCK_FAMILY_AXES),
		"no_effect": [],
		"behavior": "Scopes GL party measures; may cross-filter SLE via voucher EXISTS.",
	},
	"dimension": {
		"affects": sorted(GL_FAMILY_AXES | STOCK_FAMILY_AXES),
		"no_effect": [],
		"behavior": "Scopes GL dimension measures; may cross-filter SLE via voucher EXISTS.",
	},
	"currency": {
		"affects": sorted(GL_FAMILY_AXES | STOCK_FAMILY_AXES),
		"no_effect": [],
		"behavior": "Scopes GL currency framing; may cross-filter SLE via voucher EXISTS.",
	},
	"voucher": {
		"affects": sorted(GL_FAMILY_AXES | STOCK_FAMILY_AXES),
		"no_effect": [],
		"behavior": "Scopes GL voucher measures; may cross-filter SLE via voucher EXISTS.",
	},
}


def inventory_filters_affect_axis(view_axis: str) -> bool:
	"""True when Item / Item Group / Warehouse change that axis's row population."""
	return view_axis in STOCK_FAMILY_AXES or view_axis in GL_FAMILY_AXES


def inventory_filters_ignored_on_axis(view_axis: str) -> bool:
	"""Deprecated alias: inventory filters are never ignored on published axes."""
	return False


def inventory_filters_voucher_scope_gl_axis(view_axis: str) -> bool:
	"""True when inventory filters narrow this axis via SLE→voucher→GL EXISTS.

	Account Levels use SLE-scoped stock attribution instead (Case A).
	"""
	return view_axis in GL_FAMILY_AXES and view_axis != "account_level"


def inventory_filters_construction_account_axis(view_axis: str) -> bool:
	"""Deprecated: construction replay is not the Account summary engine."""
	return False


def inventory_filters_sle_scoped_account_axis(view_axis: str) -> bool:
	return view_axis == "account_level"
