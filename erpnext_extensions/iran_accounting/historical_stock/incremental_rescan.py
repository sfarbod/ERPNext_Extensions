# Copyright (c) 2026, ERPNext Extensions contributors
"""Incremental Historical Repair rescan — update snapshot without Scan All.

After repairing one root chain, prefer these focused rescans over a full
ledger Scan All.
"""

from __future__ import annotations

from time import perf_counter

import frappe
from frappe.utils import cint

from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
	load_metrics_snapshot,
	patch_metrics_snapshot,
	save_metrics_snapshot,
)


def rescan_item_warehouse(company=None, item_code=None, warehouse=None, limit=500) -> dict:
	"""Rescan Wrong/Zero/I1/I4/SLE-Bin surfaces for one Item+Warehouse."""
	if not item_code or not warehouse:
		frappe.throw("item_code and warehouse are required")
	t0 = perf_counter()
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import scan_i1_negative_rate
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from frappe.utils import nowdate

	zero = scan_zero_rate_rows(company=company, item_code=item_code, warehouse=warehouse)
	wrong = scan_wrong_rates(company=company, item_code=item_code, warehouse=warehouse, limit=cint(limit) or 500)
	sle = scan_sle_bin(company=company, item_code=item_code, warehouse=warehouse, limit=cint(limit) or 500)
	i1 = scan_i1_negative_rate(company=company, item_code=item_code, warehouse=warehouse, limit=cint(limit) or 500)
	i4 = scan_i4_leftover(
		company=company,
		item_code=item_code,
		warehouse=warehouse,
		from_date="2026-03-21",
		to_date=str(nowdate()),
		limit=cint(limit) or 500,
	)
	# Patch only the affected headline chips when we can derive them cheaply.
	updates = {}
	snap = load_metrics_snapshot(company) or {"dashboard": {}}
	# Keep prior global KPIs; annotate incremental evidence in extra via patch source.
	result = {
		"ok": True,
		"scope": {"company": company, "item_code": item_code, "warehouse": warehouse},
		"zero_rate": {"count": zero.get("count"), "rows": len(zero.get("rows") or [])},
		"wrong_rate": {"count": wrong.get("count"), "rows": len(wrong.get("rows") or [])},
		"sle_bin": {"count": sle.get("count"), "bin": len(sle.get("bin_mismatches") or [])},
		"i1": {"count": i1.get("count"), "rows": len(i1.get("rows") or [])},
		"i4": {"count": i4.get("count"), "rows": len(i4.get("rows") or [])},
		"elapsed_s": round(perf_counter() - t0, 3),
		"snapshot": patch_metrics_snapshot(company, updates, source="rescan_item_warehouse"),
	}
	return result


def rescan_voucher(company=None, voucher=None) -> dict:
	if not voucher:
		frappe.throw("voucher is required")
	t0 = perf_counter()
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

	zero = scan_zero_rate_rows(company=company, voucher=voucher)
	wrong = scan_wrong_rates(company=company, voucher=voucher, limit=200)
	snap = patch_metrics_snapshot(company, {}, source="rescan_voucher")
	return {
		"ok": True,
		"voucher": voucher,
		"zero_rate_rows": len(zero.get("rows") or []),
		"wrong_rate_rows": len(wrong.get("rows") or []),
		"elapsed_s": round(perf_counter() - t0, 3),
		"snapshot": snap,
	}


def rescan_batch(company=None, item_code=None, batch=None, warehouse=None) -> dict:
	if not item_code or not batch:
		frappe.throw("item_code and batch are required")
	return rescan_item_warehouse(company=company, item_code=item_code, warehouse=warehouse)


def rescan_dependency_closure(company=None, voucher=None, item_code=None, warehouse=None) -> dict:
	"""Rescan the repair dependency neighborhood of a voucher / identity."""
	t0 = perf_counter()
	parts = []
	if item_code and warehouse:
		parts.append(rescan_item_warehouse(company=company, item_code=item_code, warehouse=warehouse))
	if voucher:
		parts.append(rescan_voucher(company=company, voucher=voucher))
	# Refresh blocker lanes for company (cheap).
	try:
		from erpnext_extensions.iran_accounting.historical_stock.blockers import sync_blockers_from_scan

		# Prefer lightweight blocker recount if sync is heavy — count only.
		if frappe.db.exists("DocType", "Historical Repair Blocker"):
			ua = frappe.db.count(
				"Historical Repair Blocker",
				{"lane": "USER_ACTION_REQUIRED", "status": ("!=", "RESOLVED"), **({"company": company} if company else {})},
			)
			tl = frappe.db.count(
				"Historical Repair Blocker",
				{"lane": "TOOL_LIMIT", "status": ("!=", "RESOLVED"), **({"company": company} if company else {})},
			)
			patch_metrics_snapshot(
				company,
				{"User Action Required": ua, "Tool Limit": tl},
				source="rescan_dependency_closure",
			)
	except Exception:
		pass
	return {
		"ok": True,
		"parts": parts,
		"elapsed_s": round(perf_counter() - t0, 3),
		"snapshot": load_metrics_snapshot(company),
	}


def rescan_root(root_id=None, company=None, voucher=None, item_code=None, warehouse=None) -> dict:
	"""Alias for closure rescan keyed by root/voucher."""
	return rescan_dependency_closure(
		company=company,
		voucher=voucher or root_id,
		item_code=item_code,
		warehouse=warehouse,
	)


def seed_snapshot_from_scan(company=None) -> dict:
	"""Run one full Scan All synchronously and persist snapshot (admin/bench use)."""
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan

	t0 = perf_counter()
	result = run_full_integrity_scan(company=company, include_manufacture=False)
	snap = save_metrics_snapshot(
		company=company,
		dashboard=(result or {}).get("dashboard") or {},
		timing=(result or {}).get("timing") or {},
		source="seed_sync_scan",
		extra={"elapsed_s": round(perf_counter() - t0, 3)},
	)
	return {"ok": True, "snapshot": snap, "timing": (result or {}).get("timing")}
