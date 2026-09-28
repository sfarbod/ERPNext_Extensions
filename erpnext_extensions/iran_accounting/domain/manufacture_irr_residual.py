# Copyright (c) 2026, ERPNext Extensions contributors
"""Manufacture IRR residual classification — TYPE A / B / C (v5.3.34).

TYPE A — real additional cost / LCV capitalization (never rounding).
TYPE B — single-FG amount-authoritative economic residual (v5.3.30; SA = 0).
TYPE C — pure whole-IRR multi-output allocation residual → native Stock Adjustment.

TYPE C is returned only when mathematically proven. Unknown gaps fail closed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import floor, gcd
from typing import Any

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.domain.currency import (
	get_company_currency,
	is_irr_company,
	round_currency,
)
from erpnext_extensions.iran_accounting.rounding import get_currency_precision

CLASS_NONE = "NONE"
CLASS_TYPE_A = "TYPE_A_REAL_ADDITIONAL_COST"
CLASS_TYPE_B = "TYPE_B_SINGLE_FG_ECONOMIC_RESIDUAL"
CLASS_TYPE_C = "TYPE_C_PURE_IRR_ALLOCATION_RESIDUAL"
CLASS_INVALID = "INVALID"

# Contract stamps: 5.3.3 = legacy leftover-in-additional_cost; 5.3.34+ = TYPE C → SA.
LEGACY_MANUFACTURE_COSTING_CONTRACT_VERSION = "5.3.3"
TYPE_C_SA_CONTRACT_VERSION = "5.3.34"


@dataclass
class ManufactureIrrResidualResult:
	classification: str
	economic_pool: float = 0.0
	composed_output_value: float = 0.0
	residual: float = 0.0
	mathematical_bound: float = 0.0
	output_quantities: list[float] = field(default_factory=list)
	gcd: float = 0.0
	reason: str = ""
	evidence: dict[str, Any] = field(default_factory=dict)

	def to_dict(self) -> dict:
		return asdict(self)

	@property
	def is_type_c(self) -> bool:
		return self.classification == CLASS_TYPE_C

	@property
	def is_invalid(self) -> bool:
		return self.classification == CLASS_INVALID


def _contract_version(doc) -> str:
	return str(doc.get("custom_manufacturing_costing_contract_version") or "").strip()


def _persisted_docstatus(doc) -> int:
	"""DB docstatus — during submit, in-memory docstatus is already 1 while DB is still 0."""
	name = doc.get("name")
	if not name or doc.get("__islocal"):
		return 0
	try:
		return cint(frappe.db.get_value(doc.doctype, name, "docstatus") or 0)
	except Exception:
		return cint(doc.get("docstatus") or 0)


def uses_type_c_sa_residual_policy(doc) -> bool:
	"""Drafts and 5.3.34+ stamps use TYPE C → Stock Adjustment.

	Submitted documents stamped ``5.3.3`` keep the legacy leftover→additional_cost
	path so RIV cannot silently rewrite historical economics.

	Important: ``Document.submit()`` sets in-memory ``docstatus=1`` before hooks
	run while the DB row is still draft. Policy therefore keys off **persisted**
	docstatus, not the in-memory flag.
	"""
	if getattr(doc, "doctype", None) != "Stock Entry":
		return False
	if getattr(doc, "purpose", None) != "Manufacture":
		return False
	version = _contract_version(doc)
	persisted = _persisted_docstatus(doc)
	if persisted == 1:
		if not version:
			return False
		if version == LEGACY_MANUFACTURE_COSTING_CONTRACT_VERSION:
			return False
		return version == TYPE_C_SA_CONTRACT_VERSION or _version_at_least(version, "5.3.34")
	# Draft (including submit-in-progress of a draft): adopt TYPE C.
	return True


def _version_at_least(version: str, minimum: str) -> bool:
	"""Compare dotted numeric versions; non-numeric → False."""
	try:
		v_parts = [int(p) for p in version.split(".")]
		m_parts = [int(p) for p in minimum.split(".")]
	except ValueError:
		return False
	# pad
	n = max(len(v_parts), len(m_parts))
	v_parts += [0] * (n - len(v_parts))
	m_parts += [0] * (n - len(m_parts))
	return v_parts >= m_parts


def integer_qtys_gcd(quantities: list[float]) -> int:
	"""GCD of positive integer quantities (IRR qty basis)."""
	ints: list[int] = []
	for q in quantities:
		qf = flt(q)
		if qf <= 0:
			continue
		# Require whole quantities for the multi-output integer composition model.
		if abs(qf - round(qf)) > 1e-9:
			return 0
		ints.append(int(round(qf)))
	if not ints:
		return 0
	g = ints[0]
	for q in ints[1:]:
		g = gcd(g, q)
		if g == 1:
			return 1
	return g


def type_c_residual_bound(quantities: list[float]) -> float:
	"""Max |leftover| for nearest whole-IRR multi-rate composition.

	Reachable totals are multiples of g = gcd(q_i). The nearest leftover therefore
	satisfies |L| <= floor(g / 2).
	"""
	g = integer_qtys_gcd(quantities)
	if g <= 0:
		return 0.0
	return float(floor(g / 2))


def _row_qty(row) -> float:
	value = row.get("transfer_qty")
	if value in (None, ""):
		value = row.get("qty")
	return flt(value)


def _header_additional_cost_total(doc) -> float:
	return sum(flt(t.get("base_amount") if t.get("base_amount") not in (None, "") else t.get("amount")) for t in (doc.get("additional_costs") or []))


def _row_lcv_total(doc) -> float:
	return sum(flt(r.get("landed_cost_voucher_amount")) for r in (doc.get("items") or []))


def classify_manufacture_irr_residual(doc) -> ManufactureIrrResidualResult:
	"""Classify Manufacture residual as NONE / TYPE_A / TYPE_B / TYPE_C / INVALID.

	Does not mutate the document. Call after output rates/amounts are set.
	"""
	if getattr(doc, "doctype", None) != "Stock Entry" or getattr(doc, "purpose", None) != "Manufacture":
		return ManufactureIrrResidualResult(CLASS_NONE, reason="not_manufacture")

	company = doc.get("company")
	if not company or not is_irr_company(company):
		return ManufactureIrrResidualResult(CLASS_NONE, reason="non_irr")

	currency = get_company_currency(company)
	precision = get_currency_precision(currency)
	if precision != 0:
		return ManufactureIrrResidualResult(CLASS_NONE, reason="non_whole_irr_precision")

	from erpnext_extensions.iran_accounting.scrap_costing import (
		CLASS_COMPONENT_SCRAP,
		CLASS_MAIN_FG,
		CLASS_MAIN_PRODUCT_REJECT,
		classify_manufacture_outputs,
		is_scrap_row,
	)

	classified = classify_manufacture_outputs(doc)
	fg_rows = classified[CLASS_MAIN_FG]
	reject_rows = classified[CLASS_MAIN_PRODUCT_REJECT]
	component_rows = classified[CLASS_COMPONENT_SCRAP]

	header_add = _header_additional_cost_total(doc)
	lcv_total = _row_lcv_total(doc)
	evidence: dict[str, Any] = {
		"header_additional_cost": header_add,
		"lcv_total": lcv_total,
		"fg_count": len(fg_rows),
		"reject_count": len(reject_rows),
		"component_scrap_count": len(component_rows),
		"contract_version": _contract_version(doc),
		"type_c_policy": uses_type_c_sa_residual_policy(doc),
	}

	if header_add or lcv_total:
		# TYPE A may coexist with TYPE C; surface A when only real cost and no C gap.
		evidence["has_real_capitalization"] = True

	# Integrity: negative amounts / rates
	for row in doc.get("items") or []:
		if flt(row.get("amount")) < 0 or flt(row.get("basic_amount") or 0) < 0:
			return ManufactureIrrResidualResult(
				CLASS_INVALID, reason="negative_row_amount", evidence=evidence
			)
		if row.get("basic_rate") not in (None, "") and flt(row.basic_rate) < 0:
			return ManufactureIrrResidualResult(
				CLASS_INVALID, reason="negative_basic_rate", evidence=evidence
			)

	# Component scrap must already match issued-rate economics when present.
	if component_rows:
		from erpnext_extensions.iran_accounting.scrap_costing import (
			_component_scrap_matches_issued_rate,
		)

		if not _component_scrap_matches_issued_rate(doc, currency):
			return ManufactureIrrResidualResult(
				CLASS_INVALID, reason="component_scrap_source_mismatch", evidence=evidence
			)

	outgoing = sum(flt(row.get("amount")) for row in doc.get("items") or [] if row.get("s_warehouse"))
	component_value = sum(flt(row.get("amount")) for row in component_rows)
	# Product allocation pool: consume − component scrap (issued economics).
	economic_pool = round_currency(outgoing - component_value, currency)
	evidence["outgoing"] = outgoing
	evidence["component_value"] = component_value
	evidence["economic_pool"] = economic_pool

	# --- TYPE C candidate: single FG + ≥1 product reject, whole-IRR rates ---
	if len(fg_rows) == 1 and reject_rows:
		product_rows = [fg_rows[0]] + list(reject_rows)
		qtys = [_row_qty(r) for r in product_rows]
		if any(q <= 0 for q in qtys):
			return ManufactureIrrResidualResult(
				CLASS_INVALID, reason="non_positive_output_qty", evidence=evidence, economic_pool=economic_pool
			)

		g = integer_qtys_gcd(qtys)
		bound = type_c_residual_bound(qtys)
		evidence["output_quantities"] = qtys
		evidence["gcd"] = g
		evidence["mathematical_bound"] = bound

		if g <= 0:
			return ManufactureIrrResidualResult(
				CLASS_INVALID,
				reason="non_integer_output_qty",
				evidence=evidence,
				economic_pool=economic_pool,
				output_quantities=qtys,
			)

		# Each product row must compose as qty × integer rate for the MATERIAL share.
		composed = 0.0
		for row in product_rows:
			qty = _row_qty(row)
			rate = flt(row.get("basic_rate"))
			if abs(rate - round(rate)) > 1e-9:
				return ManufactureIrrResidualResult(
					CLASS_INVALID,
					reason="non_integer_output_rate",
					evidence=evidence,
					economic_pool=economic_pool,
					output_quantities=qtys,
					gcd=float(g),
					mathematical_bound=bound,
				)
			expected_basic = round_currency(rate * qty, currency)
			if abs(flt(row.get("basic_amount")) - expected_basic) > 0:
				return ManufactureIrrResidualResult(
					CLASS_INVALID,
					reason="basic_amount_not_qty_times_rate",
					evidence=evidence,
					economic_pool=economic_pool,
					output_quantities=qtys,
					gcd=float(g),
					mathematical_bound=bound,
				)
			# Real capitalization may sit on amount; material composed value uses basic.
			composed += expected_basic

		composed = round_currency(composed, currency)
		residual = round_currency(economic_pool - composed, currency)
		evidence["composed_output_value"] = composed
		evidence["residual"] = residual

		# Real ac on product rows must equal header distribution (+ LCV on those rows),
		# not a leftover plug.
		row_ac = sum(flt(r.get("additional_cost")) for r in product_rows)
		row_lcv = sum(flt(r.get("landed_cost_voucher_amount")) for r in product_rows)
		evidence["product_row_additional_cost"] = row_ac
		evidence["product_row_lcv"] = row_lcv

		if residual == 0:
			if header_add or lcv_total:
				return ManufactureIrrResidualResult(
					CLASS_TYPE_A,
					economic_pool=economic_pool,
					composed_output_value=composed,
					residual=0.0,
					mathematical_bound=bound,
					output_quantities=qtys,
					gcd=float(g),
					reason="exact_composition_with_real_capitalization",
					evidence=evidence,
				)
			return ManufactureIrrResidualResult(
				CLASS_NONE,
				economic_pool=economic_pool,
				composed_output_value=composed,
				residual=0.0,
				mathematical_bound=bound,
				output_quantities=qtys,
				gcd=float(g),
				reason="exact_composition",
				evidence=evidence,
			)

		if abs(residual) > bound:
			return ManufactureIrrResidualResult(
				CLASS_INVALID,
				economic_pool=economic_pool,
				composed_output_value=composed,
				residual=residual,
				mathematical_bound=bound,
				output_quantities=qtys,
				gcd=float(g),
				reason="residual_outside_mathematical_bound",
				evidence=evidence,
			)

		# Phantom leftover in additional_cost (beyond header) is invalid under TYPE C policy.
		if uses_type_c_sa_residual_policy(doc):
			# Allow row_ac to match header allocation to product rows (≈ header_add).
			# Disallow leftover-sized plug when header is 0 but ac != 0 without LCV.
			if header_add == 0 and row_lcv == 0 and abs(row_ac) > 0:
				# Still TYPE C economically if residual matches bound; caller must clear ac.
				evidence["phantom_additional_cost_detected"] = row_ac

		return ManufactureIrrResidualResult(
			CLASS_TYPE_C,
			economic_pool=economic_pool,
			composed_output_value=composed,
			residual=residual,
			mathematical_bound=bound,
			output_quantities=qtys,
			gcd=float(g),
			reason="proven_whole_irr_multi_output_allocation_residual",
			evidence=evidence,
		)

	# --- TYPE B: single FG, no product reject ---
	if len(fg_rows) == 1 and not reject_rows:
		fg = fg_rows[0]
		qty = _row_qty(fg)
		if qty <= 0:
			return ManufactureIrrResidualResult(CLASS_INVALID, reason="fg_qty_non_positive", evidence=evidence)

		other_incoming = sum(
			flt(r.get("amount"))
			for r in (doc.get("items") or [])
			if r.get("t_warehouse") and r is not fg
		)
		# Material target for FG after other incoming (component scrap etc.).
		material_target = round_currency(outgoing - other_incoming, currency)
		rate = flt(fg.get("basic_rate"))
		rate_product = round_currency(rate * qty, currency) if rate else 0.0
		fg_basic = flt(fg.get("basic_amount"))
		fg_amount = flt(fg.get("amount"))
		capitalized = flt(fg.get("additional_cost")) + flt(fg.get("landed_cost_voucher_amount"))

		evidence.update(
			{
				"material_target": material_target,
				"fg_basic": fg_basic,
				"fg_amount": fg_amount,
				"rate_product": rate_product,
				"capitalized": capitalized,
			}
		)

		if header_add or lcv_total:
			# Prefer announcing TYPE A when header capitalization exists and pool closes
			# with capitalized costs (TYPE B residual may still sit in amount).
			pass

		# Exact: amount material part matches pool (within amount = material + cap).
		expected_amount = round_currency(material_target + capitalized, currency)
		# If basic == qty×rate and amount closes pool via residual in amount/basic:
		residual_vs_rate = round_currency(fg_basic - rate_product, currency) if rate else 0.0
		pool_gap = round_currency(fg_amount - expected_amount, currency)

		if abs(pool_gap) == 0 and residual_vs_rate == 0 and not (header_add or lcv_total):
			return ManufactureIrrResidualResult(
				CLASS_NONE,
				economic_pool=outgoing,
				composed_output_value=fg_amount + other_incoming,
				residual=0.0,
				reason="single_fg_exact",
				evidence=evidence,
			)

		if abs(pool_gap) == 0 and abs(residual_vs_rate) > 0:
			# Amount-authoritative residual inside FG (v5.3.30 TYPE B).
			return ManufactureIrrResidualResult(
				CLASS_TYPE_B,
				economic_pool=outgoing,
				composed_output_value=fg_amount + other_incoming,
				residual=residual_vs_rate,
				reason="single_fg_amount_authoritative_residual",
				evidence=evidence,
			)

		if abs(pool_gap) == 0 and (header_add or lcv_total):
			return ManufactureIrrResidualResult(
				CLASS_TYPE_A,
				economic_pool=outgoing,
				composed_output_value=fg_amount + other_incoming,
				residual=0.0,
				reason="single_fg_with_real_capitalization",
				evidence=evidence,
			)

		# Pool does not close — invalid unless explained.
		return ManufactureIrrResidualResult(
			CLASS_INVALID,
			economic_pool=outgoing,
			composed_output_value=fg_amount + other_incoming,
			residual=pool_gap,
			reason="single_fg_unexplained_pool_gap",
			evidence=evidence,
		)

	# Multi-FG / co-product / missing FG without a composition proof.
	# Reachable only when len(fg_rows) != 1: both single-FG arms above return on every path.
	return ManufactureIrrResidualResult(
		CLASS_INVALID if fg_rows else CLASS_NONE,
		reason="multi_fg_or_missing_fg_unsupported_for_auto_type_c",
		evidence=evidence,
	)


def proven_type_c_residual(doc) -> float | None:
	"""Return signed TYPE C residual when proven under current policy; else None."""
	if not uses_type_c_sa_residual_policy(doc):
		return None
	result = classify_manufacture_irr_residual(doc)
	if result.classification != CLASS_TYPE_C:
		return None
	return float(result.residual)
