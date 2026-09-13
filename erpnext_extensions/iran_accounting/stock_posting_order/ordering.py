# Copyright (c) 2026, ERPNext Extensions contributors
"""SLE ordering helpers matching ERPNext 16.34.2.

Canonical order (stock_ledger.py, Stock Ledger report, future-qty updates)::

    ORDER BY posting_datetime ASC, creation ASC

Same ``posting_datetime``: the earlier ``creation`` is processed first.
Insertion/voucher-name order is not used.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from frappe.utils import get_datetime

from erpnext_extensions.iran_accounting.stock_posting_order import MINIMUM_DEPENDENT_SECONDS


def sle_sort_key(row: Any) -> tuple:
	"""Tie-break identical to ERPNext ``order by posting_datetime, creation``."""
	return (
		_as_datetime(getattr(row, "posting_datetime", None) or row.get("posting_datetime")),
		str(getattr(row, "creation", None) or row.get("creation") or ""),
		str(getattr(row, "name", None) or row.get("name") or ""),
	)


def sort_sles(rows: list) -> list:
	return sorted(rows, key=sle_sort_key)


def _as_datetime(value) -> datetime:
	if isinstance(value, datetime):
		return value
	return get_datetime(value)


def combine_posting(posting_date, posting_time) -> datetime:
	if posting_time is None:
		posting_time = "00:00:00"
	if hasattr(posting_time, "total_seconds") and not isinstance(posting_time, datetime):
		posting_time = str(posting_time)
	return get_datetime(f"{posting_date} {posting_time}")


def add_seconds(dt, seconds: int = MINIMUM_DEPENDENT_SECONDS) -> datetime:
	return _as_datetime(dt) + timedelta(seconds=int(seconds))


def crosses_posting_date(current_dt, proposed_dt) -> bool:
	return _as_datetime(current_dt).date() != _as_datetime(proposed_dt).date()


def format_time(dt) -> str:
	return _as_datetime(dt).strftime("%H:%M:%S")


def format_date(dt) -> str:
	return _as_datetime(dt).strftime("%Y-%m-%d")


def format_datetime(dt) -> str:
	return _as_datetime(dt).strftime("%Y-%m-%d %H:%M:%S")
