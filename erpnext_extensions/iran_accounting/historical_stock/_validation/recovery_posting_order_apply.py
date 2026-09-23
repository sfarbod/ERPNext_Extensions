# Copyright (c) 2026 — apply READY Posting Order / Warehouse SAFE_GROUP
from __future__ import annotations

import json
from time import perf_counter


COMPANY = "اسپاد فارمد دارو"


def collect_moved_vouchers(applied_entries: list | None, *, primary: str | None = None) -> set[str]:
	"""Vouchers whose posting time was written by a successful apply_repairs call.

	Optimizer multi-move lists often bump collision siblings in the same identity
	window. A single-scan batch must treat those siblings as already handled —
	re-applying them against the pre-move preview correctly aborts as STALE_PREVIEW.
	"""
	moved: set[str] = set()
	if primary:
		moved.add(str(primary))
	for entry in applied_entries or []:
		out = entry.get("outbound_document") or entry.get("outbound")
		if out:
			moved.add(str(out))
		for m in entry.get("moves") or []:
			doc = m.get("document") if isinstance(m, dict) else None
			if doc:
				moved.add(str(doc))
	return moved


def run(*, max_n=5, dry_run=0, batch_scoped_only=1, exact_only=1):
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import (
		READY_STATUSES,
		attach_plan,
	)
	from erpnext_extensions.iran_accounting.historical_stock.campaign_clusters import (
		cluster_independent_roots,
	)
	from erpnext_extensions.iran_accounting.stock_posting_order.repair import apply_repairs
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.replay import (
		apply_warehouse_repair,
	)

	t0 = perf_counter()
	scan = run_full_history_scan(company=COMPANY)
	cache = {}
	ready = []
	for raw in scan.get("rows") or []:
		r = attach_plan(dict(raw), cache=cache)
		ps = str(r.get("planner_status") or "")
		if not (ps in READY_STATUSES and int(r.get("sql_updates") or 0) > 0 and r.get("eligible")):
			continue
		if int(batch_scoped_only) and ps != "READY_BATCH_SCOPED_REPAIR":
			continue
		# Effective EXACT includes LIKELY→EXACT promotion (confidence stamped by attach_plan).
		if int(exact_only) and str(r.get("confidence") or "") != "EXACT":
			continue
		ready.append(r)

	if not ready:
		out = {
			"ok": True,
			"n_ready": 0,
			"repaired": [],
			"failed": [],
			"elapsed": round(perf_counter() - t0, 3),
		}
		print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
		return out

	# Prefer independent identities for SAFE_GROUP
	for r in ready:
		r.setdefault("voucher", r.get("outbound_document") or r.get("inbound_document"))
		r.setdefault("topic", "POSTING_ORDER")
	clusters = cluster_independent_roots(ready, max_cluster=int(max_n))
	group = clusters.get("recommended_first_group") or {}
	order = group.get("roots") or group.get("repair_order") or []
	# Prefer SAFE_GROUP order, then any remaining READY outbounds.
	voucher_order = []
	for root in order[: int(max_n)]:
		vn = root if isinstance(root, str) else (root.get("voucher") if isinstance(root, dict) else None)
		if vn and vn not in voucher_order:
			voucher_order.append(vn)
	for r in ready:
		vn = r.get("outbound_document") or r.get("voucher")
		if vn and vn not in voucher_order:
			voucher_order.append(vn)
		if len(voucher_order) >= int(max_n):
			break

	repaired, failed = [], []
	seen_out = set()
	# Outbounds already timestamp-shifted by an earlier apply in this batch
	# (primary or collision multi-move). Skip to avoid false STALE_PREVIEW.
	moved_elsewhere: set[str] = set()
	is_dry = bool(int(dry_run))
	for vn in voucher_order:
		if vn in seen_out:
			continue
		seen_out.add(vn)
		matches = [r for r in ready if (r.get("outbound_document") or r.get("voucher")) == vn]
		if not matches:
			continue
		if vn in moved_elsewhere:
			repaired.append(
				{
					"inbound": matches[0].get("inbound_document"),
					"outbound": vn,
					"item": ",".join(sorted({str(m.get("item") or "") for m in matches})),
					"ps": str(matches[0].get("planner_status") or ""),
					"conf": matches[0].get("confidence"),
					"raw_conf": matches[0].get("raw_confidence"),
					"ok": True,
					"reason": "already_moved_as_collateral",
					"planner_status": None,
					"joint_n": len(matches),
					"applied_n": 0,
					"blocked_n": 0,
					"dry_run": is_dry,
					"collateral": True,
				}
			)
			continue
		ps = str(matches[0].get("planner_status") or "")
		try:
			if ps == "READY_WAREHOUSE_REPLAY":
				res = apply_warehouse_repair(matches[0], dry_run=is_dry)
				entry = {
					"inbound": matches[0].get("inbound_document"),
					"outbound": matches[0].get("outbound_document"),
					"item": matches[0].get("item"),
					"ps": ps,
					"conf": matches[0].get("confidence"),
					"raw_conf": matches[0].get("raw_confidence"),
					"ok": bool(res.get("ok")),
					"reason": res.get("reason") or res.get("error"),
					"planner_status": res.get("planner_status"),
					"joint_n": 1,
					"dry_run": is_dry,
				}
				(repaired if entry["ok"] else failed).append(entry)
			else:
				# Pass all item-pairs for this outbound together so apply_repairs
				# can coalesce to a single joint timestamp move.
				res = apply_repairs(matches, dry_run=is_dry)
				blocked = (res or {}).get("blocked") or []
				applied = (res or {}).get("applied") or []
				ok = bool(applied) and not blocked and not (res or {}).get("aborted")
				reason = None
				if blocked:
					reason = blocked[0].get("error")
				reason = reason or (res or {}).get("reason") or (res or {}).get("skip_reason")
				entry = {
					"inbound": matches[0].get("inbound_document"),
					"outbound": vn,
					"item": ",".join(sorted({str(m.get("item") or "") for m in matches})),
					"ps": ps,
					"conf": matches[0].get("confidence"),
					"raw_conf": matches[0].get("raw_confidence"),
					"ok": ok,
					"reason": reason,
					"planner_status": None,
					"joint_n": len(matches),
					"applied_n": len(applied),
					"blocked_n": len(blocked),
					"dry_run": is_dry,
				}
				(repaired if ok else failed).append(entry)
				if ok and not is_dry:
					moved_elsewhere |= collect_moved_vouchers(applied, primary=vn)
		except Exception as e:
			failed.append(
				{
					"inbound": matches[0].get("inbound_document"),
					"outbound": vn,
					"item": matches[0].get("item"),
					"ps": ps,
					"ok": False,
					"reason": f"{type(e).__name__}: {e}",
					"joint_n": len(matches),
					"dry_run": is_dry,
				}
			)

	out = {
		"ok": True,
		"group_class": group.get("group_class"),
		"filter": {
			"batch_scoped_only": bool(int(batch_scoped_only)),
			"exact_only": bool(int(exact_only)),
		},
		"n_ready_before": len(ready),
		"n_attempted": len(repaired) + len(failed),
		"n_repaired": len(repaired),
		"n_failed": len(failed),
		"repaired": repaired,
		"failed": failed,
		"elapsed": round(perf_counter() - t0, 3),
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:8000])
	return out
