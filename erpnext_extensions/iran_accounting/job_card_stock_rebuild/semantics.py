# Copyright (c) 2026, ERPNext Extensions contributors
"""ERPNext 16.37 Job Card Item field semantics for Material-Transferred backflush.

Proven on Development (CTRL PO-JOB08761 / 13200544):

* Core ``set_transferred_qty_in_job_card_item`` sums *all* MTfM ``qty`` including
  returns → would yield 2950, but stored ``transferred_qty`` is **2890**.
* ``ManufactureEntry.add_raw_materials`` uses::

      qty = transferred_qty - consumed_qty

  under ``backflush_raw_materials_based_on = Material Transferred for Manufacture``.

Therefore Phase 1 writes:

* ``transferred_qty`` = issued − returned   (net available before consume)
* ``consumed_qty``    = Σ Manufacture consume rows (actual, never inferred)
* custom issued/returned/returnable/still_in_wip mirror site CTRL pattern
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild import statuses as S


TRACKING_FIELDS = (
	"transferred_qty",
	"consumed_qty",
	"custom_issued_qty",
	"custom_returned_qty",
	"custom_still_in_wip",
	"custom_returnable_qty",
)


def qty_equal(a, b, precision: int = 6) -> bool:
	return abs(flt(a) - flt(b)) <= (0.5 * 10 ** (-precision))


def load_job_card_items(job_card: str) -> list[dict]:
	return frappe.db.sql(
		"""
		select name, idx, item_code, source_warehouse, required_qty, transferred_qty,
		       consumed_qty, uom, stock_uom,
		       custom_issued_qty, custom_returned_qty, custom_still_in_wip,
		       custom_returnable_qty
		from `tabJob Card Item`
		where parent=%s
		order by idx
		""",
		job_card,
		as_dict=1,
	)


def resolve_unique_jc_item(job_card: str, item_code: str) -> tuple[str | None, str | None]:
	"""Return (jc_item_name, error_status). Ambiguous → AMBIGUOUS_OWNERSHIP."""
	rows = [r for r in load_job_card_items(job_card) if r.item_code == item_code]
	if not rows:
		return None, S.MANUAL_REVIEW
	if len(rows) > 1:
		return None, S.AMBIGUOUS_OWNERSHIP
	return rows[0].name, None


def derive_tracking_for_item(
	jc_item: dict | None,
	evidence_item: dict,
) -> dict[str, Any]:
	"""Derive tracking field values from stock evidence for one JC Item / batch aggregate.

	``evidence_item`` is item-level (batches already reconciled upstream).
	"""
	issued = flt(evidence_item["issued"])
	returned = flt(evidence_item["returned"])
	consumed = flt(evidence_item["consumed"])
	scrap = flt(evidence_item["component_scrap"])
	paired_scrap = flt(evidence_item.get("paired_component_scrap") or 0)
	net = issued - returned
	# Scrap output does not drain WIP again when paired with CONSUME source rows.
	still = net - consumed
	if scrap > paired_scrap + 1e-9:
		# Unpaired scrap — leave remainder math on consume only; status flags mismatch.
		pass

	derived = {
		"transferred_qty": net,
		"consumed_qty": consumed,
		"custom_issued_qty": issued,
		"custom_returned_qty": returned,
		# CTRL pattern: returnable = net issued (not reduced by consume)
		"custom_returnable_qty": net,
		"custom_still_in_wip": still if still > 0 else 0.0,
	}

	current = {f: flt(jc_item.get(f)) if jc_item else 0.0 for f in TRACKING_FIELDS}
	field_plan = []
	for f in TRACKING_FIELDS:
		cur = current[f]
		der = derived[f]
		field_plan.append(
			{
				"fieldname": f,
				"current": cur,
				"derived": der,
				"write_required": not qty_equal(cur, der),
				"source": "stock_movement_net" if f != "consumed_qty" else "manufacture_consume_rows",
				"owner": "custom" if f.startswith("custom_") else "core",
				"why": _why(f),
			}
		)

	statuses = []
	if returned > issued + 1e-9:
		statuses.append(S.RETURN_MISMATCH)
	if consumed > net + 1e-9:
		statuses.append(S.OVER_CONSUMED)
	if issued > 0 and consumed == 0 and still > 1e-9:
		statuses.append(S.MISSING_MANUFACTURE_CONSUMPTION)
	elif issued > 0 and consumed > 0 and still > 1e-9:
		statuses.append(S.PARTIAL_MANUFACTURE_CONSUMPTION)

	tracking_incomplete = any(fp["write_required"] for fp in field_plan)
	if tracking_incomplete:
		statuses.append(S.JOB_CARD_TRACKING_INCOMPLETE)

	return {
		"item_code": evidence_item["item_code"],
		"jc_item": jc_item["name"] if jc_item else None,
		"required_qty": flt(jc_item["required_qty"]) if jc_item else None,
		"issued": issued,
		"returned": returned,
		"consumed": consumed,
		"component_scrap": scrap,
		"wip_remainder": still,
		"net_available_for_manufacture": net - consumed,
		"field_plan": field_plan,
		"statuses": statuses,
		"action": "UPDATE TRACKING" if tracking_incomplete else "NO CHANGE",
	}


def _why(field: str) -> str:
	return {
		"transferred_qty": (
			"Net MTfM issued−returned. ManufactureEntry backflush uses "
			"transferred_qty − consumed_qty (ERPNext 16.37)."
		),
		"consumed_qty": "Sum of existing Manufacture source rows only; never issued−returned.",
		"custom_issued_qty": "Gross Material Transfer for Manufacture to WIP.",
		"custom_returned_qty": "Gross MTfM returns from WIP.",
		"custom_returnable_qty": "Site pattern: equals net issued (issued−returned).",
		"custom_still_in_wip": (
			"Physical remainder: issued−returned−consumed. "
			"Paired Component Scrap output is not a second WIP drain."
		),
	}.get(field, "")


def propose_link_backfills(job_card: str, evidence_item: dict) -> list[dict]:
	"""Deterministic job_card_item backfill proposals for ISSUE/RETURN rows."""
	jc_item_name, err = resolve_unique_jc_item(job_card, evidence_item["item_code"])
	proposals = []
	if err:
		return [
			{
				"action": "BLOCKED",
				"status": err,
				"detail": f"Cannot resolve unique Job Card Item for {evidence_item['item_code']}",
			}
		]
	for rec in evidence_item.get("link_candidates") or []:
		current = rec.get("job_card_item")
		if current and current != jc_item_name:
			proposals.append(
				{
					"action": "BLOCKED",
					"status": S.AMBIGUOUS_OWNERSHIP,
					"voucher": rec["voucher"],
					"detail_name": rec["detail_name"],
					"current": current,
					"derived": jc_item_name,
					"reason": "Existing job_card_item points to a different JC Item",
				}
			)
			continue
		if current == jc_item_name:
			proposals.append(
				{
					"action": "NO CHANGE",
					"voucher": rec["voucher"],
					"detail_name": rec["detail_name"],
					"current": current,
					"derived": jc_item_name,
				}
			)
			continue
		# NULL → backfill
		proposals.append(
			{
				"action": "BACKFILL LINK",
				"status": S.JOB_CARD_TRACKING_INCOMPLETE,
				"voucher": rec["voucher"],
				"detail_name": rec["detail_name"],
				"item_code": rec["item_code"],
				"batch_no": rec.get("batch_no"),
				"qty": rec["qty"],
				"bucket": rec["bucket"],
				"current": None,
				"derived": jc_item_name,
				"write_required": True,
			}
		)
	return proposals


def reconcile_batches(evidence_items: list[dict]) -> tuple[list[dict], list[str]]:
	"""Collapse batch rows to item level; detect batch mismatches."""
	by_item: dict[str, list[dict]] = {}
	for row in evidence_items:
		by_item.setdefault(row["item_code"], []).append(row)

	collapsed = []
	statuses = []
	for item_code, batches in by_item.items():
		issued = sum(flt(b["issued"]) for b in batches)
		returned = sum(flt(b["returned"]) for b in batches)
		consumed = sum(flt(b["consumed"]) for b in batches)
		scrap = sum(flt(b["component_scrap"]) for b in batches)
		paired = sum(flt(b.get("paired_component_scrap") or 0) for b in batches)
		other = sum(flt(b["other"]) for b in batches)
		# Paired scrap output is not a second WIP drain (consume already includes source).
		remainder = issued - returned - consumed - other
		batch_rems = [
			flt(b["issued"]) - flt(b["returned"]) - flt(b["consumed"]) - flt(b["other"]) for b in batches
		]
		if scrap > paired + 1e-9:
			statuses.append(S.COMPONENT_SCRAP_MISMATCH)
		if len(batches) > 1:
			if abs(sum(batch_rems) - remainder) > 1e-6:
				statuses.append(S.BATCH_MISMATCH)
			# negative batch remainder with positive item remainder → mismatch
			if any(r < -1e-9 for r in batch_rems) and remainder >= -1e-9:
				statuses.append(S.BATCH_MISMATCH)
		link_candidates = []
		evidence = []
		for b in batches:
			link_candidates.extend(b.get("link_candidates") or [])
			evidence.extend(b.get("evidence") or [])
		collapsed.append(
			{
				"item_code": item_code,
				"batch_no": batches[0]["batch_no"] if len(batches) == 1 else "",
				"batches": batches,
				"issued": issued,
				"returned": returned,
				"consumed": consumed,
				"component_scrap": scrap,
				"paired_component_scrap": paired,
				"product_reject": sum(flt(b["product_reject"]) for b in batches),
				"ordinary_scrap": sum(flt(b["ordinary_scrap"]) for b in batches),
				"other": other,
				"wip_remainder": remainder,
				"net_available_for_manufacture": issued - returned - consumed,
				"evidence": evidence,
				"link_candidates": link_candidates,
			}
		)
	return collapsed, list(dict.fromkeys(statuses))


__all__ = [
	"TRACKING_FIELDS",
	"qty_equal",
	"load_job_card_items",
	"resolve_unique_jc_item",
	"derive_tracking_for_item",
	"propose_link_backfills",
	"reconcile_batches",
]
