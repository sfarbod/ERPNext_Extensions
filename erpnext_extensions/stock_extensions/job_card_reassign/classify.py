# Copyright (c) 2026, ERPNext Extensions contributors
"""Classify Stock Entries related to a Job Card reassignment."""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import cint

MOVE_PURPOSES = frozenset(
	{
		"Material Transfer for Manufacture",
		"Material Consumption for Manufacture",
		"Manufacture",
	}
)

CONSIGNMENT_FLAGS = (
	"custom_is_consignment_receipt",
	"custom_is_consignment_return",
	"custom_is_material_loan_issue",
	"custom_is_material_loan_return",
)

CLASS_MOVE = "MOVE"
CLASS_KEEP = "KEEP"
CLASS_BLOCK = "BLOCK"


def _g(row, key, default=None):
	if isinstance(row, dict):
		return row.get(key, default)
	return row.get(key, default) if hasattr(row, "get") else getattr(row, key, default)


def fetch_job_card_item_parents(job_card_item_names: list[str]) -> dict[str, str]:
	"""Map Job Card Item.name → parent Job Card."""
	names = [n for n in job_card_item_names if n]
	if not names:
		return {}
	rows = frappe.get_all(
		"Job Card Item",
		filters={"name": ["in", names]},
		fields=["name", "parent"],
	)
	return {r.name: r.parent for r in rows}


def fetch_stock_entries_for_scope(source_work_order: str, selected_job_cards: list[str]) -> list[dict]:
	"""Load manufacturing SEs on the source WO or tagged to the selected Job Cards."""
	if not source_work_order and not selected_job_cards:
		return []

	filters = []
	values: list = []
	if selected_job_cards:
		placeholders = ", ".join(["%s"] * len(selected_job_cards))
		filters.append(f"se.job_card in ({placeholders})")
		values.extend(selected_job_cards)
	if source_work_order:
		filters.append("se.work_order = %s")
		values.append(source_work_order)
	where = " OR ".join(f"({f})" for f in filters)

	parents = frappe.db.sql(
		f"""
		SELECT
			se.name, se.purpose, se.docstatus, se.work_order, se.job_card,
			se.is_return, se.modified, se.fg_completed_qty, se.process_loss_qty,
			se.bom_no, se.pick_list, se.source_stock_entry,
			se.custom_operation, se.custom_operation_row_id
		FROM `tabStock Entry` se
		WHERE se.docstatus != 2 AND ({where})
		ORDER BY se.creation
		""",
		tuple(values),
		as_dict=True,
	)
	if not parents:
		return []

	names = [p.name for p in parents]
	details = frappe.db.sql(
		"""
		SELECT parent, name, item_code, job_card_item, s_warehouse, t_warehouse,
			is_finished_item, qty, transfer_qty
		FROM `tabStock Entry Detail`
		WHERE parent IN ({})
		""".format(", ".join(["%s"] * len(names))),
		tuple(names),
		as_dict=True,
	)
	by_parent: dict[str, list] = defaultdict(list)
	item_names = []
	for d in details:
		by_parent[d.parent].append(d)
		if d.job_card_item:
			item_names.append(d.job_card_item)
	item_parents = fetch_job_card_item_parents(item_names)

	out = []
	for p in parents:
		row = dict(p)
		row["items"] = by_parent.get(p.name, [])
		row["job_card_item_parents"] = sorted(
			{
				item_parents[d.job_card_item]
				for d in row["items"]
				if d.job_card_item and d.job_card_item in item_parents
			}
		)
		out.append(row)
	return out


def _has_consignment_flag(se_name: str) -> bool:
	meta = frappe.get_meta("Stock Entry")
	fields = [f for f in CONSIGNMENT_FLAGS if meta.has_field(f)]
	if not fields:
		return False
	vals = frappe.db.get_value("Stock Entry", se_name, fields, as_dict=True) or {}
	return any(cint(vals.get(f)) for f in fields)


def _has_landed_cost(se_name: str) -> bool:
	if not frappe.db.exists("DocType", "Landed Cost Purchase Receipt"):
		return False
	return bool(
		frappe.db.sql(
			"""
			SELECT lcv.name
			FROM `tabLanded Cost Voucher` lcv
			INNER JOIN `tabLanded Cost Purchase Receipt` lcr ON lcr.parent = lcv.name
			WHERE lcr.receipt_document_type = 'Stock Entry'
				AND lcr.receipt_document = %s
				AND lcv.docstatus = 1
			LIMIT 1
			""",
			se_name,
		)
	)


def classify_stock_entry(row: dict, selected: set[str], source_work_order: str) -> dict:
	"""Return classification for one Stock Entry row."""
	name = _g(row, "name")
	job_card = _g(row, "job_card") or ""
	purpose = _g(row, "purpose") or ""
	item_parents = set(_g(row, "job_card_item_parents") or [])
	result = {
		"name": name,
		"purpose": purpose,
		"docstatus": cint(_g(row, "docstatus")),
		"work_order": _g(row, "work_order"),
		"job_card": job_card,
		"is_return": cint(_g(row, "is_return")),
		"fg_completed_qty": _g(row, "fg_completed_qty") or 0,
		"process_loss_qty": _g(row, "process_loss_qty") or 0,
		"modified": str(_g(row, "modified") or ""),
		"classification": CLASS_KEEP,
		"reason": "",
	}

	shared_unselected = item_parents - selected if item_parents else set()
	touches_selected_items = bool(item_parents & selected)

	if job_card and job_card in selected:
		if purpose not in MOVE_PURPOSES:
			result["classification"] = CLASS_BLOCK
			result["reason"] = (
				f"Stock Entry {name} is linked to selected Job Card {job_card} "
				f"but purpose {purpose} cannot be reassigned."
			)
			return result
		if shared_unselected:
			result["classification"] = CLASS_BLOCK
			result["reason"] = (
				f"Stock Entry {name} is shared with unselected Job Card(s) "
				f"{', '.join(sorted(shared_unselected))}."
			)
			return result
		if _g(row, "pick_list"):
			result["classification"] = CLASS_BLOCK
			result["reason"] = f"Stock Entry {name} is linked to Pick List {_g(row, 'pick_list')}."
			return result
		if purpose == "Disassemble" or _g(row, "source_stock_entry"):
			if purpose != "Manufacture" and purpose not in MOVE_PURPOSES:
				result["classification"] = CLASS_BLOCK
				result["reason"] = f"Stock Entry {name} is a disassembly or sourced entry and cannot move."
				return result
		if _has_landed_cost(name):
			result["classification"] = CLASS_BLOCK
			result["reason"] = f"Stock Entry {name} has a submitted Landed Cost Voucher."
			return result
		if _has_consignment_flag(name):
			result["classification"] = CLASS_BLOCK
			result["reason"] = f"Stock Entry {name} is a consignment or material-loan voucher."
			return result
		result["classification"] = CLASS_MOVE
		result["reason"] = "Exclusively linked to a selected Job Card."
		return result

	if job_card and job_card not in selected:
		if touches_selected_items:
			result["classification"] = CLASS_BLOCK
			result["reason"] = (
				f"Stock Entry {name} belongs to Job Card {job_card} (not selected) "
				f"but has job_card_item rows from selected Job Cards."
			)
			return result
		if _g(row, "work_order") == source_work_order:
			result["classification"] = CLASS_KEEP
			result["reason"] = f"Linked to unselected Job Card {job_card}."
			return result
		result["classification"] = CLASS_KEEP
		result["reason"] = "Not in reassignment scope."
		return result

	# No job_card on the parent.
	if touches_selected_items and shared_unselected:
		result["classification"] = CLASS_BLOCK
		result["reason"] = (
			f"Stock Entry {name} has job_card_item rows spanning selected and unselected Job Cards."
		)
		return result
	if touches_selected_items and not job_card:
		result["classification"] = CLASS_BLOCK
		result["reason"] = (
			f"Stock Entry {name} has job_card_item rows for selected Job Cards "
			f"but no Stock Entry.job_card. Ownership cannot be separated safely."
		)
		return result
	if _g(row, "work_order") == source_work_order:
		result["classification"] = CLASS_KEEP
		result["reason"] = "Linked only to the source Work Order."
		return result

	result["classification"] = CLASS_KEEP
	result["reason"] = "Not in reassignment scope."
	return result


def classify_scope(source_work_order: str, selected_job_cards: list[str], rows: list[dict] | None = None) -> dict:
	selected = set(selected_job_cards)
	rows = rows if rows is not None else fetch_stock_entries_for_scope(source_work_order, selected_job_cards)
	classified = [classify_stock_entry(row, selected, source_work_order) for row in rows]
	return {
		"rows": classified,
		"move": [r["name"] for r in classified if r["classification"] == CLASS_MOVE],
		"keep": [r["name"] for r in classified if r["classification"] == CLASS_KEEP],
		"block": [r for r in classified if r["classification"] == CLASS_BLOCK],
	}
