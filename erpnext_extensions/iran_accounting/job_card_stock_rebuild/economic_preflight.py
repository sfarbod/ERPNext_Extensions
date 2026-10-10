# Copyright (c) 2026, ERPNext Extensions contributors
"""Economic Prepare for Job Card Manufacture repair (experiment/jc-repair-native-riv).

Computes corrected material pool vs historical FG economics BEFORE mutation.
Decides when MAIN_FG rates must be unlocked so Core + Iran can reallocate.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, flt


def _is_consume_row(row: dict) -> bool:
	if row.get("type") == "CONSUME":
		return True
	if cint(row.get("is_finished_item")):
		return False
	return bool(row.get("s_warehouse")) and not row.get("t_warehouse")


def _is_main_fg_row(row: dict) -> bool:
	if cint(row.get("is_finished_item")):
		return True
	cls = (row.get("custom_output_class") or row.get("type") or "").strip()
	return cls == "MAIN_FG"


def _is_scrap_or_secondary(row: dict) -> bool:
	cls = (row.get("custom_output_class") or row.get("type") or "").strip()
	if cls in ("COMPONENT_SCRAP", "MAIN_PRODUCT_REJECT"):
		return True
	if (row.get("secondary_item_type") or "") == "Scrap":
		return True
	return False


def summarize_canonical_economics(canonical_rows: list[dict] | None) -> dict[str, Any]:
	"""Sum consume / MAIN_FG amounts from planner canonical rows (IRR floats)."""
	material = 0.0
	fg = 0.0
	scrap = 0.0
	consume_rows: list[dict] = []
	fg_rows: list[dict] = []
	for row in canonical_rows or []:
		qty = flt(row.get("qty"))
		rate = flt(row.get("basic_rate") or row.get("valuation_rate"))
		amt = rate * qty
		if _is_consume_row(row):
			material += amt
			consume_rows.append(
				{
					"item_code": row.get("item_code"),
					"batch_no": row.get("batch_no") or "",
					"qty": qty,
					"rate": rate,
					"amount": amt,
					"rate_source": row.get("rate_source"),
				}
			)
		elif _is_main_fg_row(row):
			fg += amt
			fg_rows.append(
				{
					"item_code": row.get("item_code"),
					"batch_no": row.get("batch_no") or "",
					"qty": qty,
					"rate": rate,
					"amount": amt,
					"rate_source": row.get("rate_source"),
				}
			)
		elif _is_scrap_or_secondary(row):
			scrap += amt
	return {
		"material_pool": material,
		"fg_value_frozen_preview": fg,
		"scrap_value": scrap,
		"consume_rows": consume_rows,
		"fg_rows": fg_rows,
		"gap_pool_minus_fg": material - fg,
	}


def historical_manufacture_economics(mfg_name: str | None) -> dict[str, Any] | None:
	if not mfg_name or not frappe.db.exists("Stock Entry", mfg_name):
		return None
	se = frappe.db.get_value(
		"Stock Entry",
		mfg_name,
		[
			"name",
			"purpose",
			"docstatus",
			"total_outgoing_value",
			"total_incoming_value",
			"value_difference",
			"total_additional_costs",
			"company",
		],
		as_dict=1,
	)
	if not se or se.purpose != "Manufacture":
		return None
	adj_account = frappe.db.get_value("Company", se.company, "stock_adjustment_account")
	sa_net = 0.0
	if adj_account:
		sa_net = flt(
			frappe.db.sql(
				"""
				select ifnull(sum(credit),0) - ifnull(sum(debit),0)
				from `tabGL Entry`
				where voucher_type='Stock Entry' and voucher_no=%s
				  and account=%s and ifnull(is_cancelled,0)=0
				""",
				(mfg_name, adj_account),
			)[0][0]
		)
	return {
		"historical_mfg": mfg_name,
		"outgoing": flt(se.total_outgoing_value),
		"incoming": flt(se.total_incoming_value),
		"value_difference": flt(se.value_difference),
		"additional_costs": flt(se.total_additional_costs),
		"sa_net_credit": sa_net,
	}


def analyze_manufacture_economics(plan: dict) -> dict[str, Any]:
	"""Prepare-phase economics for a built manufacture plan.

	Returns unlock policy + residual classification guidance.
	Does not mutate stock.
	"""
	canon = plan.get("canonical_manufacture") or {}
	rows = canon.get("rows") or []
	summary = summarize_canonical_economics(rows)
	supersedes = list(canon.get("supersedes") or plan.get("merge_documents") or [])
	hist = historical_manufacture_economics(supersedes[0] if supersedes else None)

	material = flt(summary["material_pool"])
	fg_preview = flt(summary["fg_value_frozen_preview"])
	hist_out = flt((hist or {}).get("outgoing"))
	hist_in = flt((hist or {}).get("incoming"))
	hist_vd = flt((hist or {}).get("value_difference"))

	# Value-neutral source correction: outgoing pool unchanged → may preserve R3.
	value_neutral = bool(hist) and abs(material - hist_out) < 0.5
	# Material pool changed relative to historical outgoing → unlock MAIN_FG.
	unlock_main_fg = bool(hist) and not value_neutral
	# Also unlock when planner FG preview already disagrees with material pool
	# (frozen historical FG rates on unlocked-needed path).
	if abs(material - fg_preview) >= 0.5 and not value_neutral:
		unlock_main_fg = True

	# Expected FG under closed Multi-FG allocation = material pool
	# (no Type-A additional cost / independent scrap deducted here — Iran handles).
	expected_fg_if_unlocked = material
	expected_residual = 0.0
	residual_class = None
	notes: list[str] = []

	if unlock_main_fg:
		notes.append(
			"MAIN_FG_UNLOCK: corrected material pool differs from historical outgoing; "
			"do not freeze obsolete FG rates; Core+Iran must reallocate."
		)
		notes.append(
			"HISTORICAL_R3_NOT_PRESERVABLE: historical Stock Adjustment is invalidated "
			"by non-value-neutral consumption correction."
		)
		residual_class = "REALLOCATE_TO_POOL"
		expected_residual = 0.0
	elif value_neutral and hist and abs(hist_vd) >= 0.5:
		notes.append("VALUE_NEUTRAL: historical R3 residual may be preserved.")
		residual_class = "PRESERVE_HISTORICAL_R3"
		expected_fg_if_unlocked = hist_in
		expected_residual = hist_vd
	elif abs(material - fg_preview) < 0.5:
		notes.append("POOL_MATCHES_FG_PREVIEW: no MAIN_FG unlock required.")
		residual_class = "BALANCED"
	else:
		# Genuine unexplained gap with frozen FG and no hist change — R1.
		residual_class = "R1_ECONOMIC_ALLOCATION_GAP"
		expected_residual = material - fg_preview
		notes.append(
			f"R1_PREFLIGHT: material pool {material} ≠ FG preview {fg_preview}."
		)

	blockers: list[str] = []
	if residual_class == "R1_ECONOMIC_ALLOCATION_GAP":
		blockers.append(
			"R1 ECONOMIC_ALLOCATION_GAP (preflight): MAIN_FG material Σ "
			f"{fg_preview} ≠ allocatable pool {material}. "
			"Pool allocation is incomplete. Stock Adjustment is not allowed for R1."
		)

	return {
		"ok": not blockers,
		"blockers": blockers,
		"unlock_main_fg": unlock_main_fg,
		"preserve_historical_residual": residual_class == "PRESERVE_HISTORICAL_R3",
		"value_neutral": value_neutral,
		"residual_class": residual_class,
		"material_pool": material,
		"fg_value_frozen_preview": fg_preview,
		"expected_fg_if_unlocked": expected_fg_if_unlocked,
		"expected_residual": expected_residual,
		"historical": hist,
		"consume_rows": summary["consume_rows"],
		"fg_rows": summary["fg_rows"],
		"notes": notes,
		"valuation_mode": "ESTIMATED_PENDING_RIV" if unlock_main_fg else "CLOSED_OR_PRESERVED",
	}


def attach_economic_preflight(plan: dict) -> dict:
	"""Mutate plan with economic_preflight; append blockers when fail-closed."""
	econ = analyze_manufacture_economics(plan)
	plan["economic_preflight"] = econ
	plan["unlock_main_fg"] = bool(econ.get("unlock_main_fg"))
	plan["preserve_historical_residual"] = bool(econ.get("preserve_historical_residual"))
	for b in econ.get("blockers") or []:
		if b not in (plan.get("blockers") or []):
			plan.setdefault("blockers", []).append(b)
	if plan.get("blockers"):
		plan["apply_allowed"] = False
	return plan
