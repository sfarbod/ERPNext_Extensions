# Copyright (c) 2026, ERPNext Extensions contributors
"""Canonical batch identity: SLE.batch_no or single-batch SABB entry.

Multi-batch Serial and Batch Bundles are not mixed into another batch group.
"""

from __future__ import annotations

from typing import Any


def _g(row: Any, key: str, default=None):
	if row is None:
		return default
	if isinstance(row, dict):
		return row.get(key, default)
	return getattr(row, key, default)


def sabb_batches_from_entries(entries: list) -> list[str]:
	out = []
	seen = set()
	for row in entries or []:
		bn = (_g(row, "batch_no") or "").strip()
		if bn and bn not in seen:
			seen.add(bn)
			out.append(bn)
	return out


def canonical_batch_no(row: Any, sabb_entries: list | None = None) -> str | None:
	"""Return a single batch id, ``None`` for non-batch, ``\"*MULTI*\"`` if mixed SABB."""
	bn = (_g(row, "batch_no") or "").strip()
	if bn:
		return bn
	batches = sabb_batches_from_entries(sabb_entries or _g(row, "sabb_entries") or [])
	if not batches:
		bundle_bn = (_g(row, "sabb_batch_no") or "").strip()
		return bundle_bn or None
	if len(batches) > 1:
		return "*MULTI*"
	return batches[0]


def group_key(item_code, warehouse, canonical_batch, posting_date, posting_time) -> tuple:
	batch = (canonical_batch or "") if canonical_batch != "*MULTI*" else "*MULTI*"
	return (
		item_code or "",
		warehouse or "",
		batch,
		str(posting_date or ""),
		str(posting_time or "")[:8] if posting_time is not None else "",
	)
