# Copyright (c) 2026, ERPNext Extensions contributors
"""Whitelisted Historical Stock Integrity & Repair APIs. Default dry_run=True."""

from __future__ import annotations

import json

import frappe
from frappe.utils import cint

from erpnext_extensions.iran_accounting.historical_stock.failed_riv import retry_failed_riv, scan_failed_riv
from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import (
	classify_stock_entry_gl,
	rebuild_gl_for_voucher,
	scan_gl_integrity,
)
from erpnext_extensions.iran_accounting.historical_stock.integrity import chain_integrity, voucher_integrity
from erpnext_extensions.iran_accounting.historical_stock.manufacture import scan_manufacture_anomalies
from erpnext_extensions.iran_accounting.historical_stock.reconstruct import (
	repair_manufacture_selected,
	repair_wrong_rate_selected,
	repair_zero_rate_selected,
)
from erpnext_extensions.iran_accounting.historical_stock.repost import preview_repost_selected, repost_selected
from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan
from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin
from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
from erpnext_extensions.iran_accounting.stock_posting_order.api import (
	dry_run_posting_order_repair,
	posting_order_integrity_check,
	repair_posting_order_selected,
	scan_posting_order_anomalies,
)


from erpnext_extensions.iran_accounting.historical_stock.permissions import (
	require_admin,
	require_read,
	require_repair,
	require_write_if_applying,
)


def _parse(value):
	if isinstance(value, str):
		value = value.strip()
		if not value:
			return []
		try:
			return json.loads(value)
		except json.JSONDecodeError:
			return [value]
	return value or []


def _guard():
	require_read()


def _guard_repair():
	require_repair()


def _guard_admin():
	require_admin()


def _dry(value, default=True) -> bool:
	if value in (None, ""):
		return default
	if isinstance(value, bool):
		return value
	return bool(cint(value))


@frappe.whitelist()
def session_info_api():
	from erpnext_extensions.iran_accounting.historical_stock.permissions import session_info

	require_read()
	return session_info()


@frappe.whitelist()
def scan_all(company=None):
	_guard()
	return run_full_integrity_scan(company=company or None)


@frappe.whitelist()
def scan_zero_rates(company=None, voucher=None):
	_guard()
	return scan_zero_rate_rows(company=company or None, voucher=voucher or None)


@frappe.whitelist()
def dry_run_zero_rates(rows=None, company=None, voucher=None):
	_guard()
	parsed = _parse(rows)
	if parsed:
		from erpnext_extensions.iran_accounting.historical_stock.zero_rate import classify_zero_row
		from erpnext_extensions.iran_accounting.historical_stock.reconstruct import _load_detail

		out = []
		for raw in parsed:
			try:
				out.append(classify_zero_row(_load_detail(raw)))
			except Exception as exc:
				out.append({**raw, "status": "BLOCKED", "error": str(exc)})
		return {"dry_run": True, "count": len(out), "rows": out, "eligible": [r for r in out if r.get("eligible")]}
	return scan_zero_rate_rows(company=company or None, voucher=voucher or None)


@frappe.whitelist()
def repair_zero_rates_selected(rows=None, dry_run=True):
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	parsed = _parse(rows)
	if not parsed:
		frappe.throw("Exact document rows are required")
	return repair_zero_rate_selected(parsed, dry_run=is_dry)


@frappe.whitelist()
def scan_wrong_rates_api(company=None, voucher=None, item_code=None, warehouse=None, batch=None, from_date=None, to_date=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

	return scan_wrong_rates(
		company=company or None,
		voucher=voucher or None,
		item_code=item_code or None,
		warehouse=warehouse or None,
		batch=batch or None,
		from_date=from_date or None,
		to_date=to_date or None,
	)


@frappe.whitelist()
def repair_wrong_rates_selected(rows=None, dry_run=True):
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	parsed = _parse(rows)
	if not parsed:
		frappe.throw("Exact document rows are required")
	return repair_wrong_rate_selected(parsed, dry_run=is_dry)


@frappe.whitelist()
def scan_manufacture(company=None, voucher=None):
	_guard()
	return scan_manufacture_anomalies(company=company or None, voucher=voucher or None)


@frappe.whitelist()
def dry_run_manufacture(rows=None, company=None):
	_guard()
	parsed = _parse(rows)
	if parsed:
		from erpnext_extensions.iran_accounting.historical_stock.manufacture import preview_manufacture_voucher

		out = [preview_manufacture_voucher(r.get("voucher") or r.get("voucher_no") or r) for r in parsed]
		return {"dry_run": True, "rows": out, "count": len(out)}
	return scan_manufacture_anomalies(company=company or None)


@frappe.whitelist()
def repair_manufacture_selected_api(rows=None, dry_run=True):
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	parsed = _parse(rows)
	if not parsed:
		frappe.throw("Exact document rows are required")
	return repair_manufacture_selected(parsed, dry_run=is_dry)


@frappe.whitelist()
def scan_sle_bin_api(company=None, item_code=None, warehouse=None):
	_guard()
	return scan_sle_bin(company=company or None, item_code=item_code or None, warehouse=warehouse or None)


@frappe.whitelist()
def scan_gl_api(company=None):
	_guard()
	return scan_gl_integrity(company=company or None)


@frappe.whitelist()
def rebuild_gl_selected(vouchers=None, dry_run=True):
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	parsed = _parse(vouchers)
	if not parsed:
		frappe.throw("Exact vouchers are required")
	out = [rebuild_gl_for_voucher(v if isinstance(v, str) else v.get("voucher"), dry_run=is_dry) for v in parsed]
	return {"dry_run": is_dry, "rows": out}


@frappe.whitelist()
def classify_gl_voucher(voucher_no):
	_guard()
	return classify_stock_entry_gl(voucher_no)


@frappe.whitelist()
def scan_failed_riv_api():
	_guard()
	return scan_failed_riv()


@frappe.whitelist()
def retry_failed_riv_selected(names=None, dry_run=True):
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	parsed = _parse(names)
	if not parsed:
		frappe.throw("Exact RIV names are required")
	out = []
	for name in parsed:
		riv = name if isinstance(name, str) else name.get("riv_name") or name.get("name")
		out.append(retry_failed_riv(riv, dry_run=is_dry))
	return {"dry_run": is_dry, "rows": out}


@frappe.whitelist()
def preview_repost(item_code=None, warehouse=None, batch=None, from_date=None):
	_guard()
	return preview_repost_selected(item_code, warehouse, batch=batch or None, from_date=from_date or None)


@frappe.whitelist()
def repost_selected_api(item_code=None, warehouse=None, from_date=None, dry_run=True):
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	return repost_selected(item_code, warehouse, from_date=from_date or None, dry_run=is_dry)


@frappe.whitelist()
def integrity_api(vouchers=None, item_code=None, warehouse=None):
	_guard()
	parsed = _parse(vouchers)
	if item_code and warehouse:
		return chain_integrity(item_code, warehouse, parsed)
	out = [voucher_integrity(v) for v in parsed]
	return {"rows": out}


@frappe.whitelist()
def resume_last_run(topic=None):
	_guard_repair()
	from erpnext_extensions.iran_accounting.historical_stock.audit import last_open_run

	log = last_open_run(topic)
	if not log:
		return {"status": "NONE"}
	return {"repair_run_id": log.repair_run_id, "status": log.status, "cursor": log.resume_cursor, "topic": log.topic}


@frappe.whitelist()
def rebuild_affected_documents_api(rows=None, dry_run=True):
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import rebuild_affected_documents

	parsed = _parse(rows)
	if not parsed:
		frappe.throw("Exact inbound/outbound rows are required")
	return rebuild_affected_documents(parsed, dry_run=is_dry)


@frappe.whitelist()
def replay_downstream_api(rows=None, dry_run=True):
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	from erpnext_extensions.iran_accounting.historical_stock.downstream_replay import (
		replay_downstream_for_rows,
	)

	parsed = _parse(rows)
	if not parsed:
		frappe.throw("Exact item/batch chain rows are required")
	return replay_downstream_for_rows(parsed, dry_run=is_dry)


@frappe.whitelist()
def selective_repair_api(scope=None, op="replay", dry_run=True):
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	from erpnext_extensions.iran_accounting.historical_stock.selective import selective_pipeline

	parsed = scope if isinstance(scope, dict) else _parse(scope)
	if isinstance(parsed, list):
		parsed = parsed[0] if parsed else {}
	return selective_pipeline(parsed or {}, op=op or "replay", dry_run=is_dry)


@frappe.whitelist()
def repair_graph_api(item=None, batch=None, work_order=None, voucher=None, warehouse=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.graph import repair_graph

	return repair_graph(item=item or None, batch=batch or None, work_order=work_order or None, voucher=voucher or None, warehouse=warehouse or None)


@frappe.whitelist()
def repair_history_api():
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.audit import list_runs

	return {"rows": list_runs()}


@frappe.whitelist()
def rollback_run_api(repair_run_id=None, dry_run=True):
	_guard_admin()
	from erpnext_extensions.iran_accounting.historical_stock.audit import rollback_run

	return rollback_run(repair_run_id, dry_run=_dry(dry_run, True))


@frappe.whitelist()
def impact_analysis_api(rows=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.impact import plan_repair_impact

	return plan_repair_impact(_parse(rows))


@frappe.whitelist()
def preview_reconstruction_api(row=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.expected import preview_reconstruction

	parsed = row if isinstance(row, dict) else (_parse(row) or {})
	if isinstance(parsed, list):
		parsed = parsed[0] if parsed else {}
	return preview_reconstruction(parsed or {})


@frappe.whitelist()
def historical_benchmark_api(company=None):
	_guard_admin()
	from erpnext_extensions.iran_accounting.historical_stock.benchmark import run_historical_benchmark

	return run_historical_benchmark(company=company or None)


@frappe.whitelist()
def export_xlsx_api(topic=None, headers=None, rows=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.export import xlsx_bytes
	import base64

	hdrs = _parse(headers) or []
	body = _parse(rows) or []
	content = xlsx_bytes([str(h) for h in hdrs], body)
	return {
		"filename": f"historical-repair-{topic or 'export'}.xlsx",
		"filedata": base64.b64encode(content).decode(),
		"content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
	}


# Re-export posting-order APIs so the page can use one namespace.
scan_posting_order = scan_posting_order_anomalies
dry_run_posting_order = dry_run_posting_order_repair
repair_posting_order = repair_posting_order_selected
posting_order_integrity = posting_order_integrity_check
