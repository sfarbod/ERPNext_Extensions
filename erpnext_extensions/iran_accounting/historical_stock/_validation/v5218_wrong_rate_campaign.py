# Copyright (c) 2026 — v5.2.18 Wrong Rate controlled campaign runner
from __future__ import annotations

import json


def run():
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_campaign import (
		run_controlled_campaign,
	)

	out = run_controlled_campaign(max_group=8)
	summary = {
		"promotion_status": out.get("promotion_status"),
		"ready_wrong_rate": out.get("ready_wrong_rate"),
		"by_rate_status": out.get("by_rate_status"),
		"by_bucket": out.get("by_bucket"),
		"proof1_ok": (out.get("proof1") or {}).get("ok"),
		"proof1_voucher": (out.get("proof1") or {}).get("voucher"),
		"proof1_after": (out.get("proof1") or {}).get("after_rate"),
		"proof1_expected": (out.get("proof1") or {}).get("expected"),
		"proof1_stage": (out.get("proof1") or {}).get("stage"),
		"proof1_reason": (out.get("proof1") or {}).get("applied", {}).get("reason")
		if isinstance((out.get("proof1") or {}).get("applied"), dict)
		else (out.get("proof1") or {}).get("reason") or ((out.get("proof1") or {}).get("dry") or {}).get("reason"),
		"proof2_ok": (out.get("proof2") or {}).get("ok") if out.get("proof2") else None,
		"proof2_voucher": (out.get("proof2") or {}).get("voucher") if out.get("proof2") else None,
		"safe_group_ok": (out.get("safe_group") or {}).get("ok") if out.get("safe_group") else None,
		"safe_group_n": (out.get("safe_group") or {}).get("n_roots") if out.get("safe_group") else None,
	}
	# Include failure detail
	p1 = out.get("proof1") or {}
	if not p1.get("ok"):
		summary["proof1_detail"] = {
			k: p1.get(k)
			for k in ("stage", "dry", "applied", "reason")
			if p1.get(k) is not None
		}
	print(json.dumps(summary, ensure_ascii=False, indent=2, default=str)[:8000])
	return out
