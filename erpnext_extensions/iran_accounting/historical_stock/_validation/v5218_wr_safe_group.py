# Copyright (c) 2026 — resume Wrong Rate SAFE_GROUP only (proofs already done)
from __future__ import annotations

import json


def run(max_n=8):
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_campaign import (
		inventory,
		prove_safe_group,
	)

	inv = inventory()
	ready = inv.get("ready_rows") or []
	# Exclude already proven vouchers from prior runs
	proven = {
		"MAT-STE-2026-03112",
		"MAT-STE-2026-03119",
		"MAT-STE-2026-03120",
		"MAT-STE-2026-03121",
		"MAT-STE-2026-03127",
		"MAT-STE-2026-03128",
	}
	rest = [r for r in ready if r.get("voucher") not in proven]
	group = prove_safe_group(rest, max_n=max_n)
	summary = {
		"ready_remaining": len(ready),
		"candidates": len(rest),
		"ok": group.get("ok"),
		"stage": group.get("stage"),
		"n_roots": group.get("n_roots") or group.get("n_attempted"),
		"vouchers": group.get("vouchers"),
		"promotion_status": group.get("promotion_status"),
		"first_fail": next((r for r in (group.get("results") or []) if not r.get("ok")), None),
	}
	print(json.dumps(summary, ensure_ascii=False, indent=2, default=str)[:5000])
	return group
