# Copyright (c) 2026, ERPNext Extensions contributors
"""Deterministic Material Issue ownership classifier for Manufacture merge (v5.5.0).

MI is never auto-merged from weak similarity. Only proven Job Card manufacturing
WIP consumption may become MERGE INTO CANONICAL MANUFACTURE.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.evidence import (
	collect_detail_rows,
)

MI_MERGE_SAFE = "MI_MERGE_SAFE"
MI_USER_DECISION = "MI_USER_DECISION"
MI_FINANCE_REVIEW = "MI_FINANCE_REVIEW"
MI_BLOCKED = "MI_BLOCKED"

# UI / plan actions (collapsed classifier)
ACTION_MERGE = "MERGE"
ACTION_KEEP = "KEEP"
ACTION_BLOCKED = "BLOCKED"


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


def _wip_warehouse(job_card: str) -> str:
	return (frappe.db.get_value("Job Card", job_card, "wip_warehouse") or "").strip()


def _sle_outgoing_rate(voucher: str, item_code: str, warehouse: str | None) -> float:
	row = frappe.db.sql(
		"""
		select outgoing_rate, valuation_rate
		from `tabStock Ledger Entry`
		where voucher_no=%s and item_code=%s and is_cancelled=0 and actual_qty < 0
		  and (%s is null or warehouse=%s)
		order by abs(actual_qty) desc
		limit 1
		""",
		(voucher, item_code, warehouse, warehouse),
		as_dict=1,
	)
	if not row:
		return 0.0
	return flt(row[0].outgoing_rate or row[0].valuation_rate)


def classify_material_issue(
	se_name: str,
	job_card: str,
	evidence_items: list[dict] | None = None,
) -> dict[str, Any]:
	"""Classify one submitted Material Issue for merge eligibility."""
	se = frappe.db.get_value(
		"Stock Entry",
		se_name,
		[
			"name",
			"purpose",
			"docstatus",
			"job_card",
			"work_order",
			"posting_date",
			"posting_time",
			"custom_rahkaran_no",
		],
		as_dict=1,
	)
	if not se or se.purpose != "Material Issue" or cint(se.docstatus) != 1:
		return {
			"name": se_name,
			"classification": MI_BLOCKED,
			"action": ACTION_BLOCKED,
			"reason": "Not a submitted Material Issue",
			"rows": [],
			"owned_rows": [],
			"unrelated_rows": [],
		}

	jc_wip = _wip_warehouse(job_card)
	jc_wo = frappe.db.get_value("Job Card", job_card, "work_order")
	details = collect_detail_rows(se_name)
	rows_out = []
	owned = []
	unrelated = []
	evidence_items = evidence_items or []
	ev_map = {(i["item_code"], i.get("batch_no") or ""): i for i in evidence_items}

	header_jc_match = (se.job_card or "") == job_card
	header_wo_match = bool(jc_wo) and (se.work_order or "") == jc_wo

	for d in details:
		batch = _detail_batch(d)
		item = d.get("item_code")
		qty = flt(d.get("transfer_qty") or d.get("qty"))
		s_wh = (d.get("s_warehouse") or "").strip()
		key = (item, batch)
		ev = ev_map.get(key) or {}
		issued = flt(ev.get("issued"))
		returned = flt(ev.get("returned"))
		mfg_consumed = flt(ev.get("consumed"))
		mi_already = flt(ev.get("mi_consumed") or 0)
		available = issued - returned - mfg_consumed
		from_wip = bool(jc_wip) and s_wh == jc_wip
		row_jc_item = d.get("job_card_item")
		row_owned = False
		reasons = []

		if not header_jc_match and not row_jc_item:
			reasons.append("no Job Card link")
		if not from_wip:
			reasons.append("source warehouse is not Job Card WIP")
		if issued <= 1e-9:
			reasons.append("item×batch not issued to this Job Card")
		elif qty > available + 1e-6 and qty > mi_already + 1e-6:
			# Allow when this MI qty is already the proven OTHER/MI outflow balancing the rule
			reasons.append("qty exceeds unresolved WIP capacity")

		# Strong ownership: header JC match + WIP + issued evidence + qty fits
		qty_fits = issued > 1e-9 and qty <= available + 1e-6
		qty_is_proven_mi = mi_already > 1e-9 and abs(qty - mi_already) <= 1e-6
		if header_jc_match and from_wip and issued > 1e-9 and (qty_fits or qty_is_proven_mi):
			row_owned = True
			reasons = ["proven JC×Item×Batch WIP consumption"]
		elif header_jc_match and from_wip and issued > 1e-9:
			# WIP + JC link but qty awkward → user decision
			row_owned = False
			reasons = reasons or ["qty relationship needs user review"]

		rec = {
			"item_code": item,
			"batch_no": batch,
			"qty": qty,
			"s_warehouse": s_wh,
			"valuation_rate": flt(d.get("valuation_rate")),
			"sle_rate": _sle_outgoing_rate(se_name, item, s_wh),
			"owned": row_owned,
			"from_wip": from_wip,
			"reasons": reasons,
			"job_card_item": row_jc_item,
		}
		rows_out.append(rec)
		if row_owned:
			owned.append(rec)
		else:
			unrelated.append(rec)

	# Shared document: mix of owned + unrelated → BLOCK (no row-split MVP)
	if owned and unrelated:
		return {
			"name": se_name,
			"purpose": "Material Issue",
			"classification": MI_BLOCKED,
			"action": ACTION_BLOCKED,
			"reason": "SHARED_DOCUMENT: Material Issue has both merge-owned and unrelated rows",
			"shared_document": True,
			"job_card_link": se.job_card,
			"work_order_link": se.work_order,
			"header_jc_match": header_jc_match,
			"header_wo_match": header_wo_match,
			"rows": rows_out,
			"owned_rows": owned,
			"unrelated_rows": unrelated,
			"rahkaran": se.custom_rahkaran_no,
		}

	if unrelated and not owned:
		# Outside WIP or weak evidence
		outside_wip = all(not r["from_wip"] for r in unrelated)
		cls = MI_BLOCKED if outside_wip and not header_jc_match else MI_USER_DECISION
		if outside_wip and header_jc_match:
			cls = MI_USER_DECISION
		if any("qty exceeds" in " ".join(r.get("reasons") or []) for r in unrelated):
			cls = MI_FINANCE_REVIEW if header_jc_match else MI_BLOCKED
		return {
			"name": se_name,
			"purpose": "Material Issue",
			"classification": cls,
			"action": ACTION_BLOCKED if cls == MI_BLOCKED else ACTION_KEEP,
			"reason": "; ".join(
				sorted({x for r in unrelated for x in (r.get("reasons") or [])})
			)
			or "ownership not proven",
			"shared_document": False,
			"job_card_link": se.job_card,
			"work_order_link": se.work_order,
			"header_jc_match": header_jc_match,
			"header_wo_match": header_wo_match,
			"rows": rows_out,
			"owned_rows": owned,
			"unrelated_rows": unrelated,
			"rahkaran": se.custom_rahkaran_no,
			"propose_merge": False,
		}

	# All rows owned
	strong = header_jc_match and all(r["from_wip"] for r in owned) and len(owned) == len(rows_out)
	# Exact Golden Rule solve: MI qty == remaining without counting MI, or MI already balances
	solves = False
	for r in owned:
		ev = ev_map.get((r["item_code"], r["batch_no"] or "")) or {}
		rem_ex_mi = (
			flt(ev.get("issued"))
			- flt(ev.get("returned"))
			- flt(ev.get("consumed"))
			- flt(ev.get("other") or 0)
		)
		# If MI is already in mi_consumed/other, remainder excluding MI should equal MI qty
		if abs(rem_ex_mi + flt(ev.get("mi_consumed") or 0) - flt(r["qty"])) <= 1e-6:
			solves = True
		if abs(rem_ex_mi - flt(r["qty"])) <= 1e-6:
			solves = True

	if strong and (solves or header_wo_match):
		cls = MI_MERGE_SAFE
		action = ACTION_MERGE
		reason = "Proven Material Issue WIP consumption for Job Card×Item×Batch"
		propose = True
	elif strong:
		cls = MI_USER_DECISION
		action = ACTION_KEEP
		reason = "JC-linked WIP Material Issue — confirm merge into Manufacture"
		propose = True  # suggest but require approval (checkbox)
	else:
		cls = MI_USER_DECISION
		action = ACTION_KEEP
		reason = "Partial ownership evidence — user decision required"
		propose = False

	return {
		"name": se_name,
		"purpose": "Material Issue",
		"classification": cls,
		"action": action,
		"reason": reason,
		"shared_document": False,
		"job_card_link": se.job_card,
		"work_order_link": se.work_order,
		"header_jc_match": header_jc_match,
		"header_wo_match": header_wo_match,
		"rows": rows_out,
		"owned_rows": owned,
		"unrelated_rows": unrelated,
		"rahkaran": se.custom_rahkaran_no,
		"propose_merge": propose,
		"solves_golden_rule": solves,
	}


def discover_material_issues(job_card: str, evidence_items: list[dict] | None = None) -> list[dict]:
	"""All submitted Material Issues linked to Job Card (header)."""
	names = frappe.db.sql(
		"""
		select name from `tabStock Entry`
		where purpose='Material Issue' and docstatus=1 and job_card=%s
		order by posting_date, posting_time, name
		""",
		job_card,
		pluck="name",
	)
	return [classify_material_issue(n, job_card, evidence_items) for n in names]


def mi_consume_rows_for_merge(classified: list[dict], selected: list[str] | None) -> list[dict]:
	"""Build extra CONSUME rows from approved MI merge set."""
	selected_set = set(selected or [])
	extra = []
	for mi in classified:
		if mi["name"] not in selected_set:
			continue
		if mi.get("shared_document") or mi.get("classification") == MI_BLOCKED:
			continue
		if not mi.get("owned_rows"):
			continue
		for r in mi.get("owned_rows") or []:
			rate = flt(r.get("sle_rate") or r.get("valuation_rate"))
			extra.append(
				{
					"item_code": r["item_code"],
					"batch_no": r.get("batch_no") or "",
					"qty": flt(r["qty"]),
					"s_warehouse": r.get("s_warehouse"),
					"valuation_rate": rate,
					"basic_rate": rate,
					"rate_source": "mi_sle_outgoing",
					"source_voucher": mi["name"],
					"source_lineage": f"Material Issue {mi['name']}",
				}
			)
	return extra
