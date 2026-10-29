# Copyright (c) 2026, ERPNext Extensions contributors
"""RQ/pickle-safe guard around ERPNext ``execute_reposting_entry``.

Historical Repair / Sequential HRC hold per-RIV Redis ownership while running
sync RIV execution. This module:

1. Wraps ``execute_reposting_entry`` so RQ/scheduler workers defer when another
   owner holds the claim (``riv_exec_lock_held``).
2. Wraps ``enqueue_reposting_entry`` so Core parallel reposting does not enqueue
   a second job for a sync-owned RIV (HRC-00008: sync Sequential is invisible
   to ``get_entries_with_active_jobs``, which only inspects RQ job ids).
3. Extends ``get_entries_with_active_jobs`` with Redis-claimed RIV names so Core
   treats sync-owned work as busy.

Must remain module-level callables so ``frappe.enqueue(execute_reposting_entry)``
can pickle/import them in a fresh worker process.
"""

from __future__ import annotations

from typing import Any, Callable

import frappe

_ORIG_EXECUTE_REPOSTING_ENTRY: Callable[..., Any] | None = None
_ORIG_PRIVATE_EXECUTE_REPOSTING_ENTRY: Callable[..., Any] | None = None
_ORIG_ENQUEUE_REPOSTING_ENTRY: Callable[..., Any] | None = None
_ORIG_GET_ENTRIES_WITH_ACTIVE_JOBS: Callable[..., Any] | None = None


def _resolve_original_execute_reposting_entry() -> Callable[..., Any]:
	"""Return the true upstream callable; never return this wrapper.

	Fresh RQ workers may unpickle this module before ``before_job`` runs
	``install_execute_reposting_entry_guard``. Resolve lazily without recursion.
	"""
	global _ORIG_EXECUTE_REPOSTING_ENTRY

	if _ORIG_EXECUTE_REPOSTING_ENTRY is not None and _ORIG_EXECUTE_REPOSTING_ENTRY is not execute_reposting_entry:
		return _ORIG_EXECUTE_REPOSTING_ENTRY

	import erpnext.stock.doctype.repost_item_valuation.repost_item_valuation as riv_mod

	stored = getattr(riv_mod, "_iran_original_execute_reposting_entry", None)
	if stored is not None and stored is not execute_reposting_entry:
		_ORIG_EXECUTE_REPOSTING_ENTRY = stored
		return stored

	current = riv_mod.execute_reposting_entry
	if current is not execute_reposting_entry:
		_ORIG_EXECUTE_REPOSTING_ENTRY = current
		return current

	frappe.throw(
		"RIV execute guard cannot resolve upstream execute_reposting_entry "
		"(original is missing or points at the guard itself).",
		title="RIV Execute Guard Misconfigured",
	)


def _ensure_iran_runtime_for_riv() -> None:
	"""Install Iran monkey patches before any RIV executor body runs.

	``before_job`` / ``before_request`` normally call bootstrap. Direct callers
	(``_execute_reposting_entry``, bench console, campaign drivers) can skip
	those hooks. Without patches, vanilla RIV writes float ``basic_rate =
	|SVD|/qty`` and Core Multi-FG can double-count the material pool on
	Manufacture recalculate. Fail closed if the recalculate wrapper is still
	inactive after bootstrap.
	"""
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		ensure_iran_riv_recalculate_wrapper_active,
	)

	ensure_iran_riv_recalculate_wrapper_active(bootstrap=True)


def _should_defer_riv_execution(name: str) -> bool:
	"""True when another sync owner holds the RIV (or company GL) claim."""
	from erpnext_extensions.iran_accounting.historical_stock.db_concurrency import (
		company_gl_lock_held,
		riv_exec_lock_held,
	)

	owner = getattr(frappe.local, "iran_hr_riv_exec_owner", None)
	if riv_exec_lock_held(name) and owner != name:
		return True
	if owner is None:
		company = frappe.db.get_value("Repost Item Valuation", name, "company")
		if company and company_gl_lock_held(company):
			return True
	return False


def execute_reposting_entry(name, continue_reposting=False):
	"""Compatibility wrapper — same contract as 5.5.22 nested patch."""
	_ensure_iran_runtime_for_riv()
	try:
		if _should_defer_riv_execution(name):
			# Sync campaign owns this RIV — do not start a second executor.
			return
	except Exception:
		# Fail-open: lock probe must never block legitimate upstream execution.
		pass

	original = _resolve_original_execute_reposting_entry()
	return original(name, continue_reposting=continue_reposting)


def _wrapped_private_execute_reposting_entry(name):
	"""Bootstrap-safe wrapper for ERPNext ``_execute_reposting_entry``.

	Campaign drivers often call the private executor and bypass both RQ
	``before_job`` and the public ``execute_reposting_entry`` guard.
	"""
	_ensure_iran_runtime_for_riv()
	try:
		if _should_defer_riv_execution(name):
			return
	except Exception:
		pass
	original = _ORIG_PRIVATE_EXECUTE_REPOSTING_ENTRY
	if original is None:
		import erpnext.stock.doctype.repost_item_valuation.repost_item_valuation as riv_mod

		original = getattr(riv_mod, "_iran_original_private_execute_reposting_entry", None)
	if original is None or original is _wrapped_private_execute_reposting_entry:
		frappe.throw(
			"RIV execute guard cannot resolve upstream _execute_reposting_entry.",
			title="RIV Execute Guard Misconfigured",
		)
	return original(name)


def enqueue_reposting_entry(name):
	"""Skip Core enqueue when Sequential/HR already owns this RIV (sync path)."""
	try:
		from erpnext_extensions.iran_accounting.historical_stock.db_concurrency import (
			riv_exec_lock_held,
		)

		if riv_exec_lock_held(name):
			return None
	except Exception:
		pass
	original = _ORIG_ENQUEUE_REPOSTING_ENTRY
	if original is None:
		import erpnext.stock.doctype.repost_item_valuation.repost_item_valuation as riv_mod

		original = getattr(riv_mod, "_iran_original_enqueue_reposting_entry", None)
	if original is None or original is enqueue_reposting_entry:
		frappe.throw(
			"RIV execute guard cannot resolve upstream enqueue_reposting_entry.",
			title="RIV Execute Guard Misconfigured",
		)
	return original(name)


def get_entries_with_active_jobs() -> set:
	"""Union Core RQ-active RIV names with Redis sync ownership claims."""
	original = _ORIG_GET_ENTRIES_WITH_ACTIVE_JOBS
	if original is None:
		import erpnext.stock.doctype.repost_item_valuation.repost_item_valuation as riv_mod

		original = getattr(riv_mod, "_iran_original_get_entries_with_active_jobs", None)
	if original is None or original is get_entries_with_active_jobs:
		frappe.throw(
			"RIV execute guard cannot resolve upstream get_entries_with_active_jobs.",
			title="RIV Execute Guard Misconfigured",
		)
	active = set(original() or set())
	try:
		from erpnext_extensions.iran_accounting.historical_stock.db_concurrency import (
			listed_claimed_riv_executions,
		)

		active |= listed_claimed_riv_executions()
	except Exception:
		pass
	return active


def install_execute_reposting_entry_guard(riv_mod) -> None:
	"""Idempotently install execute / enqueue / active-job guards on ERPNext RIV."""
	global \
		_ORIG_EXECUTE_REPOSTING_ENTRY, \
		_ORIG_PRIVATE_EXECUTE_REPOSTING_ENTRY, \
		_ORIG_ENQUEUE_REPOSTING_ENTRY, \
		_ORIG_GET_ENTRIES_WITH_ACTIVE_JOBS

	if getattr(riv_mod, "_iran_patched_execute_reposting_entry_lock", None):
		# Re-entered (e.g. apply_monkey_patches / second install): keep original stable.
		stored = getattr(riv_mod, "_iran_original_execute_reposting_entry", None)
		if stored is not None and stored is not execute_reposting_entry:
			_ORIG_EXECUTE_REPOSTING_ENTRY = stored
		if riv_mod.execute_reposting_entry is not execute_reposting_entry:
			riv_mod.execute_reposting_entry = execute_reposting_entry
		priv_stored = getattr(riv_mod, "_iran_original_private_execute_reposting_entry", None)
		if priv_stored is not None and priv_stored is not _wrapped_private_execute_reposting_entry:
			_ORIG_PRIVATE_EXECUTE_REPOSTING_ENTRY = priv_stored
		if getattr(riv_mod, "_execute_reposting_entry", None) is not _wrapped_private_execute_reposting_entry:
			riv_mod._execute_reposting_entry = _wrapped_private_execute_reposting_entry
		enq = getattr(riv_mod, "_iran_original_enqueue_reposting_entry", None)
		if enq is not None and enq is not enqueue_reposting_entry:
			_ORIG_ENQUEUE_REPOSTING_ENTRY = enq
		if getattr(riv_mod, "enqueue_reposting_entry", None) is not enqueue_reposting_entry:
			riv_mod.enqueue_reposting_entry = enqueue_reposting_entry
		active = getattr(riv_mod, "_iran_original_get_entries_with_active_jobs", None)
		if active is not None and active is not get_entries_with_active_jobs:
			_ORIG_GET_ENTRIES_WITH_ACTIVE_JOBS = active
		if getattr(riv_mod, "get_entries_with_active_jobs", None) is not get_entries_with_active_jobs:
			riv_mod.get_entries_with_active_jobs = get_entries_with_active_jobs
		return

	original = riv_mod.execute_reposting_entry
	if original is execute_reposting_entry:
		frappe.throw(
			"Refusing to install RIV execute guard over itself (recursion risk).",
			title="RIV Execute Guard Misconfigured",
		)

	_ORIG_EXECUTE_REPOSTING_ENTRY = original
	riv_mod.execute_reposting_entry = execute_reposting_entry
	riv_mod._iran_patched_execute_reposting_entry_lock = True
	riv_mod._iran_original_execute_reposting_entry = original

	private_original = riv_mod._execute_reposting_entry
	if private_original is not _wrapped_private_execute_reposting_entry:
		_ORIG_PRIVATE_EXECUTE_REPOSTING_ENTRY = private_original
		riv_mod._iran_original_private_execute_reposting_entry = private_original
		riv_mod._execute_reposting_entry = _wrapped_private_execute_reposting_entry
		riv_mod._iran_patched_private_execute_reposting_entry = True

	# Parallel-path ownership visibility (v5.6.28 / HRC-00008).
	if getattr(riv_mod, "enqueue_reposting_entry", None) is not enqueue_reposting_entry:
		_ORIG_ENQUEUE_REPOSTING_ENTRY = riv_mod.enqueue_reposting_entry
		riv_mod._iran_original_enqueue_reposting_entry = _ORIG_ENQUEUE_REPOSTING_ENTRY
		riv_mod.enqueue_reposting_entry = enqueue_reposting_entry
		riv_mod._iran_patched_enqueue_reposting_entry = True

	if getattr(riv_mod, "get_entries_with_active_jobs", None) is not get_entries_with_active_jobs:
		_ORIG_GET_ENTRIES_WITH_ACTIVE_JOBS = riv_mod.get_entries_with_active_jobs
		riv_mod._iran_original_get_entries_with_active_jobs = _ORIG_GET_ENTRIES_WITH_ACTIVE_JOBS
		riv_mod.get_entries_with_active_jobs = get_entries_with_active_jobs
		riv_mod._iran_patched_get_entries_with_active_jobs = True
