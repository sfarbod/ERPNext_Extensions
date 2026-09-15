# Copyright (c) 2026 — probe already_valued false MANUAL + circular PZ
from __future__ import annotations

import json
from collections import Counter


def run():
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import evaluate_row, attach_plan
	import frappe

	company = "اسپاد فارمد دارو"
	wrong = scan_wrong_rates(company=company, limit=2500)
	rows = wrong.get("rows") or []
	by_v = {r.get("voucher"): r for r in rows}

	already = [
		r
		for r in rows
		if str(r.get("source") or r.get("source_of_truth") or "") == "already_valued"
		or str(r.get("status") or "") == "RATE_REBUILD_COMPLETE"
	]
	# How many WAITING rows point at already_valued PZ?
	waiting = [r for r in rows if str(r.get("planner_status") or "") == "WAITING_PATIENT_ZERO"]
	blocked_by_already = 0
	blocked_by_circular = 0
	samples_already = []
	samples_circular = []
	for r in waiting:
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		prow = by_v.get(pz_v) if pz_v else None
		if prow and (
			str(prow.get("source") or "") == "already_valued"
			or str(prow.get("status") or "") == "RATE_REBUILD_COMPLETE"
			or (
				abs(float(prow.get("current") or prow.get("current_rate") or 0) - float(prow.get("expected") or prow.get("proposed_rate") or 0))
				<= 1.0
				and abs(float(prow.get("expected") or prow.get("proposed_rate") or 0)) > 0.0001
				and prow.get("confidence") == "EXACT"
			)
		):
			blocked_by_already += 1
			if len(samples_already) < 8:
				samples_already.append(
					{
						"dep": r.get("voucher"),
						"pz": pz_v,
						"pz_status": prow.get("status"),
						"pz_source": prow.get("source"),
						"pz_ps": prow.get("planner_status"),
						"cur": prow.get("current"),
						"exp": prow.get("expected"),
					}
				)
		# circular: A waits B, B waits A
		if prow:
			ppz = prow.get("patient_zero")
			ppz_v = ppz.get("voucher_no") if isinstance(ppz, dict) else ppz
			if ppz_v == r.get("voucher"):
				blocked_by_circular += 1
				if len(samples_circular) < 8:
					samples_circular.append(
						{
							"a": r.get("voucher"),
							"b": pz_v,
							"a_dt": r.get("posting_datetime") or r.get("posting_date"),
							"b_dt": prow.get("posting_datetime") or prow.get("posting_date"),
							"a_source": r.get("source"),
							"b_source": prow.get("source"),
							"a_exp": r.get("expected") or r.get("proposed_rate"),
							"b_exp": prow.get("expected") or prow.get("proposed_rate"),
						}
					)

	# LIKELY previous_healthy_sle alone — would EXACT unlock?
	likely_prev = [
		r
		for r in rows
		if r.get("confidence") == "LIKELY"
		and str(r.get("source") or r.get("source_of_truth") or "") == "previous_healthy_sle"
		and abs(float(r.get("expected") or r.get("proposed_rate") or 0)) > 0.0001
	]

	out = {
		"already_valued_or_complete": len(already),
		"waiting_blocked_by_already_valued_pz": blocked_by_already,
		"waiting_circular_pairs": blocked_by_circular,
		"samples_already": samples_already,
		"samples_circular": samples_circular,
		"likely_previous_healthy_sle": len(likely_prev),
		"likely_prev_sample": [
			{
				"voucher": r.get("voucher"),
				"item": r.get("item"),
				"current": r.get("current"),
				"expected": r.get("expected") or r.get("proposed_rate"),
				"ps": r.get("planner_status"),
				"flags": r.get("flags"),
			}
			for r in likely_prev[:8]
		],
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:8000])
	return out
