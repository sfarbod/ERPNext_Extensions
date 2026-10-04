# Copyright (c) 2026, ERPNext Extensions contributors
"""Build canonical Manufacture repair plan (v5.5.0) — preview only until Apply."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.evidence import collect_detail_rows
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
	scan_golden_rule,
	validate_dispositions,
)


_ALLOWED_LOGISTICS_PURPOSES = {
	"Material Transfer",
	"Material Transfer for Manufacture",
	"Material Issue",
}
_MAX_LOGISTICS_DOCS = 25


def _se_sort_key(name: str) -> tuple:
	r = frappe.db.get_value(
		"Stock Entry", name, ["posting_date", "posting_time", "creation"], as_dict=1
	)
	if not r:
		return ("", "", "", name)
	return (str(r.posting_date), str(r.posting_time), str(r.creation), name)


def _detail_batch(row: dict) -> str:
	batch = (row.get("batch_no") or "").strip()
	if batch:
		return batch
	bundle = row.get("serial_and_batch_bundle")
	if not bundle:
		return ""
	bb = frappe.db.sql(
		"select batch_no from `tabSerial and Batch Entry` where parent=%s and ifnull(batch_no,'')!='' limit 1",
		bundle,
	)
	return bb[0][0] if bb else ""


def _fg_batch_moves(item_code: str, batch_no: str, after_voucher: str | None = None) -> list[dict]:
	if not item_code or not batch_no:
		return []
	direct = frappe.db.sql(
		"""
		select se.name, se.purpose, se.docstatus, se.posting_date, se.posting_time,
		       se.creation, se.job_card, se.work_order, sed.qty, sed.s_warehouse, sed.t_warehouse,
		       sed.item_code, sed.batch_no, se.modified
		from `tabStock Entry Detail` sed
		join `tabStock Entry` se on se.name=sed.parent
		where sed.item_code=%s and sed.batch_no=%s and se.docstatus=1
		order by se.posting_date, se.posting_time, se.creation, se.name
		""",
		(item_code, batch_no),
		as_dict=1,
	)
	via_bundle = frappe.db.sql(
		"""
		select se.name, se.purpose, se.docstatus, se.posting_date, se.posting_time,
		       se.creation, se.job_card, se.work_order, sed.qty, sed.s_warehouse, sed.t_warehouse,
		       sed.item_code, %s as batch_no, se.modified
		from `tabSerial and Batch Entry` sbe
		join `tabStock Entry Detail` sed on sed.serial_and_batch_bundle=sbe.parent
		join `tabStock Entry` se on se.name=sed.parent
		where sed.item_code=%s and sbe.batch_no=%s and se.docstatus=1
		  and ifnull(sed.batch_no,'')=''
		order by se.posting_date, se.posting_time, se.creation, se.name
		""",
		(batch_no, item_code, batch_no),
		as_dict=1,
	)
	seen = set()
	out = []
	for mv in list(direct) + list(via_bundle):
		if mv.name in seen:
			continue
		seen.add(mv.name)
		out.append(mv)
	out.sort(key=lambda x: (str(x.posting_date), str(x.posting_time), str(x.creation), x.name))
	return out


def _has_delivery_note(item_code: str, batch_no: str) -> list[str]:
	if not batch_no:
		return []
	rows = frappe.db.sql(
		"""
		select distinct dn.name
		from `tabDelivery Note Item` dni
		join `tabDelivery Note` dn on dn.name=dni.parent
		where dni.item_code=%s and dni.batch_no=%s and dn.docstatus=1
		limit 5
		""",
		(item_code, batch_no),
		as_dict=1,
	)
	if rows:
		return [r.name for r in rows]
	# SLE lineage (batch may only exist on SLE via bundle rebuild)
	sle = frappe.db.sql(
		"""
		select distinct voucher_no
		from `tabStock Ledger Entry`
		where item_code=%s and batch_no=%s and is_cancelled=0
		  and voucher_type='Delivery Note'
		limit 5
		""",
		(item_code, batch_no),
	)
	return [r[0] for r in sle]


def _se_lines(name: str) -> list[dict]:
	rows = frappe.db.sql(
		"""
		select item_code, batch_no, serial_and_batch_bundle, s_warehouse, t_warehouse, qty
		from `tabStock Entry Detail` where parent=%s
		""",
		name,
		as_dict=1,
	)
	for r in rows:
		r.batch_no = _detail_batch(r)
	return rows


def _seed_tracked_keys(seed_names: list[str]) -> set[tuple[str, str]]:
	"""Item×Batch keys present on seed logistics vouchers (incl. shared lines)."""
	keys: set[tuple[str, str]] = set()
	for name in seed_names:
		for ln in _se_lines(name):
			if ln.item_code and ln.batch_no:
				keys.add((ln.item_code, ln.batch_no))
	return keys


def _expand_future_outbound(
	seed_names: list[str],
	tracked_keys: set[tuple[str, str]] | None = None,
) -> tuple[list[str], list[str]]:
	"""Add later SEs required to safely cancel seed logistics.

	Tracked keys are frozen from the seed vouchers. Expansion follows only those
	Item×Batch pairs so shared multi-item quarantine transfers can be cancelled
	without recursively adopting unrelated site transfers.
	"""
	seen = set(seed_names)
	queue = list(seed_names)
	blockers: list[str] = []
	tracked_keys = tracked_keys or _seed_tracked_keys(seed_names)
	while queue:
		if len(seen) > _MAX_LOGISTICS_DOCS:
			blockers.append(
				f"MERGE_BLOCKED: downstream logistics chain exceeds {_MAX_LOGISTICS_DOCS} documents"
			)
			break
		cur = queue.pop(0)
		cdt = _se_sort_key(cur)
		purpose = frappe.db.get_value("Stock Entry", cur, "purpose")
		if purpose and purpose not in _ALLOWED_LOGISTICS_PURPOSES and cur not in seed_names:
			blockers.append(f"MERGE_BLOCKED: unsupported logistics purpose {purpose} on {cur}")
			continue
		for ln in _se_lines(cur):
			if not ln.t_warehouse or not ln.batch_no:
				continue
			key = (ln.item_code, ln.batch_no)
			if key not in tracked_keys:
				continue
			dns = _has_delivery_note(ln.item_code, ln.batch_no)
			if dns:
				blockers.append(
					f"MERGE_BLOCKED: Delivery Note / sales stock dependency ({', '.join(dns)})"
				)
				continue
			cands = frappe.db.sql(
				"""
				select distinct se.name, se.purpose, se.posting_date, se.posting_time, se.creation
				from `tabStock Entry Detail` sed
				join `tabStock Entry` se on se.name=sed.parent
				where se.docstatus=1 and sed.item_code=%s and ifnull(sed.batch_no,'')=%s
				  and sed.s_warehouse=%s and se.name!=%s
				""",
				(ln.item_code, ln.batch_no, ln.t_warehouse, cur),
				as_dict=1,
			)
			cands2 = frappe.db.sql(
				"""
				select distinct se.name, se.purpose, se.posting_date, se.posting_time, se.creation
				from `tabSerial and Batch Entry` sbe
				join `tabStock Entry Detail` sed on sed.serial_and_batch_bundle=sbe.parent
				join `tabStock Entry` se on se.name=sed.parent
				where se.docstatus=1 and sed.item_code=%s and sbe.batch_no=%s
				  and sed.s_warehouse=%s and se.name!=%s
				""",
				(ln.item_code, ln.batch_no, ln.t_warehouse, cur),
				as_dict=1,
			)
			for c in list(cands) + list(cands2):
				if c.name in seen:
					continue
				if (str(c.posting_date), str(c.posting_time), str(c.creation), c.name) <= cdt:
					continue
				if c.purpose not in _ALLOWED_LOGISTICS_PURPOSES:
					blockers.append(
						f"MERGE_BLOCKED: unsupported downstream purpose {c.purpose} on {c.name}"
					)
					continue
				seen.add(c.name)
				queue.append(c.name)
	ordered = sorted(seen, key=_se_sort_key, reverse=True)
	return ordered, blockers


def _collect_mfg_rows(mfg_names: list[str]) -> list[dict]:
	out = []
	for name in mfg_names:
		for d in collect_detail_rows(name):
			row = dict(d)
			row["source_voucher"] = name
			out.append(row)
	return out


def _merge_consume_rows(details: list[dict], extra_consume: list[dict]) -> list[dict]:
	"""Merge CONSUME by Item×Batch; keep scrap/FG/secondary as listed."""
	consume: dict[tuple[str, str], dict] = {}
	outputs = []
	for d in details:
		qty = flt(d.get("transfer_qty") or d.get("qty"))
		item = d.get("item_code")
		batch = d.get("batch_no") or ""
		s_wh = d.get("s_warehouse")
		t_wh = d.get("t_warehouse")
		out_class = (d.get("custom_output_class") or "").strip()
		is_fg = cint(d.get("is_finished_item")) or out_class == "MAIN_FG"
		is_scrap = out_class == "COMPONENT_SCRAP" or (
			(d.get("secondary_item_type") or "") == "Scrap" and out_class != "MAIN_PRODUCT_REJECT"
		)
		if s_wh and not t_wh:
			key = (item, batch)
			if key not in consume:
				consume[key] = {
					"type": "CONSUME",
					"item_code": item,
					"batch_no": batch,
					"qty": 0.0,
					"s_warehouse": s_wh,
					"t_warehouse": None,
					"valuation_rate": flt(d.get("valuation_rate")),
					"basic_rate": flt(d.get("basic_rate")),
					"custom_output_class": None,
					"secondary_item_type": None,
					"is_finished_item": 0,
					"rate_source": "historical_mfg",
				}
			consume[key]["qty"] += qty
		elif is_fg or t_wh:
			outputs.append(
				{
					"type": "MAIN_FG"
					if is_fg
					else ("COMPONENT_SCRAP" if is_scrap else (out_class or d.get("secondary_item_type") or "OUTPUT")),
					"item_code": item,
					"batch_no": batch,
					"qty": qty,
					"s_warehouse": s_wh,
					"t_warehouse": t_wh,
					"valuation_rate": flt(d.get("valuation_rate")),
					"basic_rate": flt(d.get("basic_rate")),
					"custom_output_class": out_class or ("MAIN_FG" if is_fg else None),
					"secondary_item_type": d.get("secondary_item_type"),
					"is_finished_item": 1 if is_fg else 0,
					"rate_source": "historical_mfg",
				}
			)
	for e in extra_consume:
		key = (e["item_code"], e.get("batch_no") or "")
		if key not in consume:
			consume[key] = {
				"type": "CONSUME",
				"item_code": e["item_code"],
				"batch_no": e.get("batch_no") or "",
				"qty": 0.0,
				"s_warehouse": e.get("s_warehouse"),
				"t_warehouse": None,
				"valuation_rate": flt(e.get("valuation_rate")),
				"basic_rate": flt(e.get("basic_rate")),
				"custom_output_class": None,
				"secondary_item_type": None,
				"is_finished_item": 0,
				"rate_source": e.get("rate_source") or "issue_transfer",
			}
		consume[key]["qty"] += flt(e["qty"])
		if e.get("valuation_rate"):
			consume[key]["valuation_rate"] = flt(e["valuation_rate"])
			consume[key]["basic_rate"] = flt(e.get("basic_rate") or e["valuation_rate"])
			consume[key]["rate_source"] = e.get("rate_source") or consume[key]["rate_source"]
	return list(consume.values()) + outputs


def discover_downstream(job_card: str, mfg_names: list[str]) -> dict:
	"""FG logistics after Manufacture that may need TEMP CANCEL/RECREATE.

	Also expands later outbound dependents of shared multi-item logistics SEs
	so cancel does not trip future negative-stock validation.
	"""
	fg_rows = frappe.db.sql(
		"""
		select sed.item_code, sed.batch_no, sed.serial_and_batch_bundle, sed.qty,
		       sed.t_warehouse, se.name as voucher
		from `tabStock Entry Detail` sed
		join `tabStock Entry` se on se.name=sed.parent
		where se.name in %s and (sed.is_finished_item=1 or sed.custom_output_class='MAIN_FG')
		""",
		(mfg_names or ["__none__"],),
		as_dict=1,
	)
	for fr in fg_rows:
		fr.batch_no = _detail_batch(fr)

	seed = []
	blocked = []
	seen = set()
	fg_keys: set[tuple[str, str]] = set()
	for fr in fg_rows:
		if fr.item_code and fr.batch_no:
			fg_keys.add((fr.item_code, fr.batch_no))
		dns = _has_delivery_note(fr.item_code, fr.batch_no or "")
		if dns:
			blocked.append(
				{"reason": "Delivery Note dependency", "documents": dns, "batch_no": fr.batch_no}
			)
		for mv in _fg_batch_moves(fr.item_code, fr.batch_no or ""):
			if mv.name in mfg_names:
				continue
			if mv.purpose == "Manufacture":
				continue
			if mv.purpose not in _ALLOWED_LOGISTICS_PURPOSES:
				blocked.append(
					{
						"reason": f"Unsupported logistics purpose {mv.purpose}",
						"documents": [mv.name],
						"batch_no": fr.batch_no,
					}
				)
				continue
			if mv.name in seen:
				continue
			seen.add(mv.name)
			seed.append(mv.name)

	# Seed logistics may be shared multi-item transfers. Track:
	# 1) Manufacture FG Item×Batch keys, and
	# 2) every Item×Batch on the seed vouchers (needed so cancel order
	#    reverses later outbounds of co-moved items and avoids negative stock).
	# Expansion still refuses unsupported purposes / DN / oversized chains.
	tracked = set(fg_keys) | _seed_tracked_keys(seed)
	expanded_names, expand_blockers = _expand_future_outbound(seed, tracked_keys=tracked)
	for msg in expand_blockers:
		blocked.append({"reason": msg, "documents": [], "batch_no": None})

	logistics = []
	for name in expanded_names:
		meta = frappe.db.get_value(
			"Stock Entry",
			name,
			["name", "purpose", "docstatus", "posting_date", "posting_time", "creation", "modified"],
			as_dict=1,
		)
		if meta:
			logistics.append(meta)
	# Reverse chrono already from expand; keep stable
	logistics.sort(key=lambda x: _se_sort_key(x.name), reverse=True)
	return {"logistics": logistics, "blocked": blocked, "fg_rows": fg_rows, "seed": seed}


def build_manufacture_plan(
	job_card: str,
	dispositions: list[dict] | None = None,
	merge_documents: list[str] | None = None,
	stamp_mode: str | None = None,
) -> dict[str, Any]:
	"""Authoritative repair plan from server evidence + user choices."""
	scan = scan_golden_rule(job_card)
	ok, errors, normalized = validate_dispositions(scan["rows"], dispositions or [])
	mfgs = scan["manufactures"]
	mfg_names = [m.name for m in mfgs]
	if merge_documents is None:
		merge_documents = list(mfg_names)
	else:
		merge_documents = [n for n in merge_documents if n in mfg_names]

	blockers = list(errors)
	if scan.get("stamp_conflict") and stamp_mode not in ("HISTORICAL", "MIGRATE"):
		blockers.append("FINANCE DECISION REQUIRED: incompatible historical stamps")
	if not merge_documents and mfg_names:
		blockers.append("No Manufacture selected to merge/replace")
	for r in scan["rows"]:
		if r["status"] == "SCRAP MISMATCH":
			blockers.append(f"SCRAP MISMATCH {r['item_code']} / {r['batch_no']}")

	downstream = discover_downstream(job_card, merge_documents or mfg_names)
	for b in downstream.get("blocked") or []:
		reason = b.get("reason") if isinstance(b, dict) else str(b)
		if reason and reason not in blockers:
			if str(reason).startswith("MERGE_BLOCKED"):
				blockers.append(str(reason))
			else:
				blockers.append(f"MERGE_BLOCKED: {reason}")

	# Extra consume from dispositions
	extra = []
	wip = scan.get("wip_warehouse")
	for d in normalized:
		qty = flt(d.get("proposed_consumed"))
		if qty <= 1e-9:
			continue
		# Rate from latest JC issue for item×batch
		rate_row = frappe.db.sql(
			"""
			select sed.valuation_rate, sed.basic_rate, sed.s_warehouse, sed.t_warehouse
			from `tabStock Entry Detail` sed
			join `tabStock Entry` se on se.name=sed.parent
			where se.job_card=%s and se.purpose='Material Transfer for Manufacture'
			  and se.docstatus=1 and ifnull(se.is_return,0)=0 and sed.item_code=%s
			  and ifnull(sed.batch_no,'')=%s
			order by se.posting_date desc, se.posting_time desc
			limit 1
			""",
			(job_card, d["item_code"], d.get("batch_no") or ""),
			as_dict=1,
		)
		rate = flt(rate_row[0].valuation_rate) if rate_row else 0
		extra.append(
			{
				"item_code": d["item_code"],
				"batch_no": d.get("batch_no") or "",
				"qty": qty,
				"s_warehouse": wip,
				"valuation_rate": rate,
				"basic_rate": rate,
				"rate_source": "issue_transfer",
			}
		)
		if flt(d.get("proposed_return")) > 1e-9:
			# Returns stay as separate Return Components — noted in plan, not Manufacture
			pass
		if flt(d.get("proposed_scrap")) > 1e-9:
			blockers.append(
				f"{d['item_code']}: proposed Component Scrap on unresolved WIP requires MANUAL REVIEW in MVP"
			)
		if flt(d.get("proposed_still_in_wip")) > 1e-9:
			blockers.append(
				f"{d['item_code']}: STILL IN WIP approved — Manufacture repair for this remainder is skipped"
			)

	details = _collect_mfg_rows(merge_documents)
	canonical_rows = _merge_consume_rows(details, extra) if details or extra else list(
		_merge_consume_rows([], extra)
	)

	# Posting datetime from latest selected MFG
	posting_date = None
	posting_time = None
	fg_qty = 0.0
	for m in mfgs:
		if m.name in merge_documents:
			posting_date = m.posting_date
			posting_time = m.posting_time
			fg_qty += flt(m.fg_completed_qty)

	stamp = scan.get("historical_stamp")
	if stamp_mode == "MIGRATE":
		from erpnext_extensions.iran_accounting.scrap_costing import MANUFACTURE_COSTING_CONTRACT_VERSION

		stamp = MANUFACTURE_COSTING_CONTRACT_VERSION
	elif stamp_mode == "HISTORICAL" or not stamp_mode:
		stamp = scan.get("historical_stamp")

	returns_needed = [
		{
			"item_code": d["item_code"],
			"batch_no": d.get("batch_no") or "",
			"qty": flt(d.get("proposed_return")),
		}
		for d in normalized
		if flt(d.get("proposed_return")) > 1e-9
	]

	plan_fp = hashlib.sha256(
		json.dumps(
			{
				"scan_fp": scan["fingerprint"],
				"dispositions": normalized,
				"merge_documents": merge_documents,
				"stamp": stamp,
				"stamp_mode": stamp_mode or "HISTORICAL",
				"logistics": [x.name for x in downstream["logistics"]],
			},
			default=str,
			sort_keys=True,
		).encode()
	).hexdigest()

	documents = []
	for m in mfgs:
		documents.append(
			{
				"name": m.name,
				"role": "MERGE" if m.name in merge_documents else "KEEP",
				"fg_completed_qty": flt(m.fg_completed_qty),
				"stamp": m.stamp,
				"posting_date": str(m.posting_date),
			}
		)
	for lg in downstream["logistics"]:
		documents.append(
			{
				"name": lg.name,
				"role": "TEMP CANCEL / RECREATE",
				"purpose": lg.purpose,
				"posting_date": str(lg.posting_date),
			}
		)

	apply_allowed = not blockers and bool(merge_documents) and (
		any(flt(d.get("proposed_consumed")) > 0 for d in normalized)
		or len(merge_documents) > 1
		or any(flt(r.get("remaining_wip")) > 1e-9 for r in scan["rows"]) is False and len(merge_documents) >= 1
	)
	# Allow repair when missing consumption dispositions present OR multi-mfg merge
	needs_repair = any(flt(d.get("proposed_consumed")) > 0 for d in normalized) or len(merge_documents) > 1
	if not needs_repair and not blockers:
		blockers.append("Nothing to repair")
		apply_allowed = False
	if blockers:
		apply_allowed = False

	return {
		"job_card": job_card,
		"work_order": scan["work_order"],
		"scan": scan,
		"dispositions": normalized,
		"merge_documents": merge_documents,
		"documents": documents,
		"downstream_logistics": downstream["logistics"],
		"downstream_blocked": downstream["blocked"],
		"canonical_manufacture": {
			"purpose": "Manufacture",
			"job_card": job_card,
			"work_order": scan["work_order"],
			"posting_date": str(posting_date) if posting_date else None,
			"posting_time": str(posting_time) if posting_time else None,
			"fg_completed_qty": fg_qty,
			"historical_stamp": stamp,
			"stamp_mode": stamp_mode or "HISTORICAL",
			"rows": canonical_rows,
			"supersedes": list(merge_documents),
		},
		"returns_needed": returns_needed,
		"blockers": blockers,
		"apply_allowed": apply_allowed,
		"fingerprint": plan_fp,
		"scan_fingerprint": scan["fingerprint"],
	}
