# Copyright (c) 2026 — classify Posting Order optimizer vs planner gaps
from __future__ import annotations

import json
from collections import Counter, defaultdict


COMPANY = "اسپاد فارمد دارو"


def _vn(doc):
	if isinstance(doc, dict):
		return doc.get("voucher_no") or doc.get("name")
	return doc


def run():
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan, READY_STATUSES

	scan = run_full_history_scan(company=COMPANY)
	rows = scan.get("rows") or []
	cache = {}
	# Cross-tab optimizer × planner
	matrix = Counter()
	repairable_opt = []
	no_path_exact = []
	blocked_exact = []
	for raw in rows:
		r = attach_plan(dict(raw), cache=cache)
		opt = str(r.get("optimizer_status") or r.get("status") or "")
		ps = str(r.get("planner_status") or "")
		conf = str(r.get("confidence") or "")
		matrix[f"{opt} → {ps} ({conf})"] += 1
		if opt in ("CROSS_TIME_REPAIRABLE", "REPAIRABLE_SECONDS", "SAME_TIME_REPAIRABLE", "CROSS_TIME_REPAIRABLE"):
			repairable_opt.append(r)
		if ps == "NO_REPAIR_PATH" and conf == "EXACT":
			no_path_exact.append(r)
		if ps == "BLOCKED" and conf == "EXACT":
			blocked_exact.append(r)

	def sample(rows, n=8):
		out = []
		for r in rows[:n]:
			out.append(
				{
					"in": _vn(r.get("inbound_document")),
					"out": _vn(r.get("outbound_document")),
					"item": r.get("item") or r.get("item_code"),
					"wh": r.get("warehouse"),
					"batch": r.get("batch") or r.get("batch_no"),
					"opt": r.get("optimizer_status") or r.get("status"),
					"ps": r.get("planner_status"),
					"conf": r.get("confidence"),
					"reason": (r.get("reason") or r.get("skip_reason") or "")[:180],
					"scope": (r.get("scope") or {}).get("status"),
					"dep": (r.get("scope") or {}).get("dependency_type") or r.get("dependency_type"),
					"min_after": r.get("min_qty_after"),
					"min_before": r.get("min_qty_before"),
				}
			)
		return out

	# Why NO_REPAIR_PATH?
	no_path_reasons = Counter()
	for r in no_path_exact:
		no_path_reasons[str(r.get("reason") or r.get("skip_reason") or "unknown")[:140]] += 1

	blocked_reasons = Counter()
	for r in blocked_exact:
		blocked_reasons[str(r.get("reason") or r.get("skip_reason") or "unknown")[:140]] += 1

	# Among REAL_STOCK_SHORTAGE: any EXACT with proposed that almost works?
	shortage_exact = [
		r
		for r in (attach_plan(dict(x), cache=cache) for x in rows)
		if str(r.get("optimizer_status") or r.get("status")) == "REAL_STOCK_SHORTAGE" and r.get("confidence") == "EXACT"
	]
	shortage_reasons = Counter(str(r.get("reason") or "")[:100] for r in shortage_exact)

	out = {
		"matrix_top": dict(matrix.most_common(40)),
		"n_repairable_optimizer": len(repairable_opt),
		"repairable_optimizer_planner": dict(Counter(str(r.get("planner_status")) for r in repairable_opt)),
		"repairable_samples": sample(repairable_opt, 12),
		"n_no_path_exact": len(no_path_exact),
		"no_path_reasons": dict(no_path_reasons.most_common(15)),
		"no_path_samples": sample(no_path_exact, 8),
		"n_blocked_exact": len(blocked_exact),
		"blocked_reasons": dict(blocked_reasons.most_common(15)),
		"blocked_samples": sample(blocked_exact, 8),
		"n_shortage_exact": len(shortage_exact),
		"shortage_exact_planner": dict(Counter(str(r.get("planner_status")) for r in shortage_exact)),
		"shortage_reason_top": dict(shortage_reasons.most_common(10)),
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:14000])
	return out
