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


# Registry of RIVs claimed by sync Sequential / Historical Repair executors.
# Used so Core ``get_entries_with_active_jobs`` can treat sync-owned RIVs as busy
# even when no RQ job id exists for them (HRC-00008 dual-exec gap).
_RIV_EXEC_ACTIVE_SET = "iran_hr:riv_exec_active"

# Default TTL must exceed the longest expected single Core ``repost()`` call.
# Ownership is re-asserted between Sequential attempts / waits; a single in-flight
# Core repost that outlives TTL without refresh remains a documented residual risk.
DEFAULT_RIV_EXEC_CLAIM_TTL_S = 6 * 60 * 60  # 6 hours

# Atomic refresh: only extend TTL when the stored token still matches.
_REFRESH_CLAIM_LUA = """
local cur = redis.call('GET', KEYS[1])
if cur == ARGV[1] then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
  redis.call('SADD', KEYS[2], ARGV[3])
  redis.call('EXPIRE', KEYS[2], tonumber(ARGV[2]))
  return 1
end
return 0
"""

# Atomic release: delete only when token matches (never steal another's claim).
_RELEASE_CLAIM_LUA = """
local cur = redis.call('GET', KEYS[1])
if cur == ARGV[1] then
  redis.call('DEL', KEYS[1])
  redis.call('SREM', KEYS[2], ARGV[2])
  return 1
end
return 0
"""


class RivExecutionOwnershipError(RuntimeError):
	"""Raised when Sequential/HR no longer owns the RIV execution claim."""


def claim_riv_execution(riv_name: str, *, ttl_s: int | None = None) -> str | None:
	"""Acquire exclusive Redis ownership for one RIV. Returns token or None.

	Uses SET NX EX — atomic under concurrent claimants. Empty/malformed names
	are rejected. Does not touch MariaDB; callers must order claim vs commit.
	"""
	if not riv_name or not isinstance(riv_name, str):
		return None
	ttl = int(ttl_s if ttl_s is not None else DEFAULT_RIV_EXEC_CLAIM_TTL_S)
	if ttl <= 0:
		return None
	conn = _redis()
	token = f"{frappe.local.site}:{riv_name}:{time.time_ns()}"
	key = riv_exec_lock_key(riv_name)
	ok = bool(conn.set(key, token, nx=True, ex=ttl))
	if not ok:
		return None
	try:
		conn.sadd(_RIV_EXEC_ACTIVE_SET, riv_name)
		conn.expire(_RIV_EXEC_ACTIVE_SET, ttl)
	except Exception:
		# Lock key already held; active-set is best-effort visibility for Core.
		pass
	return token


def refresh_riv_execution_claim(
	riv_name: str, token: str, *, ttl_s: int | None = None
) -> bool:
	"""Atomically extend claim TTL if ``token`` still owns the lock."""
	if not riv_name or not token:
		return False
	ttl = int(ttl_s if ttl_s is not None else DEFAULT_RIV_EXEC_CLAIM_TTL_S)
	if ttl <= 0:
		return False
	conn = _redis()
	key = riv_exec_lock_key(riv_name)
	try:
		# Prefer Lua for get+expire atomicity (avoids TOCTOU with a new claimant).
		try:
			result = conn.eval(
				_REFRESH_CLAIM_LUA, 2, key, _RIV_EXEC_ACTIVE_SET, token, str(ttl), riv_name
			)
			return bool(int(result or 0))
		except Exception:
			# Fallback for Redis clients without eval — still compare-then-expire.
			cur = conn.get(key)
			cur_s = cur.decode() if isinstance(cur, (bytes, bytearray)) else cur
			if cur_s != token:
				return False
			conn.expire(key, ttl)
			conn.sadd(_RIV_EXEC_ACTIVE_SET, riv_name)
			conn.expire(_RIV_EXEC_ACTIVE_SET, ttl)
			return True
	except Exception:
		return False


def assert_riv_execution_owner(riv_name: str, token: str, *, ttl_s: int | None = None) -> None:
	"""Refresh claim or raise — call before starting / continuing sync execute."""
	if refresh_riv_execution_claim(riv_name, token, ttl_s=ttl_s):
		return
	raise RivExecutionOwnershipError(
		f"Lost RIV execution ownership for {riv_name!r}; refuse to execute"
	)


def release_riv_execution_claim(riv_name: str, token: str | None) -> bool:
	"""Release claim only when ``token`` matches. Never delete without a token.

	Returns True when this caller deleted the lock.
	"""
	if not riv_name or not token:
		return False
	conn = _redis()
	key = riv_exec_lock_key(riv_name)
	try:
		try:
			result = conn.eval(
				_RELEASE_CLAIM_LUA, 2, key, _RIV_EXEC_ACTIVE_SET, token, riv_name
			)
			return bool(int(result or 0))
		except Exception:
			cur = conn.get(key)
			cur_s = cur.decode() if isinstance(cur, (bytes, bytearray)) else cur
			if cur_s != token:
				return False
			conn.delete(key)
			conn.srem(_RIV_EXEC_ACTIVE_SET, riv_name)
			return True
	except Exception:
		return False


def listed_claimed_riv_executions() -> set[str]:
	"""RIV names currently claimed by sync executors (may include stale on Redis loss)."""
	try:
		raw = _redis().smembers(_RIV_EXEC_ACTIVE_SET) or set()
	except Exception:
		return set()
	out: set[str] = set()
	for item in raw:
		name = item.decode() if isinstance(item, (bytes, bytearray)) else str(item)
		# Drop set members whose lock key already expired.
		if riv_exec_lock_held(name):
			out.add(name)
		else:
			try:
				_redis().srem(_RIV_EXEC_ACTIVE_SET, name)
			except Exception:
				pass
	return out


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
