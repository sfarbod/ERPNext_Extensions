# Copyright (c) 2026 — Posting Order root-cause inventory for recovery
from __future__ import annotations

import json
from collections import Counter, defaultdict


COMPANY = "اسپاد فارمد دارو"


def run():
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import (
		READY_STATUSES,
		evaluate_row,
		attach_plan,
	)
	from erpnext_extensions.iran_accounting.historical_stock.scope import evaluate_minimal_scope

	scan = run_full_history_scan(company=COMPANY)
	rows = list(scan.get("rows") or [])
	# Stamp with planner
	stamped = []
	cache = {}
	by_ps = Counter()
	by_opt = Counter()
	by_conf = Counter()
	ready = []
	escalation = []
	shortage = []
	ambiguous = []
	manual = []
	waiting = []
	unlock_candidates = []

	for r in rows:
		r = dict(r)
		# Ensure topic surface for planner routing
		if not r.get("inbound_document"):
			# scanner rows usually have inbound/outbound docs
			pass
		planned = attach_plan(r, cache=cache)
		stamped.append(planned)
		ps = str(planned.get("planner_status") or "")
		opt = str(planned.get("optimizer_status") or planned.get("status") or "")
		conf = str(planned.get("confidence") or "")
		by_ps[ps] += 1
		by_opt[opt] += 1
		by_conf[conf] += 1
		if ps in READY_STATUSES and int(planned.get("sql_updates") or 0) > 0 and planned.get("eligible"):
			ready.append(planned)
		elif "WAREHOUSE_ESCALATION" in ps:
			escalation.append(planned)
		elif "SHORTAGE" in ps or "SHORTAGE" in opt or opt == "REAL_STOCK_SHORTAGE":
			shortage.append(planned)
		elif "AMBIGUOUS" in ps or conf == "AMBIGUOUS":
			ambiguous.append(planned)
		elif "WAITING" in ps:
			waiting.append(planned)
		elif "MANUAL" in ps or "MIDNIGHT" in ps or "MIDNIGHT" in opt:
			manual.append(planned)

	# Escalation deep dive: is it engine limit or real shortage?
	esc_reasons = Counter()
	esc_samples = []
	for r in escalation[:200]:
		scope = r.get("scope") or {}
		reason = (
			scope.get("escalation_reason")
			or scope.get("reason")
			or r.get("reason")
			or r.get("skip_reason")
			or "unknown"
		)
		esc_reasons[str(reason)[:160]] += 1
		if len(esc_samples) < 12:
			esc_samples.append(
				{
					"inbound": (r.get("inbound_document") or {}).get("voucher_no")
					if isinstance(r.get("inbound_document"), dict)
					else r.get("inbound_document"),
					"outbound": (r.get("outbound_document") or {}).get("voucher_no")
					if isinstance(r.get("outbound_document"), dict)
					else r.get("outbound_document"),
					"item": r.get("item") or r.get("item_code"),
					"warehouse": r.get("warehouse"),
					"batch": r.get("batch") or r.get("batch_no"),
					"ps": r.get("planner_status"),
					"opt": r.get("optimizer_status") or r.get("status"),
					"conf": r.get("confidence"),
					"reason": reason,
					"scope_status": scope.get("status"),
					"dependency_type": scope.get("dependency_type") or r.get("dependency_type"),
					"local_poisons": len(scope.get("local_poisons") or []),
					"sql": r.get("sql_updates"),
				}
			)

	# READY samples
	ready_sample = []
	for r in ready[:15]:
		ready_sample.append(
			{
				"inbound": (r.get("inbound_document") or {}).get("voucher_no")
				if isinstance(r.get("inbound_document"), dict)
				else r.get("inbound_document"),
				"outbound": (r.get("outbound_document") or {}).get("voucher_no")
				if isinstance(r.get("outbound_document"), dict)
				else r.get("outbound_document"),
				"item": r.get("item") or r.get("item_code"),
				"ps": r.get("planner_status"),
				"sql": r.get("sql_updates"),
				"conf": r.get("confidence"),
			}
		)

	# Potential unlock: EXACT rows stuck in escalation/manual with batch isolation possible
	for r in escalation + manual:
		conf = str(r.get("confidence") or "")
		if conf != "EXACT":
			continue
		scope = r.get("scope") or {}
		if scope.get("can_isolate_batch") or r.get("batch") or r.get("batch_no"):
			unlock_candidates.append(
				{
					"item": r.get("item") or r.get("item_code"),
					"warehouse": r.get("warehouse"),
					"batch": r.get("batch") or r.get("batch_no"),
					"ps": r.get("planner_status"),
					"reason": (scope.get("escalation_reason") or r.get("reason") or "")[:120],
					"opt": r.get("optimizer_status") or r.get("status"),
				}
			)
		if len(unlock_candidates) >= 25:
			break

	out = {
		"n_rows": len(rows),
		"n_stamped": len(stamped),
		"by_planner_status": dict(by_ps.most_common()),
		"by_optimizer_status": dict(by_opt.most_common(25)),
		"by_confidence": dict(by_conf.most_common()),
		"n_ready": len(ready),
		"n_escalation": len(escalation),
		"n_shortage": len(shortage),
		"n_ambiguous": len(ambiguous),
		"n_manual": len(manual),
		"n_waiting": len(waiting),
		"escalation_reasons": dict(esc_reasons.most_common(20)),
		"escalation_samples": esc_samples,
		"ready_sample": ready_sample,
		"unlock_candidates_n": len(unlock_candidates),
		"unlock_candidates": unlock_candidates[:15],
		"scan_keys": sorted(scan.keys()) if isinstance(scan, dict) else [],
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:14000])
	return out
