# Copyright (c) 2026, ERPNext Extensions contributors
"""Detect, preview, and apply production posting-order repairs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

import frappe
from frappe.utils import cint, flt, get_datetime, now_datetime

from erpnext_extensions.iran_accounting.stock_posting_order import (
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	MINIMUM_DEPENDENT_SECONDS,
	PRODUCTION_PURPOSES,
	STATUS_BLOCKED,
	STATUS_CYCLE,
	STATUS_DRAFT,
	STATUS_DRY_RUN,
	STATUS_ELIGIBLE,
	STATUS_INSUFFICIENT_STOCK,
	STATUS_MIDNIGHT,
	STATUS_NO_QTY_DEFICIT,
	STATUS_REPAIRED,
	STATUS_STALE,
)
from erpnext_extensions.iran_accounting.stock_posting_order.dag import assign_minimum_offsets
from erpnext_extensions.iran_accounting.stock_posting_order.dependency import classify_edge
from erpnext_extensions.iran_accounting.stock_posting_order.ordering import (
	combine_posting,
	format_date,
	format_datetime,
	format_time,
)
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import (
	classify_valuation_impact,
	order_sles,
	simulate_repair,
)

SCAN_LIMIT = 400


def _signature(payload: dict) -> str:
	blob = json.dumps(payload, sort_keys=True, default=str)
	return hashlib.sha256(blob.encode("utf-8")).hexdigest()


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
) -> list[dict]:
	"""Find same-datetime inbound/outbound production SLE pairs."""
	conditions = [
		"a.is_cancelled=0",
		"b.is_cancelled=0",
		"a.voucher_type='Stock Entry'",
		"b.voucher_type='Stock Entry'",
		"a.voucher_no <> b.voucher_no",
		"a.actual_qty > 0",
		"b.actual_qty < 0",
		"a.item_code = b.item_code",
		"a.warehouse = b.warehouse",
		"a.posting_datetime = b.posting_datetime",
		"IFNULL(a.batch_no,'') = IFNULL(b.batch_no,'')",
		"se_in.docstatus = 1",
		"se_out.docstatus = 1",
		"se_in.purpose IN %(purposes)s",
		"se_out.purpose IN %(purposes)s",
	]
	params: dict = {"purposes": PRODUCTION_PURPOSES}
	if company:
		conditions.append("se_in.company = %(company)s")
		params["company"] = company
	if from_date:
		conditions.append("a.posting_date >= %(from_date)s")
		params["from_date"] = from_date
	if to_date:
		conditions.append("a.posting_date <= %(to_date)s")
		params["to_date"] = to_date

	sql = f"""
		SELECT
			a.item_code, a.warehouse, a.batch_no, a.posting_datetime,
			a.voucher_no inbound_voucher, b.voucher_no outbound_voucher,
			a.name inbound_sle, b.name outbound_sle,
			a.actual_qty inbound_qty, b.actual_qty outbound_qty,
			a.qty_after_transaction inbound_after, b.qty_after_transaction outbound_after,
			a.incoming_rate inbound_rate, b.incoming_rate outbound_rate,
			a.valuation_rate inbound_valuation, b.valuation_rate outbound_valuation,
			a.creation inbound_creation, b.creation outbound_creation,
			a.serial_and_batch_bundle inbound_sabb, b.serial_and_batch_bundle outbound_sabb,
			se_in.purpose inbound_purpose, se_out.purpose outbound_purpose,
			se_in.work_order inbound_wo, se_out.work_order outbound_wo,
			se_in.job_card inbound_jc, se_out.job_card outbound_jc,
			se_in.company, se_in.modified inbound_modified, se_out.modified outbound_modified,
			se_in.posting_date inbound_date, se_in.posting_time inbound_time,
			se_out.posting_date outbound_date, se_out.posting_time outbound_time
		FROM `tabStock Ledger Entry` a
		INNER JOIN `tabStock Ledger Entry` b
			ON a.item_code = b.item_code
			AND a.warehouse = b.warehouse
			AND a.posting_datetime = b.posting_datetime
			AND IFNULL(a.batch_no,'') = IFNULL(b.batch_no,'')
		INNER JOIN `tabStock Entry` se_in ON se_in.name = a.voucher_no
		INNER JOIN `tabStock Entry` se_out ON se_out.name = b.voucher_no
		WHERE {" AND ".join(conditions)}
		ORDER BY a.posting_datetime DESC
		LIMIT {int(SCAN_LIMIT)}
	"""
	rows = frappe.db.sql(sql, params, as_dict=True)
	out = []
	seen = set()
	for r in rows:
		key = (r.inbound_voucher, r.outbound_voucher, r.item_code, r.warehouse, r.batch_no or "")
		if key in seen:
			continue
		seen.add(key)
		against = bool(
			frappe.db.exists(
				"Stock Entry Detail",
				{"parent": r.outbound_voucher, "against_stock_entry": r.inbound_voucher},
			)
		)
		same_batch = bool((r.batch_no or "").strip())
		confidence, reason = classify_edge(
			{
				"item_code": r.item_code,
				"warehouse": r.warehouse,
				"batch_no": r.batch_no,
			},
			{
				"item_code": r.item_code,
				"warehouse": r.warehouse,
				"batch_no": r.batch_no,
			},
			same_work_order=bool(r.inbound_wo and r.inbound_wo == r.outbound_wo),
			same_job_card=bool(r.inbound_jc and r.inbound_jc == r.outbound_jc),
			against_stock_entry=against,
			same_batch=same_batch,
			inbound_purpose=r.inbound_purpose,
			outbound_purpose=r.outbound_purpose,
		)
		if confidence == "AMBIGUOUS":
			continue
		if confidence == CONFIDENCE_LIKELY and not include_likely:
			continue
		out.append(_build_candidate(r, confidence, reason, against))
	return out


def _ledger_window(item_code, warehouse, batch_no, posting_datetime) -> list:
	conds = [
		"item_code=%s",
		"warehouse=%s",
		"is_cancelled=0",
		"posting_datetime=%s",
	]
	args = [item_code, warehouse, posting_datetime]
	if batch_no:
		conds.append("IFNULL(batch_no,'')=%s")
		args.append(batch_no)
	return frappe.db.sql(
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


def _build_candidate(r, confidence, reason, against) -> dict:
	sles = _ledger_window(r.item_code, r.warehouse, r.batch_no, r.posting_datetime)
	inbound_dt = combine_posting(r.inbound_date, r.inbound_time)
	plan = assign_minimum_offsets(
		{
			r.inbound_voucher: inbound_dt,
			r.outbound_voucher: combine_posting(r.outbound_date, r.outbound_time),
		},
		[(r.inbound_voucher, r.outbound_voucher)],
		stock_key_of={
			r.inbound_voucher: (r.item_code, r.warehouse, r.batch_no or ""),
			r.outbound_voucher: (r.item_code, r.warehouse, r.batch_no or ""),
		},
		minimum_seconds=MINIMUM_DEPENDENT_SECONDS,
	)
	proposed_times = plan.get("proposed") or {}
	sim = simulate_repair(sles, proposed_times)
	val_impact = classify_valuation_impact(
		order_sles(sles),
		order_sles(sles, proposed_times=proposed_times),
	)
	status = STATUS_ELIGIBLE
	if plan.get("status") == STATUS_CYCLE:
		status = STATUS_CYCLE
	elif plan.get("status") == STATUS_MIDNIGHT:
		status = STATUS_MIDNIGHT
	elif sim.get("insufficient_stock"):
		status = STATUS_INSUFFICIENT_STOCK
	elif sim.get("no_qty_deficit"):
		status = STATUS_NO_QTY_DEFICIT
	elif not sim.get("eligible"):
		status = STATUS_BLOCKED
	if confidence != CONFIDENCE_EXACT and status == STATUS_ELIGIBLE:
		status = STATUS_NO_QTY_DEFICIT if sim.get("no_qty_deficit") else "MANUAL_APPROVAL"

	gl_in = _gl_balanced(r.inbound_voucher)
	gl_out = _gl_balanced(r.outbound_voucher)
	gl_impact = "NONE"
	if val_impact == "VALUATION-IMPACTING":
		gl_impact = "RIV_REQUIRED"

	payload = {
		"inbound_document": r.inbound_voucher,
		"outbound_document": r.outbound_voucher,
		"inbound_purpose": r.inbound_purpose,
		"outbound_purpose": r.outbound_purpose,
		"item": r.item_code,
		"warehouse": r.warehouse,
		"batch": r.batch_no,
		"sabb_inbound": r.inbound_sabb,
		"sabb_outbound": r.outbound_sabb,
		"current_inbound_time": format_datetime(inbound_dt),
		"current_outbound_time": format_datetime(combine_posting(r.outbound_date, r.outbound_time)),
		"proposed_outbound_time": proposed_times.get(r.outbound_voucher),
		"proposed_inbound_time": proposed_times.get(r.inbound_voucher),
		"min_qty_before": str(sim["current"]["min_qty"]),
		"min_qty_after": str(sim["proposed"]["min_qty"]),
		"final_qty_before": str(sim["current"]["final_qty"]),
		"final_qty_after": str(sim["proposed"]["final_qty"]),
		"valuation_impact": val_impact,
		"gl_impact": gl_impact,
		"gl_inbound_balanced": gl_in["balanced"],
		"gl_outbound_balanced": gl_out["balanced"],
		"confidence": confidence,
		"dependency_reason": reason,
		"against_stock_entry": against,
		"status": status,
		"inbound_modified": str(r.inbound_modified),
		"outbound_modified": str(r.outbound_modified),
		"work_order": r.inbound_wo or r.outbound_wo,
		"inbound_job_card": r.inbound_jc,
		"outbound_job_card": r.outbound_jc,
		"company": r.company,
		"current_series": sim["current"]["series"],
		"proposed_series": sim["proposed"]["series"],
		"eligible": status == STATUS_ELIGIBLE and confidence == CONFIDENCE_EXACT,
	}
	payload["dependency_signature"] = _signature(
		{
			"in": payload["inbound_document"],
			"out": payload["outbound_document"],
			"item": payload["item"],
			"wh": payload["warehouse"],
			"batch": payload["batch"],
			"in_time": payload["current_inbound_time"],
			"out_time": payload["current_outbound_time"],
			"in_mod": payload["inbound_modified"],
			"out_mod": payload["outbound_modified"],
			"reason": reason,
		}
	)
	payload["chain"] = f"{payload['inbound_document']} → {payload['outbound_document']}"
	return payload


def dry_run(candidates: list[dict] | None = None, **scan_kwargs) -> dict:
	rows = candidates if candidates is not None else scan_production_posting_order_anomalies(**scan_kwargs)
	return {
		"dry_run": True,
		"status": STATUS_DRY_RUN,
		"count": len(rows),
		"eligible": [r for r in rows if r.get("eligible")],
		"rows": rows,
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
		status = STATUS_CANCELLED if 2 in (in_ds, out_ds) else STATUS_DRAFT
		frappe.throw(f"Repair blocked ({status}): documents must be submitted.", title=status)


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


def _replay_qty_after(item_code, warehouse, batch_no, from_dt) -> None:
	conds = ["item_code=%s", "warehouse=%s", "is_cancelled=0", "posting_datetime >= %s"]
	args = [item_code, warehouse, from_dt]
	if batch_no:
		conds.append("IFNULL(batch_no,'')=%s")
		args.append(batch_no)
	prev = frappe.db.sql(
		f"""
		SELECT qty_after_transaction, actual_qty
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND posting_datetime < %s
		  {"AND IFNULL(batch_no,'')=%s" if batch_no else ""}
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 1
		""",
		tuple(args[:3] + ([batch_no] if batch_no else [])),
		as_dict=True,
	)
	running = 0.0
	if prev:
		running = flt(prev[0].qty_after_transaction)
	rows = frappe.db.sql(
		f"""
		SELECT name, actual_qty
		FROM `tabStock Ledger Entry`
		WHERE {" AND ".join(conds)}
		ORDER BY posting_datetime, creation
		""",
		args,
		as_dict=True,
	)
	for row in rows:
		running += flt(row.actual_qty)
		frappe.db.set_value(
			"Stock Ledger Entry",
			row.name,
			"qty_after_transaction",
			running,
			update_modified=False,
		)


def _maybe_riv(row: dict) -> str | None:
	if row.get("valuation_impact") != "VALUATION-IMPACTING" and row.get("gl_impact") != "RIV_REQUIRED":
		return None
	if not frappe.db.exists("DocType", "Repost Item Valuation"):
		return None
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import repost

	riv = frappe.new_doc("Repost Item Valuation")
	riv.company = row.get("company") or frappe.db.get_value(
		"Stock Entry", row["outbound_document"], "company"
	)
	riv.based_on = "Transaction"
	riv.voucher_type = "Stock Entry"
	riv.voucher_no = row["outbound_document"]
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
			if row.get("status") == STATUS_MIDNIGHT:
				raise frappe.ValidationError("Midnight boundary requires manual review")
			if row.get("status") == STATUS_INSUFFICIENT_STOCK:
				raise frappe.ValidationError("Real insufficient stock; timestamp change refused")
			if not row.get("eligible") and row.get("status") != STATUS_ELIGIBLE:
				# allow only qty-eligible exact
				if row.get("status") != STATUS_ELIGIBLE:
					raise frappe.ValidationError(f"Row not eligible ({row.get('status')})")
			# revalidate simulation
			sles = _ledger_window(row["item"], row["warehouse"], row.get("batch"), get_datetime(row["current_inbound_time"]))
			proposed_times = {
				row["inbound_document"]: row["proposed_inbound_time"] or row["current_inbound_time"],
				row["outbound_document"]: row["proposed_outbound_time"],
			}
			sim = simulate_repair(sles, proposed_times)
			if not sim["eligible"]:
				raise frappe.ValidationError("Revalidation failed: quantity simulation not eligible")
			old_out = row["current_outbound_time"]
			new_out = row["proposed_outbound_time"]
			_update_posting_datetime(row["outbound_document"], get_datetime(new_out))
			_replay_qty_after(
				row["item"],
				row["warehouse"],
				row.get("batch"),
				get_datetime(row["current_inbound_time"]),
			)
			riv_name = _maybe_riv(row)
			entry = {
				**row,
				"status": STATUS_REPAIRED,
				"old_outbound_time": old_out,
				"new_outbound_time": new_out,
				"min_qty_before": str(sim["current"]["min_qty"]),
				"min_qty_after": str(sim["proposed"]["min_qty"]),
				"riv": riv_name,
				"repair_run_id": run_id,
			}
			_append_audit(log, entry)
			applied.append(entry)
		except Exception as exc:
			blocked.append({"row": row, "error": str(exc), "status": STATUS_STALE if "preview" in str(exc).lower() else STATUS_BLOCKED})
	if log:
		log.save(ignore_permissions=True)
	return {
		"dry_run": False,
		"repair_run_id": run_id,
		"applied": applied,
		"blocked": blocked,
	}


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
	log.append(
		"entries",
		{
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
		},
	)
