# Copyright (c) 2026, ERPNext Extensions contributors
"""Campaign-scoped RIV_SETTLEMENT_BARRIER.

Replaces SQL status mutation and fixed sleeps. Tracks exact RIV names created
by a campaign operation and waits for legitimate terminal statuses.

Forbidden: UPDATE status='Skipped' on Queued / In Progress.
"""

from __future__ import annotations

from time import perf_counter, sleep
from typing import Iterable

import frappe

from erpnext_extensions.iran_accounting.historical_stock.worker_preflight import (
	WORKER_PREFLIGHT_BLOCKED,
	run_worker_preflight,
)

RIV_SETTLEMENT_COMPLETE = "RIV_SETTLEMENT_COMPLETE"
RIV_SETTLEMENT_TIMEOUT = "RIV_SETTLEMENT_TIMEOUT"
RIV_SETTLEMENT_FAILED = "RIV_SETTLEMENT_FAILED"
RIV_SETTLEMENT_BLOCKED = "RIV_SETTLEMENT_BLOCKED"

TERMINAL_SUCCESS = frozenset({"Completed"})
TERMINAL_FAILURE = frozenset({"Failed"})
# Skipped is terminal for ERPNext but NOT an accepted campaign outcome.
TERMINAL_UNEXPECTED = frozenset({"Skipped"})
OPEN_STATUSES = frozenset({"Queued", "In Progress"})

STALE_QUEUED_RIV = "STALE_QUEUED_RIV"
ORPHANED_IN_PROGRESS_RIV = "ORPHANED_IN_PROGRESS_RIV"
WORKER_LOST_DURING_RIV = "WORKER_LOST_DURING_RIV"
UNEXPECTED_SKIPPED_RIV = "UNEXPECTED_SKIPPED_RIV"


class CampaignRivRegistry:
	"""Track RIVs created/required by one campaign operation."""

	def __init__(self, operation_id: str, *, root_id: str | None = None):
		self.operation_id = operation_id
		self.root_id = root_id or operation_id
		self.entries: list[dict] = []

	def register(
		self,
		riv_name: str,
		*,
		item: str | None = None,
		warehouse: str | None = None,
		voucher: str | None = None,
		expected_terminal: str = "Completed",
		job_id: str | None = None,
		phase: str | None = None,
	) -> None:
		if not riv_name:
			return
		self.entries.append(
			{
				"operation_id": self.operation_id,
				"root_id": self.root_id,
				"riv_name": riv_name,
				"item": item,
				"warehouse": warehouse,
				"voucher": voucher,
				"expected_terminal": expected_terminal,
				"job_id": job_id,
				"phase": phase,
				"registered_at": str(frappe.utils.now_datetime()),
			}
		)

	def names(self) -> list[str]:
		return [e["riv_name"] for e in self.entries if e.get("riv_name")]

	def to_manifest(self) -> dict:
		return {
			"operation_id": self.operation_id,
			"root_id": self.root_id,
			"n": len(self.entries),
			"entries": list(self.entries),
		}


def _read_riv(name: str) -> dict | None:
	row = frappe.db.get_value(
		"Repost Item Valuation",
		name,
		["name", "status", "item_code", "warehouse", "voucher_no", "modified", "creation", "error_log"],
		as_dict=True,
	)
	return row


def _classify_open(row: dict, *, workers_ready: bool) -> str | None:
	"""Return infrastructure failure code if open RIV is stale/orphaned."""
	if not row:
		return None
	st = row.get("status")
	if st == "Queued" and not workers_ready:
		return STALE_QUEUED_RIV
	if st == "In Progress" and not workers_ready:
		return ORPHANED_IN_PROGRESS_RIV
	if st in OPEN_STATUSES and not workers_ready:
		return WORKER_LOST_DURING_RIV
	return None


def wait_for_rivs(
	riv_names: Iterable[str],
	*,
	timeout_s: float = 600.0,
	poll_s: float = 1.0,
	require_workers: bool = True,
	queues: tuple[str, ...] = ("long",),
	operation_id: str | None = None,
) -> dict:
	"""Poll campaign-scoped RIV names until terminal success or hard stop.

	Never mutates RIV status.
	"""
	t0 = perf_counter()
	names = [n for n in riv_names if n]
	if not names:
		return {
			"status": RIV_SETTLEMENT_COMPLETE,
			"ready": True,
			"operation_id": operation_id,
			"tracked_n": 0,
			"settled": [],
			"elapsed": 0.0,
		}

	settled: list[dict] = []
	pending = set(names)
	deadline = t0 + timeout_s

	while pending and perf_counter() < deadline:
		workers = run_worker_preflight(queues=queues, require_probe=False)
		workers_ready = bool(workers.get("ready"))
		if require_workers and not workers_ready:
			# Keep waiting only if still within timeout; classify stale on timeout.
			pass

		still = set()
		for name in list(pending):
			row = _read_riv(name)
			if not row:
				still.add(name)
				continue
			st = row.status
			rec = {
				"riv_name": name,
				"status": st,
				"item": row.item_code,
				"warehouse": row.warehouse,
				"modified": str(row.modified),
			}
			if st in TERMINAL_SUCCESS:
				settled.append({**rec, "outcome": "SUCCESS"})
				continue
			if st in TERMINAL_FAILURE:
				return {
					"status": RIV_SETTLEMENT_FAILED,
					"ready": False,
					"operation_id": operation_id,
					"failed": {**rec, "error_log": (row.error_log or "")[:500]},
					"settled": settled,
					"pending": sorted(pending),
					"workers": workers,
					"elapsed": round(perf_counter() - t0, 3),
				}
			if st in TERMINAL_UNEXPECTED:
				return {
					"status": RIV_SETTLEMENT_BLOCKED,
					"ready": False,
					"code": UNEXPECTED_SKIPPED_RIV,
					"operation_id": operation_id,
					"unexpected": rec,
					"settled": settled,
					"pending": sorted(pending),
					"workers": workers,
					"elapsed": round(perf_counter() - t0, 3),
					"message": "Campaign RIV reached Skipped — not an accepted settlement.",
				}
			if st in OPEN_STATUSES:
				code = _classify_open(row, workers_ready=workers_ready)
				if code and (perf_counter() - t0) > min(30.0, timeout_s / 5):
					# After grace, open + no workers = infrastructure stop
					if not workers_ready:
						return {
							"status": RIV_SETTLEMENT_BLOCKED,
							"ready": False,
							"code": code,
							"operation_id": operation_id,
							"open": rec,
							"settled": settled,
							"pending": sorted(pending),
							"workers": {
								"status": WORKER_PREFLIGHT_BLOCKED,
								"blockers": workers.get("blockers"),
							},
							"elapsed": round(perf_counter() - t0, 3),
						}
				still.add(name)
			else:
				# Unknown status — block
				return {
					"status": RIV_SETTLEMENT_BLOCKED,
					"ready": False,
					"code": "UNKNOWN_RIV_STATUS",
					"operation_id": operation_id,
					"row": rec,
					"settled": settled,
					"pending": sorted(pending),
					"elapsed": round(perf_counter() - t0, 3),
				}
		pending = still
		if pending:
			sleep(poll_s)

	if pending:
		# Timeout — diagnose each open row without mutating
		opens = []
		workers = run_worker_preflight(queues=queues, require_probe=False)
		workers_ready = bool(workers.get("ready"))
		for name in sorted(pending):
			row = _read_riv(name)
			code = _classify_open(row, workers_ready=workers_ready) if row else STALE_QUEUED_RIV
			opens.append(
				{
					"riv_name": name,
					"status": row.status if row else None,
					"code": code or "RIV_STILL_OPEN",
					"item": row.item_code if row else None,
					"warehouse": row.warehouse if row else None,
				}
			)
		return {
			"status": RIV_SETTLEMENT_TIMEOUT,
			"ready": False,
			"operation_id": operation_id,
			"opens": opens,
			"settled": settled,
			"pending": sorted(pending),
			"workers": workers,
			"elapsed": round(perf_counter() - t0, 3),
		}

	return {
		"status": RIV_SETTLEMENT_COMPLETE,
		"ready": True,
		"operation_id": operation_id,
		"tracked_n": len(names),
		"settled": settled,
		"elapsed": round(perf_counter() - t0, 3),
	}


def wait_registry(registry: CampaignRivRegistry, **kwargs) -> dict:
	out = wait_for_rivs(registry.names(), operation_id=registry.operation_id, **kwargs)
	out["registry"] = registry.to_manifest()
	return out


def assert_campaign_queue_quiescent(
	riv_names: Iterable[str] | None = None,
	*,
	also_forbid_global_open: bool = False,
) -> dict:
	"""Pre-fingerprint quiescence check — no campaign RIV left open."""
	names = [n for n in (riv_names or []) if n]
	open_scoped = []
	for name in names:
		row = _read_riv(name)
		if row and row.status in OPEN_STATUSES:
			open_scoped.append({"riv_name": name, "status": row.status})
	global_open = []
	if also_forbid_global_open:
		global_open = frappe.db.sql(
			"""
			SELECT name, status, item_code, warehouse
			FROM `tabRepost Item Valuation`
			WHERE status IN ('Queued','In Progress')
			LIMIT 50
			""",
			as_dict=True,
		)
	ok = not open_scoped and (not also_forbid_global_open or not global_open)
	return {
		"status": "CAMPAIGN_QUEUE_QUIESCENT" if ok else "CAMPAIGN_QUEUE_NOT_QUIESCENT",
		"ready": ok,
		"open_scoped": open_scoped,
		"global_open_sample": global_open,
	}


def forbid_sql_riv_status_mutation() -> None:
	"""Documentation sentinel — call sites must not SQL-mutate RIV status.

	This function exists so audits can grep for the replacement contract.
	"""
	raise RuntimeError(
		"Direct SQL mutation of Repost Item Valuation status is forbidden. "
		"Use native execute_reposting_entry / wait_for_rivs settlement."
	)
