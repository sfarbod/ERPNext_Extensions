# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 4 fixed-point wave — repeatedly apply earliest independent Zero roots.

  bench --site development.localhost execute \\
    erpnext_extensions.iran_accounting.historical_stock._validation.phase4_wave.run \\
    --kwargs "{'max_waves': 8, 'roots_per_wave': 10}"
"""

from __future__ import annotations

import json

COMPANY = "اسپاد فارمد دارو"


def run(*, max_waves: int = 8, roots_per_wave: int = 10):
	from erpnext_extensions.iran_accounting.historical_stock._validation.phase4_canary import run as canary

	waves = []
	stop = None
	for i in range(int(max_waves)):
		res = canary(n_roots=int(roots_per_wave), apply=1)
		summary = {
			"wave": i + 1,
			"verdict": res.get("verdict"),
			"vouchers": res.get("selected_vouchers"),
			"applied_n": (res.get("apply_result") or {}).get("applied_n"),
			"ZERO_HEALED": res.get("ZERO_HEALED"),
			"WRONG_HEALED_ON_SELECTED": res.get("WRONG_HEALED_ON_SELECTED"),
			"zero_raw": (res.get("zero_after") or {}).get("raw"),
			"zero_recon": (res.get("zero_after") or {}).get("recon"),
			"zero_waiting": (res.get("zero_after") or {}).get("waiting"),
			"neg": res.get("after_neg"),
			"i4": res.get("i4_after_count"),
		}
		waves.append(summary)
		v = res.get("verdict")
		if v not in ("PHASE_4_CANARY_APPLY_OK",):
			stop = v
			break
		if not (res.get("apply_result") or {}).get("applied_n"):
			stop = "NO_MORE_APPLIES"
			break
		# Stop if no remaining reconstructable in after-scan
		za = res.get("zero_after") or {}
		if int(za.get("recon") or 0) == 0:
			stop = "ZERO_RECONSTRUCTABLE_DRAINED"
			break

	out = {
		"phase": "PHASE_4_WAVE",
		"waves": waves,
		"stop": stop or "MAX_WAVES",
		"totals": {
			"applied": sum(int(w.get("applied_n") or 0) for w in waves),
			"zero_healed_sum": sum(int(w.get("ZERO_HEALED") or 0) for w in waves),
			"final_zero_raw": (waves[-1].get("zero_raw") if waves else None),
			"final_recon": (waves[-1].get("zero_recon") if waves else None),
			"final_waiting": (waves[-1].get("zero_waiting") if waves else None),
		},
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
