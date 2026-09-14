# Copyright (c) 2026, ERPNext Extensions contributors
"""Apply EXACT Stock Entry rate reconstruction, then SLE incoming_rate + replay."""

from __future__ import annotations

import frappe
from frappe.utils import flt, now_datetime

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_EXACT,
	HISTORICAL_REPAIR_FLAG,
	STATUS_BLOCKED,
	STATUS_DEPENDENCY_REPAIR_REQUIRED,
	STATUS_REPAIRED,
	STATUS_VALUATION_POISON_DEPENDENCY,
)
from erpnext_extensions.iran_accounting.historical_stock.audit import append_entry, finish_run, start_run
from erpnext_extensions.iran_accounting.historical_stock.manufacture import (
	apply_manufacture_preview_to_doc,
	preview_manufacture_voucher,
)
from erpnext_extensions.iran_accounting.historical_stock.replay import replay_from_patient_zero
from erpnext_extensions.iran_accounting.historical_stock.zero_rate import classify_zero_row


def repair_zero_rate_selected(rows: list[dict], *, dry_run=True) -> dict:
	applied = []
	blocked = []
	classified_rows = []
	log = start_run("ZERO_RATE", dry_run=dry_run)
	frappe.flags[HISTORICAL_REPAIR_FLAG] = True
	savepoint = None
	closed = False
	try:
		for raw in rows or []:
			try:
				classified = classify_zero_row(_load_detail(raw))
				merged = {**classified, **{k: v for k, v in raw.items() if v not in (None, "")}}
				if merged.get("status") == STATUS_VALUATION_POISON_DEPENDENCY:
					raise frappe.ValidationError("abort on poison dependency")
				if merged.get("confidence") != CONFIDENCE_EXACT:
					raise frappe.ValidationError("AMBIGUOUS/LIKELY rows cannot auto-write")
				if not merged.get("eligible"):
					raise frappe.ValidationError(merged.get("status") or STATUS_BLOCKED)
				patient = merged.get("patient_zero") or {}
				if patient.get("voucher_no") and patient["voucher_no"] != merged["voucher"]:
					raise frappe.ValidationError(
						f"{STATUS_DEPENDENCY_REPAIR_REQUIRED}: patient-zero is {patient['voucher_no']}"
					)
				classified_rows.append((raw, merged, patient))
			except Exception as exc:
				blocked.append({"row": raw, "error": str(exc), "status": STATUS_BLOCKED})
		if blocked and not dry_run:
			finish_run(log, applied=0, blocked=len(blocked), error="aborted: no partial commits")
			closed = True
			return {
				"dry_run": False,
				"aborted": True,
				"reason": "ambiguity, poison, or ineligible row in selection",
				"database_backup_recommended": True,
				"applied": [],
				"blocked": blocked,
				"repair_run_id": getattr(log, "repair_run_id", None),
			}
		if not dry_run:
			savepoint = f"hsr_{frappe.generate_hash(length=8)}"
			frappe.db.savepoint(savepoint)
		from erpnext_extensions.iran_accounting.historical_stock.snapshot import capture_identity_snapshot

		for raw, merged, patient in classified_rows:
			if dry_run:
				applied.append({**merged, "written": False, "status": "DRY_RUN", "database_backup_recommended": True})
				append_entry(log, merged, written=False)
				continue
			snap = capture_identity_snapshot(merged["voucher"], merged.get("item"), merged.get("warehouse"), merged.get("batch"))
			merged["snapshot_before"] = snap
			merged["full_rollback_possible"] = snap.get("full_rollback_possible")
			_write_se_row(merged)
			_write_sle_incoming(merged)
			replay = replay_from_patient_zero(
				merged["item"],
				merged["warehouse"],
				merged.get("batch"),
				from_dt=patient.get("posting_datetime"),
			)
			from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
				sync_sabb_from_sle,
				write_sle_transaction_rates,
			)

			write_sle_transaction_rates(merged["voucher"], merged["item"])
			sync_sabb_from_sle(merged["voucher"], merged["item"])
			applied.append({**merged, "written": True, "status": STATUS_REPAIRED, "replay": replay})
			append_entry(log, {**merged, "replay": replay, "snapshot_before": snap}, written=True)
	except Exception:
		if savepoint:
			frappe.db.rollback(save_point=savepoint)
		raise
	finally:
		frappe.flags[HISTORICAL_REPAIR_FLAG] = False
		if not closed:
			finish_run(log, applied=len(applied), blocked=len(blocked))
	return {
		"dry_run": dry_run,
		"repair_run_id": getattr(log, "repair_run_id", None),
		"applied": applied,
		"blocked": blocked,
		"database_backup_recommended": True,
		"aborted": False,
	}


def repair_wrong_rate_selected(rows: list[dict], *, dry_run=True) -> dict:
	"""Repair EXACT wrong/zero rates. SLE-only implied SVD uses SABB/SLE writers."""
	se_rows = [r for r in rows or [] if r.get("voucher_detail") or r.get("surface") != "SLE"]
	sle_rows = [r for r in rows or [] if r.get("surface") == "SLE"]
	result = (
		repair_zero_rate_selected(se_rows, dry_run=dry_run)
		if se_rows
		else {"dry_run": dry_run, "applied": [], "blocked": []}
	)
	if dry_run:
		result["sle_preview"] = sle_rows
		return result
	from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
		sync_sabb_from_sle,
		write_sle_transaction_rates,
	)

	for r in sle_rows:
		if r.get("confidence") != CONFIDENCE_EXACT or not r.get("eligible"):
			result.setdefault("blocked", []).append({"row": r, "error": "not EXACT", "status": STATUS_BLOCKED})
			continue
		write_sle_transaction_rates(r.get("voucher"), r.get("item"))
		sync_sabb_from_sle(r.get("voucher"), r.get("item"))
		result.setdefault("applied", []).append({**r, "written": True, "status": STATUS_REPAIRED})
	return result


def repair_manufacture_selected(rows: list[dict], *, dry_run=True) -> dict:
	applied = []
	blocked = []
	log = start_run("MANUFACTURE", dry_run=dry_run)
	frappe.flags[HISTORICAL_REPAIR_FLAG] = True
	try:
		for raw in rows or []:
			vn = raw.get("voucher") or raw.get("voucher_no")
			try:
				preview = preview_manufacture_voucher(vn)
				if not preview.get("eligible"):
					raise frappe.ValidationError(preview.get("status") or STATUS_BLOCKED)
				if dry_run:
					applied.append({**preview, "written": False})
					append_entry(log, preview, written=False)
					continue
				doc = frappe.get_doc("Stock Entry", vn)
				apply_manufacture_preview_to_doc(doc)
				from erpnext_extensions.iran_accounting.stock_entry import persist_irr_stock_entry_header_and_rows

				persist_irr_stock_entry_header_and_rows(doc)
				item = (doc.items or [None])[0]
				wh = item.t_warehouse or item.s_warehouse if item else None
				replay = None
				if item and wh:
					replay = replay_from_patient_zero(item.item_code, wh, item.batch_no, from_dt=doc.posting_date)
					from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
						sync_sabb_from_sle,
						write_sle_transaction_rates,
					)

					write_sle_transaction_rates(vn, item.item_code)
					sync_sabb_from_sle(vn, item.item_code)
				applied.append({**preview, "written": True, "status": STATUS_REPAIRED, "replay": replay})
				append_entry(log, preview, written=True)
			except Exception as exc:
				blocked.append({"row": raw, "error": str(exc), "status": STATUS_BLOCKED})
	finally:
		frappe.flags[HISTORICAL_REPAIR_FLAG] = False
		finish_run(log, applied=len(applied), blocked=len(blocked))
	return {
		"dry_run": dry_run,
		"repair_run_id": getattr(log, "repair_run_id", None),
		"applied": applied,
		"blocked": blocked,
	}


def _load_detail(raw) -> dict:
	name = raw.get("voucher_detail") or raw.get("name")
	parent = raw.get("voucher") or raw.get("parent")
	if name:
		row = frappe.db.sql(
			"""
			SELECT sed.*, se.purpose, se.posting_date, se.posting_time, se.company,
			       se.work_order, se.job_card
			FROM `tabStock Entry Detail` sed
			JOIN `tabStock Entry` se ON se.name = sed.parent
			WHERE sed.name=%s
			""",
			name,
			as_dict=True,
		)
		if row:
			return row[0]
	if parent and raw.get("idx"):
		row = frappe.db.sql(
			"""
			SELECT sed.*, se.purpose, se.posting_date, se.posting_time, se.company,
			       se.work_order, se.job_card
			FROM `tabStock Entry Detail` sed
			JOIN `tabStock Entry` se ON se.name = sed.parent
			WHERE sed.parent=%s AND sed.idx=%s
			""",
			(parent, raw["idx"]),
			as_dict=True,
		)
		if row:
			return row[0]
	frappe.throw("Stock Entry Detail identity is required")


def _write_se_row(row: dict) -> None:
	rate = flt(row["proposed_rate"])
	qty = flt(row.get("qty") or 0)
	amount = flt(row.get("proposed_amount") or rate * qty)
	frappe.db.set_value(
		"Stock Entry Detail",
		row["voucher_detail"],
		{
			"basic_rate": rate,
			"valuation_rate": rate,
			"basic_amount": amount,
			"amount": amount,
		},
		update_modified=False,
	)
	# Refresh parent totals from remaining rows.
	parent = row["voucher"]
	totals = frappe.db.sql(
		"""
		SELECT
		  SUM(CASE WHEN t_warehouse IS NOT NULL AND t_warehouse != '' THEN amount ELSE 0 END) incoming,
		  SUM(CASE WHEN s_warehouse IS NOT NULL AND s_warehouse != '' THEN amount ELSE 0 END) outgoing
		FROM `tabStock Entry Detail` WHERE parent=%s
		""",
		parent,
		as_dict=True,
	)[0]
	frappe.db.set_value(
		"Stock Entry",
		parent,
		{
			"total_incoming_value": flt(totals.incoming),
			"total_outgoing_value": flt(totals.outgoing),
			"value_difference": flt(totals.incoming) - flt(totals.outgoing),
		},
		update_modified=False,
	)


def _write_sle_incoming(row: dict) -> None:
	rate = flt(row["proposed_rate"])
	qty = flt(row.get("qty") or 0)
	sles = frappe.db.sql(
		"""
		SELECT name, actual_qty, voucher_detail_no
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND item_code=%s AND is_cancelled=0
		""",
		(row["voucher"], row["item"]),
		as_dict=True,
	)
	for sle in sles:
		if row.get("voucher_detail") and sle.voucher_detail_no not in (
			row["voucher_detail"],
			None,
			"",
		):
			# Still update both legs of a transfer for this item.
			pass
		qty_signed = flt(sle.actual_qty)
		svd = rate * qty_signed
		values = {
			"incoming_rate": rate,
			"stock_value_difference": svd,
		}
		if qty_signed < 0 and "outgoing_rate" in frappe.db.get_table_columns("Stock Ledger Entry"):
			values["outgoing_rate"] = rate
		frappe.db.set_value("Stock Ledger Entry", sle.name, values, update_modified=False)
	if row.get("sabb"):
		frappe.db.set_value(
			"Serial and Batch Bundle",
			row["sabb"],
			{"avg_rate": rate, "total_amount": rate * abs(qty)},
			update_modified=False,
		)
		frappe.db.sql(
			"""
			UPDATE `tabSerial and Batch Entry`
			SET incoming_rate=%s
			WHERE parent=%s
			""",
			(rate, row["sabb"]),
		)
