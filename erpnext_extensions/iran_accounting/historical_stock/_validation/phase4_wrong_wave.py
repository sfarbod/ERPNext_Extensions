# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 4 Wrong Rate fixed-point waves."""

from __future__ import annotations

import json


def run(*, max_waves: int = 6, roots_per_wave: int = 10):
	from erpnext_extensions.iran_accounting.historical_stock._validation.phase4_wrong_canary import (
		run as canary,
	)

	waves = []
	stop = None
	for i in range(int(max_waves)):
		res = canary(n_roots=int(roots_per_wave), apply=1)
		w = {
			"wave": i + 1,
			"verdict": res.get("verdict"),
			"vouchers": res.get("selected_vouchers"),
			"applied_n": (res.get("apply_result") or {}).get("applied_n"),
			"wrong_ready_before": (res.get("wrong_before") or {}).get("ready"),
			"wrong_ready_after": (res.get("wrong_after") or {}).get("ready"),
			"wrong_active_after": (res.get("wrong_after") or {}).get("active"),
			"zero_raw": (res.get("zero_after") or {}).get("raw"),
			"zero_recon": (res.get("zero_after") or {}).get("recon"),
			"neg": res.get("after_neg"),
		}
		waves.append(w)
		if res.get("verdict") not in ("PHASE_4_WRONG_CANARY_APPLY_OK",):
			stop = res.get("verdict")
			break
		if not (res.get("apply_result") or {}).get("applied_n"):
			stop = "NO_MORE_APPLIES"
			break
		if int((res.get("wrong_after") or {}).get("ready") or 0) == 0:
			stop = "WRONG_READY_DRAINED"
			break
	out = {
		"phase": "PHASE_4_WRONG_WAVE",
		"waves": waves,
		"stop": stop or "MAX_WAVES",
		"totals": {
			"applied": sum(int(x.get("applied_n") or 0) for x in waves),
			"final_ready": waves[-1].get("wrong_ready_after") if waves else None,
			"final_zero_raw": waves[-1].get("zero_raw") if waves else None,
		},
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
