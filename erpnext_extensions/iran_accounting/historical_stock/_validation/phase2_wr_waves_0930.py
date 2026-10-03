# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase-2 Development: Wrong Rate EXACT root compression + controlled waves."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from time import perf_counter

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.historical_stock._validation.phase2_dev_waves_0930 import (
	_gates,
	analyze_36934_residual,
)
from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_campaign import (
	inventory,
	prove_single_root,
)
from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine import READY_WRONG_RATE
from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.classifier import (
	classify_wrong_rate_row,
)

OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/"
	"private/files/hr_correction_20260930"
)
COMPANY = "اسپاد فارمد دارو"


def _dump(name: str, payload: dict) -> None:
	OUT.mkdir(parents=True, exist_ok=True)
	(OUT / name).write_text(
		json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
	)


def _canary_36934() -> dict:
	row = frappe.db.sql(
		"""
		SELECT qty, ROUND(basic_rate) br, custom_output_class, valuation_type
		FROM `tabStock Entry Detail`
		WHERE parent='MAT-STE-2026-36934' AND item_code='30500009'
		""",
		as_dict=True,
	)
	open_riv = frappe.db.count(
		"Repost Item Valuation", {"status": ("in", ["Queued", "In Progress"])}
	)
	bp = row[0] if row else {}
	return {
		"qty": flt(bp.get("qty")),
		"basic_rate": flt(bp.get("br")),
		"custom_output_class": bp.get("custom_output_class"),
		"open_riv": int(open_riv),
		"qty_ok": flt(bp.get("qty")) == 1,
		"class_ok": bp.get("custom_output_class") == "CO_PRODUCT",
		"nonzero": flt(bp.get("br")) > 0,
	}


def _enrich_root(row: dict) -> dict:
	"""Attach WO/JC + Iran output class + SLE rates for proof cards."""
	v = row.get("voucher")
	item = row.get("item")
	se = frappe.db.sql(
		"""
		SELECT name, work_order, job_card, purpose, posting_date, posting_time
		FROM `tabStock Entry` WHERE name=%s
		""",
		v,
		as_dict=True,
	)
	sed = frappe.db.sql(
		"""
		SELECT name, qty, basic_rate, valuation_rate, is_finished_item, is_scrap_item,
		       secondary_item_type, custom_output_class, valuation_type,
		       t_warehouse, s_warehouse, batch_no
		FROM `tabStock Entry Detail`
		WHERE parent=%s AND item_code=%s
		""",
		(v, item),
		as_dict=True,
	)
	sle = frappe.db.sql(
		"""
		SELECT name, actual_qty, incoming_rate, outgoing_rate, valuation_rate,
		       stock_value_difference, warehouse, batch_no
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND item_code=%s AND IFNULL(is_cancelled,0)=0
		""",
		(v, item),
		as_dict=True,
	)
	meta = se[0] if se else {}
	return {
		"voucher": v,
		"item": item,
		"warehouse": row.get("warehouse"),
		"batch": row.get("batch") or (sed[0].batch_no if sed else None),
		"purpose": row.get("purpose") or meta.get("purpose"),
		"work_order": meta.get("work_order"),
		"job_card": meta.get("job_card"),
		"posting_date": str(meta.get("posting_date") or ""),
		"posting_time": str(meta.get("posting_time") or ""),
		"current_rate": flt(row.get("current_value") or row.get("current_rate")),
		"expected_rate": flt(row.get("expected_value") or row.get("proposed_rate")),
		"expected_source": row.get("expected_source") or row.get("source_of_truth"),
		"rate_bucket": row.get("rate_bucket"),
		"confidence": row.get("confidence"),
		"sql_updates": int(row.get("sql_updates") or 0),
		"surface": row.get("surface"),
		"sed_rows": sed,
		"sle_rows": sle,
		"iran_classes": sorted(
			{
				str(d.get("custom_output_class") or d.get("secondary_item_type") or "PRIMARY")
				for d in sed
			}
		),
		"why_exact": (
			"EXACT reconstruction from authoritative historical economic evidence: "
			f"{row.get('expected_source') or row.get('source_of_truth')}; "
			"planner stamped READY_WRONG_RATE / confidence=EXACT; "
			"not nearest/future/ABS/average/downstream copy."
		),
		"downstream_scope": "item+warehouse identity replay via official RIV after surface write",
		"row": row,
	}


def _iran_safety_lane(voucher: str) -> dict:
	"""Gate WR EXACT apply against Iran output matrix (not generic scrap residual)."""
	if voucher == "MAT-STE-2026-36934":
		return {
			"lane": "CANARY_PRESERVE",
			"reason": "36934 stage-equivalent By-Product canary — do not overwrite with SVD residual",
		}
	rows = frappe.db.sql(
		"""
		SELECT item_code, IFNULL(is_finished_item,0) is_fg, IFNULL(is_scrap_item,0) is_scrap,
		       IFNULL(secondary_item_type,'') sec, IFNULL(valuation_type,'') vt,
		       IFNULL(custom_output_class,'') oclass
		FROM `tabStock Entry Detail` WHERE parent=%s
		""",
		voucher,
		as_dict=True,
	)
	fg_items = {r.item_code for r in rows if cint(r.is_fg)}
	copro = any(
		r.sec in ("By-Product", "Co-Product", "Additional Finished Good")
		or "CO_PRODUCT" in (r.oclass or "")
		for r in rows
	)
	reject = any(
		"REJECT" in (r.oclass or "")
		or (
			(cint(r.is_scrap) or r.sec == "Scrap")
			and r.vt == "Valuation Rate"
			and r.item_code in fg_items
		)
		for r in rows
	)
	vr_scrap = any(
		(cint(r.is_scrap) or r.sec == "Scrap") and r.vt == "Valuation Rate" for r in rows
	)
	fg_n = sum(1 for r in rows if cint(r.is_fg))
	if copro:
		return {"lane": "IRAN_STAGE_COPRODUCT", "reason": "co/by-product — stage-equivalent path required"}
	if reject:
		return {"lane": "IRAN_PRODUCT_REJECT", "reason": "product reject — absorbed FG rate path required"}
	if vr_scrap:
		return {
			"lane": "IRAN_VR_SCRAP_REVIEW",
			"reason": "Valuation Rate scrap present — not auto via generic scrap deduction",
		}
	if fg_n != 1:
		return {"lane": "NON_SINGLE_FG", "reason": f"fg_n={fg_n} — not single-FG manufacture residual"}
	return {"lane": "SAFE_SVD_RESIDUAL", "reason": "single FG; component scrap/no VR reject; SVD residual OK"}


def compress_wr_exact(*, limit: int = 3000) -> dict:
	"""Raw findings → identities → causal roots (self-PZ / earliest item+wh)."""
	t0 = perf_counter()
	inv = inventory(company=COMPANY, limit=limit)
	ready = list(inv.get("ready_rows") or [])
	# Prefer rows already stamped READY_WRONG_RATE by classifier inventory.
	ready = [r for r in ready if r.get("rate_status") == READY_WRONG_RATE or r.get("eligible")]

	identities = defaultdict(list)
	for r in ready:
		identities[(r.get("item"), r.get("warehouse"), r.get("batch") or "")].append(r)

	self_roots = []
	downstream = []
	for r in ready:
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		if pz_v and pz_v != r.get("voucher"):
			downstream.append(r)
		else:
			self_roots.append(r)

	# Causal roots: earliest (by sql size then voucher) unique item+warehouse among self-roots.
	seen = set()
	causal = []
	for r in sorted(
		self_roots,
		key=lambda x: (int(x.get("sql_updates") or 99), str(x.get("voucher") or "")),
	):
		key = (r.get("item"), r.get("warehouse"))
		if key in seen:
			continue
		seen.add(key)
		causal.append(r)

	enriched = []
	lane_counts: Counter = Counter()
	for r in causal:
		e = _enrich_root(r)
		safety = _iran_safety_lane(e["voucher"])
		e["iran_lane"] = safety["lane"]
		e["iran_lane_reason"] = safety["reason"]
		lane_counts[safety["lane"]] += 1
		enriched.append(e)

	# Apply order: SAFE_SVD_RESIDUAL first (smallest sql), then MTFM sabb, never canary/stage/reject.
	safe = [e for e in enriched if e["iran_lane"] == "SAFE_SVD_RESIDUAL"]
	mtfm = [
		e
		for e in enriched
		if e.get("expected_source") == "batch_inward_sabb_rate"
		and e["iran_lane"] in ("NON_SINGLE_FG", "SAFE_SVD_RESIDUAL")
	]
	apply_queue = safe + [e for e in mtfm if e not in safe]

	by_source = Counter(e["expected_source"] for e in enriched)
	by_purpose = Counter(e["purpose"] for e in enriched)
	by_bucket = Counter(e["rate_bucket"] for e in enriched)

	out = {
		"raw_ready_findings": len(ready),
		"unique_identities_item_wh_batch": len(identities),
		"unique_vouchers": len({r.get("voucher") for r in ready}),
		"self_pz_findings": len(self_roots),
		"downstream_dependent_findings": len(downstream),
		"causal_item_wh_roots": len(causal),
		"iran_lanes": dict(lane_counts),
		"safe_apply_queue_n": len(apply_queue),
		"by_expected_source": dict(by_source),
		"by_purpose": dict(by_purpose),
		"by_rate_bucket": dict(by_bucket),
		"inventory_by_rate_status": inv.get("by_rate_status"),
		"inventory_by_bucket": inv.get("by_bucket"),
		"roots": [
			{k: e[k] for k in e if k not in ("row", "sed_rows", "sle_rows")}
			| {
				"sed_n": len(e.get("sed_rows") or []),
				"sle_n": len(e.get("sle_rows") or []),
				"sed_summary": [
					{
						"qty": flt(d.qty),
						"br": flt(d.basic_rate),
						"class": d.custom_output_class,
						"sec": d.secondary_item_type,
						"vt": d.valuation_type,
						"fg": d.is_finished_item,
					}
					for d in (e.get("sed_rows") or [])
				],
			}
			for e in enriched
		],
		"elapsed": round(perf_counter() - t0, 3),
		"gates": _gates(),
		"canary_36934": _canary_36934(),
	}
	_dump("wr_exact_compression.json", out)
	# Persist SAFE apply queue for waves (include row payload).
	_dump(
		"wr_exact_causal_roots_full.json",
		{
			"roots": [{"proof": e, "apply_row": e["row"]} for e in apply_queue],
			"held": [
				{
					"voucher": e["voucher"],
					"item": e["item"],
					"iran_lane": e["iran_lane"],
					"reason": e["iran_lane_reason"],
				}
				for e in enriched
				if e["iran_lane"] != "SAFE_SVD_RESIDUAL"
				and e.get("expected_source") != "batch_inward_sabb_rate"
			],
		},
	)
	return {
		"raw_ready_findings": out["raw_ready_findings"],
		"unique_identities": out["unique_identities_item_wh_batch"],
		"unique_vouchers": out["unique_vouchers"],
		"causal_roots": out["causal_item_wh_roots"],
		"iran_lanes": out["iran_lanes"],
		"safe_apply_queue_n": out["safe_apply_queue_n"],
		"by_expected_source": out["by_expected_source"],
		"by_purpose": out["by_purpose"],
		"gates": out["gates"],
		"canary_36934": out["canary_36934"],
	}


def _load_causal_roots() -> list[dict]:
	path = OUT / "wr_exact_causal_roots_full.json"
	if not path.exists():
		compress_wr_exact()
	data = json.loads(path.read_text(encoding="utf-8"))
	return data.get("roots") or []


def debug_apply_root(voucher: str, *, dry_run: bool = False) -> dict:
	"""Apply one queued root by voucher and return SE before/after."""
	roots = _load_causal_roots()
	pack = next((p for p in roots if (p.get("apply_row") or {}).get("voucher") == voucher), None)
	if not pack:
		return {"ok": False, "reason": "voucher not in safe queue", "voucher": voucher}
	row = pack["apply_row"]
	item = row.get("item")
	before = frappe.db.sql(
		"""
		SELECT name, ROUND(basic_rate,6) br, ROUND(valuation_rate,6) vr, qty
		FROM `tabStock Entry Detail`
		WHERE parent=%s AND item_code=%s
		""",
		(voucher, item),
		as_dict=True,
	)
	res = prove_single_root(row, dry_run=dry_run)
	after = frappe.db.sql(
		"""
		SELECT name, ROUND(basic_rate,6) br, ROUND(valuation_rate,6) vr, qty
		FROM `tabStock Entry Detail`
		WHERE parent=%s AND item_code=%s
		""",
		(voucher, item),
		as_dict=True,
	)
	out = {
		"voucher": voucher,
		"item": item,
		"expected": row.get("proposed_rate") or row.get("expected_value"),
		"before": before,
		"after": after,
		"result": res,
	}
	_dump(f"wr_debug_{voucher}.json", out)
	return {
		"ok": res.get("ok"),
		"stage": res.get("stage"),
		"expected": out["expected"],
		"before_br": [b.br for b in before],
		"after_br": [a.br for a in after],
		"idempotent": res.get("idempotent"),
		"second": res.get("second"),
		"reason": (res.get("applied") or res.get("dry") or {}).get("reason")
		if not res.get("ok")
		else None,
	}


def _residual_still_live(row: dict) -> bool:
	"""True only when SE FG basic_rate still differs from planned expected (>1 IRR)."""
	voucher = row.get("voucher")
	item = row.get("item")
	expected = flt(row.get("proposed_rate") or row.get("expected_value") or row.get("expected"))
	if not voucher or not item or abs(expected) <= 1e-9:
		return False
	cur = frappe.db.sql(
		"""
		SELECT basic_rate FROM `tabStock Entry Detail`
		WHERE parent=%s AND item_code=%s AND IFNULL(is_finished_item,0)=1
		LIMIT 1
		""",
		(voucher, item),
	)
	if not cur:
		# Non-FG / MTFM — compare any detail for item
		cur = frappe.db.sql(
			"""
			SELECT basic_rate FROM `tabStock Entry Detail`
			WHERE parent=%s AND item_code=%s LIMIT 1
			""",
			(voucher, item),
		)
	if not cur:
		return False
	return abs(flt(cur[0][0]) - abs(expected)) > 1.0


def wr_wave(n: int = 1, *, dry_run: bool = True, offset: int = 0) -> dict:
	"""Apply next n causal WR EXACT roots. Root-based wave size."""
	t0 = perf_counter()
	roots = _load_causal_roots()
	# Skip already-cleared: require live SE residual vs planned expected.
	live = []
	for pack in roots:
		row = pack.get("apply_row") or {}
		classified = classify_wrong_rate_row(row)
		row = {
			**row,
			**{
				k: classified.get(k)
				for k in ("proposed_rate", "expected_value", "rate_status", "eligible")
			},
		}
		row["proposed_rate"] = flt(
			classified.get("expected_value")
			or classified.get("proposed_rate")
			or row.get("proposed_rate")
		)
		if not _residual_still_live(row):
			continue
		if classified.get("rate_status") == READY_WRONG_RATE or (
			classified.get("eligible") and classified.get("confidence") == "EXACT"
		):
			live.append({"proof": pack.get("proof"), "apply_row": row})

	target = live[int(offset) : int(offset) + int(n)]
	gates0 = _gates()
	canary0 = _canary_36934()
	results = []
	regressed = False
	for pack in target:
		row = pack["apply_row"]
		res = prove_single_root(row, dry_run=dry_run)
		results.append(
			{
				"voucher": row.get("voucher"),
				"item": row.get("item"),
				"warehouse": row.get("warehouse"),
				"expected_source": row.get("expected_source") or row.get("source"),
				"expected": row.get("proposed_rate") or row.get("expected_value"),
				"result": res,
			}
		)
		if not res.get("ok"):
			break

	# Settle open RIV briefly if any
	if not dry_run:
		_wait_riv(timeout_s=120)

	gates1 = _gates()
	canary1 = _canary_36934()
	regressed = (
		gates1["i1"] > 0
		or gates1["neg_stock"] > 0
		or gates1["broken_bin"] > gates0["broken_bin"]
		or not canary1.get("qty_ok")
		or not canary1.get("class_ok")
		or not canary1.get("nonzero")
	)
	# open_riv should settle; if still open after wait, flag
	if not dry_run and gates1["open_riv"] > 0:
		regressed = True

	out = {
		"dry_run": dry_run,
		"wave_n": n,
		"offset": offset,
		"live_ready_roots": len(live),
		"targeted": len(target),
		"ok_count": sum(1 for r in results if (r.get("result") or {}).get("ok")),
		"fail_count": sum(1 for r in results if not (r.get("result") or {}).get("ok")),
		"results": results,
		"gates_before": gates0,
		"gates_after": gates1,
		"canary_before": canary0,
		"canary_after": canary1,
		"regressed": regressed,
		"elapsed": round(perf_counter() - t0, 3),
	}
	_dump(f"wr_wave_{n}_{'dry' if dry_run else 'apply'}_off{offset}.json", out)
	return {
		k: out[k]
		for k in (
			"dry_run",
			"wave_n",
			"offset",
			"live_ready_roots",
			"targeted",
			"ok_count",
			"fail_count",
			"regressed",
			"gates_before",
			"gates_after",
			"canary_after",
			"elapsed",
		)
	} | {
		"first_fail": next(
			(r for r in results if not (r.get("result") or {}).get("ok")), None
		),
		"vouchers": [r.get("voucher") for r in results],
	}


def _wait_riv(timeout_s: int = 120) -> None:
	import time

	deadline = time.time() + timeout_s
	while time.time() < deadline:
		n = frappe.db.count(
			"Repost Item Valuation", {"status": ("in", ["Queued", "In Progress"])}
		)
		if not n:
			return
		frappe.db.commit()
		time.sleep(2)


def rebucket_wr_manual(*, limit: int = 3000) -> dict:
	"""Classify MANUAL / LIKELY / AMBIGUOUS into first-class lanes."""
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

	scan = scan_wrong_rates(company=COMPANY, limit=limit)
	rows = scan.get("rows") or []
	lanes: Counter = Counter()
	detail = []
	for r in rows:
		status = str(r.get("status") or "")
		conf = str(r.get("confidence") or "")
		ps = str(r.get("planner_status") or "")
		reason = str(r.get("reason") or r.get("message") or "")
		if status in ("NO_ACTION_REQUIRED", "RATE_REBUILD_COMPLETE") or r.get("kpi_bucket") == "complete":
			lane, reason_code = "LEGITIMATE", "NO_ACTION"
		elif status == "RECONSTRUCTABLE" or ps.startswith("READY") or (
			r.get("eligible") and conf == "EXACT"
		):
			lane, reason_code = "AUTO_REPAIRABLE", "EXACT_READY"
		elif status == "DEPENDENCY_REPAIR_REQUIRED" or "WAITING" in ps or "WAITING" in status:
			lane, reason_code = "WAITING_UPSTREAM", "WAITING_UPSTREAM"
		elif conf == "LIKELY":
			lane, reason_code = "TECHNICAL_TOOL_GAP", "UNPROMOTED_LIKELY_REQUIRES_EXACT_PROMOTION"
		elif conf == "AMBIGUOUS" or "AMBIGUOUS" in reason or "sources disagree" in reason:
			lane, reason_code = "TECHNICAL_TOOL_GAP", "AMBIGUOUS_RECONSTRUCTION_SOURCES"
		elif "PRECISION_DUST" in reason:
			lane, reason_code = "LEGITIMATE", "PRECISION_DUST"
		elif conf == "MANUAL" or status == "MANUAL_REVIEW":
			lane, reason_code = "MANUAL_BUSINESS_EVIDENCE_REQUIRED", "MANUAL_REVIEW"
		else:
			lane, reason_code = "TECHNICAL_TOOL_GAP", "UNCLASSIFIED_SCANNER_LIMIT"
		lanes[lane] += 1
		if lane in ("TECHNICAL_TOOL_GAP", "MANUAL_BUSINESS_EVIDENCE_REQUIRED", "WAITING_UPSTREAM"):
			detail.append(
				{
					"voucher": r.get("voucher"),
					"item": r.get("item"),
					"lane": lane,
					"reason_code": reason_code,
					"confidence": conf,
					"status": status,
					"planner_status": ps,
					"reason": reason[:160],
				}
			)

	tool_gap_reasons = Counter(d["reason_code"] for d in detail if d["lane"] == "TECHNICAL_TOOL_GAP")
	out = {
		"total_rows": len(rows),
		"by_lane": dict(lanes),
		"tool_gap_reason_codes": dict(tool_gap_reasons),
		"manual_business_n": lanes.get("MANUAL_BUSINESS_EVIDENCE_REQUIRED", 0),
		"technical_tool_gap_n": lanes.get("TECHNICAL_TOOL_GAP", 0),
		"auto_n": lanes.get("AUTO_REPAIRABLE", 0),
		"waiting_n": lanes.get("WAITING_UPSTREAM", 0),
		"legitimate_n": lanes.get("LEGITIMATE", 0),
		"sample_manual": [d for d in detail if d["lane"] == "MANUAL_BUSINESS_EVIDENCE_REQUIRED"][:30],
		"sample_tool_gap": [d for d in detail if d["lane"] == "TECHNICAL_TOOL_GAP"][:30],
		"by_kpi_bucket": scan.get("by_kpi_bucket"),
	}
	_dump("wr_rebucket_v5338.json", out)
	return {k: out[k] for k in out if k not in ("sample_manual", "sample_tool_gap")} | {
		"sample_manual_n": len(out["sample_manual"]),
		"sample_tool_gap_n": len(out["sample_tool_gap"]),
	}
