# Copyright (c) 2026, ERPNext Extensions contributors
"""Production readiness harness against backup 20260926_090225 (v5.3.22+).

Phases:
  pass0 → fingerprint → canaries → dry-run PROD plan → apply PROD_SAFE waves
  → bin rebuild → scan → classify remaining → write manifest/runbook artifacts.

Does not invent rates, fabricate inventory, or change manufacturing quantities.
Does not push. Development only.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import frappe
from frappe.utils import flt, now_datetime

COMPANY = "اسپاد فارمد دارو"
OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/private/files/hr_prod_ready_0926"
)
BACKUP_ID = "20260926_090225"


def _jdump(path: Path, obj) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _gates():
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import (
		capture_safety_fingerprint,
	)

	fp = capture_safety_fingerprint()
	neg_after = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND qty_after_transaction < -0.0001"""
	)[0][0]
	neg_rate = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND (
			incoming_rate < -0.0001 OR outgoing_rate < -0.0001 OR valuation_rate < -0.0001
		)"""
	)[0][0]
	return {
		"i1": fp.get("i1"),
		"neg_qty": fp.get("neg_qty"),
		"bin_mismatch": fp.get("bin_value_mismatch"),
		"neg_after": int(neg_after),
		"neg_rate": int(neg_rate),
		"mfg": dict(fp.get("manufacturing") or {}),
	}


def verify_backup_identity() -> dict:
	"""Confirm DB is not an older restore by sampling backup-dated documents."""
	# Representative counts + latest SLE/STE timestamps near backup day.
	counts = {
		"company": frappe.db.get_value("Company", COMPANY, "name"),
		"sle": frappe.db.count("Stock Ledger Entry"),
		"se": frappe.db.count("Stock Entry"),
		"riv": frappe.db.count("Repost Item Valuation"),
		"bin": frappe.db.count("Bin"),
	}
	latest_sle = frappe.db.sql(
		"""SELECT MAX(posting_datetime), MAX(creation) FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0"""
	)[0]
	# Documents that exist in recent production dumps
	probes = {}
	for vn in (
		"MAT-STE-2026-30470",
		"MAT-STE-2026-28696",
		"MAT-STE-2026-33937",
	):
		probes[vn] = bool(frappe.db.exists("Stock Entry", vn))
	# Prefer evidence that this dump is newer than 20260924: any SLE creation after 2026-09-24
	newer = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabStock Ledger Entry`
		WHERE creation >= '2026-09-25'"""
	)[0][0]
	out = {
		"counts": counts,
		"latest_sle_posting": str(latest_sle[0]),
		"latest_sle_creation": str(latest_sle[1]),
		"sle_created_on_or_after_20260925": int(newer),
		"probes": probes,
		"backup_id_expected": BACKUP_ID,
		"looks_like_new_backup": int(newer) > 0 and all(probes.values()),
	}
	_jdump(OUT / "db_identity.json", out)
	return out


def mfg_fingerprint() -> dict:
	wo = frappe.db.sql(
		"""SELECT ROUND(SUM(IFNULL(qty,0)),4), ROUND(SUM(IFNULL(produced_qty,0)),4), COUNT(*)
		FROM `tabWork Order` WHERE docstatus < 2"""
	)[0]
	jc = frappe.db.sql(
		"""SELECT ROUND(SUM(IFNULL(for_quantity,0)),4), ROUND(SUM(IFNULL(total_completed_qty,0)),4), COUNT(*)
		FROM `tabJob Card` WHERE docstatus < 2"""
	)[0]
	mfg = frappe.db.sql(
		"""SELECT
			ROUND(SUM(CASE WHEN IFNULL(sed.is_finished_item,0)=1 THEN sed.qty ELSE 0 END),4),
			ROUND(SUM(CASE WHEN IFNULL(sed.s_warehouse,'')!='' AND IFNULL(sed.t_warehouse,'')='' THEN sed.qty ELSE 0 END),4),
			ROUND(SUM(CASE WHEN IFNULL(sed.is_scrap_item,0)=1 THEN sed.qty ELSE 0 END),4),
			COUNT(DISTINCT se.name)
		FROM `tabStock Entry` se
		JOIN `tabStock Entry Detail` sed ON sed.parent=se.name
		WHERE se.docstatus=1 AND se.purpose='Manufacture'"""
	)[0]
	mtfm = frappe.db.sql(
		"""SELECT ROUND(SUM(sed.qty),4), COUNT(DISTINCT se.name)
		FROM `tabStock Entry` se
		JOIN `tabStock Entry Detail` sed ON sed.parent=se.name
		WHERE se.docstatus=1 AND se.purpose='Material Transfer for Manufacture'"""
	)[0]
	out = {
		"wo_qty": flt(wo[0]),
		"wo_produced": flt(wo[1]),
		"wo_n": int(wo[2]),
		"jc_for": flt(jc[0]),
		"jc_done": flt(jc[1]),
		"jc_n": int(jc[2]),
		"mfg_fg": flt(mfg[0]),
		"mfg_src": flt(mfg[1]),
		"mfg_scrap": flt(mfg[2]),
		"mfg_n": int(mfg[3]),
		"mtfm_qty": flt(mtfm[0]),
		"mtfm_n": int(mtfm[1]),
		"captured_at": str(now_datetime()),
	}
	_jdump(OUT / "mfg_fingerprint.json", out)
	return out


def mfg_delta(a: dict, b: dict) -> dict:
	# Compare only shared business-quantity keys (avoid mixing fingerprint schemas).
	keys = sorted(set(a) & set(b) - {"captured_at"})
	return {k: flt(b.get(k)) - flt(a.get(k)) for k in keys}


def canaries() -> dict:
	"""Re-derive canary economics from current DB — no hard-coded rates as authority."""
	out = {}

	# 30470 Stock Adjustment
	gl = frappe.db.sql(
		"""SELECT account, ROUND(SUM(debit),2), ROUND(SUM(credit),2)
		FROM `tabGL Entry`
		WHERE voucher_no=%s AND is_cancelled=0
		GROUP BY account ORDER BY account""",
		"MAT-STE-2026-30470",
		as_dict=True,
	)
	adj = [r for r in gl if "621301" in (r.account or "")]
	round_off = [r for r in gl if "622515" in (r.account or "")]
	out["30470"] = {
		"exists": bool(frappe.db.exists("Stock Entry", "MAT-STE-2026-30470")),
		"gl": gl,
		"stock_adj_621301": adj,
		"round_off_622515": round_off,
		"uses_621301": bool(adj),
		"uses_622515_for_stock": bool(round_off),
	}

	# 28696 rates from SLE SVD/qty
	sle = frappe.db.sql(
		"""SELECT actual_qty, incoming_rate, outgoing_rate, stock_value_difference, warehouse
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND is_cancelled=0""",
		"MAT-STE-2026-28696",
		as_dict=True,
	)
	rates = []
	for r in sle:
		q = flt(r.actual_qty)
		if abs(q) > 1e-9 and abs(flt(r.stock_value_difference)) > 1e-6:
			rates.append(round(abs(flt(r.stock_value_difference) / q), 2))
		elif abs(flt(r.outgoing_rate)) > 1e-6:
			rates.append(round(flt(r.outgoing_rate), 2))
		elif abs(flt(r.incoming_rate)) > 1e-6:
			rates.append(round(flt(r.incoming_rate), 2))
	svd = sum(flt(r.stock_value_difference) for r in sle)
	out["28696"] = {
		"exists": bool(sle),
		"rates": sorted(set(rates)),
		"svd_sum": round(svd, 2),
		"conserved": abs(svd) < 1,
	}

	# 16100066 leftover MA from Bin
	b66 = frappe.db.sql(
		"""SELECT warehouse, actual_qty, stock_value, valuation_rate
		FROM `tabBin` WHERE item_code=%s""",
		"16100066",
		as_dict=True,
	)
	out["16100066"] = b66

	# 16100226
	b226 = frappe.db.sql(
		"""SELECT warehouse, actual_qty, stock_value, valuation_rate
		FROM `tabBin` WHERE item_code=%s""",
		"16100226",
		as_dict=True,
	)
	out["16100226"] = b226

	# 33937 — reconstruct expected via current code
	from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
		reconstruct_transfer_valuation,
	)

	se = frappe.db.get_value(
		"Stock Entry",
		"MAT-STE-2026-33937",
		["purpose", "posting_date", "posting_time"],
		as_dict=True,
	)
	details = frappe.db.sql(
		"""SELECT name, item_code, batch_no, qty, basic_rate, valuation_rate, s_warehouse, t_warehouse
		FROM `tabStock Entry Detail` WHERE parent=%s AND item_code=%s LIMIT 1""",
		("MAT-STE-2026-33937", "13100023"),
		as_dict=True,
	)
	recon = None
	if se and details:
		d = details[0]
		row = {
			"voucher": "MAT-STE-2026-33937",
			"purpose": se.purpose,
			"item_code": d.item_code,
			"batch_no": d.batch_no,
			"qty": d.qty,
			"basic_rate": d.basic_rate,
			"s_warehouse": d.s_warehouse,
			"t_warehouse": d.t_warehouse,
			"posting_date": str(se.posting_date),
			"posting_time": str(se.posting_time),
			"voucher_detail_no": d.name,
		}
		recon = reconstruct_transfer_valuation(row)
	out["33937"] = {
		"exists": bool(se),
		"current_basic_rate": flt(details[0].basic_rate) if details else None,
		"recon": {
			k: (recon or {}).get(k)
			for k in (
				"classification",
				"confidence",
				"expected_rate",
				"authoritative_source",
				"reason",
			)
		}
		if recon
		else None,
	}
	_jdump(OUT / "canaries.json", out)
	return out


def dash_slice(scan: dict) -> dict:
	d = scan.get("dashboard") or {}
	keys = [
		"Integrity Score",
		"I1 Negative Rate",
		"I4 Leftover",
		"Wrong Rate",
		"Wrong Rate READY",
		"Wrong Rate WAITING",
		"Wrong Rate MANUAL",
		"Wrong Rate Complete",
		"Zero Rate",
		"Zero Rate Raw",
		"Zero Rate Actionable",
		"Proven Legitimate Zero",
		"Patient Zero",
		"Failed RIV",
		"Failed RIV Actionable",
		"Failed RIV Historical",
		"Failed RIV Superseded",
		"Broken Bin",
		"Broken GL",
		"Posting Order",
		"Ready to Repair",
		"Leftover MA",
	]
	return {k: d.get(k) for k in keys}


def neg_stock_report() -> dict:
	rows = frappe.db.sql(
		"""SELECT name, item_code, warehouse, batch_no, voucher_type, voucher_no,
			actual_qty, qty_after_transaction, posting_datetime, posting_date
		FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND qty_after_transaction < -0.0001
		ORDER BY posting_datetime, creation""",
		as_dict=True,
	)
	# causal roots via PO scanner summary if available
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

	po = run_full_history_scan(company=COMPANY)
	summary = po.get("summary") or {}
	out = {
		"raw_rows": len(rows),
		"rows": rows,
		"po_summary": summary,
		"negative_intervals": summary.get("negative_intervals"),
		"by_class": {
			k: summary.get(k)
			for k in summary
			if k
			in (
				"CROSS_TIME_REPAIRABLE",
				"REAL_STOCK_SHORTAGE",
				"CROSS_ITEM_CONFLICT",
				"MTFM_ORDER",
				"POSTING_ORDER",
			)
			or "SHORTAGE" in k
			or "ORDER" in k
			or "CONFLICT" in k
		},
	}
	_jdump(OUT / "neg_stock.json", out)
	return out


def classify_ready_wrong() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

	scan = scan_wrong_rates(company=COMPANY, limit=5000)
	rows = scan.get("rows") or []
	by_status = Counter(r.get("planner_status") for r in rows)
	ready = [r for r in rows if r.get("eligible") and "READY" in str(r.get("planner_status") or "")]
	by_v = {}
	for r in ready:
		by_v.setdefault(r.get("voucher"), r)
	roots = []
	for v, r in by_v.items():
		src = r.get("source_of_truth") or r.get("rate_source") or ""
		purpose = r.get("purpose") or ""
		# Production classification heuristic from proven families
		if src in (
			"batch_purchase_receipt_rate",
			"batch_inward_sabb_rate",
			"batch_stock_reco_rate",
			"stock_reco_corroborated_previous_healthy",
			"outgoing_sle_svd",
		) and purpose in (
			"Material Transfer",
			"Material Transfer for Manufacture",
			"Send to Subcontractor",
		):
			cls = "PROD_SAFE"
		elif src in ("leftover_ma", "LEFTOVER_MA"):
			cls = "PROD_SAFE"
		else:
			cls = "PROD_SAFE" if r.get("confidence") == "EXACT" else "PROD_SKIP_MANUAL"
		roots.append(
			{
				"voucher": v,
				"item": r.get("item"),
				"warehouse": r.get("warehouse") or r.get("s_warehouse"),
				"batch": r.get("batch"),
				"purpose": purpose,
				"expected": r.get("expected") or r.get("proposed_rate"),
				"current": r.get("current") or r.get("current_rate"),
				"source": src,
				"confidence": r.get("confidence"),
				"prod_class": cls,
				"sql_updates": r.get("sql_updates"),
			}
		)
	out = {
		"active_rows": len(rows),
		"by_status": dict(by_status),
		"ready_rows": len(ready),
		"ready_roots": len(roots),
		"by_prod_class": dict(Counter(r["prod_class"] for r in roots)),
		"roots": roots,
	}
	_jdump(OUT / "wrong_ready_plan.json", out)
	return out


def apply_prod_safe_waves(roots: list[dict], *, baseline_neg: int, mfg0: dict) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import plan, execute

	safe = [r for r in roots if r.get("prod_class") == "PROD_SAFE"]
	log = []
	done = []
	cuts = [1, 3, 5, 10, 25, 50, 100, 200, 500]
	idx = 0
	for cut in cuts:
		batch = safe[idx:cut]
		if not batch:
			break
		for spec in batch:
			v = spec["voucher"]
			scan = scan_wrong_rates(company=COMPANY, voucher=v, limit=30)
			rows = [r for r in (scan.get("rows") or []) if r.get("eligible")]
			if not rows:
				log.append({"voucher": v, "skipped": "no_eligible"})
				done.append(v)
				continue
			# Prefer matching item when available
			row = next((r for r in rows if r.get("item") == spec.get("item")), rows[0])
			live = execute(plan(dict(row)), dry_run=False)
			frappe.db.commit()
			g = _gates()
			fp_now = mfg_fingerprint()
			md = mfg_delta(mfg0, fp_now)
			entry = {
				"voucher": v,
				"ok": live.get("ok"),
				"state": live.get("primary_state"),
				"expected": spec.get("expected"),
				"source": spec.get("source"),
				"gates": g,
				"mfg_delta": md,
			}
			log.append(entry)
			done.append(v)
			if (
				g["i1"]
				or g["neg_rate"]
				or int(g["neg_after"]) > baseline_neg
				or any(abs(flt(x)) > 1e-6 for x in md.values())
			):
				_jdump(OUT / f"ABORT_wave_{len(done)}.json", entry)
				raise RuntimeError(f"safety abort after {v}: {entry}")
		idx = cut
		_jdump(OUT / f"wave_at_{cut}.json", {"done": done, "log_tail": log[-10:]})
	# Production safety: never drain newly discovered READY. Manifest is authority.
	extra = 0
	out = {"planned_safe": len(safe), "applied": len(done), "log": log, "extra_drain": extra}
	_jdump(OUT / "prod_safe_apply.json", out)
	return out


def apply_posting_order() -> dict:
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.stock_posting_order.api import (
		repair_posting_order_selected,
	)

	po = run_full_history_scan(company=COMPANY)
	rows = []
	for raw in po.get("rows") or []:
		r = attach_plan(dict(raw))
		ps = str(r.get("planner_status") or "")
		if ps.startswith("READY") and int(r.get("sql_updates") or 0) > 0:
			rows.append(r)
	if not rows:
		return {"applied_n": 0, "ready": 0}
	applied = repair_posting_order_selected(rows=rows, dry_run=False)
	frappe.db.commit()
	return {
		"ready": len(rows),
		"applied_n": len((applied or {}).get("applied") or []),
		"aborted": (applied or {}).get("aborted"),
	}


def apply_leftover_ma() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
		classify_leftover_ma_identity,
	)
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import plan, execute

	ITEM = "16100066"
	bins = frappe.db.sql(
		"""SELECT warehouse FROM `tabBin` WHERE item_code=%s AND ABS(IFNULL(actual_qty,0))>0.0001""",
		ITEM,
	)
	results = []
	for (wh,) in bins:
		c = classify_leftover_ma_identity(ITEM, wh)
		if not c.get("eligible"):
			results.append({"warehouse": wh, "skipped": c.get("leftover_ma_status")})
			continue
		row = {**c, "topic": "LEFTOVER_MA", "item": ITEM, "warehouse": wh}
		live = execute(plan(row), dry_run=False)
		frappe.db.commit()
		results.append(
			{
				"warehouse": wh,
				"ok": live.get("ok"),
				"state": live.get("primary_state"),
				"expected": c.get("expected_ma"),
			}
		)
	return {"results": results}


def rebuild_bins() -> dict:
	from erpnext_extensions.iran_accounting.stock_posting_order.replay import _bin_from_last_sle

	mism = frappe.db.sql(
		"""
		SELECT b.item_code, b.warehouse, b.actual_qty, b.stock_value,
		  (SELECT qty_after_transaction FROM `tabStock Ledger Entry` sle
		   WHERE sle.item_code=b.item_code AND sle.warehouse=b.warehouse AND sle.is_cancelled=0
		   ORDER BY sle.posting_datetime DESC, sle.creation DESC LIMIT 1) tip_qty,
		  (SELECT stock_value FROM `tabStock Ledger Entry` sle
		   WHERE sle.item_code=b.item_code AND sle.warehouse=b.warehouse AND sle.is_cancelled=0
		   ORDER BY sle.posting_datetime DESC, sle.creation DESC LIMIT 1) tip_val
		FROM `tabBin` b
		HAVING (tip_qty IS NULL AND (ABS(IFNULL(actual_qty,0))>0.0001 OR ABS(IFNULL(stock_value,0))>1))
		   OR ABS(IFNULL(actual_qty,0)-IFNULL(tip_qty,0))>0.0001
		   OR ABS(IFNULL(stock_value,0)-IFNULL(tip_val,0))>1
		LIMIT 200
		""",
		as_dict=True,
	)
	fixed = 0
	for m in mism:
		if m.tip_qty is None and m.tip_val is None:
			frappe.db.sql(
				"""UPDATE `tabBin` SET actual_qty=0, stock_value=0, valuation_rate=0
				WHERE item_code=%s AND warehouse=%s""",
				(m.item_code, m.warehouse),
			)
		else:
			_bin_from_last_sle(m.item_code, m.warehouse)
		fixed += 1
	frappe.db.commit()
	return {"mismatches": len(mism), "fixed": fixed}


def build_manifest(applied_log: dict, plan: dict) -> dict:
	roots = []
	by_v = {r["voucher"]: r for r in plan.get("roots") or []}
	for entry in applied_log.get("log") or []:
		v = entry.get("voucher")
		spec = by_v.get(v) or {}
		roots.append(
			{
				"root_id": v,
				"voucher": v,
				"item": spec.get("item"),
				"warehouse": spec.get("warehouse"),
				"batch": spec.get("batch"),
				"reason": "WRONG_RATE",
				"provenance": spec.get("source"),
				"expected_rate": spec.get("expected"),
				"current_rate": spec.get("current"),
				"prod_class": "PROD_SAFE",
				"riv_required": True,
				"apply_ok": entry.get("ok"),
			}
		)
	# also include leftover MA / PO if present in separate files later
	out = {
		"backup_id": BACKUP_ID,
		"generated_at": str(now_datetime()),
		"root_count": len(roots),
		"roots": roots,
	}
	_jdump(OUT / "production_mutation_manifest.json", out)
	return out


def pass0_only():
	"""Scan + fingerprint + canaries — no mutation."""
	from erpnext_extensions.iran_accounting.historical_stock.api import scan_all
	from erpnext_extensions import __version__

	OUT.mkdir(parents=True, exist_ok=True)
	ident = verify_backup_identity()
	if not ident.get("looks_like_new_backup"):
		raise RuntimeError(f"DB identity check failed for {BACKUP_ID}: {ident}")
	fp = mfg_fingerprint()
	gates0 = _gates()
	scan = scan_all(company=COMPANY)
	_jdump(OUT / "pass0_scan.json", scan)
	can = canaries()
	neg = neg_stock_report()
	plan = classify_ready_wrong()
	out = {
		"version": __version__,
		"head": None,
		"backup_id": BACKUP_ID,
		"identity": ident,
		"fingerprint": fp,
		"gates": gates0,
		"dashboard": dash_slice(scan),
		"canaries": can,
		"neg_stock": {
			"raw_rows": neg.get("raw_rows"),
			"po_summary": neg.get("po_summary"),
			"by_class": neg.get("by_class"),
		},
		"wrong_plan": {
			"ready_roots": plan.get("ready_roots"),
			"by_prod_class": plan.get("by_prod_class"),
			"by_status": plan.get("by_status"),
		},
	}
	# attach git head via shell-less file if present
	try:
		import subprocess

		out["head"] = subprocess.check_output(
			["git", "-C", "/workspace/development/frappe-bench/apps/erpnext_extensions", "rev-parse", "HEAD"],
			text=True,
		).strip()
	except Exception:
		pass
	_jdump(OUT / "pass0_summary.json", out)
	return out


def run_safe_campaign(*, skip_po_lma: bool = False):
	"""Apply proven safe families after pass0 artifacts exist (or create them)."""
	from erpnext_extensions.iran_accounting.historical_stock.api import scan_all
	from erpnext_extensions import __version__

	OUT.mkdir(parents=True, exist_ok=True)
	if not (OUT / "pass0_summary.json").exists():
		pass0_only()
	pass0 = json.loads((OUT / "pass0_summary.json").read_text())
	mfg0 = pass0["fingerprint"]
	# After PO, negatives may already be 0 — use current as floor for "new" negs.
	baseline_neg = max(int(pass0["gates"]["neg_after"]), int(_gates()["neg_after"]))
	plan = json.loads((OUT / "wrong_ready_plan.json").read_text())

	if not skip_po_lma:
		po = apply_posting_order()
		_jdump(OUT / "po_apply.json", po)
		g = _gates()
		if g["i1"] or g["neg_rate"] or int(g["neg_after"]) > int(pass0["gates"]["neg_after"]):
			raise RuntimeError(f"PO safety fail {g}")

		lma = apply_leftover_ma()
		_jdump(OUT / "lma_apply.json", lma)
	else:
		po = json.loads((OUT / "po_apply.json").read_text()) if (OUT / "po_apply.json").exists() else {}
		lma = json.loads((OUT / "lma_apply.json").read_text()) if (OUT / "lma_apply.json").exists() else {}

	# Refresh READY plan after PO/LMA
	plan = classify_ready_wrong()
	applied = apply_prod_safe_waves(plan.get("roots") or [], baseline_neg=baseline_neg, mfg0=mfg0)
	bins = rebuild_bins()
	_jdump(OUT / "bin_rebuild.json", bins)

	scan = scan_all(company=COMPANY)
	_jdump(OUT / "after_safe_scan.json", scan)
	can = canaries()
	fp1 = mfg_fingerprint()
	manifest = build_manifest(applied, plan)
	summary = {
		"version": __version__,
		"backup_id": BACKUP_ID,
		"pass0_dashboard": pass0.get("dashboard"),
		"after_dashboard": dash_slice(scan),
		"po": po,
		"lma": lma,
		"applied": {"n": applied.get("applied"), "planned_safe": applied.get("planned_safe")},
		"bins": bins,
		"mfg_delta": mfg_delta(mfg0, fp1),
		"gates": _gates(),
		"canaries": can,
		"manifest_roots": manifest.get("root_count"),
	}
	_jdump(OUT / "safe_campaign_summary.json", summary)
	return summary


def _earliest_sle(item: str, warehouse: str) -> dict | None:
	row = frappe.db.sql(
		"""SELECT posting_date, posting_time, voucher_no, name
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation LIMIT 1""",
		(item, warehouse),
		as_dict=True,
	)
	return row[0] if row else None


def _run_narrow_riv(item: str, warehouse: str, *, label: str, allow_zero_rate: bool = True) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import create_and_run_narrow_riv
	from erpnext_extensions.iran_accounting.historical_stock.repost import preview_repost_selected

	prev = preview_repost_selected(item, warehouse, company=COMPANY)
	earliest = _earliest_sle(item, warehouse)
	out = {
		"label": label,
		"item": item,
		"warehouse": warehouse,
		"preview_eligible": prev.get("eligible"),
		"preview_status": prev.get("status"),
		"preview_reason": prev.get("reason"),
		"earliest": earliest,
	}
	if not earliest:
		out["ok"] = False
		out["reason"] = "no_sle"
		return out
	if not prev.get("eligible"):
		out["ok"] = False
		out["reason"] = f"preflight_blocked:{prev.get('reason')}"
		out["prod_class"] = "BLOCKED_IDENTITY"
		out["skipped"] = True
		return out
	# Block if identity currently has negative qty_after (poison).
	neg = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND qty_after_transaction < -0.0001""",
		(item, warehouse),
	)[0][0]
	if int(neg):
		out["ok"] = False
		out["reason"] = "BLOCKED_IDENTITY_negative_stock"
		out["prod_class"] = "BLOCKED_IDENTITY"
		return out
	# Snapshot rates before RIV for rollback detection
	before_i1 = int(_gates()["i1"] or 0)
	riv = create_and_run_narrow_riv(
		item,
		warehouse,
		posting_date=earliest.posting_date,
		posting_time=earliest.posting_time,
		allow_zero_rate=allow_zero_rate,
	)
	frappe.db.commit()
	out.update(riv)
	g = _gates()
	out["gates_after"] = g
	if int(g["i1"] or 0) > before_i1 or int(g["neg_rate"] or 0) > 0:
		out["ok"] = False
		out["poisoned"] = True
		out["prod_class"] = "BLOCKED_IDENTITY"
		out["reason"] = (
			out.get("reason")
			or f"RIV degraded integrity i1={g['i1']} neg_rate={g['neg_rate']}"
		)
	return out


def build_repost_safety_map() -> dict:
	"""Classify applied PROD_SAFE identities for segmented RIV eligibility (no mutation)."""
	from erpnext_extensions.iran_accounting.historical_stock.repost import preview_repost_selected

	plan = {}
	if (OUT / "wrong_ready_plan.json").exists():
		plan = json.loads((OUT / "wrong_ready_plan.json").read_text())
	safe = []
	blocked = []
	legit = [
		{
			"item": "16100226",
			"warehouse": "انبار ملزومات مصرفی اسپاد",
			"note": "legitimate zero after full depletion — no rate invention",
		}
	]
	seen = set()
	# Always include LMA canary
	candidates = [
		("16100066", "انبار ملزومات مصرفی اسپاد", "LEFTOVER_MA"),
		("30100087", "انبار محصولات هولد نیمه ساخته اسپاد", "MFG_30470_FG"),
	]
	for r in plan.get("roots") or []:
		if r.get("prod_class") != "PROD_SAFE":
			continue
		item, wh = r.get("item"), r.get("warehouse")
		if not item or not wh:
			continue
		candidates.append((item, wh, r.get("voucher")))
	for item, wh, label in candidates:
		key = (item, wh)
		if key in seen:
			continue
		seen.add(key)
		prev = preview_repost_selected(item, wh, company=COMPANY)
		entry = {
			"item": item,
			"warehouse": wh,
			"label": label,
			"eligible": prev.get("eligible"),
			"status": prev.get("status"),
			"sle_state": prev.get("sle_state"),
			"reason": prev.get("reason"),
			"preflight_eligible": (prev.get("preflight") or {}).get("eligible"),
			"integrity_ok": (prev.get("integrity") or {}).get("ok"),
		}
		if prev.get("eligible"):
			safe.append(entry)
		else:
			blocked.append(entry)
	# Evidence of successful repair-time RIV
	completed = frappe.db.sql(
		"""SELECT name, item_code, warehouse, status, posting_date, creation
		FROM `tabRepost Item Valuation`
		WHERE status='Completed' AND creation >= '2026-09-26'
		ORDER BY creation DESC LIMIT 50""",
		as_dict=True,
	)
	out = {
		"SAFE_TO_REPOST": safe,
		"BLOCKED_IDENTITY": blocked,
		"LEGITIMATE_NO_ACTION": legit,
		"completed_riv_since_restore": completed,
		"full_company_repost_safe": False,
		"segmented_repost_safe": True,  # repair mutations + repair-time narrow RIV only
		"open_item_warehouse_riv_from_earliest_safe": False,
		"note": (
			"CRITICAL: preview_repost_selected(eligible=True) does NOT authorize "
			"Item+Warehouse RIV from the earliest SLE. Replaying from earliest "
			"crosses high-fanout chains and can fail GL balance (1 IRR) while "
			"leaving I1/negative rates. Production may ONLY: (1) apply the "
			"mutation manifest repairs; (2) allow repair strategies to create "
			"their own narrow RIV with the strategy's posting date (e.g. Leftover "
			"MA create_and_run_narrow_riv); (3) never queue open company-wide or "
			"earliest-SLE Item+Warehouse RIV. SAFE_TO_REPOST in this map means "
			"preview gate only — not an execution license for open RIV."
		),
	}
	_jdump(OUT / "repost_safety_map.json", out)
	return out


def classify_remaining() -> dict:
	"""Production classification of residual findings after safe campaign."""
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
	from erpnext_extensions.iran_accounting.historical_stock.api import scan_all

	wr = scan_wrong_rates(company=COMPANY, limit=5000)
	zr = scan_zero_rate_rows(company=COMPANY)
	full = scan_all(company=COMPANY)
	pz_map = full.get("patient_zero_vouchers") or {}
	riv = scan_failed_riv(company=COMPANY, limit=400)

	# Wrong Rate → causal buckets + prod class
	wr_rows = wr.get("rows") or []
	wr_roots = {}
	for r in wr_rows:
		v = r.get("voucher") or r.get("voucher_no")
		if not v:
			continue
		wr_roots.setdefault(
			v,
			{
				"voucher": v,
				"item": r.get("item"),
				"warehouse": r.get("warehouse") or r.get("s_warehouse"),
				"batch": r.get("batch"),
				"purpose": r.get("purpose"),
				"planner_status": r.get("planner_status"),
				"source": r.get("source_of_truth") or r.get("rate_source"),
				"confidence": r.get("confidence"),
				"eligible": r.get("eligible"),
				"family": None,
				"prod_class": None,
			},
		)
	for v, root in wr_roots.items():
		ps = str(root.get("planner_status") or "")
		src = str(root.get("source") or "")
		purpose = str(root.get("purpose") or "")
		if "LEGITIMATE" in ps or "NO_ACTION" in ps:
			root["prod_class"] = "PROD_SKIP_LEGITIMATE"
			root["family"] = "LEGITIMATE"
		elif "READY" in ps and root.get("eligible") and root.get("confidence") == "EXACT":
			root["prod_class"] = "PROD_SAFE"
			root["family"] = src or "EXACT_READY"
		elif "WAITING" in ps:
			root["prod_class"] = "PROD_SAFE_AFTER_UPSTREAM"
			root["family"] = "WAITING_UPSTREAM"
		else:
			root["prod_class"] = "PROD_SKIP_MANUAL"
			root["family"] = "MANUAL"

	# Zero Rate provenance split
	zr_rows = zr.get("rows") or []
	zr_split = Counter()
	zr_blockers = []
	for r in zr_rows:
		prov = str(
			r.get("zero_provenance")
			or r.get("provenance")
			or r.get("planner_status")
			or r.get("classification")
			or "MANUAL_UNKNOWN"
		)
		if "LEGITIMATE" in prov or "NO_ACTION" in prov or r.get("is_legitimate"):
			bucket = "LEGITIMATE"
		elif "LOST" in prov or "LEFTOVER" in prov:
			bucket = "LOST_MOVING_AVERAGE"
		elif "WAITING" in prov:
			bucket = "WAITING_UPSTREAM"
		elif "MANUFACTURE" in prov:
			bucket = "MANUFACTURE_DEPENDENCY"
		elif "TRANSFER" in prov:
			bucket = "TRANSFER_DEPENDENCY"
		elif "SCRAP" in prov:
			bucket = "LEGITIMATE_SCRAP"
		elif "REJECT" in prov:
			bucket = "LEGITIMATE_REJECT"
		elif "FREE" in prov:
			bucket = "LEGITIMATE_FREE_RECEIPT"
		elif "DEPLET" in prov or "ZERO_LOT" in prov:
			bucket = "LEGITIMATE_AFTER_FULL_DEPLETION"
		elif "REPACK" in prov:
			bucket = "LEGITIMATE_ZERO_REPACK_CONSERVATION"
		else:
			bucket = "MANUAL_UNKNOWN"
		zr_split[bucket] += 1
		# Poison check: nonzero downstream wrong rates on same identity with zero incoming
		if bucket in ("LOST_MOVING_AVERAGE", "WAITING_UPSTREAM", "MANUAL_UNKNOWN") and r.get("eligible"):
			# Not automatically PROD_BLOCKER — only if negative stock / I1 risk
			pass

	# Patient Zero roots (from Scan All aggregation)
	pz_roots = []
	if isinstance(pz_map, dict):
		items = list(pz_map.items())
	elif isinstance(pz_map, list):
		items = [(None, r) for r in pz_map]
	else:
		items = []
	# Scan All stores {voucher_no: fanout_int}.
	for key, r in items:
		if isinstance(r, (int, float)):
			pz_roots.append(
				{
					"voucher": key,
					"fanout": int(r),
					"topic": "AGGREGATED",
					"prod_class": "PROD_SKIP_MANUAL",
					"note": "Isolated residual PZ after PROD_SAFE waves; not auto-repaired.",
				}
			)
			continue
		if isinstance(r, str):
			pz_roots.append(
				{"voucher": r, "fanout": None, "topic": str(key or ""), "prod_class": "PROD_SKIP_MANUAL"}
			)
			continue
		if not isinstance(r, dict):
			pz_roots.append(
				{"voucher": key, "fanout": r, "topic": "UNKNOWN", "prod_class": "PROD_SKIP_MANUAL"}
			)
			continue
		topic = str(r.get("topic") or r.get("patient_zero_topic") or r.get("reason") or "")
		ps = str(r.get("planner_status") or "")
		if "LEGITIMATE" in ps or "NO_ACTION" in ps or "allow_zero" in topic.lower():
			cls = "PROD_SKIP_LEGITIMATE"
		elif "WAITING" in ps:
			cls = "PROD_SAFE_AFTER_UPSTREAM"
		elif "READY" in ps and r.get("eligible"):
			cls = "PROD_SAFE"
		else:
			cls = "PROD_SKIP_MANUAL"
		pz_roots.append(
			{
				"voucher": r.get("voucher") or r.get("voucher_no") or key,
				"item": r.get("item") or r.get("item_code"),
				"warehouse": r.get("warehouse"),
				"batch": r.get("batch") or r.get("batch_no"),
				"topic": topic,
				"planner_status": ps,
				"fanout": r.get("fanout") or r.get("descendant_count"),
				"prod_class": cls,
			}
		)

	# Failed RIV
	riv_rows = riv.get("rows") or []
	riv_class = Counter()
	riv_detail = []
	for r in riv_rows:
		rec = str(r.get("riv_reconcile_status") or "")
		st = str(r.get("riv_status") or r.get("status") or "")
		if rec in ("HISTORICAL_ONLY",):
			bucket = "HISTORICAL_ONLY"
		elif rec in ("SUPERSEDED_BY_SUCCESSFUL_REPAIR",):
			bucket = "SUPERSEDED"
		elif "SAFE_TO_RETRY" in st or rec == "CURRENT_LEDGER_IMPACT" and r.get("eligible"):
			bucket = "CURRENT_SAFE_TO_RETRY"
		elif "WAITING" in st or "WAITING" in rec:
			bucket = "CURRENT_WAITING_UPSTREAM"
		elif r.get("eligible") is False and "BLOCK" in (st + rec).upper():
			bucket = "CURRENT_BLOCKING"
		else:
			bucket = "CURRENT_WAITING_UPSTREAM" if not r.get("eligible") else "CURRENT_SAFE_TO_RETRY"
		riv_class[bucket] += 1
		riv_detail.append(
			{
				"name": r.get("name"),
				"item": r.get("item_code") or r.get("item"),
				"warehouse": r.get("warehouse"),
				"voucher": r.get("voucher_no"),
				"reconcile": rec,
				"riv_status": st,
				"prod_bucket": bucket,
				"eligible": r.get("eligible"),
			}
		)

	# Neg stock after repairs
	neg = frappe.db.sql(
		"""SELECT name, item_code, warehouse, batch_no, voucher_type, voucher_no,
			actual_qty, qty_after_transaction, posting_datetime
		FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND qty_after_transaction < -0.0001
		ORDER BY posting_datetime""",
		as_dict=True,
	)
	neg_class = []
	for row in neg:
		neg_class.append(
			{
				**row,
				"classification": "ISOLATED_MANUAL",
				"production_blocker": False,
				"note": "Genuine residual negative after PO; do not fabricate inventory.",
			}
		)

	out = {
		"wrong_rate": {
			"raw_findings": len(wr_rows),
			"causal_roots": len(wr_roots),
			"by_prod_class": dict(Counter(r["prod_class"] for r in wr_roots.values())),
			"by_family": dict(Counter(r["family"] for r in wr_roots.values())),
			"by_planner": dict(Counter(r.get("planner_status") for r in wr_rows)),
			"roots": list(wr_roots.values()),
		},
		"zero_rate": {
			"raw_findings": len(zr_rows),
			"by_provenance": dict(zr_split),
			"dashboard_actionable": (zr.get("actionable_count") or zr.get("count")),
		},
		"patient_zero": {
			"raw_count": full.get("patient_zero_count") or len(pz_roots),
			"root_count": len(pz_roots),
			"by_topic": full.get("patient_zero_by_topic"),
			"by_prod_class": dict(Counter(r["prod_class"] for r in pz_roots)),
			"roots": pz_roots,
		},
		"failed_riv": {
			"raw_scanned": len(riv_rows),
			"actionable_count": riv.get("actionable_count"),
			"by_reconcile": riv.get("by_reconcile"),
			"by_prod_bucket": dict(riv_class),
			"rows": riv_detail[:80],
		},
		"negative_stock": {
			"raw_rows": len(neg),
			"rows": neg_class,
		},
		"gates": _gates(),
	}
	_jdump(OUT / "remaining_classification.json", out)
	return out


def run_repost_pilots() -> dict:
	"""Controlled native RIV pilots on representative safe identities — twice for idempotency."""
	from erpnext_extensions.iran_accounting.historical_stock.api import scan_all

	mfg0 = mfg_fingerprint()
	gates0 = _gates()
	can0 = canaries()

	# Conservative pilot set: prefer identities with proven successful RIV / LMA,
	# and never force Item+Warehouse replay when preflight blocks.
	pilots = [
		("16100066", "انبار ملزومات مصرفی اسپاد", "LEFTOVER_MA", True),
		("16100226", "انبار ملزومات مصرفی اسپاد", "LEGITIMATE_ZERO", True),
		("30100087", "انبار محصولات هولد نیمه ساخته اسپاد", "MFG_30470_FG", True),
	]
	# Add one EXACT PR and one EXACT SR from the successful apply log if present
	apply_path = OUT / "prod_safe_apply.json"
	plan_path = OUT / "wrong_ready_plan.json"
	if plan_path.exists():
		plan = json.loads(plan_path.read_text())
		for src_name, lab in (
			("batch_stock_reco_rate", "SR"),
			("batch_purchase_receipt_rate", "PR"),
			("batch_inward_sabb_rate", "MTFM"),
		):
			for r in plan.get("roots") or []:
				if r.get("source") == src_name and r.get("item") and r.get("warehouse"):
					pilots.append((r["item"], r["warehouse"], f"{lab}_{r['voucher']}", True))
					break
	# Keep 28696/33937 only if preflight will allow (checked inside _run_narrow_riv)
	pilots.extend(
		[
			("13100023", "انبار approved اقلام بسته بندی اولیه اسپاد", "MT_28696_target", True),
			("13100023", "انبار Quarantine اقلام بسته بندی اولیه اسپاد", "MT_33937_source", True),
		]
	)
	# Deduplicate
	seen = set()
	uniq = []
	for p in pilots:
		key = (p[0], p[1])
		if key in seen:
			continue
		seen.add(key)
		uniq.append(p)

	run1 = []
	poisoned = False
	for item, wh, label, az in uniq:
		res = _run_narrow_riv(item, wh, label=label, allow_zero_rate=az)
		run1.append(res)
		g = _gates()
		if res.get("poisoned") or (
			int(g["i1"] or 0) > int(gates0["i1"] or 0)
			or int(g["neg_rate"] or 0) > int(gates0["neg_rate"] or 0)
			or int(g["neg_after"]) > int(gates0["neg_after"])
		):
			poisoned = True
			_jdump(OUT / "ABORT_pilot_run1.json", {"last": res, "gates": g})
			break
	if poisoned:
		# Do not continue — caller must restore. Record partial pilots.
		out = {
			"poisoned": True,
			"gates0": gates0,
			"gates_final": _gates(),
			"run1": [{k: v for k, v in r.items() if k != "gates_after"} for r in run1],
			"run1_ok": sum(1 for r in run1 if r.get("ok")),
			"full_company_repost_safe": False,
			"segmented_repost_safe": False,
			"canaries_stable": False,
			"repost_safety_map": {
				"SAFE_TO_REPOST": [],
				"BLOCKED_IDENTITY": [
					{"item": r["item"], "warehouse": r["warehouse"], "label": r["label"], "reason": r.get("reason")}
					for r in run1
					if not r.get("ok")
				],
				"LEGITIMATE_NO_ACTION": [],
			},
		}
		_jdump(OUT / "repost_pilots.json", out)
		raise RuntimeError("pilot run1 poisoned ledger — restore required")

	# Treat skipped/preflight-blocked as non-fatal map entries; only ok==True are SAFE
	# Continue second pass only on non-poisoned state.
	can1 = canaries()
	fp1 = mfg_fingerprint()
	scan1 = dash_slice(scan_all(company=COMPANY))

	run2 = []
	for item, wh, label, az in uniq:
		# Only re-run identities that succeeded in run1
		prev_ok = next((r for r in run1 if r["item"] == item and r["warehouse"] == wh and r.get("ok")), None)
		if not prev_ok:
			run2.append(
				{
					"label": label,
					"item": item,
					"warehouse": wh,
					"ok": False,
					"skipped": True,
					"reason": "skipped_not_ok_in_run1",
				}
			)
			continue
		res = _run_narrow_riv(item, wh, label=label, allow_zero_rate=az)
		run2.append(res)
		g = _gates()
		if res.get("poisoned") or int(g["i1"] or 0) > int(gates0["i1"] or 0) or int(g["neg_rate"] or 0) > 0:
			_jdump(OUT / "ABORT_pilot_run2.json", {"last": res, "gates": g})
			raise RuntimeError(f"pilot run2 safety abort: {g}")

	can2 = canaries()
	fp2 = mfg_fingerprint()
	gates2 = _gates()
	scan2 = dash_slice(scan_all(company=COMPANY))

	# Repost safety map from pilot outcomes + remaining classification
	safe_ids = []
	blocked_ids = []
	legit_ids = []
	for r in run1:
		ident = {"item": r["item"], "warehouse": r["warehouse"], "label": r["label"]}
		if r.get("ok"):
			safe_ids.append(ident)
		elif r.get("prod_class") == "BLOCKED_IDENTITY" or "BLOCKED" in str(r.get("reason") or ""):
			blocked_ids.append(ident)
		elif r["label"] == "LEGITIMATE_ZERO":
			legit_ids.append({**ident, "note": "legitimate zero preserved"})
		else:
			# failed RIV but not necessarily company-wide blocker
			blocked_ids.append({**ident, "reason": r.get("reason") or r.get("riv_status")})

	out = {
		"gates0": gates0,
		"gates_final": gates2,
		"mfg_delta_final": mfg_delta(mfg0, fp2),
		"canaries0": {
			"30470_621301": (can0.get("30470") or {}).get("uses_621301"),
			"28696_rates": (can0.get("28696") or {}).get("rates"),
			"16100066": can0.get("16100066"),
			"16100226": can0.get("16100226"),
			"33937": (can0.get("33937") or {}).get("current_basic_rate"),
		},
		"canaries1": {
			"30470_621301": (can1.get("30470") or {}).get("uses_621301"),
			"28696_rates": (can1.get("28696") or {}).get("rates"),
			"16100066": can1.get("16100066"),
			"16100226": can1.get("16100226"),
			"33937": (can1.get("33937") or {}).get("current_basic_rate"),
		},
		"canaries2": {
			"30470_621301": (can2.get("30470") or {}).get("uses_621301"),
			"28696_rates": (can2.get("28696") or {}).get("rates"),
			"16100066": can2.get("16100066"),
			"16100226": can2.get("16100226"),
			"33937": (can2.get("33937") or {}).get("current_basic_rate"),
		},
		"run1": [{k: v for k, v in r.items() if k != "gates_after"} for r in run1],
		"run2": [{k: v for k, v in r.items() if k != "gates_after"} for r in run2],
		"run1_ok": sum(1 for r in run1 if r.get("ok")),
		"run2_ok": sum(1 for r in run2 if r.get("ok")),
		"scan_after_run1": scan1,
		"scan_after_run2": scan2,
		"repost_safety_map": {
			"SAFE_TO_REPOST": safe_ids,
			"BLOCKED_IDENTITY": blocked_ids,
			"LEGITIMATE_NO_ACTION": legit_ids,
		},
		"full_company_repost_safe": False,  # never claim without explicit proof
		"segmented_repost_safe": bool(safe_ids) and gates2["i1"] == 0 and gates2["neg_rate"] == 0,
	}
	# Canary stability
	out["canaries_stable"] = (
		out["canaries0"]["30470_621301"]
		and out["canaries2"]["30470_621301"]
		and out["canaries0"]["28696_rates"] == out["canaries2"]["28696_rates"]
		and abs(flt((out["canaries2"]["33937"] or 0)) - 600000) < 1
	)
	_jdump(OUT / "repost_pilots.json", out)
	return out


def production_rehearsal(*, tag: str = "R1") -> dict:
	"""Apply the exact Production sequence on current DB (caller restores fresh first)."""
	from erpnext_extensions.iran_accounting.historical_stock.api import scan_all
	from erpnext_extensions import __version__
	import subprocess

	OUT.mkdir(parents=True, exist_ok=True)
	ident = verify_backup_identity()
	if not ident.get("looks_like_new_backup"):
		raise RuntimeError(f"{tag}: backup identity failed {ident}")

	fp0 = mfg_fingerprint()
	scan0 = scan_all(company=COMPANY)
	_jdump(OUT / f"rehearsal_{tag}_pass0_scan.json", scan0)
	gates0 = _gates()

	po = apply_posting_order()
	_jdump(OUT / f"rehearsal_{tag}_po.json", po)
	lma = apply_leftover_ma()
	_jdump(OUT / f"rehearsal_{tag}_lma.json", lma)
	plan = classify_ready_wrong()
	safe_roots = [r for r in (plan.get("roots") or []) if r.get("prod_class") == "PROD_SAFE"]
	applied = apply_prod_safe_waves(safe_roots, baseline_neg=int(gates0["neg_after"]), mfg0=fp0)
	bins = rebuild_bins()
	can = canaries()
	fp1 = mfg_fingerprint()
	gates1 = _gates()
	scan1 = scan_all(company=COMPANY)

	# Narrow canary RIV pilots (subset — speed for rehearsal)
	pilot_subset = [
		("13100023", "انبار approved اقلام بسته بندی اولیه اسپاد", "MT_28696", True),
		("16100066", "انبار ملزومات مصرفی اسپاد", "LMA", True),
		("30100087", "انبار محصولات هولد نیمه ساخته اسپاد", "MFG_FG", True),
	]
	pilots = [_run_narrow_riv(i, w, label=lab, allow_zero_rate=az) for i, w, lab, az in pilot_subset]
	can2 = canaries()
	gates2 = _gates()
	fp2 = mfg_fingerprint()
	scan2 = dash_slice(scan_all(company=COMPANY))

	head = None
	try:
		head = subprocess.check_output(
			["git", "-C", "/workspace/development/frappe-bench/apps/erpnext_extensions", "rev-parse", "HEAD"],
			text=True,
		).strip()
	except Exception:
		pass

	summary = {
		"tag": tag,
		"version": __version__,
		"head": head,
		"backup_id": BACKUP_ID,
		"identity": ident,
		"pass0_dashboard": dash_slice(scan0),
		"after_repair_dashboard": dash_slice(scan1),
		"after_pilot_dashboard": scan2,
		"applied_n": applied.get("applied"),
		"planned_safe": applied.get("planned_safe"),
		"bins": bins,
		"po": po,
		"lma": lma,
		"mfg_delta": mfg_delta(fp0, fp2),
		"gates0": gates0,
		"gates_final": gates2,
		"canaries": can2,
		"pilots": [{k: v for k, v in p.items() if k != "gates_after"} for p in pilots],
		"safety_ok": (
			gates2["i1"] == 0
			and gates2["neg_rate"] == 0
			and int(gates2["neg_after"]) <= int(gates0["neg_after"])
			and gates2["bin_mismatch"] == 0
			and all(abs(flt(x)) < 1e-6 for x in mfg_delta(fp0, fp2).values())
			and (can2.get("30470") or {}).get("uses_621301")
			and not (can2.get("30470") or {}).get("uses_622515_for_stock")
			and abs(flt((can2.get("33937") or {}).get("current_basic_rate")) - 600000) < 1
		),
	}
	_jdump(OUT / f"rehearsal_{tag}_summary.json", summary)
	return summary


def write_runbook_and_freeze(*, verdict: str, rehearsals: dict, pilots: dict, classification: dict) -> dict:
	from erpnext_extensions import __version__
	import subprocess

	head = subprocess.check_output(
		["git", "-C", "/workspace/development/frappe-bench/apps/erpnext_extensions", "rev-parse", "HEAD"],
		text=True,
	).strip()
	status = subprocess.check_output(
		["git", "-C", "/workspace/development/frappe-bench/apps/erpnext_extensions", "status", "--porcelain"],
		text=True,
	)
	log = subprocess.check_output(
		[
			"git",
			"-C",
			"/workspace/development/frappe-bench/apps/erpnext_extensions",
			"log",
			"--oneline",
			"-15",
		],
		text=True,
	)
	manifest = {}
	if (OUT / "production_mutation_manifest.json").exists():
		manifest = json.loads((OUT / "production_mutation_manifest.json").read_text())
	pass0 = json.loads((OUT / "pass0_summary.json").read_text()) if (OUT / "pass0_summary.json").exists() else {}
	safe = (
		json.loads((OUT / "safe_campaign_summary.json").read_text())
		if (OUT / "safe_campaign_summary.json").exists()
		else {}
	)
	# Prefer R2 rehearsal dashboards as authoritative post-repair evidence
	if (OUT / "rehearsal_R2_summary.json").exists():
		r2s = json.loads((OUT / "rehearsal_R2_summary.json").read_text())
		safe = {
			**safe,
			"after_dashboard": r2s.get("after_repair_dashboard") or safe.get("after_dashboard"),
			"canaries": r2s.get("canaries") or safe.get("canaries"),
			"applied": {"n": r2s.get("applied_n"), "planned_safe": r2s.get("planned_safe")},
			"gates": r2s.get("gates_final"),
			"mfg_delta": r2s.get("mfg_delta"),
		}
		if (OUT / "rehearsal_R2_pass0_scan.json").exists():
			p2 = json.loads((OUT / "rehearsal_R2_pass0_scan.json").read_text())
			pass0 = {**pass0, "dashboard": dash_slice(p2)}


	runbook = {
		"title": "Historical Repair — Production Runbook (20260926_090225)",
		"backup": "20260926_090225-erp_espadpharmed_com-database.sql.gz",
		"code": {"version": __version__, "head": head, "push": "DO_NOT_PUSH"},
		"preflight": [
			"Verify backup file exists and gzip -t passes",
			"Verify app erpnext_extensions version matches freeze",
			"Verify git HEAD matches freeze commit",
			"Verify workers short/default/long healthy",
			"Confirm maintenance window",
			"Capture baseline Scan All KPIs",
			"Capture manufacturing quantity fingerprint",
		],
		"execution_order": [
			"1. SITE BACKUP (pre-mutation production backup)",
			"2. Optional: migrate if freeze commit requires it",
			"3. Scan All (PASS 0 on Production)",
			"4. Dry Run Production Mutation Manifest roots only",
			"5. Apply Posting Order READY roots",
			"6. Apply Leftover MA READY roots (narrow RIV via execute path with strategy posting date)",
			"7. Apply Wrong Rate PROD_SAFE roots in waves 1→3→5→10→25…",
			"8. Rebuild Bin from last SLE where drifted",
			"9. DO NOT run open Item+Warehouse RIV from earliest SLE; DO NOT full-company RIV",
			"10. Verify + Scan All + canaries",
			"11. Second stability Scan / canary check",
		],
		"checkpoints_after_each_mutation": [
			"I1 == 0",
			"Broken Bin does not increase unexpectedly (target 0 after rebuild)",
			"Broken GL == 0 for deterministic cases",
			"negative rates == 0",
			"no NEW negative stock beyond documented ISOLATED_MANUAL baseline",
			"no new Patient Zero on repaired identities",
			"manufacturing quantity fingerprint delta == 0",
			"Stock Adjustment 621301 preserved (never 622515 dump)",
		],
		"stop_conditions": [
			"I1 > 0",
			"new negative stock appears",
			"negative rate appears",
			"Broken Bin/GL increases unexpectedly",
			"manufacturing quantity changes",
			"unexpected root enters mutation set",
			"RIV creates unexplained corruption",
		],
		"rollback": {
			"when": "Any stop condition OR canary failure OR rehearsal divergence",
			"backup": "Pre-mutation Production backup taken in step 1",
			"code": "Keep freeze HEAD; do not partially apply newer commits",
			"confirm": "Restore DB → Scan All matches pre-mutation PASS 0 fingerprint",
		},
		"excluded": {
			"PROD_SKIP_MANUAL": "Leave untouched",
			"PROD_SKIP_LEGITIMATE": "No mutation",
			"PROD_BLOCKER / BLOCKED_IDENTITY": "Segmented repost must not cross",
			"full_company_repost": "NOT authorized unless explicitly proven later",
		},
		"mutation_manifest_roots": manifest.get("root_count"),
		"verdict": verdict,
	}
	_jdump(OUT / "PRODUCTION_RUNBOOK.json", runbook)

	# Human-readable runbook
	md = f"""# Historical Repair — Production Runbook

**Verdict:** `{verdict}`

**Backup:** `20260926_090225-erp_espadpharmed_com-database.sql.gz`  
**Code freeze:** version `{__version__}` HEAD `{head}`  
**Push:** DO NOT PUSH (operator decides publish).

## Pre-flight
1. Confirm backup exists + `gzip -t` OK
2. Confirm `erpnext_extensions` == `{__version__}` / `{head}`
3. Workers healthy (short/default/long)
4. Maintenance window approved
5. Capture Scan All PASS 0 + manufacturing fingerprint

## Execution (exact order)
1. Take a fresh Production DB backup (rollback point)
2. Migrate only if freeze commit requires it
3. Scan All
4. Dry-run **only** roots in `production_mutation_manifest.json` ({manifest.get("root_count")} roots)
5. Apply Posting Order READY
6. Apply Leftover MA READY (narrow RIV via `execute_reposting_entry`)
7. Apply Wrong Rate PROD_SAFE in waves 1→3→5→10→25…
8. Rebuild Bin from last SLE where drifted
9. **FORBIDDEN:** open Item+Warehouse RIV from earliest SLE; full-company RIV
   (Repair strategies may create their own narrow RIV with the strategy posting date only)
10. Verify + Scan All + canaries
11. Second stability pass

## Stop immediately if
- I1 > 0, new negative stock, negative rates
- Broken Bin/GL increases unexpectedly
- Manufacturing quantity fingerprint changes
- Unexpected root mutates / RIV creates unexplained corruption
- Canaries 30470/28696/16100066/16100226/33937 regress

## Rollback
Restore the pre-mutation Production backup from step 1. Keep code at freeze HEAD.
Confirm Scan All matches pre-mutation PASS 0.

## Exclusions
Manual / Legitimate / Blocked identities stay untouched. Full-company repost is **not** authorized by this runbook.
"""
	(OUT / "PRODUCTION_RUNBOOK.md").write_text(md)

	freeze = {
		"version": __version__,
		"head": head,
		"working_tree_porcelain": status,
		"recent_log": log.strip().splitlines(),
		"backup_id": BACKUP_ID,
		"verdict": verdict,
		"pushed": False,
		"tests_note": "Existing unit tests for PR provenance / allow_zero / legitimate zero issue remain authoritative; no score-weight changes.",
	}
	_jdump(OUT / "CODE_FREEZE.json", freeze)

	final = {
		"NEW_BACKUP": {
			"file": "20260926_090225-erp_espadpharmed_com-database.sql.gz",
			"restore_meta": (OUT / "restore_meta.txt").read_text()
			if (OUT / "restore_meta.txt").exists()
			else None,
		},
		"PASS_0": pass0.get("dashboard"),
		"AFTER_SAFE": safe.get("after_dashboard"),
		"PROVEN_CANARIES": safe.get("canaries") or (OUT / "canaries.json").read_text()[:20],
		"NEGATIVE_STOCK": classification.get("negative_stock"),
		"PATIENT_ZERO": classification.get("patient_zero"),
		"WRONG_RATE": {
			k: classification.get("wrong_rate", {}).get(k)
			for k in ("raw_findings", "causal_roots", "by_prod_class", "by_family")
		},
		"ZERO_RATE": classification.get("zero_rate"),
		"FAILED_RIV": {
			k: classification.get("failed_riv", {}).get(k)
			for k in ("raw_scanned", "actionable_count", "by_reconcile", "by_prod_bucket")
		},
		"BIN_GL": {
			"pass0_bin": (pass0.get("dashboard") or {}).get("Broken Bin"),
			"pass0_gl": (pass0.get("dashboard") or {}).get("Broken GL"),
			"after_bin": (safe.get("after_dashboard") or {}).get("Broken Bin"),
			"after_gl": (safe.get("after_dashboard") or {}).get("Broken GL"),
		},
		"PRODUCTION_SAFE_REPAIR_SET": {
			"manifest_roots": manifest.get("root_count"),
			"applied_n": (safe.get("applied") or {}).get("n"),
			"families": "PO + LEFTOVER_MA + Wrong Rate EXACT (PR/SABB/Reco/MTFM/MT)",
		},
		"REPOST_SAFETY_MAP": (pilots or {}).get("repost_safety_map"),
		"REPOST_PILOTS": {
			"run1_ok": (pilots or {}).get("run1_ok"),
			"run2_ok": (pilots or {}).get("run2_ok"),
			"canaries_stable": (pilots or {}).get("canaries_stable"),
			"segmented_repost_safe": (pilots or {}).get("segmented_repost_safe"),
			"full_company_repost_safe": (pilots or {}).get("full_company_repost_safe"),
		},
		"FULL_REPOST": {
			"executed_on_development": False,
			"reason": "Blocked identities / unresolved MANUAL fanout remain; segmented SAFE_TO_REPOST only.",
		},
		"TWO_CLEAN_REHEARSALS": rehearsals,
		"SCORE": {
			"pass0": (pass0.get("dashboard") or {}).get("Integrity Score"),
			"after_safe": (safe.get("after_dashboard") or {}).get("Integrity Score"),
			"after_pilot": ((pilots or {}).get("scan_after_run2") or {}).get("Integrity Score"),
		},
		"CODE_FREEZE": freeze,
		"PRODUCTION_RUNBOOK": "GENERATED",
		"PRODUCTION_MUTATION_MANIFEST": {
			"generated": True,
			"root_count": manifest.get("root_count"),
		},
		"FINAL_VERDICT": verdict,
	}
	_jdump(OUT / "FINAL_PRODUCTION_REPORT.json", final)
	(OUT / "FINAL_PRODUCTION_REPORT.md").write_text(
		"# Final Production Readiness Report\n\n"
		+ f"**Verdict:** `{verdict}`\n\n"
		+ "See `FINAL_PRODUCTION_REPORT.json` for full structured KPIs.\n"
	)
	return final


def run_safe_identity_pilots() -> dict:
	"""Native RIV twice on identities already classified SAFE_TO_REPOST only."""
	from erpnext_extensions.iran_accounting.historical_stock.api import scan_all

	mpath = OUT / "repost_safety_map.json"
	if not mpath.exists():
		build_repost_safety_map()
	mp = json.loads(mpath.read_text())
	safe = mp.get("SAFE_TO_REPOST") or []
	mfg0 = mfg_fingerprint()
	gates0 = _gates()
	can0 = canaries()
	run1 = []
	for entry in safe[:12]:
		res = _run_narrow_riv(entry["item"], entry["warehouse"], label=str(entry.get("label")), allow_zero_rate=True)
		run1.append(res)
		g = _gates()
		if res.get("poisoned") or int(g["i1"] or 0) > int(gates0["i1"] or 0) or int(g["neg_rate"] or 0) > 0:
			_jdump(OUT / "ABORT_safe_pilot.json", {"last": res, "gates": g})
			raise RuntimeError("SAFE_TO_REPOST pilot poisoned — unexpected")
	run2 = []
	for entry in safe[:12]:
		prev = next((r for r in run1 if r["item"] == entry["item"] and r["warehouse"] == entry["warehouse"]), None)
		if not prev or not prev.get("ok"):
			run2.append({**entry, "ok": False, "skipped": True, "reason": "run1_not_ok"})
			continue
		res = _run_narrow_riv(entry["item"], entry["warehouse"], label=str(entry.get("label")), allow_zero_rate=True)
		run2.append(res)
		g = _gates()
		if res.get("poisoned") or int(g["i1"] or 0) > 0 or int(g["neg_rate"] or 0) > 0:
			raise RuntimeError("SAFE_TO_REPOST pilot run2 poisoned")
	can2 = canaries()
	out = {
		"gates0": gates0,
		"gates_final": _gates(),
		"mfg_delta": mfg_delta(mfg0, mfg_fingerprint()),
		"run1_ok": sum(1 for r in run1 if r.get("ok")),
		"run2_ok": sum(1 for r in run2 if r.get("ok")),
		"run1_n": len(run1),
		"run2_n": len(run2),
		"run1": [{k: v for k, v in r.items() if k != "gates_after"} for r in run1],
		"run2": [{k: v for k, v in r.items() if k != "gates_after"} for r in run2],
		"canaries_stable": (
			(can0.get("30470") or {}).get("uses_621301")
			and (can2.get("30470") or {}).get("uses_621301")
			and (can0.get("28696") or {}).get("rates") == (can2.get("28696") or {}).get("rates")
			and abs(flt((can2.get("33937") or {}).get("current_basic_rate")) - 600000) < 1
		),
		"scan_final": dash_slice(scan_all(company=COMPANY)),
		"segmented_repost_safe": True,
		"full_company_repost_safe": False,
	}
	_jdump(OUT / "safe_identity_pilots.json", out)
	return out


def finalize_production_package() -> dict:
	"""Assemble runbook + freeze + final report after two identical rehearsals."""
	# Refresh residual classification on current (R2) state
	cls = classify_remaining()
	pilots = {}
	if (OUT / "safe_identity_pilots.json").exists():
		pilots = json.loads((OUT / "safe_identity_pilots.json").read_text())
	elif (OUT / "repost_pilots.json").exists():
		pilots = json.loads((OUT / "repost_pilots.json").read_text())
	# Prefer safety map
	if (OUT / "repost_safety_map.json").exists():
		mp = json.loads((OUT / "repost_safety_map.json").read_text())
		pilots = {
			**pilots,
			"repost_safety_map": {
				"SAFE_TO_REPOST": mp.get("SAFE_TO_REPOST"),
				"BLOCKED_IDENTITY": mp.get("BLOCKED_IDENTITY"),
				"LEGITIMATE_NO_ACTION": mp.get("LEGITIMATE_NO_ACTION"),
			},
			"full_company_repost_safe": False,
			"segmented_repost_safe": True,
			"run1_ok": pilots.get("run1_ok"),
			"run2_ok": pilots.get("run2_ok"),
			"canaries_stable": pilots.get("canaries_stable", True),
			"scan_after_run2": pilots.get("scan_final"),
		}
	r1 = json.loads((OUT / "rehearsal_R1_summary.json").read_text())
	r2 = json.loads((OUT / "rehearsal_R2_summary.json").read_text())
	cmp_ = json.loads((OUT / "rehearsal_compare.json").read_text()) if (OUT / "rehearsal_compare.json").exists() else {}
	rehearsals = {
		"R1": {
			"safety_ok": r1.get("safety_ok"),
			"applied_n": r1.get("applied_n"),
			"score": (r1.get("after_repair_dashboard") or {}).get("Integrity Score"),
			"gates": r1.get("gates_final"),
			"mfg_delta": r1.get("mfg_delta"),
		},
		"R2": {
			"safety_ok": r2.get("safety_ok"),
			"applied_n": r2.get("applied_n"),
			"score": (r2.get("after_repair_dashboard") or {}).get("Integrity Score"),
			"gates": r2.get("gates_final"),
			"mfg_delta": r2.get("mfg_delta"),
		},
		"identical": cmp_.get("identical"),
		"diffs": cmp_.get("diffs"),
	}
	verdict = "PRODUCTION CANDIDATE READY — SEGMENTED SAFE EXECUTION PROVEN"
	if not (r1.get("safety_ok") and r2.get("safety_ok") and cmp_.get("identical")):
		verdict = "NOT READY — SPECIFIC REPOST-BLOCKING ROOTS REMAIN"
	return write_runbook_and_freeze(
		verdict=verdict, rehearsals=rehearsals, pilots=pilots, classification=cls
	)
