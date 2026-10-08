# Copyright (c) 2026, ERPNext Extensions contributors
"""RQ/pickle-safe guard around ERPNext ``execute_reposting_entry``.

Historical Repair holds per-RIV and company-GL Redis locks while running narrow
RIV sync. This module-level wrapper defers scheduler/worker executors that would
otherwise dual-execute the same RIV.

Must remain a true module-level function so ``frappe.enqueue(execute_reposting_entry)``
(ERPNext parallel path) can pickle/import it in a fresh worker process.
"""

from __future__ import annotations

from typing import Any, Callable

import frappe

_ORIG_EXECUTE_REPOSTING_ENTRY: Callable[..., Any] | None = None


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


def execute_reposting_entry(name, continue_reposting=False):
	"""Compatibility wrapper — same contract as 5.5.22 nested patch."""
	try:
		from erpnext_extensions.iran_accounting.historical_stock.db_concurrency import (
			company_gl_lock_held,
			riv_exec_lock_held,
		)

		owner = getattr(frappe.local, "iran_hr_riv_exec_owner", None)
		if riv_exec_lock_held(name) and owner != name:
			# Sync campaign owns this RIV — do not start a second executor.
			return
		# Serialize company GL: while a sync identity holds the company GL
		# lock, defer all other RIV executors (they stay Queued/In Progress).
		if owner is None:
			company = frappe.db.get_value("Repost Item Valuation", name, "company")
			if company and company_gl_lock_held(company):
				return
	except Exception:
		# Fail-open: lock probe must never block legitimate upstream execution.
		pass

	original = _resolve_original_execute_reposting_entry()
	return original(name, continue_reposting=continue_reposting)


def install_execute_reposting_entry_guard(riv_mod) -> None:
	"""Idempotently install this module-level guard on the ERPNext RIV module."""
	global _ORIG_EXECUTE_REPOSTING_ENTRY

	if getattr(riv_mod, "_iran_patched_execute_reposting_entry_lock", None):
		# Re-entered (e.g. apply_monkey_patches / second install): keep original stable.
		stored = getattr(riv_mod, "_iran_original_execute_reposting_entry", None)
		if stored is not None and stored is not execute_reposting_entry:
			_ORIG_EXECUTE_REPOSTING_ENTRY = stored
		if riv_mod.execute_reposting_entry is not execute_reposting_entry:
			riv_mod.execute_reposting_entry = execute_reposting_entry
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
