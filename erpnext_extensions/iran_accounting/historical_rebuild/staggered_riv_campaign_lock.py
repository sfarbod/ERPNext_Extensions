# Copyright (c) 2026, ERPNext Extensions contributors
"""Campaign-level mutual exclusion for Sequential vs Staggered HRC (v5.7.51).

Only one execution mode may own a campaign at a time. Lock payload includes
campaign, session_id, mode, and owner_token. Owners never release another
session's lock. Stale locks are cleared only via verified recovery.
"""

from __future__ import annotations

import json
import time
from typing import Any

import frappe
from frappe.utils import cint, now_datetime

MODE_SEQUENTIAL = "sequential"
MODE_STAGGERED = "staggered"
_VALID_MODES = frozenset({MODE_SEQUENTIAL, MODE_STAGGERED})

CACHE_PREFIX = "hrc_campaign_exec_lock"
DEFAULT_TTL_S = 6 * 60 * 60


def campaign_lock_key(campaign: str) -> str:
	return f"{CACHE_PREFIX}:{campaign}"


def lock_ttl_seconds() -> int:
	try:
		return max(300, cint(frappe.conf.get("hrc_campaign_exec_lock_ttl_seconds") or DEFAULT_TTL_S))
	except Exception:
		return DEFAULT_TTL_S


def _redis():
	from frappe.utils.background_jobs import get_redis_conn

	return get_redis_conn()


def _encode(payload: dict[str, Any]) -> str:
	return json.dumps(payload, sort_keys=True, default=str)


def _decode(raw: Any) -> dict[str, Any] | None:
	if raw is None:
		return None
	if isinstance(raw, dict):
		return raw
	if isinstance(raw, bytes):
		raw = raw.decode("utf-8", errors="replace")
	try:
		data = json.loads(raw)
	except Exception:
		return None
	return data if isinstance(data, dict) else None


def get_campaign_execution_lock(campaign: str) -> dict[str, Any] | None:
	"""Return current lock payload or None."""
	# Prefer Redis NX store; fall back to frappe.cache for test environments.
	key = campaign_lock_key(campaign)
	try:
		r = _redis()
		# Avoid tenacity storm when queue Redis is down in unit tests.
		raw = r.get(key) if hasattr(r, "get") else None
		parsed = _decode(raw)
		if parsed:
			return parsed
	except Exception:
		pass
	try:
		cached = frappe.cache().get_value(key)
		return _decode(cached) if cached else None
	except Exception:
		return None


def _same_owner(
	held: dict[str, Any],
	*,
	mode: str,
	session_id: str,
	owner_token: str,
	campaign: str | None = None,
) -> bool:
	if campaign is not None and str(held.get("campaign") or "") != str(campaign):
		return False
	return (
		str(held.get("mode")) == str(mode)
		and str(held.get("session_id")) == str(session_id)
		and str(held.get("owner_token")) == str(owner_token)
	)


def acquire_campaign_execution_lock(
	campaign: str,
	*,
	mode: str,
	session_id: str,
	owner_token: str,
	ttl_s: int | None = None,
) -> dict[str, Any]:
	"""Atomically acquire or refresh campaign execution ownership."""
	if mode not in _VALID_MODES:
		frappe.throw(f"Invalid execution mode: {mode}", frappe.ValidationError)
	if not campaign or not session_id or not owner_token:
		frappe.throw("campaign, session_id, and owner_token are required", frappe.ValidationError)

	ttl = int(ttl_s if ttl_s is not None else lock_ttl_seconds())
	key = campaign_lock_key(campaign)
	payload = {
		"campaign": campaign,
		"mode": mode,
		"session_id": str(session_id),
		"owner_token": str(owner_token),
		"acquired_at": str(now_datetime()),
		"refreshed_at": str(now_datetime()),
		"epoch": time.time(),
	}

	held = get_campaign_execution_lock(campaign)
	if held:
		if _same_owner(held, mode=mode, session_id=session_id, owner_token=owner_token):
			payload["acquired_at"] = held.get("acquired_at") or payload["acquired_at"]
			_store(key, payload, ttl)
			return {"ok": True, "refreshed": True, "lock": payload}
		frappe.throw(
			f"Campaign {campaign} already owned by mode={held.get('mode')} "
			f"session={held.get('session_id')}; refuse {mode} acquire",
			frappe.ValidationError,
		)

	# Atomic NX via Redis when available.
	try:
		r = _redis()
		ok = r.set(key, _encode(payload), nx=True, ex=ttl)
		if ok:
			frappe.cache().set_value(key, payload, expires_in_sec=ttl)
			return {"ok": True, "acquired": True, "lock": payload}
		# Lost race — re-read
		held2 = get_campaign_execution_lock(campaign)
		if held2 and _same_owner(held2, mode=mode, session_id=session_id, owner_token=owner_token):
			_store(key, payload, ttl)
			return {"ok": True, "refreshed": True, "lock": payload}
		frappe.throw(
			f"Campaign {campaign} execution lock race lost "
			f"(held mode={((held2 or {}).get('mode'))})",
			frappe.ValidationError,
		)
	except frappe.ValidationError:
		raise
	except Exception:
		# Fallback: frappe.cache only (unit tests / degraded Redis)
		if frappe.cache().get_value(key):
			held3 = get_campaign_execution_lock(campaign)
			frappe.throw(
				f"Campaign {campaign} already owned by mode={(held3 or {}).get('mode')}",
				frappe.ValidationError,
			)
		frappe.cache().set_value(key, payload, expires_in_sec=ttl)
		return {"ok": True, "acquired": True, "lock": payload, "backend": "frappe_cache"}


def refresh_campaign_execution_lock(
	campaign: str,
	*,
	mode: str,
	session_id: str,
	owner_token: str,
	ttl_s: int | None = None,
) -> bool:
	held = get_campaign_execution_lock(campaign)
	if not held:
		return False
	if not _same_owner(held, mode=mode, session_id=session_id, owner_token=owner_token):
		return False
	ttl = int(ttl_s if ttl_s is not None else lock_ttl_seconds())
	held = dict(held)
	held["refreshed_at"] = str(now_datetime())
	held["epoch"] = time.time()
	_store(campaign_lock_key(campaign), held, ttl)
	return True


def release_campaign_execution_lock(
	campaign: str,
	*,
	mode: str,
	session_id: str,
	owner_token: str,
) -> dict[str, Any]:
	"""Release only if caller owns the lock. Never deletes another owner's lock."""
	key = campaign_lock_key(campaign)
	held = get_campaign_execution_lock(campaign)
	if not held:
		return {"ok": True, "released": False, "reason": "absent"}
	if not _same_owner(held, mode=mode, session_id=session_id, owner_token=owner_token):
		return {
			"ok": False,
			"released": False,
			"reason": "not_owner",
			"held": {
				"mode": held.get("mode"),
				"session_id": held.get("session_id"),
			},
		}
	_delete(key)
	return {"ok": True, "released": True, "lock": held}


def recover_stale_campaign_execution_lock(
	campaign: str,
	*,
	expected_mode: str,
	expected_session_id: str,
	expected_owner_token: str,
	confirm: str,
	max_age_s: int = DEFAULT_TTL_S,
) -> dict[str, Any]:
	"""Verified recovery: only clear a lock that matches expected ownership and is stale.

	confirm must equal RECOVER_CAMPAIGN_EXEC_LOCK.
	"""
	if str(confirm or "").strip() != "RECOVER_CAMPAIGN_EXEC_LOCK":
		frappe.throw("confirm must equal RECOVER_CAMPAIGN_EXEC_LOCK", frappe.ValidationError)
	held = get_campaign_execution_lock(campaign)
	if not held:
		return {"ok": True, "recovered": False, "reason": "absent"}
	if not _same_owner(
		held,
		mode=expected_mode,
		session_id=expected_session_id,
		owner_token=expected_owner_token,
	):
		frappe.throw(
			"Refuse recovery: lock ownership does not match expected session/token",
			frappe.ValidationError,
		)
	age = time.time() - float(held.get("epoch") or 0)
	if age < max(60, int(max_age_s)):
		frappe.throw(
			f"Refuse recovery: lock age {age:.0f}s is below verified stale threshold "
			f"{max_age_s}s — stop the owner first",
			frappe.ValidationError,
		)
	_delete(campaign_lock_key(campaign))
	return {"ok": True, "recovered": True, "age_s": age, "cleared": held}


def assert_campaign_free_for_mode(campaign: str, mode: str) -> None:
	"""Throw if another mode owns the campaign."""
	held = get_campaign_execution_lock(campaign)
	if not held:
		return
	if str(held.get("mode")) != str(mode):
		frappe.throw(
			f"Campaign {campaign} owned by {held.get('mode')} "
			f"(session={held.get('session_id')}); {mode} refused",
			frappe.ValidationError,
		)


def sequential_still_active(campaign: str) -> dict[str, Any]:
	"""Read-only probe used before staggered migration."""
	from erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild import (
		_executor_lock_key,
		get_seq_session,
		parse_campaign_session_id,
	)

	sess = get_seq_session()
	exec_lock = frappe.cache().get_value(_executor_lock_key(campaign))
	camp_lock = get_campaign_execution_lock(campaign)
	status = (sess or {}).get("status") if sess and sess.get("campaign") == campaign else None
	active = bool(
		exec_lock
		or (status == "Running")
		or (camp_lock and camp_lock.get("mode") == MODE_SEQUENTIAL)
	)
	return {
		"active": active,
		"seq_session_id": (sess or {}).get("session_id") if sess else parse_campaign_session_id(campaign),
		"seq_status": status,
		"executor_lock": exec_lock,
		"campaign_lock": camp_lock,
	}


def _store(key: str, payload: dict, ttl: int) -> None:
	try:
		_redis().set(key, _encode(payload), ex=ttl)
	except Exception:
		pass
	frappe.cache().set_value(key, payload, expires_in_sec=ttl)


def _delete(key: str) -> None:
	try:
		_redis().delete(key)
	except Exception:
		pass
	try:
		frappe.cache().delete_value(key)
	except Exception:
		pass
