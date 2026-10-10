# Copyright (c) 2026, ERPNext Extensions contributors
"""Secondary Item business-type suggestion engine (v5.4.1 completion).

Detects / explains / suggests type transitions. Never mutates without
explicit per-row user approval validated by the service layer.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.secondary import (
	CLASS_ADDITIONAL_FINISHED_GOOD,
	CLASS_BY_PRODUCT,
	CLASS_CO_PRODUCT,
	CLASS_COMPONENT_SCRAP,
	CLASS_MAIN_PRODUCT_REJECT,
	CLASS_ORDINARY_SCRAP,
	CLASS_UNKNOWN,
	classify_secondary_row,
	load_jc_secondary_items,
)

CONF_PROVEN = "PROVEN"
CONF_HIGH = "HIGH"
CONF_AMBIGUOUS = "AMBIGUOUS"
CONF_UNKNOWN = "UNKNOWN"

TYPE_SCRAP = "Scrap"
TYPE_CO = "Co-Product"
TYPE_BY = "By-Product"
TYPE_AFG = "Additional Finished Good"

SUPPORTED_TRANSITIONS = frozenset(
	{
		(TYPE_CO, TYPE_SCRAP),
		(TYPE_BY, TYPE_SCRAP),
		(TYPE_SCRAP, TYPE_CO),
		(TYPE_SCRAP, TYPE_BY),
		(TYPE_CO, TYPE_BY),
		(TYPE_BY, TYPE_CO),
	}
)

APPROVABLE_CONFIDENCE = frozenset({CONF_PROVEN, CONF_HIGH})


def row_identity(job_card: str, row: dict) -> dict:
	"""Stable identity for approval binding (not idx alone)."""
	name = (row.get("name") or "").strip()
	payload = {
		"job_card": job_card,
		"secondary_row": name,
		"item_code": (row.get("item_code") or "").strip(),
		"current_type": (row.get("secondary_item_type") or "").strip(),
		"stock_uom": (row.get("stock_uom") or row.get("uom") or "").strip(),
		"bom_secondary_item": (row.get("bom_secondary_item") or "").strip(),
		"stock_qty": flt(row.get("stock_qty") or row.get("qty")),
	}
	digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:24]
	payload["evidence_fingerprint"] = digest
	return payload


def _finished_good(job_card: str) -> str | None:
	jc = frappe.db.get_value(
		"Job Card", job_card, ["finished_good", "production_item", "work_order"], as_dict=1
	)
	if not jc:
		return None
	fg = jc.finished_good or jc.production_item
	if not fg and jc.work_order:
		fg = frappe.db.get_value("Work Order", jc.work_order, "production_item")
	return fg


def _jc_item_for(job_card: str, item_code: str) -> dict | None:
	return frappe.db.get_value(
		"Job Card Item",
		{"parent": job_card, "item_code": item_code},
		["name", "item_code", "required_qty", "transferred_qty", "consumed_qty"],
		as_dict=1,
	)


def _has_component_transfer(job_card: str, item_code: str) -> bool:
	return bool(
		frappe.db.sql(
			"""
			select 1
			from `tabStock Entry` se
			join `tabStock Entry Detail` sed on sed.parent=se.name
			where se.job_card=%s and se.docstatus=1
			  and se.purpose='Material Transfer for Manufacture'
			  and ifnull(se.is_return,0)=0 and ifnull(se.custom_is_returned,0)=0
			  and sed.item_code=%s
			limit 1
			""",
			(job_card, item_code),
		)
	)


def _historical_component_scrap(item_code: str, finished_good: str | None) -> int:
	"""Count submitted Manufacture rows where item is COMPONENT_SCRAP scrap."""
	conds = [
		"se.purpose='Manufacture'",
		"se.docstatus=1",
		"sed.item_code=%s",
		"sed.secondary_item_type='Scrap'",
		"(sed.custom_output_class='COMPONENT_SCRAP' or ifnull(sed.is_finished_item,0)=0)",
	]
	vals: list[Any] = [item_code]
	if finished_good:
		conds.append(
			"""se.job_card in (
				select name from `tabJob Card`
				where ifnull(finished_good, '')=%s or ifnull(production_item,'')=%s
			)"""
		)
		vals.extend([finished_good, finished_good])
	return int(
		frappe.db.sql(
			f"""
			select count(*) from `tabStock Entry` se
			join `tabStock Entry Detail` sed on sed.parent=se.name
			where {" AND ".join(conds)}
			""",
			vals,
		)[0][0]
		or 0
	)


def _bom_secondary_type(bom_secondary_item: str | None, bom_no: str | None, item_code: str) -> str | None:
	if bom_secondary_item and frappe.db.exists("BOM Secondary Item", bom_secondary_item):
		return frappe.db.get_value("BOM Secondary Item", bom_secondary_item, "secondary_item_type")
	if bom_no:
		return frappe.db.get_value(
			"BOM Secondary Item",
			{"parent": bom_no, "item_code": item_code},
			"secondary_item_type",
		)
	return None


def _has_stage_metadata(row: dict) -> bool:
	return any(
		[
			flt(row.get("custom_output_equivalent_factor")),
			flt(row.get("custom_physical_conversion")),
			(row.get("custom_common_uom") or "").strip(),
			(row.get("custom_parent_co_product") or "").strip(),
			(row.get("custom_output_class") or "").strip()
			in (CLASS_CO_PRODUCT, CLASS_BY_PRODUCT, "CO_PRODUCT", "BY_PRODUCT"),
		]
	)


def _expected_iran_class_for_type(
	job_card: str, item_code: str, suggested_type: str, finished_good: str | None
) -> str:
	"""Predict Iran class after business-type change (no persisted write)."""
	from erpnext_extensions.iran_accounting.scrap_costing import is_product_reject

	if suggested_type == TYPE_CO:
		return CLASS_CO_PRODUCT
	if suggested_type == TYPE_BY:
		# Iran stage engine maps By-Product into the CO_PRODUCT bucket.
		return CLASS_CO_PRODUCT
	if suggested_type == TYPE_AFG:
		return CLASS_ADDITIONAL_FINISHED_GOOD
	if suggested_type == TYPE_SCRAP:
		adapter = frappe._dict({"item_code": item_code, "secondary_item_type": TYPE_SCRAP})
		if is_product_reject(adapter, finished_good):
			return CLASS_MAIN_PRODUCT_REJECT
		# Component family if JC Item / transfer exists
		if _jc_item_for(job_card, item_code) or _has_component_transfer(job_card, item_code):
			return CLASS_COMPONENT_SCRAP
		return CLASS_ORDINARY_SCRAP
	return CLASS_UNKNOWN


def _manufacture_se_stage_evidence(job_card: str, item_code: str) -> dict | None:
	"""Submitted Manufacture incoming row for JC×item carrying stage Co/By evidence."""
	if not job_card or not item_code:
		return None
	rows = frappe.db.sql(
		"""
		select sed.name, sed.parent, sed.secondary_item_type, sed.custom_output_class,
		       sed.custom_output_equivalent_factor, sed.qty, sed.t_warehouse
		from `tabStock Entry` se
		join `tabStock Entry Detail` sed on sed.parent = se.name
		where se.job_card = %s
		  and se.purpose = 'Manufacture'
		  and se.docstatus = 1
		  and sed.item_code = %s
		  and ifnull(sed.t_warehouse, '') != ''
		  and ifnull(sed.s_warehouse, '') = ''
		order by se.posting_date desc, se.posting_time desc, sed.idx
		""",
		(job_card, item_code),
		as_dict=1,
	)
	for row in rows:
		sit = (row.get("secondary_item_type") or "").strip()
		out_class = (row.get("custom_output_class") or "").strip()
		if sit in (TYPE_BY, TYPE_CO) or out_class in (CLASS_CO_PRODUCT, "CO_PRODUCT", CLASS_BY_PRODUCT):
			return row
	return None


def suggest_secondary_type_changes(job_card: str) -> dict:
	"""Analyze JC Secondary Items and return per-row suggestions (no mutation)."""
	rows = load_jc_secondary_items(job_card)
	fg = _finished_good(job_card)
	bom_no = frappe.db.get_value("Job Card", job_card, "semi_fg_bom") or frappe.db.get_value(
		"Job Card", job_card, "bom_no"
	)
	suggestions = []
	statuses = []

	for row in rows:
		current_type = (row.get("secondary_item_type") or "").strip()
		item_code = row.get("item_code")
		current_class = classify_secondary_row(row)
		ident = row_identity(job_card, row)
		evidence = []
		confidence = CONF_UNKNOWN
		suggested_type = None
		suggested_class = None
		approval_enabled = False
		action = "NO CHANGE"
		manual_review = False
		block_reason = None

		# --- Product Reject protection: Scrap of finished_good ---
		from erpnext_extensions.iran_accounting.scrap_costing import is_product_reject

		if current_type == TYPE_SCRAP and is_product_reject(
			frappe._dict({"item_code": item_code, "secondary_item_type": TYPE_SCRAP}), fg
		):
			evidence.append("Product Reject protection: item_code equals finished_good / main item")
			suggestions.append(
				{
					**ident,
					"idx": row.get("idx"),
					"item_name": row.get("item_name"),
					"qty": flt(row.get("stock_qty")),
					"uom": row.get("stock_uom"),
					"current_type": current_type,
					"suggested_type": None,
					"current_iran_class": CLASS_MAIN_PRODUCT_REJECT,
					"suggested_iran_class": CLASS_MAIN_PRODUCT_REJECT,
					"confidence": CONF_PROVEN,
					"evidence": evidence,
					"approval_required": False,
					"approval_enabled": False,
					"action": "NO CHANGE",
					"downstream_impact": "MAIN_PRODUCT_REJECT preserved; participates with MAIN_FG in stage set.",
				}
			)
			continue

		# --- Scrap → By/Co-Product when submitted Manufacture already carries stage evidence ---
		if current_type == TYPE_SCRAP:
			se_ev = _manufacture_se_stage_evidence(job_card, item_code)
			bom_type = _bom_secondary_type(row.get("bom_secondary_item"), bom_no, item_code)
			if se_ev:
				se_sit = (se_ev.get("secondary_item_type") or "").strip()
				suggested_type = se_sit if se_sit in (TYPE_BY, TYPE_CO) else TYPE_BY
				suggested_class = _expected_iran_class_for_type(
					job_card, item_code, suggested_type, fg
				)
				confidence = CONF_PROVEN
				action = "TYPE_CHANGE_SUGGESTED"
				approval_enabled = True
				evidence.append(
					f"Submitted Manufacture {se_ev.parent} row {se_ev.name} already "
					f"secondary_item_type={se_sit or '(blank)'} "
					f"custom_output_class={se_ev.custom_output_class or '(blank)'}"
				)
				evidence.append(
					"JC Scrap lags Manufacture stage classification — approve to sync Job Card"
				)
				suggestions.append(
					{
						**ident,
						"idx": row.get("idx"),
						"item_name": row.get("item_name"),
						"qty": flt(row.get("stock_qty")),
						"uom": row.get("stock_uom"),
						"current_type": current_type,
						"suggested_type": suggested_type,
						"current_iran_class": current_class,
						"suggested_iran_class": suggested_class,
						"confidence": confidence,
						"evidence": evidence,
						"approval_required": True,
						"approval_enabled": approval_enabled,
						"action": action,
						"downstream_impact": (
							f"If approved: JC {current_type}→{suggested_type}; "
							f"Iran class expected {current_class}→{suggested_class}. "
							"Manufacture SE stage fields are synced without cancel/recreate; "
							"qty and stock movements are preserved. Native RIV is not auto-started."
						),
						"manufacture_se": se_ev.parent,
						"manufacture_se_detail": se_ev.name,
					}
				)
				continue
			if bom_type in (TYPE_BY, TYPE_CO):
				suggested_type = bom_type
				suggested_class = _expected_iran_class_for_type(
					job_card, item_code, suggested_type, fg
				)
				confidence = CONF_HIGH
				action = "TYPE_CHANGE_SUGGESTED"
				approval_enabled = True
				evidence.append(f"BOM Secondary declares {bom_type} while JC says Scrap")
				suggestions.append(
					{
						**ident,
						"idx": row.get("idx"),
						"item_name": row.get("item_name"),
						"qty": flt(row.get("stock_qty")),
						"uom": row.get("stock_uom"),
						"current_type": current_type,
						"suggested_type": suggested_type,
						"current_iran_class": current_class,
						"suggested_iran_class": suggested_class,
						"confidence": confidence,
						"evidence": evidence,
						"approval_required": True,
						"approval_enabled": approval_enabled,
						"action": action,
						"downstream_impact": (
							f"If approved: JC Scrap→{suggested_type}; sync matching Manufacture "
							"SE secondary_item_type + CO_PRODUCT stamp without cancel/recreate."
						),
					}
				)
				continue

			# Default Scrap: keep NO CHANGE (component / ordinary scrap)
			exp = _expected_iran_class_for_type(job_card, item_code, TYPE_SCRAP, fg)
			evidence.append(f"Already Scrap; expected Iran class {exp}")
			suggestions.append(
				{
					**ident,
					"idx": row.get("idx"),
					"item_name": row.get("item_name"),
					"qty": flt(row.get("stock_qty")),
					"uom": row.get("stock_uom"),
					"current_type": current_type,
					"suggested_type": None,
					"current_iran_class": current_class if current_class != CLASS_ORDINARY_SCRAP else exp,
					"suggested_iran_class": exp,
					"confidence": CONF_PROVEN,
					"evidence": evidence,
					"approval_required": False,
					"approval_enabled": False,
					"action": "NO CHANGE",
					"downstream_impact": "No business-type change suggested.",
				}
			)
			continue

		# --- Co/By → Scrap when component evidence is strong ---
		if current_type in (TYPE_CO, TYPE_BY):
			jc_item = _jc_item_for(job_card, item_code)
			has_xfer = _has_component_transfer(job_card, item_code)
			hist = _historical_component_scrap(item_code, fg)
			bom_type = _bom_secondary_type(row.get("bom_secondary_item"), bom_no, item_code)
			stage_meta = _has_stage_metadata(row)
			se_ev = _manufacture_se_stage_evidence(job_card, item_code)

			if jc_item:
				evidence.append(
					f"Same item is Job Card Item (transferred_qty={flt(jc_item.transferred_qty)})"
				)
			if has_xfer:
				evidence.append("Submitted Material Transfer for Manufacture exists for this JC×item")
			if hist:
				evidence.append(f"Historical COMPONENT_SCRAP Manufacture rows for pattern: {hist}")
			if bom_type:
				evidence.append(f"BOM Secondary type={bom_type}")
			if stage_meta:
				evidence.append("Explicit stage/equivalence metadata present on row")
			if se_ev:
				evidence.append(
					f"Submitted Manufacture {se_ev.parent} confirms stage "
					f"{(se_ev.secondary_item_type or se_ev.custom_output_class)}"
				)

			# Legitimate stage output protection (BOM+JC meta, or live Manufacture stamp)
			if (bom_type in (TYPE_CO, TYPE_BY, TYPE_AFG) and stage_meta) or se_ev:
				evidence.append("Legitimate Co/By-Product protected by stage evidence")
				suggestions.append(
					{
						**ident,
						"idx": row.get("idx"),
						"item_name": row.get("item_name"),
						"qty": flt(row.get("stock_qty")),
						"uom": row.get("stock_uom"),
						"current_type": current_type,
						"suggested_type": None,
						"current_iran_class": current_class,
						"suggested_iran_class": current_class,
						"confidence": CONF_PROVEN,
						"evidence": evidence,
						"approval_required": False,
						"approval_enabled": False,
						"action": "NO CHANGE",
						"downstream_impact": "Preserve legitimate stage output; do not convert to Scrap.",
					}
				)
				continue

			if stage_meta and not (jc_item and has_xfer):
				manual_review = True
				confidence = CONF_AMBIGUOUS
				block_reason = "MANUAL REVIEW REQUIRED — equivalence metadata conflicts with component suggestion"
				statuses.append("MANUAL_REVIEW")
			elif jc_item and (has_xfer or flt(jc_item.transferred_qty) > 0) and item_code != fg:
				# Strong component-scrap retype candidate
				suggested_type = TYPE_SCRAP
				suggested_class = CLASS_COMPONENT_SCRAP
				confidence = CONF_PROVEN if (has_xfer and hist) else CONF_HIGH
				if hist == 0 and has_xfer and jc_item:
					confidence = CONF_HIGH
				action = "TYPE_CHANGE_SUGGESTED"
				approval_enabled = confidence in APPROVABLE_CONFIDENCE
				evidence.append(
					"Suggested Scrap → Iran COMPONENT_SCRAP via component family (not stage Co-Product)"
				)
			elif bom_type == TYPE_SCRAP and current_type in (TYPE_CO, TYPE_BY):
				suggested_type = TYPE_SCRAP
				suggested_class = _expected_iran_class_for_type(job_card, item_code, TYPE_SCRAP, fg)
				confidence = CONF_HIGH
				action = "TYPE_CHANGE_SUGGESTED"
				approval_enabled = True
				evidence.append("BOM Secondary declares Scrap while JC says Co/By-Product")
			else:
				confidence = CONF_AMBIGUOUS
				manual_review = True
				action = "MANUAL REVIEW"
				statuses.append("MANUAL_REVIEW")

		# Equivalence safety: approved transition cannot invent factors; if meta present + retype
		if suggested_type and _has_stage_metadata(row) and suggested_type == TYPE_SCRAP:
			# Changing away from Co/By while factor/common_uom exist → manual review
			manual_review = True
			approval_enabled = False
			confidence = CONF_AMBIGUOUS
			suggested_type = None
			action = "MANUAL REVIEW"
			block_reason = (
				"MANUAL REVIEW REQUIRED — approved type change would invalidate existing "
				"explicit stage metadata; will not invent/clear factors automatically"
			)
			statuses.append("MANUAL_REVIEW")

		if manual_review and not suggested_type:
			suggestions.append(
				{
					**ident,
					"idx": row.get("idx"),
					"item_name": row.get("item_name"),
					"qty": flt(row.get("stock_qty")),
					"uom": row.get("stock_uom"),
					"current_type": current_type,
					"suggested_type": "MANUAL REVIEW",
					"current_iran_class": current_class,
					"suggested_iran_class": CLASS_UNKNOWN,
					"confidence": confidence,
					"evidence": evidence,
					"approval_required": False,
					"approval_enabled": False,
					"action": action,
					"block_reason": block_reason,
					"downstream_impact": block_reason or "Ambiguous — checkbox disabled.",
				}
			)
			continue

		if suggested_type and (current_type, suggested_type) in SUPPORTED_TRANSITIONS:
			suggestions.append(
				{
					**ident,
					"idx": row.get("idx"),
					"item_name": row.get("item_name"),
					"qty": flt(row.get("stock_qty")),
					"uom": row.get("stock_uom"),
					"current_type": current_type,
					"suggested_type": suggested_type,
					"current_iran_class": current_class,
					"suggested_iran_class": suggested_class,
					"confidence": confidence,
					"evidence": evidence,
					"approval_required": True,
					"approval_enabled": approval_enabled,
					"action": action,
					"downstream_impact": (
						f"If approved: business type {current_type}→{suggested_type}; "
						f"Iran class expected {current_class}→{suggested_class}. "
						"Stage-equivalent set must exclude this row when it becomes COMPONENT_SCRAP."
					),
				}
			)
		else:
			suggestions.append(
				{
					**ident,
					"idx": row.get("idx"),
					"item_name": row.get("item_name"),
					"qty": flt(row.get("stock_qty")),
					"uom": row.get("stock_uom"),
					"current_type": current_type,
					"suggested_type": None,
					"current_iran_class": current_class,
					"suggested_iran_class": current_class,
					"confidence": CONF_PROVEN if current_type else CONF_UNKNOWN,
					"evidence": evidence or ["No supported transition evidenced"],
					"approval_required": False,
					"approval_enabled": False,
					"action": "NO CHANGE",
					"downstream_impact": "No change.",
				}
			)

	return {
		"rows": suggestions,
		"statuses": list(dict.fromkeys(statuses)),
		"approvable_count": sum(1 for s in suggestions if s.get("approval_enabled")),
	}


def validate_type_approvals(job_card: str, approvals: list[dict], suggestions: list[dict]) -> dict:
	"""Revalidate explicit per-row approvals against current suggestions/DB."""
	if not approvals:
		return {"ok": True, "normalized": [], "errors": []}

	by_row = {
		(s.get("secondary_row") or s.get("name")): s
		for s in suggestions
		if s.get("secondary_row") or s.get("name")
	}
	normalized = []
	errors = []
	for raw in approvals:
		if isinstance(raw, str):
			try:
				raw = json.loads(raw)
			except Exception:
				errors.append({"error": "Invalid approval payload", "raw": raw})
				continue
		sec_row = (raw.get("secondary_row") or "").strip()
		appr_jc = (raw.get("job_card") or job_card or "").strip()
		if appr_jc != job_card:
			errors.append(
				{
					"error": "Approval references another Job Card",
					"secondary_row": sec_row,
					"job_card": appr_jc,
				}
			)
			continue
		if not sec_row or not frappe.db.exists("Job Card Secondary Item", sec_row):
			errors.append({"error": "Secondary row not found", "secondary_row": sec_row})
			continue
		parent = frappe.db.get_value("Job Card Secondary Item", sec_row, "parent")
		if parent != job_card:
			errors.append(
				{
					"error": "Approved row identity does not belong to selected Job Card",
					"secondary_row": sec_row,
					"parent": parent,
				}
			)
			continue
		db_type = (frappe.db.get_value("Job Card Secondary Item", sec_row, "secondary_item_type") or "").strip()
		current = (raw.get("current_type") or "").strip()
		approved = (raw.get("approved_type") or "").strip()
		fp = (raw.get("evidence_fingerprint") or "").strip()
		sug = by_row.get(sec_row)
		if not sug or not sug.get("approval_enabled"):
			errors.append({"error": "Suggestion not approvable", "secondary_row": sec_row})
			continue
		if current != db_type:
			errors.append(
				{
					"error": "STALE_PREVIEW — approval current_type differs from DB",
					"secondary_row": sec_row,
					"current": current,
					"db": db_type,
				}
			)
			continue
		if current != (sug.get("current_type") or ""):
			errors.append({"error": "STALE_PREVIEW — suggestion current_type mismatch", "secondary_row": sec_row})
			continue
		if approved != (sug.get("suggested_type") or ""):
			errors.append(
				{
					"error": "Approved type is not the suggested type",
					"secondary_row": sec_row,
					"approved": approved,
					"suggested": sug.get("suggested_type"),
				}
			)
			continue
		if (current, approved) not in SUPPORTED_TRANSITIONS:
			errors.append({"error": "Unsupported transition", "secondary_row": sec_row})
			continue
		if fp and fp != sug.get("evidence_fingerprint"):
			errors.append({"error": "STALE_PREVIEW — evidence fingerprint mismatch", "secondary_row": sec_row})
			continue
		normalized.append(
			{
				"job_card": job_card,
				"secondary_row": sec_row,
				"current_type": current,
				"approved_type": approved,
				"evidence_fingerprint": sug.get("evidence_fingerprint"),
				"item_code": sug.get("item_code"),
				"suggested_iran_class": sug.get("suggested_iran_class"),
			}
		)
	return {"ok": not errors, "normalized": normalized, "errors": errors}


def _sync_manufacture_se_secondary_type(job_card: str, item_code: str, approved_type: str) -> list[dict]:
	"""Align submitted Manufacture SE rows for JC×item without cancel/recreate.

	Writes business type + Iran CO_PRODUCT stamp when approving By/Co-Product.
	Does not change qty, warehouses, rates, SLE, or GL. Does not start RIV.
	"""
	if approved_type not in (TYPE_BY, TYPE_CO):
		return []
	rows = frappe.db.sql(
		"""
		select sed.name, sed.parent, sed.secondary_item_type, sed.custom_output_class,
		       sed.custom_output_equivalent_factor
		from `tabStock Entry` se
		join `tabStock Entry Detail` sed on sed.parent = se.name
		where se.job_card = %s
		  and se.purpose = 'Manufacture'
		  and se.docstatus = 1
		  and sed.item_code = %s
		  and ifnull(sed.t_warehouse, '') != ''
		  and ifnull(sed.s_warehouse, '') = ''
		""",
		(job_card, item_code),
		as_dict=1,
	)
	synced = []
	for row in rows:
		updates: dict[str, Any] = {}
		if (row.get("secondary_item_type") or "").strip() != approved_type:
			updates["secondary_item_type"] = approved_type
		if (row.get("custom_output_class") or "").strip() != CLASS_CO_PRODUCT:
			updates["custom_output_class"] = CLASS_CO_PRODUCT
		# Preserve positive snapshotted factors; stamp 1.0 only when unset/zero.
		if flt(row.get("custom_output_equivalent_factor")) <= 0:
			updates["custom_output_equivalent_factor"] = 1.0
		if not updates:
			synced.append(
				{
					"type": "sed_secondary_type_noop",
					"detail_name": row.name,
					"stock_entry": row.parent,
					"item_code": item_code,
					"approved_type": approved_type,
				}
			)
			continue
		frappe.db.set_value(
			"Stock Entry Detail",
			row.name,
			updates,
			update_modified=False,
		)
		frappe.clear_document_cache("Stock Entry", row.parent)
		synced.append(
			{
				"type": "sed_secondary_type",
				"detail_name": row.name,
				"stock_entry": row.parent,
				"item_code": item_code,
				"approved_type": approved_type,
				"updates": updates,
			}
		)
	return synced


def apply_type_approvals(approvals: list[dict]) -> list[dict]:
	"""Persist approved JC secondary_item_type and sync matching Manufacture SE rows.

	Does not invent rates, cancel vouchers, or start RIV.
	"""
	applied = []
	for a in approvals:
		frappe.db.set_value(
			"Job Card Secondary Item",
			a["secondary_row"],
			"secondary_item_type",
			a["approved_type"],
			update_modified=False,
		)
		applied.append({**a, "type": "secondary_item_type"})
		applied.extend(
			_sync_manufacture_se_secondary_type(
				a.get("job_card") or "",
				a.get("item_code") or "",
				a.get("approved_type") or "",
			)
		)
	return applied


__all__ = [
	"CONF_PROVEN",
	"CONF_HIGH",
	"CONF_AMBIGUOUS",
	"CONF_UNKNOWN",
	"SUPPORTED_TRANSITIONS",
	"APPROVABLE_CONFIDENCE",
	"row_identity",
	"suggest_secondary_type_changes",
	"validate_type_approvals",
	"apply_type_approvals",
	"_manufacture_se_stage_evidence",
	"_sync_manufacture_se_secondary_type",
]
