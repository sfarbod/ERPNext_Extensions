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


def _normalize_row_filter(
	stock_entry: str | None,
	row_name: str | None,
	item_code: str | None,
) -> tuple[str | None, str | None]:
	"""Validate selective unlock arguments. Never expands an invalid filter.

	``item_code`` without ``row_name`` is rejected: one item can appear on more
	than one row, and a partial filter must not fall through to every eligible row.
	"""
	row_name = (row_name or "").strip() or None
	item_code = (item_code or "").strip() or None
	if item_code and not row_name:
		raise UnlockManufactureError(
			"item_code requires row_name. Item-only selection is ambiguous and "
			"will not unlock every matching row."
		)
	if row_name and not (stock_entry or "").strip():
		raise UnlockManufactureError("row_name requires stock_entry.")
	return row_name, item_code


def _assert_selected_row(doc, row_name: str, item_code: str | None):
	"""The named row must be one incoming output on this Manufacture document."""
	matches = [row for row in (doc.get("items") or []) if row.name == row_name]
	if not matches:
		raise UnlockManufactureError(
			f"{doc.name}: row {row_name!r} does not belong to this Stock Entry. "
			"Refusing to unlock any row."
		)
	if len(matches) != 1:
		raise UnlockManufactureError(
			f"{doc.name}: row {row_name!r} matched {len(matches)} times. "
			"Refusing to unlock any row."
		)
	row = matches[0]
	actual_item = row.item_code
	if item_code and actual_item != item_code:
		raise UnlockManufactureError(
			f"{doc.name} row {row_name}: item_code {actual_item!r} does not match "
			f"{item_code!r}. Refusing to unlock any row."
		)
	if not _is_incoming_output(row):
		raise UnlockManufactureError(
			f"{doc.name} row {row_name} ({actual_item}) is not an incoming Manufacture "
			"output. Refusing to unlock any row."
		)
	return row


def _restrict_to_selected_row(
	plans: list[dict[str, Any]], row_name: str
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
	"""Keep the selected plan. Other unlock plans become excluded and are not applied."""
	selected = [plan for plan in plans if plan.get("row_name") == row_name]
	if len(selected) != 1:
		raise UnlockManufactureError(
			f"Selective unlock found {len(selected)} plans for row {row_name!r}. "
			"Refusing to unlock any row."
		)
	excluded: list[dict[str, Any]] = []
	filtered: list[dict[str, Any]] = []
	selected_plan: dict[str, Any] | None = None
	for plan in plans:
		if plan.get("row_name") == row_name:
			selected_plan = {**plan, "selected": True}
			filtered.append(selected_plan)
			continue
		if plan.get("action") == "unlock":
			excluded_plan = {
				**plan,
				"action": "excluded",
				"reason": "selective_filter",
				"selected": False,
			}
			excluded.append(excluded_plan)
			filtered.append(excluded_plan)
		else:
			filtered.append({**plan, "selected": False})
	if selected_plan is None:
		raise UnlockManufactureError(
			f"Selective unlock lost row {row_name!r} while filtering. Refusing to unlock any row."
		)
	return filtered, selected_plan, excluded


def _revalidate_unlock_plan(plan: dict[str, Any], doc) -> dict[str, Any]:
	"""Re-read the selected row and refuse a stale or non-unlock plan."""
	_assert_selected_row(doc, plan["row_name"], plan.get("item_code"))
	live_plans = _collect_doc_plans(doc)
	live = next((candidate for candidate in live_plans if candidate.get("row_name") == plan["row_name"]), None)
	if not live or live.get("action") != "unlock":
		raise UnlockManufactureError(
			f"{doc.name} row {plan['row_name']}: unlock plan is stale or no longer eligible "
			f"(action={None if not live else live.get('action')!r}). No row was updated."
		)
	if live.get("item_code") != plan.get("item_code"):
		raise UnlockManufactureError(
			f"{doc.name} row {plan['row_name']}: item changed from {plan.get('item_code')!r} "
			f"to {live.get('item_code')!r}. No row was updated."
		)
	if cint(live.get("set_basic_rate_manually_before")) != cint(plan.get("set_basic_rate_manually_before")):
		raise UnlockManufactureError(
			f"{doc.name} row {plan['row_name']}: manual flag changed before apply. No row was updated."
		)
	if (live.get("valuation_type_before") or "") != (plan.get("valuation_type_before") or ""):
		raise UnlockManufactureError(
			f"{doc.name} row {plan['row_name']}: valuation type changed before apply. No row was updated."
		)
	if flt(live.get("basic_rate_before")) != flt(plan.get("basic_rate_before")):
		raise UnlockManufactureError(
			f"{doc.name} row {plan['row_name']}: basic_rate changed before apply. No row was updated."
		)
	if flt(live.get("basic_amount_before")) != flt(plan.get("basic_amount_before")):
		raise UnlockManufactureError(
			f"{doc.name} row {plan['row_name']}: basic_amount changed before apply. No row was updated."
		)
	return live


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


def _expected_write(plan: dict[str, Any]) -> dict[str, Any]:
	"""Metadata fields this unlock would write. Rates and quantities are not included."""
	return {
		"doctype": "Stock Entry Detail",
		"row_name": plan.get("row_name"),
		"item_code": plan.get("item_code"),
		"output_class": plan.get("output_class"),
		"set_basic_rate_manually_before": plan.get("set_basic_rate_manually_before"),
		"set_basic_rate_manually_after": plan.get("set_basic_rate_manually_after", 0),
		"valuation_type_before": plan.get("valuation_type_before") or "",
		"valuation_type_after": plan.get("valuation_type_after", plan.get("valuation_type_before") or ""),
		"basic_rate_unchanged": plan.get("basic_rate_before"),
		"basic_amount_unchanged": plan.get("basic_amount_before"),
		"writes": ["set_basic_rate_manually"]
		+ (["valuation_type"] if plan.get("clear_valuation_type") else []),
	}


def _summarize(
	plans: list[dict[str, Any]],
	*,
	dry_run: bool,
	scanned_docs: int,
	row_name: str | None = None,
	item_code: str | None = None,
	excluded: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
		if action in ("unlock", "preserve", "blocked", "excluded"):
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

	excluded = excluded or []
	selected_plans = [plan for plan in plans if plan.get("selected")]
	selected_plan = selected_plans[0] if len(selected_plans) == 1 else None
	unlock_plans = [plan for plan in plans if plan.get("action") == "unlock"]
	return {
		"ok": True,
		"dry_run": bool(dry_run),
		"scanned_documents": scanned_docs,
		"planned_rows": len(plans),
		"unlock_count": by_action.get("unlock", 0),
		"selected_unlock_count": by_action.get("unlock", 0),
		"excluded_unlock_count": len(excluded),
		"preserve_bulk_scrap_count": by_action.get("preserve", 0),
		"blocked_count": by_action.get("blocked", 0),
		"already_dynamic_count": by_action.get("skip", 0),
		"row_filter": {"row_name": row_name, "item_code": item_code},
		"selected_row": (
			{
				"row_name": selected_plan.get("row_name"),
				"item_code": selected_plan.get("item_code"),
				"output_class": selected_plan.get("output_class"),
				"action": selected_plan.get("action"),
				"reason": selected_plan.get("reason"),
				"set_basic_rate_manually_before": selected_plan.get("set_basic_rate_manually_before"),
				"set_basic_rate_manually_after": selected_plan.get("set_basic_rate_manually_after"),
				"valuation_type_before": selected_plan.get("valuation_type_before"),
				"valuation_type_after": selected_plan.get("valuation_type_after"),
			}
			if selected_plan
			else None
		),
		"excluded_rows": [
			{
				"row_name": plan.get("row_name"),
				"item_code": plan.get("item_code"),
				"output_class": plan.get("output_class"),
				"reason": plan.get("reason"),
				"set_basic_rate_manually_before": plan.get("set_basic_rate_manually_before"),
				"valuation_type_before": plan.get("valuation_type_before"),
				"basic_rate_before": plan.get("basic_rate_before"),
			}
			for plan in excluded
		],
		"expected_writes": [_expected_write(plan) for plan in unlock_plans],
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
	row_name: str | None = None,
	item_code: str | None = None,
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
	        row_name: Optional Stock Entry Detail name. Requires ``stock_entry``.
	                Only that incoming output row can be unlocked.
	        item_code: Optional confirmation of the selected row's item. Requires
	                ``row_name``. A mismatch rejects the call and unlocks nothing.
	"""
	size = _normalize_batch_size(batch_size)
	row_name, item_code = _normalize_row_filter(stock_entry, row_name, item_code)
	names = _list_manufacture_names(stock_entry)
	all_plans: list[dict[str, Any]] = []
	excluded_plans: list[dict[str, Any]] = []
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
		if row_name:
			_assert_selected_row(doc, row_name, item_code)
		plans = _collect_doc_plans(doc)
		if row_name:
			plans, _selected, excluded = _restrict_to_selected_row(plans, row_name)
			excluded_plans.extend(excluded)
		all_plans.extend(plans)

		if dry_run:
			continue

		for plan in plans:
			if plan.get("action") != "unlock":
				continue
			if row_name and plan.get("row_name") != row_name:
				raise UnlockManufactureError(
					f"{name}: refused to unlock non-selected row {plan.get('row_name')!r}."
				)
			live_doc = frappe.get_doc("Stock Entry", name)
			live_plan = _revalidate_unlock_plan(plan, live_doc)
			_apply_plan(live_plan)
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

	out = _summarize(
		all_plans,
		dry_run=dry_run,
		scanned_docs=len(names),
		row_name=row_name,
		item_code=item_code,
		excluded=excluded_plans,
	)
	out["stock_entry"] = stock_entry
	out["applied_count"] = 0 if dry_run else applied
	out["batch_size"] = size
	out["adopt_contract_version"] = adopt_contract_version
	out["contracts_stamped"] = stamped
	print(json.dumps({k: v for k, v in out.items() if k != "audit"}, default=str), flush=True)
	return out
