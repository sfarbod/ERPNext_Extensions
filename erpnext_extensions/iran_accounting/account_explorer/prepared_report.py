# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer prepared-result orchestration (Frappe Prepared Report pattern)."""

from __future__ import annotations

import gzip
import json
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.iran_accounting.account_explorer.cache_revision import get_accounting_revision
from erpnext_extensions.iran_accounting.account_explorer.constants import (
	CURRENCY_SORTABLE_FIELDS,
	DIMENSION_SORTABLE_FIELDS,
	PARTY_SORTABLE_FIELDS,
	SORTABLE_FIELDS,
	UNIFIED_PARTY_SORTABLE_FIELDS,
	VOUCHER_SORTABLE_FIELDS,
)
from erpnext_extensions.iran_accounting.account_explorer.pagination import paginate_summary_rows, sort_rows
from erpnext_extensions.iran_accounting.account_explorer.query_fingerprint import (
	build_fingerprint,
	query_hash,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec

PREPARED_DOCTYPE = "Account Explorer Prepared Result"
PREPARED_AXES = frozenset({"account_level", "voucher"})
RESULT_SCHEMA_VERSION = 1


def axis_uses_prepared_results(spec: AccountExplorerQuerySpec) -> bool:
	if spec.detail_mode != "summary":
		return False
	return spec.view_axis in PREPARED_AXES


def preparing_response(
	*,
	job_id: str,
	fingerprint: str,
	state: str,
	accounting_revision: int,
	message: str | None = None,
) -> dict:
	from erpnext_extensions.iran_accounting.account_explorer.queue_policy import AE_PREPARED_QUEUE

	return {
		"status": "preparing",
		"job_id": job_id,
		"fingerprint": fingerprint,
		"state": state,
		"queue": AE_PREPARED_QUEUE,
		"accounting_revision": accounting_revision,
		"message": message or frappe._("Preparing Account Explorer report…"),
		"rows": [],
		"totals": {},
		"pagination": {"page": 1, "page_size": 50, "total_rows": 0, "has_next": False},
		"warnings": [],
	}


def resolve_prepared_or_enqueue(
	spec: AccountExplorerQuerySpec,
	*,
	payload: Any,
	columns: list[dict],
	response_builder,
) -> dict:
	"""Return ready summary, preparing status, or live fallback."""
	revision = get_accounting_revision(spec.company)
	fingerprint = build_fingerprint(spec, revision)
	existing = _get_prepared_doc(fingerprint)

	if existing and existing.status == "Completed" and cint(existing.accounting_revision) == revision:
		artifact = _load_artifact(existing)
		if artifact and cint(artifact.get("accounting_revision")) == revision:
			return _present_artifact(spec, columns, artifact, response_builder)

	if existing and existing.status == "Error" and cint(existing.accounting_revision) == revision:
		return {
			"status": "Error",
			"state": "Error",
			"job_id": existing.name,
			"fingerprint": fingerprint,
			"accounting_revision": revision,
			"message": existing.error_message
			or frappe._("Account Explorer preparation failed. Please try again."),
			"error_message": existing.error_message,
			"rows": [],
			"totals": {},
			"pagination": {"page": 1, "page_size": 50, "total_rows": 0, "has_next": False},
			"warnings": [],
		}

	if existing and existing.status in ("Queued", "Started") and cint(existing.accounting_revision) == revision:
		# Poll path: ensure the worker job still exists (enqueue_after_commit can be lost).
		_ensure_prepared_job_running(existing.name, payload)
		existing.reload()
		if existing.status == "Completed":
			artifact = _load_artifact(existing)
			if artifact:
				return _present_artifact(spec, columns, artifact, response_builder)
		return preparing_response(
			job_id=existing.name,
			fingerprint=fingerprint,
			state=existing.status,
			accounting_revision=revision,
		)

	job = _create_or_reuse_job(spec, fingerprint, revision, existing)
	_enqueue_or_inline(job.name, payload)

	job.reload()
	if job.status == "Completed":
		artifact = _load_artifact(job)
		if artifact:
			return _present_artifact(spec, columns, artifact, response_builder)

	return preparing_response(
		job_id=job.name,
		fingerprint=fingerprint,
		state=job.status,
		accounting_revision=revision,
	)


def build_materialized_result(spec: AccountExplorerQuerySpec) -> dict:
	"""Build full-axis rows (no pagination) for storage."""
	axis = spec.view_axis
	if axis == "account_level":
		from erpnext_extensions.iran_accounting.account_explorer.query_builder import (
			build_account_level_summary,
		)

		return _materialize_paginated_builder(build_account_level_summary, spec)
	if axis == "voucher":
		return _materialize_voucher(spec)
	frappe.throw(frappe._("Prepared results are not supported for axis {0}.").format(axis))


def _materialize_paginated_builder(builder, spec: AccountExplorerQuerySpec) -> dict:
	original_page = spec.pagination.page
	original_size = spec.pagination.page_size
	try:
		spec.pagination.page = 1
		spec.pagination.page_size = 1_000_000
		# Bypass server_page_size clamp by calling builder after temporarily
		# expanding page_size on the already-parsed spec.
		result = builder(spec)
	finally:
		spec.pagination.page = original_page
		spec.pagination.page_size = original_size

	# Builder may still have clamped; if so, gather via page walk is not needed
	# for account axis (row count << 1e6). Keep rows as returned.
	return {
		"rows": list(result.get("rows") or []),
		"warnings": list(result.get("warnings") or []),
		"extras": {k: v for k, v in result.items() if k not in {"rows", "totals", "pagination", "warnings"}},
	}


def _materialize_voucher(spec: AccountExplorerQuerySpec) -> dict:
	"""Store lean voucher aggregates (no title enrichment) for the full scope."""
	from erpnext_extensions.iran_accounting.account_explorer.gle_filters import collect_scope_warnings
	from erpnext_extensions.iran_accounting.account_explorer.voucher_summary import (
		_create_scoped_voucher_temp_table,
		_order_by_sql_for_alias,
		_totals_from_temp_table,
	)

	table = _create_scoped_voucher_temp_table(spec)
	try:
		totals, total_rows = _totals_from_temp_table(table)
		order_sql = _order_by_sql_for_alias(spec)
		raw_rows = frappe.db.sql(
			f"""
			select voucher_type, voucher_no, posting_date, scoped_debit, scoped_credit
			from `{table}`
			order by {order_sql}
			""",
			as_dict=True,
		)
		rows = []
		for row in raw_rows:
			scoped_debit = flt(row.get("scoped_debit"))
			scoped_credit = flt(row.get("scoped_credit"))
			rows.append(
				{
					"row_key": f"voucher:{row.voucher_type}:{row.voucher_no}",
					"posting_date": str(row.get("posting_date") or ""),
					"voucher_type": row.voucher_type,
					"voucher_no": row.voucher_no,
					"party_type": "",
					"party": "",
					"party_name": "",
					"voucher_title": row.voucher_no,
					"reference": None,
					"scoped_debit": scoped_debit,
					"scoped_credit": scoped_credit,
					"scoped_net": scoped_debit - scoped_credit,
					"full_voucher_debit": 0.0,
					"full_voucher_credit": 0.0,
					"has_multiple_parties": 0,
					"is_virtual_group": 0,
					"drill_down_enabled": 1,
					"_lean": 1,
				}
			)
		return {
			"rows": rows,
			"warnings": collect_scope_warnings(spec),
			"extras": {"voucher_totals": totals, "voucher_total_rows": total_rows},
		}
	finally:
		frappe.db.sql(f"drop temporary table if exists `{table}`")


def _enrich_voucher_page(spec: AccountExplorerQuerySpec, page_rows: list[dict]) -> list[dict]:
	if not page_rows:
		return page_rows
	from frappe.utils import flt

	from erpnext_extensions.iran_accounting.account_explorer.voucher_metadata import enrich_voucher_rows
	from erpnext_extensions.iran_accounting.account_explorer.voucher_summary import (
		_enrich_party_fields,
		_full_voucher_amounts_for_keys,
		_party_name,
	)

	aggregates = [
		{
			"voucher_type": row.get("voucher_type"),
			"voucher_no": row.get("voucher_no"),
			"posting_date": row.get("posting_date"),
			"scoped_debit": row.get("scoped_debit"),
			"scoped_credit": row.get("scoped_credit"),
		}
		for row in page_rows
	]
	full_map = _full_voucher_amounts_for_keys(spec, aggregates)
	_enrich_party_fields(spec, aggregates, scoped=True)
	out = []
	for base, agg in zip(page_rows, aggregates, strict=False):
		voucher_type = agg.get("voucher_type")
		voucher_no = agg.get("voucher_no")
		full_row = full_map.get((voucher_type, voucher_no), {})
		party_type = agg.get("party_type") or ""
		party = agg.get("party") or ""
		scoped_debit = flt(agg.get("scoped_debit"))
		scoped_credit = flt(agg.get("scoped_credit"))
		out.append(
			{
				**base,
				"party_type": party_type,
				"party": party,
				"party_name": _party_name(party_type, party),
				"scoped_debit": scoped_debit,
				"scoped_credit": scoped_credit,
				"scoped_net": scoped_debit - scoped_credit,
				"full_voucher_debit": flt(full_row.get("scoped_debit")),
				"full_voucher_credit": flt(full_row.get("scoped_credit")),
				"has_multiple_parties": int(flt(agg.get("party_count") or 0) > 1),
				"_lean": 0,
			}
		)
	enrich_voucher_rows(out)
	return out


def _sortable_fields(spec: AccountExplorerQuerySpec):
	axis = spec.view_axis
	if axis == "party":
		return PARTY_SORTABLE_FIELDS
	if axis == "unified_party":
		return UNIFIED_PARTY_SORTABLE_FIELDS
	if axis == "dimension":
		return DIMENSION_SORTABLE_FIELDS
	if axis == "currency":
		return CURRENCY_SORTABLE_FIELDS
	if axis == "voucher":
		return VOUCHER_SORTABLE_FIELDS
	return SORTABLE_FIELDS


def _present_artifact(spec: AccountExplorerQuerySpec, columns, artifact: dict, response_builder) -> dict:
	rows = list(artifact.get("rows") or [])
	rows = sort_rows(rows, spec, _sortable_fields(spec))
	if spec.view_axis == "voucher":
		paged = _paginate_voucher_lean_rows(rows, spec, artifact.get("extras") or {})
		paged["rows"] = _enrich_voucher_page(spec, paged.get("rows") or [])
	else:
		paged = paginate_summary_rows(rows, spec)
	paged["warnings"] = list(artifact.get("warnings") or [])
	for key, value in (artifact.get("extras") or {}).items():
		if key in {"voucher_totals", "voucher_total_rows"}:
			continue
		paged[key] = value
	response = response_builder(spec, columns, paged)
	response["status"] = "ready"
	response["prepared"] = 1
	response["fingerprint"] = artifact.get("fingerprint")
	response["accounting_revision"] = artifact.get("accounting_revision")
	return response


def _paginate_voucher_lean_rows(rows: list[dict], spec: AccountExplorerQuerySpec, extras: dict) -> dict:
	if spec.hide_zero_rows:
		rows = [
			row
			for row in rows
			if flt(row.get("scoped_debit")) or flt(row.get("scoped_credit"))
		]
	total_rows = cint(extras.get("voucher_total_rows"))
	if not total_rows:
		total_rows = len(rows)
	page = max(cint(spec.pagination.page) or 1, 1)
	page_size = max(cint(spec.pagination.page_size) or 50, 1)
	offset = (page - 1) * page_size
	page_rows = rows[offset : offset + page_size]
	totals = extras.get("voucher_totals") or {
		"scoped_debit": sum(flt(r.get("scoped_debit")) for r in rows),
		"scoped_credit": sum(flt(r.get("scoped_credit")) for r in rows),
	}
	if "scoped_net" not in totals:
		totals["scoped_net"] = flt(totals.get("scoped_debit")) - flt(totals.get("scoped_credit"))
	return {
		"rows": page_rows,
		"totals": totals,
		"pagination": {
			"page": page,
			"page_size": page_size,
			"total_rows": total_rows,
			"has_next": offset + page_size < total_rows,
		},
	}


def _get_prepared_doc(fingerprint: str):
	name = frappe.db.get_value(PREPARED_DOCTYPE, {"fingerprint": fingerprint}, "name")
	if not name:
		return None
	return frappe.get_doc(PREPARED_DOCTYPE, name)


def _create_or_reuse_job(spec: AccountExplorerQuerySpec, fingerprint: str, revision: int, existing):
	if existing and cint(existing.accounting_revision) == revision and existing.status in (
		"Queued",
		"Started",
		"Completed",
	):
		return existing

	if existing:
		existing.status = "Queued"
		existing.accounting_revision = revision
		existing.spec_hash = query_hash(spec)
		existing.view_axis = spec.view_axis
		existing.error_message = None
		existing.result_file = None
		existing.row_count = 0
		existing.flags.ignore_permissions = True
		existing.save(ignore_permissions=True)
		return existing

	doc = frappe.get_doc(
		{
			"doctype": PREPARED_DOCTYPE,
			"company": spec.company,
			"fingerprint": fingerprint,
			"spec_hash": query_hash(spec),
			"accounting_revision": revision,
			"view_axis": spec.view_axis,
			"status": "Queued",
			"row_count": 0,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc


def _rq_job_id(job_name: str) -> str:
	return f"{frappe.local.site}||ae-prep-{job_name}"


def _rq_job_is_active(job_name: str) -> bool:
	try:
		from rq.job import Job

		from frappe.utils.background_jobs import get_redis_conn

		job = Job.fetch(_rq_job_id(job_name), connection=get_redis_conn())
		return job.get_status() in {"queued", "started", "deferred", "scheduled"}
	except Exception:
		return False


def _delete_stale_rq_job(job_name: str) -> None:
	try:
		from rq.job import Job

		from frappe.utils.background_jobs import get_redis_conn

		job = Job.fetch(_rq_job_id(job_name), connection=get_redis_conn())
		job.delete()
	except Exception:
		pass


def _ensure_prepared_job_running(job_name: str, payload: Any) -> None:
	"""Re-enqueue or inline-build if a Queued/Started doc has no active RQ job."""
	doc = frappe.get_doc(PREPARED_DOCTYPE, job_name)
	if doc.status == "Completed":
		return
	if doc.status == "Started" and _rq_job_is_active(job_name):
		return
	if doc.status == "Queued" and _rq_job_is_active(job_name):
		return
	_enqueue_or_inline(job_name, payload)


def _enqueue_or_inline(job_name: str, payload: Any) -> None:
	from erpnext_extensions.iran_accounting.account_explorer.background_jobs import (
		build_account_explorer_prepared_result,
		workers_available,
	)

	# Tests / explicit inline flag: build synchronously so callers receive ready data after enqueue path.
	if frappe.flags.in_test or getattr(frappe.flags, "ae_prepared_inline", False):
		if getattr(frappe.flags, "ae_prepared_defer", False):
			return
		build_account_explorer_prepared_result(job_name, payload)
		return

	if not workers_available():
		build_account_explorer_prepared_result(job_name, payload)
		return

	from erpnext_extensions.iran_accounting.account_explorer.queue_policy import (
		assert_queues_isolated,
		prepared_enqueue_kwargs,
	)

	assert_queues_isolated()
	# Persist Queued row before worker starts; do not rely on after-commit hooks.
	frappe.db.commit()
	_delete_stale_rq_job(job_name)
	try:
		# NOTE: frappe.enqueue reserves `job_name=` as the RQ label — do NOT pass the
		# prepared-doc name that way or the worker never receives it (TypeError).
		frappe.enqueue(
			"erpnext_extensions.iran_accounting.account_explorer.background_jobs.build_account_explorer_prepared_result",
			**prepared_enqueue_kwargs(
				job_id=_rq_job_id(job_name).split("||", 1)[-1],
				prepared_result_name=job_name,
				payload=payload,
				enqueue_after_commit=False,
			),
		)
	except Exception:
		# DuplicateJobError or redis blip: fall back to inline so UI cannot hang forever.
		if not _rq_job_is_active(job_name):
			build_account_explorer_prepared_result(job_name, payload)


def _load_artifact(doc) -> dict | None:
	if not doc.result_file:
		return None
	file_doc = frappe.get_doc("File", {"file_url": doc.result_file})
	content = file_doc.get_content()
	if isinstance(content, str):
		content = content.encode("utf-8")
	try:
		raw = gzip.decompress(content)
	except OSError:
		raw = content
	return json.loads(raw.decode("utf-8"))


def store_artifact(doc, artifact: dict) -> None:
	payload = gzip.compress(json.dumps(artifact, ensure_ascii=False, default=str).encode("utf-8"))
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"{doc.fingerprint}.json.gz",
			"is_private": 1,
			"content": payload,
			"attached_to_doctype": PREPARED_DOCTYPE,
			"attached_to_name": doc.name,
		}
	)
	file_doc.flags.ignore_permissions = True
	file_doc.insert(ignore_permissions=True)
	doc.result_file = file_doc.file_url
	doc.row_count = len(artifact.get("rows") or [])
	doc.status = "Completed"
	doc.error_message = None
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
