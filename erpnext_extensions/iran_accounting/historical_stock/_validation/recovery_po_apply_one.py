# Copyright (c) 2026 — apply one outbound's READY Posting Order pairs
from __future__ import annotations

import json


def run(*, outbound: str | None = None, dry_run: int = 0):
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan, READY_STATUSES
	from erpnext_extensions.iran_accounting.stock_posting_order.repair import (
		apply_repairs,
		_voucher_doctype,
	)

	scan = run_full_history_scan(company="اسپاد فارمد دارو")
	ready = []
	for raw in scan.get("rows") or []:
		r = attach_plan(dict(raw))
		if not (
			r.get("eligible")
			and str(r.get("planner_status") or "") in READY_STATUSES
			and int(r.get("sql_updates") or 0) > 0
		):
			continue
		if outbound and r.get("outbound_document") != outbound:
			continue
		ready.append(r)

	by_out = {}
	for r in ready:
		by_out.setdefault(r.get("outbound_document"), []).append(r)

	out = {"n_ready": len(ready), "outbounds": list(by_out), "results": []}
	for vn, matches in by_out.items():
		meta = {
			"outbound": vn,
			"n": len(matches),
			"inbound_vt": _voucher_doctype(matches[0].get("inbound_document")),
			"valuation_impact": matches[0].get("valuation_impact"),
			"items": [m.get("item") for m in matches],
		}
		try:
			res = apply_repairs(matches, dry_run=bool(int(dry_run)))
			meta["applied"] = len(res.get("applied") or [])
			meta["blocked"] = [
				{"item": (b.get("row") or {}).get("item"), "error": (b.get("error") or "")[:240]}
				for b in (res.get("blocked") or [])
			]
			meta["ok"] = bool(res.get("applied")) and not res.get("blocked")
		except Exception as e:
			meta["ok"] = False
			meta["exc"] = f"{type(e).__name__}: {e}"
		out["results"].append(meta)
		# one outbound per call when targeting
		if outbound:
			break

	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:12000])
	return out
