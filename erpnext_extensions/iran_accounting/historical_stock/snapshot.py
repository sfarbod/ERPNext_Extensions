# Copyright (c) 2026, ERPNext Extensions contributors
"""Identity-scoped before-images for Historical Repair rollback."""

from __future__ import annotations

import json

import frappe
from frappe.utils import flt

DATABASE_BACKUP_REQUIRED = "DATABASE BACKUP REQUIRED"
MAX_SNAPSHOT_CHARS = 400_000


def capture_identity_snapshot(voucher, item=None, warehouse=None, batch=None) -> dict:
	"""Capture SE / SLE / SABB / SBE / Bin / GL rows for one voucher identity."""
	se_before = []
	if voucher:
		se_before = frappe.db.sql(
			"""
			SELECT name, parent, item_code, qty, basic_rate, valuation_rate, amount, basic_amount,
			       s_warehouse, t_warehouse, serial_and_batch_bundle, batch_no
			FROM `tabStock Entry Detail`
			WHERE parent=%s
			""",
			voucher,
			as_dict=True,
		)
		if item:
			se_before = [r for r in se_before if r.item_code == item]
	sle_before = []
	if voucher:
		sle_conds = ["voucher_type='Stock Entry'", "voucher_no=%s", "is_cancelled=0"]
		sle_args: list = [voucher]
		if item:
			sle_conds.append("item_code=%s")
			sle_args.append(item)
		if warehouse:
			sle_conds.append("warehouse=%s")
			sle_args.append(warehouse)
		sle_before = frappe.db.sql(
			f"""
			SELECT name, item_code, warehouse, actual_qty, incoming_rate, outgoing_rate,
			       valuation_rate, stock_value_difference, stock_value, qty_after_transaction,
			       serial_and_batch_bundle, voucher_detail_no, batch_no
			FROM `tabStock Ledger Entry`
			WHERE {" AND ".join(sle_conds)}
			""",
			sle_args,
			as_dict=True,
		)
	bundles = sorted({r.serial_and_batch_bundle for r in sle_before if r.get("serial_and_batch_bundle")})
	sabb_before = []
	sbe_before = []
	if bundles:
		sabb_before = frappe.db.sql(
			"""
			SELECT name, avg_rate, total_amount, total_qty
			FROM `tabSerial and Batch Bundle`
			WHERE name IN %(n)s
			""",
			{"n": bundles},
			as_dict=True,
		)
		sbe_before = frappe.db.sql(
			"""
			SELECT name, parent, batch_no, qty, incoming_rate, stock_value_difference
			FROM `tabSerial and Batch Entry`
			WHERE parent IN %(n)s
			""",
			{"n": bundles},
			as_dict=True,
		)
	pairs = {(r.item_code, r.warehouse) for r in sle_before}
	if item and warehouse:
		pairs.add((item, warehouse))
	bin_before = []
	for it, wh in pairs:
		if not it or not wh:
			continue
		b = frappe.db.get_value(
			"Bin",
			{"item_code": it, "warehouse": wh},
			["name", "actual_qty", "stock_value", "valuation_rate"],
			as_dict=True,
		)
		if b:
			bin_before.append(b)
	gl_before = []
	if voucher:
		gl_before = frappe.db.sql(
			"""
			SELECT name, account, debit, credit, cost_center, is_cancelled
			FROM `tabGL Entry`
			WHERE voucher_type='Stock Entry' AND voucher_no=%s
			""",
			voucher,
			as_dict=True,
		)
	snap = {
		"voucher": voucher,
		"item": item,
		"warehouse": warehouse,
		"batch": batch,
		"stock_entry_before": _jsonable(se_before),
		"sle_before": _jsonable(sle_before),
		"serial_and_batch_bundle_before": _jsonable(sabb_before),
		"serial_and_batch_entry_before": _jsonable(sbe_before),
		"bin_before": _jsonable(bin_before),
		"gl_before": _jsonable(gl_before),
	}
	encoded = json.dumps(snap, default=str)
	full = len(encoded) <= MAX_SNAPSHOT_CHARS
	snap["full_rollback_possible"] = full
	if not full:
		snap["warning"] = DATABASE_BACKUP_REQUIRED
		snap["truncated"] = True
		snap["stock_entry_before"] = snap["stock_entry_before"][:40]
		snap["sle_before"] = snap["sle_before"][:80]
		snap["gl_before"] = []
		snap["full_rollback_possible"] = False
	return snap


def restore_snapshot(snapshot: dict, *, dry_run=True) -> dict:
	"""Restore captured before-images. Identity only. GL names must still exist."""
	if not snapshot:
		return {"restored": [], "full_rollback_possible": False, "warning": DATABASE_BACKUP_REQUIRED, "dry_run": dry_run}
	if snapshot.get("truncated") or not snapshot.get("full_rollback_possible", True):
		if not dry_run:
			frappe.throw(f"{DATABASE_BACKUP_REQUIRED}: this run cannot be rolled back from the in-app snapshot.")
		return {
			"dry_run": dry_run,
			"restored": [],
			"full_rollback_possible": False,
			"warning": DATABASE_BACKUP_REQUIRED,
		}
	ops = []
	ops.extend(_restore_doctype("Stock Entry Detail", snapshot.get("stock_entry_before"), ["basic_rate", "valuation_rate", "amount", "basic_amount"], dry_run))
	ops.extend(
		_restore_doctype(
			"Stock Ledger Entry",
			snapshot.get("sle_before"),
			["incoming_rate", "outgoing_rate", "valuation_rate", "stock_value_difference", "stock_value", "qty_after_transaction"],
			dry_run,
		)
	)
	ops.extend(_restore_doctype("Serial and Batch Bundle", snapshot.get("serial_and_batch_bundle_before"), ["avg_rate", "total_amount"], dry_run))
	ops.extend(_restore_doctype("Serial and Batch Entry", snapshot.get("serial_and_batch_entry_before"), ["incoming_rate", "stock_value_difference"], dry_run))
	ops.extend(_restore_doctype("Bin", snapshot.get("bin_before"), ["actual_qty", "stock_value", "valuation_rate"], dry_run))
	gl_rows = snapshot.get("gl_before") or []
	gl_ok = True
	for gl in gl_rows:
		if not frappe.db.exists("GL Entry", gl.get("name")):
			gl_ok = False
			break
	if gl_rows and not gl_ok:
		return {
			"dry_run": dry_run,
			"restored": ops,
			"full_rollback_possible": False,
			"warning": DATABASE_BACKUP_REQUIRED,
			"gl_restored": False,
		}
	ops.extend(_restore_doctype("GL Entry", gl_rows, ["debit", "credit"], dry_run))
	return {"dry_run": dry_run, "restored": ops, "full_rollback_possible": True, "count": len(ops)}


def _restore_doctype(doctype, rows, fields, dry_run) -> list[dict]:
	out = []
	for row in rows or []:
		name = row.get("name")
		if not name:
			continue
		values = {f: row.get(f) for f in fields if f in row}
		out.append({"doctype": doctype, "name": name, "values": values})
		if dry_run:
			continue
		if not frappe.db.exists(doctype, name):
			continue
		frappe.db.set_value(doctype, name, values, update_modified=False)
	return out


def _jsonable(rows):
	out = []
	for r in rows or []:
		if hasattr(r, "as_dict"):
			r = r.as_dict()
		out.append({k: (flt(v) if isinstance(v, float) else v) for k, v in dict(r).items()})
	return out
