# Copyright (c) 2026, ERPNext Extensions contributors
"""Bounded DB concurrency recovery for Historical Repair economic identities.

ONLY retry proven transient MariaDB concurrency failures after full rollback.
Never retry valuation-integrity / I1–I4 / business assertion failures.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable

import frappe

DB_DEADLOCK_RETRY = "DB_DEADLOCK_RETRY"
DB_1020_RETRY = "DB_1020_RETRY"
DB_LOCK_TIMEOUT_RETRY = "DB_LOCK_TIMEOUT_RETRY"

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_S = (0.25, 0.75, 1.5)

_RIV_LOCK_PREFIX = "iran_hr:riv_exec:"
_COMPANY_GL_LOCK_PREFIX = "iran_hr:company_gl:"


def _errno(exc: BaseException) -> int | None:
	cur: BaseException | None = exc
	seen = set()
	while cur is not None and id(cur) not in seen:
		seen.add(id(cur))
		args = getattr(cur, "args", None)
		if args and isinstance(args[0], int):
			return int(args[0])
		# Frappe wraps: QueryDeadlockError(original)
		cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
	msg = str(exc) or ""
	if "1020" in msg or "Record has changed since last read" in msg:
		return 1020
	if "1213" in msg or "Deadlock found" in msg:
		return 1213
	if "1205" in msg or "Lock wait timeout" in msg:
		return 1205
	return None


def classify_db_concurrency_error(exc: BaseException) -> str | None:
	"""Return retry reason code or None if not a transient DB concurrency error."""
	# Never treat valuation integrity as concurrency.
	name = type(exc).__name__
	if "ValuationIntegrity" in name or "IntegrityError" == name and "valuation" in str(exc).lower():
		return None
	if "ValuationIntegrityError" in name:
		return None

	code = _errno(exc)
	if code == 1213:
		return DB_DEADLOCK_RETRY
	if code == 1020:
		return DB_1020_RETRY
	if code == 1205:
		return DB_LOCK_TIMEOUT_RETRY

	# Frappe maps CHECKREAD(1020) and LOCK_DEADLOCK(1213) → QueryDeadlockError
	if isinstance(exc, frappe.QueryDeadlockError) or name == "QueryDeadlockError":
		# Prefer 1020 when message says so; else generic deadlock.
		if "1020" in str(exc) or "Record has changed" in str(exc):
			return DB_1020_RETRY
		return DB_DEADLOCK_RETRY

	if isinstance(exc, frappe.QueryTimeoutError) or name == "QueryTimeoutError":
		return DB_LOCK_TIMEOUT_RETRY

	return None


def is_retryable_db_concurrency(exc: BaseException) -> bool:
	return classify_db_concurrency_error(exc) is not None


def _redis():
	from frappe.utils.background_jobs import get_redis_conn

	return get_redis_conn()


@contextmanager
def exclusive_redis_lock(key: str, *, ttl_s: int = 7200, wait_s: float = 0.0, poll_s: float = 0.2):
	"""SET NX EX lock. wait_s=0 → fail immediately if held."""
	conn = _redis()
	token = f"{frappe.local.site}:{time.time()}:{id(key)}"
	deadline = time.time() + max(wait_s, 0.0)
	acquired = False
	while True:
		acquired = bool(conn.set(key, token, nx=True, ex=ttl_s))
		if acquired:
			break
		if time.time() >= deadline:
			break
		time.sleep(poll_s)
	if not acquired:
		yield False
		return
	try:
		yield True
	finally:
		try:
			# delete only our token
			cur = conn.get(key)
			if cur and (cur.decode() if isinstance(cur, (bytes, bytearray)) else cur) == token:
				conn.delete(key)
		except Exception:
			pass


def riv_exec_lock_key(riv_name: str) -> str:
	return f"{_RIV_LOCK_PREFIX}{riv_name}"


def company_gl_lock_key(company: str) -> str:
	return f"{_COMPANY_GL_LOCK_PREFIX}{company}"


def riv_exec_lock_held(riv_name: str) -> bool:
	try:
		return bool(_redis().get(riv_exec_lock_key(riv_name)))
	except Exception:
		return False


def company_gl_lock_held(company: str) -> bool:
	try:
		return bool(_redis().get(company_gl_lock_key(company)))
	except Exception:
		return False


def run_with_db_concurrency_retry(
	fn: Callable[[], dict],
	*,
	max_attempts: int = DEFAULT_MAX_ATTEMPTS,
	backoff_s: tuple[float, ...] = DEFAULT_BACKOFF_S,
	on_retry: Callable[[int, str, BaseException], None] | None = None,
	is_success: Callable[[dict], bool] | None = None,
) -> dict:
	"""Run ``fn`` with bounded retry on transient DB concurrency errors.

	``fn`` must fully roll back economic side effects before returning/raising
	a retryable failure (or raise so the caller rolled back).
	"""
	attempts: list[dict] = []
	last: dict | None = None
	for attempt in range(1, max_attempts + 1):
		try:
			last = fn()
		except Exception as exc:  # noqa: BLE001 — classified immediately
			reason = classify_db_concurrency_error(exc)
			attempts.append(
				{
					"attempt": attempt,
					"ok": False,
					"retry_reason": reason,
					"error": f"{type(exc).__name__}: {exc}"[:500],
				}
			)
			if reason and attempt < max_attempts:
				if on_retry:
					on_retry(attempt, reason, exc)
				time.sleep(backoff_s[min(attempt - 1, len(backoff_s) - 1)])
				continue
			out = {
				"ok": False,
				"status": "DB_CONCURRENCY_EXHAUSTED" if reason else "FAILED",
				"retry_reason": reason,
				"attempts": attempts,
				"error": str(exc)[:500],
			}
			return out

		ok = bool(last.get("ok")) if is_success is None else bool(is_success(last))
		# If fn returns failed-with-retryable reason embedded, allow retry.
		embedded = last.get("retry_reason") or classify_db_concurrency_error(
			Exception(str(last.get("reason") or last.get("error") or ""))
		)
		# Only retry when fn signals retryable via flag
		if (not ok) and last.get("db_concurrency_retry") and attempt < max_attempts:
			reason = last.get("retry_reason") or DB_DEADLOCK_RETRY
			attempts.append(
				{
					"attempt": attempt,
					"ok": False,
					"retry_reason": reason,
					"error": (last.get("reason") or last.get("error") or "")[:500],
				}
			)
			if on_retry:
				on_retry(attempt, reason, Exception(str(last.get("reason") or "retry")))
			time.sleep(backoff_s[min(attempt - 1, len(backoff_s) - 1)])
			continue

		attempts.append({"attempt": attempt, "ok": ok, "retry_reason": None})
		out = dict(last or {})
		out["attempts"] = attempts
		out["attempt_n"] = attempt
		return out

	out = dict(last or {"ok": False})
	out["attempts"] = attempts
	out["status"] = out.get("status") or "DB_CONCURRENCY_EXHAUSTED"
	return out
