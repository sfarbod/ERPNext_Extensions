# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import csv
import io
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, now_datetime
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
		from erpnext_extensions.iran_accounting.account_explorer.voucher_gl_lines import (
			build_voucher_gl_line_summary,
		)

		return build_voucher_gl_line_summary(spec)
	if axis == "item_group":
		from erpnext_extensions.iran_accounting.account_explorer.commercial_item_analysis import (
			build_commercial_item_group_summary,
			identity_requires_commercial_item_mode,
		)
		from erpnext_extensions.iran_accounting.account_explorer.item_group_summary import (
			build_item_group_summary,
		)

		if identity_requires_commercial_item_mode(spec):
			return build_commercial_item_group_summary(spec)
		return build_item_group_summary(spec)
	if axis == "item":
		from erpnext_extensions.iran_accounting.account_explorer.commercial_item_analysis import (
			build_commercial_item_summary,
			identity_requires_commercial_item_mode,
		)
		from erpnext_extensions.iran_accounting.account_explorer.item_summary import build_item_summary

		if identity_requires_commercial_item_mode(spec):
			return build_commercial_item_summary(spec)
		return build_item_summary(spec)
	if axis == "inventory_account":
		from erpnext_extensions.iran_accounting.account_explorer.inventory_account_summary import (
			build_inventory_account_summary,
		)

		return build_inventory_account_summary(spec)
	frappe.throw(_("Export is not supported for the current analysis context."))


def _normalize_selected_row_keys(selected_row_keys: Any) -> list[str]:
	if selected_row_keys in (None, "", [], ()):
		return []
	if isinstance(selected_row_keys, str):
		try:
			selected_row_keys = frappe.parse_json(selected_row_keys)
		except Exception:
			selected_row_keys = [selected_row_keys]
	keys: list[str] = []
	seen: set[str] = set()
	for raw in selected_row_keys or []:
		key = cstr(raw).strip()
		if not key or key in seen:
			continue
		seen.add(key)
		keys.append(key)
	return keys


def _filter_rows_by_selected_keys(rows: list[dict], selected_row_keys: list[str]) -> list[dict]:
	"""Keep rows whose authoritative ``row_key`` is in the selection set."""
	if not selected_row_keys:
		return []
	wanted = set(selected_row_keys)
	return [row for row in rows if cstr(row.get("row_key")) in wanted]


def _totals_for_selected_rows(rows: list[dict], spec: AccountExplorerQuerySpec) -> dict:
	"""Recompute export totals from the selected row set only."""
	axis = spec.view_axis
	if axis == "voucher":
		debit = sum(flt(row.get("debit", row.get("scoped_debit"))) for row in rows)
		credit = sum(flt(row.get("credit", row.get("scoped_credit"))) for row in rows)
		return {
			"debit": debit,
			"credit": credit,
			"net": debit - credit,
			"scoped_debit": debit,
			"scoped_credit": credit,
			"scoped_net": debit - credit,
		}
	if axis in {"item", "item_group", "inventory_account"}:
		from erpnext_extensions.iran_accounting.account_explorer.stock_measures import (
			sum_stock_measure_rows,
		)

		include_qty = axis == "item"
		totals = sum_stock_measure_rows(rows, include_qty=include_qty)
		if not include_qty:
			for qty_key in ("in_qty", "out_qty", "balance_qty", "opening_qty", "closing_qty"):
				totals.pop(qty_key, None)
		return totals
	from erpnext_extensions.iran_accounting.account_explorer.measures import sum_measure_rows

	return sum_measure_rows(rows)


def collect_export_rows(
	spec: AccountExplorerQuerySpec,
	*,
	row_scope: str = "all",
	selected_row_keys: list[str] | None = None,
) -> tuple[list[dict], dict, int]:
	"""Collect export rows with a single accounting aggregation pass.

	Phase 3 re-ran full aggregation per page. Phase 4:

	- voucher: SQL-paginated GL line pages (totals once)
	- other axes: one builder call (chart/party cardinality, not GL rows)

	``row_scope=selected`` filters by authoritative ``row_key`` values
	(``glentry:<name>``, ``item:<code>``, etc.) — never by visible index.
	"""
	scope = (row_scope or "all").lower()
	keys = _normalize_selected_row_keys(selected_row_keys)
	if scope == "selected":
		if not keys:
			frappe.throw(_("Select at least one row before exporting the selection."))
	else:
		scope = "all"
		keys = []

	if spec.view_axis == "voucher":
		from erpnext_extensions.iran_accounting.account_explorer.voucher_gl_lines import (
			build_voucher_gl_line_summary,
		)

		settings = _export_settings()
		page_size = min(max(cint(settings["server_page_size"]) or 200, 1), 500)
		all_rows: list[dict] = []
		totals: dict = {}
		total_rows = 0
		original_page = spec.pagination.page
		original_size = spec.pagination.page_size
		page = 1
		wanted = set(keys)
		try:
			while True:
				spec.pagination.page = page
				spec.pagination.page_size = page_size
				chunk = build_voucher_gl_line_summary(spec)
				chunk_rows = chunk.get("rows") or []
				if scope == "selected":
					all_rows.extend(
						row for row in chunk_rows if cstr(row.get("row_key")) in wanted
					)
					# Stop early once every selected key has been found.
					if wanted and len({cstr(r.get("row_key")) for r in all_rows}) >= len(wanted):
						# Still need accurate filtered totals below; break page walk.
						totals = chunk.get("totals") or totals
						total_rows = cint((chunk.get("pagination") or {}).get("total_rows") or total_rows)
						break
				else:
					all_rows.extend(chunk_rows)
				totals = chunk.get("totals") or totals
				total_rows = cint((chunk.get("pagination") or {}).get("total_rows") or total_rows)
				if page * page_size >= total_rows:
					break
				page += 1
		finally:
			spec.pagination.page = original_page
			spec.pagination.page_size = original_size
		if scope == "selected":
			# Preserve selection order from the client keys list.
			by_key = {cstr(row.get("row_key")): row for row in all_rows}
			ordered = [by_key[key] for key in keys if key in by_key]
			return ordered, _totals_for_selected_rows(ordered, spec), len(ordered)
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
	if scope == "selected":
		by_key = {cstr(row.get("row_key")): row for row in rows}
		ordered = [by_key[key] for key in keys if key in by_key]
		return ordered, _totals_for_selected_rows(ordered, spec), len(ordered)
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
			{"fieldname": "party_identifier", "label": _("Identifier Field")},
			{"fieldname": "related_roles_label", "label": _("Related Roles")},
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
			{"fieldname": "posting_date", "label": _("Posting Date"), "fieldtype": "Date"},
			{"fieldname": "voucher_type", "label": _("Voucher Type")},
			{"fieldname": "voucher_no", "label": _("Voucher No")},
			{"fieldname": "gl_entry", "label": _("GL Entry ID")},
			{"fieldname": "account_code", "label": _("Account Code")},
			{"fieldname": "account_name", "label": _("Account Name")},
			{"fieldname": "account", "label": _("Account")},
			{"fieldname": "debit", "label": _("Debit")},
			{"fieldname": "credit", "label": _("Credit")},
			{"fieldname": "account_currency", "label": _("Account Currency")},
			{"fieldname": "remarks", "label": _("Description")},
			{"fieldname": "party_type", "label": _("Party Type")},
			{"fieldname": "party_name", "label": _("Party Name")},
			{"fieldname": "party", "label": _("Party")},
			{"fieldname": "cost_center", "label": _("Cost Center")},
		]
	if axis == "item_group":
		from erpnext_extensions.iran_accounting.account_explorer.commercial_item_analysis import (
			identity_requires_commercial_item_mode,
		)

		if identity_requires_commercial_item_mode(spec):
			return [
				{"fieldname": "analysis_source", "label": _("Analysis Source")},
				{"fieldname": "display_code", "label": _("Item Group")},
				{"fieldname": "display_title", "label": _("Title")},
				{"fieldname": "sales_base_net_amount", "label": _("Sales Net (Base)")},
				{"fieldname": "purchase_base_net_amount", "label": _("Purchase Net (Base)")},
				{"fieldname": "return_base_net_amount", "label": _("Return Amount (Base)")},
				{"fieldname": "document_count", "label": _("Documents")},
				{"fieldname": "item_count", "label": _("Items")},
			]
		return [
			{"fieldname": "display_code", "label": _("Item Group")},
			{"fieldname": "display_title", "label": _("Title")},
			{"fieldname": "inward_value", "label": _("Inward Value")},
			{"fieldname": "outward_value", "label": _("Outward Value")},
			{"fieldname": "debit_balance", "label": _("Debit Balance")},
			{"fieldname": "credit_balance", "label": _("Credit Balance")},
		]
	if axis == "item":
		from erpnext_extensions.iran_accounting.account_explorer.commercial_item_analysis import (
			identity_requires_commercial_item_mode,
		)

		if identity_requires_commercial_item_mode(spec):
			return [
				{"fieldname": "analysis_source", "label": _("Analysis Source")},
				{"fieldname": "display_code", "label": _("Item")},
				{"fieldname": "display_title", "label": _("Item Name")},
				{"fieldname": "item_group", "label": _("Item Group")},
				{"fieldname": "transaction_direction", "label": _("Direction")},
				{"fieldname": "party_type", "label": _("Party Type")},
				{"fieldname": "party_name", "label": _("Party Name")},
				{"fieldname": "party", "label": _("Party")},
				{"fieldname": "quantity", "label": _("Quantity")},
				{"fieldname": "stock_quantity", "label": _("Stock Qty")},
				{"fieldname": "uom", "label": _("UOM")},
				{"fieldname": "net_amount", "label": _("Net Amount")},
				{"fieldname": "base_net_amount", "label": _("Base Net Amount")},
				{"fieldname": "currency", "label": _("Currency")},
				{"fieldname": "document_count", "label": _("Documents")},
				{"fieldname": "return_quantity", "label": _("Return Qty")},
				{"fieldname": "return_base_net_amount", "label": _("Return Amount (Base)")},
			]
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


_TEXT_IDENTIFIER_FIELDS = frozenset(
	{
		"party_identifier",
		"identifier_summary",
		"display_code",
		"party",
		"related_roles_label",
	}
)


def _format_export_identifier(value: Any) -> Any:
	"""Keep identifier / party codes as text (preserve leading zeros)."""
	if value is None or value == "":
		return value
	return cstr(value)


def rows_to_matrix(
	rows: list[dict],
	columns: list[dict[str, str]],
	*,
	date_format: str = "jalali",
) -> tuple[list[str], list[list[Any]]]:
	headers = [column["label"] for column in columns]
	data = []
	for row in rows:
		line = []
		for column in columns:
			value = row.get(column["fieldname"])
			fieldname = column["fieldname"]
			fieldtype = column.get("fieldtype") or _infer_export_fieldtype(fieldname, value)
			if fieldtype in {"Date", "Datetime"}:
				value = _format_export_date(value, fieldtype, date_format)
			elif fieldname in _TEXT_IDENTIFIER_FIELDS or fieldtype == "Data":
				if fieldname in _TEXT_IDENTIFIER_FIELDS:
					value = _format_export_identifier(value)
			line.append(value)
		data.append(line)
	return headers, data


def _infer_export_fieldtype(fieldname: str, value: Any) -> str | None:
	name = (fieldname or "").lower()
	if name.endswith("_date") or name in {"posting_date", "from_date", "to_date", "creation", "modified"}:
		if "time" in name or name in {"creation", "modified"}:
			return "Datetime"
		return "Date"
	if name in _TEXT_IDENTIFIER_FIELDS:
		return "Data"
	return None


def _export_report_title(spec: AccountExplorerQuerySpec) -> str | None:
	if spec.view_axis == "inventory_account":
		return _("Account Levels — Case A SLE-scoped stock breakdown")
	return None


def build_csv_content(
	rows: list[dict],
	columns: list[dict[str, str]],
	*,
	title: str | None = None,
	date_format: str = "jalali",
) -> str:
	headers, data = rows_to_matrix(rows, columns, date_format=date_format)
	buffer = io.StringIO()
	writer = csv.writer(buffer)
	if title:
		writer.writerow([title])
	writer.writerow(headers)
	writer.writerows(data)
	return buffer.getvalue()


def build_csv_content_from_rows(
	rows: list[dict], columns: list[dict[str, str]], *, date_format: str = "jalali"
) -> str:
	return build_csv_content(rows, columns, date_format=date_format)


def _iter_voucher_export_rows(spec: AccountExplorerQuerySpec, *, page_size: int = 500):
	"""Yield voucher export rows page-by-page (legacy UI-enrichment path).

	Prefer ``build_streaming_voucher_csv`` / ``xlsx`` which use the lean batch path.
	"""
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


# Measured default for lean keyset export (see v5.1.5 profile benchmarks).
AE_VOUCHER_EXPORT_BATCH_SIZE = 10000


def _voucher_export_fieldnames(columns: list[dict[str, str]]) -> list[str]:
	return [column["fieldname"] for column in columns]


def _tuple_to_export_values(row: tuple, fieldnames: list[str], *, date_format: str = "jalali") -> list[Any]:
	"""Map lean GL-line SQL tuple → export column order.

	Tuple layout:
	gl_entry, posting_date, voucher_type, voucher_no, account_code, account_name,
	debit, credit, account_currency, remarks, party_type, party_name, party,
	cost_center, account
	"""
	(
		gl_entry,
		posting_date,
		voucher_type,
		voucher_no,
		account_code,
		account_name,
		debit,
		credit,
		account_currency,
		remarks,
		party_type,
		party_name,
		party,
		cost_center,
		account,
	) = row
	debit = flt(debit)
	credit = flt(credit)
	values = {
		"gl_entry": gl_entry,
		"posting_date": _format_export_date(posting_date, "Date", date_format),
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"account_code": cstr(account_code or ""),
		"account_name": account_name,
		"account": account,
		"debit": debit,
		"credit": credit,
		"account_currency": account_currency,
		"remarks": remarks,
		"party_type": party_type,
		"party_name": party_name,
		"party": party,
		"cost_center": cost_center,
		# Legacy aliases
		"scoped_debit": debit,
		"scoped_credit": credit,
		"scoped_net": debit - credit,
	}
	return [values.get(name) for name in fieldnames]


def _format_export_date(value: Any, fieldtype: str, date_format: str) -> Any:
	"""Convert Date/Datetime cells for export without mutating stored values."""
	if value in (None, ""):
		return ""
	mode = (date_format or "jalali").lower()
	if mode in {"gregorian", "iso", "en"}:
		return str(value)
	try:
		from persian_calendar.utils.data_io import convert_export_value

		converted = convert_export_value(value, fieldtype, enabled=True)
		if converted in (None, ""):
			return str(value)
		# Prefer YYYY-MM-DD for Date cells when persian_calendar returns slash form.
		text = cstr(converted)
		if fieldtype == "Date" and "/" in text and len(text) >= 10:
			try:
				from persian_calendar.utils.jalali import toshamshi

				return toshamshi(value, format="YYYY-MM-DD")
			except Exception:
				return text.replace("/", "-")
		return text
	except Exception:
		return str(value)


def _publish_export_progress(*, phase: str, written: int = 0, total_rows: int = 0, extra: dict | None = None):
	try:
		from rq import get_current_job

		job = get_current_job()
		job_id = job.id if job else None
	except Exception:
		job_id = None
	payload = {
		"job_id": job_id,
		"phase": phase,
		"status": "running" if phase not in {"ready", "failed"} else phase,
		"written": cint(written),
		"total_rows": cint(total_rows),
	}
	if extra:
		payload.update(extra)
	try:
		frappe.publish_realtime(
			event="account_explorer_export_progress",
			message=payload,
			user=frappe.session.user,
			after_commit=False,
		)
	except Exception:
		pass


def build_streaming_voucher_csv(
	spec: AccountExplorerQuerySpec,
	columns: list[dict[str, str]],
	*,
	batch_size: int | None = None,
	date_format: str = "jalali",
) -> tuple[str, dict, int]:
	"""Stream voucher GL-line CSV via keyset batches on GL Entry.name."""
	from erpnext_extensions.iran_accounting.account_explorer.voucher_gl_lines import (
		_gl_line_totals,
		iter_voucher_gl_line_export_batches,
	)

	batch_size = cint(batch_size) or AE_VOUCHER_EXPORT_BATCH_SIZE
	fieldnames = _voucher_export_fieldnames(columns)
	buffer = io.StringIO()
	writer = csv.writer(buffer)
	writer.writerow([column["label"] for column in columns])
	totals: dict = {}
	total_rows = 0
	written = 0
	_publish_export_progress(phase="preparing_rows", written=0, total_rows=0)
	for batch, totals, total_rows in iter_voucher_gl_line_export_batches(spec, batch_size=batch_size):
		if not batch and total_rows == 0:
			break
		_publish_export_progress(phase="writing_csv", written=written, total_rows=total_rows)
		for row in batch:
			writer.writerow(_tuple_to_export_values(row, fieldnames, date_format=date_format))
			written += 1
	if written == 0:
		totals, total_rows = _gl_line_totals(spec)
	_publish_export_progress(phase="writing_csv", written=written, total_rows=cint(total_rows or written))
	return buffer.getvalue(), totals, cint(total_rows or written)


def build_streaming_voucher_xlsx(
	spec: AccountExplorerQuerySpec,
	columns: list[dict[str, str]],
	*,
	batch_size: int | None = None,
	date_format: str = "jalali",
) -> tuple[bytes, dict, int]:
	"""Write-only XLSX for voucher GL-line export (keyset batches)."""
	from openpyxl import Workbook

	from erpnext_extensions.iran_accounting.account_explorer.voucher_gl_lines import (
		_gl_line_totals,
		iter_voucher_gl_line_export_batches,
	)

	batch_size = cint(batch_size) or AE_VOUCHER_EXPORT_BATCH_SIZE
	fieldnames = _voucher_export_fieldnames(columns)
	wb = Workbook(write_only=True)
	ws = wb.create_sheet(title="Account Explorer")
	ws.append([column["label"] for column in columns])
	totals: dict = {}
	total_rows = 0
	written = 0
	_publish_export_progress(phase="preparing_rows", written=0, total_rows=0)
	for batch, totals, total_rows in iter_voucher_gl_line_export_batches(spec, batch_size=batch_size):
		if not batch and total_rows == 0:
			break
		_publish_export_progress(phase="writing_xlsx", written=written, total_rows=total_rows)
		for row in batch:
			ws.append(_tuple_to_export_values(row, fieldnames, date_format=date_format))
			written += 1
	if written == 0:
		totals, total_rows = _gl_line_totals(spec)
	bio = io.BytesIO()
	_publish_export_progress(phase="saving_file", written=written, total_rows=cint(total_rows or written))
	wb.save(bio)
	return bio.getvalue(), totals, cint(total_rows or written)



def build_xlsx_content(
	rows: list[dict],
	columns: list[dict[str, str]],
	*,
	title: str | None = None,
	date_format: str = "jalali",
) -> bytes:
	headers, data = rows_to_matrix(rows, columns, date_format=date_format)
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


def _file_size_bytes(file_url: str) -> int:
	try:
		name = frappe.db.get_value("File", {"file_url": file_url}, "name")
		if not name:
			return 0
		return cint(frappe.db.get_value("File", name, "file_size") or 0)
	except Exception:
		return 0


def _notify_export_ready(*, user: str, file_url: str, filename: str, total_rows: int) -> dict[str, Any]:
	"""AE-local finalization: never let secondary notification failure fail the job.

	Primary channel is Desk realtime + Notification Log. Optional email uses
	``now=False`` so SMTP is NOT attempted on this job's ``after_commit``
	(``now=True`` previously marked successful file jobs as Failed when the
	site Email Account password could not be decrypted).
	"""
	payload = {
		"file_url": file_url,
		"filename": filename,
		"total_rows": cint(total_rows),
		"file_size": _file_size_bytes(file_url),
	}
	channels: dict[str, Any] = {
		"realtime": False,
		"notification_log": False,
		"email_queued": False,
		"email_error": None,
	}

	try:
		frappe.publish_realtime(
			event="account_explorer_export_ready",
			message=payload,
			user=user,
			after_commit=True,
		)
		channels["realtime"] = True
	except Exception:
		frappe.log_error(title="Account Explorer export realtime notify failed")

	try:
		doc = frappe.get_doc(
			{
				"doctype": "Notification Log",
				"for_user": user,
				"from_user": user,
				"type": "Alert",
				"document_type": "File",
				"subject": _("Account Explorer export is ready"),
				"email_content": _("Your export {0} is ready ({1} rows).").format(
					filename, cint(total_rows)
				),
				"link": file_url,
			}
		)
		doc.insert(ignore_permissions=True)
		channels["notification_log"] = True
		channels["notification_log_name"] = doc.name
	except Exception:
		frappe.log_error(title="Account Explorer export Notification Log failed")

	# Optional email: queue only. Never send_now / now=True on this worker commit.
	try:
		recipient = user
		if "@" not in (user or ""):
			recipient = frappe.db.get_value("User", user, "email") or ""
		if recipient and "@" in recipient:
			frappe.sendmail(
				recipients=[recipient],
				subject=_("Account Explorer export is ready"),
				message=_("Your export {0} is ready: {1}").format(filename, file_url),
				now=False,
				delayed=True,
				is_notification=True,
			)
			channels["email_queued"] = True
	except Exception as exc:
		channels["email_error"] = str(exc)
		frappe.log_error(title="Account Explorer export email queue failed")

	return channels


# Backward-compatible alias (tests / callers).
def _send_export_ready_email(user: str, file_url: str, filename: str) -> None:
	_notify_export_ready(user=user, file_url=file_url, filename=filename, total_rows=0)


def _prepare_export_payload(payload: Any) -> AccountExplorerQuerySpec:
	assert_export_allowed()
	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
	if spec.detail_mode != "summary":
		frappe.throw(_("Export is only supported for summary view."))
	if spec.view_axis not in EXPORT_AXES:
		frappe.throw(_("Export is not supported for the current analysis axis."))
	return spec


def _probe_export_size(
	spec: AccountExplorerQuerySpec,
	*,
	row_scope: str = "all",
	selected_row_keys: list[str] | None = None,
) -> int:
	"""Cheap total_rows probe (voucher uses GL-line totals query only)."""
	if (row_scope or "all").lower() == "selected":
		return len(_normalize_selected_row_keys(selected_row_keys))
	if spec.view_axis == "voucher":
		from erpnext_extensions.iran_accounting.account_explorer.voucher_gl_lines import (
			_gl_line_totals,
		)

		_totals, total_rows = _gl_line_totals(spec)
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


def export_account_explorer(
	payload: Any,
	file_format: str = "csv",
	date_format: str = "jalali",
	*,
	row_scope: str = "all",
	selected_row_keys: Any = None,
	force_sync: bool = False,
) -> dict:
	file_format = (file_format or "csv").lower()
	date_format = (date_format or "jalali").lower()
	row_scope = (row_scope or "all").lower()
	if file_format not in EXPORT_FORMATS:
		frappe.throw(_("Unsupported export format."))
	if date_format not in {"jalali", "gregorian", "iso", "en"}:
		date_format = "jalali"
	if row_scope not in {"all", "selected"}:
		row_scope = "all"
	selected_keys = _normalize_selected_row_keys(selected_row_keys)

	spec = _prepare_export_payload(payload)
	settings = _export_settings()
	total_rows = _probe_export_size(
		spec, row_scope=row_scope, selected_row_keys=selected_keys
	)

	if not force_sync and total_rows > settings["export_background_threshold"]:
		from erpnext_extensions.iran_accounting.account_explorer.queue_policy import (
			AE_EXPORT_QUEUE,
			assert_queues_isolated,
			export_enqueue_kwargs,
		)

		assert_queues_isolated()
		job = frappe.enqueue(
			"erpnext_extensions.iran_accounting.account_explorer.export.run_account_explorer_export_job",
			**export_enqueue_kwargs(
				payload=payload,
				file_format=file_format,
				date_format=date_format,
				row_scope=row_scope,
				selected_row_keys=selected_keys,
				user=frappe.session.user,
			),
		)
		return {
			"queued": 1,
			"total_rows": total_rows,
			"queue": AE_EXPORT_QUEUE,
			"job_id": getattr(job, "id", None),
			"status": "queued",
			"message": _("Export is being prepared."),
		}

	columns = get_export_columns(spec)
	filename = _export_filename(spec, file_format)
	report_title = _export_report_title(spec)
	use_stream = (
		spec.view_axis == "voucher"
		and row_scope == "all"
	)

	if use_stream and file_format == "csv":
		content, totals, total_rows = build_streaming_voucher_csv(
			spec, columns, date_format=date_format
		)
		_trigger_download(content, filename)
	elif use_stream and file_format == "xlsx":
		content, totals, total_rows = build_streaming_voucher_xlsx(
			spec, columns, date_format=date_format
		)
		_trigger_download(content, filename)
	else:
		rows, totals, total_rows = collect_export_rows(
			spec, row_scope=row_scope, selected_row_keys=selected_keys
		)
		rows = _normalize_export_rows(rows, spec)
		if file_format == "csv":
			_trigger_download(
				build_csv_content(rows, columns, title=report_title, date_format=date_format),
				filename,
			)
		else:
			_trigger_download(
				build_xlsx_content(rows, columns, title=report_title, date_format=date_format),
				filename,
			)

	return {
		"queued": 0,
		"total_rows": total_rows,
		"filename": filename,
		"totals": totals,
		"row_scope": row_scope,
	}


def run_account_explorer_export_job(
	payload: Any,
	file_format: str,
	user: str,
	date_format: str = "jalali",
	row_scope: str = "all",
	selected_row_keys: Any = None,
) -> dict[str, Any]:
	"""Background export: persist file first; notifications must not fail the job."""
	frappe.set_user(user)
	date_format = (date_format or "jalali").lower()
	row_scope = (row_scope or "all").lower()
	selected_keys = _normalize_selected_row_keys(selected_row_keys)
	spec = _prepare_export_payload(payload)
	columns = get_export_columns(spec)
	filename = _export_filename(spec, file_format)
	total_rows = 0
	use_stream = spec.view_axis == "voucher" and row_scope == "all"

	if use_stream and file_format == "csv":
		content, _totals, total_rows = build_streaming_voucher_csv(
			spec, columns, date_format=date_format
		)
	elif use_stream and file_format == "xlsx":
		content, _totals, total_rows = build_streaming_voucher_xlsx(
			spec, columns, date_format=date_format
		)
	else:
		rows, _totals, total_rows = collect_export_rows(
			spec, row_scope=row_scope, selected_row_keys=selected_keys
		)
		rows = _normalize_export_rows(rows, spec)
		report_title = _export_report_title(spec)
		if file_format == "csv":
			content = build_csv_content(rows, columns, title=report_title, date_format=date_format)
		else:
			content = build_xlsx_content(rows, columns, title=report_title, date_format=date_format)

	file_url = _save_export_file(content, filename, file_format)
	# Persist File before any notification side-effects so a broken Email Account
	# cannot leave the user without a downloadable artifact.
	frappe.db.commit()
	_publish_export_progress(
		phase="ready",
		written=cint(total_rows),
		total_rows=cint(total_rows),
		extra={"file_url": file_url, "filename": filename},
	)

	notify = _notify_export_ready(
		user=user,
		file_url=file_url,
		filename=filename,
		total_rows=cint(total_rows),
	)
	return {
		"ok": 1,
		"status": "ready",
		"file_url": file_url,
		"filename": filename,
		"total_rows": cint(total_rows),
		"file_size": _file_size_bytes(file_url),
		"view_axis": spec.view_axis,
		"file_format": file_format,
		"notify": notify,
	}


def get_export_job_status(job_id: str) -> dict[str, Any]:
	"""RQ status for a background Account Explorer export job."""
	from rq.job import Job

	from frappe.utils.background_jobs import get_redis_conn

	job_id = (job_id or "").strip()
	if not job_id:
		frappe.throw(_("Job ID is required."))

	try:
		job = Job.fetch(job_id, connection=get_redis_conn())
	except Exception:
		return {"ok": 0, "job_id": job_id, "status": "missing", "ui_status": "failed"}

	rq_status = job.get_status(refresh=True)
	ui_status = {
		"queued": "queued",
		"deferred": "queued",
		"scheduled": "queued",
		"started": "running",
		"finished": "ready",
		"failed": "failed",
		"stopped": "failed",
		"canceled": "failed",
	}.get(rq_status, rq_status)
	result = job.result if rq_status == "finished" else None
	out: dict[str, Any] = {
		"ok": 1,
		"job_id": job_id,
		"status": ui_status,
		"rq_status": rq_status,
		"queue": (job.origin or "").split(":")[-1] if job.origin else None,
		"enqueued_at": str(job.enqueued_at) if job.enqueued_at else None,
		"started_at": str(job.started_at) if job.started_at else None,
		"completed_at": str(job.ended_at) if job.ended_at else None,
		"ended_at": str(job.ended_at) if job.ended_at else None,
	}
	if isinstance(result, dict):
		out.update(
			{
				"file_url": result.get("file_url"),
				"filename": result.get("filename"),
				"file_name": result.get("filename"),
				"total_rows": result.get("total_rows"),
				"row_count": result.get("total_rows"),
				"file_size": result.get("file_size"),
				"view_axis": result.get("view_axis"),
			}
		)
	if ui_status == "failed":
		out["error"] = (job.exc_info or "").splitlines()[-1] if job.exc_info else _("Export job failed.")
	return out
