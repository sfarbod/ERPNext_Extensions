# Copyright (c) 2026, ERPNext Extensions contributors
"""Keep Frappe API errors as JSON when diagnostic stdout/stderr is a dead pipe.

On development.localhost, ``bench serve`` stdout/stderr can be a pipe whose
reader is gone (deleted tty). ``frappe.errprint`` then does ``print()`` which
raises ``BrokenPipeError``. That exception escapes ``handle_exception``,
Werkzeug's debugger returns HTML 500, and Desk ``request.js`` fails with
``JSON Parse error: Unrecognized token '<'``.

This module does **not** change stock / batch / I4 / workflow business rules.
It only stops diagnostic I/O from replacing the original Frappe exception.

Snapshot policy
---------------
``log_error_snapshot`` is skipped for ``frappe.ValidationError`` and subclasses
(including ``BatchNegativeStockError`` and ``NegativeStockError``).

Why this is safe and scoped:

- Those exceptions already reach the client as JSON 417 ``_server_messages``.
- Frappe ``developer_mode`` snapshots dump frame locals (``with_context=True``).
  On a Stock Entry submit that can stall for minutes **while the write
  transaction is still open** (rollback runs only after ``handle_exception``).
- Not skipped: ``AuthenticationError``, ``PermissionError``, ``QueryTimeoutError``,
  generic ``RuntimeError`` / unexpected server errors. Those still snapshot.
- ``AuthenticationError`` is already in Frappe ``EXCLUDE_EXCEPTIONS``.

Diagnostic snapshot failures (including BrokenPipe while snapshotting) are
swallowed. Snapshot code must never replace the original application exception.
"""

from __future__ import annotations

import errno

import frappe

_PATCHED = False
# EPIPE: classic dead pipe. ECONNRESET: write to a closed socket — same class of
# diagnostic I/O failure, not an application bug. Other OSError values re-raise
# from errprint (e.g. ENOSPC, EIO).
_BROKEN_PIPE_ERRNOS = {errno.EPIPE, errno.ECONNRESET}


def apply_safe_error_transport() -> None:
	"""Idempotent. Safe to call from ``before_request`` / ``before_job``."""
	global _PATCHED
	if _PATCHED:
		return
	_patch_errprint()
	_patch_log_error_snapshot()
	_PATCHED = True


def _is_broken_pipe(exc: BaseException) -> bool:
	if isinstance(exc, BrokenPipeError):
		return True
	if isinstance(exc, OSError) and getattr(exc, "errno", None) in _BROKEN_PIPE_ERRNOS:
		return True
	return False


def _patch_errprint() -> None:
	original = frappe.errprint
	if getattr(original, "_ee_safe_broken_pipe", False):
		return

	def errprint(msg: str) -> None:
		try:
			return original(msg)
		except OSError as e:
			if not _is_broken_pipe(e):
				raise
			# print() failed after as_unicode; still record the traceback for JSON.
			frappe.local.error_log.append({"exc": frappe.as_unicode(msg)})

	errprint._ee_safe_broken_pipe = True
	errprint._ee_original = original
	frappe.errprint = errprint


def _patch_log_error_snapshot() -> None:
	import frappe.app as frappe_app
	import frappe.utils.error as error_mod

	original = error_mod.log_error_snapshot
	if getattr(original, "_ee_skip_validation_snapshot", False):
		return

	def log_error_snapshot(exception: Exception) -> None:
		if isinstance(exception, frappe.ValidationError):
			return
		try:
			return original(exception)
		except Exception:
			# Diagnostic-only. Never replace the original application exception
			# (including BrokenPipe from logger/stderr while snapshotting).
			return

	log_error_snapshot._ee_skip_validation_snapshot = True
	log_error_snapshot._ee_original = original
	error_mod.log_error_snapshot = log_error_snapshot
	frappe_app.log_error_snapshot = log_error_snapshot
