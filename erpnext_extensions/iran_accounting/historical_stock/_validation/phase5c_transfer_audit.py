# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5C — sample audit of transfer reconstruction before root drain."""

from __future__ import annotations

import json

COMPANY = "اسپاد فارمد دارو"


def run(*, n: int = 12):
	import frappe
	from frappe.utils import flt
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
		EXACT,
		RECONSTRUCTABLE,
		reconstruct_transfer_valuation,
	)

	TRANSFER = {
		"Material Transfer",
		"Material Transfer for Manufacture",
		"Send to Subcontractor",
	}
	w = scan_wrong_rates(company=COMPANY, limit=6000)
	rows = [attach_plan(dict(r)) for r in (w.get("rows") or [])]
	cands = []
	for r in rows:
		if (r.get("purpose") or "") not in TRANSFER:
			continue
		tr = r.get("transfer_reconstruction") or reconstruct_transfer_valuation(r)
		r["transfer_reconstruction"] = tr
		if tr.get("classification") not in (EXACT, RECONSTRUCTABLE):
			continue
		if abs(flt(r.get("proposed_rate") or tr.get("expected_rate"))) < 0.0001:
			continue
		cands.append(r)

	# Diversity sample
	picked = []
	seen_kinds = set()

	def kind(r):
		tr = r.get("transfer_reconstruction") or {}
		bits = [r.get("purpose") or ""]
		if r.get("batch") or r.get("batch_no"):
			bits.append("BATCHED")
		else:
			bits.append("NO_BATCH")
		if "Manufacture" in (r.get("purpose") or ""):
			bits.append("FOR_MFG")
		if r.get("matched_but_corrupt") or r.get("matched_but_corrupt_candidate"):
			bits.append("MBC")
		bits.append(tr.get("classification") or "")
		return "|".join(bits)

	for r in sorted(cands, key=lambda x: str(x.get("posting_date") or "")):
		k = kind(r)
		if k not in seen_kinds or len(picked) < 4:
			seen_kinds.add(k)
			picked.append(r)
		if len(picked) >= int(n):
			break

	audits = []
	ok = 0
	for r in picked:
		tr = r["transfer_reconstruction"]
		exp = flt(tr.get("expected_rate"))
		# Native check: outgoing SLE svd/qty
		vn = r.get("voucher")
		item = r.get("item") or r.get("item_code")
		out_sle = frappe.db.sql(
			"""
			SELECT actual_qty, outgoing_rate, stock_value_difference, warehouse
			FROM `tabStock Ledger Entry`
			WHERE voucher_type='Stock Entry' AND voucher_no=%s AND item_code=%s
			  AND is_cancelled=0 AND actual_qty < 0
			ORDER BY posting_datetime LIMIT 1
			""",
			(vn, item),
			as_dict=True,
		)
		native = None
		if out_sle:
			oq = flt(out_sle[0].actual_qty)
			svd = flt(out_sle[0].stock_value_difference)
			native = abs(svd / oq) if abs(oq) > 1e-9 else abs(flt(out_sle[0].outgoing_rate))
		agree = native is not None and abs(native - exp) <= 1.0
		if agree:
			ok += 1
		audits.append(
			{
				"voucher": vn,
				"purpose": r.get("purpose"),
				"item": item,
				"cls": tr.get("classification"),
				"source": tr.get("authoritative_source"),
				"expected": exp,
				"native_outgoing": native,
				"agree": agree,
				"kind": kind(r),
				"upstream": tr.get("upstream_health"),
			}
		)

	out = {
		"sampled": len(audits),
		"agree_n": ok,
		"agree_pct": round(100.0 * ok / len(audits), 1) if audits else 0,
		"kinds": sorted(seen_kinds),
		"audits": audits,
		"engine_ok": ok == len(audits) and len(audits) > 0,
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
