# Copyright (c) 2026, ERPNext Extensions contributors
"""Historical Manufacture equal-rate By-Product via Iran stage-equivalent pool.

Business contract (generic — no item hard-codes):

	stage By-Product equivalent-unit rate == MAIN_FG equivalent-unit rate

Implemented by clearing a failed Core ``Valuation Rate`` auto-default on a
zero-rate Manufacture By-Product, stamping the current manufacture contract,
and calling :func:`allocate_stage_output_cost`. Quantities are never mutated.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
	allocate_stage_output_cost,
	assert_bridged_stage_outputs_priced,
	clear_core_auto_valuation_for_stage_bridge,
	clear_core_vr_auto_default_for_zero_byproduct,
	is_stage_equivalent_output_candidate,
	permit_stage_equivalent_zero_valuation,
)
from erpnext_extensions.iran_accounting.scrap_costing import (
	MANUFACTURE_COSTING_CONTRACT_VERSION,
	_is_incoming,
	secondary_item_type_of,
)
from erpnext_extensions.iran_accounting.stock_entry import persist_irr_stock_entry_header_and_rows


STRATEGY = "MANUFACTURE_STAGE_EQUAL_RATE_BYPRODUCT"


def _main_fg_rows(doc):
	return [
		row
		for row in (doc.get("items") or [])
		if cint_safe(row.get("is_finished_item")) and row.get("t_warehouse")
	]


def cint_safe(v) -> int:
	try:
		return int(v or 0)
	except (TypeError, ValueError):
		return 0


def find_zero_rate_stage_byproducts(doc) -> list:
	"""Incoming By-Product / Co-Product / Additional FG with non-positive rate."""
	from erpnext_extensions.iran_accounting.scrap_costing import STAGE_SECONDARY_TYPES

	items = doc.get("items") if hasattr(doc, "get") else getattr(doc, "items", None)
	out = []
	for row in items or []:
		if not _is_incoming(row):
			continue
		if secondary_item_type_of(row) not in STAGE_SECONDARY_TYPES:
			continue
		if flt(row.get("qty") or row.get("transfer_qty")) <= 0:
			continue
		if flt(row.get("basic_rate")) > 0 and flt(row.get("basic_amount")) > 0:
			continue
		out.append(row)
	return out


def analyze_equal_rate_byproduct(voucher_no: str) -> dict:
	"""Read-only plan for stage equal-rate By-Product repair."""
	doc = frappe.get_doc("Stock Entry", voucher_no)
	if doc.purpose != "Manufacture" or cint_safe(doc.docstatus) != 1:
		return {"ok": False, "reason": "not_submitted_manufacture", "strategy": STRATEGY}
	fg_rows = _main_fg_rows(doc)
	if len(fg_rows) != 1:
		return {
			"ok": False,
			"reason": "need_exactly_one_main_fg",
			"fg_count": len(fg_rows),
			"strategy": STRATEGY,
		}
	fg = fg_rows[0]
	candidates = find_zero_rate_stage_byproducts(doc)
	if not candidates:
		return {"ok": False, "reason": "no_zero_rate_stage_byproduct", "strategy": STRATEGY}

	qty_fingerprint = {
		row.idx: {
			"item_code": row.item_code,
			"qty": flt(row.qty),
			"secondary_item_type": secondary_item_type_of(row),
			"valuation_type": row.valuation_type,
			"basic_rate": flt(row.basic_rate),
		}
		for row in candidates
	}
	return {
		"ok": True,
		"strategy": STRATEGY,
		"voucher": voucher_no,
		"work_order": doc.work_order,
		"job_card": doc.job_card,
		"main_fg": {
			"item_code": fg.item_code,
			"qty": flt(fg.qty),
			"basic_rate": flt(fg.basic_rate),
			"valuation_rate": flt(fg.valuation_rate),
			"amount": flt(fg.amount),
		},
		"candidates": qty_fingerprint,
		"contract_stamp_target": MANUFACTURE_COSTING_CONTRACT_VERSION,
		"quantity_mutation": False,
		"authority": "iran_accounting.manufacture_stage_costing.allocate_stage_output_cost",
	}


def dry_run_equal_rate_byproduct(voucher_no: str) -> dict:
	"""In-memory stage allocation; no DB write."""
	plan = analyze_equal_rate_byproduct(voucher_no)
	if not plan.get("ok"):
		return plan
	doc = frappe.get_doc("Stock Entry", voucher_no)
	doc._iran_historical_stage_repair = True
	doc.set(
		"custom_manufacturing_costing_contract_version",
		MANUFACTURE_COSTING_CONTRACT_VERSION,
	)
	cleared = []
	for row in find_zero_rate_stage_byproducts(doc):
		if clear_core_vr_auto_default_for_zero_byproduct(doc, row):
			cleared.append(row.item_code)
	permit_stage_equivalent_zero_valuation(doc)
	clear_core_auto_valuation_for_stage_bridge(doc)
	# Re-clear after permit in case Core VR default was re-asserted conceptually.
	for row in find_zero_rate_stage_byproducts(doc):
		clear_core_vr_auto_default_for_zero_byproduct(doc, row)
		if not is_stage_equivalent_output_candidate(doc, row):
			# Force bridge mark after VR clear for historical origin path.
			setattr(row, "_iran_stage_equivalent_bridge", True)
			row.allow_zero_valuation_rate = 1

	applied = allocate_stage_output_cost(doc)
	try:
		assert_bridged_stage_outputs_priced(doc)
		assert_ok = True
		assert_err = None
	except Exception as exc:  # noqa: BLE001 — dry-run surfaces the throw
		assert_ok = False
		assert_err = str(exc)

	fg = _main_fg_rows(doc)[0]
	after = []
	for row in doc.get("items") or []:
		if secondary_item_type_of(row) in (
			"By-Product",
			"Co-Product",
			"Additional Finished Good",
		) and _is_incoming(row):
			after.append(
				{
					"item_code": row.item_code,
					"qty": flt(row.qty),
					"basic_rate": flt(row.basic_rate),
					"basic_amount": flt(row.basic_amount),
					"valuation_rate": flt(row.valuation_rate),
					"amount": flt(row.amount),
					"additional_cost": flt(row.additional_cost),
				}
			)
	return {
		**plan,
		"dry_run": True,
		"cleared_vr": cleared,
		"allocate_applied": bool(applied),
		"assert_priced_ok": assert_ok,
		"assert_err": assert_err,
		"after_fg": {
			"item_code": fg.item_code,
			"basic_rate": flt(fg.basic_rate),
			"valuation_rate": flt(fg.valuation_rate),
			"amount": flt(fg.amount),
			"additional_cost": flt(fg.additional_cost),
		},
		"after_secondaries": after,
	}


def apply_equal_rate_byproduct(voucher_no: str, *, dry_run: bool = True) -> dict:
	"""Apply stage equal-rate By-Product repair. Default dry-run.

	When ``dry_run=False``: persist SE economics via Iran finalize path, then
	caller must run native RIV for the affected item-warehouse pairs.
	"""
	preview = dry_run_equal_rate_byproduct(voucher_no)
	if dry_run or not preview.get("ok") or not preview.get("allocate_applied"):
		return {**preview, "applied": False}
	if not preview.get("assert_priced_ok"):
		return {**preview, "applied": False, "reason": "bridged_outputs_not_priced"}

	# Quantity fingerprint gate — refuse any qty drift.
	doc = frappe.get_doc("Stock Entry", voucher_no)
	before_qty = {
		(row.item_code, row.idx): flt(row.qty) for row in (doc.get("items") or [])
	}
	doc._iran_historical_stage_repair = True
	doc.db_set(
		"custom_manufacturing_costing_contract_version",
		MANUFACTURE_COSTING_CONTRACT_VERSION,
		update_modified=False,
	)
	doc.set(
		"custom_manufacturing_costing_contract_version",
		MANUFACTURE_COSTING_CONTRACT_VERSION,
	)
	for row in find_zero_rate_stage_byproducts(doc):
		clear_core_vr_auto_default_for_zero_byproduct(doc, row)
	permit_stage_equivalent_zero_valuation(doc)
	clear_core_auto_valuation_for_stage_bridge(doc)
	for row in find_zero_rate_stage_byproducts(doc):
		clear_core_vr_auto_default_for_zero_byproduct(doc, row)
		setattr(row, "_iran_stage_equivalent_bridge", True)
		row.allow_zero_valuation_rate = 1
	if not allocate_stage_output_cost(doc):
		return {**preview, "applied": False, "reason": "allocate_stage_output_cost_false"}
	assert_bridged_stage_outputs_priced(doc)
	after_qty = {
		(row.item_code, row.idx): flt(row.qty) for row in (doc.get("items") or [])
	}
	if before_qty != after_qty:
		frappe.throw(f"Quantity fingerprint changed on {voucher_no} — aborting equal-rate repair")

	persist_irr_stock_entry_header_and_rows(doc)
	frappe.db.commit()
	return {
		**preview,
		"applied": True,
		"persisted": True,
		"quantity_fingerprint_ok": True,
	}
