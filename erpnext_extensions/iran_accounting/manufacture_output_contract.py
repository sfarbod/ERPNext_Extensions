# Copyright (c) 2026, ERPNext Extensions contributors
"""Unified Iran Manufacture output accounting contract (Phase 1).

ONE contract spine + MULTIPLE allocation strategies.

Phase 1 introduces:
- Normalized AllocationPlan / NormalizedOutputRow
- Common finalizer + verifier
- Closed-plan protection (economics authoritative; rates representational)
- SAME_ITEM_MULTI_FG strategy (warehouse-split same item MAIN_FG)

Existing STAGE_CO / Product Reject / By-Product paths keep their allocators;
they participate in ownership/closed-plan semantics so later rewriters cannot
reopen strategy-owned economics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.domain.currency import (
	amount_rate_qty_residual,
	integer_valuation_rate_from_amount,
	round_monetary_rate,
)
from erpnext_extensions.iran_accounting.rounding import (
	get_company_currency,
	is_irr_company,
	round_currency,
)
from erpnext_extensions.iran_accounting.scrap_costing import (
	CLASS_BULK_SCRAP,
	CLASS_COMPONENT_SCRAP,
	CLASS_CO_PRODUCT,
	CLASS_CO_PRODUCT_REJECT,
	CLASS_MAIN_FG,
	CLASS_MAIN_PRODUCT_REJECT,
	_is_incoming,
	_row_qty,
	classify_manufacture_outputs,
)

# ---------------------------------------------------------------------------
# Strategy / residual taxonomy
# ---------------------------------------------------------------------------

STRATEGY_STAGE_CO = "STAGE_CO"
STRATEGY_PRODUCT_REJECT = "PRODUCT_REJECT"
STRATEGY_SAME_ITEM_MULTI_FG = "SAME_ITEM_MULTI_FG"
STRATEGY_PLAIN_SINGLE_FG = "PLAIN_SINGLE_FG"
STRATEGY_INDEPENDENT_BY = "INDEPENDENT_BY"
STRATEGY_COMPONENT_SCRAP = "COMPONENT_SCRAP"

R1_ECONOMIC_ALLOCATION_GAP = "R1_ECONOMIC_ALLOCATION_GAP"
R2_IRR_RATE_REPRESENTATION = "R2_IRR_RATE_REPRESENTATION"
R3_LEGITIMATE_BUSINESS_RESIDUAL = "R3_LEGITIMATE_BUSINESS_RESIDUAL"
R4_CORRUPTION_UNEXPLAINED = "R4_CORRUPTION_UNEXPLAINED"

# Legacy TYPE A/B/C → R1–R4 compatibility mapping (verifier only; no rewrite).
LEGACY_TYPE_TO_RESIDUAL = {
	"TYPE_A_REAL_ADDITIONAL_COST": R3_LEGITIMATE_BUSINESS_RESIDUAL,
	"TYPE_B_ROUNDING_RESIDUAL": R2_IRR_RATE_REPRESENTATION,
	"TYPE_C_MULTI_OUTPUT_ALLOCATION_RESIDUAL": R3_LEGITIMATE_BUSINESS_RESIDUAL,
}

ALLOCATION_OWNER_ATTR = "_iran_manufacture_allocation_owner"
ALLOCATION_CLOSED_ATTR = "_iran_allocation_closed"
ALLOCATION_PLAN_ATTR = "_iran_allocation_plan"
OWNED_ROW_IDS_ATTR = "_iran_allocation_owned_row_ids"


@dataclass
class NormalizedOutputRow:
	row_ref: Any
	classification: str
	allocation_basis: float
	allocation_owner: str
	qty: float
	material_amount: float = 0.0
	operating_amount: float = 0.0
	lcv_amount: float = 0.0
	final_amount: float = 0.0
	basic_rate: float = 0.0
	valuation_rate: float = 0.0
	basic_rate_amount_residual: float = 0.0
	valuation_rate_amount_residual: float = 0.0
	flags: dict = field(default_factory=dict)


@dataclass
class AllocationPlan:
	strategy_id: str
	outgoing_material: float = 0.0
	independent_output_value: float = 0.0
	allocatable_material_pool: float = 0.0
	operating_pool: float = 0.0
	lcv_pool: float = 0.0
	rows: list[NormalizedOutputRow] = field(default_factory=list)
	economic_target: float = 0.0
	final_incoming_value: float = 0.0
	economic_residual: float = 0.0
	residual_class: str | None = None
	closed: bool = False
	preserved_manual: bool = False
	notes: list[str] = field(default_factory=list)

	@property
	def pools(self) -> dict[str, float]:
		return {
			"outgoing_material": self.outgoing_material,
			"independent_output_value": self.independent_output_value,
			"allocatable_material_pool": self.allocatable_material_pool,
			"operating_pool": self.operating_pool,
			"lcv_pool": self.lcv_pool,
		}


# ---------------------------------------------------------------------------
# Closed-plan ownership
# ---------------------------------------------------------------------------


def get_allocation_owner(doc) -> str | None:
	return getattr(doc, ALLOCATION_OWNER_ATTR, None) or None


def is_allocation_closed(doc) -> bool:
	return bool(getattr(doc, ALLOCATION_CLOSED_ATTR, False))


def get_allocation_plan(doc) -> AllocationPlan | None:
	return getattr(doc, ALLOCATION_PLAN_ATTR, None)


def owned_row_ids(doc) -> set[int]:
	raw = getattr(doc, OWNED_ROW_IDS_ATTR, None)
	return set(raw or ())


def row_is_allocation_owned(doc, row) -> bool:
	"""True when a closed plan owns this row's economics."""
	if not is_allocation_closed(doc):
		return False
	ids = owned_row_ids(doc)
	if not ids:
		# Owner claimed the document; protect all incoming outputs.
		return bool(_is_incoming(row))
	return id(row) in ids


def mark_allocation_closed(
	doc,
	strategy_id: str,
	*,
	plan: AllocationPlan | None = None,
	owned_rows: list | None = None,
) -> None:
	"""Stamp exactly-one owner and close the plan (in-memory only)."""
	existing = get_allocation_owner(doc)
	if existing and existing != strategy_id:
		frappe.throw(
			_(
				"Manufacture allocation ownership conflict: {0} already owns this document; "
				"{1} cannot also claim it. Exactly one allocation strategy may own a document."
			).format(existing, strategy_id),
			frappe.ValidationError,
		)
	setattr(doc, ALLOCATION_OWNER_ATTR, strategy_id)
	setattr(doc, ALLOCATION_CLOSED_ATTR, True)
	if plan is not None:
		plan.closed = True
		plan.strategy_id = strategy_id
		setattr(doc, ALLOCATION_PLAN_ATTR, plan)
	if owned_rows is not None:
		setattr(doc, OWNED_ROW_IDS_ATTR, {id(r) for r in owned_rows})
	elif plan is not None:
		setattr(doc, OWNED_ROW_IDS_ATTR, {id(r.row_ref) for r in plan.rows if r.row_ref is not None})


def claim_stage_co_ownership(doc) -> None:
	"""STAGE_CO allocator succeeded — close plan so residual aligners cannot reopen."""
	classified = classify_manufacture_outputs(doc)
	owned = list(classified[CLASS_MAIN_FG]) + list(classified[CLASS_MAIN_PRODUCT_REJECT])
	from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
		stage_participating_co_product_rows,
	)

	owned.extend(stage_participating_co_product_rows(doc))
	mark_allocation_closed(doc, STRATEGY_STAGE_CO, owned_rows=owned)


def claim_product_reject_ownership(doc) -> None:
	classified = classify_manufacture_outputs(doc)
	owned = list(classified[CLASS_MAIN_FG]) + list(classified[CLASS_MAIN_PRODUCT_REJECT])
	mark_allocation_closed(doc, STRATEGY_PRODUCT_REJECT, owned_rows=owned)


# ---------------------------------------------------------------------------
# Economic pools (common equations)
# ---------------------------------------------------------------------------


def _is_finance_excluded_output(row) -> bool:
	from erpnext_extensions.iran_accounting.manufacture_stage_costing import _is_finance_excluded

	return _is_finance_excluded(row)


def build_economic_pools(doc, classified: dict[str, list] | None = None) -> dict[str, float]:
	"""Common pool equations shared by strategies."""
	classified = classified or classify_manufacture_outputs(doc)
	currency = get_company_currency(doc.company)

	outgoing_material = round_currency(
		sum(flt(r.get("basic_amount")) for r in (doc.get("items") or []) if r.get("s_warehouse")),
		currency,
	)

	finance_excluded = [
		row
		for row in classified[CLASS_CO_PRODUCT] + classified[CLASS_CO_PRODUCT_REJECT]
		if _is_finance_excluded_output(row)
	]
	independent = (
		sum(flt(r.get("basic_amount")) for r in classified[CLASS_COMPONENT_SCRAP])
		+ sum(flt(r.get("basic_amount")) for r in classified.get(CLASS_BULK_SCRAP, []))
		+ sum(flt(r.get("basic_amount")) for r in finance_excluded)
	)
	independent = round_currency(independent, currency)
	allocatable = round_currency(outgoing_material - independent, currency)

	header_oh = flt(doc.get("total_additional_costs"))
	if not header_oh and doc.get("additional_costs"):
		header_oh = sum(
			flt(t.get("base_amount") if t.get("base_amount") not in (None, "") else t.get("amount"))
			for t in doc.additional_costs
		)
	excluded_oh = sum(flt(r.get("additional_cost")) for r in finance_excluded)
	operating_pool = round_currency(max(header_oh - excluded_oh, 0.0), currency)

	lcv_pool = round_currency(
		sum(flt(r.get("landed_cost_voucher_amount")) for r in (doc.get("items") or []) if _is_incoming(r)),
		currency,
	)
	return {
		"outgoing_material": outgoing_material,
		"independent_output_value": independent,
		"allocatable_material_pool": allocatable,
		"operating_pool": operating_pool,
		"lcv_pool": lcv_pool,
	}


# ---------------------------------------------------------------------------
# R2 mathematical bound (IRR integer ROUND_HALF_UP representation)
# ---------------------------------------------------------------------------


def r2_representation_bound(qty: float) -> float:
	"""Max |amount − rate×qty| for rate = ROUND_HALF_UP(amount/qty).

	For integer IRR amounts and positive qty, half-up guarantees
	|residual| ≤ qty/2.  Bound is derived from rounding semantics, not policy.
	"""
	q = abs(flt(qty))
	if q <= 0:
		return 0.0
	return q / 2.0


def assert_r2_within_bound(residual: float, qty: float, *, label: str) -> None:
	bound = r2_representation_bound(qty)
	if abs(flt(residual)) > bound + 1e-9:
		frappe.throw(
			_(
				"{0}: IRR rate representation residual {1} exceeds mathematical bound "
				"±{2} (qty={3}). Amount remains authoritative; this is not Stock Adjustment."
			).format(label, residual, bound, qty),
			frappe.ValidationError,
		)


# ---------------------------------------------------------------------------
# Strategy selection — SAME_ITEM_MULTI_FG trigger (narrow)
# ---------------------------------------------------------------------------


def _main_fg_rows(classified) -> list:
	return list(classified.get(CLASS_MAIN_FG) or [])


def _row_manual(row) -> bool:
	return bool(cint(row.get("set_basic_rate_manually")))


def detect_same_item_multi_fg_eligibility(doc) -> tuple[bool, str]:
	"""Return (eligible, reason). Never steals STAGE_CO / Product Reject docs."""
	if getattr(doc, "doctype", None) != "Stock Entry" or doc.get("purpose") != "Manufacture":
		return False, "not_manufacture"
	if not is_irr_company(doc.company):
		return False, "not_irr"

	from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
		has_stage_participating_co_product,
		uses_v533_contract,
	)

	if not uses_v533_contract(doc):
		return False, "contract_not_applicable"

	# Existing owner already claimed.
	if is_allocation_closed(doc) and get_allocation_owner(doc) != STRATEGY_SAME_ITEM_MULTI_FG:
		return False, f"owned_by_{get_allocation_owner(doc)}"

	# STAGE_CO participates → never steal.
	if has_stage_participating_co_product(doc):
		return False, "stage_co_participating"

	classified = classify_manufacture_outputs(doc)
	fg_rows = _main_fg_rows(classified)
	if len(fg_rows) < 2:
		return False, "lt_2_main_fg"

	if classified[CLASS_MAIN_PRODUCT_REJECT]:
		return False, "product_reject_unsupported"

	# Unsupported reject combination with Multi-FG.
	if classified[CLASS_CO_PRODUCT_REJECT] and not has_stage_participating_co_product(doc):
		# CO_PRODUCT_REJECT without stage ownership is ambiguous with Multi-FG.
		return False, "co_product_reject_without_stage"

	item_codes = {r.get("item_code") for r in fg_rows}
	if None in item_codes or "" in item_codes:
		return False, "missing_item_code"
	if len(item_codes) != 1:
		return False, "different_main_fg_items"

	uoms = {(r.get("stock_uom") or "").strip() or None for r in fg_rows}
	uoms.discard(None)
	if len(uoms) > 1:
		return False, "incompatible_uom"

	warehouses = {(r.get("t_warehouse") or "").strip() for r in fg_rows}
	if "" in warehouses:
		return False, "missing_destination_warehouse"
	if len(warehouses) < 2:
		return False, "same_destination_warehouse"

	for row in fg_rows:
		if _row_qty(row) <= 0:
			return False, "invalid_qty_basis"

	manual_flags = [_row_manual(r) for r in fg_rows]
	if any(manual_flags) and not all(manual_flags):
		return False, "mixed_manual_automatic"

	return True, "eligible"


def assert_multi_fg_fail_closed(doc) -> None:
	"""BLOCK ambiguous Multi-FG configurations that no strategy owns."""
	if getattr(doc, "doctype", None) != "Stock Entry" or doc.get("purpose") != "Manufacture":
		return
	if not is_irr_company(doc.company):
		return
	from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
		has_stage_participating_co_product,
		uses_v533_contract,
	)

	if not uses_v533_contract(doc):
		return
	if is_allocation_closed(doc):
		return
	if has_stage_participating_co_product(doc):
		return

	classified = classify_manufacture_outputs(doc)
	fg_rows = _main_fg_rows(classified)
	if len(fg_rows) < 2:
		return

	# Multi-FG + Product Reject
	if classified[CLASS_MAIN_PRODUCT_REJECT]:
		frappe.throw(
			_(
				"Same-item Multi-MAIN_FG Manufacture with Product Reject is not supported in "
				"this contract phase. Detected {0} MAIN_FG rows and {1} MAIN_PRODUCT_REJECT "
				"row(s). Remove the reject combination or use a supported single-FG reject path."
			).format(len(fg_rows), len(classified[CLASS_MAIN_PRODUCT_REJECT])),
			frappe.ValidationError,
		)

	item_codes = {r.get("item_code") for r in fg_rows}
	if len(item_codes) > 1:
		frappe.throw(
			_(
				"Multiple MAIN_FG rows with different item codes ({0}) have no STAGE_CO / "
				"equivalent-factor policy on this document. Economics are ambiguous — "
				"configure stage Co-Product equivalence, or use a single finished-good item."
			).format(", ".join(sorted(str(c) for c in item_codes if c))),
			frappe.ValidationError,
		)

	eligible, reason = detect_same_item_multi_fg_eligibility(doc)
	if not eligible and reason == "mixed_manual_automatic":
		frappe.throw(
			_(
				"Same-item Multi-MAIN_FG has mixed manual and automatic rates. "
				"Either set set_basic_rate_manually on all MAIN_FG rows with coherent "
				"shared economics, or leave all automatic for quantity-basis allocation."
			),
			frappe.ValidationError,
		)
	if not eligible and reason == "invalid_qty_basis":
		frappe.throw(
			_(
				"Same-item Multi-MAIN_FG is missing a valid quantity basis on at least one "
				"MAIN_FG row (qty/transfer_qty must be > 0)."
			),
			frappe.ValidationError,
		)
	if not eligible and reason == "incompatible_uom":
		frappe.throw(
			_(
				"Same-item Multi-MAIN_FG rows do not share a compatible stock UOM / quantity "
				"basis. Correct UOM alignment before valuation."
			),
			frappe.ValidationError,
		)


# ---------------------------------------------------------------------------
# Deterministic remainder allocation
# ---------------------------------------------------------------------------


def _remainder_row_index(rows: list) -> int:
	"""Largest stock qty, then stable idx, then name, then list position."""
	best_i = 0
	best_key = None
	for i, row in enumerate(rows):
		qty = _row_qty(row)
		idx = cint(row.get("idx") or 0)
		name = str(row.get("name") or "")
		key = (qty, idx, name, i)
		if best_key is None or key > best_key:
			best_key = key
			best_i = i
	return best_i


def allocate_amount_by_qty(pool: float, rows: list, currency: str) -> list[float]:
	"""Amount-first proportional allocation with deterministic remainder row."""
	if not rows:
		return []
	pool = round_currency(pool, currency)
	qtys = [_row_qty(r) for r in rows]
	total_qty = sum(qtys)
	if total_qty <= 0:
		frappe.throw(
			_("Cannot allocate manufacture pool {0}: total MAIN_FG quantity is zero.").format(pool),
			frappe.ValidationError,
		)
	rem_i = _remainder_row_index(rows)
	amounts = [0.0] * len(rows)
	assigned = 0.0
	for i, row in enumerate(rows):
		if i == rem_i:
			continue
		share = round_currency(pool * qtys[i] / total_qty, currency)
		amounts[i] = share
		assigned += share
	amounts[rem_i] = round_currency(pool - assigned, currency)
	return amounts


def _manual_economics_coherent(fg_rows, allocatable: float, currency: str) -> bool:
	"""Healthy shared-manual Multi-FG: Σ material ≈ allocatable (tol 0 IRR)."""
	total = round_currency(sum(flt(r.get("basic_amount")) for r in fg_rows), currency)
	return abs(total - round_currency(allocatable, currency)) == 0


# ---------------------------------------------------------------------------
# SAME_ITEM_MULTI_FG allocator
# ---------------------------------------------------------------------------


def build_same_item_multi_fg_plan(doc) -> AllocationPlan:
	classified = classify_manufacture_outputs(doc)
	pools = build_economic_pools(doc, classified)
	currency = get_company_currency(doc.company)
	fg_rows = _main_fg_rows(classified)

	if pools["allocatable_material_pool"] < 0:
		frappe.throw(
			_(
				"SAME_ITEM_MULTI_FG material pool is negative ({0}) after deducting independent "
				"outputs (Component Scrap / Bulk Scrap / finance-excluded). Outgoing material={1}, "
				"independent={2}. Correct scrap/independent valuation before allocation."
			).format(
				pools["allocatable_material_pool"],
				pools["outgoing_material"],
				pools["independent_output_value"],
			),
			frappe.ValidationError,
		)

	plan = AllocationPlan(
		strategy_id=STRATEGY_SAME_ITEM_MULTI_FG,
		outgoing_material=pools["outgoing_material"],
		independent_output_value=pools["independent_output_value"],
		allocatable_material_pool=pools["allocatable_material_pool"],
		operating_pool=pools["operating_pool"],
		lcv_pool=pools["lcv_pool"],
	)

	all_manual = all(_row_manual(r) for r in fg_rows)
	if all_manual and _manual_economics_coherent(
		fg_rows, plan.allocatable_material_pool, currency
	):
		plan.preserved_manual = True
		plan.notes.append("preserved_healthy_manual_shared_rate")
		for row in fg_rows:
			qty = _row_qty(row)
			mat = round_currency(flt(row.get("basic_amount")), currency)
			oh = round_currency(flt(row.get("additional_cost")), currency)
			lcv = round_currency(flt(row.get("landed_cost_voucher_amount")), currency)
			final = round_currency(mat + oh + lcv, currency)
			br = round_monetary_rate(mat / qty, currency) if qty else 0
			vr = integer_valuation_rate_from_amount(final, qty, currency)
			plan.rows.append(
				NormalizedOutputRow(
					row_ref=row,
					classification=CLASS_MAIN_FG,
					allocation_basis=qty,
					allocation_owner=STRATEGY_SAME_ITEM_MULTI_FG,
					qty=qty,
					material_amount=mat,
					operating_amount=oh,
					lcv_amount=lcv,
					final_amount=final,
					basic_rate=br,
					valuation_rate=vr,
					basic_rate_amount_residual=amount_rate_qty_residual(mat, qty, br, currency),
					valuation_rate_amount_residual=amount_rate_qty_residual(final, qty, vr, currency),
					flags={"preserved_manual": True},
				)
			)
	else:
		mat_shares = allocate_amount_by_qty(plan.allocatable_material_pool, fg_rows, currency)
		oh_shares = allocate_amount_by_qty(plan.operating_pool, fg_rows, currency)
		for row, mat, oh in zip(fg_rows, mat_shares, oh_shares, strict=True):
			qty = _row_qty(row)
			lcv = round_currency(flt(row.get("landed_cost_voucher_amount")), currency)
			final = round_currency(mat + oh + lcv, currency)
			br = round_monetary_rate(mat / qty, currency) if qty else 0
			vr = integer_valuation_rate_from_amount(final, qty, currency)
			plan.rows.append(
				NormalizedOutputRow(
					row_ref=row,
					classification=CLASS_MAIN_FG,
					allocation_basis=qty,
					allocation_owner=STRATEGY_SAME_ITEM_MULTI_FG,
					qty=qty,
					material_amount=mat,
					operating_amount=oh,
					lcv_amount=lcv,
					final_amount=final,
					basic_rate=br,
					valuation_rate=vr,
					basic_rate_amount_residual=amount_rate_qty_residual(mat, qty, br, currency),
					valuation_rate_amount_residual=amount_rate_qty_residual(final, qty, vr, currency),
				)
			)

	# Independent rows recorded for verifier (not rewritten).
	for cls in (CLASS_COMPONENT_SCRAP, CLASS_BULK_SCRAP, CLASS_CO_PRODUCT):
		for row in classified.get(cls) or []:
			qty = _row_qty(row)
			mat = round_currency(flt(row.get("basic_amount")), currency)
			oh = round_currency(flt(row.get("additional_cost")), currency)
			lcv = round_currency(flt(row.get("landed_cost_voucher_amount")), currency)
			final = round_currency(mat + oh + lcv, currency)
			plan.rows.append(
				NormalizedOutputRow(
					row_ref=row,
					classification=cls,
					allocation_basis=qty,
					allocation_owner=STRATEGY_SAME_ITEM_MULTI_FG,
					qty=qty,
					material_amount=mat,
					operating_amount=oh,
					lcv_amount=lcv,
					final_amount=final,
					basic_rate=round_monetary_rate(mat / qty, currency) if qty else 0,
					valuation_rate=integer_valuation_rate_from_amount(final, qty, currency)
					if qty
					else 0,
					flags={"passthrough": True},
				)
			)

	fg_only = [r for r in plan.rows if r.classification == CLASS_MAIN_FG]
	plan.final_incoming_value = round_currency(
		sum(r.final_amount for r in plan.rows), currency
	)
	plan.economic_target = round_currency(
		plan.outgoing_material + plan.operating_pool + plan.lcv_pool, currency
	)
	# Independent passthrough + FG should absorb full outgoing+OH; residual class later.
	fg_mat = round_currency(sum(r.material_amount for r in fg_only), currency)
	if abs(fg_mat - plan.allocatable_material_pool) > 0:
		plan.residual_class = R1_ECONOMIC_ALLOCATION_GAP
		plan.economic_residual = round_currency(plan.allocatable_material_pool - fg_mat, currency)
	else:
		# R2 representation residuals only — not Stock Adjustment.
		r2 = sum(
			abs(flt(r.basic_rate_amount_residual)) + abs(flt(r.valuation_rate_amount_residual))
			for r in fg_only
		)
		plan.economic_residual = 0.0
		plan.residual_class = R2_IRR_RATE_REPRESENTATION if r2 else None
	return plan


def finalize_allocation_plan(doc, plan: AllocationPlan) -> AllocationPlan:
	"""Common finalizer: write strategy-owned SED fields; refresh header totals."""
	currency = get_company_currency(doc.company)
	for nrow in plan.rows:
		if nrow.flags.get("passthrough"):
			continue
		row = nrow.row_ref
		qty = nrow.qty
		row.basic_amount = nrow.material_amount
		row.additional_cost = nrow.operating_amount
		row.basic_rate = nrow.basic_rate
		row.amount = nrow.final_amount
		row.valuation_rate = nrow.valuation_rate
		if flt(row.amount):
			row.allow_zero_valuation_rate = 0
		# Recompute representation residuals after write (idempotent).
		nrow.basic_rate_amount_residual = amount_rate_qty_residual(
			nrow.material_amount, qty, nrow.basic_rate, currency
		)
		nrow.valuation_rate_amount_residual = amount_rate_qty_residual(
			nrow.final_amount, qty, nrow.valuation_rate, currency
		)
		assert_r2_within_bound(
			nrow.basic_rate_amount_residual,
			qty,
			label=_("row {0} basic_rate").format(row.get("idx") or row.get("item_code")),
		)
		assert_r2_within_bound(
			nrow.valuation_rate_amount_residual,
			qty,
			label=_("row {0} valuation_rate").format(row.get("idx") or row.get("item_code")),
		)

	_refresh_header_totals(doc)
	plan.final_incoming_value = round_currency(
		sum(flt(r.amount) for r in (doc.get("items") or []) if _is_incoming(r)),
		currency,
	)
	return plan


def _refresh_header_totals(doc) -> None:
	inc = sum(flt(d.amount) for d in doc.get("items") or [] if d.get("t_warehouse"))
	out = sum(flt(d.amount) for d in doc.get("items") or [] if d.get("s_warehouse"))
	doc.total_incoming_value = inc
	doc.total_outgoing_value = out
	doc.value_difference = flt(inc) - flt(out)


# ---------------------------------------------------------------------------
# Common verifier
# ---------------------------------------------------------------------------


def verify_allocation_plan(doc, plan: AllocationPlan | None = None, *, ledger: bool = False) -> list[str]:
	"""Validate closed-plan invariants. Raises on R1/R4; returns soft notes."""
	plan = plan or get_allocation_plan(doc)
	notes: list[str] = []
	if plan is None:
		if is_allocation_closed(doc):
			return notes
		return notes

	currency = get_company_currency(doc.company)
	owner = get_allocation_owner(doc)
	if owner and owner != plan.strategy_id:
		frappe.throw(
			_("Verifier: allocation owner {0} disagrees with plan strategy {1}.").format(
				owner, plan.strategy_id
			),
			frappe.ValidationError,
		)
	if not plan.closed and is_allocation_closed(doc):
		plan.closed = True

	fg_rows = [r for r in plan.rows if r.classification == CLASS_MAIN_FG]
	if plan.strategy_id == STRATEGY_SAME_ITEM_MULTI_FG:
		# Authoritative SED amounts (not only in-memory plan snapshots).
		fg_mat = round_currency(
			sum(flt(r.row_ref.get("basic_amount")) for r in fg_rows), currency
		)
		if abs(fg_mat - plan.allocatable_material_pool) != 0:
			frappe.throw(
				_(
					"R1 ECONOMIC_ALLOCATION_GAP: MAIN_FG material Σ {0} ≠ allocatable pool {1}. "
					"Pool allocation is incomplete. Stock Adjustment is not allowed for R1."
				).format(fg_mat, plan.allocatable_material_pool),
				frappe.ValidationError,
			)
		fg_oh = round_currency(
			sum(flt(r.row_ref.get("additional_cost")) for r in fg_rows), currency
		)
		if abs(fg_oh - plan.operating_pool) != 0:
			frappe.throw(
				_(
					"R1 ECONOMIC_ALLOCATION_GAP: MAIN_FG operating Σ {0} ≠ operating pool {1}."
				).format(fg_oh, plan.operating_pool),
				frappe.ValidationError,
			)

		# SED identity: amount = basic + additional + LCV
		for nrow in fg_rows:
			row = nrow.row_ref
			expected = round_currency(
				flt(row.get("basic_amount"))
				+ flt(row.get("additional_cost"))
				+ flt(row.get("landed_cost_voucher_amount")),
				currency,
			)
			if abs(flt(row.get("amount")) - expected) != 0:
				frappe.throw(
					_(
						"Post-finalization SED mismatch on row {0}: amount {1} ≠ "
						"basic_amount+additional_cost+LCV {2} (R4)."
					).format(row.get("idx"), row.get("amount"), expected),
					frappe.ValidationError,
				)
			# Plan snapshot must match SED after finalizer.
			if abs(flt(nrow.material_amount) - flt(row.get("basic_amount"))) != 0:
				frappe.throw(
					_(
						"Post-finalization plan/SED drift on row {0}: plan material {1} ≠ "
						"SED basic_amount {2} (R4)."
					).format(row.get("idx"), nrow.material_amount, row.get("basic_amount")),
					frappe.ValidationError,
				)
			assert_r2_within_bound(
				nrow.basic_rate_amount_residual,
				nrow.qty,
				label=_("verifier basic_rate residual row {0}").format(row.get("idx")),
			)
			assert_r2_within_bound(
				nrow.valuation_rate_amount_residual,
				nrow.qty,
				label=_("verifier valuation_rate residual row {0}").format(row.get("idx")),
			)

		# No Stock Adjustment for R1/R2: value_difference should equal OH+LCV economic
		# identity: incoming − outgoing ≈ operating + LCV (material closed).
		outgoing = plan.outgoing_material
		incoming_basic = round_currency(
			sum(flt(r.get("basic_amount")) for r in (doc.get("items") or []) if _is_incoming(r)),
			currency,
		)
		material_gap = round_currency(outgoing - incoming_basic, currency)
		if abs(material_gap) != 0:
			frappe.throw(
				_(
					"R4 CORRUPTION_UNEXPLAINED: material pool not closed (outgoing basic {0} − "
					"incoming basic {1} = {2}). Refusing Stock Adjustment plug."
				).format(outgoing, incoming_basic, material_gap),
				frappe.ValidationError,
			)

		# Double-pool smoke: FG material must not approach 2× allocatable.
		if plan.allocatable_material_pool and fg_mat > plan.allocatable_material_pool * 1.5:
			frappe.throw(
				_(
					"R4 CORRUPTION_UNEXPLAINED: MAIN_FG material Σ {0} looks like a double-pool "
					"relative to allocatable {1}."
				).format(fg_mat, plan.allocatable_material_pool),
				frappe.ValidationError,
			)

	if plan.residual_class == R4_CORRUPTION_UNEXPLAINED:
		frappe.throw(
			_("R4 CORRUPTION_UNEXPLAINED residual on plan; blocking."),
			frappe.ValidationError,
		)
	if plan.residual_class == R1_ECONOMIC_ALLOCATION_GAP:
		frappe.throw(
			_("R1 ECONOMIC_ALLOCATION_GAP residual on plan; blocking (no Stock Adjustment)."),
			frappe.ValidationError,
		)

	if ledger:
		_verify_ledger_if_present(doc, notes)

	return notes


def _verify_ledger_if_present(doc, notes: list[str]) -> None:
	"""Submitted path: SLE mirrors SED.amount; GL D=C; SA only for R3."""
	name = doc.get("name")
	if not name or cint(doc.get("docstatus")) != 1:
		return
	if not frappe.db.exists("Stock Entry", name):
		return
	# Soft check only when SLE rows exist.
	sle_rows = frappe.db.sql(
		"""
		SELECT voucher_detail_no, stock_value_difference, actual_qty
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND is_cancelled=0
		""",
		name,
		as_dict=True,
	)
	if not sle_rows:
		return
	sed_by_name = {r.name: r for r in (doc.get("items") or []) if r.get("name")}
	for sle in sle_rows:
		sed = sed_by_name.get(sle.voucher_detail_no)
		if not sed or not sed.get("t_warehouse"):
			continue
		expected = flt(sed.amount)
		# Incoming SLE stock_value_difference mirrors amount (positive actual_qty).
		if flt(sle.actual_qty) > 0 and abs(flt(sle.stock_value_difference) - expected) > 0:
			frappe.throw(
				_(
					"SLE stock_value_difference {0} ≠ SED.amount {1} for {2}."
				).format(sle.stock_value_difference, expected, sle.voucher_detail_no),
				frappe.ValidationError,
			)
	notes.append("ledger_sle_checked")


def reject_r4_double_pool(fg_material_sum: float, allocatable_pool: float) -> None:
	"""Explicit verifier helper for tests / diagnostics."""
	if allocatable_pool and fg_material_sum > allocatable_pool * 1.5:
		frappe.throw(
			_(
				"R4 CORRUPTION_UNEXPLAINED: double-pool detected (FG material {0} vs pool {1})."
			).format(fg_material_sum, allocatable_pool),
			frappe.ValidationError,
		)


# ---------------------------------------------------------------------------
# Apply SAME_ITEM_MULTI_FG
# ---------------------------------------------------------------------------


def apply_same_item_multi_fg(doc) -> bool:
	"""Allocate + finalize + verify + close. Returns True when strategy applied."""
	eligible, reason = detect_same_item_multi_fg_eligibility(doc)
	if not eligible:
		if reason in (
			"mixed_manual_automatic",
			"invalid_qty_basis",
			"incompatible_uom",
			"product_reject_unsupported",
			"different_main_fg_items",
		):
			assert_multi_fg_fail_closed(doc)
		return False

	plan = build_same_item_multi_fg_plan(doc)
	finalize_allocation_plan(doc, plan)
	owned = [r.row_ref for r in plan.rows if r.classification == CLASS_MAIN_FG]
	mark_allocation_closed(doc, STRATEGY_SAME_ITEM_MULTI_FG, plan=plan, owned_rows=owned)
	verify_allocation_plan(doc, plan)
	return True


def simulate_same_item_multi_fg(doc) -> AllocationPlan:
	"""In-memory dry-run: build+finalize on a working copy semantics without DB writes.

	Mutates the provided doc object (caller should pass a copy / loaded doc they
	will discard). Does not db_update / submit / repair.
	"""
	plan = build_same_item_multi_fg_plan(doc)
	finalize_allocation_plan(doc, plan)
	plan.closed = True
	return plan


# ---------------------------------------------------------------------------
# Closed-plan aligner protection
# ---------------------------------------------------------------------------


def protect_closed_row_amounts(doc, row, currency: str) -> bool:
	"""Amount-authoritative polish for a closed-plan owned row.

	Returns True when the row was handled (caller must skip rate-first rewrite).
	"""
	if not row_is_allocation_owned(doc, row):
		return False
	transfer_qty = _row_qty(row)
	if row.get("additional_cost") not in (None, ""):
		row.additional_cost = round_currency(row.additional_cost, currency)
	if row.get("landed_cost_voucher_amount") not in (None, ""):
		row.landed_cost_voucher_amount = round_currency(
			row.landed_cost_voucher_amount, currency
		)
	# Economics authoritative — do NOT recompose basic_amount from integer rate.
	row.basic_amount = round_currency(flt(row.get("basic_amount")), currency)
	if transfer_qty:
		row.basic_rate = round_monetary_rate(flt(row.basic_amount) / transfer_qty, currency)
	elif row.get("basic_rate") is not None:
		row.basic_rate = round_monetary_rate(row.basic_rate, currency)
	row.amount = round_currency(
		flt(row.basic_amount)
		+ flt(row.get("additional_cost"))
		+ flt(row.get("landed_cost_voucher_amount")),
		currency,
	)
	if transfer_qty and flt(row.amount):
		row.valuation_rate = integer_valuation_rate_from_amount(
			row.amount, transfer_qty, currency
		)
	elif row.get("valuation_rate") is not None:
		row.valuation_rate = round_monetary_rate(row.valuation_rate, currency)
	return True


def polish_closed_plan_after_align(doc) -> None:
	"""Header refresh + optional re-verify when a plan is closed."""
	if not is_allocation_closed(doc):
		return
	_refresh_header_totals(doc)
	plan = get_allocation_plan(doc)
	if plan and plan.strategy_id == STRATEGY_SAME_ITEM_MULTI_FG:
		# Refresh row snapshots from SED after any non-destructive polish.
		currency = get_company_currency(doc.company)
		for nrow in plan.rows:
			if nrow.classification != CLASS_MAIN_FG:
				continue
			row = nrow.row_ref
			nrow.material_amount = round_currency(flt(row.get("basic_amount")), currency)
			nrow.operating_amount = round_currency(flt(row.get("additional_cost")), currency)
			nrow.lcv_amount = round_currency(flt(row.get("landed_cost_voucher_amount")), currency)
			nrow.final_amount = round_currency(flt(row.get("amount")), currency)
			nrow.basic_rate = flt(row.get("basic_rate"))
			nrow.valuation_rate = flt(row.get("valuation_rate"))
			nrow.basic_rate_amount_residual = amount_rate_qty_residual(
				nrow.material_amount, nrow.qty, nrow.basic_rate, currency
			)
			nrow.valuation_rate_amount_residual = amount_rate_qty_residual(
				nrow.final_amount, nrow.qty, nrow.valuation_rate, currency
			)
		verify_allocation_plan(doc, plan)
