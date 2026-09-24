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
from erpnext_extensions.iran_accounting.historical_stock.sle_gl_drift import (
	classify_sle_gl_drift,
	dry_run_sle_gl_drift,
	repair_sle_gl_drift_selected,
	scan_sle_gl_drift,
)
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
	"""Synchronous Scan All (compat / small tenants). Prefer start_scan_all_job on UI."""
	_guard()
	# Dashboard Scan All skips manufacture get_doc loops; Manufacture tab still scans itself.
	result = run_full_integrity_scan(company=company or None, include_manufacture=False)
	try:
		from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
			save_metrics_snapshot,
		)

		save_metrics_snapshot(
			company=company or None,
			dashboard=(result or {}).get("dashboard") or {},
			timing=(result or {}).get("timing") or {},
			source="scan_all_sync",
		)
	except Exception:
		pass
	return result


@frappe.whitelist()
def start_scan_all_job(company=None, force=0):
	"""Enqueue Scan All on the long queue; returns immediately with job_id."""
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.scan_job import start_scan_all_job as _start

	return _start(company=company or None, force=force)


@frappe.whitelist()
def get_scan_all_job(job_id=None):
	"""Poll Scan All job status / result."""
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.scan_job import get_scan_all_job as _get

	if not job_id:
		frappe.throw("job_id required")
	return _get(job_id)


@frappe.whitelist()
def cancel_scan_all_job(job_id=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.scan_job import cancel_scan_all_job as _cancel

	if not job_id:
		frappe.throw("job_id required")
	return _cancel(job_id)


@frappe.whitelist()
def get_dashboard_summary_api(company=None):
	"""Fast page-load summary from metrics snapshot + worker state. No full scan."""
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
		get_dashboard_summary,
	)

	return get_dashboard_summary(company=company or None)


@frappe.whitelist()
def worker_status_api():
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.metrics_snapshot import (
		worker_queue_status,
	)

	return worker_queue_status("long")


@frappe.whitelist()
def rescan_item_warehouse_api(company=None, item_code=None, warehouse=None, limit=500):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.incremental_rescan import (
		rescan_item_warehouse,
	)

	return rescan_item_warehouse(
		company=company or None,
		item_code=item_code,
		warehouse=warehouse,
		limit=limit,
	)


@frappe.whitelist()
def rescan_voucher_api(company=None, voucher=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.incremental_rescan import (
		rescan_voucher,
	)

	return rescan_voucher(company=company or None, voucher=voucher)


@frappe.whitelist()
def rescan_root_api(company=None, root_id=None, voucher=None, item_code=None, warehouse=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.incremental_rescan import rescan_root

	return rescan_root(
		company=company or None,
		root_id=root_id,
		voucher=voucher,
		item_code=item_code,
		warehouse=warehouse,
	)


@frappe.whitelist()
def scan_zero_rates(
	company=None,
	voucher=None,
	item_code=None,
	warehouse=None,
	batch=None,
	serial_and_batch_bundle=None,
	work_order=None,
	from_date=None,
	to_date=None,
	repair_class=None,
	planner_status=None,
	patient_zero=None,
):
	_guard()
	return scan_zero_rate_rows(
		company=company or None,
		voucher=voucher or None,
		item_code=item_code or None,
		warehouse=warehouse or None,
		batch=batch or None,
		serial_and_batch_bundle=serial_and_batch_bundle or None,
		work_order=work_order or None,
		from_date=from_date or None,
		to_date=to_date or None,
		repair_class=repair_class or None,
		planner_status=planner_status or None,
		patient_zero=patient_zero or None,
	)


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
		from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

		return stamp_scan_result({"dry_run": True, "count": len(out), "rows": out})
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
def scan_wrong_rates_api(
	company=None,
	voucher=None,
	item_code=None,
	warehouse=None,
	batch=None,
	from_date=None,
	to_date=None,
	planner_status=None,
	kpi_bucket=None,
):
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
		planner_status=planner_status or None,
		kpi_bucket=kpi_bucket or None,
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
def scan_manufacture(
	company=None,
	voucher=None,
	item_code=None,
	warehouse=None,
	work_order=None,
	from_date=None,
	to_date=None,
):
	_guard()
	return scan_manufacture_anomalies(
		company=company or None,
		voucher=voucher or None,
		item_code=item_code or None,
		warehouse=warehouse or None,
		work_order=work_order or None,
		from_date=from_date or None,
		to_date=to_date or None,
	)


@frappe.whitelist()
def dry_run_manufacture(rows=None, company=None):
	_guard()
	parsed = _parse(rows)
	if parsed:
		from erpnext_extensions.iran_accounting.historical_stock.manufacture import preview_manufacture_voucher

		out = [preview_manufacture_voucher(r.get("voucher") or r.get("voucher_no") or r) for r in parsed]
		from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

		return stamp_scan_result({"dry_run": True, "rows": out, "count": len(out)})
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
def scan_sle_bin_api(
	company=None,
	item_code=None,
	warehouse=None,
	voucher=None,
	batch=None,
	serial_and_batch_bundle=None,
	work_order=None,
	from_date=None,
	to_date=None,
	repair_class=None,
	planner_status=None,
	patient_zero=None,
	kpi_bucket=None,
):
	_guard()
	result = scan_sle_bin(
		company=company or None,
		item_code=item_code or None,
		warehouse=warehouse or None,
		voucher=voucher or None,
		batch=batch or None,
		serial_and_batch_bundle=serial_and_batch_bundle or None,
		work_order=work_order or None,
		from_date=from_date or None,
		to_date=to_date or None,
		repair_class=repair_class or None,
		planner_status=planner_status or None,
		patient_zero=patient_zero or None,
	)
	if kpi_bucket:
		from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import filter_rows_by_kpi_bucket

		rows = filter_rows_by_kpi_bucket(result.get("rows") or [], kpi_bucket)
		result = dict(result)
		result["rows"] = rows
		result["count"] = len(rows)
		result["filtered_by"] = {"kpi_bucket": kpi_bucket}
	return result


@frappe.whitelist()
def scan_gl_api(
	company=None,
	voucher=None,
	item_code=None,
	warehouse=None,
	work_order=None,
	from_date=None,
	to_date=None,
	kpi_bucket=None,
):
	_guard()
	result = scan_gl_integrity(
		company=company or None,
		voucher=voucher or None,
		item_code=item_code or None,
		warehouse=warehouse or None,
		work_order=work_order or None,
		from_date=from_date or None,
		to_date=to_date or None,
	)
	if kpi_bucket:
		from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import filter_rows_by_kpi_bucket

		rows = filter_rows_by_kpi_bucket(result.get("rows") or [], kpi_bucket)
		result = dict(result)
		result["rows"] = rows
		result["count"] = len(rows)
		result["filtered_by"] = {"kpi_bucket": kpi_bucket}
	return result


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
def scan_sle_gl_drift_api(
	company=None,
	voucher=None,
	item_code=None,
	warehouse=None,
	from_date=None,
	to_date=None,
	vouchers=None,
	limit=500,
):
	"""Discover SLE↔GL drift. Does not write. False G0 transfers are included."""
	_guard()
	parsed = _parse(vouchers) if vouchers else None
	return scan_sle_gl_drift(
		company=company or None,
		voucher=voucher or None,
		item_code=item_code or None,
		warehouse=warehouse or None,
		from_date=from_date or None,
		to_date=to_date or None,
		vouchers=parsed,
		limit=cint(limit) or 500,
	)


@frappe.whitelist()
def classify_sle_gl_drift_api(voucher_no):
	_guard()
	return classify_sle_gl_drift(voucher_no)


@frappe.whitelist()
def dry_run_sle_gl_drift_api(vouchers=None, company=None, item_code=None, limit=500):
	_guard()
	parsed = _parse(vouchers) if vouchers else None
	return dry_run_sle_gl_drift(
		vouchers=parsed,
		company=company or None,
		item_code=item_code or None,
		limit=cint(limit) or 500,
	)


@frappe.whitelist()
def repair_sle_gl_drift_selected_api(
	vouchers=None,
	dry_run=True,
	batch_size=50,
	stop_on_error=True,
	resume_cursor=0,
):
	"""GL-only repair. Default dry_run=True. Never modifies SLE."""
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	_guard_repair()
	parsed = _parse(vouchers)
	if not parsed:
		frappe.throw("Exact vouchers are required for SLE_GL_DRIFT repair")
	rows = [{"voucher": v} if isinstance(v, str) else v for v in parsed]
	return repair_sle_gl_drift_selected(
		rows,
		dry_run=is_dry,
		batch_size=cint(batch_size) or 50,
		stop_on_error=_dry(stop_on_error, True) if not isinstance(stop_on_error, bool) else stop_on_error,
		resume_cursor=cint(resume_cursor) or 0,
	)


@frappe.whitelist()
def classify_gl_voucher(voucher_no):
	_guard()
	return classify_stock_entry_gl(voucher_no)


@frappe.whitelist()
def scan_failed_riv_api(
	item_code=None,
	warehouse=None,
	voucher=None,
	from_date=None,
	to_date=None,
	company=None,
	kpi_bucket=None,
):
	_guard()
	result = scan_failed_riv(
		item_code=item_code or None,
		warehouse=warehouse or None,
		voucher=voucher or None,
		from_date=from_date or None,
		to_date=to_date or None,
		company=company or None,
	)
	if kpi_bucket:
		from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import filter_rows_by_kpi_bucket

		rows = filter_rows_by_kpi_bucket(result.get("rows") or [], kpi_bucket)
		result = dict(result)
		result["rows"] = rows
		result["count"] = len(rows)
		result["filtered_by"] = {"kpi_bucket": kpi_bucket}
	return result


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
def preview_repost(
	item_code=None,
	warehouse=None,
	batch=None,
	from_date=None,
	to_date=None,
	work_order=None,
	voucher=None,
	company=None,
):
	_guard()
	return preview_repost_selected(
		item_code,
		warehouse,
		batch=batch or None,
		from_date=from_date or None,
		to_date=to_date or None,
		work_order=work_order or None,
		voucher=voucher or None,
		company=company or None,
	)


@frappe.whitelist()
def repost_selected_api(
	item_code=None,
	warehouse=None,
	from_date=None,
	to_date=None,
	batch=None,
	work_order=None,
	voucher=None,
	company=None,
	dry_run=True,
):
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	return repost_selected(
		item_code,
		warehouse,
		from_date=from_date or None,
		to_date=to_date or None,
		batch=batch or None,
		work_order=work_order or None,
		voucher=voucher or None,
		company=company or None,
		dry_run=is_dry,
	)


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
	_guard_admin()
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
def repair_graph_api(item=None, batch=None, work_order=None, voucher=None, warehouse=None, row=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.graph import repair_graph

	parsed = row if isinstance(row, dict) else (_parse(row) or None)
	if isinstance(parsed, list):
		parsed = parsed[0] if parsed else None
	return repair_graph(
		item=item or None,
		batch=batch or None,
		work_order=work_order or None,
		voucher=voucher or None,
		warehouse=warehouse or None,
		row=parsed,
	)


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
	from erpnext_extensions.iran_accounting.historical_stock.planner import plan_selection

	return plan_selection(_parse(rows))


@frappe.whitelist()
def repair_planner_api(rows=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.planner import plan_selection

	return plan_selection(_parse(rows))


@frappe.whitelist()
def resolve_dependencies_api(row=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.dependency import resolve_dependencies

	parsed = row if isinstance(row, dict) else (_parse(row) or {})
	if isinstance(parsed, list):
		parsed = parsed[0] if parsed else {}
	return resolve_dependencies(parsed or {})


@frappe.whitelist()
def repair_dependency_chain_api(row=None, dry_run=True):
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	from erpnext_extensions.iran_accounting.historical_stock.dependency import apply_dependency_chain

	parsed = row if isinstance(row, dict) else (_parse(row) or {})
	if isinstance(parsed, list):
		parsed = parsed[0] if parsed else {}
	if not parsed:
		frappe.throw("A dependency row is required")
	return apply_dependency_chain(parsed, dry_run=is_dry)


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


@frappe.whitelist()
def scan_i4_api(
	company=None,
	voucher=None,
	item_code=None,
	warehouse=None,
	batch=None,
	serial_and_batch_bundle=None,
	work_order=None,
	from_date=None,
	to_date=None,
):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover

	return scan_i4_leftover(
		company=company or None,
		voucher=voucher or None,
		item_code=item_code or None,
		warehouse=warehouse or None,
		batch=batch or None,
		serial_and_batch_bundle=serial_and_batch_bundle or None,
		work_order=work_order or None,
		from_date=from_date or None,
		to_date=to_date or None,
	)


@frappe.whitelist()
def dry_run_i4_api(rows=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import dry_run_i4_repair

	return dry_run_i4_repair(_parse(rows))


@frappe.whitelist()
def repair_i4_patient_zero_api(rows=None, dry_run=True):
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import repair_i4_selected

	parsed = _parse(rows)
	if not parsed:
		frappe.throw("Exact I4 rows are required")
	return repair_i4_selected(parsed, dry_run=is_dry)


@frappe.whitelist()
def find_patient_zero_api(voucher=None, item=None, warehouse=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import find_patient_zero_for_voucher

	if not voucher:
		frappe.throw("Voucher is required")
	return find_patient_zero_for_voucher(voucher, item=item or None, warehouse=warehouse or None)


@frappe.whitelist()
def root_cause_explorer_api(voucher=None, item=None, warehouse=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import root_cause_explorer

	if not voucher:
		frappe.throw("Voucher is required")
	return root_cause_explorer(voucher, item=item or None, warehouse=warehouse or None)


@frappe.whitelist()
def identity_health_api(item_code=None, warehouse=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import identity_health

	if not item_code or not warehouse:
		frappe.throw("Item and Warehouse are required")
	return identity_health(item_code, warehouse)


@frappe.whitelist()
def scan_i1_api(
	company=None,
	voucher=None,
	item_code=None,
	warehouse=None,
	work_order=None,
	from_date=None,
	to_date=None,
):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import scan_i1_negative_rate

	return scan_i1_negative_rate(
		company=company or None,
		voucher=voucher or None,
		item_code=item_code or None,
		warehouse=warehouse or None,
		work_order=work_order or None,
		from_date=from_date or None,
		to_date=to_date or None,
	)


@frappe.whitelist()
def dry_run_i1_api(rows=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import dry_run_i1_repair

	return dry_run_i1_repair(_parse(rows))


@frappe.whitelist()
def repair_i1_selected_api(rows=None, dry_run=True):
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import repair_i1_selected

	parsed = _parse(rows)
	if not parsed:
		frappe.throw("Exact I1 rows are required")
	return repair_i1_selected(parsed, dry_run=is_dry)


@frappe.whitelist()
def i1_root_cause_api(voucher=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.i1_repair import i1_root_cause

	if not voucher:
		frappe.throw("Voucher is required")
	return i1_root_cause(voucher)


@frappe.whitelist()
def scan_leftover_ma_api(company=None, item_code=None, warehouse=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import scan_leftover_ma

	return scan_leftover_ma(company=company or None, item_code=item_code or None, warehouse=warehouse or None)


@frappe.whitelist()
def dry_run_leftover_ma_api(rows=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import repair_leftover_ma_selected

	return repair_leftover_ma_selected(_parse(rows), dry_run=True)


@frappe.whitelist()
def repair_leftover_ma_selected_api(rows=None, dry_run=True):
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import repair_leftover_ma_selected

	parsed = _parse(rows)
	if not parsed:
		frappe.throw("Exact leftover-MA rows are required")
	return repair_leftover_ma_selected(parsed, dry_run=is_dry)


@frappe.whitelist()
def classify_zero_provenance_api(item_code=None, warehouse=None, posting_date=None, posting_time=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.zero_provenance import (
		classify_zero_provenance,
		provenance_as_of,
	)

	if not item_code or not warehouse:
		frappe.throw("item_code and warehouse are required")
	if posting_date:
		return provenance_as_of(item_code, warehouse, posting_date, posting_time)
	return classify_zero_provenance(item=item_code, warehouse=warehouse)


@frappe.whitelist()
def master_repair_plan_api(company=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.master_plan import build_master_repair_plan

	return build_master_repair_plan(company=company or None)


@frappe.whitelist()
def riv_preflight_api(item_code=None, warehouse=None, posting_date=None, company=None):
	"""Read-only RIV preflight + dependency-closure + impact preview (v5.3.0)."""
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.riv_preflight import riv_preflight_gate

	if not item_code or not warehouse:
		frappe.throw("item_code and warehouse are required")
	return riv_preflight_gate(
		item_code,
		warehouse,
		posting_date=posting_date or None,
		company=company or None,
	)


@frappe.whitelist()
def preview_repost_impact_api(item_code=None, warehouse=None, posting_date=None, company=None):
	"""Read-only repost impact preview (v5.3.0)."""
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.riv_preflight import preview_repost_impact

	if not item_code or not warehouse:
		frappe.throw("item_code and warehouse are required")
	return preview_repost_impact(
		item_code,
		warehouse,
		posting_date=posting_date or None,
		company=company or None,
	)


@frappe.whitelist()
def validate_dashboard_api(company=None):
	"""Compare Dashboard ↔ Scan ↔ Planner ↔ SQL ↔ Queue for every KPI (read-only)."""
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock._validation.dashboard_consistency_v5214 import (
		collect_kpi_matrix,
		audit_cache,
	)

	matrix = collect_kpi_matrix(company=company or None)
	return {
		"ok": True,
		"company": company,
		"pass_count": matrix.get("pass_count"),
		"fail_count": matrix.get("fail_count"),
		"all_pass": matrix.get("all_pass"),
		"matrix": matrix.get("matrix"),
		"dashboard": matrix.get("dashboard"),
		"queue_breakdown": matrix.get("queue_breakdown"),
		"i4_by_status": matrix.get("i4_by_status"),
		"sources": matrix.get("sources"),
		"cache": audit_cache(),
		"authorize_repairs": bool(matrix.get("all_pass")),
	}


@frappe.whitelist()
def rebuild_metrics_api(company=None):
	"""Force a fresh Scan All and return the new dashboard (no repair writes)."""
	_guard()
	return scan_all(company=company)


@frappe.whitelist()
def campaign_wizard_api(company=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.campaign_wizard import campaign_wizard

	return campaign_wizard(company=company or None)


@frappe.whitelist()
def campaign_preview_api(topic=None, company=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.campaign_wizard import campaign_preview

	return campaign_preview(topic=topic or "ZERO_RATE", company=company or None)


@frappe.whitelist()
def campaign_health_api(company=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.campaign_wizard import campaign_health

	return campaign_health(company=company or None)


@frappe.whitelist()
def campaign_history_api():
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.campaign_wizard import campaign_history

	return campaign_history()


@frappe.whitelist()
def campaign_export_api(company=None):
	_guard_admin()
	from erpnext_extensions.iran_accounting.historical_stock.campaign_wizard import campaign_export

	return campaign_export(company=company or None)


@frappe.whitelist()
def classify_zero_clusters_api(company=None, max_cluster=15):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate_campaign import classify_zero_clusters

	return classify_zero_clusters(company=company or None, max_cluster=cint(max_cluster) or 15)


@frappe.whitelist()
def preview_zero_campaign_api(company=None, max_roots=15):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate_campaign import preview_zero_campaign

	return preview_zero_campaign(company=company or None, max_roots=cint(max_roots) or 15)


@frappe.whitelist()
def run_zero_safe_cluster_api(company=None, max_roots=12, dry_run=True):
	"""Apply only when dry_run=0 after backup. Default dry_run=True."""
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate_campaign import (
		run_zero_safe_cluster_campaign,
	)

	return run_zero_safe_cluster_campaign(
		company=company or None,
		max_roots=cint(max_roots) or 12,
		apply=not is_dry,
	)


@frappe.whitelist()
def classify_wrong_clusters_api(company=None, max_cluster=15):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_campaign import classify_wrong_clusters

	return classify_wrong_clusters(company=company or None, max_cluster=cint(max_cluster) or 15)


@frappe.whitelist()
def classify_riv_campaign_api(company=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv_campaign import (
		classify_failed_riv_campaign,
	)

	return classify_failed_riv_campaign(company=company or None)


@frappe.whitelist()
def classify_gl_campaign_api(company=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.gl_campaign import classify_gl_campaign

	return classify_gl_campaign(company=company or None)


@frappe.whitelist()
def warehouse_dependency_api(warehouse=None, company=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_dependency import (
		analyze_warehouse_dependencies,
	)

	if not warehouse:
		frappe.throw("warehouse is required")
	return analyze_warehouse_dependencies(warehouse, company=company or None)


@frappe.whitelist()
def warehouse_replay_simulate_api(warehouse=None, item_code=None, company=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_dependency import (
		simulate_warehouse_replay_scope,
	)

	if not warehouse or not item_code:
		frappe.throw("warehouse and item_code are required")
	return simulate_warehouse_replay_scope(warehouse, item_code, company=company or None)


@frappe.whitelist()
def warehouse_plan_api(row=None):
	"""Plan a warehouse-scoped repair for a posting-order / escalation row (read-only)."""
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.planner import (
		plan_warehouse_repair,
	)

	parsed = row if isinstance(row, dict) else (_parse(row) or {})
	if isinstance(parsed, list):
		parsed = parsed[0] if parsed else {}
	if not parsed:
		frappe.throw("A warehouse repair row is required")
	return plan_warehouse_repair(parsed)


@frappe.whitelist()
def warehouse_dry_run_api(row=None):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.replay import (
		apply_warehouse_repair,
	)

	parsed = row if isinstance(row, dict) else (_parse(row) or {})
	if isinstance(parsed, list):
		parsed = parsed[0] if parsed else {}
	if not parsed:
		frappe.throw("A warehouse repair row is required")
	return apply_warehouse_repair(parsed, dry_run=True)


@frappe.whitelist()
def warehouse_apply_api(row=None, dry_run=True):
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.replay import (
		apply_warehouse_repair,
	)

	parsed = row if isinstance(row, dict) else (_parse(row) or {})
	if isinstance(parsed, list):
		parsed = parsed[0] if parsed else {}
	if not parsed:
		frappe.throw("A warehouse repair row is required")
	return apply_warehouse_repair(parsed, dry_run=is_dry)


@frappe.whitelist()
def scan_blockers_api(
	company=None,
	lane=None,
	issue_type=None,
	item_code=None,
	warehouse=None,
	batch_no=None,
	status=None,
	severity=None,
	sync=0,
	limit=500,
):
	"""List Historical Repair Blockers; optionally sync from live scans first."""
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.blockers import (
		list_blockers,
		scan_and_sync_blockers,
	)

	sync_result = None
	if cint(sync):
		sync_result = scan_and_sync_blockers(company=company or None, limit=cint(limit) or 500)
	listed = list_blockers(
		company=company or None,
		lane=lane or None,
		issue_type=issue_type or None,
		item_code=item_code or None,
		warehouse=warehouse or None,
		batch_no=batch_no or None,
		status=status or None,
		severity=severity or None,
		limit=cint(limit) or 500,
	)
	listed["sync"] = sync_result
	return listed


@frappe.whitelist()
def recheck_blocker_api(name=None):
	_guard()
	if not name:
		frappe.throw("Blocker name is required")
	from erpnext_extensions.iran_accounting.historical_stock.blockers import recheck_blocker

	return recheck_blocker(name)


@frappe.whitelist()
def sync_blockers_api(company=None, include_tool_limits=1, limit=500):
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.blockers import scan_and_sync_blockers

	return scan_and_sync_blockers(
		company=company or None,
		include_tool_limits=bool(cint(include_tool_limits)),
		limit=cint(limit) or 500,
	)


@frappe.whitelist()
def scan_job_card_flow_api(company=None, job_card=None, limit=500):
	"""Read-only Job Card material-flow recon (one row per Job Card)."""
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.job_card_flow import scan_job_card_flow

	return scan_job_card_flow(company=company or None, job_card=job_card or None, limit=cint(limit) or 500)


@frappe.whitelist()
def verify_repair_api(item_code=None, warehouse=None, voucher=None):
	"""Post-repair check: chain integrity + leftover-MA postcondition when applicable."""
	_guard()
	out = {"primary_state": "MANUAL", "ok": False}
	if voucher:
		out["voucher"] = voucher_integrity(voucher)
	if item_code and warehouse:
		out["chain"] = chain_integrity(item_code, warehouse)
		try:
			from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
				classify_leftover_ma_identity,
			)

			out["leftover_ma"] = classify_leftover_ma_identity(item_code, warehouse)
		except Exception as exc:
			out["leftover_ma_error"] = str(exc)
	out["ok"] = True
	return out


@frappe.whitelist()
def repair_safe_api(rows=None, dry_run=True):
	"""ONE mutation path: plan → repair → (caller) repost → verify."""
	is_dry = _dry(dry_run, True)
	require_write_if_applying(is_dry)
	parsed = _parse(rows)
	if not parsed:
		frappe.throw("Exact READY root rows are required")
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import repair_safe_roots

	return repair_safe_roots(parsed, dry_run=is_dry)


@frappe.whitelist()
def plan_roots_api(rows=None):
	"""Build RepairPlan objects for selected findings (read-only)."""
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import compress_roots, plan

	parsed = _parse(rows)
	plans = [plan(r).to_dict() for r in parsed]
	return {"count": len(plans), "plans": plans, "compression": compress_roots(parsed)}


@frappe.whitelist()
def compress_wrong_rate_roots_api(company=None, limit=4000):
	"""Group Wrong Rate findings into causal roots."""
	_guard()
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import compress_roots
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

	scan = scan_wrong_rates(company=company or None, limit=cint(limit) or 4000)
	rows = scan.get("rows") or []
	ready = [
		r
		for r in rows
		if str(r.get("planner_status") or r.get("rate_status") or "").startswith("READY")
		or r.get("eligible")
	]
	return {
		"scan_count": len(rows),
		"ready_findings": len(ready),
		"all": compress_roots(rows),
		"ready": compress_roots(ready),
	}


# Re-export posting-order APIs so the page can use one namespace.
scan_posting_order = scan_posting_order_anomalies
dry_run_posting_order = dry_run_posting_order_repair
repair_posting_order = repair_posting_order_selected
posting_order_integrity = posting_order_integrity_check
