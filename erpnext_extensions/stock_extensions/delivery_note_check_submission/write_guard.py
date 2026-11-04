# Copyright (c) 2026, ERPNext Extensions contributors
"""Strict read-only database / side-effect enforcement for Check Submission.

Does not use savepoints. Any accidental mutating SQL or enqueue / notify call
raises ``ReadOnlyViolation`` immediately.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Iterator

import frappe
from frappe import _


class ReadOnlyViolation(frappe.ValidationError):
	"""Raised when Check Submission attempts a forbidden write or side effect."""


_MUTATING_SQL = re.compile(
	r"^\s*(INSERT|UPDATE|DELETE|REPLACE|ALTER|DROP|CREATE|TRUNCATE|RENAME|"
	r"CALL|LOAD|HANDLER|GRANT|REVOKE|LOCK|UNLOCK|MERGE|DO\s|"
	r"SAVEPOINT|RELEASE\s+SAVEPOINT|ROLLBACK|COMMIT|"
	r"START\s+TRANSACTION|BEGIN\b)",
	re.IGNORECASE,
)

_ALLOWED_PREFIX = re.compile(
	r"^\s*(WITH\b|SELECT\b|SHOW\b|DESCRIBE\b|DESC\b|EXPLAIN\b|SET\s+NAMES\b|"
	r"SET\s+CHARACTER\b|SET\s+SESSION\s+CHARACTER\b|SET\s+@@|"
	r"SET\s+autocommit\b|SET\s+sql_mode\b)",
	re.IGNORECASE,
)


def _normalize_sql(query) -> str:
	if query is None:
		return ""
	if not isinstance(query, str):
		query = str(query)
	# Strip leading SQL comments / parentheses used by some query builders.
	q = query.strip()
	while q.startswith("("):
		q = q[1:].lstrip()
	# Remove leading /* ... */ or -- comments once.
	while True:
		if q.startswith("/*"):
			end = q.find("*/")
			if end < 0:
				break
			q = q[end + 2 :].lstrip()
			continue
		if q.startswith("--"):
			nl = q.find("\n")
			if nl < 0:
				return ""
			q = q[nl + 1 :].lstrip()
			continue
		break
	return q


def is_mutating_sql(query) -> bool:
	q = _normalize_sql(query)
	if not q:
		return False
	if _ALLOWED_PREFIX.match(q):
		return False
	return bool(_MUTATING_SQL.match(q))


@contextmanager
def enforce_read_only() -> Iterator[None]:
	"""Block DB mutations, commits, enqueue, and realtime publish for the window."""
	db = frappe.db
	orig_sql = db.sql
	orig_commit = db.commit
	orig_rollback = db.rollback
	orig_set_value = db.set_value
	orig_delete = db.delete
	orig_bulk_insert = getattr(db, "bulk_insert", None)
	orig_sql_ddl = getattr(db, "sql_ddl", None)
	orig_enqueue = frappe.enqueue
	orig_enqueue_doc = getattr(frappe, "enqueue_doc", None)
	orig_publish = getattr(frappe, "publish_realtime", None)

	def _reject(kind: str, detail: str = ""):
		msg = _("Check Submission is read-only and blocked a {0} attempt.").format(kind)
		if detail:
			msg = f"{msg} {detail}"
		raise ReadOnlyViolation(msg)

	def guarded_sql(query, *args, **kwargs):
		if is_mutating_sql(query):
			_reject("SQL write", _normalize_sql(query)[:180])
		return orig_sql(query, *args, **kwargs)

	def guarded_commit(*_a, **_k):
		_reject("commit")

	def guarded_rollback(*, save_point=None, chain=False):
		# Full rollback would destroy the outer request transaction; savepoints
		# are forbidden by architecture. Reject both.
		_reject("rollback" + (f" (save_point={save_point})" if save_point else ""))

	def guarded_set_value(*args, **kwargs):
		_reject("db.set_value")

	def guarded_delete(*args, **kwargs):
		_reject("db.delete")

	def guarded_bulk_insert(*args, **kwargs):
		_reject("db.bulk_insert")

	def guarded_sql_ddl(*args, **kwargs):
		_reject("db.sql_ddl")

	def guarded_enqueue(*args, **kwargs):
		_reject("frappe.enqueue")

	def guarded_enqueue_doc(*args, **kwargs):
		_reject("frappe.enqueue_doc")

	def guarded_publish(*args, **kwargs):
		_reject("frappe.publish_realtime")

	db.sql = guarded_sql  # type: ignore[method-assign]
	db.commit = guarded_commit  # type: ignore[method-assign]
	db.rollback = guarded_rollback  # type: ignore[method-assign]
	db.set_value = guarded_set_value  # type: ignore[method-assign]
	db.delete = guarded_delete  # type: ignore[method-assign]
	if orig_bulk_insert is not None:
		db.bulk_insert = guarded_bulk_insert  # type: ignore[method-assign]
	if orig_sql_ddl is not None:
		db.sql_ddl = guarded_sql_ddl  # type: ignore[method-assign]
	frappe.enqueue = guarded_enqueue  # type: ignore[assignment]
	if orig_enqueue_doc is not None:
		frappe.enqueue_doc = guarded_enqueue_doc  # type: ignore[assignment]
	if orig_publish is not None:
		frappe.publish_realtime = guarded_publish  # type: ignore[assignment]

	frappe.flags.dn_check_submission_read_only = True
	try:
		yield
	finally:
		db.sql = orig_sql  # type: ignore[method-assign]
		db.commit = orig_commit  # type: ignore[method-assign]
		db.rollback = orig_rollback  # type: ignore[method-assign]
		db.set_value = orig_set_value  # type: ignore[method-assign]
		db.delete = orig_delete  # type: ignore[method-assign]
		if orig_bulk_insert is not None:
			db.bulk_insert = orig_bulk_insert  # type: ignore[method-assign]
		if orig_sql_ddl is not None:
			db.sql_ddl = orig_sql_ddl  # type: ignore[method-assign]
		frappe.enqueue = orig_enqueue  # type: ignore[assignment]
		if orig_enqueue_doc is not None:
			frappe.enqueue_doc = orig_enqueue_doc  # type: ignore[assignment]
		if orig_publish is not None:
			frappe.publish_realtime = orig_publish  # type: ignore[assignment]
		frappe.flags.dn_check_submission_read_only = False
