# Copyright (c) 2026 — analyze WR WAITING_PZ / MANUAL blockers for engine unlock
from __future__ import annotations

import json
from collections import Counter, defaultdict


def run():
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES, evaluate_row
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover, classify_i4_row
	from frappe.utils import nowdate
	import frappe

	company = "اسپاد فارمد دارو"
	wrong = scan_wrong_rates(company=company, limit=3000)
	rows = wrong.get("rows") or []

	waiting = [r for r in rows if str(r.get("planner_status") or "") == "WAITING_PATIENT_ZERO"]
	manual = [
		r
		for r in rows
		if str(r.get("planner_status") or "") in ("RATE_MANUAL", "MANUAL", "RATE_AMBIGUOUS", "AMBIGUOUS")
	]

	# --- WAITING_PZ: why is PZ not READY? ---
	pz_buckets = Counter()
	unlockable = []  # PZ has EXACT reconstructable surface we can promote
	circular = []
	pz_not_scanned = []
	pz_details = []

	by_voucher = {r.get("voucher"): r for r in rows}

	for r in waiting:
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		if not pz_v:
			pz_buckets["NO_PZ"] += 1
			continue
		if pz_v == r.get("voucher"):
			circular.append(r.get("voucher"))
			pz_buckets["SELF_PZ"] += 1
			continue
		prow = by_voucher.get(pz_v)
		if not prow:
			pz_buckets["PZ_NOT_IN_SCAN"] += 1
			# Can we reconstruct PZ SLE directly?
			pz_not_scanned.append({"dep": r.get("voucher"), "pz": pz_v, "item": r.get("item"), "wh": r.get("warehouse")})
			continue
		ps = str(prow.get("planner_status") or "")
		pz_buckets[f"PZ_{ps}"] += 1
		# Unlock candidate: PZ EXACT + reconstructable + sql>0 but waiting/manual wrongly
		if (
			prow.get("confidence") == "EXACT"
			and abs(float(prow.get("expected") or prow.get("proposed_rate") or 0)) > 0.0001
			and prow.get("source") not in (None, "manual", "bin")
		):
			unlockable.append(
				{
					"dep": r.get("voucher"),
					"pz": pz_v,
					"pz_ps": ps,
					"pz_conf": prow.get("confidence"),
					"pz_source": prow.get("source") or prow.get("source_of_truth"),
					"pz_eligible": prow.get("eligible"),
					"pz_sql": prow.get("sql_updates"),
					"pz_flags": prow.get("flags"),
					"pz_surface": prow.get("surface"),
					"pz_current": prow.get("current") or prow.get("current_rate"),
					"pz_expected": prow.get("expected") or prow.get("proposed_rate"),
					"reason": prow.get("reason"),
				}
			)

	# Fresh evaluate a sample of unlockable PZs
	fresh = []
	seen_pz = set()
	for u in unlockable:
		if u["pz"] in seen_pz:
			continue
		seen_pz.add(u["pz"])
		prow = by_voucher.get(u["pz"])
		if not prow:
			continue
		d = evaluate_row(dict(prow, topic="WRONG_RATE"))
		fresh.append(
			{
				"pz": u["pz"],
				"stamped_ps": u["pz_ps"],
				"fresh_ps": d.get("planner_status"),
				"fresh_eligible": d.get("eligible"),
				"fresh_sql": d.get("sql_updates"),
				"fresh_reason": d.get("reason"),
				"source": u["pz_source"],
				"expected": u["pz_expected"],
			}
		)
		if len(fresh) >= 25:
			break

	# MANUAL breakdown by confidence / source / flags
	man_conf = Counter(str(r.get("confidence")) for r in manual)
	man_source = Counter(str(r.get("source") or r.get("source_of_truth") or "") for r in manual)
	man_flag = Counter()
	for r in manual:
		for f in r.get("flags") or [r.get("mismatch_class")]:
			if f:
				man_flag[str(f)] += 1
	# LIKELY with single strong source that could be EXACT with policy change
	likely_implied = [
		r
		for r in manual
		if r.get("confidence") == "LIKELY"
		and str(r.get("source") or r.get("source_of_truth") or "")
		in ("previous_healthy_sle", "batch_inward", "manufacture_pool", "version")
	]

	# I4 WAITING / MANUAL sample
	i4 = scan_i4_leftover(company=company, from_date="2026-03-21", to_date=str(nowdate()), limit=5000)
	i4_manual = [r for r in (i4.get("rows") or []) if str(r.get("i4_status") or r.get("planner_status")) == "MANUAL"]
	i4_wait = [r for r in (i4.get("rows") or []) if "WAITING" in str(r.get("i4_status") or r.get("planner_status") or "")]
	i4_man_reasons = Counter((r.get("reason") or "")[:80] for r in i4_manual)
	i4_wait_reasons = Counter((r.get("reason") or "")[:80] for r in i4_wait)

	# PZ_NOT_IN_SCAN: check if SLE has implied_svd wrong outgoing
	pz_sle_probe = []
	for item in pz_not_scanned[:15]:
		sles = frappe.db.sql(
			"""
			SELECT name, voucher_no, actual_qty, incoming_rate, outgoing_rate,
			       stock_value_difference, valuation_rate
			FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND item_code=%s AND is_cancelled=0
			LIMIT 5
			""",
			(item["pz"], item["item"]),
			as_dict=True,
		)
		pz_sle_probe.append({"pz": item["pz"], "item": item["item"], "sles": sles})

	out = {
		"waiting_pz_n": len(waiting),
		"manual_n": len(manual),
		"pz_buckets": dict(pz_buckets),
		"unlockable_exact_pz": len(unlockable),
		"unique_unlockable_pz": len({u["pz"] for u in unlockable}),
		"unlockable_sample": unlockable[:12],
		"fresh_evaluate_pz": fresh,
		"fresh_ps_counts": dict(Counter(f["fresh_ps"] for f in fresh)),
		"manual_by_confidence": dict(man_conf),
		"manual_by_source": dict(man_source.most_common(15)),
		"manual_by_flag": dict(man_flag.most_common(15)),
		"likely_strong_source_n": len(likely_implied),
		"i4_manual_n": len(i4_manual),
		"i4_waiting_n": len(i4_wait),
		"i4_manual_reasons": dict(i4_man_reasons.most_common(8)),
		"i4_waiting_reasons": dict(i4_wait_reasons.most_common(8)),
		"pz_not_in_scan_n": len(pz_not_scanned),
		"pz_sle_probe": pz_sle_probe[:5],
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:14000])
	return out
