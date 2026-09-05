# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import csv
import io
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, now_datetime
from frappe.utils.xlsxutils import make_xlsx

from erpnext_extensions.iran_accounting.account_explorer.permissions import assert_export_allowed
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec

EXPORT_FORMATS = frozenset({"csv", "xlsx"})
EXPORT_AXES = frozenset(
	{
		"account_level",
		"party",
		"unified_party",
		"dimension",
		"currency",
		"voucher",
		"item_group",
		"item",
	}
)

# DocType default for Iran Accounting Settings.export_background_threshold.
# Values < 1 (including 0 from cleared Int fields) are treated as unset.
DEFAULT_EXPORT_BACKGROUND_THRESHOLD = 5000


def normalize_export_background_threshold(raw: Any) -> int:
	"""Return a sane positive threshold; never allow 0 to mean 'queue everything'."""
	if raw is None or raw == "":
		return DEFAULT_EXPORT_BACKGROUND_THRESHOLD
	try:
		value = cint(raw)
	except Exception:
		return DEFAULT_EXPORT_BACKGROUND_THRESHOLD
	if value < 1:
		return DEFAULT_EXPORT_BACKGROUND_THRESHOLD
	return value


def _export_settings() -> dict[str, int]:
	settings = frappe.get_single("Iran Accounting Settings")
	return {
		"export_enabled": cint(settings.export_enabled),
		"export_background_threshold": normalize_export_background_threshold(
			settings.export_background_threshold
		),
		"server_page_size": cint(settings.server_page_size or 200),
	}


def _run_summary_builder(spec: AccountExplorerQuerySpec) -> dict:
	axis = spec.view_axis
	if axis == "account_level":
		from erpnext_extensions.iran_accounting.account_explorer.query_builder import (
			build_account_level_summary,
		)

		return build_account_level_summary(spec)
	if axis == "party":
		from erpnext_extensions.iran_accounting.account_explorer.party_summary import build_party_summary

		return build_party_summary(spec)
	if axis == "unified_party":
		from erpnext_extensions.iran_accounting.account_explorer.unified_party_summary import (
			build_unified_party_summary,
		)

		return build_unified_party_summary(spec)
	if axis == "dimension":
		from erpnext_extensions.iran_accounting.account_explorer.dimension_summary import (
			build_dimension_summary,
		)

		return build_dimension_summary(spec)
	if axis == "currency":
		from erpnext_extensions.iran_accounting.account_explorer.currency_summary import (
			build_currency_summary,
		)

		return build_currency_summary(spec)
	if axis == "voucher":
		from erpnext_extensions.iran_accounting.account_explorer.voucher_summary import (
			build_voucher_summary,
		)

		return build_voucher_summary(spec)
	if axis == "item_group":
		from erpnext_extensions.iran_accounting.account_explorer.item_group_summary import (
			build_item_group_summary,
		)

		return build_item_group_summary(spec)
	if axis == "item":
		from erpnext_extensions.iran_accounting.account_explorer.item_summary import build_item_summary

		return build_item_summary(spec)
	if axis == "inventory_account":
		from erpnext_extensions.iran_accounting.account_explorer.inventory_account_summary import (
			build_inventory_account_summary,
		)

		return build_inventory_account_summary(spec)
	frappe.throw(_("Export is not supported for the current analysis context."))


def collect_export_rows(spec: AccountExplorerQuerySpec) -> tuple[list[dict], dict, int]:
	"""Collect export rows with a single accounting aggregation pass.

	Phase 3 re-ran full aggregation per page. Phase 4:

	- voucher: SQL-paginated ``iter_voucher_summary_pages`` (totals once)
	- other axes: one builder call (chart/party cardinality, not GL rows)
	"""
	if spec.view_axis == "voucher":
		from erpnext_extensions.iran_accounting.account_explorer.voucher_summary import (
			iter_voucher_summary_pages,
		)

		settings = _export_settings()
		page_size = min(max(cint(settings["server_page_size"]) or 200, 1), 500)
		all_rows: list[dict] = []
		totals: dict = {}
		total_rows = 0
		for chunk in iter_voucher_summary_pages(spec, page_size=page_size):
			all_rows.extend(chunk.get("rows") or [])
			totals = chunk.get("totals") or totals
			total_rows = cint((chunk.get("pagination") or {}).get("total_rows") or total_rows)
		return all_rows, totals, total_rows

	original_page = spec.pagination.page
	original_size = spec.pagination.page_size
	try:
		spec.pagination.page = 1
		spec.pagination.page_size = 1_000_000
		result = _run_summary_builder(spec)
	finally:
		spec.pagination.page = original_page
		spec.pagination.page_size = original_size

	rows = result.get("rows") or []
	totals = result.get("totals") or {}
	total_rows = cint((result.get("pagination") or {}).get("total_rows") or len(rows))
	return rows, totals, total_rows


def get_export_columns(spec: AccountExplorerQuerySpec) -> list[dict[str, str]]:
	axis = spec.view_axis
	if axis == "account_level":
		return [
			{"fieldname": "display_code", "label": _("Account Code")},
			{"fieldname": "display_title", "label": _("Account Name")},
			{"fieldname": "opening_debit", "label": _("Opening Debit")},
			{"fieldname": "opening_credit", "label": _("Opening Credit")},
			{"fieldname": "period_debit", "label": _("Period Debit")},
			{"fieldname": "period_credit", "label": _("Period Credit")},
			{"fieldname": "debit_balance", "label": _("Closing Debit")},
			{"fieldname": "credit_balance", "label": _("Closing Credit")},
		]
	if axis == "party":
		return [
			{"fieldname": "party_type", "label": _("Party Type")},
			{"fieldname": "display_code", "label": _("Party")},
			{"fieldname": "display_title", "label": _("Party Name")},
			{"fieldname": "period_debit", "label": _("Debit Turnover")},
			{"fieldname": "period_credit", "label": _("Credit Turnover")},
			{"fieldname": "debit_balance", "label": _("Debit Balance")},
			{"fieldname": "credit_balance", "label": _("Credit Balance")},
		]
	if axis == "unified_party":
		return [
			{"fieldname": "display_code", "label": _("Unified Party")},
			{"fieldname": "display_title", "label": _("Unified Name")},
			{"fieldname": "member_count", "label": _("Member Count")},
			{"fieldname": "period_debit", "label": _("Debit Turnover")},
			{"fieldname": "period_credit", "label": _("Credit Turnover")},
			{"fieldname": "debit_balance", "label": _("Debit Balance")},
			{"fieldname": "credit_balance", "label": _("Credit Balance")},
		]
	if axis == "dimension":
		return [
			{"fieldname": "dimension_type", "label": _("Dimension Type")},
			{"fieldname": "display_code", "label": _("Dimension Value")},
			{"fieldname": "display_title", "label": _("Dimension Title")},
			{"fieldname": "period_debit", "label": _("Debit Turnover")},
			{"fieldname": "period_credit", "label": _("Credit Turnover")},
			{"fieldname": "debit_balance", "label": _("Debit Balance")},
			{"fieldname": "credit_balance", "label": _("Credit Balance")},
		]
	if axis == "currency":
		company_currency = "Company"
		company = getattr(spec, "company", None)
		if company:
			company_currency = frappe.get_cached_value("Company", company, "default_currency") or "Company"
		return [
			{"fieldname": "currency", "label": _("Currency")},
			{"fieldname": "period_debit", "label": _("Debit Amount (Currency)")},
			{"fieldname": "company_period_debit", "label": _("Debit Amount ({0})").format(company_currency)},
			{"fieldname": "period_credit", "label": _("Credit Amount (Currency)")},
			{"fieldname": "company_period_credit", "label": _("Credit Amount ({0})").format(company_currency)},
			{"fieldname": "net_balance", "label": _("Balance (Currency)")},
			{"fieldname": "company_net_balance", "label": _("Balance ({0})").format(company_currency)},
		]
	if axis == "voucher":
		return [
			{"fieldname": "posting_date", "label": _("Posting Date")},
			{"fieldname": "voucher_type", "label": _("Voucher Type")},
			{"fieldname": "voucher_no", "label": _("Voucher No")},
			{"fieldname": "scoped_debit", "label": _("Scoped Debit")},
			{"fieldname": "scoped_credit", "label": _("Scoped Credit")},
			{"fieldname": "scoped_net", "label": _("Scoped Net")},
		]
	if axis == "item_group":
		return [
			{"fieldname": "display_code", "label": _("Item Group")},
			{"fieldname": "display_title", "label": _("Title")},
			{"fieldname": "inward_value", "label": _("Inward Value")},
			{"fieldname": "outward_value", "label": _("Outward Value")},
			{"fieldname": "debit_balance", "label": _("Debit Balance")},
			{"fieldname": "credit_balance", "label": _("Credit Balance")},
		]
	if axis == "item":
		return [
			{"fieldname": "display_code", "label": _("Item")},
			{"fieldname": "display_title", "label": _("Item Name")},
			{"fieldname": "item_group", "label": _("Item Group")},
			{"fieldname": "in_qty", "label": _("In Qty")},
			{"fieldname": "out_qty", "label": _("Out Qty")},
			{"fieldname": "balance_qty", "label": _("Balance Qty")},
			{"fieldname": "inward_value", "label": _("Inward Value")},
			{"fieldname": "outward_value", "label": _("Outward Value")},
			{"fieldname": "debit_balance", "label": _("Debit Balance")},
			{"fieldname": "credit_balance", "label": _("Credit Balance")},
		]
	if axis == "inventory_account":
		# Legacy axis id (remapped to account_level in UI) — Case A stock breakdown labels.
		return [
			{"fieldname": "display_code", "label": _("Account")},
			{"fieldname": "display_title", "label": _("Account Name")},
			{"fieldname": "inward_value", "label": _("Inward Value")},
			{"fieldname": "outward_value", "label": _("Outward Value")},
			{"fieldname": "debit_balance", "label": _("Debit Balance")},
			{"fieldname": "credit_balance", "label": _("Credit Balance")},
		]
	frappe.throw(_("Export is not supported for the current analysis context."))


def _normalize_export_rows(rows: list[dict], spec: AccountExplorerQuerySpec) -> list[dict]:
	if spec.view_axis != "dimension":
		return rows
	dimension_type = spec.dimension_scope.dimension_type
	return [{**row, "dimension_type": row.get("dimension_type") or dimension_type} for row in rows]


def rows_to_matrix(rows: list[dict], columns: list[dict[str, str]]) -> tuple[list[str], list[list[Any]]]:
	headers = [column["label"] for column in columns]
	data = [[row.get(column["fieldname"]) for column in columns] for row in rows]
	return headers, data


def _export_report_title(spec: AccountExplorerQuerySpec) -> str | None:
	if spec.view_axis == "inventory_account":
		return _("Account Levels — Case A SLE-scoped stock breakdown")
	return None


def build_csv_content(rows: list[dict], columns: list[dict[str, str]], *, title: str | None = None) -> str:
	headers, data = rows_to_matrix(rows, columns)
	buffer = io.StringIO()
	writer = csv.writer(buffer)
	if title:
		writer.writerow([title])
	writer.writerow(headers)
	writer.writerows(data)
	return buffer.getvalue()


def build_csv_content_from_rows(rows: list[dict], columns: list[dict[str, str]]) -> str:
	return build_csv_content(rows, columns)


def _iter_voucher_export_rows(spec: AccountExplorerQuerySpec, *, page_size: int = 500):
	"""Yield voucher export rows page-by-page (single GROUP BY via temp table)."""
	from erpnext_extensions.iran_accounting.account_explorer.voucher_summary import (
		iter_voucher_summary_pages,
	)

	totals: dict = {}
	total_rows = 0
	for chunk in iter_voucher_summary_pages(spec, page_size=page_size):
		totals = chunk.get("totals") or totals
		total_rows = cint((chunk.get("pagination") or {}).get("total_rows") or total_rows)
		for row in chunk.get("rows") or []:
			yield row, totals, total_rows


def build_streaming_voucher_csv(spec: AccountExplorerQuerySpec, columns: list[dict[str, str]]) -> tuple[str, dict, int]:
	"""Stream voucher CSV without holding the full row list in Python."""
	settings = _export_settings()
	page_size = min(max(cint(settings["server_page_size"]) or 200, 1), 500)
	buffer = io.StringIO()
	writer = csv.writer(buffer)
	writer.writerow([column["label"] for column in columns])
	totals: dict = {}
	total_rows = 0
	written = 0
	for row, totals, total_rows in _iter_voucher_export_rows(spec, page_size=page_size):
		writer.writerow([row.get(column["fieldname"]) for column in columns])
		written += 1
	if written == 0:
		# still return totals from empty iterator path
		from erpnext_extensions.iran_accounting.account_explorer.voucher_summary import (
			_scoped_voucher_totals,
		)

		totals, total_rows = _scoped_voucher_totals(spec)
	return buffer.getvalue(), totals, cint(total_rows or written)


def build_streaming_voucher_xlsx(spec: AccountExplorerQuerySpec, columns: list[dict[str, str]]) -> tuple[bytes, dict, int]:
	"""Write-only XLSX for voucher export (bounded row buffering)."""
	from openpyxl import Workbook

	settings = _export_settings()
	page_size = min(max(cint(settings["server_page_size"]) or 200, 1), 500)
	wb = Workbook(write_only=True)
	ws = wb.create_sheet(title="Account Explorer")
	ws.append([column["label"] for column in columns])
	totals: dict = {}
	total_rows = 0
	written = 0
	for row, totals, total_rows in _iter_voucher_export_rows(spec, page_size=page_size):
		ws.append([row.get(column["fieldname"]) for column in columns])
		written += 1
	if written == 0:
		from erpnext_extensions.iran_accounting.account_explorer.voucher_summary import (
			_scoped_voucher_totals,
		)

		totals, total_rows = _scoped_voucher_totals(spec)
	bio = io.BytesIO()
	wb.save(bio)
	return bio.getvalue(), totals, cint(total_rows or written)



def build_xlsx_content(rows: list[dict], columns: list[dict[str, str]], *, title: str | None = None) -> bytes:
	headers, data = rows_to_matrix(rows, columns)
	sheet_title = (title or "Account Explorer")[:31]
	matrix = [headers, *data]
	if title:
		matrix = [[title], *matrix]
	xlsx_file = make_xlsx(matrix, sheet_title)
	return xlsx_file.getvalue()


def _export_filename(spec: AccountExplorerQuerySpec, file_format: str) -> str:
	timestamp = now_datetime().strftime("%Y%m%d_%H%M%S")
	return f"account_explorer_{spec.view_axis}_{timestamp}.{file_format}"


def _save_export_file(content: bytes | str, filename: str, file_format: str) -> str:
	if file_format == "xlsx":
		from frappe.utils.file_manager import save_file

		file_doc = save_file(filename, content, None, None, is_private=1)
		return file_doc.file_url

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": filename,
			"is_private": 1,
			"content": content,
		}
	)
	file_doc.save(ignore_permissions=True)
	return file_doc.file_url


def _send_export_ready_email(user: str, file_url: str, filename: str) -> None:
	frappe.sendmail(
		recipients=[user],
		subject=_("Account Explorer export is ready"),
		message=_("Your export {0} is ready: {1}").format(filename, file_url),
		now=True,
	)


def _prepare_export_payload(payload: Any) -> AccountExplorerQuerySpec:
	assert_export_allowed()
	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
	if spec.detail_mode != "summary":
		frappe.throw(_("Export is only supported for summary view."))
	if spec.view_axis not in EXPORT_AXES:
		frappe.throw(_("Export is not supported for the current analysis axis."))
	return spec


def _probe_export_size(spec: AccountExplorerQuerySpec) -> int:
	"""Cheap total_rows probe (voucher uses totals query only)."""
	if spec.view_axis == "voucher":
		from erpnext_extensions.iran_accounting.account_explorer.voucher_summary import (
			_scoped_voucher_totals,
		)

		_totals, total_rows = _scoped_voucher_totals(spec)
		return cint(total_rows)

	settings = _export_settings()
	page_size = min(settings["server_page_size"], 500)
	original_page = spec.pagination.page
	original_size = spec.pagination.page_size
	try:
		spec.pagination.page_size = page_size
		spec.pagination.page = 1
		result = _run_summary_builder(spec)
	finally:
		spec.pagination.page = original_page
		spec.pagination.page_size = original_size
	return cint((result.get("pagination") or {}).get("total_rows") or 0)


def _trigger_download(content: bytes | str, filename: str) -> None:
	frappe.local.response.filename = filename
	frappe.local.response.filecontent = content
	frappe.local.response.type = "download"


def export_account_explorer(payload: Any, file_format: str = "csv", *, force_sync: bool = False) -> dict:
	file_format = (file_format or "csv").lower()
	if file_format not in EXPORT_FORMATS:
		frappe.throw(_("Unsupported export format."))

	spec = _prepare_export_payload(payload)
	settings = _export_settings()
	total_rows = _probe_export_size(spec)

	if not force_sync and total_rows > settings["export_background_threshold"]:
		frappe.enqueue(
			"erpnext_extensions.iran_accounting.account_explorer.export.run_account_explorer_export_job",
			queue="long",
			payload=payload,
			file_format=file_format,
			user=frappe.session.user,
		)
		return {
			"queued": 1,
			"total_rows": total_rows,
			"message": _("Export queued in background because the dataset exceeds {0} rows.").format(
				settings["export_background_threshold"]
			),
		}

	columns = get_export_columns(spec)
	filename = _export_filename(spec, file_format)
	report_title = _export_report_title(spec)

	if spec.view_axis == "voucher" and file_format == "csv":
		content, totals, total_rows = build_streaming_voucher_csv(spec, columns)
		_trigger_download(content, filename)
	elif spec.view_axis == "voucher" and file_format == "xlsx":
		content, totals, total_rows = build_streaming_voucher_xlsx(spec, columns)
		_trigger_download(content, filename)
	else:
		rows, totals, total_rows = collect_export_rows(spec)
		rows = _normalize_export_rows(rows, spec)
		if file_format == "csv":
			_trigger_download(build_csv_content(rows, columns, title=report_title), filename)
		else:
			_trigger_download(build_xlsx_content(rows, columns, title=report_title), filename)

	return {
		"queued": 0,
		"total_rows": total_rows,
		"filename": filename,
		"totals": totals,
	}


def run_account_explorer_export_job(payload: Any, file_format: str, user: str) -> None:
	frappe.set_user(user)
	spec = _prepare_export_payload(payload)
	columns = get_export_columns(spec)
	filename = _export_filename(spec, file_format)

	if spec.view_axis == "voucher" and file_format == "csv":
		content, _totals, _total_rows = build_streaming_voucher_csv(spec, columns)
	elif spec.view_axis == "voucher" and file_format == "xlsx":
		content, _totals, _total_rows = build_streaming_voucher_xlsx(spec, columns)
	else:
		rows, _totals, _total_rows = collect_export_rows(spec)
		rows = _normalize_export_rows(rows, spec)
		report_title = _export_report_title(spec)
		if file_format == "csv":
			content = build_csv_content(rows, columns, title=report_title)
		else:
			content = build_xlsx_content(rows, columns, title=report_title)

	file_url = _save_export_file(content, filename, file_format)
	_send_export_ready_email(user, file_url, filename)
