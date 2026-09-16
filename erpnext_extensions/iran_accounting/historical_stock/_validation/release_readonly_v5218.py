# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only release validation + timings (no writes)."""

from __future__ import annotations

import json
from time import perf_counter

import frappe

from erpnext_extensions.iran_accounting.historical_stock.util import resolve_company


def run(company=None):
	company = resolve_company(company)
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan
	from erpnext_extensions.iran_accounting.historical_stock.master_plan import build_master_repair_plan
	from erpnext_extensions.iran_accounting.historical_stock._validation.dashboard_consistency_v5214 import (
		collect_kpi_matrix,
	)
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover

	timings = {}

	def timed(name, fn):
		t0 = perf_counter()
		out = fn()
		timings[name] = round(perf_counter() - t0, 2)
		return out

	full = timed("scan_all", lambda: run_full_integrity_scan(company=company, include_manufacture=False))
	dash = full.get("dashboard") or {}
	matrix = timed("validate_dashboard", lambda: collect_kpi_matrix(company=company))
	plan_err = None
	plan = None
	try:
		plan = timed("master_plan", lambda: build_master_repair_plan(company=company))
	except Exception as e:
		plan_err = f"{type(e).__name__}: {e}"
	timed("posting_order_scan", lambda: run_full_history_scan(company=company))
	timed("wrong_rate_scan", lambda: scan_wrong_rates(company=company, limit=2000))
	timed("zero_rate_scan", lambda: scan_zero_rate_rows(company=company))
	timed("i4_scan", lambda: scan_i4_leftover(company=company, limit=5000))

	spot = {}
	for v in (
		"MAT-STE-2026-25791",
		"MAT-STE-2026-03112",
		"MAT-STE-2026-36677",
		"MAT-STE-2026-03120",
	):
		spot[v] = bool(frappe.db.exists("Stock Entry", v))

	# Farvardin item chain presence (read-only)
	item_exists = bool(frappe.db.exists("Item", "30300042"))

	return {
		"company": company,
		"page": bool(frappe.db.exists("Page", "historical-repair")),
		"dashboard": {
			k: dash.get(k)
			for k in (
				"Repairable",
				"Patient Zero",
				"Posting Order",
				"Wrong Rate",
				"Wrong Rate READY",
				"Zero Rate",
				"I4 Leftover",
				"READY_I4",
				"Failed RIV",
				"Broken GL",
				"GL READY",
				"Integrity Score",
			)
		},
		"validate": {
			"pass_count": matrix.get("pass_count"),
			"fail_count": matrix.get("fail_count"),
			"all_pass": matrix.get("all_pass"),
		},
		"master_plan_error": plan_err,
		"master_plan_classes": [
			{
				"repair_class": c.get("repair_class"),
				"READY": c.get("READY"),
				"WAITING": c.get("WAITING"),
				"promotion_status": c.get("promotion_status"),
			}
			for c in (plan or {}).get("classes") or []
		],
		"spot_vouchers": spot,
		"item_30300042": item_exists,
		"timings_s": timings,
	}
