# Copyright (c) 2026, ERPNext Extensions contributors
"""Queue-aware wait before sequential RIV creation (v5.6.14).

When site-wide Repost Item Valuation rows are Queued or In Progress, the
sequential executor WAIT → RECHECK → CONTINUE instead of raising.

Does not cancel/delete RIVs. Does not hold DB transactions while sleeping.
Does not bypass MAX_ACTIVE_RIV=1 for campaign-created RIVs.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import frappe
from frappe.utils import cint

# Defaults (site_config overrides)
SEQ_QUEUE_POLL_SECONDS = 30
ACTIVE_RIV_STATUSES = ("Queued", "In Progress")

_sleep_fn: Callable[[float], None] = time.sleep
_now_fn: Callable[[], float] = time.time


def configure_queue_wait_clock(
	*,
	sleep_fn: Callable[[float], None] | None = None,
	now_fn: Callable[[], float] | None = None,
) -> None:
	"""Test hook — avoid real 30s sleeps in unit tests."""
	global _sleep_fn, _now_fn
	if sleep_fn is not None:
		_sleep_fn = sleep_fn
	if now_fn is not None:
		_now_fn = now_fn


def reset_queue_wait_clock() -> None:
	configure_queue_wait_clock(sleep_fn=time.sleep, now_fn=time.time)


def seq_queue_poll_seconds() -> float:
	try:
		return float(
			frappe.conf.get("hrc_seq_riv_queue_poll_seconds") or SEQ_QUEUE_POLL_SECONDS
		)
	except Exception:
		return float(SEQ_QUEUE_POLL_SECONDS)


def count_active_rivs() -> int:
	"""Site-wide count of submitted RIVs that are Queued or In Progress."""
	return cint(
		frappe.db.count(
			"Repost Item Valuation",
			{"docstatus": 1, "status": ("in", list(ACTIVE_RIV_STATUSES))},
		)
	)


def list_active_rivs(*, limit: int = 20) -> list[dict[str, Any]]:
	"""Sample active RIV rows for logging / session metadata."""
	return (
		frappe.get_all(
			"Repost Item Valuation",
			filters={"docstatus": 1, "status": ("in", list(ACTIVE_RIV_STATUSES))},
			fields=["name", "status", "item_code", "warehouse", "modified"],
			order_by="modified desc",
			limit_page_length=cint(limit) or 20,
		)
		or []
	)


def _stamp() -> str:
	try:
		from frappe.utils import now_datetime

		return str(now_datetime())
	except Exception:
		from datetime import datetime

		return datetime.utcnow().isoformat(sep=" ", timespec="seconds")


def wait_for_riv_queue_drain(
	*,
	sess: dict | None = None,
	save_session: Callable[[dict], None] | None = None,
	stop_check: Callable[[], bool] | None = None,
	lock_ok: Callable[[], bool] | None = None,
	heartbeat: Callable[[], None] | None = None,
	poll_s: float | None = None,
	log: Callable[[str], None] | None = None,
	campaign: str | None = None,
) -> dict[str, Any]:
	"""Block until zero active RIVs, or STOP / lock loss.

	Must be called with no open write transaction held. Caller should
	``frappe.db.commit()`` / ``rollback()`` before entering.
	"""
	poll = float(poll_s if poll_s is not None else seq_queue_poll_seconds())
	logger = log or (lambda msg: frappe.logger("hrc_sequential").info(msg))
	prefix = f"[HRC{(' ' + campaign) if campaign else ''}]"
	checks = 0
	max_seen = 0
	entered_wait = False

	def _persist_waiting(n: int, sample: list) -> None:
		nonlocal entered_wait
		if sess is None:
			return
		entered_wait = True
		sess["status"] = "Waiting for RIV Queue"
		sess["queue_wait"] = {
			"at": _stamp(),
			"active_count": n,
			"active_sample": sample[:10],
			"poll_s": poll,
			"checks": checks,
		}
		if save_session:
			save_session(sess)

	# Ensure DB connection is idle before any sleep.
	try:
		frappe.db.rollback()
	except Exception:
		pass

	while True:
		if stop_check and stop_check():
			logger(f"{prefix} STOP requested during RIV queue wait.")
			if sess is not None:
				sess["status"] = "Paused"
				if save_session:
					save_session(sess)
			return {
				"ok": False,
				"stopped": True,
				"waited": entered_wait,
				"checks": checks,
				"max_active_seen": max_seen,
			}
		if lock_ok and not lock_ok():
			logger(f"{prefix} Executor lock lost during RIV queue wait — stopping.")
			if sess is not None:
				sess["status"] = "Paused"
				sess["queue_wait_lock_lost"] = {"at": _stamp(), "checks": checks}
				if save_session:
					save_session(sess)
			return {
				"ok": False,
				"lock_lost": True,
				"waited": entered_wait,
				"checks": checks,
				"max_active_seen": max_seen,
			}

		n = count_active_rivs()
		checks += 1
		max_seen = max(max_seen, n)
		if n <= 0:
			if entered_wait:
				logger(f"{prefix} Active RIVs: 0 — queue drained. Resuming sequential execution.")
			if sess is not None and entered_wait:
				sess["status"] = "Running"
				sess["queue_wait_cleared"] = {"at": _stamp(), "checks": checks, "max_active_seen": max_seen}
				if save_session:
					save_session(sess)
			return {
				"ok": True,
				"waited": entered_wait,
				"checks": checks,
				"max_active_seen": max_seen,
			}

		sample = list_active_rivs(limit=5)

		def _riv_label(r) -> str:
			if isinstance(r, dict):
				return f"{r.get('name')}:{r.get('status')}"
			return f"{getattr(r, 'name', '?')}:{getattr(r, 'status', '?')}"

		names = ", ".join(_riv_label(r) for r in sample) or "(none)"
		if checks == 1 or checks % 10 == 0:
			logger(
				f"{prefix} Waiting for RIV queue to drain. "
				f"Active RIVs: {n}. Sample: {names}. Next check: {poll:g} seconds"
			)
		else:
			logger(f"{prefix} Active RIVs: {n}. Waiting...")

		_persist_waiting(n, sample)
		if heartbeat:
			try:
				heartbeat()
			except Exception:
				pass

		try:
			frappe.db.rollback()
		except Exception:
			pass
		_sleep_fn(poll)
