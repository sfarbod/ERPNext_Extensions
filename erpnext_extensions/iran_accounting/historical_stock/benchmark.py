# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only Historical Repair benchmark (development.localhost)."""

from __future__ import annotations

import resource
import time
from datetime import datetime

import frappe


def _rss_kb() -> int:
	try:
		with open("/proc/self/status") as fh:
			for line in fh:
				if line.startswith("VmRSS:"):
					return int(line.split()[1])
	except Exception:
		pass
	return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _hwm_kb() -> int:
	try:
		with open("/proc/self/status") as fh:
			for line in fh:
				if line.startswith("VmHWM:"):
					return int(line.split()[1])
	except Exception:
		pass
	return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _db_counters() -> dict:
	rows = frappe.db.sql(
		"""
		SHOW SESSION STATUS WHERE Variable_name IN
		('Questions','Slow_queries','Bytes_received','Bytes_sent','Created_tmp_disk_tables')
		""",
		as_dict=True,
	)
	return {r.Variable_name: int(r.Value or 0) for r in rows}


def _measure(name, fn) -> dict:
	start = datetime.now()
	cpu0 = resource.getrusage(resource.RUSAGE_SELF)
	db0 = _db_counters()
	mem0 = _rss_kb()
	t0 = time.perf_counter()
	result = fn()
	elapsed = time.perf_counter() - t0
	cpu1 = resource.getrusage(resource.RUSAGE_SELF)
	db1 = _db_counters()
	end = datetime.now()
	rows = 0
	if isinstance(result, dict):
		rows = int(result.get("count") or result.get("sle") or len(result.get("rows") or result.get("applied") or result.get("nodes") or []) or 0)
		if not rows and result.get("stock_entries"):
			rows = int(result.get("stock_entries") or 0)
	return {
		"name": name,
		"start": start.isoformat(timespec="seconds"),
		"end": end.isoformat(timespec="seconds"),
		"elapsed": round(elapsed, 4),
		"rows": rows,
		"rows_per_sec": round(rows / elapsed, 2) if elapsed else 0,
		"memory_kb": _rss_kb(),
		"peak_memory_kb": _hwm_kb(),
		"memory_delta_kb": _rss_kb() - mem0,
		"cpu_user_s": round(cpu1.ru_utime - cpu0.ru_utime, 4),
		"cpu_sys_s": round(cpu1.ru_stime - cpu0.ru_stime, 4),
		"python_time_s": round(elapsed, 4),
		"mariadb_questions": db1.get("Questions", 0) - db0.get("Questions", 0),
		"mariadb_bytes_sent": db1.get("Bytes_sent", 0) - db0.get("Bytes_sent", 0),
		"mariadb_tmp_disk_tables": db1.get("Created_tmp_disk_tables", 0) - db0.get("Created_tmp_disk_tables", 0),
		"writes": False,
	}


def run_historical_benchmark(company=None) -> dict:
	"""Measure Scan / Dry Run / planners. Never writes. Never global RIV."""
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity
	from erpnext_extensions.iran_accounting.historical_stock.impact import plan_repair_impact
	from erpnext_extensions.iran_accounting.historical_stock.integrity import voucher_integrity
	from erpnext_extensions.iran_accounting.historical_stock.repost import preview_repost_selected
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.stock_posting_order.api import dry_run_posting_order_repair
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

	FARVARDIN = {
		"item": "30300042",
		"warehouse": None,
		"batch": "504135-30300042-AK264401A11",
		"voucher": "MAT-STE-2026-25824-1",
	}
	ops = []
	ops.append(_measure("Full Scan", lambda: run_full_integrity_scan(company=company, include_manufacture=False)))
	ops.append(_measure("Wrong Rate Scan", lambda: scan_wrong_rates(company=company, limit=2000)))
	ops.append(_measure("Posting Order Scan", lambda: run_full_history_scan(company=company)))
	ops.append(_measure("Zero Rate Scan", lambda: scan_zero_rate_rows(company=company)))
	ops.append(
		_measure(
			"Replay Planner",
			lambda: plan_repair_impact(
				[
					{
						"voucher": FARVARDIN["voucher"],
						"item": FARVARDIN["item"],
						"batch": FARVARDIN["batch"],
						"confidence": "EXACT",
						"eligible": True,
					}
				]
			),
		)
	)

	def _identity_replay_plan():
		from erpnext_extensions.iran_accounting.historical_stock.downstream_replay import (
			replay_downstream_for_rows,
		)

		return replay_downstream_for_rows(
			[
				{
					"item": FARVARDIN["item"],
					"batch": FARVARDIN["batch"],
					"inbound_document": "MAT-STE-2026-25824-1",
					"outbound_document": "MAT-STE-2026-25825",
				}
			],
			dry_run=True,
		)

	ops.append(_measure("Identity Replay", _identity_replay_plan))
	ops.append(_measure("Bin Rebuild", lambda: scan_sle_bin(company=company, limit=500)))
	ops.append(_measure("GL Rebuild", lambda: scan_gl_integrity(company=company, limit=80)))
	ops.append(
		_measure(
			"Selective RIV",
			lambda: preview_repost_selected(FARVARDIN["item"], _warehouse_for(FARVARDIN["item"], FARVARDIN["voucher"])),
		)
	)
	ops.append(_measure("Dry Run", lambda: dry_run_posting_order_repair(company=company)))
	ops.append(
		_measure(
			"Integrity",
			lambda: {
				"count": 4,
				"rows": [
					voucher_integrity("MAT-STE-2026-25824-1"),
					voucher_integrity("MAT-STE-2026-25825"),
					voucher_integrity("MAT-STE-2026-25912"),
					voucher_integrity("MAT-STE-2026-25911-1"),
				],
			},
		)
	)
	return {
		"site": frappe.local.site,
		"start": ops[0]["start"] if ops else None,
		"end": ops[-1]["end"] if ops else None,
		"elapsed": round(sum(o["elapsed"] for o in ops), 4),
		"operations": ops,
		"writes": False,
		"global_riv": False,
	}


def _warehouse_for(item, voucher) -> str:
	wh = frappe.db.get_value(
		"Stock Ledger Entry",
		{"voucher_no": voucher, "item_code": item, "is_cancelled": 0},
		"warehouse",
	)
	return wh or frappe.db.get_value("Stock Entry Detail", {"parent": voucher, "item_code": item}, "t_warehouse")
