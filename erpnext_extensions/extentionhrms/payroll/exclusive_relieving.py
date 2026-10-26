# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Framework-free helpers for exclusive relieving-date payroll math.

Business rule: ``relieving_date`` is the first *non-payable* day, so the last
payable day is ``relieving_date - 1``.

These helpers are imported by the Salary Slip / Payroll Entry extensions and by
plain ``pytest`` unit tests (no Frappe import required).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional


def _as_date(value) -> date:
	if isinstance(value, date):
		return value
	# Accept datetime / date-like with .date(), or ISO strings.
	if hasattr(value, "date") and callable(value.date):
		return value.date()
	return date.fromisoformat(str(value)[:10])


def exclusive_payable_end_date(
	start_date,
	end_date,
	relieving_date: Optional[object],
) -> date:
	"""Return the last payable date under exclusive-relieving policy.

	- No relieving date → payroll ``end_date``.
	- Relieving after the payroll period → payroll ``end_date``.
	- Relieving on or before ``start_date`` through ``end_date`` (inclusive) →
	  ``relieving_date - 1 day`` (may be *before* ``start_date``, yielding zero
	  payable days when combined with ``actual_start_date``).

	Joining-date inclusivity is handled separately by HRMS ``actual_start_date``.
	"""
	start = _as_date(start_date)
	end = _as_date(end_date)
	if not relieving_date:
		return end

	rel = _as_date(relieving_date)
	if start <= rel <= end:
		return rel - timedelta(days=1)
	return end


def calendar_payment_days(actual_start, actual_end) -> float:
	"""Inclusive calendar day count between actual start/end; never negative."""
	start = _as_date(actual_start)
	end = _as_date(actual_end)
	if end < start:
		return 0.0
	return float((end - start).days + 1)


def is_first_day_relieving(start_date, relieving_date: Optional[object]) -> bool:
	"""True when relieving falls exactly on the payroll period start."""
	if not relieving_date:
		return False
	return _as_date(relieving_date) == _as_date(start_date)
