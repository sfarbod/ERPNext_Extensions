# Copyright (c) 2026, ERPNext Extensions contributors
"""Historical Manufacture repair via CURRENT Iran Accounting native contract.

Does not invent rates. Reconstructs minimum contract/classification state, then
runs :func:`apply_iran_manufacture_output_contract` so Product Reject, Component
Scrap, Stage Co-/By-Product and AFG follow their class-specific rules.

Quantities are never mutated.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_EXACT,
	CONFIDENCE_MANUAL,
	HISTORICAL_REPAIR_FLAG,
	QTY_EPS,
	RATE_EPS,
)
from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
	clear_core_auto_valuation_for_stage_bridge,
	permit_stage_equivalent_zero_valuation,
	uses_v533_contract,
)
from erpnext_extensions.iran_accounting.scrap_costing import (
	MANUFACTURE_COSTING_CONTRACT_VERSION,
	STAGE_SECONDARY_TYPES,
	apply_iran_manufacture_output_contract,
	clear_core_auto_valuation_for_product_reject_bridge,
	permit_product_reject_zero_valuation,
	secondary_item_type_of,
)
from erpnext_extensions.iran_accounting.stock_entry import persist_irr_stock_entry_header_and_rows

STRATEGY = "IRAN_NATIVE_HISTORICAL"
ALREADY_HEALTHY = "ALREADY_HEALTHY"
EXACT_REPAIRABLE = "EXACT_REPAIRABLE"
WAITING_UPSTREAM = "WAITING_UPSTREAM"
MANUAL = "MANUAL"
RATE_DELTA_EPS = 1.0


def _qty_fingerprint(doc) -> dict:
	return {(row.item_code, int(row.idx)): flt(row.qty) for row in (doc.get("items") or [])}


def _rate_snapshot(doc) -> dict[str, dict]:
	out = {}
	for row in doc.get("items") or []:
		out[row.name] = {
			"item_code": row.item_code,
			"basic_rate": flt(row.basic_rate),
			"basic_amount": flt(row.basic_amount),
			"valuation_rate": flt(row.valuation_rate),
			"amount": flt(row.amount),
			"additional_cost": flt(row.additional_cost),
			"custom_output_class": row.custom_output_class,
			"valuation_type": row.valuation_type,
			"secondary_item_type": secondary_item_type_of(row),
			"is_finished_item": cint(row.is_finished_item),
			"qty": flt(row.qty),
		}
	return out


def _prepare_historical_doc(doc) -> list[str]:
	"""Stamp + bridge flags for historical adoption. Returns notes."""
	notes = []
	doc._iran_historical_stage_repair = True
	if not doc.get("custom_manufacturing_costing_contract_version"):
		doc.set("custom_manufacturing_costing_contract_version", MANUFACTURE_COSTING_CONTRACT_VERSION)
		notes.append("stamp_set")
	elif str(doc.get("custom_manufacturing_costing_contract_version")) < "5.3.34":
		# Keep legacy stamp semantics if already present below TYPE C; still allow repair flag.
		notes.append(f"stamp_kept:{doc.get('custom_manufacturing_costing_contract_version')}")
	permit_product_reject_zero_valuation(doc)
	permit_stage_equivalent_zero_valuation(doc)
	clear_core_auto_valuation_for_product_reject_bridge(doc)
	clear_core_auto_valuation_for_stage_bridge(doc)
	notes.append("bridges_cleared")
	return notes


def _families_present(doc) -> list[str]:
	fams = set()
	fg_items = {
		r.item_code
		for r in (doc.get("items") or [])
		if cint(r.is_finished_item) and r.get("t_warehouse")
	}
	for row in doc.get("items") or []:
		oc = str(row.get("custom_output_class") or "")
		sec = secondary_item_type_of(row)
		vt = str(row.get("valuation_type") or "")
		if oc == "MAIN_PRODUCT_REJECT" or (
			sec == "Scrap" and vt == "Valuation Rate" and row.item_code in fg_items
		):
			fams.add("PRODUCT_REJECT")
		elif sec == "Scrap" and vt == "Valuation Rate":
			fams.add("VR_SCRAP")
		elif sec == "By-Product":
			fams.add("BY_PRODUCT")
		elif sec == "Co-Product":
			fams.add("STAGE_CO_PRODUCT")
		elif sec == "Additional Finished Good":
			fams.add("AFG")
		elif oc == "COMPONENT_SCRAP" or (sec == "Scrap" and vt != "Valuation Rate"):
			fams.add("COMPONENT_SCRAP")
		if sec in STAGE_SECONDARY_TYPES:
			fams.add("STAGE_EQUIVALENT")
	return sorted(fams)


def analyze_iran_native_historical(voucher: str) -> dict[str, Any]:
	"""Read-only: would CURRENT Iran contract change economics?"""
	if not voucher or not frappe.db.exists("Stock Entry", voucher):
		return {"ok": False, "classification": MANUAL, "reason": "missing_voucher", "strategy": STRATEGY}
	doc = frappe.get_doc("Stock Entry", voucher)
	if doc.purpose != "Manufacture" or cint(doc.docstatus) != 1:
		return {
			"ok": False,
			"classification": MANUAL,
			"reason": "not_submitted_manufacture",
			"strategy": STRATEGY,
		}

	before = _rate_snapshot(doc)
	qty_before = _qty_fingerprint(doc)
	families = _families_present(doc)
	notes = _prepare_historical_doc(doc)
	# Poisoned sources: outgoing qty with zero SLE value blocks deterministic native replay.
	poisoned = _count_poisoned_sources(voucher)
	if poisoned:
		return {
			"ok": False,
			"classification": WAITING_UPSTREAM,
			"reason": f"{poisoned} source row(s) moved qty with zero SLE value",
			"strategy": STRATEGY,
			"families": families,
			"voucher": voucher,
		}

	try:
		applied = apply_iran_manufacture_output_contract(doc)
	except Exception as exc:  # noqa: BLE001 — analyze must never abort scanners
		return {
			"ok": False,
			"classification": MANUAL,
			"reason": f"iran_contract_assertion: {exc}",
			"strategy": STRATEGY,
			"families": families,
			"voucher": voucher,
			"notes": notes,
			"tool_gap": True,
		}

	qty_after = _qty_fingerprint(doc)
	if qty_before != qty_after:
		return {
			"ok": False,
			"classification": MANUAL,
			"reason": "quantity_fingerprint_changed_in_memory — refuse",
			"strategy": STRATEGY,
			"families": families,
		}

	deltas = []
	for row in doc.get("items") or []:
		b = before[row.name]
		if abs(flt(row.basic_rate) - b["basic_rate"]) > RATE_DELTA_EPS or abs(
			flt(row.basic_amount) - b["basic_amount"]
		) > RATE_DELTA_EPS:
			deltas.append(
				{
					"detail": row.name,
					"item_code": row.item_code,
					"before_basic_rate": b["basic_rate"],
					"after_basic_rate": flt(row.basic_rate),
					"before_basic_amount": b["basic_amount"],
					"after_basic_amount": flt(row.basic_amount),
					"custom_output_class": row.custom_output_class,
					"secondary_item_type": secondary_item_type_of(row),
					"is_finished_item": cint(row.is_finished_item),
				}
			)

	if not deltas:
		return {
			"ok": True,
			"classification": ALREADY_HEALTHY,
			"strategy": STRATEGY,
			"voucher": voucher,
			"families": families,
			"contract_applied": bool(applied),
			"notes": notes,
			"delta_n": 0,
			"authority": "apply_iran_manufacture_output_contract",
			"quantity_mutation": False,
		}

	# FG-only IRR remainder polish after a prior native persist is not a new corruption.
	# Secondaries already match; primary FG holds TYPE-C / stage remainder dust.
	if _fg_remainder_only(deltas):
		return {
			"ok": True,
			"classification": ALREADY_HEALTHY,
			"strategy": STRATEGY,
			"voucher": voucher,
			"families": families,
			"contract_applied": bool(applied),
			"notes": notes + ["EXPECTED_PRIMARY_FG_REMAINDER"],
			"delta_n": 0,
			"fg_remainder_deltas": deltas,
			"authority": "apply_iran_manufacture_output_contract",
			"quantity_mutation": False,
		}

	return {
		"ok": True,
		"classification": EXACT_REPAIRABLE,
		"strategy": STRATEGY,
		"voucher": voucher,
		"families": families,
		"contract_applied": bool(applied),
		"notes": notes,
		"delta_n": len(deltas),
		"deltas": deltas,
		"authority": "apply_iran_manufacture_output_contract",
		"quantity_mutation": False,
		"confidence": CONFIDENCE_EXACT,
	}


def _fg_remainder_only(deltas: list[dict]) -> bool:
	"""True when only MAIN_FG / finished rows differ by small IRR remainder dust."""
	if not deltas:
		return True
	for d in deltas:
		is_fg = cint(d.get("is_finished_item"))
		oc = str(d.get("custom_output_class") or "")
		sec = str(d.get("secondary_item_type") or "")
		if not is_fg and oc != "MAIN_FG":
			return False
		if sec in STAGE_SECONDARY_TYPES or "SCRAP" in oc or "REJECT" in oc:
			return False
		delta = abs(flt(d.get("after_basic_rate")) - flt(d.get("before_basic_rate")))
		# Integer IRR stage/reject remainder polish can move FG by a few thousand.
		if delta > 10000.0:
			return False
	return True


def _count_poisoned_sources(voucher: str) -> int:
	rows = frappe.db.sql(
		"""
		SELECT sed.name, sed.qty, sed.s_warehouse,
		       IFNULL(sle.stock_value_difference,0) svd,
		       IFNULL(sle.outgoing_rate,0) ogr
		FROM `tabStock Entry Detail` sed
		LEFT JOIN `tabStock Ledger Entry` sle
		  ON sle.voucher_detail_no=sed.name AND IFNULL(sle.is_cancelled,0)=0
		WHERE sed.parent=%s AND IFNULL(sed.s_warehouse,'')!=''
		""",
		voucher,
		as_dict=True,
	)
	n = 0
	for r in rows:
		if abs(flt(r.qty)) > QTY_EPS and abs(flt(r.svd)) <= RATE_EPS and abs(flt(r.ogr)) <= RATE_EPS:
			n += 1
	return n


def apply_iran_native_historical(voucher: str, *, dry_run: bool = True) -> dict:
	"""Apply native Iran manufacture contract to a historical submitted SE."""
	plan = analyze_iran_native_historical(voucher)
	if plan.get("classification") == ALREADY_HEALTHY:
		return {**plan, "applied": False, "dry_run": dry_run, "skipped": "already_healthy"}
	if not plan.get("ok") or plan.get("classification") != EXACT_REPAIRABLE:
		return {**plan, "applied": False, "dry_run": dry_run}

	if dry_run:
		return {**plan, "applied": False, "dry_run": True}

	frappe.flags[HISTORICAL_REPAIR_FLAG] = True
	try:
		doc = frappe.get_doc("Stock Entry", voucher)
		qty_before = _qty_fingerprint(doc)
		before = _rate_snapshot(doc)
		notes = _prepare_historical_doc(doc)
		# Persist stamp when missing
		if not frappe.db.get_value(
			"Stock Entry", voucher, "custom_manufacturing_costing_contract_version"
		):
			doc.db_set(
				"custom_manufacturing_costing_contract_version",
				MANUFACTURE_COSTING_CONTRACT_VERSION,
				update_modified=False,
			)
			doc.set(
				"custom_manufacturing_costing_contract_version",
				MANUFACTURE_COSTING_CONTRACT_VERSION,
			)
		try:
			applied_contract = apply_iran_manufacture_output_contract(doc)
		except Exception as exc:  # noqa: BLE001
			return {
				**plan,
				"applied": False,
				"dry_run": False,
				"reason": f"iran_contract_assertion: {exc}",
			}
		if not applied_contract:
			# Contract returned False but analyze saw deltas — refuse rather than half-write.
			return {
				**plan,
				"applied": False,
				"dry_run": False,
				"reason": "apply_iran_manufacture_output_contract_false",
			}
		if _qty_fingerprint(doc) != qty_before:
			frappe.throw(f"Quantity fingerprint changed on {voucher} — abort Iran native historical")

		persist_irr_stock_entry_header_and_rows(doc)
		# Sync SLE rates from SE for changed items (native RIV rebuilds descendants).
		changed_items = sorted(
			{
				d["item_code"]
				for d in (plan.get("deltas") or [])
				if abs(d["after_basic_rate"] - d["before_basic_rate"]) > RATE_DELTA_EPS
			}
		)
		# Recompute changed from persisted doc
		changed_items = []
		for row in doc.get("items") or []:
			b = before.get(row.name) or {}
			if abs(flt(row.basic_rate) - flt(b.get("basic_rate"))) > RATE_DELTA_EPS:
				changed_items.append(row.item_code)
		changed_items = sorted(set(changed_items))
		from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
			sync_sle_from_stock_entry_detail,
		)

		for item in changed_items:
			try:
				sync_sle_from_stock_entry_detail(voucher, item)
			except Exception:
				pass
		frappe.db.commit()
		return {
			**plan,
			"applied": True,
			"dry_run": False,
			"persisted": True,
			"notes": notes,
			"changed_items": changed_items,
			"quantity_fingerprint_ok": True,
			"riv": "NOT_INVOKED",
		}
	finally:
		frappe.flags[HISTORICAL_REPAIR_FLAG] = False


def stamp_wrong_rate_row_from_iran_native(row: dict, evidence: dict | None = None) -> dict:
	"""Stamp a Wrong Rate scan row from iran-native analyze evidence."""
	row = dict(row or {})
	ev = evidence or analyze_iran_native_historical(row.get("voucher") or row.get("voucher_no") or "")
	cls = ev.get("classification")
	if cls == ALREADY_HEALTHY:
		row["status"] = "NO_ACTION_REQUIRED"
		row["confidence"] = CONFIDENCE_EXACT
		row["eligible"] = False
		row["actionable"] = False
		row["no_action_required"] = True
		row["source_of_truth"] = "iran_native_already_healthy"
		row["rate_source"] = "iran_native_already_healthy"
		row["manual_lane"] = None
		row["kpi_bucket"] = "NO_ACTION"
		row["planner_status"] = "RATE_REPAIR_COMPLETE"
		row["message"] = "Iran native contract reproduces current SE economics — not a tool gap"
		row["iran_native"] = ev
		return row
	if cls == EXACT_REPAIRABLE:
		row["status"] = "RECONSTRUCTABLE"
		row["confidence"] = CONFIDENCE_EXACT
		row["eligible"] = True
		row["actionable"] = True
		row["source_of_truth"] = "iran_native_historical"
		row["rate_source"] = "iran_native_historical"
		row["repair_strategy"] = STRATEGY
		row["manual_lane"] = None
		row["kpi_bucket"] = "ZERO_RATE_RECONSTRUCTABLE"
		row["planner_status"] = "READY_WRONG_RATE"
		row["message"] = (
			f"Iran native historical repair — families={ev.get('families')} deltas={ev.get('delta_n')}"
		)
		row["iran_native"] = ev
		# Do not set proposed_rate from SVD — apply path must call native contract.
		row["proposed_rate"] = 0.0
		row["sql_updates"] = max(1, int(ev.get("delta_n") or 1))
		return row
	if cls == WAITING_UPSTREAM:
		row["status"] = "DEPENDENCY_REPAIR_REQUIRED"
		row["confidence"] = CONFIDENCE_MANUAL
		row["eligible"] = False
		row["manual_lane"] = "WAITING_UPSTREAM"
		row["kpi_bucket"] = "ZERO_RATE_WAITING_UPSTREAM"
		row["planner_status"] = "WAITING_RATE_DEPENDENCY"
		row["message"] = ev.get("reason")
		row["iran_native"] = ev
		return row
	row["status"] = "MANUAL_REVIEW"
	row["confidence"] = CONFIDENCE_MANUAL
	row["eligible"] = False
	row["manual_lane"] = "TOOL_LIMIT"
	row["kpi_bucket"] = "TECHNICAL_TOOL_GAP"
	row["planner_status"] = "RATE_MANUAL"
	row["message"] = ev.get("reason") or "Iran native historical unsupported"
	row["iran_native"] = ev
	return row
