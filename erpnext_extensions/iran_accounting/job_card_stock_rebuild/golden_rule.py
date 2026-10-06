# Copyright (c) 2026, ERPNext Extensions contributors
"""Golden Rule scan for Job Card × Item × Batch (v5.5.0)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.evidence import (
	build_evidence,
	collect_detail_rows,
	collect_job_card_stock_entries,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.scrap_pairing import (
	json_safe_pairing,
	pair_scrap_for_job_card_evidence,
)


STATUS_OK = "OK"
STATUS_MISSING_CONSUMPTION = "MISSING CONSUMPTION"
STATUS_UNRESOLVED_WIP = "MISSING RETURN / UNRESOLVED WIP"
STATUS_SCRAP_MISMATCH = "SCRAP MISMATCH"
STATUS_MULTIPLE_MANUFACTURE = "MULTIPLE MANUFACTURE"
STATUS_BLOCKED = "BLOCKED"

DISPOSITIONS = ("CONSUMED", "SCRAP", "RETURN", "STILL IN WIP", "MANUAL REVIEW")


def _suggest(item: dict, jc_meta: dict) -> dict:
	rem = flt(item.get("wip_remainder"))
	if rem <= 1e-9:
		return {"action": None, "qty": 0.0, "confidence": None, "reason": None}
	completed = (jc_meta.get("status") or "") == "Completed" or flt(jc_meta.get("total_completed_qty")) > 0
	has_fg = cint(jc_meta.get("has_manufacture_fg"))
	if completed and has_fg and flt(item.get("consumed")) <= 1e-9 and rem > 1e-9:
		return {
			"action": "CONSUMED",
			"qty": rem,
			"confidence": "HIGH",
			"reason": "Completed Job Card with FG Manufacture and unexplained WIP remainder",
		}
	if completed and flt(item.get("consumed")) > 0 and rem > 1e-9:
		return {
			"action": "CONSUMED",
			"qty": rem,
			"confidence": "MEDIUM",
			"reason": "Partial consumption with residual WIP on completed Job Card",
		}
	return {
		"action": "MANUAL REVIEW",
		"qty": rem,
		"confidence": "LOW",
		"reason": "Unresolved WIP without strong consumption evidence",
	}


def _row_status(item: dict, unpaired: bool, multi_mfg: bool) -> str:
	if unpaired:
		return STATUS_SCRAP_MISMATCH
	if multi_mfg and flt(item.get("wip_remainder")) <= 1e-9:
		return STATUS_MULTIPLE_MANUFACTURE
	rem = flt(item.get("wip_remainder"))
	if rem <= 1e-9 and flt(item.get("consumed")) >= 0:
		if rem < -1e-9:
			return STATUS_BLOCKED
		return STATUS_OK if not multi_mfg else STATUS_MULTIPLE_MANUFACTURE
	if flt(item.get("consumed")) <= 1e-9 and rem > 1e-9:
		return STATUS_MISSING_CONSUMPTION
	if rem > 1e-9:
		return STATUS_UNRESOLVED_WIP
	return STATUS_OK


def scan_golden_rule(job_card: str) -> dict[str, Any]:
	"""Read-only Golden Rule table + document discovery for one Job Card."""
	if not job_card or not frappe.db.exists("Job Card", job_card):
		frappe.throw(frappe._("Job Card {0} not found").format(job_card))

	jc = frappe.db.get_value(
		"Job Card",
		job_card,
		["name", "status", "docstatus", "work_order", "for_quantity", "total_completed_qty", "modified", "wip_warehouse"],
		as_dict=1,
	)
	evidence = build_evidence(job_card)
	pairing = pair_scrap_for_job_card_evidence(evidence.get("movements") or [])
	unpaired_keys = {
		(u.get("item_code"), u.get("batch_no") or "") for u in (pairing.get("unpaired_scrap") or [])
	}

	mfgs = frappe.db.sql(
		"""
		select name, docstatus, posting_date, posting_time, fg_completed_qty, amended_from,
		       custom_manufacturing_costing_contract_version as stamp, custom_rahkaran_no as rahkaran,
		       modified
		from `tabStock Entry`
		where job_card=%s and purpose='Manufacture' and docstatus=1
		order by posting_date, posting_time, name
		""",
		job_card,
		as_dict=1,
	)
	multi_mfg = len(mfgs) > 1
	has_fg = False
	for m in mfgs:
		if flt(m.fg_completed_qty) > 0:
			has_fg = True
			break
	jc_meta = {**jc, "has_manufacture_fg": has_fg}

	rows = []
	for item in evidence.get("items") or []:
		# Golden Rule scope is components issued to WIP, not FG/secondary outputs.
		if (
			flt(item.get("issued")) <= 1e-9
			and flt(item.get("returned")) <= 1e-9
			and flt(item.get("consumed")) <= 1e-9
			and flt(item.get("mi_consumed")) <= 1e-9
			and flt(item.get("component_scrap")) <= 1e-9
		):
			continue
		key = (item["item_code"], item.get("batch_no") or "")
		unpaired = key in unpaired_keys or not item.get("scrap_pair_ok", True)
		status = _row_status(item, unpaired, multi_mfg)
		suggestion = _suggest(item, jc_meta) if status != STATUS_OK else {
			"action": None, "qty": 0.0, "confidence": None, "reason": None
		}
		# If MI already proves consumption, prefer OK / merge suggestion over missing consume.
		if flt(item.get("mi_consumed")) > 1e-9 and flt(item.get("wip_remainder")) <= 1e-9:
			status = STATUS_MULTIPLE_MANUFACTURE if multi_mfg else STATUS_OK
			suggestion = {
				"action": None,
				"qty": 0.0,
				"confidence": "HIGH",
				"reason": "Material Issue already proves WIP outflow; merge into Manufacture if selected",
			}
		rows.append(
			{
				"item_code": item["item_code"],
				"batch_no": item.get("batch_no") or "",
				"issued": flt(item["issued"]),
				"returned": flt(item["returned"]),
				"consumed": flt(item["consumed"]),
				"mi_consumed": flt(item.get("mi_consumed") or 0),
				"scrap": flt(item["component_scrap"]),
				"paired_scrap": flt(item.get("paired_component_scrap") or 0),
				"remaining_wip": flt(item["wip_remainder"]),
				"suggested_action": suggestion["action"],
				"suggested_qty": suggestion["qty"],
				"confidence": suggestion["confidence"],
				"reason": suggestion["reason"],
				"proposed_consumed": 0.0,
				"proposed_scrap": 0.0,
				"proposed_return": 0.0,
				"proposed_still_in_wip": 0.0,
				"status": status,
			}
		)

	# Pre-fill proposed from suggestion for UI convenience (not applied)
	for r in rows:
		if r["suggested_action"] == "CONSUMED":
			r["proposed_consumed"] = flt(r["suggested_qty"])
		elif r["suggested_action"] == "SCRAP":
			r["proposed_scrap"] = flt(r["suggested_qty"])
		elif r["suggested_action"] == "RETURN":
			r["proposed_return"] = flt(r["suggested_qty"])
		elif r["suggested_action"] == "STILL IN WIP":
			r["proposed_still_in_wip"] = flt(r["suggested_qty"])

	raw_stamps = [(m.stamp or "").strip() for m in mfgs]
	named = {s for s in raw_stamps if s}
	blank = any(not s for s in raw_stamps)
	# Mixed blank + named stamps, or multiple distinct named stamps → Finance decision
	stamp_conflict = len(named) > 1 or (bool(named) and blank and len(mfgs) > 1)
	historical_stamp = None
	if len(named) == 1 and not stamp_conflict:
		historical_stamp = next(iter(named))
	elif len(mfgs) == 1:
		historical_stamp = mfgs[0].stamp
	elif named:
		# Prefer latest named stamp as default display; conflict still blocks Apply
		historical_stamp = next(iter(named))

	fingerprint = hashlib.sha256(
		json.dumps(
			{
				"jc": jc.name,
				"jc_modified": str(jc.modified),
				"wo": jc.work_order,
				"wo_modified": str(
					frappe.db.get_value("Work Order", jc.work_order, "modified") if jc.work_order else ""
				),
				"mfgs": [(m.name, m.docstatus, str(m.modified), flt(m.fg_completed_qty)) for m in mfgs],
				"rows": [
					(r["item_code"], r["batch_no"], r["issued"], r["returned"], r["consumed"], r["remaining_wip"])
					for r in rows
				],
				"pairing_ok": pairing.get("ok"),
			},
			default=str,
			sort_keys=True,
		).encode()
	).hexdigest()

	return {
		"job_card": job_card,
		"work_order": jc.work_order,
		"jc_status": jc.status,
		"for_quantity": flt(jc.for_quantity),
		"total_completed_qty": flt(jc.total_completed_qty),
		"wip_warehouse": jc.wip_warehouse,
		"rows": rows,
		"manufactures": mfgs,
		"multiple_manufacture": multi_mfg,
		"historical_stamp": historical_stamp,
		"stamp_conflict": stamp_conflict,
		"scrap_pairing": json_safe_pairing(pairing),
		"fingerprint": fingerprint,
		"evidence_fingerprint": evidence.get("fingerprint"),
	}


def validate_dispositions(
	rows: list[dict],
	dispositions: list[dict],
	offset_exempt_keys: set[tuple[str, str]] | None = None,
) -> tuple[bool, list[str], list[dict]]:
	"""Validate user dispositions against authoritative remaining WIP.

	``offset_exempt_keys`` — Job Card × Item × Batch keys covered by an
	explicit APPROVED_BATCH_OFFSET (v5.5.14). Those rows require zero stock
	disposition and do not trigger "disposition required".
	"""
	errors = []
	exempt = offset_exempt_keys or set()
	by_key = {(r["item_code"], r.get("batch_no") or ""): r for r in rows}
	normalized = []
	seen = set()
	for d in dispositions or []:
		key = (d.get("item_code"), d.get("batch_no") or "")
		if key not in by_key:
			errors.append(f"Unknown Item×Batch {key}")
			continue
		seen.add(key)
		base = by_key[key]
		rem = flt(base["remaining_wip"])
		c = flt(d.get("proposed_consumed"))
		s = flt(d.get("proposed_scrap"))
		r = flt(d.get("proposed_return"))
		w = flt(d.get("proposed_still_in_wip"))
		if key in exempt:
			if max(c, s, r, w) > 1e-9:
				errors.append(
					f"{key}: BATCH_OFFSET approved — no stock disposition allowed "
					"(NO_STOCK_DOCUMENT_CHANGE)"
				)
				continue
			normalized.append(
				{
					**base,
					"proposed_consumed": 0,
					"proposed_scrap": 0,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
					"disposition": "APPROVED_BATCH_OFFSET",
					"repair_status": "APPROVED_BATCH_OFFSET",
				}
			)
			continue
		if min(c, s, r, w) < -1e-9:
			errors.append(f"{key}: negative allocation")
			continue
		total = c + s + r + w
		if rem <= 1e-9:
			if total > 1e-9:
				errors.append(f"{key}: no unresolved WIP to allocate")
			normalized.append({**base, "proposed_consumed": 0, "proposed_scrap": 0, "proposed_return": 0, "proposed_still_in_wip": 0})
			continue
		if abs(total - rem) > 1e-6:
			errors.append(f"{key}: allocations {total} != remaining {rem}")
			continue
		normalized.append(
			{
				**base,
				"proposed_consumed": c,
				"proposed_scrap": s,
				"proposed_return": r,
				"proposed_still_in_wip": w,
				"disposition": d.get("disposition") or (
					"CONSUMED" if c >= rem - 1e-9 else "SPLIT" if c + s + r + w > 0 else "MANUAL REVIEW"
				),
			}
		)
	# Unresolved rows without disposition
	for key, base in by_key.items():
		if key in seen:
			continue
		if key in exempt:
			normalized.append(
				{
					**base,
					"proposed_consumed": 0,
					"proposed_scrap": 0,
					"proposed_return": 0,
					"proposed_still_in_wip": 0,
					"disposition": "APPROVED_BATCH_OFFSET",
					"repair_status": "APPROVED_BATCH_OFFSET",
				}
			)
			continue
		if flt(base["remaining_wip"]) > 1e-9:
			errors.append(f"{key}: disposition required for remaining {base['remaining_wip']}")
		else:
			normalized.append({**base, "proposed_consumed": 0, "proposed_scrap": 0, "proposed_return": 0, "proposed_still_in_wip": 0})
	return not errors, errors, normalized
