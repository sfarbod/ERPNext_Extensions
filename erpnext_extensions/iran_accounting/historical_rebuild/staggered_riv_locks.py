# Copyright (c) 2026, ERPNext Extensions contributors
"""Conflict-scope locks for staggered HRC RIV dispatch (v5.7.50).

ERPNext core does not mutex item+warehouse during ``repost_future_sle``.
These Redis locks serialize conflicting valuation chains while allowing
independent identities to run concurrently.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

import frappe
from frappe.utils import cint

_SCOPE_PREFIX = "hrc_stagger:scope:"
_ACTIVE_SET = "hrc_stagger:active_scopes"
_RIV_SCOPES_PREFIX = "hrc_stagger:riv_scopes:"
_VOUCHER_SET_PREFIX = "hrc_stagger:vouchers:"

# Atomic multi-key acquire: all or nothing.
_ACQUIRE_LUA = """
for i, key in ipairs(KEYS) do
  if redis.call('EXISTS', key) == 1 then
    return 0
  end
end
for i, key in ipairs(KEYS) do
  redis.call('SET', key, ARGV[1], 'EX', tonumber(ARGV[2]))
  redis.call('SADD', ARGV[3], key)
end
redis.call('EXPIRE', ARGV[3], tonumber(ARGV[2]))
return 1
"""

_RELEASE_LUA = """
local token = ARGV[1]
local released = 0
for i, key in ipairs(KEYS) do
  local cur = redis.call('GET', key)
  if cur == token then
    redis.call('DEL', key)
    redis.call('SREM', ARGV[2], key)
    released = released + 1
  end
end
return released
"""


def _redis():
	from frappe.utils.background_jobs import get_redis_conn

	return get_redis_conn()


def lock_timeout_seconds() -> int:
	try:
		return max(60, cint(frappe.conf.get("hrc_stagger_lock_timeout_seconds") or 7200))
	except Exception:
		return 7200


def max_concurrent_per_conflict_scope() -> int:
	try:
		return max(1, cint(frappe.conf.get("hrc_stagger_max_concurrent_per_conflict_scope") or 1))
	except Exception:
		return 1


def identity_key(item_code: str, warehouse: str) -> str:
	return f"{item_code}|{warehouse}"


def scope_keys_for_identity(item_code: str, warehouse: str) -> list[str]:
	"""Primary item|warehouse + item soft-cap scopes."""
	return [
		f"{_SCOPE_PREFIX}iw:{item_code}|{warehouse}",
		f"{_SCOPE_PREFIX}item:{item_code}",
	]


def fetch_future_voucher_keys(
	item_code: str,
	warehouse: str,
	posting_date,
	posting_time=None,
	*,
	limit: int = 500,
) -> set[str]:
	"""Read-only SLE voucher keys for this identity from posting forward.

	Bounded sample (default 500) — sufficient to detect common multi-item
	voucher overlap without scanning entire future chains on every dispatch.
	"""
	lim = max(50, cint(limit) or 500)
	rows = frappe.db.sql(
		"""
		select voucher_type, voucher_no
		from `tabStock Ledger Entry`
		where item_code=%s and warehouse=%s and is_cancelled=0
		  and posting_date >= %s
		limit %s
		""",
		(item_code, warehouse, posting_date, lim),
	)
	return {f"{vt}/{vn}" for vt, vn in (rows or []) if vt and vn}


def voucher_overlap(a: set[str], b: set[str]) -> bool:
	if not a or not b:
		return False
	# Prefer smaller set for membership checks.
	small, large = (a, b) if len(a) <= len(b) else (b, a)
	return any(x in large for x in small)


def _token() -> str:
	site = getattr(frappe.local, "site", None) or "nosite"
	return f"{site}:{time.time_ns()}"


def try_acquire_scopes(
	scopes: list[str],
	*,
	ttl_s: int | None = None,
	token: str | None = None,
) -> str | None:
	"""Acquire all scope keys atomically. Returns token or None."""
	if not scopes:
		return None
	# Soft-cap >1 is not implemented as multi-holder counters; plan default is 1.
	if max_concurrent_per_conflict_scope() != 1:
		# Still exclusive SET NX semantics for safety until multi-holder ships.
		pass
	ttl = int(ttl_s if ttl_s is not None else lock_timeout_seconds())
	tok = token or _token()
	conn = _redis()
	keys = list(dict.fromkeys(scopes))  # stable unique
	try:
		ok = conn.eval(_ACQUIRE_LUA, len(keys), *keys, tok, str(ttl), _ACTIVE_SET)
		return tok if int(ok or 0) else None
	except Exception:
		# Fallback: sequential NX (best-effort; may partial-fail then rollback)
		acquired: list[str] = []
		try:
			for key in keys:
				if not conn.set(key, tok, nx=True, ex=ttl):
					for a in acquired:
						cur = conn.get(a)
						cur_s = cur.decode() if isinstance(cur, (bytes, bytearray)) else cur
						if cur_s == tok:
							conn.delete(a)
					return None
				acquired.append(key)
			return tok
		except Exception:
			return None


def release_scopes(scopes: list[str], token: str | None) -> int:
	if not scopes or not token:
		return 0
	conn = _redis()
	keys = list(dict.fromkeys(scopes))
	try:
		return int(conn.eval(_RELEASE_LUA, len(keys), *keys, token, _ACTIVE_SET) or 0)
	except Exception:
		n = 0
		for key in keys:
			try:
				cur = conn.get(key)
				cur_s = cur.decode() if isinstance(cur, (bytes, bytearray)) else cur
				if cur_s == token:
					conn.delete(key)
					n += 1
			except Exception:
				pass
		return n


def remember_riv_scopes(
	riv_name: str,
	scopes: list[str],
	vouchers: set[str],
	token: str,
	*,
	ttl_s: int | None = None,
) -> None:
	"""Persist active riv → scopes/vouchers for overlap checks and cleanup."""
	ttl = int(ttl_s if ttl_s is not None else lock_timeout_seconds())
	conn = _redis()
	payload = {
		"scopes": scopes,
		"token": token,
		"vouchers": sorted(vouchers)[:5000],
		"at": time.time(),
	}
	conn.set(f"{_RIV_SCOPES_PREFIX}{riv_name}", json.dumps(payload), ex=ttl)
	# Compact voucher fingerprint set for overlap
	vkey = f"{_VOUCHER_SET_PREFIX}{riv_name}"
	pipe = conn.pipeline()
	pipe.delete(vkey)
	if vouchers:
		# Store hashed chunks to bound Redis memory
		for vk in list(vouchers)[:5000]:
			pipe.sadd(vkey, vk)
		pipe.expire(vkey, ttl)
	pipe.execute()


def clear_riv_scopes(riv_name: str) -> None:
	conn = _redis()
	raw = None
	try:
		raw = conn.get(f"{_RIV_SCOPES_PREFIX}{riv_name}")
	except Exception:
		raw = None
	token = None
	scopes: list[str] = []
	if raw:
		try:
			data = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
			token = data.get("token")
			scopes = list(data.get("scopes") or [])
		except Exception:
			pass
	if scopes and token:
		release_scopes(scopes, token)
	try:
		conn.delete(f"{_RIV_SCOPES_PREFIX}{riv_name}")
	except Exception:
		pass
	try:
		conn.delete(f"{_VOUCHER_SET_PREFIX}{riv_name}")
	except Exception:
		pass


def purge_orphan_voucher_locks(*, active_rivs: set[str] | None = None) -> int:
	"""Drop voucher/scope tracking for RIVs that are no longer active."""
	conn = _redis()
	active = active_rivs or set()
	removed = 0
	try:
		cursor = 0
		while True:
			cursor, keys = conn.scan(cursor=cursor, match=f"{_VOUCHER_SET_PREFIX}*", count=200)
			for key in keys:
				name = key.decode() if isinstance(key, (bytes, bytearray)) else key
				riv = name[len(_VOUCHER_SET_PREFIX) :]
				if riv in active:
					continue
				clear_riv_scopes(riv)
				removed += 1
			if cursor == 0:
				break
	except Exception:
		pass
	return removed


def active_riv_voucher_union(exclude_riv: str | None = None) -> set[str]:
	"""Union of voucher keys for RIVs that still hold scope locks."""
	conn = _redis()
	out: set[str] = set()
	try:
		cursor = 0
		pattern = f"{_RIV_SCOPES_PREFIX}*"
		while True:
			cursor, keys = conn.scan(cursor=cursor, match=pattern, count=200)
			for key in keys:
				name = key.decode() if isinstance(key, (bytes, bytearray)) else key
				riv = name[len(_RIV_SCOPES_PREFIX) :]
				if exclude_riv and riv == exclude_riv:
					continue
				# Only count if scope payload still exists (active lock tracking).
				try:
					if not conn.exists(name):
						continue
				except Exception:
					continue
				vkey = f"{_VOUCHER_SET_PREFIX}{riv}"
				try:
					members = conn.smembers(vkey) or set()
				except Exception:
					members = set()
				for m in members:
					out.add(m.decode() if isinstance(m, (bytes, bytearray)) else m)
			if cursor == 0:
				break
	except Exception:
		pass
	return out


def scopes_held(scopes: list[str]) -> bool:
	conn = _redis()
	for key in scopes:
		try:
			if conn.exists(key):
				return True
		except Exception:
			return True  # fail closed
	return False


def conflict_blocks_dispatch(
	*,
	item_code: str,
	warehouse: str,
	posting_date,
	posting_time=None,
	candidate_vouchers: set[str] | None = None,
) -> dict[str, Any]:
	"""Return {blocked: bool, reason: str|None, scopes, vouchers}."""
	scopes = scope_keys_for_identity(item_code, warehouse)
	if scopes_held(scopes):
		return {
			"blocked": True,
			"reason": "scope_held",
			"scopes": scopes,
			"vouchers": set(),
		}
	vouchers = candidate_vouchers
	if vouchers is None:
		vouchers = fetch_future_voucher_keys(item_code, warehouse, posting_date, posting_time)
	active = active_riv_voucher_union()
	if voucher_overlap(vouchers, active):
		return {
			"blocked": True,
			"reason": "sle_voucher_overlap",
			"scopes": scopes,
			"vouchers": vouchers,
		}
	return {"blocked": False, "reason": None, "scopes": scopes, "vouchers": vouchers}


def fingerprint_vouchers(vouchers: set[str]) -> str:
	h = hashlib.sha1()
	for v in sorted(vouchers):
		h.update(v.encode("utf-8"))
		h.update(b"\0")
	return h.hexdigest()
