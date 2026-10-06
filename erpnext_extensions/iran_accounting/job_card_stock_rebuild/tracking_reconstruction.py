# Copyright (c) 2026, ERPNext Extensions contributors
"""Deterministic Job Card tracking reconstruction for Manufacture repair.

Reconstructs Job Card Item linkage and Core-compatible counters from Stock Entry /
SLE evidence so site Server Script ``Custom 6 - Manufacturing Integrity Validator``
can pass naturally on the canonical Manufacture.

Site Custom 2a semantics (authoritative for this deployment):

* ``transferred_qty`` = net MTfM ``transfer_qty`` (forward − return), keyed by
  ``job_card_item``
* ``custom_issued_qty`` / ``custom_returned_qty`` = gross forward / return
* Core ``consumed_qty`` = Σ Manufacture source ``qty`` keyed by ``job_card_item``

Historical Rahkaran rows often have ``SED.job_card_item`` NULL, so Custom 2a
never updates counters. This module backfills only SAFE_BACKFILL rows and
writes tracking fields inside the repair transaction (Dry Run rolls back).
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild import statuses as S
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.evidence import (
	build_evidence,
	collect_detail_rows,
	collect_job_card_stock_entries,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.semantics import (
	load_job_card_items,
	qty_equal,
	resolve_unique_jc_item,
)


MAP_UNIQUE = "UNIQUE"
MAP_AMBIGUOUS = "AMBIGUOUS"
MAP_MISSING = "MISSING"

SED_SAFE = "SAFE_BACKFILL"
SED_AMBIGUOUS = "AMBIGUOUS"
SED_FOREIGN = "FOREIGN"
SED_NOT_REQUIRED = "NOT_REQUIRED"


def build_jci_resolution(job_card: str) -> dict[str, Any]:
	"""Resolve item_code → Job Card Item.name with uniqueness guards."""
	items = load_job_card_items(job_card)
	by_code: dict[str, list[dict]] = {}
	for row in items:
		by_code.setdefault(row.item_code, []).append(row)

	resolved: dict[str, dict[str, Any]] = {}
	counts = {MAP_UNIQUE: 0, MAP_AMBIGUOUS: 0, MAP_MISSING: 0}
	for item_code, rows in sorted(by_code.items()):
		if len(rows) == 1:
			resolved[item_code] = {
				"item_code": item_code,
				"job_card_item": rows[0].name,
				"mapping_status": MAP_UNIQUE,
				"idx": rows[0].idx,
				"current_transferred": flt(rows[0].transferred_qty),
				"current_consumed": flt(rows[0].consumed_qty),
				"source_warehouse": rows[0].source_warehouse,
			}
			counts[MAP_UNIQUE] += 1
		elif len(rows) > 1:
			# No Item×Batch identity on Job Card Item in this site — cannot disambiguate.
			resolved[item_code] = {
				"item_code": item_code,
				"job_card_item": None,
				"mapping_status": MAP_AMBIGUOUS,
				"candidates": [r.name for r in rows],
				"current_transferred": None,
				"current_consumed": None,
			}
			counts[MAP_AMBIGUOUS] += 1
		else:
			resolved[item_code] = {
				"item_code": item_code,
				"job_card_item": None,
				"mapping_status": MAP_MISSING,
			}
			counts[MAP_MISSING] += 1

	return {
		"job_card": job_card,
		"by_item": resolved,
		"counts": counts,
		"blockers": _map_blockers(resolved),
	}


def _map_blockers(resolved: dict[str, dict]) -> list[str]:
	out = []
	for item_code, row in resolved.items():
		st = row.get("mapping_status")
		if st == MAP_AMBIGUOUS:
			out.append(f"AMBIGUOUS_OWNERSHIP: Job Card Item not unique for {item_code}")
		elif st == MAP_MISSING:
			out.append(f"MISSING Job Card Item for {item_code}")
	return out


def classify_sed_linkage(
	job_card: str,
	jci_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Classify historical SED rows for metadata-only ``job_card_item`` backfill."""
	jci_resolution = jci_resolution or build_jci_resolution(job_card)
	by_item = jci_resolution["by_item"]
	rows_out: list[dict[str, Any]] = []

	# Owned submitted + cancelled SE with header job_card
	owned = frappe.db.sql(
		"""
		select name, purpose, docstatus, is_return, job_card
		from `tabStock Entry`
		where job_card=%s and docstatus in (1, 2)
		order by name
		""",
		job_card,
		as_dict=1,
	)
	for se in owned:
		for d in collect_detail_rows(se.name):
			item = d.get("item_code")
			is_output = cint(d.get("is_finished_item")) or (
				d.get("t_warehouse") and not d.get("s_warehouse")
			)
			current = d.get("job_card_item") or None
			mapping = by_item.get(item) or {}
			status = mapping.get("mapping_status")
			target = mapping.get("job_card_item")

			if is_output:
				cls = SED_NOT_REQUIRED
				reason = "output / finished row"
			elif not item:
				cls = SED_NOT_REQUIRED
				reason = "empty item"
			elif status == MAP_AMBIGUOUS:
				cls = SED_AMBIGUOUS
				reason = "duplicate Job Card Item for item_code"
			elif status != MAP_UNIQUE or not target:
				cls = SED_AMBIGUOUS if status == MAP_AMBIGUOUS else SED_NOT_REQUIRED
				reason = f"no UNIQUE Job Card Item ({status})"
			elif current and current != target:
				cls = SED_AMBIGUOUS
				reason = f"existing job_card_item {current} ≠ {target}"
			elif current == target:
				cls = SED_NOT_REQUIRED
				reason = "already linked"
			elif se.docstatus == 1 and se.purpose in (
				"Material Transfer for Manufacture",
				"Manufacture",
				"Material Issue",
			):
				# Source / transfer rows with proven JC ownership + unique JCI.
				# Only submitted rows — cancelled history stays untouched metadata.
				cls = SED_SAFE
				reason = "header job_card + UNIQUE Job Card Item"
			elif se.docstatus == 2 and se.purpose in (
				"Material Transfer for Manufacture",
				"Manufacture",
				"Material Issue",
			):
				cls = SED_NOT_REQUIRED
				reason = "cancelled document — metadata backfill not required"
			else:
				cls = SED_NOT_REQUIRED
				reason = f"purpose {se.purpose} not linkage-scoped"

			rows_out.append(
				{
					"detail_name": d.get("name"),
					"parent": se.name,
					"purpose": se.purpose,
					"docstatus": se.docstatus,
					"is_return": cint(se.is_return),
					"item_code": item,
					"batch_no": d.get("batch_no") or "",
					"qty": flt(d.get("transfer_qty") or d.get("qty")),
					"s_warehouse": d.get("s_warehouse"),
					"t_warehouse": d.get("t_warehouse"),
					"current_job_card_item": current,
					"derived_job_card_item": target if cls == SED_SAFE else current,
					"classification": cls,
					"reason": reason,
					"write_required": cls == SED_SAFE,
				}
			)

	# SED on named logistics without header job_card but previously in scope → FOREIGN
	# (not auto-backfilled)

	counts = {
		SED_SAFE: sum(1 for r in rows_out if r["classification"] == SED_SAFE),
		SED_AMBIGUOUS: sum(1 for r in rows_out if r["classification"] == SED_AMBIGUOUS),
		SED_FOREIGN: sum(1 for r in rows_out if r["classification"] == SED_FOREIGN),
		SED_NOT_REQUIRED: sum(1 for r in rows_out if r["classification"] == SED_NOT_REQUIRED),
		"total": len(rows_out),
	}
	return {
		"job_card": job_card,
		"rows": rows_out,
		"counts": counts,
		"safe_backfills": [r for r in rows_out if r["write_required"]],
	}


def _evidence_by_item(job_card: str) -> dict[str, dict]:
	ev = build_evidence(job_card)
	out: dict[str, dict] = {}
	for it in ev.get("items") or []:
		code = it["item_code"]
		# evidence items may already be batch-collapsed or not — aggregate
		cur = out.setdefault(
			code,
			{
				"item_code": code,
				"issued": 0.0,
				"returned": 0.0,
				"consumed": 0.0,
				"component_scrap": 0.0,
				"paired_component_scrap": 0.0,
				"other": 0.0,
				"batches": [],
			},
		)
		cur["issued"] += flt(it.get("issued"))
		cur["returned"] += flt(it.get("returned"))
		cur["consumed"] += flt(it.get("consumed"))
		cur["component_scrap"] += flt(it.get("component_scrap"))
		cur["paired_component_scrap"] += flt(it.get("paired_component_scrap"))
		cur["other"] += flt(it.get("other"))
		if it.get("batch_no"):
			cur["batches"].append(it.get("batch_no"))
	return out


def propose_tracking_reconstruction(
	job_card: str,
	dispositions: list[dict] | None = None,
	canonical_consume_by_item: dict[str, float] | None = None,
	jci_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Build preview + apply payload for tracking repair.

	``transferred_qty`` (Custom 2a / Core backflush input) = issued − returned.

	``consumed_qty`` proposed *after* Apply = canonical Manufacture source consume
	for that item (physical source rows; scrap output is not a second drain).

	Pre-submit ``consumed_qty`` written inside the transaction (after old MFG
	cancel) is 0 when all manufactures are being replaced — Custom 6 then sees
	``need`` from the new document against ``transferred_qty``.
	"""
	jci_resolution = jci_resolution or build_jci_resolution(job_card)
	sed = classify_sed_linkage(job_card, jci_resolution)
	evidence = _evidence_by_item(job_card)
	disp_by_item: dict[str, dict] = {}
	for d in dispositions or []:
		code = d.get("item_code")
		if not code:
			continue
		cur = disp_by_item.setdefault(
			code,
			{
				"proposed_consumed": 0.0,
				"proposed_return": 0.0,
				"proposed_still_in_wip": 0.0,
				"proposed_scrap": 0.0,
			},
		)
		cur["proposed_consumed"] += flt(d.get("proposed_consumed"))
		cur["proposed_return"] += flt(d.get("proposed_return"))
		cur["proposed_still_in_wip"] += flt(d.get("proposed_still_in_wip"))
		cur["proposed_scrap"] += flt(d.get("proposed_scrap"))

	canonical_consume_by_item = canonical_consume_by_item or {}
	components: list[dict[str, Any]] = []
	blockers = list(jci_resolution.get("blockers") or [])

	for item_code, mapping in sorted(jci_resolution["by_item"].items()):
		ev = evidence.get(item_code) or {}
		issued = flt(ev.get("issued"))
		returned = flt(ev.get("returned"))
		hist_consumed = flt(ev.get("consumed"))
		scrap = flt(ev.get("component_scrap"))
		net = issued - returned
		disp = disp_by_item.get(item_code) or {}
		# Post-Apply consumed = what the single canonical Manufacture will hold
		post_consumed = flt(canonical_consume_by_item.get(item_code))
		if post_consumed <= 1e-9 and hist_consumed > 1e-9 and flt(disp.get("proposed_consumed")) <= 1e-9:
			# Fully historical consume restated on canonical via merge
			post_consumed = hist_consumed
		if flt(disp.get("proposed_consumed")) > 1e-9:
			# Extra disposition consume replaces/extends remainder
			# Canonical merge already includes hist + extra; prefer explicit map
			if item_code in canonical_consume_by_item:
				post_consumed = flt(canonical_consume_by_item[item_code])
			else:
				post_consumed = hist_consumed + flt(disp.get("proposed_consumed"))

		sed_links = [
			r
			for r in sed["safe_backfills"]
			if r["item_code"] == item_code
		]
		components.append(
			{
				"item_code": item_code,
				"job_card_item": mapping.get("job_card_item"),
				"mapping_status": mapping.get("mapping_status"),
				"issued": issued,
				"returned": returned,
				"hist_consumed": hist_consumed,
				"component_scrap": scrap,
				"remaining_wip": net - hist_consumed - flt(ev.get("other")),
				"current_transferred": flt(mapping.get("current_transferred")),
				"proposed_transferred": net if mapping.get("mapping_status") == MAP_UNIQUE else None,
				"current_consumed": flt(mapping.get("current_consumed")),
				"proposed_consumed_after_apply": post_consumed
				if mapping.get("mapping_status") == MAP_UNIQUE
				else None,
				# Pre-submit (after cancel of merged MFG): no active consume yet
				"proposed_consumed_pre_submit": 0.0
				if mapping.get("mapping_status") == MAP_UNIQUE
				else None,
				"sed_links_to_backfill": len(sed_links),
				"sed_link_details": [
					{"detail_name": r["detail_name"], "parent": r["parent"], "qty": r["qty"]}
					for r in sed_links
				],
				"batches": ev.get("batches") or [],
			}
		)

	if sed["counts"].get(SED_AMBIGUOUS):
		# Ambiguous SED that already has a conflicting link blocks; NULL+UNIQUE is SAFE
		conflict = [
			r
			for r in sed["rows"]
			if r["classification"] == SED_AMBIGUOUS
			and r.get("current_job_card_item")
			and r.get("current_job_card_item") != r.get("derived_job_card_item")
		]
		for r in conflict[:20]:
			blockers.append(
				f"AMBIGUOUS SED link {r['detail_name']} ({r['item_code']}): {r['reason']}"
			)

	return {
		"job_card": job_card,
		"jci_resolution": jci_resolution,
		"sed_linkage": {
			"counts": sed["counts"],
			"safe_backfills": sed["safe_backfills"],
			"rows": sed["rows"],
		},
		"components": components,
		"blockers": list(dict.fromkeys(blockers)),
		"custom6_contract": _custom6_contract_table(components),
	}


def _custom6_contract_table(components: list[dict]) -> list[dict]:
	"""Document Custom 6 field expectations for UI / audit."""
	rows = []
	for c in components:
		if c.get("mapping_status") != MAP_UNIQUE:
			continue
		rows.append(
			{
				"item_code": c["item_code"],
				"checks": [
					{
						"validator_check": "source row names Job Card Item",
						"required_field": "Stock Entry Detail.job_card_item",
						"current_value": None,
						"correct_value": c["job_card_item"],
						"evidence_source": "UNIQUE Job Card Item map",
					},
					{
						"validator_check": "consume ≤ transferred",
						"required_field": "Job Card Item.transferred_qty",
						"current_value": c["current_transferred"],
						"correct_value": c["proposed_transferred"],
						"evidence_source": "issued − returned (Custom 2a / MTfM net)",
					},
					{
						"validator_check": "stranded WIP at Manufacture close",
						"required_field": "Job Card Item.consumed_qty + Manufacture need",
						"current_value": c["current_consumed"],
						"correct_value": c["proposed_consumed_after_apply"],
						"evidence_source": "canonical Manufacture source consume",
					},
				],
			}
		)
	return rows


def apply_sed_backfills(safe_backfills: list[dict]) -> list[dict]:
	"""Metadata-only ``job_card_item`` writes. No SLE/GL/qty/rate changes."""
	applied = []
	for row in safe_backfills or []:
		if not row.get("write_required"):
			continue
		detail = row["detail_name"]
		target = row["derived_job_card_item"]
		if not detail or not target:
			continue
		before = frappe.db.get_value(
			"Stock Entry Detail",
			detail,
			["job_card_item", "qty", "basic_rate", "valuation_rate", "basic_amount"],
			as_dict=1,
		)
		if not before:
			continue
		if before.job_card_item == target:
			continue
		frappe.db.set_value(
			"Stock Entry Detail",
			detail,
			"job_card_item",
			target,
			update_modified=False,
		)
		after = frappe.db.get_value(
			"Stock Entry Detail",
			detail,
			["job_card_item", "qty", "basic_rate", "valuation_rate", "basic_amount"],
			as_dict=1,
		)
		applied.append(
			{
				"detail_name": detail,
				"parent": row.get("parent"),
				"item_code": row.get("item_code"),
				"before": before.job_card_item,
				"after": after.job_card_item,
				"qty_unchanged": qty_equal(before.qty, after.qty),
				"rate_unchanged": qty_equal(before.basic_rate, after.basic_rate)
				and qty_equal(before.valuation_rate, after.valuation_rate),
			}
		)
	return applied


def apply_job_card_tracking(
	job_card: str,
	components: list[dict],
	*,
	phase: str = "pre_submit",
) -> list[dict]:
	"""Write Core-compatible JC Item counters.

	phase:
	  * ``pre_submit`` — transferred = issued−returned; consumed = 0 (post-cancel)
	  * ``post_apply_preview`` — not written here; Core updates consumed on submit
	"""
	applied = []
	for c in components or []:
		jci = c.get("job_card_item")
		if not jci or c.get("mapping_status") != MAP_UNIQUE:
			continue
		if phase == "pre_submit":
			xfer = flt(c.get("proposed_transferred"))
			cons = flt(c.get("proposed_consumed_pre_submit"))
		else:
			xfer = flt(c.get("proposed_transferred"))
			cons = flt(c.get("proposed_consumed_after_apply"))
		issued = flt(c.get("issued"))
		returned = flt(c.get("returned"))
		standing = xfer - cons
		updates = {
			"transferred_qty": xfer,
			"consumed_qty": cons,
			"custom_issued_qty": issued,
			"custom_returned_qty": returned,
			"custom_returnable_qty": standing if standing > 0 else 0.0,
			"custom_still_in_wip": standing if standing > 0 else 0.0,
		}
		before = frappe.db.get_value(
			"Job Card Item",
			jci,
			list(updates.keys()),
			as_dict=1,
		)
		for field, value in updates.items():
			if not qty_equal(flt(before.get(field)), value):
				frappe.db.set_value("Job Card Item", jci, field, value, update_modified=False)
		after = frappe.db.get_value("Job Card Item", jci, list(updates.keys()), as_dict=1)
		applied.append(
			{
				"job_card_item": jci,
				"item_code": c.get("item_code"),
				"phase": phase,
				"before": {k: flt(before.get(k)) for k in updates},
				"after": {k: flt(after.get(k)) for k in updates},
			}
		)
	return applied


def stamp_canonical_consume_rows(
	rows: list[dict],
	jci_resolution: dict[str, Any],
) -> tuple[list[dict], list[str]]:
	"""Stamp ``job_card_item`` on CONSUME / source rows. Block if any remain NULL."""
	by_item = (jci_resolution or {}).get("by_item") or {}
	out = []
	errors = []
	for idx, row in enumerate(rows or [], start=1):
		r = dict(row)
		is_source = (r.get("type") == "CONSUME") or (
			r.get("s_warehouse") and not r.get("t_warehouse") and not cint(r.get("is_finished_item"))
		)
		if is_source:
			mapping = by_item.get(r.get("item_code")) or {}
			if mapping.get("mapping_status") != MAP_UNIQUE or not mapping.get("job_card_item"):
				errors.append(
					f"Row {idx} ({r.get('item_code')}): cannot stamp Job Card Item "
					f"({mapping.get('mapping_status') or MAP_MISSING})"
				)
			else:
				r["job_card_item"] = mapping["job_card_item"]
				r["jci_mapping_status"] = MAP_UNIQUE
		out.append(r)
	return out, errors


def canonical_consume_totals(rows: list[dict]) -> dict[str, float]:
	totals: dict[str, float] = {}
	for r in rows or []:
		is_source = (r.get("type") == "CONSUME") or (
			r.get("s_warehouse") and not r.get("t_warehouse") and not cint(r.get("is_finished_item"))
		)
		if is_source and r.get("item_code"):
			totals[r["item_code"]] = totals.get(r["item_code"], 0.0) + flt(r.get("qty"))
	return totals


def assert_canonical_jci_complete(rows: list[dict]) -> None:
	missing = []
	for idx, r in enumerate(rows or [], start=1):
		is_source = (r.get("type") == "CONSUME") or (
			r.get("s_warehouse") and not r.get("t_warehouse") and not cint(r.get("is_finished_item"))
		)
		if is_source and not r.get("job_card_item"):
			missing.append(f"Row {idx} ({r.get('item_code')})")
	if missing:
		frappe.throw(
			"Canonical Manufacture blocked — source rows missing job_card_item: "
			+ ", ".join(missing)
		)


__all__ = [
	"MAP_UNIQUE",
	"MAP_AMBIGUOUS",
	"MAP_MISSING",
	"SED_SAFE",
	"SED_AMBIGUOUS",
	"SED_FOREIGN",
	"SED_NOT_REQUIRED",
	"build_jci_resolution",
	"classify_sed_linkage",
	"propose_tracking_reconstruction",
	"apply_sed_backfills",
	"apply_job_card_tracking",
	"stamp_canonical_consume_rows",
	"canonical_consume_totals",
	"assert_canonical_jci_complete",
]
