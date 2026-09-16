# Copyright (c) 2026 — Data recovery campaign: drain READY Wrong Rate + GL
from __future__ import annotations

import json
import os
from datetime import datetime
from time import perf_counter

ARTIFACT = (
	"/workspace/development/frappe-bench/apps/erpnext_extensions/"
	".local-backups/restore_20260915_133438/campaigns_recovery"
)
COMPANY = "اسپاد فارمد دارو"


def _dump(name, data):
	os.makedirs(ARTIFACT, exist_ok=True)
	path = os.path.join(ARTIFACT, name)
	with open(path, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=2, default=str, ensure_ascii=False)
	return path


def _ready_wr(limit=5000):
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

	scan = scan_wrong_rates(company=COMPANY, limit=limit)
	rows = []
	for r in scan.get("rows") or []:
		ps = str(r.get("planner_status") or "")
		if (
			(ps in READY_STATUSES or ps.startswith("READY"))
			and int(r.get("sql_updates") or 0) > 0
			and r.get("eligible")
		):
			r.setdefault("topic", "WRONG_RATE")
			rows.append(r)
	return rows, scan


def _ready_gl():
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity

	scan = scan_gl_integrity(company=COMPANY, limit=300)
	rows = []
	for r in scan.get("rows") or []:
		ps = str(r.get("planner_status") or "")
		if (
			(ps in READY_STATUSES or ps.startswith("READY"))
			and int(r.get("sql_updates") or 0) > 0
			and r.get("eligible")
			and not r.get("sle_poisoned")
			and str(r.get("gl_class") or "")
			in (
				"G1_BALANCED_BUT_ECONOMICALLY_WRONG",
				"G2_MISSING",
				"G3_UNBALANCED",
			)
		):
			rows.append(r)
	return rows, scan


def repair_wr_batch(max_n=10) -> dict:
	"""One SAFE_GROUP Wrong Rate batch. Continues past single-root failures."""
	from erpnext_extensions.iran_accounting.historical_stock.campaign_clusters import (
		SAFE_GROUP,
		cluster_independent_roots,
	)
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.apply import (
		apply_wrong_rate_root,
	)

	t0 = perf_counter()
	rows, _ = _ready_wr()
	if not rows:
		return {"ok": True, "stage": "empty", "n_ready": 0, "repaired": [], "failed": []}
	clusters = cluster_independent_roots(rows, max_cluster=max_n)
	group = clusters.get("recommended_first_group") or {}
	order = group.get("roots") or group.get("repair_order") or []
	if group.get("group_class") != SAFE_GROUP and order:
		# Fall back: take first max_n independent-ish by voucher uniqueness
		order = order[:max_n]
	repaired, failed = [], []
	seen_keys = set()
	voucher_order = []
	for root in order[:max_n]:
		vn = root if isinstance(root, str) else (root.get("voucher") if isinstance(root, dict) else None)
		if vn and vn not in voucher_order:
			voucher_order.append(vn)
	for vn in voucher_order:
		matches = [r for r in rows if r.get("voucher") == vn]
		for m in matches:
			ps = str(m.get("planner_status") or "")
			if ps not in (
				"READY",
				"READY_WRONG_RATE",
				"READY_LOCAL_REPAIR",
				"READY_BATCH_SCOPED_REPAIR",
				"READY_WORK_ORDER_REPAIR",
				"READY_IDENTITY_REPAIR",
			) or not m.get("eligible"):
				continue
			key = (m.get("voucher"), m.get("item"), m.get("sle") or m.get("voucher_detail"))
			if key in seen_keys:
				continue
			seen_keys.add(key)
			res = apply_wrong_rate_root(m, dry_run=False)
			entry = {
				"voucher": m.get("voucher"),
				"item": m.get("item"),
				"ok": res.get("ok"),
				"after_rate": res.get("after_rate"),
				"expected": res.get("expected"),
				"reason": res.get("reason") or res.get("error") or ((res.get("out") or {}).get("reason")),
			}
			if res.get("ok"):
				repaired.append(entry)
			else:
				failed.append(entry)
				# continue other roots / items
	out = {
		"ok": True,
		"stage": "wr_batch",
		"group_class": group.get("group_class"),
		"n_ready_before": len(rows),
		"n_attempted": len(repaired) + len(failed),
		"n_repaired": len(repaired),
		"n_failed": len(failed),
		"repaired": repaired,
		"failed": failed,
		"elapsed": round(perf_counter() - t0, 3),
	}
	_dump(f"wr_batch_{datetime.utcnow().strftime('%H%M%S')}.json", out)
	return out


def repair_gl_batch(max_n=5) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import rebuild_gl_for_voucher
	import frappe
	from erpnext.accounts.general_ledger import toggle_debit_credit_if_negative

	t0 = perf_counter()
	rows, _ = _ready_gl()
	repaired, failed = [], []
	for r in rows[:max_n]:
		v = r.get("voucher")
		se = frappe.get_doc("Stock Entry", v)
		expected = toggle_debit_credit_if_negative(se.get_gl_entries(se.get_inventory_account_map()))
		if not expected:
			failed.append({"voucher": v, "ok": False, "reason": "NO_EXPECTED_GL"})
			continue
		dry = rebuild_gl_for_voucher(v, dry_run=True)
		if dry.get("blocked"):
			failed.append({"voucher": v, "ok": False, "reason": dry.get("reason") or dry.get("planner_status")})
			continue
		applied = rebuild_gl_for_voucher(v, dry_run=False)
		ok = bool(applied.get("written")) and not applied.get("blocked")
		entry = {
			"voucher": v,
			"ok": ok,
			"before": (applied.get("before") or {}).get("gl_class"),
			"after": applied.get("gl_class"),
			"reason": applied.get("reason"),
		}
		(repaired if ok else failed).append(entry)
		if ok:
			frappe.db.commit()
	out = {
		"ok": True,
		"stage": "gl_batch",
		"n_ready_before": len(rows),
		"n_repaired": len(repaired),
		"n_failed": len(failed),
		"repaired": repaired,
		"failed": failed,
		"elapsed": round(perf_counter() - t0, 3),
	}
	_dump(f"gl_batch_{datetime.utcnow().strftime('%H%M%S')}.json", out)
	return out


def run(*, max_wr_batches=15, batch_size=10, do_gl=1):
	"""Drain Wrong Rate READY via SAFE_GROUP batches, then GL G1/G3."""
	t0 = perf_counter()
	batches = []
	total_repaired = 0
	total_failed = 0
	for i in range(int(max_wr_batches)):
		before, _ = _ready_wr()
		if not before:
			break
		batch = repair_wr_batch(max_n=int(batch_size))
		batches.append(batch)
		total_repaired += batch.get("n_repaired") or 0
		total_failed += batch.get("n_failed") or 0
		# Stop if a batch repaired nothing (stuck)
		if (batch.get("n_repaired") or 0) == 0:
			batches.append({"ok": False, "stage": "stuck", "n_ready": len(before)})
			break
	gl_out = None
	if int(do_gl):
		gl_out = repair_gl_batch(max_n=5)
		total_repaired += gl_out.get("n_repaired") or 0
		total_failed += gl_out.get("n_failed") or 0

	# Final queue snapshot
	wr_after, _ = _ready_wr()
	gl_after, gl_scan = _ready_gl()
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from frappe.utils import nowdate

	riv = scan_failed_riv(company=COMPANY, limit=500)
	i4 = scan_i4_leftover(company=COMPANY, from_date="2026-03-21", to_date=str(nowdate()), limit=5000)
	riv_safe = sum(1 for r in (riv.get("rows") or []) if r.get("riv_status") == "SAFE_TO_RETRY")

	summary = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"wr_batches": len([b for b in batches if b.get("stage") == "wr_batch"]),
		"total_repaired": total_repaired,
		"total_failed": total_failed,
		"queues_after": {
			"wrong_rate_ready": len(wr_after),
			"gl_ready_g1_g3": len(gl_after),
			"riv_safe": riv_safe,
			"i4_by_status": i4.get("by_status"),
			"riv_by_status": riv.get("by_status"),
			"gl_by_class": gl_scan.get("by_class"),
		},
		"batches": [
			{
				"stage": b.get("stage"),
				"n_repaired": b.get("n_repaired"),
				"n_failed": b.get("n_failed"),
				"vouchers": [x.get("voucher") for x in (b.get("repaired") or [])],
				"failed_sample": (b.get("failed") or [])[:3],
			}
			for b in batches
		],
		"gl": gl_out,
		"elapsed": round(perf_counter() - t0, 3),
	}
	_dump("campaign_summary.json", summary)
	print(json.dumps({k: v for k, v in summary.items() if k != "batches"}, ensure_ascii=False, indent=2, default=str))
	print(json.dumps({"batches": summary["batches"]}, ensure_ascii=False, indent=2, default=str)[:6000])
	return summary
