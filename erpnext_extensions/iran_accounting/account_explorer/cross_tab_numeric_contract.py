# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Cross-tab numeric comparison matrix for Account Explorer (v5.1.1).

Final asymmetric product contract (FROZEN):

CASE A — source / filter is Item or Item Group (stock population authoritative):
  Item Group = Σ Item = Σ Account Level breakdown
  for the SAME scoped SLE → Warehouse → inventory-account population.
  Directions Item|Item Group → Account: EQUAL (Δ=0 on stock-value measures).

CASE B — source / filter is Account (ledger population authoritative):
  Account → Item / Item Group is discovery / reconciliation only.
  Σ Item / Item Group may be <, =, or 0 vs Account.
  Reverse equality is NOT mandatory and must NOT be encoded as EQUAL.

Vocabulary (strict):
  EQUAL              — same measure family; values must match exactly (Δ=0)
  RECONCILABLE       — membership / population relationship; not numeric equality
  NOT COMPARABLE     — different source / semantics; no equality expected
"""

from __future__ import annotations

MEASURES = (
	"opening_value",
	"period_inward_value",
	"period_outward_value",
	"displayed_inward_value",
	"displayed_outward_value",
	"signed_closing_value",
	"debit_balance",
	"credit_balance",
	"period_debit",
	"period_credit",
	"opening_debit",
	"opening_credit",
)

STOCK_AXES = ("item_group", "item")
ACCOUNT_AXIS = "account_level"
GL_AXES = ("account_level", "voucher", "party", "dimension", "currency")

# Case A Account breakdown preserves SLE gross (not posted-GL merge).
SAME_ACCOUNT_TRANSFER_CONTRACT = "SLE_GROSS_STOCK_VALUE"

# Explicit relation constants for directionality tests.
EQUAL = "EQUAL"
RECONCILABLE = "RECONCILABLE"
NOT_COMPARABLE = "NOT COMPARABLE"

CROSS_TAB_MATRIX: dict[tuple[str, str, str], str] = {}

# Stock peers (Item ↔ Item Group): EQUAL both ways on stock measures.
for _m in (
	"opening_value",
	"period_inward_value",
	"period_outward_value",
	"displayed_inward_value",
	"displayed_outward_value",
	"signed_closing_value",
	"debit_balance",
	"credit_balance",
):
	for _a in STOCK_AXES:
		for _b in STOCK_AXES:
			CROSS_TAB_MATRIX[(_a, _b, _m)] = EQUAL
		for _g in GL_AXES:
			if _g == ACCOUNT_AXIS:
				continue
			CROSS_TAB_MATRIX[(_a, _g, _m)] = NOT_COMPARABLE
			CROSS_TAB_MATRIX[(_g, _a, _m)] = NOT_COMPARABLE

# ---------------------------------------------------------------------------
# CASE A — Item / Item Group → Account: EQUAL (asymmetric; one direction only)
# ---------------------------------------------------------------------------
_CASE_A_EQUAL_MEASURES = (
	"opening_value",
	"period_inward_value",
	"period_outward_value",
	"displayed_inward_value",
	"displayed_outward_value",
	"signed_closing_value",
	"debit_balance",
	"credit_balance",
	"period_debit",
	"period_credit",
)
for _m in _CASE_A_EQUAL_MEASURES:
	CROSS_TAB_MATRIX[("item_group", ACCOUNT_AXIS, _m)] = EQUAL
	CROSS_TAB_MATRIX[("item", ACCOUNT_AXIS, _m)] = EQUAL

# ---------------------------------------------------------------------------
# CASE B — Account → Item / Item Group: discovery only (NOT EQUAL)
# ---------------------------------------------------------------------------
for _m in MEASURES:
	CROSS_TAB_MATRIX[(ACCOUNT_AXIS, "item_group", _m)] = RECONCILABLE
	CROSS_TAB_MATRIX[(ACCOUNT_AXIS, "item", _m)] = RECONCILABLE

# Voucher under stock filters: related documents, no stock-value equality.
for _a in STOCK_AXES:
	CROSS_TAB_MATRIX[(_a, "voucher", "period_debit")] = NOT_COMPARABLE
	CROSS_TAB_MATRIX[(_a, "voucher", "period_credit")] = NOT_COMPARABLE
	CROSS_TAB_MATRIX[("voucher", _a, "period_debit")] = NOT_COMPARABLE
	CROSS_TAB_MATRIX[("voucher", _a, "period_credit")] = NOT_COMPARABLE

USER_COMPARISON_GUIDANCE = {
	"case_a_stock_to_account": (
		"With Item / Item Group / Warehouse filters: Account Levels are an "
		"SLE-scoped stock breakdown (warehouse → inventory account). "
		"Item Group = Σ Item = Σ Account for Opening / Inward / Outward / "
		"Balance (Δ=0). Same-account transfers keep SLE gross In+Out."
	),
	"case_b_account_to_stock": (
		"When Account is the starting axis (no Item/Item Group stock filter): "
		"Account is posted General Ledger. Related Items / Item Groups are "
		"discovery only — reverse equality is NOT required."
	),
	"stock_peers": (
		"Item Group ↔ Item — Opening / Period In / Period Out / Displayed "
		"Inward / Displayed Outward / Signed Closing are EQUAL."
	),
	"same_account_transfer": (
		"Same-account warehouse transfers contribute SLE stock value gross "
		"(Inward + Outward) on the attributed inventory account — not posted "
		"GL net-to-zero."
	),
}


def relation(axis_a: str, axis_b: str, measure: str) -> str:
	"""Lookup cross-tab relation; default NOT COMPARABLE."""
	return CROSS_TAB_MATRIX.get((axis_a, axis_b, measure), NOT_COMPARABLE)


def is_case_a_equal(source_axis: str, target_axis: str, measure: str) -> bool:
	"""True only for Case A stock → Account EQUAL cells."""
	return (
		source_axis in STOCK_AXES
		and target_axis == ACCOUNT_AXIS
		and relation(source_axis, target_axis, measure) == EQUAL
	)


def is_case_b_discovery(source_axis: str, target_axis: str) -> bool:
	"""True when Account → stock axes (no reverse equality)."""
	return source_axis == ACCOUNT_AXIS and target_axis in STOCK_AXES
