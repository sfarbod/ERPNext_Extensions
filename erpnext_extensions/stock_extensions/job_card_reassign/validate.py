# Copyright (c) 2026, ERPNext Extensions contributors
"""Blockers and warnings for Job Card Work Order reassignment."""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.stock_extensions.job_card_reassign.classify import CLASS_MOVE

TERMINAL_TARGET_STATUSES = frozenset({"Cancelled", "Stopped", "Closed", "Completed"})
TERMINAL_SOURCE_STATUSES = frozenset({"Cancelled", "Closed", "Stopped"})


def _msg(code: str, message: str, extra: dict | None = None) -> dict:
	row = {"code": code, "message": message}
	if extra:
		row.update(extra)
	return row


def collect_sfg_consumption_blockers(
	source_work_order: str,
	selected_job_cards: list[str],
	job_cards: list[dict],
) -> list[dict]:
	"""Block when an unselected source JC already consumed a selected JC's SFG."""
	selected = set(selected_job_cards)
	sfg_by_jc = {
		j["name"]: j.get("finished_good")
		for j in job_cards
		if j["name"] in selected and j.get("finished_good") and cint(j.get("track_semi_finished_goods"))
	}
	sfg_items = {fg for fg in sfg_by_jc.values() if fg}
	if not sfg_items:
		return []

	producer = defaultdict(list)
	for jc_name, fg in sfg_by_jc.items():
		producer[fg].append(jc_name)

	blockers = []
	unselected = [j["name"] for j in job_cards if j["name"] not in selected and cint(j.get("docstatus")) != 2]
	if unselected:
		item_rows = frappe.get_all(
			"Job Card Item",
			filters={"parent": ["in", unselected], "item_code": ["in", list(sfg_items)]},
			fields=["parent", "item_code"],
		)
		seen = set()
		for row in item_rows:
			key = (row.parent, row.item_code)
			if key in seen:
				continue
			seen.add(key)
			blockers.append(
				_msg(
					"sfg_item_on_unselected_jc",
					(
						f"Selected Job Card(s) {', '.join(producer[row.item_code])} produce {row.item_code}, "
						f"which is a required item on unselected Job Card {row.parent}. "
						f"Include {row.parent} in the selection, or do not move the producer."
					),
					{"job_card": row.parent, "item_code": row.item_code},
				)
			)

	if not selected_job_cards:
		return blockers
	placeholders_jc = ", ".join(["%s"] * len(selected_job_cards))
	placeholders_fg = ", ".join(["%s"] * len(sfg_items))
	se_rows = frappe.db.sql(
		f"""
		SELECT se.job_card, sed.item_code
		FROM `tabStock Entry` se
		INNER JOIN `tabStock Entry Detail` sed ON sed.parent = se.name
		WHERE se.work_order = %s
			AND se.docstatus = 1
			AND se.purpose IN ('Manufacture', 'Material Consumption for Manufacture')
			AND IFNULL(se.job_card, '') != ''
			AND se.job_card NOT IN ({placeholders_jc})
			AND IFNULL(sed.s_warehouse, '') != ''
			AND sed.item_code IN ({placeholders_fg})
		GROUP BY se.job_card, sed.item_code
		""",
		tuple([source_work_order, *selected_job_cards, *sfg_items]),
		as_dict=True,
	)
	seen_se = set()
	for row in se_rows:
		key = (row.job_card, row.item_code)
		if key in seen_se:
			continue
		seen_se.add(key)
		blockers.append(
			_msg(
				"sfg_consumed_by_unselected_jc",
				(
					f"Unselected Job Card {row.job_card} already consumed {row.item_code} "
					f"(produced by {', '.join(producer.get(row.item_code, []))}) "
					f"on the source Work Order. Include {row.job_card} in the selection."
				),
				{"job_card": row.job_card, "item_code": row.item_code},
			)
		)
	return blockers


def evaluate_compatibility(
	*,
	source: dict,
	target: dict,
	job_cards: list[dict],
	selected: list[str],
	classified: dict,
	mappings: dict[str, dict],
	reason: str,
) -> tuple[list[dict], list[dict]]:
	blockers: list[dict] = []
	warnings: list[dict] = []

	if not reason or not str(reason).strip():
		blockers.append(_msg("reason_required", "A reason is required."))

	if not selected:
		blockers.append(_msg("no_job_cards", "Select at least one Job Card."))

	if not source or not target:
		blockers.append(_msg("missing_work_order", "Source and Target Work Order are required."))
		return blockers, warnings

	if source.get("name") == target.get("name"):
		blockers.append(_msg("same_work_order", "Source and Target Work Order must be different."))

	if source.get("company") != target.get("company"):
		blockers.append(
			_msg(
				"company_mismatch",
				f"Company differs ({source.get('company')} → {target.get('company')}).",
			)
		)

	if cint(source.get("docstatus")) != 1:
		blockers.append(_msg("source_not_submitted", "Source Work Order must be submitted."))
	if cint(target.get("docstatus")) != 1:
		blockers.append(_msg("target_not_submitted", "Target Work Order must be submitted."))

	if source.get("status") in TERMINAL_SOURCE_STATUSES:
		blockers.append(
			_msg("source_terminal", f"Source Work Order status {source.get('status')} cannot be updated.")
		)
	if target.get("status") in TERMINAL_TARGET_STATUSES:
		blockers.append(
			_msg("target_terminal", f"Target Work Order status {target.get('status')} cannot receive Job Cards.")
		)

	if cint(source.get("track_semi_finished_goods")) != cint(target.get("track_semi_finished_goods")):
		blockers.append(
			_msg(
				"sfg_flag_mismatch",
				"Source and Target Work Orders have different Track Semi-Finished Goods settings.",
			)
		)

	source_item = source.get("production_item")
	target_item = target.get("production_item")
	final_fg_selected = any(
		j.get("finished_good") == source_item or (not j.get("finished_good") and not cint(j.get("track_semi_finished_goods")))
		for j in job_cards
		if j["name"] in selected
	)
	if source_item != target_item and (final_fg_selected or not cint(source.get("track_semi_finished_goods"))):
		blockers.append(
			_msg(
				"production_item_mismatch",
				f"Production item differs ({source_item} → {target_item}).",
			)
		)
	elif source_item != target_item:
		warnings.append(
			_msg(
				"production_item_differs",
				f"Work Order production items differ ({source_item} → {target_item}). "
				f"Only intermediate Job Cards were selected.",
			)
		)

	if source.get("bom_no") != target.get("bom_no"):
		warnings.append(
			_msg(
				"bom_differs",
				f"BOM differs ({source.get('bom_no')} → {target.get('bom_no')}). "
				f"Operation mapping still requires a unique target operation.",
			)
		)

	if source.get("project") != target.get("project"):
		warnings.append(
			_msg("project_differs", f"Project differs ({source.get('project')} → {target.get('project')}).")
		)

	for flag in ("transfer_material_against", "skip_transfer"):
		if source.get(flag) != target.get(flag):
			warnings.append(
				_msg(
					f"{flag}_differs",
					f"{flag} differs ({source.get(flag)} → {target.get(flag)}).",
				)
			)

	blocked_statuses = []
	for jc in job_cards:
		if jc["name"] not in selected:
			continue
		if cint(jc.get("docstatus")) == 2 or jc.get("status") == "Cancelled":
			blockers.append(_msg("jc_cancelled", f"Job Card {jc['name']} is cancelled.", {"job_card": jc["name"]}))
		if cint(jc.get("is_paused")):
			blockers.append(_msg("jc_paused", f"Job Card {jc['name']} is paused.", {"job_card": jc["name"]}))
		if cint(jc.get("is_corrective_job_card")):
			blockers.append(
				_msg("jc_corrective", f"Job Card {jc['name']} is a corrective Job Card.", {"job_card": jc["name"]})
			)
		if cint(jc.get("is_subcontracted")):
			blockers.append(
				_msg("jc_subcontracted", f"Job Card {jc['name']} is subcontracted.", {"job_card": jc["name"]})
			)
		if jc.get("work_order") != source.get("name"):
			blockers.append(
				_msg(
					"jc_not_on_source",
					f"Job Card {jc['name']} belongs to {jc.get('work_order')}, not {source.get('name')}.",
					{"job_card": jc["name"]},
				)
			)
		if jc.get("status") == "On Hold":
			blockers.append(_msg("jc_on_hold", f"Job Card {jc['name']} is On Hold.", {"job_card": jc["name"]}))

	for se in classified.get("block") or []:
		blockers.append(_msg("stock_entry_block", se.get("reason") or f"Stock Entry {se.get('name')} cannot move."))

	for jc_name, mapped in mappings.items():
		if not mapped.get("ok"):
			# Only true integrity failures (e.g. Job Card has no operation).
			# Missing / ambiguous target ops are CREATE, not blockers.
			blockers.append(_msg("operation_unmapped", mapped.get("error") or f"Cannot map {jc_name}."))
			continue
		if mapped.get("action") == "CREATE":
			warnings.append(
				_msg(
					"create_target_operation",
					(
						f"CREATE TARGET OPERATION for Job Card {jc_name}: "
						f"{mapped.get('source_operation') or ''} / {mapped.get('source_finished_good') or '-'}."
					),
					{"job_card": jc_name, "action": "CREATE"},
				)
			)
		jc = next((j for j in job_cards if j["name"] == jc_name), {})
		if jc.get("workstation") and mapped.get("target_workstation") and jc.get("workstation") != mapped.get(
			"target_workstation"
		):
			warnings.append(
				_msg(
					"workstation_differs",
					(
						f"Job Card {jc_name} workstation {jc.get('workstation')} "
						f"differs from target {mapped.get('target_workstation')}."
					),
					{"job_card": jc_name},
				)
			)

	# target_capacity / target_completed_capacity are intentionally NOT blockers.
	# Historical repair may merge Job Cards into a previously split / incomplete Target WO.
	# Qty / operation capacity differences are reported as RECONCILE impact by the engine.

	if frappe.db.exists("DocType", "Stock Reservation Entry"):
		if frappe.db.exists(
			"Stock Reservation Entry",
			{"voucher_type": "Work Order", "voucher_no": ["in", [source.get("name"), target.get("name")]], "docstatus": 1},
		):
			blockers.append(
				_msg(
					"stock_reservation",
					"Active Stock Reservation Entries exist on the source or target Work Order.",
				)
			)

	blockers.extend(collect_sfg_consumption_blockers(source.get("name"), selected, job_cards))

	move_ses = [r for r in classified.get("rows") or [] if r.get("classification") == CLASS_MOVE]
	if any(cint(r.get("docstatus")) == 1 for r in move_ses):
		warnings.append(
			_msg(
				"submitted_stock_entries",
				"Submitted Stock Entries will have Work Order references updated. "
				"Stock Ledger and GL entries will not be cancelled or reposted.",
			)
		)

	return blockers, warnings
