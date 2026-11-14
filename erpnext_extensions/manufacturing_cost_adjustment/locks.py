# Copyright (c) 2026, ERPNext Extensions contributors
"""Exclusive per-Stock-Entry capitalization lock for MCA."""

from __future__ import annotations

import frappe
from frappe import _

from erpnext_extensions.manufacturing_cost_adjustment.constants import LOCK_PREFIX


def lock_key(stock_entry: str) -> str:
	return f"{LOCK_PREFIX}{stock_entry}"


def acquire_se_lock(stock_entry: str, owner: str, *, timeout: int = 2) -> bool:
	"""Acquire cache lock. Returns True on success."""
	key = lock_key(stock_entry)
	# frappe.cache().setnx style via redis
	cache = frappe.cache()
	ok = cache.set(key, owner, ex=3600, nx=True)
	if ok:
		return True
	current = cache.get(key)
	if current and (current == owner or current.decode() == owner if isinstance(current, bytes) else current == owner):
		return True
	return False


def release_se_lock(stock_entry: str, owner: str | None = None) -> None:
	key = lock_key(stock_entry)
	cache = frappe.cache()
	current = cache.get(key)
	if current is None:
		return
	cur = current.decode() if isinstance(current, bytes) else current
	if owner is None or cur == owner:
		cache.delete(key)


def assert_se_lock(stock_entry: str, owner: str) -> None:
	if not acquire_se_lock(stock_entry, owner):
		frappe.throw(
			_("Manufacturing Cost Adjustment lock is held for Stock Entry {0}").format(stock_entry)
		)
