# Copyright (c) 2026, ERPNext Extensions contributors
"""User-approved zero-net Batch Offset / NO-REPAIR exception (v5.5.14).

Repair remains Job Card × Item × Batch. This module only allows an *explicit*
operator exception when same-JC × same-Item batch remainders net to exactly
zero and economic/ownership safety gates pass. Never auto-applied.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import cint, flt


DECISION_ACCEPT = "ACCEPT_BATCH_OFFSET_NO_REPAIR"
DECISION_ACCEPT_PARTIAL = "ACCEPT_PARTIAL_BATCH_OFFSET_NO_REPAIR"
STATUS_APPROVED = "APPROVED_BATCH_OFFSET"
STATUS_APPROVED_PARTIAL = "APPROVED_PARTIAL_BATCH_OFFSET"
CLASS_QTY_VALUE_SAFE = "QUANTITY_AND_VALUE_SAFE"
CLASS_QTY_SAFE_VALUE_DIFF = "QUANTITY_SAFE_VALUE_DIFFERENT"
CLASS_ACCOUNTING_REVIEW = "ACCOUNTING_REVIEW"
CLASS_UNSAFE = "UNSAFE"
CLASS_AMBIGUOUS = "AMBIGUOUS_PAIR"

KIND_FULL = "FULL"
KIND_PARTIAL = "PARTIAL"

_VALUE_TOLERANCE = 1.0  # IRR — whole-unit rates; exact match expected for safe class
_QTY_TOLERANCE = 1e-6


def _row_key(item_code: str, batch_no: str | None) -> tuple[str, str]:
	return (item_code, batch_no or "")


def _group_evidence_fingerprint(job_card: str, item_code: str, batches: list[dict]) -> str:
	payload = {
		"job_card": job_card,
		"item_code": item_code,
		"batches": [
			{
				"batch_no": b.get("batch_no") or "",
				"issued": flt(b.get("issued")),
				"returned": flt(b.get("returned")),
				"consumed": flt(b.get("consumed")),
				"mi_consumed": flt(b.get("mi_consumed")),
				"scrap": flt(b.get("scrap")),
				"remaining_wip": flt(b.get("remaining_wip")),
				"status": b.get("status"),
			}
			for b in sorted(batches, key=lambda x: x.get("batch_no") or "")
		],
	}
	return hashlib.sha256(
		json.dumps(payload, sort_keys=True, default=str).encode()
	).hexdigest()


def _batch_economic_rate(job_card: str, item_code: str, batch_no: str) -> float:
	"""Authoritative JC-owned SED rate for offset value comparison (not Bin)."""
	rows = frappe.db.sql(
		"""
		select sed.basic_rate, sed.valuation_rate, sed.qty, se.purpose, se.is_return
		from `tabStock Entry Detail` sed
		join `tabStock Entry` se on se.name = sed.parent
		where se.job_card=%s and se.docstatus=1 and sed.item_code=%s
		  and ifnull(sed.batch_no,'')=%s
		order by se.posting_date desc, se.posting_time desc, se.name desc
		""",
		(job_card, item_code, batch_no or ""),
		as_dict=1,
	)
	for r in rows:
		rate = flt(r.valuation_rate) or flt(r.basic_rate)
		if rate > 1e-9:
			return rate
	return 0.0


def _item_has_mi_conflict(job_card: str, item_code: str, scan_rows: list[dict]) -> bool:
	for r in scan_rows:
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

		mis = discover_material_issues(job_card, [])
		for mi in mis or []:
			cls = mi.get("classification")
			if cls not in (MI_USER_DECISION, MI_FINANCE_REVIEW, MI_BLOCKED):
				continue
			for row in mi.get("rows") or []:
				if row.get("item_code") == item_code:
					return True
	except Exception:
		pass
	return False


def _has_repack_conflict(job_card: str, item_code: str, batch_nos: list[str]) -> bool:
	"""Repack on the JC chain for these batches makes offset semantics unsafe."""
	if not batch_nos:
		return False
	n = frappe.db.sql(
		"""
		select count(*) from `tabStock Entry Detail` sed
		join `tabStock Entry` se on se.name = sed.parent
		where se.docstatus=1 and se.purpose='Repack' and sed.item_code=%s
		  and ifnull(sed.batch_no,'') in %s
		  and (se.job_card=%s or se.work_order=%s)
		""",
		(
			item_code,
			tuple(batch_nos),
			job_card,
			frappe.db.get_value("Job Card", job_card, "work_order") or "",
		),
	)[0][0]
	return cint(n) > 0


def detect_batch_offset_candidates(job_card: str, scan_rows: list[dict] | None = None) -> list[dict]:
	"""Return eligible / ineligible zero-net Item offset groups (never auto-approved)."""
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
		rems = [(r, flt(r.get("remaining_wip"))) for r in rows]
		nonzero = [(r, rem) for r, rem in rems if abs(rem) > 1e-9]
		if len(nonzero) < 2:
			continue
		net = sum(rem for _, rem in rems)
		if abs(net) > 1e-9:
			# Partial offset — not eligible
			out.append(
				{
					"job_card": job_card,
					"item_code": item_code,
					"eligible": False,
					"reason": "PARTIAL_OFFSET_NET_NOT_ZERO",
					"net_remaining": net,
					"batches": [
						{
							"batch_no": r.get("batch_no") or "",
							"remaining_wip": flt(r.get("remaining_wip")),
							"status": r.get("status"),
						}
						for r in rows
					],
					"classification": CLASS_UNSAFE,
				}
			)
			continue

		pos = [(r, rem) for r, rem in nonzero if rem > 1e-9]
		neg = [(r, rem) for r, rem in nonzero if rem < -1e-9]
		if not pos or not neg:
			continue

		meta = frappe.db.get_value(
			"Item", item_code, ["has_serial_no", "has_batch_no", "stock_uom"], as_dict=1
		) or {}
		reasons: list[str] = []
		if cint(meta.get("has_serial_no")):
			reasons.append("SERIALIZED_ITEM_NOT_ELIGIBLE")
		if any(r.get("status") == "SCRAP MISMATCH" for r, _ in nonzero):
			reasons.append("COMPONENT_SCRAP_MISMATCH")
		if _item_has_mi_conflict(job_card, item_code, scan_rows):
			reasons.append("MI_REVIEW_OR_CONSUME_PRESENT")
		batch_nos = [r.get("batch_no") or "" for r, _ in nonzero]
		if _has_repack_conflict(job_card, item_code, batch_nos):
			reasons.append("REPACK_BATCH_CONVERSION_PRESENT")

		# Economic legs
		pos_value = 0.0
		neg_value = 0.0
		batch_details = []
		missing_rate = False
		for r, rem in nonzero:
			rate = _batch_economic_rate(job_card, item_code, r.get("batch_no") or "")
			if rate <= 1e-9:
				missing_rate = True
			leg_value = abs(rem) * rate
			if rem > 0:
				pos_value += leg_value
			else:
				neg_value += leg_value
			batch_details.append(
				{
					"batch_no": r.get("batch_no") or "",
					"issued": flt(r.get("issued")),
					"returned": flt(r.get("returned")),
					"consumed": flt(r.get("consumed")),
					"mi_consumed": flt(r.get("mi_consumed")),
					"scrap": flt(r.get("scrap")),
					"remaining_wip": rem,
					"status": r.get("status"),
					"rate": rate,
					"offset_value": leg_value if rem > 0 else -leg_value,
				}
			)

		if missing_rate:
			reasons.append("MISSING_AUTHORITATIVE_RATE")
			classification = CLASS_ACCOUNTING_REVIEW
		elif abs(pos_value - neg_value) > _VALUE_TOLERANCE:
			reasons.append("VALUE_MISMATCH")
			classification = CLASS_QTY_SAFE_VALUE_DIFF
		elif reasons:
			classification = CLASS_UNSAFE
		else:
			classification = CLASS_QTY_VALUE_SAFE

		eligible = classification == CLASS_QTY_VALUE_SAFE and not reasons
		evidence_fp = _group_evidence_fingerprint(job_card, item_code, batch_details)
		out.append(
			{
				"job_card": job_card,
				"item_code": item_code,
				"stock_uom": meta.get("stock_uom"),
				"eligible": eligible,
				"reason": None if eligible else "; ".join(reasons) or classification,
				"classification": classification,
				"net_remaining": 0.0,
				"positive_qty": sum(rem for _, rem in pos),
				"negative_qty": sum(rem for _, rem in neg),
				"positive_value": pos_value,
				"negative_value": neg_value,
				"value_delta": pos_value - neg_value,
				"batches": batch_details,
				"evidence_fingerprint": evidence_fp,
				"decision": DECISION_ACCEPT,
				"action_if_approved": "NO_STOCK_DOCUMENT_CHANGE",
				"explanation": (
					"Item-level quantity is balanced, but historical Batch attribution "
					"is inconsistent. Selecting this option accepts the historical Batch "
					"difference and leaves the original stock documents unchanged."
				),
			}
		)
	return out


def resolve_batch_offset_approvals(
	job_card: str,
	scan_rows: list[dict],
	approvals: list[dict] | None,
	candidates: list[dict] | None = None,
) -> dict[str, Any]:
	"""Validate explicit approvals against current candidates.

	Returns approved groups, exempt keys, blockers, and audit records.
	"""
	candidates = candidates if candidates is not None else detect_batch_offset_candidates(
		job_card, scan_rows
	)
	by_item = {c["item_code"]: c for c in candidates}
	approved_groups: list[dict] = []
	blockers: list[str] = []
	exempt_keys: set[tuple[str, str]] = set()
	audit: list[dict] = []

	for raw in approvals or []:
		if not raw:
			continue
		item_code = raw.get("item_code")
		if not item_code:
			blockers.append("BATCH_OFFSET: approval missing item_code")
			continue
		accepted = cint(raw.get("accepted") or raw.get("approved") or 0) or (
			str(raw.get("decision") or "") == DECISION_ACCEPT
		)
		if not accepted:
			continue
		cand = by_item.get(item_code)
		if not cand:
			blockers.append(f"BATCH_OFFSET: {item_code} is not a current offset candidate")
			continue
		client_fp = (raw.get("evidence_fingerprint") or raw.get("fingerprint") or "").strip()
		if not client_fp:
			blockers.append(f"BATCH_OFFSET: {item_code} approval missing evidence_fingerprint")
			continue
		if client_fp != cand.get("evidence_fingerprint"):
			blockers.append(f"STALE_PLAN: BATCH_OFFSET approval for {item_code} is stale")
			continue
		if not cand.get("eligible"):
			blockers.append(
				f"BATCH_OFFSET: {item_code} not eligible ({cand.get('reason') or cand.get('classification')})"
			)
			continue
		approved_groups.append(cand)
		for b in cand.get("batches") or []:
			exempt_keys.add(_row_key(item_code, b.get("batch_no")))
		audit.append(
			{
				"job_card": job_card,
				"item_code": item_code,
				"batches": [
					{
						"batch_no": b.get("batch_no"),
						"remaining_wip": b.get("remaining_wip"),
						"rate": b.get("rate"),
					}
					for b in (cand.get("batches") or [])
				],
				"net_remaining": 0.0,
				"operator": frappe.session.user,
				"decision": DECISION_ACCEPT,
				"evidence_fingerprint": cand.get("evidence_fingerprint"),
				"classification": cand.get("classification"),
				"status": STATUS_APPROVED,
			}
		)

	return {
		"candidates": candidates,
		"approved_groups": approved_groups,
		"exempt_keys": exempt_keys,
		"blockers": blockers,
		"audit": audit,
	}


def _pair_evidence_fingerprint(
	job_card: str,
	item_code: str,
	positive_batch: str,
	negative_batch: str,
	pair_qty: float,
	positive_remaining: float,
	negative_remaining: float,
	positive_rate: float,
	negative_rate: float,
	value_delta: float,
) -> str:
	payload = {
		"job_card": job_card,
		"item_code": item_code,
		"kind": KIND_PARTIAL,
		"positive_batch": positive_batch or "",
		"negative_batch": negative_batch or "",
		"pair_qty": flt(pair_qty),
		"positive_remaining": flt(positive_remaining),
		"negative_remaining": flt(negative_remaining),
		"positive_rate": flt(positive_rate),
		"negative_rate": flt(negative_rate),
		"value_delta": flt(value_delta),
	}
	return hashlib.sha256(
		json.dumps(payload, sort_keys=True, default=str).encode()
	).hexdigest()


def _find_repack_link(item_code: str, batch_a: str, batch_b: str) -> dict | None:
	"""Optional warehouse-level Repack evidence linking two batches (not required)."""
	if not batch_a or not batch_b or batch_a == batch_b:
		return None
	rows = frappe.db.sql(
		"""
		select se.name, se.posting_date, sed.batch_no, sed.qty, sed.basic_rate,
		       sed.s_warehouse, sed.t_warehouse
		from `tabStock Entry Detail` sed
		join `tabStock Entry` se on se.name = sed.parent
		where se.docstatus=1 and se.purpose='Repack' and sed.item_code=%s
		  and ifnull(sed.batch_no,'') in %s
		order by se.posting_date, se.posting_time, se.name
		""",
		(item_code, (batch_a, batch_b)),
		as_dict=1,
	)
	by_se: dict[str, list] = {}
	for r in rows or []:
		by_se.setdefault(r.name, []).append(r)
	for se_name, sed_rows in by_se.items():
		batches = {(x.batch_no or "") for x in sed_rows}
		if batch_a in batches and batch_b in batches:
			return {
				"repack_document": se_name,
				"posting_date": str(sed_rows[0].posting_date),
				"batches": sorted(batches),
				"qty": max(flt(x.qty) for x in sed_rows),
			}
	return None


def _pair_safety_reasons(
	job_card: str,
	item_code: str,
	pos_row: dict,
	neg_row: dict,
	scan_rows: list[dict],
) -> list[str]:
	reasons: list[str] = []
	meta = frappe.db.get_value(
		"Item", item_code, ["has_serial_no", "has_batch_no", "stock_uom"], as_dict=1
	) or {}
	if cint(meta.get("has_serial_no")):
		reasons.append("SERIALIZED_ITEM_NOT_ELIGIBLE")
	if pos_row.get("status") == "SCRAP MISMATCH" or neg_row.get("status") == "SCRAP MISMATCH":
		reasons.append("COMPONENT_SCRAP_MISMATCH")
	if _item_has_mi_conflict(job_card, item_code, scan_rows):
		reasons.append("MI_REVIEW_OR_CONSUME_PRESENT")
	# Ownership: both legs must be present on this JC scan (caller guarantees).
	# Positive side should reflect unresolved JC quantity (issued path).
	if flt(pos_row.get("issued")) <= 1e-9 and flt(pos_row.get("remaining_wip")) > 1e-9:
		# Allow if remaining exists from JC evidence; issued=0 with +rem is unusual.
		pass
	if flt(neg_row.get("consumed")) <= 1e-9 and flt(neg_row.get("returned")) <= 1e-9:
		# Negative remainder without JC outflow is not the attribution pattern.
		if flt(neg_row.get("remaining_wip")) < -1e-9:
			reasons.append("NEGATIVE_LEG_NO_JC_OUTFLOW")
	batch_nos = [pos_row.get("batch_no") or "", neg_row.get("batch_no") or ""]
	if _has_repack_conflict(job_card, item_code, batch_nos):
		reasons.append("REPACK_BATCH_CONVERSION_PRESENT")
	return reasons


def detect_partial_batch_offset_candidates(
	job_card: str,
	scan_rows: list[dict] | None = None,
	*,
	full_candidates: list[dict] | None = None,
) -> list[dict]:
	"""Exact 1:1 quantity pair candidates (partial Item net allowed).

	MVP: one positive Batch ↔ one negative Batch with equal |remaining|.
	Value mismatch does NOT block eligibility — classification reflects it.
	Ambiguous multi-counterpart matches require explicit USER_DECISION.
	"""
	if scan_rows is None:
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
			scan_golden_rule,
		)

		scan_rows = scan_golden_rule(job_card).get("rows") or []

	# Batches already covered by an eligible FULL item-level offset.
	covered: set[tuple[str, str]] = set()
	for c in full_candidates or []:
		if not c.get("eligible"):
			continue
		for b in c.get("batches") or []:
			covered.add((c["item_code"], b.get("batch_no") or ""))

	by_item: dict[str, list[dict]] = {}
	for r in scan_rows or []:
		by_item.setdefault(r["item_code"], []).append(r)

	out: list[dict] = []
	for item_code, rows in sorted(by_item.items()):
		pos = [(r, flt(r.get("remaining_wip"))) for r in rows if flt(r.get("remaining_wip")) > _QTY_TOLERANCE]
		neg = [(r, flt(r.get("remaining_wip"))) for r in rows if flt(r.get("remaining_wip")) < -_QTY_TOLERANCE]
		if not pos or not neg:
			continue

		# Track negatives claimed by an unambiguous eligible pair suggestion.
		used_neg: set[str] = set()
		used_pos: set[str] = set()

		for pos_row, pos_rem in sorted(pos, key=lambda x: x[0].get("batch_no") or ""):
			pos_batch = pos_row.get("batch_no") or ""
			if (item_code, pos_batch) in covered:
				continue
			matches = [
				(nr, nrem)
				for nr, nrem in neg
				if abs(pos_rem - abs(nrem)) <= _QTY_TOLERANCE
				and (nr.get("batch_no") or "") not in used_neg
				and (item_code, nr.get("batch_no") or "") not in covered
			]
			if not matches:
				continue
			if len(matches) > 1:
				out.append(
					{
						"job_card": job_card,
						"item_code": item_code,
						"kind": KIND_PARTIAL,
						"eligible": False,
						"reason": "AMBIGUOUS_PAIR",
						"classification": CLASS_AMBIGUOUS,
						"positive_batch": pos_batch,
						"positive_remaining": pos_rem,
						"negative_candidates": [
							{
								"batch_no": nr.get("batch_no") or "",
								"remaining_wip": nrem,
							}
							for nr, nrem in matches
						],
						"pair_qty": pos_rem,
						"decision": DECISION_ACCEPT_PARTIAL,
						"action_if_approved": "NO_STOCK_DOCUMENT_CHANGE",
						"explanation": (
							"Multiple negative Batches match this positive Remaining. "
							"Select the counterpart explicitly — no automatic choice."
						),
					}
				)
				continue

			neg_row, neg_rem = matches[0]
			neg_batch = neg_row.get("batch_no") or ""
			pair_qty = pos_rem
			pos_rate = _batch_economic_rate(job_card, item_code, pos_batch)
			neg_rate = _batch_economic_rate(job_card, item_code, neg_batch)
			pos_value = pair_qty * pos_rate
			neg_value = pair_qty * neg_rate
			value_delta = pos_value - neg_value
			reasons = _pair_safety_reasons(job_card, item_code, pos_row, neg_row, scan_rows)
			missing_rate = pos_rate <= 1e-9 or neg_rate <= 1e-9
			if missing_rate:
				reasons.append("MISSING_AUTHORITATIVE_RATE")
				classification = CLASS_ACCOUNTING_REVIEW
			elif abs(value_delta) > _VALUE_TOLERANCE:
				classification = CLASS_QTY_SAFE_VALUE_DIFF
			else:
				classification = CLASS_QTY_VALUE_SAFE

			# Hard safety blocks eligibility; value difference does NOT.
			hard = [r for r in reasons if r != "MISSING_AUTHORITATIVE_RATE"]
			# Missing rate → accounting review, not auto-eligible.
			eligible = not hard and not missing_rate
			if hard:
				classification = CLASS_UNSAFE

			repack = _find_repack_link(item_code, pos_batch, neg_batch)
			fp = _pair_evidence_fingerprint(
				job_card,
				item_code,
				pos_batch,
				neg_batch,
				pair_qty,
				pos_rem,
				neg_rem,
				pos_rate,
				neg_rate,
				value_delta,
			)
			meta = frappe.db.get_value("Item", item_code, "stock_uom") or ""
			out.append(
				{
					"job_card": job_card,
					"item_code": item_code,
					"kind": KIND_PARTIAL,
					"stock_uom": meta,
					"eligible": eligible,
					"reason": None if eligible else "; ".join(reasons) or classification,
					"classification": classification,
					"positive_batch": pos_batch,
					"positive_remaining": pos_rem,
					"negative_batch": neg_batch,
					"negative_remaining": neg_rem,
					"pair_qty": pair_qty,
					"positive_rate": pos_rate,
					"negative_rate": neg_rate,
					"positive_value": pos_value,
					"negative_value": neg_value,
					"value_delta": value_delta,
					"net_remaining_item": sum(flt(r.get("remaining_wip")) for r in rows),
					"unresolved_after_pair": [
						{
							"batch_no": r.get("batch_no") or "",
							"remaining_wip": flt(r.get("remaining_wip")),
							"status": r.get("status"),
						}
						for r in rows
						if (r.get("batch_no") or "") not in (pos_batch, neg_batch)
						and abs(flt(r.get("remaining_wip"))) > _QTY_TOLERANCE
					],
					"batches": [
						{
							"batch_no": pos_batch,
							"remaining_wip": pos_rem,
							"rate": pos_rate,
							"offset_value": pos_value,
							"status": pos_row.get("status"),
							"issued": flt(pos_row.get("issued")),
							"returned": flt(pos_row.get("returned")),
							"consumed": flt(pos_row.get("consumed")),
						},
						{
							"batch_no": neg_batch,
							"remaining_wip": neg_rem,
							"rate": neg_rate,
							"offset_value": -neg_value,
							"status": neg_row.get("status"),
							"issued": flt(neg_row.get("issued")),
							"returned": flt(neg_row.get("returned")),
							"consumed": flt(neg_row.get("consumed")),
						},
					],
					"repack_link_found": bool(repack),
					"repack_document": (repack or {}).get("repack_document"),
					"repack_evidence": repack,
					"evidence_fingerprint": fp,
					"decision": DECISION_ACCEPT_PARTIAL,
					"action_if_approved": "NO_STOCK_DOCUMENT_CHANGE",
					"explanation": (
						"Exact quantity pair across historical Batch attributions. "
						"Approving accepts the quantity reconciliation for repair planning "
						"only — no stock/GL repair for the pair. Other unresolved Batches "
						"for this Item remain required."
					),
				}
			)
			if eligible:
				used_pos.add(pos_batch)
				used_neg.add(neg_batch)
	return out


def resolve_partial_batch_offset_approvals(
	job_card: str,
	scan_rows: list[dict],
	approvals: list[dict] | None,
	candidates: list[dict] | None = None,
) -> dict[str, Any]:
	"""Validate explicit partial pair approvals against current candidates."""
	candidates = (
		candidates
		if candidates is not None
		else detect_partial_batch_offset_candidates(job_card, scan_rows)
	)
	# Index by item + positive + negative for exact pair match.
	by_key = {
		(
			c["item_code"],
			c.get("positive_batch") or "",
			c.get("negative_batch") or "",
		): c
		for c in candidates
		if c.get("kind") == KIND_PARTIAL and c.get("negative_batch") is not None
	}
	approved_groups: list[dict] = []
	blockers: list[str] = []
	exempt_keys: set[tuple[str, str]] = set()
	audit: list[dict] = []

	for raw in approvals or []:
		if not raw:
			continue
		item_code = raw.get("item_code")
		pos_b = raw.get("positive_batch") or ""
		neg_b = raw.get("negative_batch") or ""
		accepted = cint(raw.get("accepted") or raw.get("approved") or 0) or (
			str(raw.get("decision") or "") == DECISION_ACCEPT_PARTIAL
		)
		if not accepted:
			continue
		if not item_code or not pos_b or not neg_b:
			blockers.append(
				"PARTIAL_BATCH_OFFSET: approval requires item_code, positive_batch, negative_batch"
			)
			continue
		cand = by_key.get((item_code, pos_b, neg_b))
		if not cand:
			blockers.append(
				f"PARTIAL_BATCH_OFFSET: {item_code} {pos_b}↔{neg_b} is not a current pair candidate"
			)
			continue
		client_fp = (raw.get("evidence_fingerprint") or raw.get("fingerprint") or "").strip()
		if not client_fp:
			blockers.append(
				f"PARTIAL_BATCH_OFFSET: {item_code} approval missing evidence_fingerprint"
			)
			continue
		if client_fp != cand.get("evidence_fingerprint"):
			blockers.append(
				f"STALE_PLAN: PARTIAL_BATCH_OFFSET approval for {item_code} "
				f"{pos_b}↔{neg_b} is stale"
			)
			continue
		if not cand.get("eligible"):
			blockers.append(
				f"PARTIAL_BATCH_OFFSET: {item_code} {pos_b}↔{neg_b} not eligible "
				f"({cand.get('reason') or cand.get('classification')})"
			)
			continue
		approved_groups.append(cand)
		exempt_keys.add(_row_key(item_code, pos_b))
		exempt_keys.add(_row_key(item_code, neg_b))
		audit.append(
			{
				"job_card": job_card,
				"item_code": item_code,
				"kind": KIND_PARTIAL,
				"positive_batch": pos_b,
				"negative_batch": neg_b,
				"pair_qty": cand.get("pair_qty"),
				"positive_rate": cand.get("positive_rate"),
				"negative_rate": cand.get("negative_rate"),
				"positive_value": cand.get("positive_value"),
				"negative_value": cand.get("negative_value"),
				"value_delta": cand.get("value_delta"),
				"classification": cand.get("classification"),
				"repack_document": cand.get("repack_document"),
				"repack_link_found": cand.get("repack_link_found"),
				"operator": frappe.session.user,
				"decision": DECISION_ACCEPT_PARTIAL,
				"evidence_fingerprint": cand.get("evidence_fingerprint"),
				"status": STATUS_APPROVED_PARTIAL,
				"action": "NO_STOCK_DOCUMENT_CHANGE",
			}
		)

	return {
		"candidates": candidates,
		"approved_groups": approved_groups,
		"exempt_keys": exempt_keys,
		"blockers": blockers,
		"audit": audit,
	}
