# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5C Transfer multi-wave drain (chunked)."""

from __future__ import annotations

import json

COMPANY = "اسپاد فارمد دارو"


def run(*, max_waves: int = 8, n_roots: int = 50, chunk_size: int = 25):
	from erpnext_extensions.iran_accounting.historical_stock._validation.phase5c_transfer_wave import (
		run as wave,
	)
	from erpnext_extensions.iran_accounting.historical_stock._validation.phase5c_transfer_count import (
		run as count,
	)

	waves = []
	totals = {"roots": 0, "applied": 0}
	start = count(limit=6000)
	# count() prints — capture return
	out = {
		"phase": "PHASE_5C_TRANSFER_MULTI",
		"start_exact_roots": start.get("exact_roots"),
		"start_findings_exact": (start.get("findings_by_cls") or {}).get("EXACT"),
		"start_safety": start.get("safety"),
		"waves": waves,
	}
	for i in range(int(max_waves)):
		w = wave(n_roots=int(n_roots), chunk_size=int(chunk_size), apply=1)
		# wave prints — suppress duplicate by not re-printing full; record summary
		summary = {
			"wave": i + 1,
			"verdict": w.get("verdict"),
			"stopped": w.get("stopped"),
			"roots": w.get("selected_root_n"),
			"applied": w.get("total_applied"),
			"after_neg": w.get("after_neg"),
			"i1": w.get("i1"),
		}
		waves.append(summary)
		totals["roots"] += int(w.get("selected_root_n") or 0)
		totals["applied"] += int(w.get("total_applied") or 0)
		if w.get("verdict") != "PHASE_5C_TRANSFER_WAVE_APPLY_OK":
			out["stop"] = w.get("verdict")
			break
		if not w.get("selected_root_n"):
			out["stop"] = "NO_MORE_ROOTS"
			break
	end = count(limit=6000)
	out["end_exact_roots"] = end.get("exact_roots")
	out["end_findings_exact"] = (end.get("findings_by_cls") or {}).get("EXACT")
	out["end_safety"] = end.get("safety")
	out["end_findings_by_cls"] = end.get("findings_by_cls")
	out["end_roots_by_cls"] = end.get("roots_by_cls")
	out["totals"] = totals
	out["delta_exact_roots"] = (out["start_exact_roots"] or 0) - (out["end_exact_roots"] or 0)
	if out.get("stop"):
		out["verdict"] = out["stop"]
	elif (out["end_safety"] or {}).get("neg_valuation") or (out["end_safety"] or {}).get("i1"):
		out["verdict"] = "STOCK_REPAIR_INCIDENT_STOPPED_CAMPAIGN"
	else:
		out["verdict"] = "PHASE_5C_TRANSFER_MULTI_OK"
	# compact print (count/wave already printed a lot)
	print(
		json.dumps(
			{k: out[k] for k in out if k != "waves"}
			| {"waves": waves, "verdict": out["verdict"]},
			ensure_ascii=False,
			indent=2,
			default=str,
		)
	)
	return out
