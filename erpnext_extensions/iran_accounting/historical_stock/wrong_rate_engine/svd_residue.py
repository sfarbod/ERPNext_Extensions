# Copyright (c) 2026 — Selective SVD residue repair when valuation rate already matches
from __future__ import annotations

from frappe.utils import flt

import frappe

import frappe


def apply_svd_residue(row: dict, *, dry_run: bool = True) -> dict:
	"""When observed rate ≈ expected but stock_value_difference drifts, restore SVD.

	Typical transfer-out: incoming_rate=0, valuation_rate correct, SVD off by pennies
	or zeroed by a prior MA replay. Never changes posting order.
	"""
	from erpnext_extensions.iran_accounting.stock_posting_order.replay import sync_transfer_incoming_rates

	vn = row.get("voucher") or row.get("outbound_document")
	item = row.get("item")
	expected = flt(row.get("expected") or row.get("proposed_rate"))
	if not vn or not item or abs(expected) < 1e-9:
		return {"ok": False, "reason": "missing voucher/item/expected"}

	sles = frappe.db.sql(
		"""
		SELECT name, actual_qty, incoming_rate, valuation_rate, stock_value_difference, warehouse
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND item_code=%s AND is_cancelled=0
		""",
		(vn, item),
		as_dict=True,
	)
	if not sles:
		return {"ok": False, "reason": "no SLE"}

	writes = []
	for sle in sles:
		qty = flt(sle.actual_qty)
		if abs(qty) < 1e-12:
			continue
		# Expected signed SVD: outbound negative value, inbound positive
		exp_svd = -abs(expected) * abs(qty) if qty < 0 else abs(expected) * abs(qty)
		cur = flt(sle.stock_value_difference)
		if abs(cur - exp_svd) <= 0.5:
			continue
		writes.append({"name": sle.name, "old": cur, "new": exp_svd, "qty": qty, "wh": sle.warehouse})

	if not writes:
		return {"ok": True, "changed": 0, "reason": "svd_already_aligned", "dry_run": dry_run}

	if dry_run:
		return {"ok": True, "changed": len(writes), "writes": writes, "dry_run": True}

	for w in writes:
		frappe.db.set_value(
			"Stock Ledger Entry",
			w["name"],
			{"stock_value_difference": w["new"], "valuation_rate": abs(expected)},
			update_modified=False,
		)
	try:
		sync_transfer_incoming_rates(vn)
	except Exception:
		pass
	frappe.db.commit()
	return {"ok": True, "changed": len(writes), "writes": writes, "dry_run": False, "voucher": vn, "item": item}


def scan_matched_rate_svd_residue(company: str, *, limit: int = 200) -> list[dict]:
	"""Find Wrong Rate rows where rate matches but SVD does not."""
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.evidence import (
		build_evidence_package,
	)
	from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.ambiguity import (
		resolve_rate_ambiguity,
	)
	import frappe

	scan = scan_wrong_rates(company=company, limit=limit)
	out = []
	cache = {}
	for row in scan.get("rows") or []:
		evidence = build_evidence_package(row, cache=cache)
		res = resolve_rate_ambiguity(row, evidence, threshold=0.95)
		exp = flt(res.get("expected"))
		obs = flt(row.get("observed") or row.get("current_rate") or row.get("rate"))
		if not exp or abs(exp - obs) > max(1.0, abs(exp) * 1e-9):
			continue
		row = dict(row)
		row["expected"] = exp
		probe = apply_svd_residue(row, dry_run=True)
		if probe.get("ok") and int(probe.get("changed") or 0) > 0:
			out.append({**row, "svd_probe": probe})
		if len(out) >= 80:
			break
	return out
