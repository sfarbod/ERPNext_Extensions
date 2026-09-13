# Copyright (c) 2026, ERPNext Extensions contributors
"""Detect, preview, and apply production posting-order repairs."""

from __future__ import annotations

from datetime import datetime

import frappe
from frappe.utils import cint, flt, get_datetime, now_datetime

from erpnext_extensions.iran_accounting.stock_posting_order import (
	CONFIDENCE_EXACT,
	STATUS_BLOCKED,
	STATUS_DRAFT,
	STATUS_DRY_RUN,
	STATUS_ELIGIBLE,
	STATUS_INSUFFICIENT_STOCK,
	STATUS_MIDNIGHT,
	STATUS_MIDNIGHT_REVIEW,
	STATUS_REAL_STOCK_SHORTAGE,
	STATUS_REPAIRED,
	STATUS_STALE,
	STATUS_VALUATION_POISON,
)
from erpnext_extensions.iran_accounting.stock_posting_order.optimizer import simulate_running
from erpnext_extensions.iran_accounting.stock_posting_order.ordering import (
	format_date,
	format_time,
)
from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
	replay_item_warehouse,
	window_poison_reason,
)
from erpnext_extensions.iran_accounting.stock_posting_order.scanner import (
	run_full_history_scan,
	scan_same_time_groups,
)
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D

SCAN_LIMIT = 400


def _gl_balanced(voucher_no: str) -> dict:
	rows = frappe.db.sql(
		"""
		SELECT SUM(debit) debit, SUM(credit) credit
		FROM `tabGL Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND IFNULL(is_cancelled,0)=0
		""",
		voucher_no,
		as_dict=True,
	)
	debit = flt(rows[0].debit) if rows else 0
	credit = flt(rows[0].credit) if rows else 0
	return {"debit": debit, "credit": credit, "balanced": abs(debit - credit) < 0.5}


def scan_production_posting_order_anomalies(
	*,
	company: str | None = None,
	from_date: str | None = None,
	to_date: str | None = None,
	include_likely: bool = True,
	include_no_repair: bool = False,
) -> list[dict]:
	"""Find same-time inbound/outbound groups (SABB-aware, true opening)."""
	result = scan_same_time_groups(
		company=company,
		from_date=from_date,
		to_date=to_date,
		include_likely=include_likely,
		include_no_repair=include_no_repair,
	)
	rows = list(result.get("rows") or [])
	rows.sort(key=lambda r: (0 if r.get("eligible") else 1, str(r.get("status") or "")))
	return rows[:SCAN_LIMIT] if len(rows) > SCAN_LIMIT else rows


def dry_run(candidates: list[dict] | None = None, **scan_kwargs) -> dict:
	if candidates is not None:
		return {
			"dry_run": True,
			"status": STATUS_DRY_RUN,
			"count": len(candidates),
			"eligible": [r for r in candidates if r.get("eligible")],
			"rows": candidates,
		}
	scan_kwargs.setdefault("include_no_repair", False)
	result = run_full_history_scan(**scan_kwargs)
	rows = result.get("rows") or []
	return {
		"dry_run": True,
		"status": STATUS_DRY_RUN,
		"count": len(rows),
		"eligible": [r for r in rows if r.get("eligible")],
		"rows": rows,
		"summary": result.get("summary") or {},
		"timing": result.get("timing") or {},
		"sle_scanned": result.get("sle_scanned"),
	}


def _assert_preview_fresh(row: dict) -> None:
	in_mod = str(frappe.db.get_value("Stock Entry", row["inbound_document"], "modified"))
	out_mod = str(frappe.db.get_value("Stock Entry", row["outbound_document"], "modified"))
	if in_mod != str(row.get("inbound_modified")) or out_mod != str(row.get("outbound_modified")):
		frappe.throw(
			"Document changed since preview; aborting posting-order repair.",
			title="Stale preview",
		)
	in_ds = cint(frappe.db.get_value("Stock Entry", row["inbound_document"], "docstatus"))
	out_ds = cint(frappe.db.get_value("Stock Entry", row["outbound_document"], "docstatus"))
	if in_ds != 1 or out_ds != 1:
		status = "CANCELLED" if 2 in (in_ds, out_ds) else STATUS_DRAFT
		frappe.throw(f"Repair blocked ({status}): documents must be submitted.", title=status)


def _row_moves(row: dict) -> list[dict]:
	moves = list(row.get("moves") or [])
	if moves:
		return moves
	old = row.get("current_outbound_time")
	new = row.get("proposed_outbound_time")
	if new and old and str(new) != str(old):
		return [
			{
				"document": row["outbound_document"],
				"old": old,
				"new": new,
				"seconds": row.get("minimum_seconds_required") or 1,
			}
		]
	return []


def _update_posting_datetime(se_name: str, new_dt: datetime) -> None:
	frappe.db.set_value(
		"Stock Entry",
		se_name,
		{
			"posting_date": format_date(new_dt),
			"posting_time": format_time(new_dt),
			"set_posting_time": 1,
		},
		update_modified=True,
	)
	frappe.db.sql(
		"""
		UPDATE `tabStock Ledger Entry`
		SET posting_date=%s, posting_time=%s, posting_datetime=%s
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND is_cancelled=0
		""",
		(format_date(new_dt), format_time(new_dt), new_dt, se_name),
	)
	if frappe.db.exists("DocType", "Serial and Batch Bundle"):
		frappe.db.sql(
			"""
			UPDATE `tabSerial and Batch Bundle`
			SET posting_datetime=%s
			WHERE voucher_type='Stock Entry' AND voucher_no=%s
			""",
			(new_dt, se_name),
		)


def _voucher_item_warehouses(se_name: str) -> list[tuple[str, str]]:
	return frappe.db.sql(
		"""
		SELECT DISTINCT item_code, warehouse
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND is_cancelled=0
		""",
		se_name,
	)


def _maybe_riv(row: dict, vouchers: list[str]) -> str | None:
	if row.get("valuation_impact") != "VALUATION-IMPACTING" and row.get("gl_impact") != "RIV_REQUIRED":
		return None
	if not frappe.db.exists("DocType", "Repost Item Valuation"):
		return None
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import repost

	target = vouchers[-1] if vouchers else row.get("outbound_document")
	riv = frappe.new_doc("Repost Item Valuation")
	riv.company = row.get("company") or frappe.db.get_value("Stock Entry", target, "company")
	riv.based_on = "Transaction"
	riv.voucher_type = "Stock Entry"
	riv.voucher_no = target
	riv.allow_negative_stock = 1
	riv.flags.ignore_permissions = True
	riv.insert(ignore_permissions=True)
	repost(riv)
	return riv.name


def apply_repairs(rows: list[dict], *, dry_run: bool = True, user: str | None = None) -> dict:
	if dry_run:
		return dry_run_selected(rows)
	applied = []
	blocked = []
	run_id = f"PPO-{now_datetime().strftime('%Y%m%d%H%M%S')}-{frappe.generate_hash(length=6)}"
	log = _new_audit_log(run_id, user or frappe.session.user)
	for row in rows:
		try:
			_assert_preview_fresh(row)
			if row.get("confidence") != CONFIDENCE_EXACT:
				raise frappe.ValidationError("Auto-repair requires EXACT confidence")
			opt = row.get("optimizer_status") or row.get("status")
			if opt in (STATUS_MIDNIGHT, STATUS_MIDNIGHT_REVIEW):
				raise frappe.ValidationError("Midnight boundary requires manual review")
			if opt in (STATUS_INSUFFICIENT_STOCK, STATUS_REAL_STOCK_SHORTAGE):
				raise frappe.ValidationError("Real insufficient stock; timestamp change refused")
			if not row.get("eligible") and row.get("status") != STATUS_ELIGIBLE:
				raise frappe.ValidationError(f"Row not eligible ({row.get('status')})")
			moves = _row_moves(row)
			if not moves:
				raise frappe.ValidationError("No timestamp moves in preview")

			# poison check before any write
			from_dt = get_datetime(row["current_inbound_time"])
			identities = set(_voucher_item_warehouses(row["inbound_document"])) | set(
				_voucher_item_warehouses(row["outbound_document"])
			)
			for doc_move in moves:
				identities |= set(_voucher_item_warehouses(doc_move["document"]))
			for item_code, warehouse in identities:
				poison = window_poison_reason(item_code, warehouse, from_dt)
				if poison:
					raise frappe.ValidationError(
						f"{STATUS_VALUATION_POISON}: {item_code} {warehouse} ({poison})"
					)

			# revalidate quantity simulation on current ledger rows of this identity
			sles = _identity_window(
				row["item"],
				row["warehouse"],
				row.get("batch"),
				from_dt,
			)
			opening = D(row.get("opening_qty") or 0)
			if sles:
				times = {}
				for m in moves:
					times[m["document"]] = get_datetime(m["new"])
				times.setdefault(row["inbound_document"], get_datetime(row["proposed_inbound_time"] or row["current_inbound_time"]))
				times.setdefault(row["outbound_document"], get_datetime(row["proposed_outbound_time"]))
				current = simulate_running(sles, opening)
				proposed = simulate_running(sles, opening, times)
				if current["min_qty"] >= 0:
					raise frappe.ValidationError("Revalidation failed: repair no longer needed")
				if proposed["min_qty"] < 0:
					raise frappe.ValidationError("Revalidation failed: proposed order still negative")
				if proposed["final_qty"] != current["final_qty"]:
					raise frappe.ValidationError("Revalidation failed: final qty would change")
			else:
				current = proposed = {"min_qty": row.get("min_qty_before"), "final_qty": row.get("final_qty_before")}

			gl_before = {
				vn: _gl_balanced(vn)
				for vn in {row["inbound_document"], row["outbound_document"], *[m["document"] for m in moves]}
			}

			for m in moves:
				_update_posting_datetime(m["document"], get_datetime(m["new"]))

			replay_results = []
			valuation_changed = False
			for item_code, warehouse in identities:
				rep = replay_item_warehouse(item_code, warehouse, from_dt)
				if not rep.get("ok"):
					raise frappe.ValidationError(
						f"{rep.get('status')}: {item_code} {warehouse} ({rep.get('reason')})"
					)
				replay_results.append(rep)
				if rep.get("valuation_changed"):
					valuation_changed = True

			riv_name = None
			if valuation_changed:
				row = dict(row)
				row["valuation_impact"] = "VALUATION-IMPACTING"
				row["gl_impact"] = "RIV_REQUIRED"
				riv_name = _maybe_riv(row, [m["document"] for m in moves])

			gl_after = {
				vn: _gl_balanced(vn)
				for vn in gl_before
			}
			if not valuation_changed:
				for vn, after in gl_after.items():
					if not after["balanced"]:
						raise frappe.ValidationError(f"GL unbalanced after quantity-only repair: {vn}")

			primary = moves[0]
			entry = {
				**row,
				"status": STATUS_REPAIRED,
				"old_outbound_time": primary.get("old") or row.get("current_outbound_time"),
				"new_outbound_time": primary.get("new") or row.get("proposed_outbound_time"),
				"min_qty_before": str(current.get("min_qty")),
				"min_qty_after": str(proposed.get("min_qty")),
				"riv": riv_name,
				"repair_run_id": run_id,
				"replay": replay_results,
				"valuation_changed": valuation_changed,
				"gl_before": gl_before,
				"gl_after": gl_after,
				"seconds_shifted": max((m.get("seconds") or 0) for m in moves),
			}
			_append_audit(log, entry)
			applied.append(entry)
		except Exception as exc:
			msg = str(exc)
			status = STATUS_STALE if "preview" in msg.lower() else STATUS_BLOCKED
			if STATUS_VALUATION_POISON in msg:
				status = STATUS_VALUATION_POISON
			blocked.append({"row": row, "error": msg, "status": status})
	if log:
		log.save(ignore_permissions=True)
	return {
		"dry_run": False,
		"repair_run_id": run_id,
		"applied": applied,
		"blocked": blocked,
	}


def _identity_window(item_code, warehouse, batch_no, posting_datetime) -> list:
	conds = [
		"item_code=%s",
		"warehouse=%s",
		"is_cancelled=0",
		"posting_datetime=%s",
	]
	args = [item_code, warehouse, posting_datetime]
	rows = frappe.db.sql(
		f"""
		SELECT name, voucher_no, actual_qty, qty_after_transaction, incoming_rate,
		       valuation_rate, stock_value, stock_value_difference, posting_datetime,
		       creation, batch_no, serial_and_batch_bundle, warehouse, item_code
		FROM `tabStock Ledger Entry`
		WHERE {" AND ".join(conds)}
		ORDER BY posting_datetime, creation
		""",
		args,
		as_dict=True,
	)
	if not batch_no:
		return rows
	from erpnext_extensions.iran_accounting.stock_posting_order.batch_identity import canonical_batch_no

	wanted = str(batch_no)
	out = []
	for r in rows:
		entries = []
		if r.serial_and_batch_bundle:
			entries = frappe.db.sql(
				"SELECT batch_no FROM `tabSerial and Batch Entry` WHERE parent=%s",
				r.serial_and_batch_bundle,
				as_dict=True,
			)
		canon = canonical_batch_no(r, entries) or ""
		if canon == wanted or (not canon and not wanted):
			out.append(r)
	return out


def dry_run_selected(rows: list[dict]) -> dict:
	return {"dry_run": True, "status": STATUS_DRY_RUN, "rows": rows, "count": len(rows)}


def _new_audit_log(run_id: str, user: str):
	if not frappe.db.exists("DocType", "Production Posting Order Repair Log"):
		return None
	doc = frappe.new_doc("Production Posting Order Repair Log")
	doc.repair_run_id = run_id
	doc.repaired_by = user
	doc.repaired_on = now_datetime()
	return doc


def _append_audit(log, entry: dict) -> None:
	if not log:
		return
	payload = {
		"inbound_document": entry.get("inbound_document"),
		"outbound_document": entry.get("outbound_document"),
		"item": entry.get("item"),
		"warehouse": entry.get("warehouse"),
		"batch_no": entry.get("batch"),
		"sabb": entry.get("sabb_outbound") or entry.get("sabb_inbound"),
		"old_datetime": entry.get("old_outbound_time") or entry.get("current_outbound_time"),
		"new_datetime": entry.get("new_outbound_time") or entry.get("proposed_outbound_time"),
		"dependency_reason": entry.get("dependency_reason"),
		"min_qty_before": flt(entry.get("min_qty_before")),
		"min_qty_after": flt(entry.get("min_qty_after")),
		"valuation_impact": entry.get("valuation_impact"),
		"gl_impact": entry.get("gl_impact"),
		"confidence": entry.get("confidence"),
		"status": entry.get("status"),
	}
	meta = getattr(log, "meta", None)
	if meta and meta.get_field("entries"):
		child = frappe.get_meta("Production Posting Order Repair Entry")
		if child and child.get_field("seconds_shifted"):
			payload["seconds_shifted"] = flt(entry.get("seconds_shifted"))
	log.append("entries", payload)
