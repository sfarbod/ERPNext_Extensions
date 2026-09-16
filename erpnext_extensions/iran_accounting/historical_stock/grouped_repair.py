# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.15 SAFE_GROUP / SAFE_SEQUENTIAL_GROUP execution with savepoints."""

from __future__ import annotations

from datetime import datetime
from time import perf_counter

import frappe

from erpnext_extensions.iran_accounting.historical_stock.campaign_clusters import (
	SAFE_GROUP,
	SAFE_SEQUENTIAL_GROUP,
	UNSAFE_GROUP,
	classify_group,
)


def run_grouped_repair(roots: list[dict], apply_fn, *, dry_run=True, checkpoint_every=10) -> dict:
	"""Execute grouped repair with one logical campaign + savepoint per root.

	- UNSAFE_GROUP → refuse
	- SAFE_GROUP → independent roots (any order)
	- SAFE_SEQUENTIAL_GROUP → oldest-first
	- rollback only failed root; continue remaining
	- checkpoint every N successful repairs
	"""
	gclass = classify_group(roots)
	if gclass == UNSAFE_GROUP:
		return {
			"ok": False,
			"aborted": True,
			"group_class": UNSAFE_GROUP,
			"reason": "Different roots inside same dependency tree / duplicate identity",
			"applied": [],
			"failed": [],
		}

	ordered = list(roots)
	if gclass == SAFE_SEQUENTIAL_GROUP:
		ordered = sorted(ordered, key=lambda r: str(r.get("posting_datetime") or r.get("posting_date") or ""))

	t0 = perf_counter()
	applied, failed, checkpoints = [], [], []
	if dry_run:
		for r in ordered:
			try:
				out = apply_fn(r, dry_run=True)
				applied.append({"voucher": r.get("voucher"), "dry": out})
			except Exception as exc:
				failed.append({"voucher": r.get("voucher"), "error": str(exc)})
		return {
			"ok": not failed,
			"dry_run": True,
			"group_class": gclass,
			"n_roots": len(ordered),
			"applied": applied,
			"failed": failed,
			"elapsed_seconds": round(perf_counter() - t0, 2),
		}

	frappe.flags["historical_repair"] = True
	try:
		for i, r in enumerate(ordered, 1):
			sp = f"grp_{frappe.generate_hash(length=8)}"
			frappe.db.savepoint(sp)
			try:
				out = apply_fn(r, dry_run=False)
				if out.get("aborted") or out.get("blocked"):
					frappe.db.rollback(save_point=sp)
					failed.append({"voucher": r.get("voucher"), "out": out})
					continue
				applied.append({"voucher": r.get("voucher"), "out": out})
				frappe.db.commit()
				if i % int(checkpoint_every) == 0:
					checkpoints.append({"at": i, "ts": datetime.utcnow().isoformat() + "Z", "applied": len(applied)})
			except Exception as exc:
				frappe.db.rollback(save_point=sp)
				failed.append({"voucher": r.get("voucher"), "error": str(exc)})
	finally:
		frappe.flags["historical_repair"] = False

	return {
		"ok": bool(applied) and not failed,
		"dry_run": False,
		"group_class": gclass,
		"n_roots": len(ordered),
		"applied": applied,
		"failed": failed,
		"checkpoints": checkpoints,
		"elapsed_seconds": round(perf_counter() - t0, 2),
		"message": "Continue with dashboard validation + integrity + rescan after group",
	}
