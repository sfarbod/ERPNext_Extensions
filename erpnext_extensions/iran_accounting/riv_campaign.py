# Copyright (c) 2026, ERPNext Extensions contributors
"""Bench-only deterministic full-history RIV generation (5.5.29).

Generation only. Not whitelisted. Does not execute Repost Item Valuation.
Does not manage scheduler state. Does not mark duplicates Skipped.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

from erpnext.controllers.stock_controller import create_item_wise_repost_entries

SOURCE_DOCTYPES = (
	"Purchase Receipt",
	"Stock Entry",
	"Delivery Note",
	"Stock Reconciliation",
)

DEFAULT_BATCH_SIZE = 500
MAX_BATCH_SIZE = 1000

# Production parallel job name (also present on local Espad sites).
PARALLEL_JOB_NAME = "cbd5p88qfh"
PARALLEL_METHOD = (
	"erpnext.stock.doctype.repost_item_valuation.repost_item_valuation.run_parallel_reposting"
)
SEQUENTIAL_METHOD = (
	"erpnext.stock.doctype.repost_item_valuation.repost_item_valuation.repost_entries"
)
WEEKLY_METHOD = (
	"erpnext.stock.doctype.stock_reposting_settings.stock_reposting_settings"
	".repost_incorrect_valuation_entries"
)

STATE_DOCTYPE = "RIV Campaign State"


class RivCampaignError(frappe.ValidationError):
	"""Fail-closed campaign guard / generation error."""


def _posting_time_key(value) -> str:
	"""Stable string for ordering/checksum (handles timedelta / str / None)."""
	if value is None:
		return ""
	return str(value)


def _row_identity(row: dict) -> str:
	return "|".join(
		[
			str(row["doctype"]),
			str(row["name"]),
			str(row["posting_date"]),
			_posting_time_key(row.get("posting_time")),
			str(row["creation"]),
			str(row.get("company") or ""),
		]
	)


def _normalize_stock_entry_purpose(stock_entry_purpose: str | None) -> str | None:
	"""Optional Stock Entry.purpose filter. None preserves the full multi-doctype universe."""
	if stock_entry_purpose in (None, ""):
		return None
	purpose = str(stock_entry_purpose).strip()
	if not purpose:
		return None
	return purpose


def build_source_manifest(stock_entry_purpose: str | None = None) -> list[dict[str, Any]]:
	"""Submitted stock-affecting sources in global chronological order.

	When ``stock_entry_purpose`` is omitted, behavior is unchanged: all submitted
	rows from ``SOURCE_DOCTYPES``.

	When set (e.g. ``\"Manufacture\"``), the manifest contains only submitted
	Stock Entries with that purpose — other voucher DocTypes are excluded for
	that campaign universe. Manufacture is a Stock Entry purpose, not a DocType.
	"""
	purpose = _normalize_stock_entry_purpose(stock_entry_purpose)
	rows: list[dict[str, Any]] = []

	if purpose:
		found = frappe.get_all(
			"Stock Entry",
			filters={"docstatus": 1, "purpose": purpose},
			fields=["name", "company", "posting_date", "posting_time", "creation"],
			order_by="posting_date asc, posting_time asc, creation asc, name asc",
			limit_page_length=0,
		)
		for r in found:
			rows.append(
				{
					"doctype": "Stock Entry",
					"name": r.name,
					"company": r.company,
					"posting_date": r.posting_date,
					"posting_time": r.posting_time,
					"creation": r.creation,
				}
			)
	else:
		for doctype in SOURCE_DOCTYPES:
			found = frappe.get_all(
				doctype,
				filters={"docstatus": 1},
				fields=["name", "company", "posting_date", "posting_time", "creation"],
				order_by="posting_date asc, posting_time asc, creation asc, name asc",
				limit_page_length=0,
			)
			for r in found:
				rows.append(
					{
						"doctype": doctype,
						"name": r.name,
						"company": r.company,
						"posting_date": r.posting_date,
						"posting_time": r.posting_time,
						"creation": r.creation,
					}
				)

	rows.sort(
		key=lambda r: (
			str(r["posting_date"]),
			_posting_time_key(r.get("posting_time")),
			str(r["creation"]),
			str(r["doctype"]),
			str(r["name"]),
		)
	)
	for i, r in enumerate(rows, start=1):
		r["ordinal"] = i
	return rows


def manifest_checksum(rows: list[dict[str, Any]]) -> str:
	"""SHA-256 over ordinal identity lines. Deterministic for the same universe."""
	h = hashlib.sha256()
	for r in rows:
		h.update(_row_identity(r).encode("utf-8"))
		h.update(b"\n")
	return h.hexdigest()


def _doctype_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
	counts = {dt: 0 for dt in SOURCE_DOCTYPES}
	for r in rows:
		counts[r["doctype"]] = counts.get(r["doctype"], 0) + 1
	return counts


def riv_status_counts() -> dict[str, int]:
	total = cint(frappe.db.count("Repost Item Valuation"))
	out = {"total": total}
	for status in ("Queued", "In Progress", "Completed", "Failed", "Skipped"):
		out[status.lower().replace(" ", "_")] = cint(
			frappe.db.count("Repost Item Valuation", {"status": status})
		)
	return out


def _job_by_method(method: str) -> dict | None:
	rows = frappe.get_all(
		"Scheduled Job Type",
		filters={"method": method},
		fields=["name", "method", "stopped"],
		limit_page_length=5,
	)
	return rows[0] if rows else None


def scheduler_guard_states() -> dict[str, Any]:
	"""Read-only view of the three RIV-related scheduled jobs."""
	parallel = frappe.db.get_value(
		"Scheduled Job Type",
		PARALLEL_JOB_NAME,
		["name", "method", "stopped"],
		as_dict=True,
	)
	return {
		"parallel": parallel,
		"sequential": _job_by_method(SEQUENTIAL_METHOD),
		"weekly": _job_by_method(WEEKLY_METHOD),
	}


def assert_schedulers_stopped() -> None:
	"""Fail closed if any RIV executor job is active. Does not mutate scheduler."""
	parallel = frappe.db.get_value(
		"Scheduled Job Type",
		PARALLEL_JOB_NAME,
		["name", "method", "stopped"],
		as_dict=True,
	)
	if not parallel:
		raise RivCampaignError(
			f"Scheduled Job Type {PARALLEL_JOB_NAME} not found; refuse generation."
		)
	if parallel.method != PARALLEL_METHOD:
		raise RivCampaignError(
			f"Scheduled Job Type {PARALLEL_JOB_NAME} method mismatch: "
			f"expected {PARALLEL_METHOD!r}, got {parallel.method!r}."
		)
	if not cint(parallel.stopped):
		raise RivCampaignError(
			f"Parallel RIV scheduler {PARALLEL_JOB_NAME} is not stopped "
			f"(stopped={parallel.stopped}). Stop it before generation."
		)

	for label, method in (
		("sequential", SEQUENTIAL_METHOD),
		("weekly", WEEKLY_METHOD),
	):
		job = _job_by_method(method)
		if not job:
			raise RivCampaignError(
				f"{label} RIV scheduler job for method {method!r} not found; refuse generation."
			)
		if not cint(job.stopped):
			raise RivCampaignError(
				f"{label} RIV scheduler {job.name} is not stopped "
				f"(stopped={job.stopped}). Stop it before generation."
			)


def assert_riv_empty_for_fresh_campaign() -> None:
	counts = riv_status_counts()
	if counts["total"] != 0:
		raise RivCampaignError(
			f"Repost Item Valuation is not empty (total={counts['total']}). "
			"Fresh campaign requires Total=0."
		)
	for key in ("queued", "in_progress", "completed", "failed", "skipped"):
		if counts[key] != 0:
			raise RivCampaignError(
				f"Repost Item Valuation {key}={counts[key]}; fresh campaign requires 0."
			)


def assert_continuation_riv_safe() -> None:
	counts = riv_status_counts()
	if counts["in_progress"] != 0:
		raise RivCampaignError(
			f"RIV In Progress={counts['in_progress']}; refuse continuation."
		)
	if counts["completed"] != 0:
		raise RivCampaignError(
			f"RIV Completed={counts['completed']}; refuse continuation "
			"(execution must not have started)."
		)
	if counts["failed"] != 0:
		raise RivCampaignError(
			f"RIV Failed={counts['failed']}; refuse continuation."
		)


def _get_state(create: bool = False):
	if not frappe.db.exists("DocType", STATE_DOCTYPE):
		raise RivCampaignError(f"DocType {STATE_DOCTYPE} is missing; migrate first.")
	if create and not frappe.db.exists(STATE_DOCTYPE, STATE_DOCTYPE):
		# Singles row materializes on first get_doc/save
		pass
	return frappe.get_single(STATE_DOCTYPE)


def _campaign_initialized(state) -> bool:
	return bool(state.campaign_id and state.manifest_checksum and cint(state.source_total) > 0)


def _normalize_batch_size(batch_size) -> int:
	size = cint(batch_size) if batch_size not in (None, "") else DEFAULT_BATCH_SIZE
	if size <= 0:
		raise RivCampaignError(f"batch_size must be positive, got {batch_size!r}.")
	if size > MAX_BATCH_SIZE:
		raise RivCampaignError(
			f"batch_size {size} exceeds hard maximum {MAX_BATCH_SIZE}."
		)
	return size


def _initialize_campaign(state, rows: list[dict[str, Any]], checksum: str) -> None:
	assert_schedulers_stopped()
	assert_riv_empty_for_fresh_campaign()

	counts = _doctype_counts(rows)
	if not rows:
		raise RivCampaignError("Source universe is empty; nothing to generate.")

	first, last = rows[0], rows[-1]
	state.campaign_id = f"RIV-GEN-{now_datetime().strftime('%Y%m%d%H%M%S')}"
	state.manifest_checksum = checksum
	state.source_total = len(rows)
	state.count_purchase_receipt = counts.get("Purchase Receipt", 0)
	state.count_stock_entry = counts.get("Stock Entry", 0)
	state.count_delivery_note = counts.get("Delivery Note", 0)
	state.count_stock_reconciliation = counts.get("Stock Reconciliation", 0)
	state.first_doctype = first["doctype"]
	state.first_name = first["name"]
	state.first_posting_date = first["posting_date"]
	state.first_posting_time = _posting_time_key(first.get("posting_time"))
	state.last_doctype = last["doctype"]
	state.last_name = last["name"]
	state.last_posting_date = last["posting_date"]
	state.last_posting_time = _posting_time_key(last.get("posting_time"))
	state.last_successful_ordinal = 0
	state.last_successful_doctype = None
	state.last_successful_voucher = None
	state.campaign_created_riv = 0
	state.generation_started = now_datetime()
	state.generation_completed = 0
	state.error_state = None
	state.flags.ignore_permissions = True
	state.save(ignore_permissions=True)
	frappe.db.commit()


def _assert_manifest_unchanged(state, rows: list[dict[str, Any]], checksum: str) -> None:
	if checksum != state.manifest_checksum:
		raise RivCampaignError(
			"Source manifest checksum changed since campaign initialization. "
			f"stored={state.manifest_checksum} current={checksum}. Fail closed."
		)
	if len(rows) != cint(state.source_total):
		raise RivCampaignError(
			f"Source total changed: stored={state.source_total} current={len(rows)}."
		)
	if rows:
		first, last = rows[0], rows[-1]
		if first["doctype"] != state.first_doctype or first["name"] != state.first_name:
			raise RivCampaignError("Manifest first voucher changed; fail closed.")
		if last["doctype"] != state.last_doctype or last["name"] != state.last_name:
			raise RivCampaignError("Manifest last voucher changed; fail closed.")


def _result_shell(state, rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
	counts = riv_status_counts()
	remaining = max(0, cint(state.source_total) - cint(state.last_successful_ordinal))
	total = cint(state.source_total)
	progress = round(100.0 * cint(state.last_successful_ordinal) / total, 4) if total else 0.0
	return {
		"ok": True,
		"campaign_id": state.campaign_id,
		"manifest_checksum": state.manifest_checksum,
		"source_total": total,
		"last_successful_ordinal": cint(state.last_successful_ordinal),
		"last_successful_doctype": state.last_successful_doctype,
		"last_successful_voucher": state.last_successful_voucher,
		"campaign_created_riv": cint(state.campaign_created_riv),
		"generation_completed": cint(state.generation_completed),
		"remaining": remaining,
		"progress_percent": progress,
		"riv_total": counts["total"],
		"riv_queued": counts["queued"],
		"riv_skipped": counts["skipped"],
		"riv_in_progress": counts["in_progress"],
		"riv_completed": counts["completed"],
		"riv_failed": counts["failed"],
	}


def _ensure_campaign_ready(
	stock_entry_purpose: str | None = None,
) -> tuple[Any, list[dict[str, Any]], str]:
	"""Load/init campaign and return (state, rows, checksum). Raises on guard failure."""
	state = _get_state()
	rows = build_source_manifest(stock_entry_purpose=stock_entry_purpose)
	checksum = manifest_checksum(rows)

	if not _campaign_initialized(state):
		_initialize_campaign(state, rows, checksum)
		state = _get_state()
	else:
		assert_schedulers_stopped()
		assert_continuation_riv_safe()
		_assert_manifest_unchanged(state, rows, checksum)

	return state, rows, checksum


def _recheck_continuation_safety(state, rows: list[dict[str, Any]], checksum: str) -> None:
	"""Full safety recheck used at the start of every internal run_all batch."""
	assert_schedulers_stopped()
	assert_continuation_riv_safe()
	_assert_manifest_unchanged(state, rows, checksum)


def _process_one_batch(
	*,
	state,
	rows: list[dict[str, Any]],
	size: int,
	internal_batch_number: int = 1,
) -> dict[str, Any]:
	"""Process one observation batch. Commits once per successful voucher."""
	if cint(state.generation_completed):
		out = _result_shell(state, rows)
		out["batch_start_ordinal"] = None
		out["batch_end_ordinal"] = None
		out["batch_processed"] = 0
		out["batch_created_riv"] = 0
		out["internal_batch_number"] = internal_batch_number
		out["message"] = "generation already completed"
		return out

	start = cint(state.last_successful_ordinal) + 1
	if start > cint(state.source_total):
		state.generation_completed = 1
		state.flags.ignore_permissions = True
		state.save(ignore_permissions=True)
		frappe.db.commit()
		out = _result_shell(state, rows)
		out["batch_processed"] = 0
		out["batch_created_riv"] = 0
		out["internal_batch_number"] = internal_batch_number
		return out

	end = min(cint(state.last_successful_ordinal) + size, cint(state.source_total))
	batch_created = 0
	batch_processed = 0

	# Clear prior error when continuing successfully into a new batch attempt
	if state.error_state:
		state.error_state = None
		state.flags.ignore_permissions = True
		state.save(ignore_permissions=True)
		frappe.db.commit()

	for ordinal in range(start, end + 1):
		row = rows[ordinal - 1]
		if row["ordinal"] != ordinal:
			frappe.db.rollback()
			raise RivCampaignError(
				f"Internal ordinal mismatch: expected {ordinal}, row has {row['ordinal']}."
			)
		try:
			created = create_item_wise_repost_entries(row["doctype"], row["name"])
			n = len(created or [])
			batch_created += n
			batch_processed += 1

			state = _get_state()
			state.last_successful_ordinal = ordinal
			state.last_successful_doctype = row["doctype"]
			state.last_successful_voucher = row["name"]
			state.campaign_created_riv = cint(state.campaign_created_riv) + n
			if ordinal == cint(state.source_total):
				state.generation_completed = 1
			state.error_state = None
			state.flags.ignore_permissions = True
			state.save(ignore_permissions=True)
			# Atomic: RIV inserts from create_item_wise_repost_entries + checkpoint
			frappe.db.commit()
		except Exception as exc:
			frappe.db.rollback()
			error_info = {
				"ordinal": ordinal,
				"doctype": row["doctype"],
				"voucher": row["name"],
				"posting_date": str(row.get("posting_date")),
				"posting_time": _posting_time_key(row.get("posting_time")),
				"exception_class": type(exc).__name__,
				"message": str(exc)[:500],
			}
			# Persist error marker without advancing ordinal (separate txn)
			try:
				state = _get_state()
				state.error_state = json.dumps(error_info, default=str)
				state.flags.ignore_permissions = True
				state.save(ignore_permissions=True)
				frappe.db.commit()
			except Exception:
				frappe.db.rollback()
			out = _result_shell(_get_state(), rows)
			out["ok"] = False
			out["batch_start_ordinal"] = start
			out["batch_end_ordinal"] = end
			out["batch_processed"] = batch_processed
			out["batch_created_riv"] = batch_created
			out["internal_batch_number"] = internal_batch_number
			out["error"] = error_info
			return out

	state = _get_state()
	if cint(state.generation_completed):
		assert_continuation_riv_safe()
		assert_schedulers_stopped()

	out = _result_shell(state, rows)
	out["batch_start_ordinal"] = start
	out["batch_end_ordinal"] = cint(state.last_successful_ordinal)
	out["batch_processed"] = batch_processed
	out["batch_created_riv"] = batch_created
	out["internal_batch_number"] = internal_batch_number
	return out


def _print_batch_progress(out: dict[str, Any]) -> None:
	"""Compact progress line for long run_all sessions."""
	print(
		json.dumps(
			{
				"progress": True,
				"campaign_id": out.get("campaign_id"),
				"manifest_checksum": out.get("manifest_checksum"),
				"source_total": out.get("source_total"),
				"internal_batch_number": out.get("internal_batch_number"),
				"batch_start_ordinal": out.get("batch_start_ordinal"),
				"batch_end_ordinal": out.get("batch_end_ordinal"),
				"batch_processed": out.get("batch_processed"),
				"last_successful_ordinal": out.get("last_successful_ordinal"),
				"last_successful_doctype": out.get("last_successful_doctype"),
				"last_successful_voucher": out.get("last_successful_voucher"),
				"remaining": out.get("remaining"),
				"batch_created_riv": out.get("batch_created_riv"),
				"campaign_created_riv": out.get("campaign_created_riv"),
				"riv_total": out.get("riv_total"),
				"riv_queued": out.get("riv_queued"),
				"riv_skipped": out.get("riv_skipped"),
				"riv_in_progress": out.get("riv_in_progress"),
				"riv_completed": out.get("riv_completed"),
				"riv_failed": out.get("riv_failed"),
				"generation_completed": out.get("generation_completed"),
				"ok": out.get("ok"),
			},
			default=str,
		),
		flush=True,
	)


def generate_full_riv_campaign(
	batch_size: int | None = None,
	run_all: bool = False,
	max_batches: int | None = None,
	stock_entry_purpose: str | None = None,
) -> dict[str, Any]:
	"""Generate Item-and-Warehouse RIVs. Bench-only. Not whitelisted.

	``run_all=False`` (default): process one observation batch and return.
	``run_all=True``: iterate remaining batches until complete or a guard/error stops.

	``max_batches`` optionally caps internal iterations (testing / controlled proof).
	Omit it for full remaining history.

	``stock_entry_purpose`` optionally restricts the source universe to submitted
	Stock Entries with that purpose (e.g. ``\"Manufacture\"``). When omitted,
	behavior is unchanged (all ``SOURCE_DOCTYPES``). Pass the same purpose on
	every continuation call so the manifest checksum matches.

	Calls create_item_wise_repost_entries(doctype, name) only. Commits once per
	successful source voucher together with the checkpoint advance.

	Volume note: native ``create_item_wise_repost_entries`` emits one Item-and-
	Warehouse RIV per distinct SLE pair on each source voucher. Across a full
	Manufacture history the same item/warehouse therefore appears many times;
	ERPNext ``deduplicate_similar_repost`` marks later Queued duplicates
	``Skipped`` when an earlier RIV for that pair Completes. That redundancy is
	intentional native coverage, not a campaign bug — do not thin the universe
	by skipping create calls.
	"""
	size = _normalize_batch_size(batch_size)
	purpose = _normalize_stock_entry_purpose(stock_entry_purpose)
	if max_batches is not None:
		max_batches = cint(max_batches)
		if max_batches <= 0:
			raise RivCampaignError(f"max_batches must be positive, got {max_batches!r}.")

	state, rows, checksum = _ensure_campaign_ready(stock_entry_purpose=purpose)

	if not run_all:
		out = _process_one_batch(state=state, rows=rows, size=size, internal_batch_number=1)
		out["stock_entry_purpose"] = purpose
		return out

	# run_all: iterative loop — never recurse into generate_full_riv_campaign
	batch_summaries: list[dict[str, Any]] = []
	internal_batch = 0
	last_out: dict[str, Any] | None = None
	campaign_created_at_start = cint(state.campaign_created_riv)

	while True:
		state = _get_state()
		if cint(state.generation_completed):
			break
		if cint(state.last_successful_ordinal) >= cint(state.source_total):
			break
		if max_batches is not None and internal_batch >= max_batches:
			break

		# Rebuild + recheck between every internal batch (including the first)
		rows = build_source_manifest(stock_entry_purpose=purpose)
		checksum = manifest_checksum(rows)
		state = _get_state()
		_recheck_continuation_safety(state, rows, checksum)

		internal_batch += 1
		last_out = _process_one_batch(
			state=state,
			rows=rows,
			size=size,
			internal_batch_number=internal_batch,
		)
		_print_batch_progress(last_out)
		batch_summaries.append(
			{
				"internal_batch_number": internal_batch,
				"batch_start_ordinal": last_out.get("batch_start_ordinal"),
				"batch_end_ordinal": last_out.get("batch_end_ordinal"),
				"batch_processed": last_out.get("batch_processed"),
				"batch_created_riv": last_out.get("batch_created_riv"),
				"ok": last_out.get("ok"),
			}
		)

		if not last_out.get("ok", True):
			last_out["run_all"] = True
			last_out["stock_entry_purpose"] = purpose
			last_out["internal_batches_completed"] = internal_batch
			last_out["batch_summaries"] = batch_summaries
			last_out["run_all_created_riv"] = (
				cint(last_out.get("campaign_created_riv")) - campaign_created_at_start
			)
			return last_out

		if cint(last_out.get("generation_completed")):
			break
		if not cint(last_out.get("batch_processed")):
			# No progress — avoid infinite loop
			break

	state = _get_state()
	out = last_out or _result_shell(state, rows)
	out["run_all"] = True
	out["stock_entry_purpose"] = purpose
	out["internal_batches_completed"] = internal_batch
	out["batch_summaries"] = batch_summaries
	out["run_all_created_riv"] = cint(state.campaign_created_riv) - campaign_created_at_start
	out["stopped_by_max_batches"] = bool(
		max_batches is not None
		and internal_batch >= max_batches
		and not cint(state.generation_completed)
	)
	return out


def get_full_riv_campaign_status() -> dict[str, Any]:
	"""Read-only campaign + RIV + scheduler status. Bench-only. Not whitelisted."""
	state = _get_state()
	counts = riv_status_counts()
	remaining = max(0, cint(state.source_total) - cint(state.last_successful_ordinal))
	total = cint(state.source_total)
	progress = round(100.0 * cint(state.last_successful_ordinal) / total, 4) if total else 0.0
	return {
		"campaign_id": state.campaign_id,
		"manifest_checksum": state.manifest_checksum,
		"source_total": total,
		"last_successful_ordinal": cint(state.last_successful_ordinal),
		"last_successful_doctype": state.last_successful_doctype,
		"last_successful_voucher": state.last_successful_voucher,
		"campaign_created_riv": cint(state.campaign_created_riv),
		"generation_completed": cint(state.generation_completed),
		"generation_started": str(state.generation_started) if state.generation_started else None,
		"remaining": remaining,
		"progress_percent": progress,
		"run_all_supported": True,
		"error_state": state.error_state,
		"counts_by_doctype": {
			"Purchase Receipt": cint(state.count_purchase_receipt),
			"Stock Entry": cint(state.count_stock_entry),
			"Delivery Note": cint(state.count_delivery_note),
			"Stock Reconciliation": cint(state.count_stock_reconciliation),
		},
		"first": {
			"doctype": state.first_doctype,
			"name": state.first_name,
			"posting_date": str(state.first_posting_date) if state.first_posting_date else None,
			"posting_time": state.first_posting_time,
		},
		"last": {
			"doctype": state.last_doctype,
			"name": state.last_name,
			"posting_date": str(state.last_posting_date) if state.last_posting_date else None,
			"posting_time": state.last_posting_time,
		},
		"riv": counts,
		"schedulers": scheduler_guard_states(),
	}
