# Copyright (c) 2026, ERPNext Extensions contributors
"""Repair-scoped isolation from incidental Workstation writes (v5.5.11).

Root cause of Production (1020, tabWorkstation):
  MariaDB ``innodb_snapshot_isolation=ON`` + Core Job Card path:

    Stock Entry cancel/submit
      → JobCard.set_transferred_qty / set_manufactured_qty / set_status
        → JobCard.update_workstation_status
          → frappe.db.set_value("Workstation", …, "status", …)

  Concurrent Production updates to the same Workstation then raise MariaDB
  ER_CHECKREAD (1020) inside the long repair transaction.

Job Card Stock Rebuild does not own live Workstation operational state
(status / modified). Suppress ONLY those incidental writes while the repair
flag is active. Do not ignore_version, do not overwrite concurrent changes,
do not alter normal Desk / Core Workstation behaviour outside repair.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import frappe

# Repair-scoped flag — never set globally / outside Job Card Stock Rebuild.
SKIP_WORKSTATION_WRITES_FLAG = "jc_stock_rebuild_skip_workstation"

_PATCHED = False
_ORIG_UPDATE_STATUS = None
_ORIG_UPDATE_STATUS_IN = None


def _should_skip() -> bool:
	return bool(frappe.flags.get(SKIP_WORKSTATION_WRITES_FLAG))


def _patched_update_workstation_status(self):
	if _should_skip():
		return
	return _ORIG_UPDATE_STATUS(self)


def _patched_update_status_in_workstation(self, status):
	if _should_skip():
		return
	return _ORIG_UPDATE_STATUS_IN(self, status)


def ensure_workstation_isolation_patches() -> None:
	"""Install once-per-process guarded wrappers on Core JobCard methods."""
	global _PATCHED, _ORIG_UPDATE_STATUS, _ORIG_UPDATE_STATUS_IN
	if _PATCHED:
		return
	from erpnext.manufacturing.doctype.job_card.job_card import JobCard

	_ORIG_UPDATE_STATUS = JobCard.update_workstation_status
	_ORIG_UPDATE_STATUS_IN = JobCard.update_status_in_workstation
	JobCard.update_workstation_status = _patched_update_workstation_status  # type: ignore[method-assign]
	JobCard.update_status_in_workstation = _patched_update_status_in_workstation  # type: ignore[method-assign]
	_PATCHED = True


@contextmanager
def suppress_workstation_writes() -> Iterator[None]:
	"""Activate repair-scoped skip of incidental Workstation status writes."""
	ensure_workstation_isolation_patches()
	prev = frappe.flags.get(SKIP_WORKSTATION_WRITES_FLAG)
	frappe.flags[SKIP_WORKSTATION_WRITES_FLAG] = True
	try:
		yield
	finally:
		frappe.flags[SKIP_WORKSTATION_WRITES_FLAG] = prev


def is_workstation_write_suppressed() -> bool:
	return _should_skip()
