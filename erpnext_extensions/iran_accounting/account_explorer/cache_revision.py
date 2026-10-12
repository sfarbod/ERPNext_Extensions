# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Per-company accounting revision for Account Explorer prepared results.

The revision advances with an atomic SQL increment so concurrent GL writers
cannot collapse two commits into one revision number. The write stays in the
caller's database transaction: a rolled-back mutation also rolls back the bump.
"""

from __future__ import annotations

import time

import frappe
from frappe.utils import now_datetime

REVISION_DOCTYPE = "Account Explorer Accounting Revision"


def get_accounting_revision(company: str) -> int:
	if not company:
		return 0
	value = frappe.db.get_value(REVISION_DOCTYPE, {"company": company}, "revision")
	if value is None:
		return _ensure_revision_row(company)
	return int(value or 1)


def bump_accounting_revision(doc=None, method: str | None = None, company: str | None = None) -> int:
	"""Increment company accounting revision (invalidates prepared results).

	Doc-event hooks call this as ``(doc, method)``. Programmatic callers may use
	``bump_accounting_revision(company=...)``.
	"""
	# Legacy / convenience: first positional arg may be a company name string.
	if isinstance(doc, str) and not company:
		company = doc
		doc = None

	if not company and doc is not None:
		company = getattr(doc, "company", None)

	# Dimension metadata has no company — invalidate every tracked company.
	if not company and doc is not None and getattr(doc, "doctype", None) == "Accounting Dimension":
		last = 0
		for row in frappe.get_all(REVISION_DOCTYPE, fields=["company"]):
			last = bump_accounting_revision(company=row.company)
		return last

	if not company:
		return 0

	# Prefer UPDATE without a prior SELECT of the same row. A preceding consistent
	# read plus concurrent writers triggers MariaDB 1020 under REPEATABLE READ.
	for attempt in range(12):
		try:
			frappe.db.sql(
				f"""
				update `tab{REVISION_DOCTYPE}`
				set revision = last_insert_id(revision + 1), last_bumped_on = %s
				where company = %s
				""",
				(now_datetime(), company),
			)
			if frappe.db._cursor.rowcount:
				return int(frappe.db.sql("select last_insert_id()")[0][0])
			_ensure_revision_row(company)
		except Exception as exc:
			if _is_retryable_concurrency(exc) and attempt < 11:
				time.sleep(0.005 * (attempt + 1))
				continue
			raise
	frappe.throw(f"Could not bump accounting revision for {company}")


def _ensure_revision_row(company: str) -> int:
	"""Insert the company revision row if missing. Safe under concurrent first bumps."""
	try:
		frappe.db.sql(
			f"""
			insert into `tab{REVISION_DOCTYPE}`
				(name, creation, modified, modified_by, owner, docstatus, idx,
				 company, revision, last_bumped_on)
			values (%s, %s, %s, %s, %s, 0, 0, %s, 1, %s)
			""",
			(
				frappe.generate_hash(length=10),
				now_datetime(),
				now_datetime(),
				frappe.session.user,
				frappe.session.user,
				company,
				now_datetime(),
			),
		)
		return 1
	except Exception as exc:
		if not _is_duplicate_company_row(exc):
			raise
		existing = frappe.db.sql(
			f"select revision from `tab{REVISION_DOCTYPE}` where company=%s",
			company,
		)
		if existing:
			return int(existing[0][0] or 1)
		raise


def _is_duplicate_company_row(exc: Exception) -> bool:
	text = str(exc)
	return (
		isinstance(exc, frappe.UniqueValidationError)
		or "Duplicate entry" in text
		or "1062" in text
	)


def _is_retryable_concurrency(exc: Exception) -> bool:
	text = str(exc)
	return (
		"1020" in text
		or "1213" in text
		or "1205" in text
		or "Record has changed since last read" in text
		or "Deadlock" in text
		or "Lock wait timeout" in text
		or _is_duplicate_company_row(exc)
	)
