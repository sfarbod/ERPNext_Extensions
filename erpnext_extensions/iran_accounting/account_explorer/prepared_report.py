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


def extract_execution_id(payload: Any) -> str:
	"""Return client execution id, or mint a one-shot id when omitted.

	Polling clients must send a stable ``execution_id``. Omitting it forces a
	brand-new calculation on every request (always-fresh; cannot poll-reuse).
	"""
	data = payload
	if isinstance(payload, str):
		data = frappe.parse_json(payload) if payload else {}
	data = data or {}
	raw = (data.get("execution_id") or data.get("ae_execution_id") or "").strip()
	if raw:
		return raw[:64]
	return frappe.generate_hash(length=20)


def preparing_response(
	*,
	job_id: str,
	fingerprint: str,
	state: str,
	accounting_revision: int,
	execution_id: str | None = None,
	message: str | None = None,
) -> dict:
	from erpnext_extensions.iran_accounting.account_explorer.queue_policy import AE_PREPARED_QUEUE

	return {
		"status": "preparing",
		"job_id": job_id,
		"fingerprint": fingerprint,
		"execution_id": execution_id,
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
	"""Return ready summary or preparing status for one report execution.

	v5.5.36: Always-fresh accounting. Completed artifacts from prior executions
	are never reused. Only the current ``execution_id`` may be polled/presented.
	"""
	revision = get_accounting_revision(spec.company)
	fingerprint = build_fingerprint(spec, revision)
	execution_id = extract_execution_id(payload)
	existing = _get_prepared_by_execution(execution_id)

	if existing:
		# Filter change with a reused execution id → rebuild this execution only.
		if existing.fingerprint != fingerprint or cint(existing.accounting_revision) != revision:
			_reset_execution_for_rebuild(existing, spec, fingerprint, revision)
			existing.reload()

		if existing.status == "Completed":
			artifact = _load_artifact(existing)
			if artifact and cint(artifact.get("accounting_revision")) == revision:
				return _present_artifact(
					spec, columns, artifact, response_builder, execution_id=execution_id
				)
			_invalidate_prepared_for_rebuild(existing)
			existing.reload()

		if existing.status == "Error":
			return {
				"status": "Error",
				"state": "Error",
				"job_id": existing.name,
				"fingerprint": fingerprint,
				"execution_id": execution_id,
				"accounting_revision": revision,
				"message": existing.error_message
				or frappe._("Account Explorer preparation failed. Please try again."),
				"error_message": existing.error_message,
				"rows": [],
				"totals": {},
				"pagination": {"page": 1, "page_size": 50, "total_rows": 0, "has_next": False},
				"warnings": [],
			}

		if existing.status in ("Queued", "Started"):
			_ensure_prepared_job_running(existing.name, payload)
			existing.reload()
			if existing.status == "Completed":
				artifact = _load_artifact(existing)
				if artifact and cint(artifact.get("accounting_revision")) == revision:
					return _present_artifact(
						spec, columns, artifact, response_builder, execution_id=execution_id
					)
				_invalidate_prepared_for_rebuild(existing)
				existing.reload()
				_ensure_prepared_job_running(existing.name, payload)
				existing.reload()
			return preparing_response(
				job_id=existing.name,
				fingerprint=fingerprint,
				execution_id=execution_id,
				state=existing.status,
				accounting_revision=revision,
			)

	# Never consult other executions' Completed rows for this query fingerprint.
	job = _create_execution_job(spec, fingerprint, revision, execution_id)
	_enqueue_or_inline(job.name, payload)

	job.reload()
	if job.status == "Completed":
		artifact = _load_artifact(job)
		if artifact and cint(artifact.get("accounting_revision")) == revision:
			return _present_artifact(
				spec, columns, artifact, response_builder, execution_id=execution_id
			)
		_invalidate_prepared_for_rebuild(job)
		job.reload()
		_enqueue_or_inline(job.name, payload)
		job.reload()
		if job.status == "Completed":
			artifact = _load_artifact(job)
			if artifact and cint(artifact.get("accounting_revision")) == revision:
				return _present_artifact(
					spec, columns, artifact, response_builder, execution_id=execution_id
				)

	return preparing_response(
		job_id=job.name,
		fingerprint=fingerprint,
		execution_id=execution_id,
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


def _present_artifact(
	spec: AccountExplorerQuerySpec,
	columns,
	artifact: dict,
	response_builder,
	*,
	execution_id: str | None = None,
) -> dict:
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
	response["fresh"] = 1
	response["fingerprint"] = artifact.get("fingerprint")
	response["execution_id"] = execution_id or artifact.get("execution_id")
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
	"""Legacy helper: first row for a query fingerprint (diagnostics/tests only)."""
	name = frappe.db.get_value(PREPARED_DOCTYPE, {"fingerprint": fingerprint}, "name")
	if not name:
		return None
	return frappe.get_doc(PREPARED_DOCTYPE, name)


def _get_prepared_by_execution(execution_id: str):
	if not execution_id:
		return None
	name = frappe.db.get_value(PREPARED_DOCTYPE, {"execution_id": execution_id}, "name")
	if not name:
		return None
	return frappe.get_doc(PREPARED_DOCTYPE, name)


def _reset_execution_for_rebuild(doc, spec: AccountExplorerQuerySpec, fingerprint: str, revision: int) -> None:
	"""Reuse the same execution row when filters diverge mid-execution."""
	stale_url = doc.result_file
	doc.status = "Queued"
	doc.fingerprint = fingerprint
	doc.accounting_revision = revision
	doc.spec_hash = query_hash(spec)
	doc.view_axis = spec.view_axis
	doc.error_message = None
	doc.result_file = None
	doc.row_count = 0
	doc.flags.ignore_permissions = True
	doc.save(ignore_permissions=True)
	_unlink_local_artifact_path(stale_url)


def _execution_lock_key(execution_id: str) -> str:
	return f"ae:v511.6:exec-lock:{execution_id}"


def _with_execution_lock(execution_id: str, ttl: int = 30):
	"""Best-effort Redis lock so concurrent polls serialize job creation."""
	try:
		from frappe.utils.background_jobs import get_redis_conn

		conn = get_redis_conn()
		key = _execution_lock_key(execution_id)
		token = frappe.generate_hash(length=12)
		acquired = bool(conn.set(key, token, nx=True, ex=ttl))
		return conn, key, token, acquired
	except Exception:
		return None, None, None, False


def _release_execution_lock(conn, key, token) -> None:
	if not conn or not key or not token:
		return
	try:
		current = conn.get(key)
		expected = token.encode() if isinstance(token, str) else token
		if current in (token, expected):
			conn.delete(key)
	except Exception:
		pass


def _create_execution_job(
	spec: AccountExplorerQuerySpec, fingerprint: str, revision: int, execution_id: str
):
	"""Create (or race-safe reuse) the prepared row for this execution only."""
	existing = _get_prepared_by_execution(execution_id)
	if existing:
		if existing.status in ("Queued", "Started") and cint(existing.accounting_revision) == revision:
			return existing
		_reset_execution_for_rebuild(existing, spec, fingerprint, revision)
		existing.reload()
		return existing

	import time

	conn, lock_key, lock_token, acquired = _with_execution_lock(execution_id)
	try:
		# If another worker holds the lock, wait briefly for its insert.
		if not acquired:
			for _ in range(20):
				existing = _get_prepared_by_execution(execution_id)
				if existing:
					return existing
				time.sleep(0.05)
			# Fall through and attempt insert anyway.

		existing = _get_prepared_by_execution(execution_id)
		if existing:
			return existing

		doc = frappe.get_doc(
			{
				"doctype": PREPARED_DOCTYPE,
				"company": spec.company,
				"execution_id": execution_id,
				"fingerprint": fingerprint,
				"spec_hash": query_hash(spec),
				"accounting_revision": revision,
				"view_axis": spec.view_axis,
				"status": "Queued",
				"row_count": 0,
			}
		)
		doc.flags.ignore_permissions = True
		try:
			doc.insert(ignore_permissions=True)
			return doc
		except (frappe.UniqueValidationError, frappe.DuplicateEntryError) as exc:
			try:
				frappe.db.rollback()
			except Exception:
				pass
			raced = _get_prepared_by_execution(execution_id)
			if raced:
				return raced
			raise exc
	finally:
		_release_execution_lock(conn, lock_key, lock_token)


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


def _unlink_local_artifact_path(file_url: str | None) -> None:
	"""Best-effort removal of one private artifact path (this URL only)."""
	if not file_url or "/private/files/" not in file_url:
		return
	rel = file_url.split("/private/files/", 1)[-1]
	if not rel or ".." in rel:
		return
	path = frappe.get_site_path("private", "files", rel)
	try:
		import os

		if os.path.isfile(path):
			os.remove(path)
	except OSError:
		pass


def _invalidate_prepared_for_rebuild(doc) -> bool:
	"""Mark one prepared row for rebuild when its artifact is unreadable.

	Clears only this document's ``result_file`` pointer. Does not delete File
	rows belonging to other prepared results. Concurrent callers racing on the
	same Completed row are idempotent: a non-Completed status is left alone.
	"""
	if not doc or not doc.name:
		return False

	# Concurrent Desk polls can race on the same Completed row after a restore.
	# Retry briefly on MariaDB 1020 / deadlock; if another request already moved
	# the row out of Completed/Error, treat that as a successful no-op.
	for _attempt in range(3):
		try:
			frappe.db.sql(
				f"select name from `tab{PREPARED_DOCTYPE}` where name=%s for update",
				doc.name,
			)
			doc.reload()
			if doc.status not in ("Completed", "Error"):
				return False
			stale_url = doc.result_file
			doc.status = "Queued"
			doc.result_file = None
			doc.row_count = 0
			doc.error_message = None
			doc.flags.ignore_permissions = True
			doc.save(ignore_permissions=True)
			# Remove unreadable bytes at this URL so the rebuild can write a fresh file.
			# File DocType rows for other prepared results are left untouched.
			_unlink_local_artifact_path(stale_url)
			return True
		except (frappe.QueryDeadlockError, frappe.QueryTimeoutError):
			try:
				doc.reload()
			except Exception:
				return False
			if doc.status not in ("Completed", "Error"):
				return False
	return False


def _load_artifact(doc) -> dict | None:
	"""Load gzip JSON artifact, or return None when the file is missing/corrupt.

	Soft-fails only for restore/lifecycle failures (missing File doc, missing
	disk bytes, corrupt gzip, invalid JSON). Unexpected errors still raise.
	"""
	if not doc.result_file:
		return None
	try:
		file_doc = frappe.get_doc("File", {"file_url": doc.result_file})
	except frappe.DoesNotExistError:
		return None

	try:
		content = file_doc.get_content()
	except FileNotFoundError:
		return None
	except OSError:
		# Broken symlink / unreadable path after partial restore.
		return None

	if isinstance(content, str):
		content = content.encode("utf-8")
	if not content:
		return None

	try:
		raw = gzip.decompress(content)
	except OSError:
		raw = content

	try:
		if isinstance(raw, bytes):
			raw = raw.decode("utf-8")
		payload = json.loads(raw)
	except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
		return None

	return payload if isinstance(payload, dict) else None


def store_artifact(doc, artifact: dict) -> None:
	if getattr(doc, "execution_id", None) and not artifact.get("execution_id"):
		artifact["execution_id"] = doc.execution_id
	payload = gzip.compress(json.dumps(artifact, ensure_ascii=False, default=str).encode("utf-8"))
	# Include execution id + prepared-doc name so executions never collide on disk.
	exec_token = getattr(doc, "execution_id", None) or "noexec"
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"{doc.fingerprint}:{exec_token}:{doc.name}.json.gz",
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
