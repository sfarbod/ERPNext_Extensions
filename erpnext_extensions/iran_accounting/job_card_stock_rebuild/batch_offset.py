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
STATUS_APPROVED = "APPROVED_BATCH_OFFSET"
CLASS_QTY_VALUE_SAFE = "QUANTITY_AND_VALUE_SAFE"
CLASS_QTY_SAFE_VALUE_DIFF = "QUANTITY_SAFE_VALUE_DIFFERENT"
CLASS_ACCOUNTING_REVIEW = "ACCOUNTING_REVIEW"
CLASS_UNSAFE = "UNSAFE"

_VALUE_TOLERANCE = 1.0  # IRR — whole-unit rates; exact match expected for safe class


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
