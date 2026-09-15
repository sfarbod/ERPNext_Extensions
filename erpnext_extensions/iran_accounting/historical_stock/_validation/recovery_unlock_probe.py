# Copyright (c) 2026 — probe unlockable WAITING_PATIENT_ZERO + SLE replay + G1/G3
from __future__ import annotations

import json
from collections import Counter, defaultdict


def run():
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES, evaluate_row, attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity, classify_stock_entry_gl

	company = "اسپاد فارمد دارو"
	wrong = scan_wrong_rates(company=company, limit=3000)
	waiting = [
		r
		for r in (wrong.get("rows") or [])
		if str(r.get("planner_status") or "") == "WAITING_PATIENT_ZERO"
	]
	# Collect unique patient zeros
	pz_map = defaultdict(list)
	for r in waiting:
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		if pz_v:
			pz_map[pz_v].append(r.get("voucher"))

	# Are PZ vouchers themselves in wrong-rate scan as READY or reconstructable?
	by_voucher = {r.get("voucher"): r for r in (wrong.get("rows") or [])}
	pz_status = Counter()
	pz_ready = []
	pz_exact_not_ready = []
	for pz_v, deps in list(pz_map.items())[:200]:
		row = by_voucher.get(pz_v)
		if not row:
			pz_status["PZ_NOT_IN_SCAN"] += 1
			continue
		ps = str(row.get("planner_status") or "")
		pz_status[ps] += 1
		if ps.startswith("READY") and int(row.get("sql_updates") or 0) > 0:
			pz_ready.append({"pz": pz_v, "deps": len(deps), "sql": row.get("sql_updates")})
		elif row.get("confidence") == "EXACT" and row.get("status") == "RECONSTRUCTABLE":
			pz_exact_not_ready.append(
				{
					"pz": pz_v,
					"ps": ps,
					"sql": row.get("sql_updates"),
					"eligible": row.get("eligible"),
					"reason": row.get("reason"),
					"source": row.get("source"),
				}
			)

	# SLE REPLAY_REQUIRED sample — planner status
	sle = scan_sle_bin(company=company, limit=500)
	sle_replay = [r for r in (sle.get("rows") or []) if r.get("status") == "REPLAY_REQUIRED"]
	sle_plan = Counter(str(r.get("planner_status") or "") for r in sle_replay)
	sle_ready = [
		r
		for r in sle_replay
		if str(r.get("planner_status") or "").startswith("READY") and int(r.get("sql_updates") or 0) > 0
	]

	# G1/G3 detail
	gl = scan_gl_integrity(company=company, limit=200)
	g1g3 = [
		{
			"voucher": r.get("voucher"),
			"gl_class": r.get("gl_class"),
			"eligible": r.get("eligible"),
			"planner_status": r.get("planner_status"),
			"sql": r.get("sql_updates"),
			"sle_poisoned": r.get("sle_poisoned"),
		}
		for r in (gl.get("rows") or [])
		if r.get("gl_class") in ("G1_BALANCED_BUT_ECONOMICALLY_WRONG", "G3_UNBALANCED")
	]

	out = {
		"waiting_pz_rows": len(waiting),
		"unique_pz": len(pz_map),
		"pz_status_in_scan": dict(pz_status),
		"pz_ready_count": len(pz_ready),
		"pz_ready_sample": pz_ready[:10],
		"pz_exact_not_ready_sample": pz_exact_not_ready[:15],
		"sle_replay_planner": dict(sle_plan),
		"sle_replay_ready": len(sle_ready),
		"g1_g3": g1g3,
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:9000])
	return out
