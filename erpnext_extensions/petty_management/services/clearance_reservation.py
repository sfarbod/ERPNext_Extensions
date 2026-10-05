"""SQL predicates + locks for PM Clearance funding reservation (shared, no service cycles).

v5.5.5 — active lifecycle reserves PM Request / Opening Advance funding until
Rejected, Cancelled, or Deleted. Draft, Pending*, Returned Draft, Approved,
Pending JE Submission, and Settled all reserve.
"""

from __future__ import annotations

import frappe
from frappe.model.document import Document

from erpnext_extensions.petty_management.services.constants import (
	FUNDING_SOURCE_OPENING_ADVANCE,
	FUNDING_SOURCE_PM_REQUEST,
)

# Business statuses that permanently release funding reservation.
_RELEASED_STATUSES = ("Cancelled", "Rejected")


def clearance_reserves_pm_request_balance_sql(table_alias: str = "p") -> str:
	"""SQL predicate for clearances whose allocation rows reserve funding.

	v5.5.5 active lifecycle (reserves):
	- docstatus 0 or 1 (not cancelled)
	- status NOT IN ('Cancelled', 'Rejected')

	Covers Draft (new or Returned), Pending Approval / Pending Finance Review,
	Approved, Pending Journal Entry Submission, Settled.

	Released: Rejected, Cancelled (docstatus 2), Deleted (no row).
	"""
	p = table_alias
	return f"""
		IFNULL({p}.docstatus, 0) < 2
		AND IFNULL({p}.status, '') NOT IN ('Cancelled', 'Rejected')
	"""


def clearance_reserves_pm_request_balance(doc: Document | dict | None) -> bool:
	"""Python twin of ``clearance_reserves_pm_request_balance_sql`` for one clearance."""
	if not doc:
		return False
	if isinstance(doc, dict):
		docstatus = int(doc.get("docstatus") or 0)
		status = (doc.get("status") or "").strip()
	else:
		docstatus = int(getattr(doc, "docstatus", 0) or 0)
		status = (getattr(doc, "status", None) or "").strip()
	if docstatus >= 2:
		return False
	if status in _RELEASED_STATUSES:
		return False
	return True


_ALLOC_LOCK_PREFIX_REQ = "pm_req_alloc:"
_ALLOC_LOCK_PREFIX_OA = "pm_oa_alloc:"
_ALLOC_LOCK_TIMEOUT_SEC = 30


def _acquire_named_locks(keys: list[str]) -> None:
	"""Acquire MariaDB named locks (GET_LOCK) in order; release after commit/rollback.

	Named locks serialize allocators across connections without InnoDB gap/snapshot
	conflicts on allocation child rows. Held until COMMIT/ROLLBACK of this request.
	"""
	if not keys:
		return
	held: list[str] = []
	try:
		for key in keys:
			got = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (key, _ALLOC_LOCK_TIMEOUT_SEC))
			if not got or int(got[0][0] or 0) != 1:
				raise frappe.ValidationError(
					frappe._("Could not acquire funding allocation lock ({0}). Please retry.").format(key)
				)
			held.append(key)
	except Exception:
		for key in held:
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (key,))
		raise

	def _release() -> None:
		for key in held:
			try:
				frappe.db.sql("SELECT RELEASE_LOCK(%s)", (key,))
			except Exception:
				pass

	frappe.db.after_commit.add(_release)
	frappe.db.after_rollback.add(_release)


def lock_pm_requests_for_allocation(pm_request_names: list[str] | set[str] | tuple[str, ...]) -> None:
	"""Serialize competing allocations against the same PM Request(s).

	Uses MariaDB ``GET_LOCK('pm_req_alloc:<name>')`` in sorted name order, held
	until transaction commit/rollback. Also takes ``SELECT … FOR UPDATE`` on the
	PM Request row so the funding parent cannot be deleted mid-validate.
	"""
	names = sorted({(n or "").strip() for n in pm_request_names if (n or "").strip()})
	_acquire_named_locks([f"{_ALLOC_LOCK_PREFIX_REQ}{n}" for n in names])
	for name in names:
		frappe.db.sql("SELECT name FROM `tabPM Request` WHERE name=%s FOR UPDATE", (name,))


def lock_pm_opening_advances_for_allocation(
	opening_names: list[str] | set[str] | tuple[str, ...]
) -> None:
	"""Serialize competing Opening Advance allocations (same pattern as PM Request)."""
	names = sorted({(n or "").strip() for n in opening_names if (n or "").strip()})
	_acquire_named_locks([f"{_ALLOC_LOCK_PREFIX_OA}{n}" for n in names])
	for name in names:
		frappe.db.sql(
			"SELECT name FROM `tabPM Opening Advance` WHERE name=%s FOR UPDATE", (name,)
		)


def pm_request_allocation_sql_filter(table_alias: str = "c") -> str:
	a = table_alias
	return f"""
		(
			IFNULL({a}.funding_source_type, '') IN ('', '{FUNDING_SOURCE_PM_REQUEST}')
			AND IFNULL({a}.pm_request, '') != ''
		)
	"""


def opening_allocation_sql_filter(table_alias: str = "c") -> str:
	a = table_alias
	return f"""
		IFNULL({a}.funding_source_type, '') = '{FUNDING_SOURCE_OPENING_ADVANCE}'
		AND IFNULL({a}.pm_opening_advance, '') != ''
	"""
