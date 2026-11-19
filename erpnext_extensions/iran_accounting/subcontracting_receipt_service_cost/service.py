# Copyright (c) 2026, ERPNext Extensions contributors
"""SCR service-cost correction service (dry-run / apply / discover).

Apply path (when explicitly confirmed):
1. ``db_set`` corrected ``service_cost_per_qty`` on SCR items.
2. Reload SCR and call ``calculate_items_qty_and_amount`` (preserves RM / additional).
3. Persist item rate/amount and parent totals via ``db_update``.
4. Write an audit Comment.
5. Optionally enqueue Transaction RIV (default off — ETO propagation is separate).

Never writes SLE/GL/Bin via SQL. Never uses ``last_purchase_rate`` as the charge.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost import (
	API_VERSION,
	CONFIRM_APPLY,
	MAX_ALLOWLIST,
	SERVICE_COST_TOLERANCE,
)
from erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.allocation import (
	ScrServiceCostAllocationError,
	allocation_within_cap,
	list_sibling_scr_items,
	resolve_po_service_for_sco_item,
)

PRIVILEGED_ROLES = ("Administrator", "System Manager")


class ScrServiceCostCorrectionError(frappe.ValidationError):
	pass


def require_privileged_user() -> str:
	user = frappe.session.user
	roles = set(frappe.get_roles(user))
	if user == "Administrator" or "Administrator" in roles or "System Manager" in roles:
		return user
	frappe.throw(
		"Subcontracting Receipt service-cost correction requires Administrator or System Manager",
		frappe.PermissionError,
	)


def require_post_for_mutation(dry_run: bool) -> None:
	if dry_run:
		return
	req = getattr(frappe.local, "request", None)
	method = (getattr(req, "method", None) or "POST").upper()
	if method != "POST":
		frappe.throw(
			"Mutating SCR service-cost correction requires HTTP POST",
			frappe.PermissionError,
		)


def _parse_allowlist(names) -> list[str]:
	if names is None:
		raise ScrServiceCostCorrectionError("subcontracting_receipts allowlist is required")
	if isinstance(names, str):
		names = names.strip()
		if not names:
			raise ScrServiceCostCorrectionError("subcontracting_receipts allowlist is required")
		try:
			names = json.loads(names)
		except json.JSONDecodeError:
			names = [names]
	if not isinstance(names, (list, tuple)):
		raise ScrServiceCostCorrectionError("subcontracting_receipts must be a list of names")
	out: list[str] = []
	seen: set[str] = set()
	for raw in names:
		name = str(raw or "").strip()
		if not name or name in seen:
			continue
		seen.add(name)
		out.append(name)
	if not out:
		raise ScrServiceCostCorrectionError("subcontracting_receipts allowlist is empty")
	if len(out) > MAX_ALLOWLIST:
		raise ScrServiceCostCorrectionError(f"allowlist exceeds max of {MAX_ALLOWLIST}")
	return out


def fingerprint_scr(scr_name: str) -> str:
	"""Idempotency fingerprint of SCR FG service/rate/amount fields."""
	rows = frappe.get_all(
		"Subcontracting Receipt Item",
		filters={"parent": scr_name},
		fields=[
			"name",
			"item_code",
			"qty",
			"service_cost_per_qty",
			"rm_cost_per_qty",
			"additional_cost_per_qty",
			"rate",
			"amount",
			"subcontracting_order_item",
		],
		order_by="idx, name",
	)
	total = frappe.db.get_value("Subcontracting Receipt", scr_name, "total")
	parts = [scr_name, f"total={flt(total)}"]
	for r in rows:
		parts.append(
			"|".join(
				[
					r.name,
					str(r.item_code or ""),
					str(flt(r.qty)),
					str(flt(r.service_cost_per_qty)),
					str(flt(r.rm_cost_per_qty)),
					str(flt(r.additional_cost_per_qty)),
					str(flt(r.rate)),
					str(flt(r.amount)),
					str(r.subcontracting_order_item or ""),
				]
			)
		)
	return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _propose_item_correction(scr_item: dict, resolution: dict) -> dict[str, Any]:
	"""Build before/after proposal for one FG SCR row; preserves RM / additional."""
	qty = flt(scr_item.get("qty") or scr_item.get("received_qty"))
	rm = flt(scr_item.get("rm_cost_per_qty"))
	addl = flt(scr_item.get("additional_cost_per_qty"))
	current_svc = flt(scr_item.get("service_cost_per_qty"))
	intended_svc = flt(resolution["intended_service_cost_per_qty"])
	intended_rate = rm + intended_svc + addl
	intended_amount = qty * intended_rate
	delta_svc = intended_svc - current_svc
	already_correct = abs(delta_svc) <= SERVICE_COST_TOLERANCE

	return {
		"subcontracting_receipt_item": scr_item.get("name") or scr_item.get("subcontracting_receipt_item"),
		"item_code": scr_item.get("item_code"),
		"qty": qty,
		"subcontracting_order_item": scr_item.get("subcontracting_order_item"),
		"before": {
			"service_cost_per_qty": current_svc,
			"rm_cost_per_qty": rm,
			"additional_cost_per_qty": addl,
			"rate": flt(scr_item.get("rate")),
			"amount": flt(scr_item.get("amount")),
		},
		"after": {
			"service_cost_per_qty": intended_svc,
			"rm_cost_per_qty": rm,
			"additional_cost_per_qty": addl,
			"rate": intended_rate,
			"amount": intended_amount,
		},
		"delta_service_cost_per_qty": delta_svc,
		"delta_service_amount": qty * delta_svc,
		"already_correct": already_correct,
		"rm_preserved": True,
		"fee_type": resolution["fee_type"],
		"po_service_amount": resolution["po_service_amount"],
		"po_last_purchase_rate": resolution["po_last_purchase_rate"],
		"used_last_purchase_rate": False,
		"purchase_order": resolution["purchase_order"],
		"purchase_order_item": resolution["purchase_order_item"],
		"subcontracting_order": resolution["subcontracting_order"],
	}


def inspect_subcontracting_receipt(scr_name: str, *, company: str | None = None) -> dict[str, Any]:
	"""Read-only inspection + proposed corrections for one SCR."""
	require_privileged_user()
	if not scr_name:
		raise ScrServiceCostCorrectionError("subcontracting_receipt is required")

	scr = frappe.db.get_value(
		"Subcontracting Receipt",
		scr_name,
		["name", "docstatus", "status", "company", "supplier", "total_qty", "total", "is_return"],
		as_dict=True,
	)
	if not scr:
		raise ScrServiceCostCorrectionError(f"Subcontracting Receipt {scr_name} not found")
	if company and scr.company != company:
		raise ScrServiceCostCorrectionError(
			f"SCR {scr_name} company {scr.company} does not match requested {company}"
		)

	docstatus = cint(scr.docstatus)
	result: dict[str, Any] = {
		"api_version": API_VERSION,
		"subcontracting_receipt": scr_name,
		"docstatus": docstatus,
		"status": scr.status,
		"company": scr.company,
		"supplier": scr.supplier,
		"total_qty": flt(scr.total_qty),
		"total": flt(scr.total),
		"is_return": cint(scr.is_return),
		"fingerprint": fingerprint_scr(scr_name),
		"eligible": False,
		"action": "reject",
		"reasons": [],
		"items": [],
		"allocation_checks": [],
		"repost_notes": [
			"Correcting SCR fields does not by itself fix ETO Transfer/Manufacture valuation.",
			"See docs/investigations/ETO_SCR_REPOST_PROPAGATION.md for a separate RIV phase.",
		],
	}

	if docstatus == 2:
		result["reasons"].append("cancelled")
		result["action"] = "skip_cancelled"
		return result
	if cint(scr.is_return):
		result["reasons"].append("return_receipt_not_supported")
		result["action"] = "skip_return"
		return result
	if docstatus == 0:
		result["reasons"].append("draft_use_standard_recalc_from_sco")
		# Still compute proposals for visibility.
	elif docstatus != 1:
		result["reasons"].append(f"unexpected_docstatus_{docstatus}")
		return result

	items = frappe.get_all(
		"Subcontracting Receipt Item",
		filters={"parent": scr_name},
		fields=[
			"name",
			"item_code",
			"qty",
			"received_qty",
			"rate",
			"amount",
			"service_cost_per_qty",
			"rm_cost_per_qty",
			"additional_cost_per_qty",
			"subcontracting_order",
			"subcontracting_order_item",
			"purchase_order",
			"purchase_order_item",
			"secondary_item_type",
			"valuation_type",
		],
		order_by="idx",
	)

	fg_items = [
		i
		for i in items
		if not i.get("secondary_item_type") and not i.get("valuation_type")
	]
	if not fg_items:
		result["reasons"].append("no_finished_good_rows")
		result["action"] = "reject"
		return result

	any_change = False
	ambiguous = False
	for item in fg_items:
		if not item.subcontracting_order_item:
			result["reasons"].append(f"missing_sco_item_link:{item.name}")
			ambiguous = True
			continue
		try:
			resolution = resolve_po_service_for_sco_item(item.subcontracting_order_item)
		except ScrServiceCostAllocationError as exc:
			result["reasons"].append(str(exc))
			ambiguous = True
			continue

		proposal = _propose_item_correction(item, resolution)
		result["items"].append(proposal)
		if not proposal["already_correct"]:
			any_change = True

		siblings = list_sibling_scr_items(item.subcontracting_order_item)
		# Cap check using *intended* rate for all non-cancelled siblings.
		intended_alloc = []
		for sib in siblings:
			if cint(sib.get("is_return")):
				continue
			intended_alloc.append(
				{
					"subcontracting_receipt": sib["subcontracting_receipt"],
					"subcontracting_receipt_item": sib["subcontracting_receipt_item"],
					"qty": flt(sib.get("qty")),
					"service_cost_per_qty": resolution["intended_service_cost_per_qty"],
				}
			)
		cap = allocation_within_cap(
			po_service_amount=resolution["po_service_amount"],
			sibling_allocations=intended_alloc,
		)
		cap["subcontracting_order_item"] = item.subcontracting_order_item
		result["allocation_checks"].append(cap)
		if not cap["within_cap"]:
			result["reasons"].append(
				f"allocation_exceeds_po_service:{item.subcontracting_order_item}:over_by={cap['over_by']}"
			)
			ambiguous = True

	if ambiguous:
		result["action"] = "reject_ambiguous"
		result["eligible"] = False
		return result

	if not result["items"]:
		result["action"] = "reject"
		result["eligible"] = False
		return result

	if not any_change:
		result["action"] = "skip_already_correct"
		result["eligible"] = False
		result["reasons"].append("already_correct")
		return result

	if docstatus == 0:
		result["action"] = "propose_draft_only"
		result["eligible"] = False
		return result

	result["action"] = "correct"
	result["eligible"] = True
	return result


def discover_eligible_subcontracting_receipts(
	*,
	company: str | None = None,
	purchase_order: str | None = None,
	subcontracting_order: str | None = None,
	limit: int = 200,
) -> dict[str, Any]:
	"""Read-only discovery of submitted SCRs that appear to need service-cost correction."""
	require_privileged_user()
	limit = min(cint(limit) or 200, 500)

	sql = """
		SELECT DISTINCT scr.name, scr.company, scr.supplier, scr.total_qty, scr.total, scr.posting_date
		FROM `tabSubcontracting Receipt` scr
		INNER JOIN `tabSubcontracting Receipt Item` sri ON sri.parent = scr.name
		WHERE scr.docstatus = 1 AND IFNULL(scr.is_return, 0) = 0
		  AND IFNULL(sri.secondary_item_type, '') = ''
		  AND IFNULL(sri.valuation_type, '') = ''
		  AND IFNULL(sri.subcontracting_order_item, '') != ''
	"""
	params: list[Any] = []
	if company:
		sql += " AND scr.company = %s"
		params.append(company)
	if purchase_order:
		sql += " AND sri.purchase_order = %s"
		params.append(purchase_order)
	if subcontracting_order:
		sql += " AND sri.subcontracting_order = %s"
		params.append(subcontracting_order)
	sql += " ORDER BY scr.posting_date, scr.name LIMIT %s"
	params.append(limit)

	candidates = frappe.db.sql(sql, tuple(params), as_dict=True)
	eligible = []
	skipped = []
	errors = []
	for row in candidates:
		try:
			report = inspect_subcontracting_receipt(row.name, company=company)
		except Exception as exc:
			errors.append({"subcontracting_receipt": row.name, "error": str(exc)})
			continue
		entry = {
			"subcontracting_receipt": row.name,
			"company": row.company,
			"action": report.get("action"),
			"eligible": report.get("eligible"),
			"fingerprint": report.get("fingerprint"),
			"item_deltas": [
				{
					"item_code": i["item_code"],
					"delta_service_amount": i["delta_service_amount"],
					"before_service_cost_per_qty": i["before"]["service_cost_per_qty"],
					"after_service_cost_per_qty": i["after"]["service_cost_per_qty"],
				}
				for i in report.get("items") or []
				if not i.get("already_correct")
			],
		}
		if report.get("eligible"):
			eligible.append(entry)
		else:
			skipped.append(entry)

	return {
		"api_version": API_VERSION,
		"filters": {
			"company": company,
			"purchase_order": purchase_order,
			"subcontracting_order": subcontracting_order,
			"limit": limit,
		},
		"candidate_count": len(candidates),
		"eligible_count": len(eligible),
		"eligible": eligible,
		"skipped": skipped,
		"errors": errors,
	}


def _apply_scr_correction(scr_name: str, inspection: dict, *, enqueue_riv: bool) -> dict[str, Any]:
	"""Mutate one submitted SCR. Caller must have validated confirm + privileges."""
	if not inspection.get("eligible"):
		return {
			"subcontracting_receipt": scr_name,
			"status": "skipped",
			"action": inspection.get("action"),
			"reasons": inspection.get("reasons"),
		}

	before_fp = inspection["fingerprint"]
	doc = frappe.get_doc("Subcontracting Receipt", scr_name)
	changed_rows = []
	for proposal in inspection["items"]:
		if proposal.get("already_correct"):
			continue
		row = next((r for r in doc.items if r.name == proposal["subcontracting_receipt_item"]), None)
		if not row:
			raise ScrServiceCostCorrectionError(
				f"SCR item {proposal['subcontracting_receipt_item']} missing on reload"
			)
		# Preserve RM / additional explicitly.
		row.service_cost_per_qty = flt(proposal["after"]["service_cost_per_qty"])
		row.rm_cost_per_qty = flt(proposal["after"]["rm_cost_per_qty"])
		row.additional_cost_per_qty = flt(proposal["after"]["additional_cost_per_qty"])
		changed_rows.append(row.name)

	doc.calculate_items_qty_and_amount()
	# Persist without transitioning docstatus / running full submit hooks.
	for row in doc.items:
		row.db_update()
	doc.db_set("total", doc.total, update_modified=True)
	if hasattr(doc, "total_qty"):
		doc.db_set("total_qty", doc.total_qty, update_modified=False)

	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"reference_doctype": "Subcontracting Receipt",
			"reference_name": scr_name,
			"content": (
				f"SCR service-cost correction ({API_VERSION}) applied by {frappe.session.user} "
				f"at {now_datetime()}. Rows: {', '.join(changed_rows)}. "
				f"enqueue_riv={cint(enqueue_riv)}. before_fingerprint={before_fp}"
			),
		}
	).insert(ignore_permissions=True)

	riv_job = None
	if enqueue_riv:
		riv_job = _enqueue_transaction_riv(scr_name)

	after_fp = fingerprint_scr(scr_name)
	return {
		"subcontracting_receipt": scr_name,
		"status": "corrected",
		"changed_rows": changed_rows,
		"before_fingerprint": before_fp,
		"after_fingerprint": after_fp,
		"idempotent_replay": before_fp == after_fp,
		"riv": riv_job,
		"items": inspection["items"],
	}


def _enqueue_transaction_riv(scr_name: str) -> dict[str, Any]:
	"""Enqueue Transaction RIV for the SCR only (no automatic ETO STE fix)."""
	existing = frappe.db.get_value(
		"Repost Item Valuation",
		{
			"voucher_type": "Subcontracting Receipt",
			"voucher_no": scr_name,
			"docstatus": ["<", 2],
			"status": ["in", ["Queued", "In Progress"]],
		},
		"name",
	)
	if existing:
		return {"status": "existing", "repost_item_valuation": existing}

	riv = frappe.get_doc(
		{
			"doctype": "Repost Item Valuation",
			"based_on": "Transaction",
			"voucher_type": "Subcontracting Receipt",
			"voucher_no": scr_name,
			"allow_negative_stock": 1,
		}
	)
	riv.insert(ignore_permissions=True)
	return {"status": "queued", "repost_item_valuation": riv.name}


def process_allowlist(
	subcontracting_receipts,
	*,
	dry_run: bool = True,
	confirm: str | None = None,
	fingerprints: dict | str | None = None,
	company: str | None = None,
	enqueue_riv: bool | int = 0,
) -> dict[str, Any]:
	"""Dry-run or apply corrections for an explicit SCR allowlist."""
	user = require_privileged_user()
	dry_run = bool(cint(dry_run) if not isinstance(dry_run, bool) else dry_run)
	require_post_for_mutation(dry_run)
	enqueue_riv = bool(cint(enqueue_riv))

	names = _parse_allowlist(subcontracting_receipts)
	if isinstance(fingerprints, str) and fingerprints.strip():
		fingerprints = json.loads(fingerprints)
	fingerprints = fingerprints or {}
	if fingerprints and not isinstance(fingerprints, dict):
		raise ScrServiceCostCorrectionError("fingerprints must be a JSON object of {scr: fingerprint}")

	if not dry_run:
		if confirm != CONFIRM_APPLY:
			raise ScrServiceCostCorrectionError(
				f"confirm must equal {CONFIRM_APPLY!r} when dry_run=0"
			)

	results = []
	for name in names:
		try:
			inspection = inspect_subcontracting_receipt(name, company=company)
			expected_fp = fingerprints.get(name)
			if expected_fp and expected_fp != inspection["fingerprint"]:
				results.append(
					{
						"subcontracting_receipt": name,
						"status": "rejected_fingerprint_mismatch",
						"expected_fingerprint": expected_fp,
						"actual_fingerprint": inspection["fingerprint"],
					}
				)
				continue

			if dry_run:
				results.append(
					{
						"subcontracting_receipt": name,
						"status": "dry_run",
						"inspection": inspection,
					}
				)
				continue

			# Idempotent: already correct → skip without error.
			if inspection.get("action") == "skip_already_correct":
				results.append(
					{
						"subcontracting_receipt": name,
						"status": "skipped_already_correct",
						"inspection": inspection,
					}
				)
				continue

			applied = _apply_scr_correction(name, inspection, enqueue_riv=enqueue_riv)
			results.append(applied)
		except Exception as exc:
			frappe.log_error(title=f"SCR service-cost correction failed: {name}")
			results.append(
				{
					"subcontracting_receipt": name,
					"status": "error",
					"error": str(exc),
					"error_type": type(exc).__name__,
				}
			)

	return {
		"api_version": API_VERSION,
		"dry_run": dry_run,
		"enqueue_riv": enqueue_riv,
		"user": user,
		"confirm_required": CONFIRM_APPLY,
		"count": len(results),
		"results": results,
	}
