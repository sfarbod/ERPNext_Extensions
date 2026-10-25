# Copyright (c) 2026, ERPNext Extensions contributors
"""Bounded resilient RIV retry + completion reconciliation for sequential HRC (v5.6.12).

Retry ONLY transient MariaDB concurrency errors (1020 / 1205 / 1213).
Never retry valuation integrity (I5), AllocationError, or validation failures.

Waits happen OUTSIDE open DB transactions. After each transient failure the
executor re-reads RIV status and may reconcile a Completed RIV instead of
reposting (Production HRC-00004 race: 1020 on progress db_set while Core later
marks the same RIV Completed).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

import frappe

from erpnext_extensions.iran_accounting.historical_stock.db_concurrency import (
	classify_db_concurrency_error,
)


def _stamp() -> str:
	"""Wall-clock stamp without requiring a bound Frappe site (unit-test safe)."""
	try:
		from frappe.utils import now_datetime

		return str(now_datetime())
	except Exception:
		return datetime.utcnow().isoformat(sep=" ", timespec="seconds")


# Defaults for sequential executor (configurable via frappe.conf)
SEQ_RETRY_MAX_ATTEMPTS = 5  # includes initial attempt
SEQ_RETRY_DELAY_SECONDS = 120
# After the last transient attempt, wait once more and re-read before Session Failed.
# Addresses Production HRC-00004: retry_attempts_exhausted while RIV later Completed.
SEQ_POST_EXHAUST_GRACE_WAITS = 1
RETRYABLE_MARIADB_ERRNOS = frozenset({1020, 1205, 1213})

# Injectable clock/sleep for tests — never sleep inside an open transaction.
_sleep_fn: Callable[[float], None] = time.sleep
_now_fn: Callable[[], float] = time.time


def configure_retry_clock(
	*,
	sleep_fn: Callable[[float], None] | None = None,
	now_fn: Callable[[], float] | None = None,
) -> None:
	"""Test hook: replace wall-clock sleep/now (do not wait real 120s in CI)."""
	global _sleep_fn, _now_fn
	if sleep_fn is not None:
		_sleep_fn = sleep_fn
	if now_fn is not None:
		_now_fn = now_fn


def reset_retry_clock() -> None:
	configure_retry_clock(sleep_fn=time.sleep, now_fn=time.time)


def seq_retry_max_attempts() -> int:
	try:
		return int(frappe.conf.get("hrc_seq_riv_retry_max_attempts") or SEQ_RETRY_MAX_ATTEMPTS)
	except Exception:
		return SEQ_RETRY_MAX_ATTEMPTS


def seq_retry_delay_seconds() -> float:
	try:
		return float(frappe.conf.get("hrc_seq_riv_retry_delay_seconds") or SEQ_RETRY_DELAY_SECONDS)
	except Exception:
		return float(SEQ_RETRY_DELAY_SECONDS)


def seq_post_exhaust_grace_waits() -> int:
	try:
		return int(
			frappe.conf.get("hrc_seq_riv_post_exhaust_grace_waits")
			if frappe.conf.get("hrc_seq_riv_post_exhaust_grace_waits") is not None
			else SEQ_POST_EXHAUST_GRACE_WAITS
		)
	except Exception:
		return SEQ_POST_EXHAUST_GRACE_WAITS


def is_non_retryable_business_error(exc_or_msg: BaseException | str | None) -> bool:
	"""True for integrity / allocation / validation failures — fail closed."""
	if exc_or_msg is None:
		return False
	text = str(exc_or_msg)
	name = type(exc_or_msg).__name__ if isinstance(exc_or_msg, BaseException) else ""
	needles = (
		"ValuationIntegrity",
		"Stock valuation integrity",
		"(I5)",
		"(I3)",
		"(I4)",
		"AllocationError",
		"historical fractional pool",
		"n_high out of range",
		"empty/short pool",
		"PermissionError",
		"ValidationError",
		"MandatoryError",
	)
	blob = f"{name} {text}"
	return any(n in blob for n in needles)


def is_retryable_transient(exc_or_msg: BaseException | str | None) -> bool:
	if exc_or_msg is None:
		return False
	if is_non_retryable_business_error(exc_or_msg):
		return False
	if isinstance(exc_or_msg, BaseException):
		return classify_db_concurrency_error(exc_or_msg) is not None
	return classify_db_concurrency_error(Exception(str(exc_or_msg))) is not None


def read_riv_status(riv_name: str) -> dict[str, Any] | None:
	if not riv_name:
		return None
	row = frappe.db.get_value(
		"Repost Item Valuation",
		riv_name,
		["name", "status", "item_code", "warehouse", "posting_date", "posting_time", "error_log", "modified"],
		as_dict=True,
	)
	return row


def wait_for_retry_delay(
	*,
	delay_s: float | None = None,
	stop_check: Callable[[], bool] | None = None,
	poll_s: float = 1.0,
) -> dict[str, Any]:
	"""Sleep outside any DB transaction; honor STOP mid-wait.

	Commits/rollbacks must already have completed before calling this.
	"""
	delay = float(delay_s if delay_s is not None else seq_retry_delay_seconds())
	# Ensure connection is idle / clean before waiting.
	try:
		frappe.db.rollback()
	except Exception:
		pass
	deadline = _now_fn() + max(delay, 0.0)
	interrupted = False
	while _now_fn() < deadline:
		if stop_check and stop_check():
			interrupted = True
			break
		remaining = deadline - _now_fn()
		_sleep_fn(min(poll_s, max(remaining, 0.0)))
	return {
		"waited_s": delay,
		"interrupted_by_stop": interrupted,
		"at": _stamp(),
	}


def append_retry_audit(sess: dict, event: dict[str, Any]) -> None:
	"""Record retry/wait/reconcile events on the sequential session payload."""
	log = sess.setdefault("retry_audit", [])
	entry = {"at": _stamp(), **event}
	log.append(entry)
	# Bound growth
	if len(log) > 500:
		del log[:-500]
	sess["status"] = sess.get("status") or "RetryWait"
	if event.get("phase") in ("wait", "retry_scheduled"):
		sess["status"] = "RetryWait"
	sess["last_retry"] = entry


def mark_identity_done_once(sess: dict, *, identity_key: str, riv_name: str) -> dict[str, Any]:
	"""Advance identities_done / riv_completed exactly once."""
	done = sess.setdefault("identities_done", [])
	completed = sess.setdefault("riv_completed", [])
	failed = sess.setdefault("riv_failed", [])
	already_done = identity_key in done
	already_riv = riv_name in completed
	if not already_done:
		done.append(identity_key)
	if not already_riv:
		completed.append(riv_name)
	# Clear stale failure pointer for this RIV without erasing audit history.
	if riv_name in failed:
		# keep in riv_failed_history for audit; remove from active failed list
		hist = sess.setdefault("riv_failed_history", [])
		if riv_name not in hist:
			hist.append(riv_name)
		sess["riv_failed"] = [x for x in failed if x != riv_name]
	lf = sess.get("last_failure") or {}
	if lf.get("riv") == riv_name:
		sess["last_failure_resolved"] = {
			"at": _stamp(),
			"riv": riv_name,
			"prior": lf,
			"resolution": "reconciled_completed",
		}
		sess["last_failure"] = None
	return {
		"identity_already_done": already_done,
		"riv_already_completed_list": already_riv,
		"advanced": not already_done,
	}


def final_completion_barrier(
	*,
	riv_name: str,
	item_code: str,
	warehouse: str,
	posting_date,
	posting_time,
	company: str,
	sess: dict,
	verify_chain: Callable[..., dict],
	stop_check: Callable[[], bool] | None = None,
	save_session: Callable[[dict], None] | None = None,
	delay_s: float | None = None,
	grace_waits: int | None = None,
	prior_audit: list | None = None,
	exhausted_error: str | None = None,
) -> dict[str, Any]:
	"""Post-exhaustion / pre-Failed barrier: wait, re-read, reconcile or fail closed.

	Must run outside an open transaction. Does not create RIVs. Does not treat
	``Completed`` alone as success — SLE chain verification is mandatory.
	"""
	delay = float(delay_s if delay_s is not None else seq_retry_delay_seconds())
	waits = int(grace_waits if grace_waits is not None else seq_post_exhaust_grace_waits())
	audit = list(prior_audit or [])
	identity_key = f"{item_code}|{warehouse}"

	def _persist():
		if save_session:
			save_session(sess)

	# Immediate re-read (late completion may already be visible).
	meta = read_riv_status(riv_name)
	if meta and meta.status == "Completed":
		out = reconcile_completed_riv(
			riv_name=riv_name,
			item_code=item_code,
			warehouse=warehouse,
			posting_date=posting_date,
			posting_time=posting_time,
			company=company,
			sess=sess,
			verify_chain=verify_chain,
		)
		out["final_barrier"] = True
		out["retry_audit"] = audit
		_persist()
		if out.get("fail_closed"):
			out["stop_chain"] = True
		return out

	for grace in range(1, max(waits, 0) + 1):
		append_retry_audit(
			sess,
			{
				"phase": "final_barrier_wait",
				"grace": grace,
				"riv": riv_name,
				"delay_s": delay,
				"prior_error": (exhausted_error or "")[:300],
			},
		)
		sess["status"] = "RetryWait"
		_persist()
		wait_out = wait_for_retry_delay(delay_s=delay, stop_check=stop_check)
		audit.append({"event": "final_barrier_wait", "grace": grace, **wait_out})
		if wait_out.get("interrupted_by_stop"):
			return {
				"ok": False,
				"stopped": True,
				"riv": riv_name,
				"final_barrier": True,
				"item_code": item_code,
				"warehouse": warehouse,
				"retry_audit": audit,
			}
		meta = read_riv_status(riv_name)
		st = (meta.status if meta else None) or ""
		if st == "Completed":
			out = reconcile_completed_riv(
				riv_name=riv_name,
				item_code=item_code,
				warehouse=warehouse,
				posting_date=posting_date,
				posting_time=posting_time,
				company=company,
				sess=sess,
				verify_chain=verify_chain,
			)
			out["final_barrier"] = True
			out["reconciled_after_exhaustion"] = True
			out["retry_audit"] = audit
			_persist()
			if out.get("fail_closed"):
				out["stop_chain"] = True
			return out
		if st == "In Progress":
			# Stay in grace loop rather than creating a duplicate RIV.
			continue
		# Failed / Queued after grace — do not invent another execute here.
		break

	meta = read_riv_status(riv_name)
	st = (meta.status if meta else None) or ""
	err = exhausted_error or (meta.error_log if meta else "") or "retry_attempts_exhausted"
	append_retry_audit(
		sess,
		{
			"phase": "final_barrier_failed",
			"riv": riv_name,
			"status": st,
			"error": str(err)[:400],
		},
	)
	_persist()
	return {
		"ok": False,
		"riv": riv_name,
		"status": st,
		"error": str(err)[:500],
		"attempts_exhausted": True,
		"final_barrier": True,
		"stop_chain": True,
		"item_code": item_code,
		"warehouse": warehouse,
		"retry_audit": audit,
		"identity": identity_key,
	}


def reconcile_completed_riv(
	*,
	riv_name: str,
	item_code: str,
	warehouse: str,
	posting_date,
	posting_time,
	company: str,
	sess: dict,
	verify_chain: Callable[..., dict],
) -> dict[str, Any]:
	"""Case 1 / Case 4: Completed RIV → verify coverage then advance once or fail closed."""
	meta = read_riv_status(riv_name)
	if not meta or meta.status != "Completed":
		return {"ok": False, "reason": "not_completed", "status": meta.status if meta else None}

	chain = verify_chain(
		item_code, warehouse, company=company, from_date=posting_date, from_time=posting_time
	)
	if chain.get("dirty"):
		return {
			"ok": False,
			"reason": "completed_but_invalid",
			"verify_failed": True,
			"issue_count": chain.get("issue_count"),
			"issues_sample": (chain.get("issues") or [])[:5],
			"fail_closed": True,
			"riv": riv_name,
			"status": "Completed",
		}

	identity_key = f"{item_code}|{warehouse}"
	progress = mark_identity_done_once(sess, identity_key=identity_key, riv_name=riv_name)
	append_retry_audit(
		sess,
		{
			"phase": "reconcile_completed",
			"riv": riv_name,
			"identity": identity_key,
			**progress,
		},
	)
	sess["status"] = "Running"
	return {
		"ok": True,
		"reconciled": True,
		"riv": riv_name,
		"status": "Completed",
		"item_code": item_code,
		"warehouse": warehouse,
		"sle_count": chain.get("sle_count"),
		**progress,
	}


def run_resilient_riv_execution(
	*,
	riv_name: str,
	item_code: str,
	warehouse: str,
	posting_date,
	posting_time,
	company: str,
	sess: dict,
	execute_fn: Callable[[str], None],
	verify_chain: Callable[..., dict],
	stop_check: Callable[[], bool] | None = None,
	save_session: Callable[[dict], None] | None = None,
	max_attempts: int | None = None,
	delay_s: float | None = None,
) -> dict[str, Any]:
	"""Execute one RIV with bounded transient retry + completion reconciliation.

	``execute_fn(riv_name)`` must run Core ``execute_reposting_entry`` (or test double).
	Caller owns outer session persistence via ``save_session``.
	"""
	attempts = int(max_attempts if max_attempts is not None else seq_retry_max_attempts())
	delay = float(delay_s if delay_s is not None else seq_retry_delay_seconds())
	identity_key = f"{item_code}|{warehouse}"
	audit: list[dict] = []
	t0 = _now_fn()

	def _persist():
		if save_session:
			save_session(sess)

	for attempt in range(1, attempts + 1):
		if stop_check and stop_check():
			append_retry_audit(sess, {"phase": "stop_before_attempt", "attempt": attempt, "riv": riv_name})
			_persist()
			return {
				"ok": False,
				"stopped": True,
				"riv": riv_name,
				"attempt": attempt,
				"item_code": item_code,
				"warehouse": warehouse,
				"duration": round(_now_fn() - t0, 3),
				"retry_audit": audit,
			}

		# Re-read before each attempt — may already be Completed by Core race.
		meta = read_riv_status(riv_name)
		st = (meta.status if meta else None) or ""
		if st == "Completed":
			out = reconcile_completed_riv(
				riv_name=riv_name,
				item_code=item_code,
				warehouse=warehouse,
				posting_date=posting_date,
				posting_time=posting_time,
				company=company,
				sess=sess,
				verify_chain=verify_chain,
			)
			out["attempt"] = attempt
			out["duration"] = round(_now_fn() - t0, 3)
			out["retry_audit"] = audit
			_persist()
			if out.get("fail_closed"):
				out["stop_chain"] = True
			return out

		if st == "In Progress":
			# Do not create another RIV; wait and recheck (counts as wait cycle).
			append_retry_audit(
				sess,
				{
					"phase": "wait_in_progress",
					"attempt": attempt,
					"riv": riv_name,
					"delay_s": delay,
				},
			)
			_persist()
			wait_out = wait_for_retry_delay(delay_s=delay, stop_check=stop_check)
			audit.append({"attempt": attempt, "event": "wait_in_progress", **wait_out})
			if wait_out.get("interrupted_by_stop"):
				return {
					"ok": False,
					"stopped": True,
					"riv": riv_name,
					"status": "In Progress",
					"attempt": attempt,
					"item_code": item_code,
					"warehouse": warehouse,
					"duration": round(_now_fn() - t0, 3),
					"retry_audit": audit,
				}
			# Loop continues → re-read status
			continue

		# Queued / Failed / unknown → attempt execute (safe path). Preserve Failed audit.
		try:
			execute_fn(riv_name)
			try:
				frappe.db.commit()
			except Exception:
				frappe.db.rollback()
				raise
		except Exception as exc:  # noqa: BLE001
			try:
				frappe.db.rollback()
			except Exception:
				pass
			if is_non_retryable_business_error(exc):
				audit.append(
					{
						"attempt": attempt,
						"ok": False,
						"non_retryable": True,
						"error": f"{type(exc).__name__}: {exc}"[:500],
					}
				)
				append_retry_audit(
					sess,
					{
						"phase": "non_retryable",
						"attempt": attempt,
						"riv": riv_name,
						"error": str(exc)[:400],
					},
				)
				_persist()
				return {
					"ok": False,
					"riv": riv_name,
					"error": str(exc)[:500],
					"non_retryable": True,
					"stop_chain": True,
					"attempt": attempt,
					"item_code": item_code,
					"warehouse": warehouse,
					"duration": round(_now_fn() - t0, 3),
					"retry_audit": audit,
					"raised": True,
					"exc": exc,
				}
			reason = classify_db_concurrency_error(exc)
			audit.append(
				{
					"attempt": attempt,
					"ok": False,
					"retry_reason": reason,
					"error": f"{type(exc).__name__}: {exc}"[:500],
				}
			)
			# Critical race: Core may complete the RIV after 1020 on progress db_set.
			meta_after = read_riv_status(riv_name)
			if meta_after and meta_after.status == "Completed":
				out = reconcile_completed_riv(
					riv_name=riv_name,
					item_code=item_code,
					warehouse=warehouse,
					posting_date=posting_date,
					posting_time=posting_time,
					company=company,
					sess=sess,
					verify_chain=verify_chain,
				)
				out["attempt"] = attempt
				out["duration"] = round(_now_fn() - t0, 3)
				out["retry_audit"] = audit
				out["reconciled_after_transient"] = True
				out["transient_error"] = str(exc)[:400]
				_persist()
				if out.get("fail_closed"):
					out["stop_chain"] = True
				return out

			if reason and attempt < attempts:
				append_retry_audit(
					sess,
					{
						"phase": "retry_scheduled",
						"attempt": attempt,
						"riv": riv_name,
						"retry_reason": reason,
						"delay_s": delay,
						"error": str(exc)[:300],
					},
				)
				sess["status"] = "RetryWait"
				_persist()
				wait_out = wait_for_retry_delay(delay_s=delay, stop_check=stop_check)
				audit[-1]["wait"] = wait_out
				if wait_out.get("interrupted_by_stop"):
					return {
						"ok": False,
						"stopped": True,
						"riv": riv_name,
						"attempt": attempt,
						"retry_reason": reason,
						"item_code": item_code,
						"warehouse": warehouse,
						"duration": round(_now_fn() - t0, 3),
						"retry_audit": audit,
					}
				continue

			# Last attempt transient OR non-classified: final barrier before Session Failed.
			if reason:
				out = final_completion_barrier(
					riv_name=riv_name,
					item_code=item_code,
					warehouse=warehouse,
					posting_date=posting_date,
					posting_time=posting_time,
					company=company,
					sess=sess,
					verify_chain=verify_chain,
					stop_check=stop_check,
					save_session=save_session,
					delay_s=delay,
					prior_audit=audit,
					exhausted_error=str(exc)[:500],
				)
				out["attempt"] = attempt
				out["duration"] = round(_now_fn() - t0, 3)
				out["retry_reason"] = reason
				return out
			_persist()
			return {
				"ok": False,
				"riv": riv_name,
				"error": str(exc)[:500],
				"retry_reason": reason,
				"stop_chain": True,
				"attempt": attempt,
				"item_code": item_code,
				"warehouse": warehouse,
				"duration": round(_now_fn() - t0, 3),
				"retry_audit": audit,
				"raised": True,
				"exc": exc,
			}

		# Execute returned without raise — inspect status
		meta = read_riv_status(riv_name)
		st = (meta.status if meta else None) or ""
		err = (meta.error_log if meta else "") or ""
		if st == "Completed":
			out = reconcile_completed_riv(
				riv_name=riv_name,
				item_code=item_code,
				warehouse=warehouse,
				posting_date=posting_date,
				posting_time=posting_time,
				company=company,
				sess=sess,
				verify_chain=verify_chain,
			)
			out["attempt"] = attempt
			out["duration"] = round(_now_fn() - t0, 3)
			out["retry_audit"] = audit
			_persist()
			if out.get("fail_closed"):
				out["stop_chain"] = True
			return out

		# Transient residue in error_log while not Completed
		if is_retryable_transient(err) and attempt < attempts and st in (
			"Failed",
			"Queued",
			"In Progress",
		):
			reason = classify_db_concurrency_error(Exception(err))
			audit.append(
				{
					"attempt": attempt,
					"ok": False,
					"retry_reason": reason,
					"status": st,
					"error": err[:500],
				}
			)
			append_retry_audit(
				sess,
				{
					"phase": "retry_scheduled",
					"attempt": attempt,
					"riv": riv_name,
					"retry_reason": reason,
					"status": st,
					"delay_s": delay,
				},
			)
			sess["status"] = "RetryWait"
			_persist()
			wait_out = wait_for_retry_delay(delay_s=delay, stop_check=stop_check)
			audit[-1]["wait"] = wait_out
			if wait_out.get("interrupted_by_stop"):
				return {
					"ok": False,
					"stopped": True,
					"riv": riv_name,
					"status": st,
					"attempt": attempt,
					"item_code": item_code,
					"warehouse": warehouse,
					"duration": round(_now_fn() - t0, 3),
					"retry_audit": audit,
				}
			continue

		if is_non_retryable_business_error(err):
			return {
				"ok": False,
				"riv": riv_name,
				"status": st,
				"error": err[:500],
				"non_retryable": True,
				"stop_chain": True,
				"attempt": attempt,
				"item_code": item_code,
				"warehouse": warehouse,
				"duration": round(_now_fn() - t0, 3),
				"retry_audit": audit,
			}

		return {
			"ok": False,
			"riv": riv_name,
			"status": st,
			"error": err[:500] or f"unexpected_status:{st}",
			"stop_chain": True,
			"attempt": attempt,
			"item_code": item_code,
			"warehouse": warehouse,
			"duration": round(_now_fn() - t0, 3),
			"retry_audit": audit,
		}

	# Loop exhausted (e.g. prolonged In Progress): one final completion barrier.
	out = final_completion_barrier(
		riv_name=riv_name,
		item_code=item_code,
		warehouse=warehouse,
		posting_date=posting_date,
		posting_time=posting_time,
		company=company,
		sess=sess,
		verify_chain=verify_chain,
		stop_check=stop_check,
		save_session=save_session,
		delay_s=delay,
		prior_audit=audit,
		exhausted_error="retry_attempts_exhausted",
	)
	out["duration"] = round(_now_fn() - t0, 3)
	return out


def prepare_production_session_reconcile(
	*,
	session_payload: dict,
	riv_name: str,
	item_code: str,
	warehouse: str,
) -> dict[str, Any]:
	"""Operator-facing dry analysis for Production recovery (no writes)."""
	done = list(session_payload.get("identities_done") or [])
	completed = list(session_payload.get("riv_completed") or [])
	failed = list(session_payload.get("riv_failed") or [])
	key = f"{item_code}|{warehouse}"
	return {
		"identity_key": key,
		"identity_in_done": key in done,
		"riv_in_completed": riv_name in completed,
		"riv_in_failed": riv_name in failed,
		"identities_done_count": len(done),
		"riv_completed_count": len(completed),
		"riv_failed_count": len(failed),
		"recommendation": (
			"reconcile_clear_failed_keep_done"
			if (key in done and riv_name in completed)
			else (
				"reconcile_mark_done_from_completed_riv"
				if riv_name
				else "inspect_manually"
			)
		),
		"note": (
			"If RIV is Completed and SLE chain is clean, advance identity once and "
			"remove riv from active riv_failed without deleting audit history. "
			"Do not recreate the RIV."
		),
	}
