# Copyright (c) 2026, ERPNext Extensions contributors
"""Bench-only unlock of Manufacture output manual-rate locks (metadata only).

Clears ``set_basic_rate_manually`` on submitted Manufacture output rows so native
RIV can recalculate through the existing Iran Accounting engine.

Does not change rates, amounts, SLE, GL, Additional Cost, or Stock Adjustment.
Does not invoke RIV. Bulk Scrap (Iran ``CLASS_BULK_SCRAP``) is never unlocked.
Does not create DocTypes or a pricing engine.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
	CONTRACT_VERSION_FIELD,
	EQUIV_FACTOR_FIELD,
	uses_v533_contract,
)
from erpnext_extensions.iran_accounting.scrap_costing import (
	CLASS_BULK_SCRAP,
	CLASS_CO_PRODUCT,
	CLASS_OTHER_OUTPUT,
	MANUFACTURE_COSTING_CONTRACT_VERSION,
	OUTPUT_CLASSES,
	classify_manufacture_outputs,
	secondary_item_type_of,
)

DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 500
OUTPUT_CLASS_FIELD = "custom_output_class"


class UnlockManufactureError(frappe.ValidationError):
	"""Fail-closed unlock error."""


def _normalize_batch_size(batch_size) -> int:
	size = cint(batch_size) if batch_size not in (None, "") else DEFAULT_BATCH_SIZE
	if size <= 0:
		raise UnlockManufactureError(f"batch_size must be positive, got {batch_size!r}.")
	if size > MAX_BATCH_SIZE:
		raise UnlockManufactureError(
			f"batch_size {size} exceeds hard maximum {MAX_BATCH_SIZE}."
		)
	return size


def _is_incoming_output(row) -> bool:
	return bool(row.get("t_warehouse")) and not row.get("s_warehouse")


def _list_manufacture_names(stock_entry: str | None) -> list[str]:
	if stock_entry:
		purpose, docstatus = frappe.db.get_value(
			"Stock Entry", stock_entry, ["purpose", "docstatus"]
		) or (None, None)
		if purpose != "Manufacture" or cint(docstatus) != 1:
			raise UnlockManufactureError(
				f"{stock_entry!r} is not a submitted Manufacture Stock Entry "
				f"(purpose={purpose!r}, docstatus={docstatus!r})."
			)
		return [stock_entry]
	return frappe.get_all(
		"Stock Entry",
		filters={"purpose": "Manufacture", "docstatus": 1},
		pluck="name",
		order_by="posting_date asc, posting_time asc, creation asc, name asc",
		limit_page_length=0,
	)


def adopt_manufacture_contract_version(
	stock_entry: str,
	version: str | None = None,
	*,
	dry_run: bool = True,
) -> dict[str, Any]:
	"""Stamp historical Manufacture with an allowed Iran contract version (SE only).

	Does not modify Job Cards. Required before stage Co-Product unlock on
	submitted vouchers that lack ``custom_manufacturing_costing_contract_version``.
	"""
	version = (version or MANUFACTURE_COSTING_CONTRACT_VERSION).strip()
	if not version:
		raise UnlockManufactureError("contract version is required")
	_list_manufacture_names(stock_entry)
	doc = frappe.get_doc("Stock Entry", stock_entry)
	before = str(doc.get(CONTRACT_VERSION_FIELD) or "").strip() or None
	already = uses_v533_contract(doc)
	out = {
		"stock_entry": stock_entry,
		"field": CONTRACT_VERSION_FIELD,
		"before": before,
		"after": version,
		"already_v533": already,
		"dry_run": bool(dry_run),
		"changed": before != version,
	}
	if not dry_run and before != version:
		frappe.db.set_value(
			"Stock Entry",
			stock_entry,
			CONTRACT_VERSION_FIELD,
			version,
			update_modified=False,
		)
		frappe.db.commit()
		frappe.clear_document_cache("Stock Entry", stock_entry)
	print(json.dumps(out, default=str), flush=True)
	return out


def stamp_bulk_scrap_output_class(
	stock_entry: str,
	item_code: str,
	*,
	dry_run: bool = True,
) -> dict[str, Any]:
	"""Mark an incoming Scrap row as Iran BULK_SCRAP (preserve Manual/zero policy).

	Only sets ``custom_output_class``. Does not clear manual-rate locks or rates.
	"""
	_list_manufacture_names(stock_entry)
	doc = frappe.get_doc("Stock Entry", stock_entry)
	matches = [
		r
		for r in (doc.items or [])
		if r.item_code == item_code and _is_incoming_output(r)
	]
	if not matches:
		raise UnlockManufactureError(
			f"{stock_entry}: no incoming row for item {item_code!r}"
		)
	if len(matches) != 1:
		raise UnlockManufactureError(
			f"{stock_entry}: expected one incoming row for {item_code!r}, found {len(matches)}"
		)
	row = matches[0]
	before = str(row.get(OUTPUT_CLASS_FIELD) or "").strip() or None
	out = {
		"stock_entry": stock_entry,
		"row_name": row.name,
		"item_code": item_code,
		"secondary_item_type": secondary_item_type_of(row),
		"class_before": before,
		"class_after": CLASS_BULK_SCRAP,
		"set_basic_rate_manually": cint(row.set_basic_rate_manually),
		"valuation_type": row.valuation_type or "",
		"basic_rate": row.basic_rate,
		"allow_zero_valuation_rate": cint(row.allow_zero_valuation_rate),
		"dry_run": bool(dry_run),
		"changed": before != CLASS_BULK_SCRAP,
	}
	if not dry_run and before != CLASS_BULK_SCRAP:
		frappe.db.set_value(
			"Stock Entry Detail",
			row.name,
			OUTPUT_CLASS_FIELD,
			CLASS_BULK_SCRAP,
			update_modified=False,
		)
		frappe.db.commit()
		frappe.clear_document_cache("Stock Entry", stock_entry)
	print(json.dumps(out, default=str), flush=True)
	return out


def reclassify_se_scrap_as_by_product(
	stock_entry: str,
	item_code: str,
	*,
	equivalent_factor: float = 1.0,
	dry_run: bool = True,
) -> dict[str, Any]:
	"""Correct Manufacture SE row Scrap→By-Product for Iran stage Co-Product path.

	Updates Stock Entry Detail only (secondary type, output class, equiv factor).
	Does **not** modify Job Card history. Job Card may still show Scrap; stage
	origin is satisfied via stamped ``custom_output_class=CO_PRODUCT`` + contract.

	``equivalent_factor`` is the stage field multiplier on top of UOM physical
	conversion (``qty × physical × factor``). When FG stock UOM already encodes
	pack size (e.g. BOX(2pfs)→2 سرنگ), business “0.5 FG unit per piece” is
	realized by physical conversion with factor ``1.0`` — do not also stamp
	``0.5`` or the engine double-counts to 0.25× FG rate.
	"""
	_list_manufacture_names(stock_entry)
	factor = flt(equivalent_factor)
	if factor <= 0:
		raise UnlockManufactureError("equivalent_factor must be > 0")
	doc = frappe.get_doc("Stock Entry", stock_entry)
	matches = [
		r
		for r in (doc.items or [])
		if r.item_code == item_code and _is_incoming_output(r)
	]
	if len(matches) != 1:
		raise UnlockManufactureError(
			f"{stock_entry}: expected one incoming row for {item_code!r}, found {len(matches)}"
		)
	row = matches[0]
	sit_before = secondary_item_type_of(row)
	out = {
		"stock_entry": stock_entry,
		"row_name": row.name,
		"item_code": item_code,
		"job_card": doc.job_card,
		"secondary_item_type_before": sit_before,
		"secondary_item_type_after": "By-Product",
		"output_class_before": str(row.get(OUTPUT_CLASS_FIELD) or "").strip() or None,
		"output_class_after": CLASS_CO_PRODUCT,
		"equivalent_factor_before": row.get(EQUIV_FACTOR_FIELD),
		"equivalent_factor_after": factor,
		"dry_run": bool(dry_run),
		"job_card_untouched": True,
	}
	if not dry_run:
		frappe.db.set_value(
			"Stock Entry Detail",
			row.name,
			{
				"secondary_item_type": "By-Product",
				OUTPUT_CLASS_FIELD: CLASS_CO_PRODUCT,
				EQUIV_FACTOR_FIELD: factor,
			},
			update_modified=False,
		)
		frappe.db.commit()
		frappe.clear_document_cache("Stock Entry", stock_entry)
	print(json.dumps(out, default=str), flush=True)
	return out


def _plan_row_unlock(row, output_class: str, *, doc) -> dict[str, Any]:
	"""Return unlock plan for one classified output row, or a blocked reason."""
	base = {
		"row_name": row.name,
		"idx": row.idx,
		"item_code": row.item_code,
		"output_class": output_class,
		"set_basic_rate_manually_before": cint(row.set_basic_rate_manually),
		"valuation_type_before": row.valuation_type or "",
		"allow_zero_valuation_rate": cint(row.allow_zero_valuation_rate),
		"basic_rate_before": row.basic_rate,
		"basic_amount_before": row.basic_amount,
	}

	if not cint(row.set_basic_rate_manually):
		return {**base, "action": "skip", "reason": "already_dynamic"}

	if output_class == CLASS_BULK_SCRAP:
		return {**base, "action": "preserve", "reason": "bulk_scrap"}

	valuation_type = (row.valuation_type or "").strip()
	clear_valuation_type = False

	if valuation_type == "Manual":
		if output_class == CLASS_OTHER_OUTPUT:
			return {
				**base,
				"action": "blocked",
				"reason": (
					"valuation_type=Manual on OTHER_OUTPUT: establish business class "
					"(By-Product/CO_PRODUCT or BULK_SCRAP) before unlock; unlock without "
					"class zeros the row"
				),
			}
		if output_class == CLASS_CO_PRODUCT:
			if not uses_v533_contract(doc):
				return {
					**base,
					"action": "blocked",
					"reason": (
						"valuation_type=Manual on CO_PRODUCT: document lacks Iran "
						f"manufacture contract stamp ({CONTRACT_VERSION_FIELD}); "
						"call adopt_manufacture_contract_version before unlock so "
						"existing allocate_stage_output_cost can price the row"
					),
				}
			# Stamped: clear Manual so existing stage-equivalent Co-Product path runs.
			clear_valuation_type = True
		else:
			# Component scrap / product reject / etc.
			clear_valuation_type = True

	return {
		**base,
		"action": "unlock",
		"reason": "eligible_output",
		"clear_valuation_type": clear_valuation_type,
		"set_basic_rate_manually_after": 0,
		"valuation_type_after": "" if clear_valuation_type else valuation_type,
	}


def _collect_doc_plans(doc) -> list[dict[str, Any]]:
	classified = classify_manufacture_outputs(doc)
	row_class: dict[str, str] = {}
	for cls in OUTPUT_CLASSES:
		for row in classified.get(cls) or []:
			row_class[row.name] = cls

	plans: list[dict[str, Any]] = []
	for row in doc.get("items") or []:
		if not _is_incoming_output(row):
			continue
		cls = row_class.get(row.name)
		if not cls:
			if cint(row.set_basic_rate_manually):
				plans.append(
					{
						"parent": doc.name,
						"row_name": row.name,
						"idx": row.idx,
						"item_code": row.item_code,
						"output_class": None,
						"action": "blocked",
						"reason": "unclassified_incoming_output",
						"set_basic_rate_manually_before": 1,
						"valuation_type_before": row.valuation_type or "",
						"allow_zero_valuation_rate": cint(row.allow_zero_valuation_rate),
						"basic_rate_before": row.basic_rate,
						"basic_amount_before": row.basic_amount,
					}
				)
			continue
		plan = _plan_row_unlock(row, cls, doc=doc)
		plan["parent"] = doc.name
		plans.append(plan)
	return plans


def _apply_plan(plan: dict[str, Any]) -> None:
	"""Persist metadata unlock only. Never touches rates/amounts."""
	updates = {"set_basic_rate_manually": 0}
	if plan.get("clear_valuation_type"):
		updates["valuation_type"] = ""
	frappe.db.set_value(
		"Stock Entry Detail",
		plan["row_name"],
		updates,
		update_modified=False,
	)


def _summarize(plans: list[dict[str, Any]], *, dry_run: bool, scanned_docs: int) -> dict[str, Any]:
	by_action: dict[str, int] = {}
	by_class: dict[str, int] = {}
	audit: list[dict[str, Any]] = []
	for p in plans:
		action = p.get("action") or "unknown"
		by_action[action] = by_action.get(action, 0) + 1
		cls = p.get("output_class") or "NONE"
		if action in ("unlock", "preserve", "blocked"):
			key = f"{action}:{cls}"
			by_class[key] = by_class.get(key, 0) + 1
		if action in ("unlock", "preserve", "blocked"):
			audit.append(
				{
					"parent": p.get("parent"),
					"row_name": p.get("row_name"),
					"idx": p.get("idx"),
					"item_code": p.get("item_code"),
					"output_class": p.get("output_class"),
					"action": action,
					"reason": p.get("reason"),
					"set_basic_rate_manually_before": p.get("set_basic_rate_manually_before"),
					"valuation_type_before": p.get("valuation_type_before"),
					"clear_valuation_type": bool(p.get("clear_valuation_type")),
					"basic_rate_before": p.get("basic_rate_before"),
					"basic_amount_before": p.get("basic_amount_before"),
				}
			)

	return {
		"ok": True,
		"dry_run": bool(dry_run),
		"scanned_documents": scanned_docs,
		"planned_rows": len(plans),
		"unlock_count": by_action.get("unlock", 0),
		"preserve_bulk_scrap_count": by_action.get("preserve", 0),
		"blocked_count": by_action.get("blocked", 0),
		"already_dynamic_count": by_action.get("skip", 0),
		"by_action": by_action,
		"by_class_action": by_class,
		"audit": audit,
		"timestamp": str(now_datetime()),
	}


def unlock_manufacture_manual_rates(
	stock_entry: str | None = None,
	dry_run: bool = True,
	batch_size: int | None = None,
	adopt_contract_version: str | None = None,
) -> dict[str, Any]:
	"""Unlock Manufacture output manual-rate flags (except Bulk Scrap).

	Bench-only. Not whitelisted. Does not start RIV.

	Args:
	        stock_entry: Optional single Stock Entry name. When omitted, all
	                submitted Manufacture Stock Entries are scanned.
	        dry_run: When True (default), plan only — no database writes.
	        batch_size: Commit batch size for apply mode (default 100, max 500).
	        adopt_contract_version: When set (apply mode), stamp missing
	                ``custom_manufacturing_costing_contract_version`` on scanned
	                docs before planning so Manual CO_PRODUCT can enter the
	                existing stage-equivalent engine. Job Cards are not modified.
	"""
	size = _normalize_batch_size(batch_size)
	names = _list_manufacture_names(stock_entry)
	all_plans: list[dict[str, Any]] = []
	parents_touched: set[str] = set()
	applied = 0
	pending_commit = 0
	stamped: list[str] = []

	for name in names:
		if adopt_contract_version and not dry_run:
			doc0 = frappe.get_doc("Stock Entry", name)
			if not uses_v533_contract(doc0):
				adopt_manufacture_contract_version(
					name, adopt_contract_version, dry_run=False
				)
				stamped.append(name)

		doc = frappe.get_doc("Stock Entry", name)
		plans = _collect_doc_plans(doc)
		all_plans.extend(plans)

		if dry_run:
			continue

		for plan in plans:
			if plan.get("action") != "unlock":
				continue
			_apply_plan(plan)
			applied += 1
			pending_commit += 1
			parents_touched.add(name)
			if pending_commit >= size:
				frappe.db.commit()
				pending_commit = 0

	if not dry_run and pending_commit:
		frappe.db.commit()

	for parent in parents_touched:
		frappe.clear_document_cache("Stock Entry", parent)

	out = _summarize(all_plans, dry_run=dry_run, scanned_docs=len(names))
	out["stock_entry"] = stock_entry
	out["applied_count"] = 0 if dry_run else applied
	out["batch_size"] = size
	out["adopt_contract_version"] = adopt_contract_version
	out["contracts_stamped"] = stamped
	print(json.dumps({k: v for k, v in out.items() if k != "audit"}, default=str), flush=True)
	return out
