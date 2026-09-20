# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5C — migrate blocker keys + sync; Patient Zero breakdown (read-only metrics)."""

from __future__ import annotations

import json
from collections import Counter, defaultdict

COMPANY = "اسپاد فارمد دارو"


def run(*, sync: int = 1):
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock.blockers import (
		BLOCKER_KEY_MAX_LEN,
		build_blocker_key,
		legacy_blocker_key,
		scan_and_sync_blockers,
	)

	# --- Migrate existing blockers to canonical keys ---
	rows = frappe.get_all(
		"Historical Repair Blocker",
		fields=[
			"name",
			"blocker_key",
			"lane",
			"issue_type",
			"item_code",
			"warehouse",
			"batch_no",
			"voucher_no",
			"company",
			"dependency_root",
			"payload_json",
		],
		limit_page_length=5000,
	)
	migrated = 0
	skipped = 0
	collisions = []
	seen_keys = {}
	for r in rows:
		new_key = build_blocker_key(
			issue_type=r.issue_type,
			item_code=r.item_code,
			warehouse=r.warehouse,
			batch_no=r.batch_no,
			voucher_no=r.voucher_no,
			lane=r.lane,
			company=r.company or COMPANY,
			dependency_root=r.dependency_root,
		)
		if len(new_key) > BLOCKER_KEY_MAX_LEN:
			collisions.append({"name": r.name, "error": "new_key_too_long", "key": new_key})
			continue
		if new_key in seen_keys and seen_keys[new_key] != r.name:
			# Duplicate logical roots — keep newest, mark older resolved-ish note in payload
			collisions.append(
				{
					"name": r.name,
					"error": "duplicate_canonical",
					"other": seen_keys[new_key],
					"key": new_key,
				}
			)
			# Still migrate if current key differs and no DB unique conflict
		seen_keys[new_key] = r.name
		if r.blocker_key == new_key:
			skipped += 1
			continue
		# Unique constraint: if another doc already has new_key, skip rename
		other = frappe.db.get_value(
			"Historical Repair Blocker", {"blocker_key": new_key}, "name"
		)
		if other and other != r.name:
			collisions.append({"name": r.name, "error": "unique_conflict", "other": other})
			continue
		legacy = r.blocker_key
		frappe.db.set_value(
			"Historical Repair Blocker",
			r.name,
			"blocker_key",
			new_key,
			update_modified=False,
		)
		# Append legacy into payload_json
		try:
			pj = frappe.parse_json(r.payload_json) if r.payload_json else {}
			if not isinstance(pj, dict):
				pj = {}
		except Exception:
			pj = {}
		pj["legacy_blocker_key"] = legacy
		pj["migrated_to_canonical"] = True
		frappe.db.set_value(
			"Historical Repair Blocker",
			r.name,
			"payload_json",
			frappe.as_json(pj),
			update_modified=False,
		)
		migrated += 1
	frappe.db.commit()

	sync_result = None
	if int(sync):
		sync_result = scan_and_sync_blockers(company=COMPANY, include_tool_limits=True, limit=800)
		frappe.db.commit()

	ua = frappe.db.count(
		"Historical Repair Blocker", {"lane": "USER_ACTION_REQUIRED", "status": ("!=", "RESOLVED")}
	)
	tl = frappe.db.count(
		"Historical Repair Blocker", {"lane": "TOOL_LIMIT", "status": ("!=", "RESOLVED")}
	)
	# Length check
	max_len = frappe.db.sql(
		"SELECT MAX(CHAR_LENGTH(blocker_key)) FROM `tabHistorical Repair Blocker`"
	)[0][0]

	# Patient Zero breakdown from Wrong + Zero + PO + I4 lightweight scans
	pz = _patient_zero_breakdown()

	out = {
		"migration": {
			"rows": len(rows),
			"migrated": migrated,
			"already_canonical": skipped,
			"collisions": collisions[:20],
			"collision_n": len(collisions),
		},
		"sync": {
			"created": (sync_result or {}).get("created"),
			"updated": (sync_result or {}).get("updated"),
			"count": (sync_result or {}).get("count"),
			"user_action_required": (sync_result or {}).get("user_action_required"),
			"tool_limit": (sync_result or {}).get("tool_limit"),
			"shortage_summary": (sync_result or {}).get("shortage_summary"),
			"by_issue_type": (sync_result or {}).get("by_issue_type"),
			"by_lane": (sync_result or {}).get("by_lane"),
		}
		if sync_result
		else None,
		"dashboard_counts": {
			"USER_ACTION_REQUIRED": ua,
			"TOOL_LIMIT": tl,
			"max_blocker_key_len": max_len,
		},
		"patient_zero": pz,
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out


def _patient_zero_breakdown() -> dict:
	import frappe
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover

	pz_roots = set()
	by_family = Counter()
	pz_findings = 0
	samples = defaultdict(list)

	def _add(family, row):
		nonlocal pz_findings
		pz = row.get("patient_zero") or row.get("root_patient_zero") or row.get("dependency_root")
		if isinstance(pz, dict):
			pz_v = pz.get("voucher_no") or pz.get("voucher")
		else:
			pz_v = pz
		if not pz_v:
			# WAITING_* planner often encodes PZ
			ps = str(row.get("planner_status") or "")
			if "WAITING" in ps or "PATIENT" in ps:
				pz_v = row.get("voucher") or row.get("voucher_no") or row.get("outbound_document")
			else:
				return
		pz_findings += 1
		pz_roots.add(str(pz_v))
		by_family[family] += 1
		if len(samples[family]) < 3:
			samples[family].append(
				{
					"pz": pz_v,
					"voucher": row.get("voucher") or row.get("voucher_no") or row.get("outbound_document"),
					"purpose": row.get("purpose"),
					"item": row.get("item") or row.get("item_code"),
					"planner": row.get("planner_status"),
				}
			)

	wr = scan_wrong_rates(company=COMPANY, limit=6000)
	for r in wr.get("rows") or []:
		r = attach_plan(dict(r))
		purpose = r.get("purpose") or ""
		ps = str(r.get("planner_status") or "")
		if purpose in ("Material Transfer", "Material Transfer for Manufacture", "Send to Subcontractor"):
			fam = "TRANSFER"
		elif purpose == "Manufacture":
			fam = "MANUFACTURE"
		elif purpose == "Material Receipt":
			fam = "MATERIAL_RECEIPT_USER"
		elif "WAITING" in ps or r.get("patient_zero"):
			fam = "WRONG_RATE"
		else:
			continue
		if r.get("patient_zero") or "WAITING" in ps or "PATIENT" in ps:
			_add(fam, r)

	zr = scan_zero_rate_rows(company=COMPANY, limit=4000)
	for r in zr.get("rows") or []:
		r = attach_plan(dict(r))
		ps = str(r.get("planner_status") or r.get("status") or "")
		if r.get("patient_zero") or "WAITING" in ps:
			_add("ZERO_RATE", r)

	po = run_full_history_scan(company=COMPANY)
	for raw in po.get("rows") or []:
		r = attach_plan(dict(raw))
		opt = str(r.get("optimizer_status") or r.get("status") or "")
		ps = str(r.get("planner_status") or "")
		if opt in ("REAL_STOCK_SHORTAGE", "INSUFFICIENT_STOCK"):
			_add("NEGATIVE_STOCK", {**r, "patient_zero": r.get("outbound_document")})
		elif "WAITING" in ps:
			_add("POSTING_ORDER", r)

	try:
		i4 = scan_i4_leftover(company=COMPANY, limit=200)
		for r in i4.get("rows") or []:
			r = attach_plan(dict(r))
			if r.get("patient_zero") or "WAITING" in str(r.get("planner_status") or ""):
				_add("I4", r)
	except Exception as exc:
		samples["I4_ERROR"] = [{"error": str(exc)[:200]}]

	# Dashboard "Patient Zero" metric source
	dash_pz = None
	try:
		from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
			get_dashboard_summary,
		)

		dash = get_dashboard_summary(COMPANY) or {}
		dash_pz = (dash.get("dashboard") or {}).get("Patient Zero")
	except Exception as exc:
		dash_pz = f"error:{exc}"

	return {
		"dashboard_patient_zero": dash_pz,
		"PZ_FINDINGS": pz_findings,
		"PZ_UNIQUE_ROOTS": len(pz_roots),
		"PZ_BY_FAMILY": dict(by_family.most_common()),
		"samples": {k: v for k, v in samples.items()},
	}
