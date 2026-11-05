# Copyright (c) 2026, ERPNext Extensions contributors
"""RIV-based SABE integerization campaign preview (read-only dry-run).

Historical fractional SABE must be corrected by replaying stock ledgers through
Repost Item Valuation — not by standalone SABE SQL mutation. This module builds
the Phase-2 authorization packet: scope, projected SABE A2, existing GL
recognition, Case A/B notes, and rollback plan. It does not create or execute
RIV documents.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Any

import frappe
from frappe.utils import flt, now_datetime

from erpnext_extensions.iran_accounting.domain.currency import is_irr_company
from erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair import (
	find_fractional_sabe_rows,
)
from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
	localize_sabe_row_values,
	make_sabe_a2_idempotency_key,
	outstanding_sabe_a2_net,
)


def _audit_dir() -> str:
	path = os.path.join(
		frappe.utils.get_bench_path(),
		"sites",
		frappe.local.site,
		"private",
		"files",
		"iran_riv_sabe_integer_campaign",
	)
	os.makedirs(path, exist_ok=True)
	return path


def _project_row(row: dict) -> dict[str, Any]:
	raw_rate = flt(row.get("incoming_rate")) or flt(row.get("outgoing_rate")) or 0.0
	loc = localize_sabe_row_values(
		qty=flt(row.get("qty")),
		raw_rate=raw_rate,
		raw_svd=flt(row.get("stock_value_difference")),
	)
	return {
		"sabe": row.get("name"),
		"bundle": row.get("parent"),
		"item_code": row.get("item_code"),
		"batch_no": row.get("batch_no"),
		"warehouse": row.get("warehouse"),
		"qty": flt(row.get("qty")),
		"voucher_type": row.get("voucher_type"),
		"voucher_no": row.get("voucher_no"),
		"voucher_detail_no": row.get("voucher_detail_no"),
		"posting_datetime": str(row.get("posting_datetime") or ""),
		"old_incoming_rate": flt(row.get("incoming_rate")),
		"old_outgoing_rate": flt(row.get("outgoing_rate")),
		"old_svd": flt(row.get("stock_value_difference")),
		"new_rate": loc["rate"],
		"new_svd": loc["svd"],
		"localization_difference": loc["localization_difference"],
		"classification": "A2",
		"idempotency_key": make_sabe_a2_idempotency_key(
			voucher_type=row.get("voucher_type"),
			voucher_no=row.get("voucher_no"),
			voucher_detail_no=row.get("voucher_detail_no"),
			company=row.get("bundle_company"),
			amount=loc["localization_difference"],
		),
	}


def _voucher_gl_preview(voucher_no: str, proposed_net: float, company: str | None) -> dict[str, Any]:
	if not voucher_no or not frappe.db.exists("Stock Entry", voucher_no):
		return {
			"voucher_no": voucher_no,
			"proposed_a2_net": proposed_net,
			"outstanding_a2_net": proposed_net,
			"note": "non_stock_entry_or_missing",
		}
	doc = frappe.get_doc("Stock Entry", voucher_no)
	if company and doc.company != company:
		return {
			"voucher_no": voucher_no,
			"proposed_a2_net": proposed_net,
			"outstanding_a2_net": 0.0,
			"note": "company_mismatch",
		}
	# Preview must not mutate flags permanently; outstanding uses live DB markers
	# outside RIV (through_riv false).
	recon = outstanding_sabe_a2_net(doc, proposed_net)
	return {
		"voucher_no": voucher_no,
		"purpose": doc.purpose,
		"company": doc.company,
		"value_difference": flt(doc.value_difference),
		"posting_date": str(doc.posting_date),
		**recon,
	}


def build_riv_scope_for_items(
	*,
	item_codes: list[str],
	company: str | None = None,
) -> dict[str, Any]:
	"""Read-only RIV campaign preview for fractional SABE on the given items."""
	rows = find_fractional_sabe_rows(item_codes=item_codes, company=company)
	projected = [_project_row(r) for r in rows]

	by_item: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "net": 0.0, "abs": 0.0})
	by_voucher: dict[str, float] = defaultdict(float)
	for p in projected:
		by_item[p["item_code"]]["n"] += 1
		by_item[p["item_code"]]["net"] += p["localization_difference"]
		by_item[p["item_code"]]["abs"] += abs(p["localization_difference"])
		if p["voucher_no"]:
			by_voucher[p["voucher_no"]] += p["localization_difference"]

	gl_previews = []
	outstanding_total = 0.0
	for voucher, proposed in sorted(by_voucher.items()):
		prev = _voucher_gl_preview(voucher, proposed, company)
		gl_previews.append(prev)
		# outstanding_sabe_a2_net exposes ``outstanding_net``.
		outstanding_total += flt(prev.get("outstanding_net"))

	# RIV identity keys: unique (item, warehouse) from affected SABE + earliest posting
	identities = {}
	for p in projected:
		key = (p["item_code"], p["warehouse"])
		dt = p["posting_datetime"] or ""
		if key not in identities or (dt and dt < identities[key]["posting_datetime"]):
			identities[key] = {
				"item_code": p["item_code"],
				"warehouse": p["warehouse"],
				"posting_datetime": dt,
				"based_on": "Item and Warehouse",
			}

	riv_docs_projected = []
	for ident in identities.values():
		posting_date = (ident["posting_datetime"] or "")[:10] or None
		riv_docs_projected.append(
			{
				"based_on": "Item and Warehouse",
				"item_code": ident["item_code"],
				"warehouse": ident["warehouse"],
				"posting_date": posting_date,
				"allow_negative_stock": 1,
				"note": "Create via supported Repost Item Valuation — not executed by this preview.",
			}
		)

	case_a = _case_a_notes(projected)
	case_b = _case_b_notes()

	payload = {
		"at": str(now_datetime()),
		"site": frappe.local.site,
		"mode": "riv_dry_run_preview",
		"item_codes": item_codes,
		"company": company,
		"policy": (
			"Historical correction via Repost Item Valuation replay only. "
			"Integerization runs after Core BatchNoValuation inside process_sle "
			"(apply_integer_sabe_policy_to_sle). A2 → 621301 with source-aware "
			"dedupe (IRR_SABE_A2 keys + Manufacture value_difference coverage). "
			"Case B WH clear is not A2. No standalone SABE SQL repair."
		),
		"fractional_sabe_count": len(projected),
		"proposed_a2_net": sum(p["localization_difference"] for p in projected),
		"proposed_a2_abs": sum(abs(p["localization_difference"]) for p in projected),
		"outstanding_a2_net_after_dedupe": outstanding_total,
		"by_item": dict(by_item),
		"riv_documents_projected": riv_docs_projected,
		"gl_previews": gl_previews,
		"rows": projected,
		"case_a_102": case_a,
		"case_b_595": case_b,
		"wip_3709_note": (
			"SABE-only WIP +3,709 simulation is invalid under RIV: replay must "
			"realign SLE/Bin with integer SABE at each movement (align_sle at "
			"process_sle end) while preserving Core WH≠Batch gaps (Type B)."
		),
		"rollback_plan": [
			"Full Development DB backup before any RIV execution.",
			"Export baselines: SABE for listed names, SLE/Bin for identities, GL for vouchers.",
			"Execute only projected Repost Item Valuation docs (Item+Warehouse from earliest fractional posting).",
			"On failure: RIV rolls back its transaction; restore backup if partial campaign committed.",
			"Do not reverse pre-existing Manufacture value_difference SA that dedupe recognized.",
			"Re-run same RIV scope twice; monetary fields must be identical (ignore modified).",
		],
		"execution_status": "NOT_EXECUTED",
		"safety": "read_only_preview",
	}
	path = os.path.join(
		_audit_dir(),
		f"riv_dry_run_{now_datetime().strftime('%Y%m%d_%H%M%S')}.json",
	)
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
	payload["audit_path"] = path
	return payload


def _case_a_notes(projected: list[dict]) -> dict[str, Any]:
	batch = "691-13200333-ch240410"
	rows = [p for p in projected if p["item_code"] == "13200333" and p["batch_no"] == batch]
	net = sum(p["localization_difference"] for p in rows)
	return {
		"item_code": "13200333",
		"batch_no": batch,
		"dry_run_rows": len(rows),
		"localization_net": net,
		"draft_sed_43536": 43351004,
		"prior_expected_sle_mag": 43350902,
		"gap_102": 102,
		"classification": (
			"Not an outstanding historical SA by itself: batch A2 pairs net to "
			f"{net}. RIV must replay history; live/RIV submit applies A2 once. "
			"Do not post a separate +102 IRR adjustment without proving unposted residue."
		),
	}


def _case_b_notes() -> dict[str, Any]:
	wh = "انبار پایکار خط تولید اسپاد فارمد"
	batch = "9546-30300084-MM263202F12"
	item = "30300084"
	bin_row = frappe.db.get_value(
		"Bin",
		{"item_code": item, "warehouse": wh},
		["actual_qty", "stock_value"],
		as_dict=True,
	) or {}
	batch_val = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(stock_value_difference),0) AS v, COALESCE(SUM(qty),0) AS q
		FROM `tabSerial and Batch Entry`
		WHERE docstatus=1 AND IFNULL(is_cancelled,0)=0
		  AND item_code=%s AND batch_no=%s AND warehouse=%s
		""",
		(item, batch, wh),
	)
	bv = flt(batch_val[0][0]) if batch_val else 0.0
	bq = flt(batch_val[0][1]) if batch_val else 0.0
	wh_val = flt(bin_row.get("stock_value"))
	return {
		"item_code": item,
		"batch_no": batch,
		"warehouse": wh,
		"bin_qty": flt(bin_row.get("actual_qty")),
		"bin_stock_value": wh_val,
		"batch_qty": bq,
		"batch_sabe_value": bv,
		"type_b_gap": wh_val - bv,
		"expected_wh_clear_on_consume_to_zero": -wh_val,
		"classification": (
			"Type B warehouse MA/layer vs BatchNoValuation. RIV must preserve full "
			"warehouse clear (ABSORB). Do not post gap as A2. Not in fractional SABE scope "
			"when batch SABE rows are already integer."
		),
	}


def dry_run_riv_sabe_integer_campaign(
	item_codes: list[str] | None = None,
	company: str | None = None,
) -> dict[str, Any]:
	"""Public dry-run entrypoint for bench execute / authorization packet."""
	items = item_codes or ["13200333", "30300084"]
	if company and not is_irr_company(company):
		frappe.throw(f"Company {company} is not IRR — RIV SABE integer campaign refused.")
	return build_riv_scope_for_items(item_codes=items, company=company)
