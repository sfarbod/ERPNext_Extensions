# Copyright (c) 2026, ERPNext Extensions contributors
"""Historical Manufacture-native valuation (v5.3.7+).

Derives Finished-Good expected rate from THIS voucher's authoritative
source SLE economics — not sibling rates, not previous_healthy_sle, and
not the v5.3.3 draft-only Iran contract stamp (which refuses historical
submitted documents).

Native ERPNext Manufacture rule used here (single FG, no stage co-product):

    FG_basic_rate = (sum |source SLE stock_value_difference|
                     − sum scrap/by-product SE basic_amount) / FG_qty

Additional costs are NOT part of basic_rate; they appear on SE.amount / SLE SVD
as FG.additional_cost. Scrap deduction uses SE basic_amount (ERPNext
get_costed_out_items_cost), never inventing rates from sibling SLE.

Manufacturing business quantities are never written.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_AMBIGUOUS,
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	CONFIDENCE_MANUAL,
	QTY_EPS,
	RATE_EPS,
	VALUE_EPS,
)

WAITING_UPSTREAM = "WAITING_UPSTREAM"
EXACT = "EXACT"
RECONSTRUCTABLE = "RECONSTRUCTABLE"
LEGITIMATE = "LEGITIMATE"
MANUAL = "MANUAL"
HEALTHY = "HEALTHY"
RATE_TOL = 1.0


def reconstruct_manufacture_valuation(voucher: str) -> dict[str, Any]:
	"""Read-only reconstruction for one submitted Manufacture Stock Entry."""
	if not voucher or not frappe.db.exists("Stock Entry", voucher):
		return {"voucher": voucher, "ok": False, "classification": MANUAL, "reason": "missing voucher"}

	se = frappe.db.get_value(
		"Stock Entry",
		voucher,
		[
			"name",
			"purpose",
			"docstatus",
			"company",
			"work_order",
			"job_card",
			"posting_date",
			"posting_time",
			"total_additional_costs",
		],
		as_dict=True,
	)
	if not se or se.purpose != "Manufacture" or cint(se.docstatus) != 1:
		return {
			"voucher": voucher,
			"ok": False,
			"classification": MANUAL,
			"reason": f"not submitted Manufacture ({getattr(se, 'purpose', None)})",
		}

	details = frappe.db.sql(
		"""
		SELECT name, item_code, qty, basic_rate, valuation_rate, amount, basic_amount,
		       s_warehouse, t_warehouse,
		       IFNULL(is_finished_item,0) AS is_fg,
		       IFNULL(is_scrap_item,0) AS is_scrap,
		       IFNULL(allow_zero_valuation_rate,0) AS allow_zero,
		       secondary_item_type, valuation_type,
		       IFNULL(additional_cost,0) AS additional_cost
		FROM `tabStock Entry Detail`
		WHERE parent=%s
		ORDER BY idx
		""",
		(voucher,),
		as_dict=True,
	)
	sles = frappe.db.sql(
		"""
		SELECT name, voucher_detail_no, item_code, warehouse, actual_qty,
		       incoming_rate, outgoing_rate, valuation_rate,
		       stock_value_difference, stock_value
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
		""",
		(voucher,),
		as_dict=True,
	)
	by_detail = {s.voucher_detail_no: s for s in sles}

	sources: list[dict] = []
	scraps: list[dict] = []
	byproducts: list[dict] = []
	fgs: list[dict] = []
	consumed_value = 0.0
	scrap_value = 0.0
	byproduct_value = 0.0
	poisoned_sources = 0

	for d in details:
		sle = by_detail.get(d.name)
		qty = flt(d.qty)
		if cint(d.is_fg):
			fgs.append({"detail": d, "sle": sle})
			continue
		# ERPNext Manufacture: scrap + by-product incoming reduce the FG residual pool.
		# Deduct SE basic_amount (get_costed_out_items_cost) — not amount (may include additional_cost).
		if cint(d.is_scrap) or _is_scrap_like(d) or _is_byproduct_like(d):
			val = abs(flt(d.basic_amount))
			if val <= VALUE_EPS:
				val = _incoming_secondary_value(d, sle)
			if _is_byproduct_like(d) and not (cint(d.is_scrap) or _is_scrap_like(d)):
				byproduct_value += val
				byproducts.append({"detail": d, "sle": sle, "value": val})
			else:
				scrap_value += val
				scraps.append({"detail": d, "sle": sle, "value": val})
			continue
		if not d.s_warehouse:
			# Other target-only non-FG rows still reduce residual via basic_amount when present.
			if d.t_warehouse:
				val = abs(flt(d.basic_amount)) or _incoming_secondary_value(d, sle)
				if val > VALUE_EPS:
					byproduct_value += val
					byproducts.append({"detail": d, "sle": sle, "value": val, "kind": "other_incoming"})
			continue
		# Source / consumption row
		if sle and flt(sle.actual_qty) < 0:
			svd = abs(flt(sle.stock_value_difference))
			orate = abs(flt(sle.outgoing_rate))
			consumed_value += svd
			zero_move = abs(qty) > QTY_EPS and svd <= VALUE_EPS and orate <= RATE_EPS
			if zero_move and not cint(d.allow_zero):
				poisoned_sources += 1
			sources.append(
				{
					"detail": d.name,
					"item": d.item_code,
					"qty": qty,
					"sle_outgoing": orate,
					"sle_svd": svd,
					"zero_poison": zero_move and not cint(d.allow_zero),
				}
			)
		else:
			# No SLE — fall back to SE amount only as weak evidence
			consumed_value += abs(flt(d.amount))
			if abs(flt(d.basic_rate)) <= RATE_EPS and abs(qty) > QTY_EPS and not cint(d.allow_zero):
				poisoned_sources += 1
			sources.append(
				{
					"detail": d.name,
					"item": d.item_code,
					"qty": qty,
					"sle_outgoing": flt(d.basic_rate),
					"sle_svd": abs(flt(d.amount)),
					"zero_poison": abs(flt(d.basic_rate)) <= RATE_EPS and not cint(d.allow_zero),
					"weak_se_fallback": True,
				}
			)

	out: dict[str, Any] = {
		"voucher": voucher,
		"purpose": "Manufacture",
		"work_order": se.work_order,
		"job_card": se.job_card,
		"posting_date": str(se.posting_date),
		"consumed_value": consumed_value,
		"scrap_value": scrap_value,
		"byproduct_value": byproduct_value,
		"additional_costs": flt(se.total_additional_costs),
		"source_count": len(sources),
		"scrap_count": len(scraps),
		"byproduct_count": len(byproducts),
		"fg_count": len(fgs),
		"poisoned_sources": poisoned_sources,
		"sources": sources,
		"classification": MANUAL,
		"confidence": CONFIDENCE_MANUAL,
		"eligible": False,
		"fg_rows": [],
		"reason": "",
		"source_of_truth": "historical_manufacture_consumed_svd",
	}

	if not fgs:
		out["reason"] = "Manufacture has no finished-item row"
		return out
	if poisoned_sources:
		out["classification"] = WAITING_UPSTREAM
		out["confidence"] = CONFIDENCE_AMBIGUOUS
		out["reason"] = (
			f"{poisoned_sources} source row(s) moved qty with zero SLE value — "
			"repair upstream raw-material/MTFM valuation first"
		)
		out["patient_zero"] = _earliest_source_root(sources, se.work_order)
		return out
	if len(fgs) != 1:
		out["classification"] = MANUAL
		out["confidence"] = CONFIDENCE_MANUAL
		out["reason"] = f"multi-FG Manufacture ({len(fgs)}) — stage/co-product allocation not auto-READY"
		return out

	fg = fgs[0]
	d = fg["detail"]
	sle = fg["sle"]
	fg_qty = flt(d.qty)
	if fg_qty <= QTY_EPS:
		out["reason"] = "FG qty is zero"
		return out

	additional_costs = flt(se.total_additional_costs)
	fg_additional = flt(getattr(d, "additional_cost", None) or 0)
	# ERPNext get_basic_rate_for_manufactured_item: (outgoing − costed-out basic) / qty
	# Additional costs are NOT part of basic_rate; they land on SLE/amount separately.
	residual = consumed_value - scrap_value - byproduct_value
	# Legitimate zero manufacture: no consumption value and no scrap/by-product value
	if abs(residual) <= VALUE_EPS and abs(consumed_value) <= VALUE_EPS:
		expected = 0.0
		current = flt(d.basic_rate)
		sle_rate = flt(sle.incoming_rate) if sle else current
		out["fg_rows"] = [
			{
				"voucher_detail": d.name,
				"item": d.item_code,
				"warehouse": d.t_warehouse,
				"qty": fg_qty,
				"current_basic_rate": current,
				"current_sle_rate": sle_rate,
				"expected_rate": expected,
				"expected_amount": 0.0,
			}
		]
		if abs(current) <= RATE_EPS and abs(sle_rate) <= RATE_EPS:
			out["classification"] = LEGITIMATE
			out["confidence"] = CONFIDENCE_EXACT
			out["eligible"] = False
			out["reason"] = "legitimate zero FG — zero consumption pool"
			return out
		# FG has nonzero rate but economics are zero — suspicious
		out["classification"] = RECONSTRUCTABLE
		out["confidence"] = CONFIDENCE_LIKELY
		out["eligible"] = False
		out["reason"] = "FG rate nonzero while consumption pool is zero — needs review"
		return out

	if residual < -VALUE_EPS:
		out["classification"] = MANUAL
		out["reason"] = (
			f"scrap/by-product value {scrap_value + byproduct_value} exceeds consumption {consumed_value}"
		)
		return out

	expected = residual / fg_qty
	current = flt(d.basic_rate)
	sle_rate = flt(sle.incoming_rate) if sle else current
	# SLE / stock value may include this row's additional_cost (not in basic_rate).
	expected_sle_amount = expected * fg_qty + fg_additional
	expected_sle_rate = expected_sle_amount / fg_qty if fg_qty else expected
	current_sle_svd = flt(sle.stock_value_difference) if sle else flt(d.amount)
	row = {
		"voucher_detail": d.name,
		"sle": sle.name if sle else None,
		"item": d.item_code,
		"warehouse": d.t_warehouse,
		"qty": fg_qty,
		"current_basic_rate": current,
		"current_sle_rate": sle_rate,
		"expected_rate": expected,
		"expected_amount": expected * fg_qty,
		"expected_sle_rate": expected_sle_rate,
		"expected_sle_amount": expected_sle_amount,
		"fg_additional_cost": fg_additional,
		"difference": expected - current,
	}
	out["fg_rows"] = [row]
	out["expected_target_rate"] = expected
	out["expected_target_value"] = expected * fg_qty
	out["current_target_rate"] = current
	out["additional_costs"] = additional_costs

	# HEALTHY: SE basic matches residual; SLE amount matches residual+additional.
	se_ok = abs(current - expected) <= RATE_TOL
	sle_ok = sle is None or (
		abs(sle_rate - expected_sle_rate) <= RATE_TOL
		or abs(current_sle_svd - expected_sle_amount) <= max(VALUE_EPS, abs(expected_sle_amount) * 1e-9)
	)
	if se_ok and sle_ok:
		out["classification"] = HEALTHY
		out["confidence"] = CONFIDENCE_EXACT
		out["eligible"] = False
		out["reason"] = "FG already matches consumed-SVD residual rate"
		out["se_matches_residual"] = True
		out["sle_matches_residual"] = True
		return out

	# Deterministic repair candidate
	out["classification"] = EXACT
	out["confidence"] = CONFIDENCE_EXACT
	out["eligible"] = True
	out["se_matches_residual"] = se_ok
	out["sle_matches_residual"] = sle_ok
	out["reason"] = (
		f"FG rate {current} ≠ consumed-SVD residual rate {expected:.6f} "
		f"(consumed={consumed_value:.2f}, scrap={scrap_value:.2f}, "
		f"byproduct={byproduct_value:.2f}, additional={additional_costs:.2f}, qty={fg_qty}"
		+ ("; SE matches but SLE drifted" if se_ok and not sle_ok else "")
		+ ("; SLE matches value but SE basic drifted" if sle_ok and not se_ok else "")
		+ ")"
	)
	return out


def apply_manufacture_valuation_to_row(row: dict, *, cache: dict | None = None) -> dict:
	"""Stamp a Wrong/Zero Rate scan row using manufacture-native reconstruction."""
	purpose = str(row.get("purpose") or "")
	if purpose != "Manufacture":
		return row
	voucher = row.get("voucher") or row.get("parent")
	if not voucher:
		return row
	cache = cache if cache is not None else {}
	key = f"mfg_native:{voucher}"
	if key not in cache:
		cache[key] = reconstruct_manufacture_valuation(voucher)
	ev = cache[key]
	row = dict(row)
	row["manufacture_native"] = ev
	cls = ev.get("classification")
	if cls == WAITING_UPSTREAM:
		row["status"] = "DEPENDENCY_REPAIR_REQUIRED"
		row["confidence"] = CONFIDENCE_AMBIGUOUS
		row["eligible"] = False
		row["proposed_rate"] = 0.0
		row["source_of_truth"] = "waiting_upstream_manufacture_source"
		row["wrong_reason"] = "WAITING_MANUFACTURE_INPUTS"
		row["manual_lane"] = WAITING_UPSTREAM
		row["message"] = ev.get("reason")
		if ev.get("patient_zero"):
			row["patient_zero"] = ev["patient_zero"]
		row["kpi_bucket"] = "waiting"
		row["planner_status"] = "WAITING_RATE_DEPENDENCY"
		return row
	if cls == LEGITIMATE:
		row["status"] = "NO_ACTION_REQUIRED"
		row["confidence"] = CONFIDENCE_EXACT
		row["eligible"] = False
		row["no_action_required"] = True
		row["source_of_truth"] = ev.get("source_of_truth")
		row["message"] = ev.get("reason")
		row["kpi_bucket"] = "legitimate"
		row["manual_lane"] = None
		row["planner_status"] = "RATE_REPAIR_COMPLETE"
		return row
	if cls == HEALTHY:
		row["status"] = "NO_ACTION_REQUIRED"
		row["confidence"] = CONFIDENCE_EXACT
		row["eligible"] = False
		row["no_action_required"] = True
		row["source_of_truth"] = ev.get("source_of_truth")
		row["message"] = ev.get("reason")
		row["kpi_bucket"] = "complete"
		row["manual_lane"] = None
		row["planner_status"] = "RATE_REPAIR_COMPLETE"
		return row
	if cls == EXACT and ev.get("eligible"):
		# Only stamp FG detail rows
		fg_items = {r["item"] for r in (ev.get("fg_rows") or [])}
		item = row.get("item") or row.get("item_code")
		if item and fg_items and item not in fg_items:
			row["status"] = "MANUAL_REVIEW"
			row["confidence"] = CONFIDENCE_MANUAL
			row["eligible"] = False
			row["message"] = "Manufacture source/scrap row — repair via FG residual contract on finished item"
			return row
		exp = flt(ev.get("expected_target_rate"))
		row["proposed_rate"] = exp
		row["expected"] = exp
		row["expected_rate"] = exp
		row["source_of_truth"] = ev.get("source_of_truth")
		row["rate_source"] = ev.get("source_of_truth")
		row["confidence"] = CONFIDENCE_EXACT
		row["status"] = "RECONSTRUCTABLE"
		row["eligible"] = True
		row["difference"] = exp - flt(row.get("current_rate") or row.get("current") or 0)
		row["message"] = ev.get("reason")
		row["wrong_reason"] = "MANUFACTURE_NATIVE_FG_RESIDUAL"
		row["kpi_bucket"] = "ready"
		# Prefer FG detail identity
		if ev.get("fg_rows"):
			fr = ev["fg_rows"][0]
			row.setdefault("voucher_detail", fr.get("voucher_detail"))
			row.setdefault("sle", fr.get("sle"))
			row["warehouse"] = fr.get("warehouse") or row.get("warehouse")
		return row
	row["status"] = "MANUAL_REVIEW"
	row["confidence"] = ev.get("confidence") or CONFIDENCE_MANUAL
	row["eligible"] = False
	row["message"] = ev.get("reason")
	row["wrong_reason"] = "MANUAL_MANUFACTURE_NATIVE"
	return row


def repair_manufacture_valuation(voucher: str, *, dry_run: bool = True) -> dict:
	"""Write FG SE detail + SLE to manufacture-native residual rate. Qty untouched."""
	ev = reconstruct_manufacture_valuation(voucher)
	if not ev.get("eligible") or ev.get("classification") != EXACT:
		return {
			"ok": False,
			"aborted": True,
			"dry_run": dry_run,
			"reason": ev.get("reason") or "not eligible",
			"evidence": ev,
		}
	fg = (ev.get("fg_rows") or [None])[0]
	if not fg:
		return {"ok": False, "aborted": True, "dry_run": dry_run, "reason": "no FG row"}
	expected = flt(fg["expected_rate"])
	qty = flt(fg["qty"])
	fg_additional = flt(fg.get("fg_additional_cost") or 0)
	# When SE already matches residual basic, sync SLE to residual+additional (SE amount economics).
	write_basic = (
		flt(fg["current_basic_rate"])
		if ev.get("se_matches_residual") and abs(flt(fg["current_basic_rate"])) > RATE_EPS
		else expected
	)
	write_sle_amount = abs(write_basic) * abs(qty) + abs(fg_additional)
	write_sle_rate = write_sle_amount / abs(qty) if abs(qty) > QTY_EPS else write_basic
	se_write = not bool(ev.get("se_matches_residual"))
	preview = {
		"voucher": voucher,
		"item": fg["item"],
		"warehouse": fg["warehouse"],
		"current_basic_rate": fg["current_basic_rate"],
		"current_sle_rate": fg["current_sle_rate"],
		"expected_rate": expected,
		"write_basic_rate": write_basic,
		"write_sle_rate": write_sle_rate,
		"write_sle_amount": write_sle_amount,
		"voucher_detail": fg["voucher_detail"],
		"sle": fg.get("sle"),
		"se_write": se_write,
		"sle_only_sync": not se_write,
	}
	if dry_run:
		return {"ok": True, "dry_run": True, "preview": preview, "evidence": ev, "economic_writes": 0}

	if se_write:
		frappe.db.set_value(
			"Stock Entry Detail",
			fg["voucher_detail"],
			{
				"basic_rate": abs(write_basic),
				"valuation_rate": abs(write_basic),
				"basic_amount": abs(write_basic) * abs(qty),
				# Keep document amount = basic + additional (ERPNext shape).
				"amount": write_sle_amount,
			},
			update_modified=False,
		)
	if fg.get("sle"):
		frappe.db.set_value(
			"Stock Ledger Entry",
			fg["sle"],
			{
				"incoming_rate": abs(write_sle_rate),
				"outgoing_rate": 0,
				"valuation_rate": abs(write_sle_rate),
				"stock_value_difference": write_sle_amount,
			},
			update_modified=False,
		)
	frappe.db.commit()
	return {
		"ok": True,
		"dry_run": False,
		"written": True,
		"economic_writes": 1,
		"preview": preview,
		"evidence": ev,
		"path": "manufacture_native_fg_residual",
		"riv": "NOT_INVOKED",
	}


def _is_scrap_like(d) -> bool:
	if cint(getattr(d, "is_scrap_item", None) if not isinstance(d, dict) else d.get("is_scrap_item")):
		return True
	sec = ""
	if isinstance(d, dict):
		sec = str(d.get("secondary_item_type") or "")
	else:
		sec = str(getattr(d, "secondary_item_type", None) or "")
	return sec.lower() == "scrap"


def _is_byproduct_like(d) -> bool:
	sec = ""
	if isinstance(d, dict):
		sec = str(d.get("secondary_item_type") or "")
	else:
		sec = str(getattr(d, "secondary_item_type", None) or "")
	return "by-product" in sec.lower() or sec.lower() == "byproduct"


def _incoming_secondary_value(d, sle) -> float:
	"""Prefer SLE inbound SVD; fall back to SE amount when SLE value is missing/zero."""
	if sle and abs(flt(sle.stock_value_difference)) > VALUE_EPS:
		return abs(flt(sle.stock_value_difference))
	return abs(flt(d.amount))


def _earliest_source_root(sources: list[dict], work_order) -> dict | None:
	for s in sources:
		if s.get("zero_poison"):
			return {"voucher_no": None, "item": s.get("item"), "reason": "zero_source_on_manufacture", "work_order": work_order}
	return None
