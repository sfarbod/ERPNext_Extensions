# Copyright (c) 2026, ERPNext Extensions contributors
"""User-approved canonical Manufacture consumption corrections (v5.5.22).

Supports REMOVE / REDUCE / ADD / INCREASE of Manufacture source consumption
via target quantities on the repaired canonical document. Paired wrong→correct
Batch replacement (REPLACE_MANUFACTURE_BATCH) remains the primary detector for
over-consumed + missing-consumption pairs.

Distinct from ACCEPT_*_BATCH_OFFSET_NO_REPAIR (attribution exception / no stock
change). Never auto-applied; explicit approval + fingerprint required.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import cint, flt


DECISION_REPLACE = "REPLACE_MANUFACTURE_BATCH"
STATUS_APPROVED_REPLACE = "APPROVED_MANUFACTURE_BATCH_REPLACE"
CLASS_WRONG_MFG_BATCH = "WRONG_MANUFACTURE_BATCH_ATTRIBUTION"
CLASS_AMBIGUOUS = "AMBIGUOUS_REPLACEMENT"
CLASS_UNSAFE = "UNSAFE"
CLASS_QTY_MISMATCH = "BATCH_REPLACEMENT_QTY_MISMATCH"

_QTY_TOLERANCE = 1e-6
_VALUE_TOLERANCE = 1.0


def _row_key(item_code: str, batch_no: str | None) -> tuple[str, str]:
	return (item_code, batch_no or "")


def _evidence_fingerprint(
	job_card: str,
	item_code: str,
	wrong_batch: str,
	correct_batch: str,
	qty: float,
	wrong_rate: float,
	correct_rate: float,
	wrong_remaining: float,
	correct_remaining: float,
) -> str:
	payload = {
		"job_card": job_card,
		"item_code": item_code,
		"wrong_batch": wrong_batch,
		"correct_batch": correct_batch,
		"qty": flt(qty),
		"wrong_rate": flt(wrong_rate),
		"correct_rate": flt(correct_rate),
		"wrong_remaining": flt(wrong_remaining),
		"correct_remaining": flt(correct_remaining),
		"decision": DECISION_REPLACE,
	}
	return hashlib.sha256(
		json.dumps(payload, sort_keys=True, default=str).encode()
	).hexdigest()


def _batch_mfg_rate(job_card: str, item_code: str, batch_no: str) -> float:
	"""Rate from historical Manufacture SED for this JC×Item×Batch."""
	rows = frappe.db.sql(
		"""
		select sed.basic_rate, sed.valuation_rate
		from `tabStock Entry Detail` sed
		join `tabStock Entry` se on se.name=sed.parent
		where se.job_card=%s and se.docstatus=1 and se.purpose='Manufacture'
		  and sed.item_code=%s and ifnull(sed.batch_no,'')=%s
		  and ifnull(sed.s_warehouse,'')!='' and ifnull(sed.t_warehouse,'')=''
		order by se.posting_date desc, se.posting_time desc
		limit 1
		""",
		(job_card, item_code, batch_no or ""),
		as_dict=1,
	)
	if not rows:
		return 0.0
	return flt(rows[0].valuation_rate) or flt(rows[0].basic_rate)


def _batch_issued_rate(job_card: str, item_code: str, batch_no: str) -> float:
	"""Authoritative issued-rate from JC MTfM (outgoing issue, non-return)."""
	rows = frappe.db.sql(
		"""
		select sed.valuation_rate, sed.basic_rate
		from `tabStock Entry Detail` sed
		join `tabStock Entry` se on se.name=sed.parent
		where se.job_card=%s and se.purpose='Material Transfer for Manufacture'
		  and se.docstatus=1 and ifnull(se.is_return,0)=0
		  and sed.item_code=%s and ifnull(sed.batch_no,'')=%s
		  and ifnull(sed.t_warehouse,'')!='' and ifnull(sed.s_warehouse,'')!=''
		order by se.posting_date desc, se.posting_time desc
		limit 1
		""",
		(job_card, item_code, batch_no or ""),
		as_dict=1,
	)
	if not rows:
		return 0.0
	return flt(rows[0].valuation_rate) or flt(rows[0].basic_rate)


def _historical_mfg_consume_qty(job_card: str, item_code: str, batch_no: str) -> float:
	row = frappe.db.sql(
		"""
		select sum(sed.qty) as qty
		from `tabStock Entry Detail` sed
		join `tabStock Entry` se on se.name=sed.parent
		where se.job_card=%s and se.docstatus=1 and se.purpose='Manufacture'
		  and sed.item_code=%s and ifnull(sed.batch_no,'')=%s
		  and ifnull(sed.s_warehouse,'')!='' and ifnull(sed.t_warehouse,'')=''
		""",
		(job_card, item_code, batch_no or ""),
		as_dict=1,
	)
	return flt(row[0].qty) if row else 0.0


def _wip_warehouse_for_mfg(job_card: str, item_code: str, batch_no: str) -> str:
	wh = frappe.db.sql(
		"""
		select sed.s_warehouse
		from `tabStock Entry Detail` sed
		join `tabStock Entry` se on se.name=sed.parent
		where se.job_card=%s and se.docstatus=1 and se.purpose='Manufacture'
		  and sed.item_code=%s and ifnull(sed.batch_no,'')=%s
		  and ifnull(sed.s_warehouse,'')!='' and ifnull(sed.t_warehouse,'')=''
		order by se.posting_date desc, se.posting_time desc
		limit 1
		""",
		(job_card, item_code, batch_no or ""),
	)
	return wh[0][0] if wh else ""


def _item_has_mi_conflict(job_card: str, item_code: str, scan_rows: list[dict]) -> bool:
	for r in scan_rows or []:
		if r.get("item_code") != item_code:
			continue
		if flt(r.get("mi_consumed")) > 1e-9:
			return True
	try:
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.mi_ownership import (
			MI_BLOCKED,
			MI_FINANCE_REVIEW,
			MI_USER_DECISION,
			discover_material_issues,
		)

		for mi in discover_material_issues(job_card, []) or []:
			cls = mi.get("classification")
			if cls not in (MI_USER_DECISION, MI_FINANCE_REVIEW, MI_BLOCKED):
				continue
			for row in mi.get("rows") or []:
				if row.get("item_code") == item_code:
					return True
	except Exception:
		pass
	return False


def _item_has_scrap_mismatch(scan_rows: list[dict], item_code: str) -> bool:
	for r in scan_rows or []:
		if r.get("item_code") != item_code:
			continue
		if (r.get("status") or "") == "SCRAP MISMATCH":
			return True
	return False


def _item_is_serialized(item_code: str) -> bool:
	return cint(frappe.db.get_value("Item", item_code, "has_serial_no")) == 1


def _safety_reasons(
	job_card: str,
	item_code: str,
	wrong_row: dict,
	correct_row: dict,
	scan_rows: list[dict],
	qty: float,
) -> list[str]:
	reasons: list[str] = []
	if _item_has_mi_conflict(job_card, item_code, scan_rows):
		reasons.append("MI_BLOCKER")
	if _item_has_scrap_mismatch(scan_rows, item_code):
		reasons.append("SCRAP_PAIR_MISMATCH")
	if _item_is_serialized(item_code):
		reasons.append("SERIAL_IDENTITY_CONFLICT")
	# Wrong batch must show no JC issue ownership (issued≈0).
	if flt(wrong_row.get("issued")) > _QTY_TOLERANCE:
		reasons.append("WRONG_BATCH_HAS_JC_ISSUE")
	# Correct batch must have enough JC-owned net issue.
	owned = flt(correct_row.get("issued")) - flt(correct_row.get("returned"))
	# Subtract other legitimate outflows already recorded on the correct batch.
	already = (
		flt(correct_row.get("consumed"))
		+ flt(correct_row.get("mi_consumed"))
		+ flt(correct_row.get("scrap"))
	)
	available = owned - already
	# Remaining positive is the unexplained WIP; available must cover replacement.
	if available + _QTY_TOLERANCE < qty:
		reasons.append("INSUFFICIENT_CORRECT_BATCH_OWNERSHIP")
	if abs(flt(wrong_row.get("remaining_wip")) + qty) > _QTY_TOLERANCE:
		reasons.append(CLASS_QTY_MISMATCH)
	if abs(flt(correct_row.get("remaining_wip")) - qty) > _QTY_TOLERANCE:
		# MVP: require exact remaining match on both legs (no partial replace).
		reasons.append(CLASS_QTY_MISMATCH)
	mfg_qty = _historical_mfg_consume_qty(job_card, item_code, wrong_row.get("batch_no") or "")
	if mfg_qty + _QTY_TOLERANCE < qty:
		reasons.append("WRONG_BATCH_MFG_QTY_SHORT")
	if _historical_mfg_consume_qty(job_card, item_code, correct_row.get("batch_no") or "") > _QTY_TOLERANCE:
		# Correct batch already consumed on MFG — replacement would double unless
		# we only fill missing; MVP requires zero historical correct-batch MFG.
		reasons.append("CORRECT_BATCH_ALREADY_ON_MFG")
	return reasons


def detect_manufacture_batch_replace_candidates(
	job_card: str,
	scan_rows: list[dict] | None = None,
) -> list[dict]:
	"""Detect WRONG_MANUFACTURE_BATCH_ATTRIBUTION replacement pairs (never auto-approved)."""
	if scan_rows is None:
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
			scan_golden_rule,
		)

		scan_rows = scan_golden_rule(job_card).get("rows") or []

	by_item: dict[str, list[dict]] = {}
	for r in scan_rows or []:
		by_item.setdefault(r["item_code"], []).append(r)

	out: list[dict] = []
	for item_code, rows in sorted(by_item.items()):
		neg = [
			(r, flt(r.get("remaining_wip")))
			for r in rows
			if flt(r.get("remaining_wip")) < -_QTY_TOLERANCE
		]
		pos = [
			(r, flt(r.get("remaining_wip")))
			for r in rows
			if flt(r.get("remaining_wip")) > _QTY_TOLERANCE
		]
		if not neg or not pos:
			continue

		for wrong_row, wrong_rem in sorted(neg, key=lambda x: x[0].get("batch_no") or ""):
			wrong_batch = wrong_row.get("batch_no") or ""
			qty = abs(wrong_rem)
			# Strong wrong-batch signal: MFG consume without JC issue.
			if flt(wrong_row.get("issued")) > _QTY_TOLERANCE:
				continue
			if flt(wrong_row.get("consumed")) + _QTY_TOLERANCE < qty:
				continue

			matches = [
				(pr, prem)
				for pr, prem in pos
				if abs(prem - qty) <= _QTY_TOLERANCE
			]
			if not matches:
				continue
			if len(matches) > 1:
				out.append(
					{
						"job_card": job_card,
						"item_code": item_code,
						"eligible": False,
						"reason": CLASS_AMBIGUOUS,
						"classification": CLASS_AMBIGUOUS,
						"wrong_batch": wrong_batch,
						"wrong_remaining": wrong_rem,
						"qty": qty,
						"correct_candidates": [
							{"batch_no": pr.get("batch_no") or "", "remaining_wip": prem}
							for pr, prem in matches
						],
						"decision": DECISION_REPLACE,
						"action_if_approved": "CANONICAL_MANUFACTURE_BATCH_REPLACE",
						"explanation": (
							"Multiple correct Batches match this wrong Manufacture "
							"quantity. Select the replacement explicitly."
						),
					}
				)
				continue

			correct_row, correct_rem = matches[0]
			correct_batch = correct_row.get("batch_no") or ""
			if correct_batch == wrong_batch:
				continue

			wrong_rate = _batch_mfg_rate(job_card, item_code, wrong_batch)
			correct_rate = _batch_issued_rate(job_card, item_code, correct_batch)
			wrong_value = qty * wrong_rate
			correct_value = qty * correct_rate
			value_delta = correct_value - wrong_value
			reasons = _safety_reasons(
				job_card, item_code, wrong_row, correct_row, scan_rows, qty
			)
			if correct_rate <= 1e-9:
				reasons.append("MISSING_CORRECT_ISSUED_RATE")
			if wrong_rate <= 1e-9:
				reasons.append("MISSING_WRONG_MFG_RATE")

			eligible = not reasons
			classification = CLASS_WRONG_MFG_BATCH if eligible else CLASS_UNSAFE
			wh = _wip_warehouse_for_mfg(job_card, item_code, wrong_batch)
			fp = _evidence_fingerprint(
				job_card,
				item_code,
				wrong_batch,
				correct_batch,
				qty,
				wrong_rate,
				correct_rate,
				wrong_rem,
				correct_rem,
			)
			meta = frappe.db.get_value("Item", item_code, "stock_uom") or ""
			out.append(
				{
					"job_card": job_card,
					"item_code": item_code,
					"stock_uom": meta,
					"eligible": eligible,
					"reason": None if eligible else "; ".join(reasons) or classification,
					"classification": classification,
					"wrong_batch": wrong_batch,
					"correct_batch": correct_batch,
					"qty": qty,
					"wrong_remaining": wrong_rem,
					"correct_remaining": correct_rem,
					"wrong_rate": wrong_rate,
					"correct_rate": correct_rate,
					"wrong_value": wrong_value,
					"correct_value": correct_value,
					"value_delta": value_delta,
					"s_warehouse": wh,
					"wrong_issued": flt(wrong_row.get("issued")),
					"wrong_returned": flt(wrong_row.get("returned")),
					"wrong_consumed": flt(wrong_row.get("consumed")),
					"correct_issued": flt(correct_row.get("issued")),
					"correct_returned": flt(correct_row.get("returned")),
					"correct_consumed": flt(correct_row.get("consumed")),
					"batches": [
						{
							"role": "REMOVE",
							"batch_no": wrong_batch,
							"qty": qty,
							"rate": wrong_rate,
							"value": wrong_value,
							"remaining_wip": wrong_rem,
							"status": wrong_row.get("status"),
						},
						{
							"role": "ADD",
							"batch_no": correct_batch,
							"qty": qty,
							"rate": correct_rate,
							"value": correct_value,
							"remaining_wip": correct_rem,
							"status": correct_row.get("status"),
						},
					],
					"evidence_fingerprint": fp,
					"decision": DECISION_REPLACE,
					"action_if_approved": "CANONICAL_MANUFACTURE_BATCH_REPLACE",
					"explanation": (
						"Manufacture consumed the wrong Batch. Approving removes the "
						"wrong Batch source from the repaired canonical Manufacture and "
						"adds the same quantity on the correct Batch. Net Item "
						"consumption is unchanged; economics follow the correct Batch "
						"issued-rate policy."
					),
				}
			)
	return out


def resolve_manufacture_batch_replace_approvals(
	job_card: str,
	scan_rows: list[dict],
	approvals: list[dict] | None,
	candidates: list[dict] | None = None,
	*,
	offset_approved_keys: set[tuple[str, str]] | None = None,
) -> dict[str, Any]:
	"""Validate explicit REPLACE_MANUFACTURE_BATCH approvals."""
	candidates = (
		candidates
		if candidates is not None
		else detect_manufacture_batch_replace_candidates(job_card, scan_rows)
	)
	by_key = {
		(
			c["item_code"],
			c.get("wrong_batch") or "",
			c.get("correct_batch") or "",
		): c
		for c in candidates
		if c.get("wrong_batch") and c.get("correct_batch")
	}
	approved_groups: list[dict] = []
	blockers: list[str] = []
	exempt_keys: set[tuple[str, str]] = set()
	audit: list[dict] = []
	offset_keys = offset_approved_keys or set()

	for raw in approvals or []:
		if not raw:
			continue
		item_code = raw.get("item_code")
		wrong_b = raw.get("wrong_batch") or raw.get("from_batch") or ""
		correct_b = raw.get("correct_batch") or raw.get("to_batch") or ""
		# Explicit accepted/approved=0 must win over decision string alone.
		if "accepted" in raw or "approved" in raw:
			accepted = cint(raw.get("accepted") if "accepted" in raw else raw.get("approved"))
		else:
			accepted = cint(str(raw.get("decision") or "") == DECISION_REPLACE)
		if not accepted:
			continue
		if not item_code or not wrong_b or not correct_b:
			blockers.append(
				"MANUFACTURE_BATCH_REPLACE: approval requires item_code, wrong_batch, correct_batch"
			)
			continue
		# Mutual exclusion vs Batch Offset / Partial Offset on overlapping keys.
		for k in (_row_key(item_code, wrong_b), _row_key(item_code, correct_b)):
			if k in offset_keys:
				blockers.append(
					f"MANUFACTURE_BATCH_REPLACE: {item_code} conflicts with approved "
					f"Batch Offset on {k[1]} — choose offset OR replacement, not both"
				)
		cand = by_key.get((item_code, wrong_b, correct_b))
		if not cand:
			blockers.append(
				f"MANUFACTURE_BATCH_REPLACE: {item_code} {wrong_b}→{correct_b} "
				"is not a current replacement candidate"
			)
			continue
		client_fp = (raw.get("evidence_fingerprint") or raw.get("fingerprint") or "").strip()
		if not client_fp:
			blockers.append(
				f"MANUFACTURE_BATCH_REPLACE: {item_code} approval missing evidence_fingerprint"
			)
			continue
		if client_fp != cand.get("evidence_fingerprint"):
			blockers.append(
				f"STALE_PLAN: MANUFACTURE_BATCH_REPLACE approval for {item_code} "
				f"{wrong_b}→{correct_b} is stale"
			)
			continue
		if not cand.get("eligible"):
			blockers.append(
				f"MANUFACTURE_BATCH_REPLACE: {item_code} {wrong_b}→{correct_b} not eligible "
				f"({cand.get('reason') or cand.get('classification')})"
			)
			continue
		client_qty = raw.get("qty")
		if client_qty is not None and abs(flt(client_qty) - flt(cand.get("qty"))) > _QTY_TOLERANCE:
			blockers.append(
				f"BATCH_REPLACEMENT_QTY_MISMATCH: {item_code} "
				f"approval qty {client_qty} != candidate {cand.get('qty')}"
			)
			continue
		approved_groups.append(cand)
		exempt_keys.add(_row_key(item_code, wrong_b))
		exempt_keys.add(_row_key(item_code, correct_b))
		audit.append(
			{
				"job_card": job_card,
				"item_code": item_code,
				"wrong_batch": wrong_b,
				"correct_batch": correct_b,
				"qty": cand.get("qty"),
				"wrong_rate": cand.get("wrong_rate"),
				"correct_rate": cand.get("correct_rate"),
				"wrong_value": cand.get("wrong_value"),
				"correct_value": cand.get("correct_value"),
				"value_delta": cand.get("value_delta"),
				"classification": cand.get("classification"),
				"s_warehouse": cand.get("s_warehouse"),
				"operator": frappe.session.user,
				"decision": DECISION_REPLACE,
				"evidence_fingerprint": cand.get("evidence_fingerprint"),
				"status": STATUS_APPROVED_REPLACE,
				"action": "CANONICAL_MANUFACTURE_BATCH_REPLACE",
			}
		)

	return {
		"candidates": candidates,
		"approved_groups": approved_groups,
		"exempt_keys": exempt_keys,
		"blockers": blockers,
		"audit": audit,
	}


def apply_consumption_targets_to_canonical(
	canonical_rows: list[dict],
	targets: list[dict],
	*,
	job_card: str,
	wip_warehouse: str | None = None,
) -> tuple[list[dict], list[str]]:
	"""Apply target CONSUME qtys: target<0 block, target==0 omit, target>0 set/add.

	Each target dict: item_code, batch_no, target_qty, rate (optional),
	rate_source (optional), source_lineage (optional).
	"""
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
		resolve_mtfm_department,
	)

	rows = [dict(r) for r in (canonical_rows or [])]
	blockers: list[str] = []
	for t in targets or []:
		item = t.get("item_code")
		batch = t.get("batch_no") or ""
		target = flt(t.get("target_qty"))
		if not item:
			blockers.append("MANUFACTURE_CONSUMPTION_CORRECTION: item_code required")
			continue
		if target < -_QTY_TOLERANCE:
			blockers.append(
				f"MANUFACTURE_CONSUMPTION_CORRECTION: {item}/{batch} target {target} < 0"
			)
			continue
		# Remove existing CONSUME for this key
		rows = [
			r
			for r in rows
			if not (
				r.get("type") == "CONSUME"
				and r.get("item_code") == item
				and (r.get("batch_no") or "") == batch
			)
		]
		if target <= _QTY_TOLERANCE:
			continue  # omit row
		rate = flt(t.get("rate") or t.get("correct_rate") or t.get("valuation_rate"))
		if rate <= 1e-9:
			blockers.append(
				f"ZERO_RATE: {item}/{batch} — authoritative rate required for target consume"
			)
			continue
		wh = (t.get("s_warehouse") or wip_warehouse or "").strip()
		dept_info = resolve_mtfm_department(job_card, item, batch)
		rows.append(
			{
				"type": "CONSUME",
				"item_code": item,
				"batch_no": batch,
				"qty": target,
				"s_warehouse": wh,
				"t_warehouse": None,
				"valuation_rate": rate,
				"basic_rate": rate,
				"custom_output_class": None,
				"secondary_item_type": None,
				"is_finished_item": 0,
				"rate_source": t.get("rate_source") or "issue_transfer",
				"source_lineage": t.get("source_lineage")
				or "MANUFACTURE_CONSUMPTION_CORRECTION",
				"department": dept_info.get("department"),
				"department_evidence": dept_info.get("evidence") or [],
				"_department_ambiguous": bool(dept_info.get("ambiguous")),
				"_dept_candidates": dept_info.get("candidates") or [],
			}
		)
	return rows, blockers


def apply_replacements_to_canonical(
	canonical_rows: list[dict],
	approved_groups: list[dict],
	*,
	job_card: str,
	wip_warehouse: str | None = None,
) -> tuple[list[dict], list[str]]:
	"""REMOVE/REDUCE wrong-batch + ADD/INCREASE correct-batch via target qtys."""
	rows = [dict(r) for r in (canonical_rows or [])]
	blockers: list[str] = []
	if not approved_groups:
		return rows, blockers

	targets: list[dict] = []
	for g in approved_groups:
		item = g["item_code"]
		wrong = g.get("wrong_batch") or ""
		correct = g.get("correct_batch") or ""
		qty = flt(g.get("qty"))
		if qty <= _QTY_TOLERANCE:
			blockers.append(f"MANUFACTURE_BATCH_REPLACE: {item} qty must be positive")
			continue

		# Current wrong consume on canonical
		wrong_have = sum(
			flt(r.get("qty"))
			for r in rows
			if r.get("type") == "CONSUME"
			and r.get("item_code") == item
			and (r.get("batch_no") or "") == wrong
		)
		if wrong_have + _QTY_TOLERANCE < qty:
			blockers.append(
				f"MANUFACTURE_BATCH_REPLACE: cannot reduce {qty} of {item}/{wrong} "
				f"(found {wrong_have} on canonical)"
			)
			continue
		wrong_target = wrong_have - qty  # full removal → 0; partial → keep remainder
		correct_have = sum(
			flt(r.get("qty"))
			for r in rows
			if r.get("type") == "CONSUME"
			and r.get("item_code") == item
			and (r.get("batch_no") or "") == correct
		)
		correct_rate = flt(g.get("correct_rate"))
		targets.append(
			{
				"item_code": item,
				"batch_no": wrong,
				"target_qty": wrong_target,
				"rate": flt(g.get("wrong_rate")) or 1.0,  # unused when target 0
				"rate_source": "historical_mfg",
				"source_lineage": f"REPLACE_MANUFACTURE_BATCH reduce {qty}",
				"s_warehouse": g.get("s_warehouse") or wip_warehouse,
			}
		)
		targets.append(
			{
				"item_code": item,
				"batch_no": correct,
				"target_qty": correct_have + qty,
				"rate": correct_rate,
				"rate_source": "issue_transfer",
				"source_lineage": f"REPLACE_MANUFACTURE_BATCH from {wrong}",
				"s_warehouse": g.get("s_warehouse") or wip_warehouse,
			}
		)

	out, apply_blockers = apply_consumption_targets_to_canonical(
		rows, targets, job_card=job_card, wip_warehouse=wip_warehouse
	)
	return out, blockers + apply_blockers
