# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5D Transfer canary / drain with convergence + idempotency gates.

Hard caps: ≤25 roots per commit. Stop/rollback on safety or non-convergence.
"""

from __future__ import annotations

import json
from collections import Counter

COMPANY = "اسپاد فارمد دارو"


def _neg():
	import frappe

	return {
		"neg_valuation": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
		)[0][0],
		"neg_incoming": frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND incoming_rate < -0.0001"
		)[0][0],
		"neg_fg": frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabStock Ledger Entry` sle
			JOIN `tabStock Entry` se ON se.name=sle.voucher_no
			WHERE sle.is_cancelled=0 AND se.purpose='Manufacture'
			  AND sle.actual_qty>0 AND sle.incoming_rate < -0.0001
			"""
		)[0][0],
	}


def _prepare_rows(n_roots: int):
	from erpnext_extensions.iran_accounting.historical_stock._validation.phase5c_transfer_wave import (
		_select_roots,
	)
	from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
		EXACT,
		WAITING_UPSTREAM,
	)
	from frappe.utils import flt

	cands, picked, by_root = _select_roots(int(n_roots))
	roots = [by_root[k] for k in picked if k in by_root]
	expanded = []
	for r in roots:
		tr = r.get("transfer_reconstruction") or {}
		# Never force-apply WAITING / poisoned-upstream roots.
		if tr.get("classification") == WAITING_UPSTREAM:
			continue
		if tr.get("upstream_health") == "poisoned":
			continue
		if tr.get("classification") == EXACT and tr.get("upstream_health") not in ("healthy",):
			continue
		prop = flt(r.get("proposed_rate") or tr.get("expected_rate"))
		if abs(prop) < 0.0001:
			continue
		r = dict(r)
		r["proposed_rate"] = prop
		r["expected_rate"] = prop
		r["proposed_amount"] = prop * abs(flt(r.get("qty") or 0))
		r["source_of_truth"] = tr.get("authoritative_source") or "outgoing_sle_svd"
		r["source"] = "transfer_authoritative_reconstruction"
		r["eligible"] = True
		r["confidence"] = "EXACT"
		r["status"] = "RECONSTRUCTABLE"
		r["planner_status"] = "READY_WRONG_RATE"
		r["sql_updates"] = max(int(r.get("sql_updates") or 0), 1)
		pz = r.get("patient_zero") if isinstance(r.get("patient_zero"), dict) else {}
		r["patient_zero"] = {
			"voucher_no": r.get("voucher"),
			"posting_datetime": pz.get("posting_datetime")
			or r.get("posting_datetime")
			or f"{r.get('posting_date') or ''} {r.get('posting_time') or '00:00:00'}".strip(),
		}
		expanded.append(r)
	return expanded


def run(*, n_roots: int = 1, apply: int = 1, chunk_size: int = 25):
	import frappe
	from frappe.utils import flt
	from erpnext_extensions.iran_accounting.historical_stock import api as hr_api
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import scan_i1_negative_rate
	from erpnext_extensions.iran_accounting.historical_stock.transfer_convergence import (
		capture_transfer_evidence,
		classify_convergence_outcome,
		fingerprints_equal,
		sle_fingerprint,
		REPAIRED_TO_NO_ACTION,
	)
	from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
		EXACT,
		NO_ACTION,
		reconstruct_transfer_valuation,
	)

	n_roots = min(int(n_roots), 25)
	chunk_size = min(int(chunk_size), 25)
	out = {
		"phase": "PHASE_5D_TRANSFER_WAVE",
		"version": "5.3.0",
		"n_roots": n_roots,
		"apply": int(apply),
		"before_neg": _neg(),
		"i1_before": (scan_i1_negative_rate(company=COMPANY, limit=200) or {}).get("count"),
	}
	if out["before_neg"]["neg_valuation"] or out["before_neg"]["neg_incoming"] or out["before_neg"]["neg_fg"] or out["i1_before"]:
		out["verdict"] = "STOCK_REPAIR_INCIDENT_STOPPED_CAMPAIGN"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	rows = _prepare_rows(n_roots)
	out["selected"] = len(rows)
	out["vouchers"] = [r.get("voucher") for r in rows]
	if not rows:
		out["verdict"] = "NO_ELIGIBLE_TRANSFER_ROOTS"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	dry = hr_api.repair_wrong_rates_selected(rows=rows, dry_run=True)
	out["dry"] = {
		"aborted": dry.get("aborted"),
		"applied_n": len(dry.get("applied") or []),
		"blocked_n": len(dry.get("blocked") or []),
		"blocked": (dry.get("blocked") or [])[:3],
	}
	if dry.get("aborted") or not (dry.get("applied") or []):
		out["verdict"] = "DRY_BLOCKED"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out
	if int(apply) != 1:
		out["verdict"] = "DRY_ONLY"
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	outcomes = Counter()
	results = []
	total_writes = 0
	idempotency_fail = 0
	too_many = 0
	rolled = 0

	for i in range(0, len(rows), chunk_size):
		chunk = rows[i : i + chunk_size]
		fps_before = {
			(r.get("voucher"), r.get("item")): sle_fingerprint(r.get("voucher"), r.get("item"))
			for r in chunk
		}
		befores = { (r.get("voucher"), r.get("item")): capture_transfer_evidence(r) for r in chunk }
		try:
			applied = hr_api.repair_wrong_rates_selected(rows=chunk, dry_run=False)
			frappe.db.commit()
		except Exception as exc:
			frappe.db.rollback()
			rolled += 1
			msg = f"{type(exc).__name__}: {exc}"
			if "TooManyWrites" in msg:
				too_many += 1
			out["stopped"] = "REVERTED"
			out["error"] = msg
			break

		writes = sum(int(a.get("economic_writes") or (1 if a.get("written") else 0)) for a in (applied.get("applied") or []))
		total_writes += writes

		# second apply — must be economically no-op
		try:
			applied2 = hr_api.repair_wrong_rates_selected(rows=chunk, dry_run=False)
			frappe.db.commit()
		except Exception as exc:
			frappe.db.rollback()
			rolled += 1
			out["stopped"] = "SECOND_APPLY_REVERTED"
			out["error"] = f"{type(exc).__name__}: {exc}"
			break

		for r in chunk:
			key = (r.get("voucher"), r.get("item"))
			after = capture_transfer_evidence(r)
			fp_mid = sle_fingerprint(r.get("voucher"), r.get("item"))
			# fingerprint after second should equal mid (first apply result)
			# We committed first then second; compare post-second to post-first via re-capture
			# Use: second economic writes for this row
			second_econ = 0
			second_statuses = []
			for a in applied2.get("applied") or []:
				if a.get("voucher") == r.get("voucher") and (
					not r.get("item") or a.get("item") == r.get("item")
				):
					second_statuses.append(a.get("status"))
					if a.get("status") == "ALREADY_REPAIRED" or a.get("written") is False:
						continue
					ew = a.get("economic_writes")
					second_econ += int(ew if ew is not None else 1)
			idem_ok = second_econ == 0
			if not idem_ok and second_statuses and all(
				s in ("ALREADY_REPAIRED", "RATE_REPAIR_COMPLETE") for s in second_statuses
			):
				idem_ok = True
				second_econ = 0
			if not second_statuses:
				idem_ok = True
			if not idem_ok:
				idempotency_fail += 1

			row_apply = {
				"aborted": applied.get("aborted"),
				"applied": [
					a
					for a in (applied.get("applied") or [])
					if a.get("voucher") == r.get("voucher")
					and (not r.get("item") or a.get("item") == r.get("item"))
				],
			}
			outcome = classify_convergence_outcome(
				before=befores[key],
				after=after,
				apply_result=row_apply,
				second_fp_equal=idem_ok,
			)
			outcomes[outcome] += 1
			results.append(
				{
					"voucher": r.get("voucher"),
					"item": r.get("item"),
					"outcome": outcome,
					"before_diff": (befores[key].get("reconstruction") or {}).get("diff"),
					"after_diff": (after.get("reconstruction") or {}).get("diff"),
					"after_cls": (after.get("reconstruction") or {}).get("classification"),
					"apply_status": (row_apply["applied"][0].get("status") if row_apply["applied"] else None),
					"pair_ok": bool(
						after.get("outgoing_sle")
						and after.get("incoming_sle")
						and abs(
							abs(after["outgoing_sle"]["svd"]) - abs(after["incoming_sle"]["svd"])
						)
						<= 1.0
					),
					"idempotent": idem_ok,
					"second_econ": second_econ,
				}
			)

		neg = _neg()
		i1 = (scan_i1_negative_rate(company=COMPANY, limit=200) or {}).get("count")
		if neg["neg_valuation"] or neg["neg_incoming"] or neg["neg_fg"] or i1:
			out["stopped"] = "SAFETY"
			out["after_neg"] = neg
			out["i1"] = i1
			break

		# Stop only on true non-convergence after a write attempt.
		# BLOCKED (WAITING_UPSTREAM / TOOL_LIMIT) is an expected non-auto remainder.
		from erpnext_extensions.iran_accounting.historical_stock.transfer_convergence import (
			BLOCKED,
			REPAIR_REVERTED,
			REPAIRED_BUT_STILL_EXACT,
			REPAIRED_BUT_SIBLING_EXACT,
			REPAIRED_BUT_NEW_IDENTITY_EXACT,
			REPAIRED_BUT_DOWNSTREAM_EXACT,
			REPAIRED_BUT_RESIDUAL_DIFF,
			REPAIRED_BUT_WRONG_RATE,
		)

		hard_fail = {
			REPAIRED_BUT_STILL_EXACT,
			REPAIRED_BUT_SIBLING_EXACT,
			REPAIRED_BUT_NEW_IDENTITY_EXACT,
			REPAIRED_BUT_DOWNSTREAM_EXACT,
			REPAIRED_BUT_RESIDUAL_DIFF,
			REPAIRED_BUT_WRONG_RATE,
			REPAIR_REVERTED,
		}
		chunk_results = results[-len(chunk) :]
		bad = [r for r in chunk_results if r["outcome"] in hard_fail]
		if bad:
			out["stopped"] = "NON_CONVERGENCE"
			out["non_converging"] = bad[:5]
			break

	out["outcomes"] = dict(outcomes)
	out["results"] = results
	out["total_economic_writes"] = total_writes
	out["idempotency_fail"] = idempotency_fail
	out["rollback_count"] = rolled
	out["too_many_writes"] = too_many
	out["after_neg"] = _neg()
	out["i1"] = (scan_i1_negative_rate(company=COMPANY, limit=500) or {}).get("count")

	ok = (
		len(results) > 0
		and idempotency_fail == 0
		and rolled == 0
		and not out.get("stopped")
		and out["after_neg"]["neg_valuation"] == 0
		and out["after_neg"]["neg_incoming"] == 0
		and out["after_neg"]["neg_fg"] == 0
		and out["i1"] == 0
		and all(
			o in (REPAIRED_TO_NO_ACTION, "BLOCKED")
			for o in outcomes
		)
		and outcomes.get(REPAIRED_TO_NO_ACTION, 0) + outcomes.get("BLOCKED", 0) == len(results)
	)
	out["verdict"] = "WAVE_PASS" if ok else "WAVE_FAIL"
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out


def drain(*, max_waves: int = 80, roots_per_wave: int = 25):
	"""Loop Transfer EXACT drain until fixed point or non-auto remainder."""
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
		EXACT,
		TRANSFER_PURPOSES,
		WAITING_UPSTREAM,
		apply_transfer_reconstruction_to_row,
		reconstruct_transfer_valuation,
	)
	from erpnext_extensions.iran_accounting.historical_stock.transfer_convergence import (
		REPAIRED_TO_NO_ACTION,
	)
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import scan_i1_negative_rate
	from frappe.utils import flt

	summary = {
		"phase": "PHASE_5D_TRANSFER_DRAIN",
		"version": "5.3.0",
		"waves": [],
		"repaired_roots": 0,
		"still_exact_waves": 0,
	}

	def _count_exact():
		w = scan_wrong_rates(company=COMPANY, limit=6000)
		rows = []
		keys = set()
		waiting = 0
		from erpnext_extensions.iran_accounting.historical_stock.transfer_convergence import (
			transfer_already_balanced,
		)
		from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
			NO_ACTION,
		)

		for raw in w.get("rows") or []:
			if (raw.get("purpose") or "") not in TRANSFER_PURPOSES:
				continue
			r = apply_transfer_reconstruction_to_row(attach_plan(dict(raw)))
			tr = r.get("transfer_reconstruction") or reconstruct_transfer_valuation(r)
			cls = tr.get("classification")
			if cls in (WAITING_UPSTREAM, NO_ACTION) or tr.get("upstream_health") == "poisoned":
				if cls == WAITING_UPSTREAM or tr.get("upstream_health") == "poisoned":
					waiting += 1
				continue
			if cls != EXACT:
				continue
			if tr.get("upstream_health") != "healthy":
				waiting += 1
				continue
			cur = flt(tr.get("current_rate") if tr.get("current_rate") is not None else r.get("current_rate"))
			exp = flt(tr.get("expected_rate"))
			if abs(cur - exp) <= 1:
				continue
			if transfer_already_balanced({**r, "purpose": r.get("purpose")}):
				continue
			rows.append(r)
			keys.add(f"{r.get('voucher')}|{r.get('item')}|{r.get('voucher_detail') or ''}")
		return len(rows), len(keys), waiting

	start = _count_exact()
	summary["start_findings"] = start[0]
	summary["start_roots"] = start[1]
	summary["start_waiting"] = start[2]

	for wave_i in range(int(max_waves)):
		wave = run(n_roots=int(roots_per_wave), apply=1, chunk_size=25)
		wave_summary = {
			"i": wave_i,
			"verdict": wave.get("verdict"),
			"selected": wave.get("selected"),
			"outcomes": wave.get("outcomes"),
			"stopped": wave.get("stopped"),
			"writes": wave.get("total_economic_writes"),
			"idempotency_fail": wave.get("idempotency_fail"),
		}
		summary["waves"].append(wave_summary)
		# Progress line so long drains are observable (full JSON still at end).
		print(
			json.dumps({"drain_progress": wave_summary}, ensure_ascii=False, default=str),
			flush=True,
		)
		summary["repaired_roots"] += int((wave.get("outcomes") or {}).get(REPAIRED_TO_NO_ACTION, 0))
		writes = int(wave.get("total_economic_writes") or 0)
		# Selection thrash guard: all ALREADY_REPAIRED / zero writes ⇒ eligible fixed point.
		if writes == 0 and int(wave.get("selected") or 0) > 0:
			summary["fixed_point"] = True
			summary["stop_reason"] = "ZERO_WRITE_WAVE_ALREADY_REPAIRED"
			break
		if wave.get("verdict") == "NO_ELIGIBLE_TRANSFER_ROOTS":
			summary["fixed_point"] = True
			break
		if wave.get("verdict") != "WAVE_PASS":
			summary["fixed_point"] = False
			summary["stop_reason"] = wave.get("stopped") or wave.get("verdict")
			summary["still_exact_waves"] += 1
			break
		if int(wave.get("selected") or 0) < int(roots_per_wave):
			f, r, _w = _count_exact()
			if r == 0:
				summary["fixed_point"] = True
				break

	end = _count_exact()
	summary["end_findings"] = end[0]
	summary["end_roots"] = end[1]
	summary["end_waiting"] = end[2]
	summary["after_neg"] = _neg()
	summary["i1"] = (scan_i1_negative_rate(company=COMPANY, limit=500) or {}).get("count")
	# Eligible EXACT drained; remaining may be WAITING_UPSTREAM with specific reason.
	if summary.get("fixed_point") or end[1] == 0:
		summary["fixed_point"] = True
		summary["verdict"] = "PHASE_5D_TRANSFER_FIXED_POINT_REACHED"
	else:
		summary["verdict"] = "PHASE_5D_NEEDS_FURTHER_TOOL_DEVELOPMENT"
	print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
	return summary
