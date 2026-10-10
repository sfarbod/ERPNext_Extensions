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


def classify_logistics_document(
	name: str,
	fg_keys: set[tuple[str, str]],
	cancel_set_desc: list[str] | None = None,
) -> dict[str, Any]:
	"""DEDICATED / SHARED_RECREATE_SAFE / SHARED_BLOCKED / UNRELATED."""
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.shared_logistics import (
		classify_shared_logistics,
	)

	return classify_shared_logistics(name, fg_keys, cancel_set_desc=cancel_set_desc)


def _expand_future_outbound(
	seed_names: list[str],
	tracked_keys: set[tuple[str, str]] | None = None,
) -> tuple[list[str], list[str]]:
	"""Add later SEs required to safely cancel seed logistics.

	Tracked keys MUST be Manufacture FG Item×Batch only (batch-scoped).
	Do not adopt co-moved unrelated items from shared multi-item transfers.
	"""
	seen = set(seed_names)
	queue = list(seed_names)
	blockers: list[str] = []
	tracked_keys = tracked_keys or set()
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


def _dept_value(row: dict | None) -> str:
	return (str((row or {}).get("department") or "")).strip()


def _merge_department_into(target: dict, source: dict, conflicts: list[dict]) -> None:
	"""Copy department when unambiguous; record AMBIGUOUS_DEPARTMENT conflicts."""
	incoming = _dept_value(source)
	if not incoming:
		return
	existing = _dept_value(target)
	if not existing:
		target["department"] = incoming
		if source.get("department_evidence"):
			target["department_evidence"] = source.get("department_evidence")
		return
	if existing == incoming:
		return
	conflicts.append(
		{
			"item_code": target.get("item_code") or source.get("item_code"),
			"batch_no": target.get("batch_no") or source.get("batch_no") or "",
			"candidates": sorted({existing, incoming}),
			"evidence": list(
				dict.fromkeys(
					[
						*(target.get("department_evidence") or []),
						*(source.get("department_evidence") or []),
						source.get("source_voucher"),
					]
				)
			),
		}
	)
	target["_department_ambiguous"] = True


def resolve_mtfm_department(job_card: str, item_code: str, batch_no: str = "") -> dict[str, Any]:
	"""Deterministic Department from JC-owned non-return MTfM SED (item×batch)."""
	rows = frappe.db.sql(
		"""
		select se.name, sed.department
		from `tabStock Entry Detail` sed
		join `tabStock Entry` se on se.name=sed.parent
		where se.job_card=%s and se.purpose='Material Transfer for Manufacture'
		  and se.docstatus=1 and ifnull(se.is_return,0)=0 and sed.item_code=%s
		  and ifnull(sed.batch_no,'')=%s and ifnull(sed.department,'')!=''
		order by se.posting_date desc, se.posting_time desc, se.name desc
		""",
		(job_card, item_code, batch_no or ""),
		as_dict=1,
	)
	depts = sorted({_dept_value(r) for r in rows if _dept_value(r)})
	evidence = list(dict.fromkeys(r.name for r in rows))
	if not depts:
		return {"department": None, "candidates": [], "evidence": evidence, "ok": False}
	if len(depts) > 1:
		return {
			"department": None,
			"candidates": depts,
			"evidence": evidence,
			"ok": False,
			"ambiguous": True,
		}
	return {
		"department": depts[0],
		"candidates": depts,
		"evidence": evidence,
		"ok": True,
		"ambiguous": False,
	}


def stamp_canonical_departments(
	job_card: str,
	rows: list[dict],
	merge_documents: list[str] | None = None,
) -> tuple[list[dict], list[str]]:
	"""Ensure every canonical SED row has a proven Department (or block)."""
	out: list[dict] = []
	blockers: list[str] = []
	# Historical template SED by strong identity for output / consume fill-in
	hist_by_key: dict[tuple, list[dict]] = defaultdict(list)
	for name in merge_documents or []:
		for d in collect_detail_rows(name):
			batch = d.get("batch_no") or ""
			if not batch and d.get("serial_and_batch_bundle"):
				batch = _detail_batch(d)
			out_class = (d.get("custom_output_class") or "").strip()
			key = (
				d.get("item_code"),
				batch,
				d.get("s_warehouse") or "",
				d.get("t_warehouse") or "",
				cint(d.get("is_finished_item")),
				out_class,
			)
			hist_by_key[key].append(
				{
					"department": _dept_value(d),
					"source_voucher": name,
					"department_evidence": [name],
				}
			)

	for idx, row in enumerate(rows or [], start=1):
		r = dict(row)
		if r.get("_department_ambiguous"):
			cands = sorted({_dept_value(r), *((r.get("_dept_candidates") or []))})
			blockers.append(
				"AMBIGUOUS_DEPARTMENT: "
				f"{r.get('item_code')} / {r.get('batch_no') or ''} "
				f"candidates={cands} evidence={r.get('department_evidence') or []}"
			)
			out.append(r)
			continue
		if _dept_value(r):
			out.append(r)
			continue

		# Prefer matching historical Manufacture SED (same warehouses / class)
		out_class = (r.get("custom_output_class") or "").strip()
		hkey = (
			r.get("item_code"),
			r.get("batch_no") or "",
			r.get("s_warehouse") or "",
			r.get("t_warehouse") or "",
			cint(r.get("is_finished_item")),
			out_class,
		)
		hist = hist_by_key.get(hkey) or []
		hist_depts = sorted({h["department"] for h in hist if h.get("department")})
		if len(hist_depts) == 1:
			r["department"] = hist_depts[0]
			r["department_evidence"] = list(
				dict.fromkeys(x for h in hist for x in (h.get("department_evidence") or []))
			)
			out.append(r)
			continue
		if len(hist_depts) > 1:
			blockers.append(
				"AMBIGUOUS_DEPARTMENT: "
				f"{r.get('item_code')} / {r.get('batch_no') or ''} "
				f"candidates={hist_depts} evidence="
				f"{list(dict.fromkeys(x for h in hist for x in (h.get('department_evidence') or [])))}"
			)
			out.append(r)
			continue

		# CONSUME / source: JC MTfM fallback
		is_source = (r.get("type") == "CONSUME") or (
			r.get("s_warehouse") and not r.get("t_warehouse") and not cint(r.get("is_finished_item"))
		)
		if is_source:
			resolved = resolve_mtfm_department(job_card, r.get("item_code"), r.get("batch_no") or "")
			if resolved.get("ok"):
				r["department"] = resolved["department"]
				r["department_evidence"] = resolved.get("evidence") or []
				out.append(r)
				continue
			if resolved.get("ambiguous"):
				blockers.append(
					"AMBIGUOUS_DEPARTMENT: "
					f"{r.get('item_code')} / {r.get('batch_no') or ''} "
					f"candidates={resolved.get('candidates')} evidence={resolved.get('evidence')}"
				)
				out.append(r)
				continue

		blockers.append(
			"MISSING_DEPARTMENT: "
			f"Row {idx} {r.get('item_code')} / {r.get('batch_no') or ''} "
			f"type={r.get('type')} — no unique historical/MTfM Department"
		)
		out.append(r)
	return out, blockers


def _merge_consume_rows(details: list[dict], extra_consume: list[dict]) -> list[dict]:
	"""Merge CONSUME by Item×Batch; keep scrap/FG/secondary as listed."""
	consume: dict[tuple[str, str], dict] = {}
	outputs = []
	dept_conflicts: list[dict] = []
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
		dept = _dept_value(d)
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
					"department": dept or None,
					"cost_center": d.get("cost_center"),
					"department_evidence": [d.get("source_voucher")] if dept else [],
				}
			else:
				_merge_department_into(
					consume[key],
					{
						"item_code": item,
						"batch_no": batch,
						"department": dept,
						"department_evidence": [d.get("source_voucher")] if dept else [],
						"source_voucher": d.get("source_voucher"),
					},
					dept_conflicts,
				)
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
					"department": dept or None,
					"cost_center": d.get("cost_center"),
					"department_evidence": [d.get("source_voucher")] if dept else [],
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
				"source_voucher": e.get("source_voucher"),
				"source_lineage": e.get("source_lineage") or "",
				"department": _dept_value(e) or None,
				"cost_center": e.get("cost_center"),
				"department_evidence": list(e.get("department_evidence") or []),
			}
		else:
			_merge_department_into(consume[key], e, dept_conflicts)
		consume[key]["qty"] += flt(e["qty"])
		if e.get("valuation_rate"):
			consume[key]["valuation_rate"] = flt(e["valuation_rate"])
			consume[key]["basic_rate"] = flt(e.get("basic_rate") or e["valuation_rate"])
			consume[key]["rate_source"] = e.get("rate_source") or consume[key]["rate_source"]
		if e.get("source_lineage"):
			prev = consume[key].get("source_lineage") or ""
			consume[key]["source_lineage"] = (
				(prev + "; " if prev else "") + e["source_lineage"]
			).strip("; ")
			consume[key]["source_voucher"] = e.get("source_voucher") or consume[key].get(
				"source_voucher"
			)
	merged = list(consume.values()) + outputs
	# Attach conflict markers for stamp_canonical_departments
	for c in dept_conflicts:
		for row in merged:
			if row.get("item_code") == c.get("item_code") and (row.get("batch_no") or "") == (
				c.get("batch_no") or ""
			):
				row["_department_ambiguous"] = True
				row["_dept_candidates"] = c.get("candidates") or []
				row["department_evidence"] = list(
					dict.fromkeys(
						(row.get("department_evidence") or []) + (c.get("evidence") or [])
					)
				)
	return merged


def discover_downstream(job_card: str, mfg_names: list[str]) -> dict:
	"""FG logistics after Manufacture that may need TEMP CANCEL/RECREATE.

	Dependency graph is batch-scoped to Manufacture FG Item×Batch only.
	Shared multi-item logistics with later unrelated outbounds → SHARED_BLOCKED.
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

	# Proposed cancel set = FG seed only (reverse chrono). Co-moved unrelated items
	# do NOT expand the graph. Classify with the full seed set for cancel-safety.
	seed_desc = sorted(seed, key=_se_sort_key, reverse=True)
	seed_audit = [
		classify_logistics_document(n, fg_keys, cancel_set_desc=seed_desc) for n in seed
	]
	for aud in seed_audit:
		if aud["shared_class"] == "SHARED_BLOCKED":
			blocked.append(
				{
					"reason": (
						f"SHARED_BLOCKED logistics {aud['name']}: {aud.get('reason') or 'unsafe'}"
					),
					"documents": [aud["name"]],
					"batch_no": None,
					"shared_class": "SHARED_BLOCKED",
					"unrelated_rows": aud.get("unrelated_rows"),
				}
			)

	# Expand ONLY Manufacture FG Item×Batch keys (minimal safe closure).
	# Entire seed must be DEDICATED or SHARED_RECREATE_SAFE — one SHARED_BLOCKED
	# sibling blocks the cancel set unless a temporary Material Receipt bridge
	# can cover the exact cancel shortfall (no foreign-chain expansion).
	_SAFE = ("DEDICATED", "SHARED_RECREATE_SAFE")
	seed_all_safe = bool(seed_audit) and all(a["shared_class"] in _SAFE for a in seed_audit)
	temporary_bridge: dict[str, Any] = {
		"required": False,
		"shortages": [],
		"cancel_names": list(seed_desc),
	}
	bridge_unlock = False
	if seed and not seed_all_safe:
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.temporary_stock_bridge import (
			plan_temporary_bridge,
		)

		temporary_bridge = plan_temporary_bridge(seed_desc)
		# Unlock only when exact shortages are known and every row has a rate.
		if temporary_bridge.get("required") and temporary_bridge.get("shortages"):
			rates_ok = all(
				flt(r.get("valuation_rate")) > 0 and flt(r.get("shortage_qty")) > 0
				for r in temporary_bridge["shortages"]
			)
			if rates_ok:
				bridge_unlock = True
				# Drop SHARED_BLOCKED stock-shortfall blockers — bridge covers them.
				blocked = [b for b in blocked if b.get("shared_class") != "SHARED_BLOCKED"]
				for aud in seed_audit:
					if aud.get("shared_class") == "SHARED_BLOCKED":
						aud["bridge_unlock"] = True
						aud["shared_reason_raw"] = aud.get("reason")
						aud["reason"] = (
							"Cancel shortfall bridged by temporary Material Receipt; "
							+ (aud.get("reason") or "")
						)
				safe_seed = [
					a["name"]
					for a in seed_audit
					if a["shared_class"] in _SAFE or a.get("bridge_unlock")
				]
			else:
				safe_seed = []
		else:
			# Do not partially cancel a FG chain when a sibling shared doc is blocked.
			safe_seed = []
	else:
		safe_seed = [a["name"] for a in seed_audit if a["shared_class"] in _SAFE]
	expanded_names, expand_blockers = _expand_future_outbound(
		safe_seed, tracked_keys=set(fg_keys)
	)
	for msg in expand_blockers:
		blocked.append({"reason": msg, "documents": [], "batch_no": None})

	# Re-classify expanded FG-only names (usually empty beyond seed) with full cancel set
	cancel_probe = sorted(set(expanded_names) | set(safe_seed), key=_se_sort_key, reverse=True)
	logistics = []
	audit_by_name = {a["name"]: a for a in seed_audit}
	for name in sorted(set(cancel_probe) | set(seed), key=_se_sort_key, reverse=True):
		meta = frappe.db.get_value(
			"Stock Entry",
			name,
			["name", "purpose", "docstatus", "posting_date", "posting_time", "creation", "modified"],
			as_dict=1,
		)
		if not meta:
			continue
		aud = audit_by_name.get(name) or classify_logistics_document(
			name, fg_keys, cancel_set_desc=seed_desc
		)
		meta["shared_class"] = aud.get("shared_class")
		meta["shared_reason"] = aud.get("reason")
		meta["related_rows"] = aud.get("related_rows")
		meta["unrelated_rows"] = aud.get("unrelated_rows")
		meta["bridge_unlock"] = bool(aud.get("bridge_unlock"))
		meta["in_cancel_set"] = name in cancel_probe and (
			aud.get("shared_class") in _SAFE or aud.get("bridge_unlock")
		)
		logistics.append(meta)

	cancel_logistics = [x for x in logistics if x.get("in_cancel_set")]
	cancel_logistics.sort(key=lambda x: _se_sort_key(x.name), reverse=True)
	if bridge_unlock:
		temporary_bridge["required"] = True
		temporary_bridge["unlock"] = True
	return {
		"logistics": cancel_logistics,
		"logistics_audit": logistics,
		"blocked": blocked,
		"fg_rows": fg_rows,
		"seed": seed,
		"fg_keys": [{"item_code": i, "batch_no": b} for i, b in sorted(fg_keys)],
		"minimal_cancel_set": [x.name for x in cancel_logistics],
		"temporary_bridge": temporary_bridge,
	}


def _noop_manufacture_plan(
	*,
	job_card: str,
	scan: dict[str, Any],
	normalized: list[dict],
	merge_documents: list[str],
	merge_material_issues: list[str],
	mi_docs: list[dict],
	client_merge,
	stamp_mode: str | None,
	blockers: list[str],
	offset_candidates: list,
	offset_res: dict,
	partial_candidates: list,
	partial_res: dict,
	replace_candidates: list,
	replace_res: dict,
	replace_keys: set,
) -> dict[str, Any]:
	"""Balanced / no-action plan: no logistics cancel probe, no bridge, no rebuild."""
	noop_blockers = list(blockers)
	if not any("Nothing to repair" in str(b) for b in noop_blockers):
		noop_blockers.append("Nothing to repair")
	plan_fp = hashlib.sha256(
		json.dumps(
			{
				"scan_fp": scan.get("fingerprint"),
				"dispositions": normalized,
				"merge_documents": merge_documents,
				"merge_material_issues": merge_material_issues,
				"stamp_mode": stamp_mode or "HISTORICAL",
				"noop": True,
			},
			default=str,
			sort_keys=True,
		).encode()
	).hexdigest()
	documents = []
	for m in scan.get("manufactures") or []:
		documents.append(
			{
				"name": m.name,
				"purpose": "Manufacture",
				"role": "KEEP",
				"ownership": "MANUFACTURE",
				"fg_completed_qty": flt(m.fg_completed_qty),
				"stamp": m.stamp,
				"posting_date": str(m.posting_date),
			}
		)
	return {
		"job_card": job_card,
		"work_order": scan.get("work_order"),
		"scan": scan,
		"dispositions": normalized,
		"merge_documents": merge_documents,
		"merge_material_issues": merge_material_issues,
		"material_issues": mi_docs,
		"documents": documents,
		"downstream_logistics": [],
		"downstream_logistics_audit": [],
		"downstream_blocked": [],
		"minimal_cancel_set": [],
		"temporary_bridge": {"required": False, "shortages": []},
		"canonical_manufacture": {
			"purpose": "Manufacture",
			"job_card": job_card,
			"work_order": scan.get("work_order"),
			"posting_date": None,
			"posting_time": None,
			"fg_completed_qty": 0.0,
			"historical_stamp": scan.get("historical_stamp"),
			"stamp_mode": stamp_mode or "HISTORICAL",
			"rows": [],
			"supersedes": [],
			"noop": True,
		},
		"tracking_repair": {"components": [], "blockers": []},
		"jci_resolution": {},
		"returns_needed": [],
		"blockers": noop_blockers,
		"apply_allowed": False,
		"fingerprint": plan_fp,
		"scan_fingerprint": scan.get("fingerprint"),
		"client_merge_provided": client_merge is not None,
		"batch_offset_candidates": offset_candidates,
		"batch_offset_approvals": offset_res.get("audit") or [],
		"approved_batch_offsets": offset_res.get("approved_groups") or [],
		"approved_batch_offset_keys": [],
		"batch_offset_preview": [],
		"partial_batch_offset_candidates": partial_candidates,
		"partial_batch_offset_approvals": partial_res.get("audit") or [],
		"approved_partial_batch_offsets": partial_res.get("approved_groups") or [],
		"partial_batch_offset_preview": [],
		"manufacture_batch_replace_candidates": replace_candidates,
		"manufacture_batch_replace_approvals": replace_res.get("audit") or [],
		"approved_manufacture_batch_replacements": replace_res.get("approved_groups") or [],
		"approved_manufacture_batch_replace_keys": [
			{"item_code": k[0], "batch_no": k[1]} for k in sorted(replace_keys)
		],
		"manufacture_batch_replace_preview": [],
		"stock_repair_needed": False,
		"exception_only": False,
		"approved_offset_items": [],
		"repair_required": False,
	}


def build_manufacture_plan(
	job_card: str,
	dispositions: list[dict] | None = None,
	merge_documents: list[str] | None = None,
	stamp_mode: str | None = None,
	merge_material_issues: list[str] | None = None,
	batch_offset_approvals: list[dict] | None = None,
	partial_batch_offset_approvals: list[dict] | None = None,
	manufacture_batch_replace_approvals: list[dict] | None = None,
) -> dict[str, Any]:
	"""Authoritative repair plan from server evidence + user choices."""
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.batch_offset import (
		STATUS_APPROVED,
		STATUS_APPROVED_PARTIAL,
		detect_batch_offset_candidates,
		detect_partial_batch_offset_candidates,
		resolve_batch_offset_approvals,
		resolve_partial_batch_offset_approvals,
	)
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_batch_replace import (
		STATUS_APPROVED_REPLACE,
		apply_replacements_to_canonical,
		detect_manufacture_batch_replace_candidates,
		resolve_manufacture_batch_replace_approvals,
	)
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.mi_ownership import (
		ACTION_BLOCKED,
		MI_BLOCKED,
		MI_MERGE_SAFE,
		discover_material_issues,
		mi_consume_rows_for_merge,
	)

	scan = scan_golden_rule(job_card)
	offset_candidates = detect_batch_offset_candidates(job_card, scan["rows"])
	offset_res = resolve_batch_offset_approvals(
		job_card, scan["rows"], batch_offset_approvals, candidates=offset_candidates
	)
	partial_candidates = detect_partial_batch_offset_candidates(
		job_card, scan["rows"], full_candidates=offset_candidates
	)
	partial_res = resolve_partial_batch_offset_approvals(
		job_card,
		scan["rows"],
		partial_batch_offset_approvals,
		candidates=partial_candidates,
	)
	replace_candidates = detect_manufacture_batch_replace_candidates(
		job_card, scan["rows"]
	)
	offset_exempt_for_conflict = set(offset_res["exempt_keys"] or set()) | set(
		partial_res["exempt_keys"] or set()
	)
	replace_res = resolve_manufacture_batch_replace_approvals(
		job_card,
		scan["rows"],
		manufacture_batch_replace_approvals,
		candidates=replace_candidates,
		offset_approved_keys=offset_exempt_for_conflict,
	)
	exempt_keys = (
		set(offset_res["exempt_keys"] or set())
		| set(partial_res["exempt_keys"] or set())
		| set(replace_res["exempt_keys"] or set())
	)
	ok, errors, normalized = validate_dispositions(
		scan["rows"],
		dispositions or [],
		offset_exempt_keys=exempt_keys,
	)
	# Distinguish partial / replace approved disposition labels for audit/UI.
	partial_keys = set(partial_res.get("exempt_keys") or set())
	replace_keys = set(replace_res.get("exempt_keys") or set())
	for d in normalized:
		key = (d.get("item_code"), d.get("batch_no") or "")
		if key in replace_keys:
			d["disposition"] = STATUS_APPROVED_REPLACE
			d["repair_status"] = STATUS_APPROVED_REPLACE
		elif key in partial_keys:
			d["disposition"] = STATUS_APPROVED_PARTIAL
			d["repair_status"] = STATUS_APPROVED_PARTIAL
	errors = (
		list(errors)
		+ list(offset_res.get("blockers") or [])
		+ list(partial_res.get("blockers") or [])
		+ list(replace_res.get("blockers") or [])
	)
	mfgs = scan["manufactures"]
	mfg_names = [m.name for m in mfgs]
	client_merge = merge_documents
	if merge_documents is None:
		merge_documents = list(mfg_names)
	else:
		merge_documents = [n for n in merge_documents if n in mfg_names]

	# Material Issue ownership (evidence items carry mi_consumed)
	evidence_items = (scan.get("evidence_items") or [])
	if not evidence_items:
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.evidence import (
			build_evidence,
		)

		evidence_items = build_evidence(job_card).get("items") or []
	mi_docs = discover_material_issues(job_card, evidence_items)
	mi_by_name = {m["name"]: m for m in mi_docs}
	if merge_material_issues is None:
		# Default: propose MERGE_SAFE only (user still sees checkbox; Apply needs selection)
		merge_material_issues = [
			m["name"] for m in mi_docs if m.get("classification") == MI_MERGE_SAFE and m.get("propose_merge")
		]
	else:
		merge_material_issues = [n for n in merge_material_issues if n in mi_by_name]
		for n in list(merge_material_issues):
			cls = mi_by_name[n]
			if cls.get("classification") == MI_BLOCKED or cls.get("shared_document"):
				errors.append(f"Material Issue {n} is BLOCKED ({cls.get('reason')})")
				merge_material_issues.remove(n)

	blockers = list(errors)
	if scan.get("stamp_conflict") and stamp_mode not in ("HISTORICAL", "MIGRATE"):
		blockers.append("FINANCE DECISION REQUIRED: incompatible historical stamps")
	if not merge_documents and mfg_names:
		blockers.append("No Manufacture selected to merge/replace")
	for r in scan["rows"]:
		if r["status"] == "SCRAP MISMATCH":
			blockers.append(f"SCRAP MISMATCH {r['item_code']} / {r['batch_no']}")

	# Early NO-OP gate (v5.5.22 UX): when no disposition / approval requires a
	# stock repair, do NOT discover downstream logistics, cancel-probe, or plan
	# a temporary bridge. A balanced Job Card must not build a hypothetical
	# destructive repair plan or emit SHARED_BLOCKED / Insufficient Stock noise.
	has_replace_approval_early = bool(replace_res.get("approved_groups"))
	has_offset_approval_early = bool(offset_res.get("approved_groups")) or bool(
		partial_res.get("approved_groups")
	)
	early_stock_repair = (
		any(flt(d.get("proposed_consumed")) > 0 for d in normalized)
		or any(flt(d.get("proposed_scrap")) > 0 for d in normalized)
		or any(flt(d.get("proposed_return")) > 0 for d in normalized)
		or len(merge_documents) > 1
		or bool(merge_material_issues)
		or has_replace_approval_early
	)
	early_needs_repair = early_stock_repair or has_offset_approval_early
	if not early_needs_repair:
		return _noop_manufacture_plan(
			job_card=job_card,
			scan=scan,
			normalized=normalized,
			merge_documents=merge_documents,
			merge_material_issues=merge_material_issues,
			mi_docs=mi_docs,
			client_merge=client_merge,
			stamp_mode=stamp_mode,
			blockers=blockers,
			offset_candidates=offset_candidates,
			offset_res=offset_res,
			partial_candidates=partial_candidates,
			partial_res=partial_res,
			replace_candidates=replace_candidates,
			replace_res=replace_res,
			replace_keys=replace_keys,
		)

	downstream = discover_downstream(job_card, merge_documents or mfg_names)
	for b in downstream.get("blocked") or []:
		reason = b.get("reason") if isinstance(b, dict) else str(b)
		if reason and reason not in blockers:
			if str(reason).startswith("MERGE_BLOCKED") or str(reason).startswith("SHARED_BLOCKED"):
				blockers.append(str(reason) if str(reason).startswith("MERGE_BLOCKED") else f"MERGE_BLOCKED: {reason}")
			else:
				blockers.append(f"MERGE_BLOCKED: {reason}")

	# Extra consume from dispositions (unresolved WIP only — not double-counting MI)
	extra = []
	wip = scan.get("wip_warehouse")
	for d in normalized:
		qty = flt(d.get("proposed_consumed"))
		if qty <= 1e-9:
			continue
		# Rate from latest JC issue for item×batch (policy unchanged — not weighted)
		rate_row = frappe.db.sql(
			"""
			select se.name, sed.valuation_rate, sed.basic_rate, sed.s_warehouse, sed.t_warehouse,
			       sed.department, sed.cost_center
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
		if rate <= 1e-9 and rate_row:
			rate = flt(rate_row[0].basic_rate)
		dept_info = resolve_mtfm_department(job_card, d["item_code"], d.get("batch_no") or "")
		extra.append(
			{
				"item_code": d["item_code"],
				"batch_no": d.get("batch_no") or "",
				"qty": qty,
				"s_warehouse": wip,
				"valuation_rate": rate,
				"basic_rate": rate,
				"rate_source": "issue_transfer",
				"source_voucher": rate_row[0].name if rate_row else None,
				"department": dept_info.get("department"),
				"department_evidence": dept_info.get("evidence") or [],
				"cost_center": (rate_row[0].cost_center if rate_row else None),
				"_department_ambiguous": bool(dept_info.get("ambiguous")),
				"_dept_candidates": dept_info.get("candidates") or [],
			}
		)
		if rate <= 1e-9:
			blockers.append(
				"ZERO_RATE: "
				f"{d['item_code']} / {d.get('batch_no') or ''} — "
				"no authoritative MTfM issue_transfer rate"
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

	# MI merge consume (authoritative SLE outgoing rate) — replaces MI physical outflow
	mi_extra = mi_consume_rows_for_merge(mi_docs, merge_material_issues)
	# Avoid double-adding disposition consume that merely restates MI qty
	if mi_extra:
		mi_keys = {(e["item_code"], e.get("batch_no") or ""): flt(e["qty"]) for e in mi_extra}
		filtered_extra = []
		for e in extra:
			key = (e["item_code"], e.get("batch_no") or "")
			if key in mi_keys and abs(flt(e["qty"]) - mi_keys[key]) <= 1e-6:
				# disposition equals MI merge qty — keep MI lineage only
				continue
			filtered_extra.append(e)
		extra = filtered_extra + mi_extra
	else:
		extra = extra + mi_extra

	details = _collect_mfg_rows(merge_documents)
	canonical_rows = _merge_consume_rows(details, extra) if details or extra else list(
		_merge_consume_rows([], extra)
	)

	# REAL Manufacture Batch Replacement (v5.5.22): REMOVE wrong / ADD correct.
	canonical_rows, replace_apply_errors = apply_replacements_to_canonical(
		canonical_rows,
		replace_res.get("approved_groups") or [],
		job_card=job_card,
		wip_warehouse=scan.get("wip_warehouse"),
	)
	for err in replace_apply_errors:
		if err not in blockers:
			blockers.append(err)

	# Deterministic Job Card Item stamp on every CONSUME / source row (mandatory
	# for Custom 6). Historical NULL JCI must not be copied onto the canonical.
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.tracking_reconstruction import (
		build_jci_resolution,
		canonical_consume_totals,
		propose_tracking_reconstruction,
		stamp_canonical_consume_rows,
	)

	jci_resolution = build_jci_resolution(job_card)
	canonical_rows, jci_stamp_errors = stamp_canonical_consume_rows(canonical_rows, jci_resolution)
	for err in jci_stamp_errors:
		blockers.append(err)
	canonical_rows, dept_errors = stamp_canonical_departments(
		job_card, canonical_rows, merge_documents=merge_documents
	)
	for err in dept_errors:
		if err not in blockers:
			blockers.append(err)
	# Pre-submit economic invariant for CONSUME / source rows
	for idx, r in enumerate(canonical_rows or [], start=1):
		is_source = (r.get("type") == "CONSUME") or (
			r.get("s_warehouse") and not r.get("t_warehouse") and not cint(r.get("is_finished_item"))
		)
		if not is_source:
			continue
		if flt(r.get("basic_rate") or r.get("valuation_rate")) <= 1e-9:
			msg = (
				"ZERO_RATE: "
				f"Row {idx} {r.get('item_code')} / {r.get('batch_no') or ''} "
				f"rate_source={r.get('rate_source')} — positive rate required"
			)
			if msg not in blockers:
				blockers.append(msg)
	tracking_repair = propose_tracking_reconstruction(
		job_card,
		dispositions=normalized,
		canonical_consume_by_item=canonical_consume_totals(canonical_rows),
		jci_resolution=jci_resolution,
	)
	for err in tracking_repair.get("blockers") or []:
		if err not in blockers:
			blockers.append(err)

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

	approved_offset_items = sorted(
		{g["item_code"] for g in (offset_res.get("approved_groups") or [])}
		| {g["item_code"] for g in (partial_res.get("approved_groups") or [])}
		| {g["item_code"] for g in (replace_res.get("approved_groups") or [])}
	)
	plan_fp = hashlib.sha256(
		json.dumps(
			{
				"scan_fp": scan["fingerprint"],
				"dispositions": normalized,
				"merge_documents": merge_documents,
				"merge_material_issues": merge_material_issues,
				"stamp": stamp,
				"stamp_mode": stamp_mode or "HISTORICAL",
				"batch_offset_approvals": [
					{
						"item_code": g["item_code"],
						"evidence_fingerprint": g.get("evidence_fingerprint"),
						"decision": g.get("decision"),
					}
					for g in sorted(
						offset_res.get("approved_groups") or [],
						key=lambda x: x["item_code"],
					)
				],
				"partial_batch_offset_approvals": [
					{
						"item_code": g["item_code"],
						"positive_batch": g.get("positive_batch"),
						"negative_batch": g.get("negative_batch"),
						"evidence_fingerprint": g.get("evidence_fingerprint"),
						"decision": g.get("decision"),
						"value_delta": g.get("value_delta"),
					}
					for g in sorted(
						partial_res.get("approved_groups") or [],
						key=lambda x: (
							x["item_code"],
							x.get("positive_batch") or "",
							x.get("negative_batch") or "",
						),
					)
				],
				"manufacture_batch_replace_approvals": [
					{
						"item_code": g["item_code"],
						"wrong_batch": g.get("wrong_batch"),
						"correct_batch": g.get("correct_batch"),
						"qty": g.get("qty"),
						"evidence_fingerprint": g.get("evidence_fingerprint"),
						"decision": g.get("decision"),
						"value_delta": g.get("value_delta"),
					}
					for g in sorted(
						replace_res.get("approved_groups") or [],
						key=lambda x: (
							x["item_code"],
							x.get("wrong_batch") or "",
							x.get("correct_batch") or "",
						),
					)
				],
				"logistics": [x.name for x in downstream["logistics"]],
				"temp_bridge": {
					"required": bool((downstream.get("temporary_bridge") or {}).get("required")),
					"shortages": [
						{
							"item_code": s.get("item_code"),
							"batch_no": s.get("batch_no"),
							"warehouse": s.get("warehouse"),
							"shortage_qty": flt(s.get("shortage_qty")),
						}
						for s in ((downstream.get("temporary_bridge") or {}).get("shortages") or [])
					],
				},
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
				"purpose": "Manufacture",
				"role": "MERGE" if m.name in merge_documents else "KEEP",
				"ownership": "MANUFACTURE",
				"fg_completed_qty": flt(m.fg_completed_qty),
				"stamp": m.stamp,
				"posting_date": str(m.posting_date),
			}
		)
	for mi in mi_docs:
		in_merge = mi["name"] in merge_material_issues
		role = "MERGE" if in_merge else ("BLOCKED" if mi.get("action") == ACTION_BLOCKED else "KEEP")
		qty_desc = ", ".join(
			f"{r['item_code']} × {flt(r['qty'])}" for r in (mi.get("rows") or [])[:4]
		)
		documents.append(
			{
				"name": mi["name"],
				"purpose": "Material Issue",
				"role": role,
				"ownership": mi.get("classification"),
				"action": mi.get("action"),
				"reason": mi.get("reason"),
				"qty_summary": qty_desc,
				"propose_merge": bool(mi.get("propose_merge")),
				"shared_document": bool(mi.get("shared_document")),
				"rows": mi.get("rows") or [],
				"posting_date": str(
					frappe.db.get_value("Stock Entry", mi["name"], "posting_date") or ""
				),
			}
		)
	temporary_bridge = downstream.get("temporary_bridge") or {"required": False, "shortages": []}
	# Cancel-set logistics + full audit (including SHARED_BLOCKED seeds)
	for lg in downstream.get("logistics_audit") or downstream["logistics"]:
		shared_cls = getattr(lg, "shared_class", None) or (
			lg.get("shared_class") if isinstance(lg, dict) else None
		)
		in_cancel = getattr(lg, "in_cancel_set", None)
		if in_cancel is None and isinstance(lg, dict):
			in_cancel = lg.get("in_cancel_set")
		bridge_unlock = bool(
			getattr(lg, "bridge_unlock", None)
			if hasattr(lg, "bridge_unlock")
			else (lg.get("bridge_unlock") if isinstance(lg, dict) else False)
		)
		if shared_cls == "SHARED_RECREATE_SAFE" or (bridge_unlock and in_cancel):
			role = "TEMP CANCEL / RECREATE"
			ownership_label = (
				"SHARED — BRIDGE UNLOCK" if bridge_unlock else "SHARED — SAFE TO RECREATE"
			)
		elif shared_cls == "DEDICATED":
			role = "TEMP CANCEL / RECREATE" if in_cancel else "AUDIT"
			ownership_label = "DEDICATED"
		elif shared_cls == "SHARED_BLOCKED":
			role = "BLOCKED" if not bridge_unlock else "TEMP CANCEL / RECREATE"
			ownership_label = (
				"SHARED — BRIDGE UNLOCK" if bridge_unlock else "SHARED — BLOCKED"
			)
		else:
			role = "AUDIT"
			ownership_label = shared_cls or "AUDIT"
		documents.append(
			{
				"name": lg.name if hasattr(lg, "name") else lg.get("name"),
				"role": role,
				"purpose": lg.purpose if hasattr(lg, "purpose") else lg.get("purpose"),
				"ownership": ownership_label,
				"shared_class": shared_cls,
				"bridge_unlock": bridge_unlock,
				"shared_reason": (
					lg.shared_reason
					if hasattr(lg, "shared_reason")
					else lg.get("shared_reason")
					if isinstance(lg, dict)
					else None
				),
				"posting_date": str(
					lg.posting_date if hasattr(lg, "posting_date") else lg.get("posting_date")
				),
				"related_rows": (
					lg.related_rows if hasattr(lg, "related_rows") else lg.get("related_rows")
				),
				"unrelated_rows": (
					lg.unrelated_rows
					if hasattr(lg, "unrelated_rows")
					else lg.get("unrelated_rows")
				),
			}
		)

	has_replace_approval = bool(replace_res.get("approved_groups"))
	stock_repair_needed = (
		any(flt(d.get("proposed_consumed")) > 0 for d in normalized)
		or any(flt(d.get("proposed_scrap")) > 0 for d in normalized)
		or any(flt(d.get("proposed_return")) > 0 for d in normalized)
		or len(merge_documents) > 1
		or bool(merge_material_issues)
		or has_replace_approval
	)
	has_offset_approval = bool(offset_res.get("approved_groups")) or bool(
		partial_res.get("approved_groups")
	)
	# Replacement is a REAL stock repair — never exception-only.
	exception_only = has_offset_approval and not stock_repair_needed and not has_replace_approval
	needs_repair = stock_repair_needed or has_offset_approval
	apply_allowed = not blockers and bool(merge_documents) and needs_repair
	# Exception-only path records approved offset without stock mutation.
	if exception_only and not blockers and bool(merge_documents):
		apply_allowed = True
	if not needs_repair and not blockers:
		blockers.append("Nothing to repair")
		apply_allowed = False
	if blockers:
		apply_allowed = False

	# Annotate scan rows for UI / audit when offset / replace approved
	offset_only_keys = set(offset_res.get("exempt_keys") or set()) | set(
		partial_res.get("exempt_keys") or set()
	)
	full_keys = set(offset_res.get("exempt_keys") or set())
	for r in scan.get("rows") or []:
		key = (r["item_code"], r.get("batch_no") or "")
		if key in replace_keys:
			r["repair_status"] = STATUS_APPROVED_REPLACE
			r["manufacture_batch_replace_status"] = STATUS_APPROVED_REPLACE
		elif key in partial_keys:
			r["repair_status"] = STATUS_APPROVED_PARTIAL
			r["batch_offset_status"] = STATUS_APPROVED_PARTIAL
		elif key in full_keys:
			r["repair_status"] = STATUS_APPROVED
			r["batch_offset_status"] = STATUS_APPROVED

	batch_offset_preview = []
	for g in offset_res.get("approved_groups") or []:
		batch_offset_preview.append(
			{
				"exception": "BATCH_OFFSET_EXCEPTION",
				"item_code": g["item_code"],
				"batches": [
					{"batch_no": b.get("batch_no"), "remaining_wip": b.get("remaining_wip")}
					for b in (g.get("batches") or [])
				],
				"net": 0.0,
				"action": "NO_STOCK_DOCUMENT_CHANGE",
				"status": STATUS_APPROVED,
			}
		)
	partial_batch_offset_preview = []
	for g in partial_res.get("approved_groups") or []:
		partial_batch_offset_preview.append(
			{
				"exception": "PARTIAL_BATCH_OFFSET_EXCEPTION",
				"item_code": g["item_code"],
				"positive_batch": g.get("positive_batch"),
				"negative_batch": g.get("negative_batch"),
				"pair_qty": g.get("pair_qty"),
				"positive_rate": g.get("positive_rate"),
				"negative_rate": g.get("negative_rate"),
				"value_delta": g.get("value_delta"),
				"classification": g.get("classification"),
				"repack_document": g.get("repack_document"),
				"unresolved_after_pair": g.get("unresolved_after_pair") or [],
				"action": "NO_STOCK_DOCUMENT_CHANGE",
				"status": STATUS_APPROVED_PARTIAL,
			}
		)
	manufacture_batch_replace_preview = []
	for g in replace_res.get("approved_groups") or []:
		manufacture_batch_replace_preview.append(
			{
				"correction": "MANUFACTURE_BATCH_CORRECTION",
				"item_code": g["item_code"],
				"wrong_batch": g.get("wrong_batch"),
				"correct_batch": g.get("correct_batch"),
				"qty": g.get("qty"),
				"wrong_rate": g.get("wrong_rate"),
				"correct_rate": g.get("correct_rate"),
				"wrong_value": g.get("wrong_value"),
				"correct_value": g.get("correct_value"),
				"value_delta": g.get("value_delta"),
				"net_qty_change": 0.0,
				"classification": g.get("classification"),
				"action": "CANONICAL_MANUFACTURE_BATCH_REPLACE",
				"status": STATUS_APPROVED_REPLACE,
				"remove": {
					"batch_no": g.get("wrong_batch"),
					"qty": g.get("qty"),
					"rate": g.get("wrong_rate"),
					"value": g.get("wrong_value"),
				},
				"add": {
					"batch_no": g.get("correct_batch"),
					"qty": g.get("qty"),
					"rate": g.get("correct_rate"),
					"value": g.get("correct_value"),
				},
			}
		)

	plan = {
		"job_card": job_card,
		"work_order": scan["work_order"],
		"scan": scan,
		"dispositions": normalized,
		"merge_documents": merge_documents,
		"merge_material_issues": merge_material_issues,
		"material_issues": mi_docs,
		"documents": documents,
		"downstream_logistics": downstream["logistics"],
		"downstream_logistics_audit": downstream.get("logistics_audit") or downstream["logistics"],
		"downstream_blocked": downstream["blocked"],
		"minimal_cancel_set": downstream.get("minimal_cancel_set")
		or [x.name for x in downstream["logistics"]],
		"temporary_bridge": temporary_bridge,
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
			"supersedes": list(merge_documents) + list(merge_material_issues),
		},
		"tracking_repair": tracking_repair,
		"jci_resolution": jci_resolution,
		"returns_needed": returns_needed,
		"blockers": blockers,
		"apply_allowed": apply_allowed,
		"fingerprint": plan_fp,
		"scan_fingerprint": scan["fingerprint"],
		"client_merge_provided": client_merge is not None,
		"batch_offset_candidates": offset_candidates,
		"batch_offset_approvals": offset_res.get("audit") or [],
		"approved_batch_offsets": offset_res.get("approved_groups") or [],
		"approved_batch_offset_keys": [
			{"item_code": k[0], "batch_no": k[1]} for k in sorted(offset_only_keys)
		],
		"batch_offset_preview": batch_offset_preview,
		"partial_batch_offset_candidates": partial_candidates,
		"partial_batch_offset_approvals": partial_res.get("audit") or [],
		"approved_partial_batch_offsets": partial_res.get("approved_groups") or [],
		"partial_batch_offset_preview": partial_batch_offset_preview,
		"manufacture_batch_replace_candidates": replace_candidates,
		"manufacture_batch_replace_approvals": replace_res.get("audit") or [],
		"approved_manufacture_batch_replacements": replace_res.get("approved_groups") or [],
		"approved_manufacture_batch_replace_keys": [
			{"item_code": k[0], "batch_no": k[1]} for k in sorted(replace_keys)
		],
		"manufacture_batch_replace_preview": manufacture_batch_replace_preview,
		"stock_repair_needed": stock_repair_needed,
		"exception_only": exception_only,
		"approved_offset_items": approved_offset_items,
		"repair_required": bool(needs_repair),
	}
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.economic_preflight import (
		attach_economic_preflight,
	)

	return attach_economic_preflight(plan)
