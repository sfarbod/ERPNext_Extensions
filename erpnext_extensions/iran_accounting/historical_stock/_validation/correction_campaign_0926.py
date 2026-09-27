# Copyright (c) 2026, ERPNext Extensions contributors
"""Development-only correction campaign after the frozen 104-root baseline.

Does not mutate the Production allowlist. Applies additional deterministic
EXACT Wrong Rate / READY I4 only. Tests 13200001 native RIV.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock._validation.prod_ready_0926 import (
	COMPANY,
	_gates,
	canaries,
	mfg_delta,
	mfg_fingerprint,
	dash_slice,
)
from erpnext_extensions.iran_accounting.historical_stock.production_execution import (
	DEFAULT_MANIFEST_ID,
	load_manifest,
)

OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/private/files/hr_correction_0926"
)
PROVEN_SOURCES = {
	"batch_purchase_receipt_rate",
	"batch_inward_sabb_rate",
	"batch_stock_reco_rate",
	"stock_reco_corroborated_previous_healthy",
	"outgoing_sle_svd",
}
PROVEN_PURPOSES = {
	"Material Transfer",
	"Material Transfer for Manufacture",
	"Send to Subcontractor",
}


def _jdump(name: str, obj) -> None:
	OUT.mkdir(parents=True, exist_ok=True)
	(OUT / name).write_text(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def allowlisted_vouchers() -> set[str]:
	man = load_manifest(DEFAULT_MANIFEST_ID)
	return {r.get("voucher") for r in man["roots"] if r.get("voucher")}


def remaining_exact_wrong() -> list[dict]:
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

	blocked = allowlisted_vouchers()
	scan = scan_wrong_rates(company=COMPANY, limit=5000)
	rows = scan.get("rows") or []
	ready = [
		r
		for r in rows
		if r.get("eligible")
		and "READY" in str(r.get("planner_status") or "")
		and r.get("confidence") == "EXACT"
		and (r.get("source_of_truth") or r.get("rate_source")) in PROVEN_SOURCES
		and (r.get("purpose") or "") in PROVEN_PURPOSES
		and r.get("voucher") not in blocked
	]
	# Residual READY rows on already-allowlisted vouchers are not new roots.
	by_v = {}
	for r in ready:
		by_v.setdefault(r.get("voucher"), r)
	return list(by_v.values())


def classify_i4_five() -> list[dict]:
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import classify_i4_row

	specs = [
		("MAT-STE-2026-25954", "18200056", "انبار پایکار خط تولید اسپاد فارمد"),
		("MAT-STE-2026-26531", "18200056", "انبار پایکار خط تولید اسپاد فارمد"),
		("MAT-STE-2026-26757", "17400002", "انبار پایکار خط تولید اسپاد فارمد"),
		("MAT-STE-2026-27054", "18200056", "انبار پایکار خط تولید اسپاد فارمد"),
		("MAT-STE-2026-36853", "30100022", "انبار پایکار خط تولید اسپاد فارمد"),
	]
	out = []
	for voucher, item, wh in specs:
		c = classify_i4_row(item, wh, voucher=voucher)
		out.append(
			{
				"voucher": voucher,
				"item": item,
				"warehouse": wh,
				"i4_status": c.get("i4_status") or c.get("status"),
				"eligible": c.get("eligible"),
				"planner_status": c.get("planner_status"),
				"reason": c.get("reason"),
				"confidence": c.get("confidence"),
			}
		)
	return out


def pz_map() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.api import scan_all

	scan = scan_all(company=COMPANY)
	return {
		"count": scan.get("patient_zero_count"),
		"by_topic": scan.get("patient_zero_by_topic"),
		"vouchers": scan.get("patient_zero_vouchers"),
		"dashboard": dash_slice(scan),
	}


def i4_economic_history(item: str, warehouse: str, voucher: str) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
		classify_i4_row,
		preview_i4_replay,
	)

	c = classify_i4_row(item, warehouse, voucher=voucher)
	sles = frappe.db.sql(
		"""
		SELECT name, voucher_no, voucher_type, actual_qty, qty_after_transaction,
		       stock_value, stock_value_difference, valuation_rate, incoming_rate, outgoing_rate,
		       posting_datetime, batch_no
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation
		""",
		(item, warehouse),
		as_dict=True,
	)
	purposes = {}
	for vn in {r.voucher_no for r in sles if r.voucher_type == "Stock Entry"}:
		purposes[vn] = frappe.db.get_value("Stock Entry", vn, "purpose")
	target = next((r for r in sles if r.voucher_no == voucher), None)
	idx = next((i for i, r in enumerate(sles) if r.voucher_no == voucher), None)
	window = sles[max(0, (idx or 0) - 8) : (idx or 0) + 6] if idx is not None else []
	sim = preview_i4_replay(item, warehouse, from_dt=c.get("posting_datetime")) if c.get("posting_datetime") else {}
	# First economic divergence: earliest SLE where qty_after≈0 and |value|>1,
	# or first SLE whose stored stock_value disagrees with running MA.
	first_i4 = None
	for r in sles:
		if abs(flt(r.qty_after_transaction)) <= 1e-6 and abs(flt(r.stock_value)) > 1:
			first_i4 = r
			break
	gl = frappe.db.sql(
		"""SELECT account, ROUND(SUM(debit),2) d, ROUND(SUM(credit),2) c
		FROM `tabGL Entry` WHERE voucher_no=%s AND is_cancelled=0 GROUP BY account""",
		voucher,
		as_dict=True,
	)
	return {
		"classify": {
			k: c.get(k)
			for k in (
				"i4_status",
				"eligible",
				"previous_voucher",
				"previous_qty",
				"previous_value",
				"previous_healthy",
				"previous_blocker",
				"pz_residual_clears_in_sim",
				"qty_after",
				"current_value",
				"residual_value",
				"replay_count",
				"stop_before_voucher",
				"stop_reason",
				"message",
			)
		},
		"sle_count": len(sles),
		"first_i4": first_i4,
		"target": target,
		"purpose": purposes.get(voucher),
		"window": [{**r, "purpose": purposes.get(r.voucher_no)} for r in window],
		"simulation": sim,
		"gl": gl,
	}


def inspect_i4_all() -> dict:
	specs = [
		("MAT-STE-2026-25954", "18200056", "انبار پایکار خط تولید اسپاد فارمد"),
		("MAT-STE-2026-26531", "18200056", "انبار پایکار خط تولید اسپاد فارمد"),
		("MAT-STE-2026-26757", "17400002", "انبار پایکار خط تولید اسپاد فارمد"),
		("MAT-STE-2026-27054", "18200056", "انبار پایکار خط تولید اسپاد فارمد"),
		("MAT-STE-2026-36853", "30100022", "انبار پایکار خط تولید اسپاد فارمد"),
	]
	out = {f"{v}|{item}": i4_economic_history(item, wh, v) for v, item, wh in specs}
	_jdump("i4_histories.json", out)
	return out


def classify_all_ready_wr_sources() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

	blocked = allowlisted_vouchers()
	scan = scan_wrong_rates(company=COMPANY, limit=5000)
	rows = [r for r in (scan.get("rows") or []) if r.get("eligible") and "READY" in str(r.get("planner_status") or "")]
	by_v = {}
	for r in rows:
		by_v.setdefault(r.get("voucher"), r)
	roots = list(by_v.values())
	out = {
		"ready_roots": len(roots),
		"already_allowlisted": sum(1 for r in roots if r.get("voucher") in blocked),
		"new": sum(1 for r in roots if r.get("voucher") not in blocked),
		"by_source": dict(Counter((r.get("source_of_truth") or r.get("rate_source") or "?") for r in roots if r.get("voucher") not in blocked)),
		"by_purpose": dict(Counter((r.get("purpose") or "?") for r in roots if r.get("voucher") not in blocked)),
		"by_confidence": dict(Counter((r.get("confidence") or "?") for r in roots if r.get("voucher") not in blocked)),
		"samples": [
			{
				"voucher": r.get("voucher"),
				"item": r.get("item"),
				"source": r.get("source_of_truth") or r.get("rate_source"),
				"purpose": r.get("purpose"),
				"confidence": r.get("confidence"),
				"expected": r.get("expected") or r.get("proposed_rate"),
			}
			for r in roots
			if r.get("voucher") not in blocked
		][:40],
	}
	_jdump("ready_wr_sources.json", out)
	return out


def inspect_remaining() -> dict:
	wr = remaining_exact_wrong()
	i4 = classify_i4_five()
	pz = pz_map()
	out = {
		"gates": _gates(),
		"canaries": canaries(),
		"remaining_exact_wr": len(wr),
		"by_source": dict(Counter((r.get("source_of_truth") or r.get("rate_source")) for r in wr)),
		"by_purpose": dict(Counter(r.get("purpose") for r in wr)),
		"wr_samples": [
			{
				"voucher": r.get("voucher"),
				"item": r.get("item"),
				"source": r.get("source_of_truth") or r.get("rate_source"),
				"purpose": r.get("purpose"),
				"expected": r.get("expected") or r.get("proposed_rate"),
			}
			for r in wr[:25]
		],
		"i4": i4,
		"pz": {"count": pz["count"], "by_topic": pz["by_topic"], "dashboard": pz["dashboard"]},
	}
	_jdump("inspect_remaining.json", out)
	return out


NAMED_SABB_WAVE = (
	"MAT-STE-2026-28735",
	"MAT-STE-2026-25407",
	"MAT-STE-2026-25442",
	"PO-JOB07505-1",
	"MAT-STE-2026-25446",
	"MAT-STE-2026-25715",
	"MAT-STE-2026-25716",
	"MAT-STE-2026-25741",
	"MAT-STE-2026-25742",
)


def apply_named_sabb_wave(*, limit: int = 1) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import plan, execute
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock._validation.prod_ready_0926 import rebuild_bins

	mfg0 = mfg_fingerprint()
	baseline = _gates()
	log = []
	for voucher in NAMED_SABB_WAVE[:limit]:
		scan = scan_wrong_rates(company=COMPANY, voucher=voucher, limit=30)
		rows = [r for r in (scan.get("rows") or []) if r.get("eligible")]
		if not rows:
			log.append({"voucher": voucher, "skipped": "no_eligible"})
			continue
		# One voucher may carry several EXACT SABB identities.
		for row in rows:
			live = execute(plan(dict(row)), dry_run=False)
			frappe.db.commit()
			g = _gates()
			md = mfg_delta(mfg0, mfg_fingerprint())
			entry = {
				"voucher": voucher,
				"item": row.get("item"),
				"ok": live.get("ok"),
				"state": live.get("primary_state"),
				"source": row.get("source_of_truth") or row.get("rate_source"),
				"gates": g,
			}
			log.append(entry)
			if g["i1"] or g["neg_rate"] or int(g["neg_after"]) > int(baseline["neg_after"]):
				_jdump("ABORT_named_sabb.json", entry)
				raise RuntimeError(f"safety abort {voucher} {row.get('item')}")
			if any(abs(flt(x)) > 1e-6 for x in md.values()):
				raise RuntimeError(f"mfg qty changed after {voucher}")
	rebuild_bins()
	out = {
		"applied": len(log),
		"ok": sum(1 for e in log if e.get("ok")),
		"log": log,
		"gates": _gates(),
		"canaries": canaries(),
		"mfg_delta": mfg_delta(mfg0, mfg_fingerprint()),
	}
	_jdump(f"named_sabb_{limit}.json", out)
	return out


def apply_remaining_exact_waves(*, limit: int = 25) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import plan, execute
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock._validation.prod_ready_0926 import rebuild_bins

	mfg0 = mfg_fingerprint()
	baseline = _gates()
	roots = remaining_exact_wrong()[:limit]
	log = []
	for spec in roots:
		scan = scan_wrong_rates(company=COMPANY, voucher=spec.get("voucher"), limit=30)
		rows = [r for r in (scan.get("rows") or []) if r.get("eligible")]
		if not rows:
			log.append({"voucher": spec.get("voucher"), "skipped": "no_eligible"})
			continue
		row = next((r for r in rows if r.get("item") == spec.get("item")), rows[0])
		live = execute(plan(dict(row)), dry_run=False)
		frappe.db.commit()
		g = _gates()
		md = mfg_delta(mfg0, mfg_fingerprint())
		entry = {
			"voucher": spec.get("voucher"),
			"ok": live.get("ok"),
			"state": live.get("primary_state"),
			"source": spec.get("source_of_truth") or spec.get("rate_source"),
			"gates": g,
		}
		log.append(entry)
		if g["i1"] or g["neg_rate"] or int(g["neg_after"]) > int(baseline["neg_after"]):
			_jdump("ABORT_extra_wr.json", entry)
			raise RuntimeError(f"safety abort {spec.get('voucher')}")
		if any(abs(flt(x)) > 1e-6 for x in md.values()):
			raise RuntimeError(f"mfg qty changed after {spec.get('voucher')}")
	bins = rebuild_bins()
	out = {
		"applied": len(log),
		"ok": sum(1 for e in log if e.get("ok")),
		"log": log,
		"bins": bins,
		"gates": _gates(),
		"canaries": canaries(),
		"mfg_delta": mfg_delta(mfg0, mfg_fingerprint()),
	}
	_jdump(f"extra_wr_wave_{limit}.json", out)
	return out


def apply_ready_i4() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
		classify_i4_row,
		repair_i4_selected,
	)
	from erpnext_extensions.iran_accounting.historical_stock._validation.prod_ready_0926 import rebuild_bins

	mfg0 = mfg_fingerprint()
	baseline = _gates()
	rows = []
	for spec in classify_i4_five():
		c = classify_i4_row(spec["item"], spec["warehouse"], voucher=spec["voucher"])
		if c.get("eligible") or str(c.get("i4_status") or "") == "READY_I4":
			rows.append(c)
	item, wh = "30100022", "انبار پایکار خط تولید اسپاد فارمد"
	before = frappe.db.sql(
		"""SELECT voucher_no, actual_qty, qty_after_transaction, stock_value,
		          stock_value_difference, valuation_rate
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND voucher_no IN ('MAT-STE-2026-36853','MAT-STE-2026-36901','MAT-STE-2026-36763')
		ORDER BY posting_datetime, creation""",
		(item, wh),
		as_dict=True,
	)
	gl_before = frappe.db.sql(
		"""SELECT voucher_no, account, ROUND(SUM(debit),2) d, ROUND(SUM(credit),2) c
		FROM `tabGL Entry`
		WHERE voucher_no IN ('MAT-STE-2026-36853','MAT-STE-2026-36901') AND is_cancelled=0
		GROUP BY voucher_no, account""",
		as_dict=True,
	)
	if not rows:
		out = {"applied": 0, "reason": "no_ready_i4", "classified": classify_i4_five(), "before": before}
		_jdump("i4_apply.json", out)
		return out
	preview = repair_i4_selected(rows, dry_run=True)
	live = repair_i4_selected(rows, dry_run=False)
	g = _gates()
	if g["i1"] or g["neg_rate"] or int(g["neg_after"]) > int(baseline["neg_after"]):
		raise RuntimeError(f"I4 apply poisoned {g}")
	rebuild_bins()
	after = frappe.db.sql(
		"""SELECT voucher_no, actual_qty, qty_after_transaction, stock_value,
		          stock_value_difference, valuation_rate
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND voucher_no IN ('MAT-STE-2026-36853','MAT-STE-2026-36901','MAT-STE-2026-36763')
		ORDER BY posting_datetime, creation""",
		(item, wh),
		as_dict=True,
	)
	gl_after = frappe.db.sql(
		"""SELECT voucher_no, account, ROUND(SUM(debit),2) d, ROUND(SUM(credit),2) c
		FROM `tabGL Entry`
		WHERE voucher_no IN ('MAT-STE-2026-36853','MAT-STE-2026-36901') AND is_cancelled=0
		GROUP BY voucher_no, account""",
		as_dict=True,
	)
	adj_new = frappe.db.sql(
		"""SELECT voucher_no, account, debit, credit FROM `tabGL Entry`
		WHERE is_cancelled=0 AND account LIKE '621301%%'
		  AND voucher_no IN ('MAT-STE-2026-36853','MAT-STE-2026-36901','MAT-STE-2026-36899-1',
		                     'MAT-STE-2026-36986','MAT-STE-2026-37011','MAT-STE-2026-37221')""",
		as_dict=True,
	)
	out = {
		"preview": {k: preview.get(k) for k in ("dry_run", "aborted", "sql_updates_executed", "count") if k in preview},
		"applied_n": len(live.get("applied") or []),
		"aborted": live.get("aborted"),
		"before": before,
		"after": after,
		"gl_before": gl_before,
		"gl_after": gl_after,
		"stock_adj_621301_touched": adj_new,
		"gates": g,
		"canaries": canaries(),
		"mfg_delta": mfg_delta(mfg0, mfg_fingerprint()),
		"classified_after": classify_i4_five(),
	}
	_jdump("i4_apply.json", out)
	return out


def inspect_37090() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_native import (
		reconstruct_manufacture_valuation,
	)
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import plan, execute

	ev = reconstruct_manufacture_valuation("MAT-STE-2026-37090")
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

	scan = scan_wrong_rates(company=COMPANY, voucher="MAT-STE-2026-37090", limit=20)
	rows = scan.get("rows") or []
	plans = []
	for r in rows:
		p = plan(dict(r))
		d = execute(p, dry_run=True)
		plans.append(
			{
				"item": r.get("item"),
				"source": r.get("source_of_truth") or r.get("rate_source"),
				"eligible": r.get("eligible"),
				"expected": r.get("expected") or r.get("proposed_rate"),
				"plan_state": p.primary_state,
				"plan_reason": p.reason,
				"dry_ok": d.get("ok"),
				"dry_state": d.get("primary_state"),
			}
		)
	out = {
		"classification": ev.get("classification"),
		"eligible": ev.get("eligible"),
		"expected_target_rate": ev.get("expected_target_rate"),
		"consumed_value": ev.get("consumed_value"),
		"scrap_value": ev.get("scrap_value"),
		"reason": ev.get("reason"),
		"fg_rows": ev.get("fg_rows"),
		"plans": plans,
		"gates": _gates(),
	}
	_jdump("inspect_37090.json", out)
	return out


def test_13200001_riv() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import create_and_run_narrow_riv
	from erpnext_extensions.iran_accounting.historical_stock.repost import preview_repost_selected

	item = "13200001"
	wh = "انبار Quarantine اقلام بسته بندی ثانویه اسپاد"
	prev = preview_repost_selected(item, wh, company=COMPANY)
	earliest = frappe.db.sql(
		"""SELECT posting_date, posting_time, voucher_no
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation LIMIT 1""",
		(item, wh),
		as_dict=True,
	)
	gl36933 = frappe.db.sql(
		"""SELECT account, ROUND(SUM(debit),2) d, ROUND(SUM(credit),2) c
		FROM `tabGL Entry` WHERE voucher_no='MAT-STE-2026-36933' AND is_cancelled=0
		GROUP BY account""",
		as_dict=True,
	)
	out = {
		"preview": {k: prev.get(k) for k in ("eligible", "status", "reason", "sle_state")},
		"gl36933_before": gl36933,
	}
	if not earliest:
		out["ok"] = False
		out["reason"] = "no_sle"
		_jdump("riv_13200001.json", out)
		return out
	# Use bounded start = earliest for this identity (the previous poison case)
	g0 = _gates()
	riv = create_and_run_narrow_riv(
		item, wh, posting_date=earliest[0].posting_date, posting_time=earliest[0].posting_time
	)
	frappe.db.commit()
	g1 = _gates()
	out.update(riv)
	out["gates0"] = g0
	out["gates1"] = g1
	out["poisoned"] = bool(g1["i1"] or g1["neg_rate"] or int(g1["neg_after"]) > int(g0["neg_after"]))
	out["canaries"] = canaries()
	gl36933_after = frappe.db.sql(
		"""SELECT account, ROUND(SUM(debit),2) d, ROUND(SUM(credit),2) c
		FROM `tabGL Entry` WHERE voucher_no='MAT-STE-2026-36933' AND is_cancelled=0
		GROUP BY account""",
		as_dict=True,
	)
	out["gl36933_after"] = gl36933_after
	_jdump("riv_13200001.json", out)
	return out


def apply_37090() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import plan, execute

	mfg0 = mfg_fingerprint()
	baseline = _gates()
	scan = scan_wrong_rates(company=COMPANY, voucher="MAT-STE-2026-37090", limit=20)
	rows = [r for r in (scan.get("rows") or []) if r.get("item") == "30100255"] or (scan.get("rows") or [])
	if not rows:
		out = {"ok": False, "reason": "no_scan_row"}
		_jdump("apply_37090.json", out)
		return out
	p = plan(dict(rows[0]))
	dry = execute(p, dry_run=True)
	live = execute(p, dry_run=False)
	frappe.db.commit()
	g = _gates()
	if g["i1"] or g["neg_rate"] or int(g["neg_after"]) > int(baseline["neg_after"]):
		raise RuntimeError(f"37090 poisoned {g}")
	md = mfg_delta(mfg0, mfg_fingerprint())
	if any(abs(flt(x)) > 1e-6 for x in md.values()):
		raise RuntimeError(f"37090 mfg qty changed {md}")
	out = {
		"plan_state": p.primary_state,
		"plan_reason": p.reason,
		"dry_ok": dry.get("ok"),
		"live_ok": live.get("ok"),
		"live_state": live.get("primary_state"),
		"gates": g,
		"canaries": canaries(),
		"mfg_delta": md,
	}
	_jdump("apply_37090.json", out)
	return out


def second_riv_13200001() -> dict:
	return test_13200001_riv()


def inspect_36933_gl() -> dict:
	rows = frappe.db.sql(
		"""SELECT account, ROUND(SUM(debit),2) d, ROUND(SUM(credit),2) c
		FROM `tabGL Entry` WHERE voucher_no='MAT-STE-2026-36933' AND is_cancelled=0
		GROUP BY account ORDER BY account""",
		as_dict=True,
	)
	tot_d = sum(flt(r.d) for r in rows)
	tot_c = sum(flt(r.c) for r in rows)
	out = {
		"rows": rows,
		"debit": tot_d,
		"credit": tot_c,
		"diff": tot_d - tot_c,
		"uses_621301": any("621301" in (r.account or "") for r in rows),
		"uses_622515": any("622515" in (r.account or "") for r in rows),
		"gates": _gates(),
	}
	_jdump("gl_36933_after_riv.json", out)
	return out


def pz_first_divergence(*, limit: int = 40) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.api import scan_all

	scan = scan_all(company=COMPANY)
	pz = scan.get("patient_zero_rows") or scan.get("patient_zeros") or []
	if not pz:
		# fallback: dashboard identities
		from erpnext_extensions.iran_accounting.historical_stock.patient_zero import (
			scan_patient_zero,
		)

		pz_scan = scan_patient_zero(company=COMPANY, limit=500)
		pz = pz_scan.get("rows") or []
	roots = []
	seen = set()
	for r in pz:
		item = r.get("item") or r.get("item_code")
		wh = r.get("warehouse")
		key = (item, wh)
		if not item or not wh or key in seen:
			continue
		seen.add(key)
		first = frappe.db.sql(
			"""SELECT voucher_no, voucher_type, actual_qty, qty_after_transaction,
			          stock_value, stock_value_difference, valuation_rate, incoming_rate,
			          outgoing_rate, posting_datetime
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			ORDER BY posting_datetime, creation LIMIT 3""",
			(item, wh),
			as_dict=True,
		)
		roots.append(
			{
				"item": item,
				"warehouse": wh,
				"topic": r.get("topic") or r.get("reason"),
				"voucher": r.get("voucher_no") or r.get("voucher"),
				"first_sles": first,
			}
		)
		if len(roots) >= limit:
			break
	out = {
		"dashboard": dash_slice(scan) if isinstance(scan, dict) and scan.get("dashboard") else None,
		"pz_count": scan.get("patient_zero_count") if isinstance(scan, dict) else len(pz),
		"unique_identities": len(seen),
		"roots": roots,
		"gates": _gates(),
	}
	_jdump("pz_first_divergence.json", out)
	return {"pz_count": out["pz_count"], "unique_identities": out["unique_identities"], "n": len(roots), "dashboard": out["dashboard"], "gates": out["gates"]}


def sync_bins() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock._validation.prod_ready_0926 import rebuild_bins

	out = rebuild_bins()
	out["gates"] = _gates()
	_jdump("bin_sync.json", out)
	return out


def riv_37090() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import create_and_run_narrow_riv

	item = "30100255"
	wh = "انبارک انتقال فیلینگ به اتوکلاو - E"
	se = frappe.db.get_value(
		"Stock Entry", "MAT-STE-2026-37090", ["posting_date", "posting_time"], as_dict=True
	)
	g0 = _gates()
	riv = create_and_run_narrow_riv(item, wh, posting_date=se.posting_date, posting_time=se.posting_time)
	frappe.db.commit()
	g1 = _gates()
	out = {
		**riv,
		"gates0": g0,
		"gates1": g1,
		"poisoned": bool(g1["i1"] or g1["neg_rate"] or int(g1["neg_after"]) > int(g0["neg_after"])),
		"canaries": canaries(),
	}
	_jdump("riv_37090.json", out)
	return out


def remaining_wr_compression() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

	scan = scan_wrong_rates(company=COMPANY, limit=5000)
	rows = scan.get("rows") or []
	by_v = {}
	for r in rows:
		by_v.setdefault(r.get("voucher"), r)
	roots = list(by_v.values())
	ready = [r for r in roots if r.get("eligible") and "READY" in str(r.get("planner_status") or "")]
	out = {
		"raw_rows": len(rows),
		"unique_vouchers": len(roots),
		"ready_vouchers": len(ready),
		"ready_by_source": dict(Counter((r.get("source_of_truth") or r.get("rate_source") or "?") for r in ready)),
		"ready_by_purpose": dict(Counter((r.get("purpose") or "?") for r in ready)),
		"ready_by_confidence": dict(Counter((r.get("confidence") or "?") for r in ready)),
		"ready_samples": [
			{
				"voucher": r.get("voucher"),
				"item": r.get("item"),
				"source": r.get("source_of_truth") or r.get("rate_source"),
				"purpose": r.get("purpose"),
				"confidence": r.get("confidence"),
				"expected": r.get("expected") or r.get("proposed_rate"),
			}
			for r in ready[:25]
		],
		"by_status": dict(Counter(r.get("planner_status") for r in roots)),
		"by_source": dict(Counter((r.get("source_of_truth") or r.get("rate_source") or "?") for r in roots)),
		"by_purpose": dict(Counter((r.get("purpose") or "?") for r in roots)),
		"by_confidence": dict(Counter((r.get("confidence") or "?") for r in roots)),
		"manual_samples": [
			{
				"voucher": r.get("voucher"),
				"item": r.get("item"),
				"source": r.get("source_of_truth") or r.get("rate_source"),
				"purpose": r.get("purpose"),
				"confidence": r.get("confidence"),
				"status": r.get("planner_status"),
				"message": (r.get("message") or "")[:160],
			}
			for r in roots
			if "MANUAL" in str(r.get("planner_status") or "") or str(r.get("confidence") or "") == "MANUAL"
		][:30],
	}
	_jdump("wr_compression.json", out)
	return out


def leftover_and_ready() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import scan_leftover_ma

	lma = scan_leftover_ma(company=COMPANY, limit=200)
	ready_wr = classify_all_ready_wr_sources()
	i4 = classify_i4_five()
	out = {
		"gates": _gates(),
		"leftover_ma": {
			"count": lma.get("count"),
			"ready": lma.get("ready_count"),
			"by_status": lma.get("by_status"),
			"rows": [
				{
					"item": r.get("item") or r.get("item_code"),
					"warehouse": r.get("warehouse"),
					"status": r.get("leftover_ma_status") or r.get("status"),
					"eligible": r.get("eligible"),
					"expected": r.get("expected_ma"),
				}
				for r in (lma.get("rows") or [])[:20]
			],
		},
		"ready_wr": ready_wr,
		"i4": i4,
	}
	_jdump("leftover_and_ready.json", out)
	return out


def verify_37090_idempotent() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_native import (
		reconstruct_manufacture_valuation,
		repair_manufacture_valuation,
	)
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import plan, execute
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

	ev = reconstruct_manufacture_valuation("MAT-STE-2026-37090")
	scan = scan_wrong_rates(company=COMPANY, voucher="MAT-STE-2026-37090", limit=20)
	row = next((r for r in (scan.get("rows") or []) if r.get("item") == "30100255"), None)
	p = plan(dict(row or {})) if row else None
	second = execute(p, dry_run=False) if p else None
	frappe.db.commit()
	fg = frappe.db.sql(
		"""SELECT basic_rate, valuation_rate, qty, amount FROM `tabStock Entry Detail`
		WHERE parent='MAT-STE-2026-37090' AND item_code='30100255'""",
		as_dict=True,
	)
	sle = frappe.db.sql(
		"""SELECT incoming_rate, valuation_rate, stock_value_difference, actual_qty
		FROM `tabStock Ledger Entry`
		WHERE voucher_no='MAT-STE-2026-37090' AND item_code='30100255' AND is_cancelled=0""",
		as_dict=True,
	)
	out = {
		"classification": ev.get("classification"),
		"eligible": ev.get("eligible"),
		"expected": ev.get("expected_target_rate"),
		"reason": ev.get("reason"),
		"plan_state": p.primary_state if p else None,
		"second": {
			"ok": (second or {}).get("ok"),
			"state": (second or {}).get("primary_state"),
			"status": (second or {}).get("status"),
			"message": (second or {}).get("message"),
			"economic_writes": (second or {}).get("economic_writes"),
		},
		"fg": fg,
		"sle": sle,
		"gates": _gates(),
	}
	_jdump("verify_37090.json", out)
	return out


def inspect_37090_lines() -> dict:
	details = frappe.db.sql(
		"""SELECT name, item_code, qty, basic_rate, valuation_rate, amount, basic_amount,
		          is_finished_item, is_scrap_item, s_warehouse, t_warehouse, additional_cost,
		          secondary_item_type, valuation_type, allow_zero_valuation_rate
		FROM `tabStock Entry Detail` WHERE parent='MAT-STE-2026-37090' ORDER BY idx""",
		as_dict=True,
	)
	sles = frappe.db.sql(
		"""SELECT name, item_code, warehouse, actual_qty, incoming_rate, outgoing_rate,
		          valuation_rate, stock_value_difference, stock_value, qty_after_transaction,
		          voucher_detail_no
		FROM `tabStock Ledger Entry`
		WHERE voucher_no='MAT-STE-2026-37090' AND is_cancelled=0
		ORDER BY item_code, actual_qty""",
		as_dict=True,
	)
	se = frappe.db.get_value(
		"Stock Entry",
		"MAT-STE-2026-37090",
		["purpose", "work_order", "job_card", "total_additional_costs", "fg_completed_qty"],
		as_dict=True,
	)
	out = {"se": se, "details": details, "sles": sles}
	_jdump("inspect_37090_lines.json", out)
	return out


def revert_37090_unproven_fg() -> dict:
	"""Restore ERPNext-native FG 4,204,211 after residual write failed RIV verify."""
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_native import (
		reconstruct_manufacture_valuation,
	)

	rate = 4204211.0
	qty = 1151.0
	amount = rate * qty
	frappe.db.set_value(
		"Stock Entry Detail",
		"odtrahmcam",
		{
			"basic_rate": rate,
			"valuation_rate": rate,
			"basic_amount": amount,
			"amount": amount,
		},
		update_modified=False,
	)
	frappe.db.set_value(
		"Stock Ledger Entry",
		"MAT-SLE-2026-262327",
		{
			"incoming_rate": rate,
			"valuation_rate": rate,
			"stock_value_difference": amount,
		},
		update_modified=False,
	)
	frappe.db.commit()
	ev = reconstruct_manufacture_valuation("MAT-STE-2026-37090")
	out = {
		"restored_rate": rate,
		"classification": ev.get("classification"),
		"eligible": ev.get("eligible"),
		"expected": ev.get("expected_target_rate"),
		"current": ev.get("current_target_rate"),
		"reason": ev.get("reason"),
		"gates": _gates(),
		"canaries": canaries(),
	}
	_jdump("revert_37090.json", out)
	return out


def riv_13100023_level2() -> dict:
	"""Item + Warehouse + bounded start (34019 posting date)."""
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import create_and_run_narrow_riv

	item = "13100023"
	wh = "انبار approved اقلام بسته بندی اولیه اسپاد"
	g0 = _gates()
	riv = create_and_run_narrow_riv(item, wh, posting_date="2026-08-01", posting_time="18:10:00")
	frappe.db.commit()
	g1 = _gates()
	out = {
		**riv,
		"gates0": g0,
		"gates1": g1,
		"poisoned": bool(g1["i1"] or g1["neg_rate"] or int(g1["neg_after"]) > int(g0["neg_after"])),
		"canaries": canaries(),
	}
	_jdump("riv_13100023_level2.json", out)
	return out
