# Copyright (c) 2026, ERPNext Extensions contributors
"""Count Transfer EXACT/RECON root chains (fresh reconstruction)."""

from __future__ import annotations

import json
from collections import Counter

COMPANY = "اسپاد فارمد دارو"
TRANSFER = {
	"Material Transfer",
	"Material Transfer for Manufacture",
	"Send to Subcontractor",
}


def run(*, vouchers: list | None = None, limit: int = 6000):
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
		EXACT,
		RECONSTRUCTABLE,
		reconstruct_transfer_valuation,
		apply_transfer_reconstruction_to_row,
	)
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import scan_i1_negative_rate
	import frappe

	w = scan_wrong_rates(company=COMPANY, limit=int(limit))
	wrows = [attach_plan(dict(r)) for r in (w.get("rows") or [])]
	cls_c = Counter()
	root_cls = Counter()
	roots = {}
	sample_exact = []
	for r in wrows:
		if (r.get("purpose") or "") not in TRANSFER:
			continue
		r = apply_transfer_reconstruction_to_row(dict(r))
		tr = r.get("transfer_reconstruction") or reconstruct_transfer_valuation(r)
		cls = (tr or {}).get("classification") or "?"
		cls_c[cls] += 1
		root = (tr or {}).get("root_voucher") or r.get("voucher")
		if root and root not in roots:
			roots[root] = cls
			root_cls[cls] += 1
		if cls == EXACT and len(sample_exact) < 8:
			sample_exact.append(
				{
					"voucher": r.get("voucher"),
					"root": root,
					"item": r.get("item"),
					"current": r.get("current_rate"),
					"expected": r.get("proposed_rate") or (tr or {}).get("expected_rate"),
					"source": (tr or {}).get("authoritative_source"),
				}
			)

	check = {}
	for vn in vouchers or []:
		hits = [r for r in wrows if r.get("voucher") == vn]
		if not hits:
			check[vn] = "NOT_IN_WRONG_SCAN"
			continue
		r = apply_transfer_reconstruction_to_row(dict(hits[0]))
		tr = r.get("transfer_reconstruction") or reconstruct_transfer_valuation(r)
		check[vn] = {
			"cls": (tr or {}).get("classification"),
			"current": r.get("current_rate"),
			"expected": (tr or {}).get("expected_rate"),
			"planner": r.get("planner_status"),
		}

	neg = {
		"neg_valuation": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
		)[0][0],
		"neg_incoming": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND incoming_rate < -0.0001"
		)[0][0],
		"i1": (scan_i1_negative_rate(company=COMPANY, limit=500) or {}).get("count"),
	}
	out = {
		"findings_by_cls": dict(cls_c),
		"roots_by_cls": dict(root_cls),
		"exact_roots": root_cls.get(EXACT, 0),
		"recon_roots": root_cls.get(RECONSTRUCTABLE, 0),
		"no_action_roots": root_cls.get("NO_ACTION", 0),
		"sample_exact": sample_exact,
		"voucher_check": check,
		"safety": neg,
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
