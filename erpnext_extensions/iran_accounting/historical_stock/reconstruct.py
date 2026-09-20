# Copyright (c) 2026, ERPNext Extensions contributors
"""Apply EXACT Stock Entry rate reconstruction, then SLE incoming_rate + replay."""

from __future__ import annotations

import frappe
from frappe.utils import flt, now_datetime

from erpnext_extensions.iran_accounting.historical_stock import (
	HISTORICAL_REPAIR_FLAG,
	STATUS_BLOCKED,
	STATUS_RECONSTRUCTABLE,
	STATUS_REPAIRED,
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
				from erpnext_extensions.iran_accounting.historical_stock import STATUS_RECONSTRUCTABLE
				from erpnext_extensions.iran_accounting.historical_stock.planner import (
					_patient_name,
					_rate_patient_cleared,
					assert_ready,
				)

				# Circular earliest-root election is a planner stamp; re-classify alone
				# would re-block as DEPENDENCY_REPAIR_REQUIRED without the peer row.
				if str(raw.get("dependency") or raw.get("blocked_because") or "") == "circular_earliest_root":
					pz = raw.get("patient_zero") if isinstance(raw.get("patient_zero"), dict) else {}
					merged["patient_zero"] = {
						"voucher_no": merged.get("voucher"),
						"posting_datetime": (
							(pz or {}).get("posting_datetime")
							or merged.get("posting_datetime")
							or f"{merged.get('posting_date') or ''} {merged.get('posting_time') or ''}".strip()
						),
						"item_code": merged.get("item"),
						"warehouse": merged.get("warehouse"),
						"batch": merged.get("batch"),
						"reason": "circular_earliest_root",
					}
					merged["status"] = STATUS_RECONSTRUCTABLE
					merged["eligible"] = True
					merged["dependency"] = "circular_earliest_root"
					merged["confidence"] = merged.get("confidence") or raw.get("confidence")
				voucher = merged.get("voucher")
				cache = {"rows_by_voucher": {voucher: merged}} if voucher else {}
				# Ensure already-valued patient-zeros are visible to assert_ready.
				pz_name = _patient_name(merged)
				if pz_name and pz_name != voucher:
					_ensure_pz_row_in_cache(pz_name, cache)
					if _rate_patient_cleared(pz_name, cache, row=merged):
						merged["patient_zero"] = {
							"voucher_no": voucher,
							"posting_datetime": merged.get("posting_datetime")
							or f"{merged.get('posting_date') or ''} {merged.get('posting_time') or ''}".strip(),
							"reason": "patient_already_valued",
						}
						merged["status"] = STATUS_RECONSTRUCTABLE
						merged["eligible"] = True
				assert_ready(merged, cache=cache)
				patient = merged.get("patient_zero") or {}
				if isinstance(patient, str):
					patient = {"voucher_no": patient}
				classified_rows.append((raw, merged, patient))
			except Exception as exc:
				blocked.append({"row": raw, "error": str(exc), "status": STATUS_BLOCKED})
		if blocked and not dry_run:
			finish_run(log, applied=0, blocked=len(blocked), error="aborted: no partial commits")
			closed = True
			reason = blocked[0].get("error") or "ambiguity, poison, or ineligible row in selection"
			return {
				"dry_run": False,
				"aborted": True,
				"reason": reason,
				"skip_reason": reason,
				"sql_updates_planned": 0,
				"sql_updates_executed": 0,
				"savepoint_created": False,
				"transaction_committed": False,
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
	written = [a for a in applied if a.get("written")]
	return {
		"dry_run": dry_run,
		"repair_run_id": getattr(log, "repair_run_id", None),
		"applied": applied,
		"blocked": blocked,
		"database_backup_recommended": True,
		"aborted": False,
		"sql_updates_executed": 0 if dry_run else len(written),
		"savepoint_created": bool(savepoint) and not dry_run,
		"transaction_committed": bool(written) and not dry_run,
		"skip_reason": None if written or dry_run else "No SQL updates. Nothing was written.",
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
	if result.get("aborted"):
		return result
	from erpnext_extensions.iran_accounting.historical_stock.planner import assert_ready
	from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
		sync_sabb_from_sle,
		write_sle_transaction_rates,
	)

	for r in sle_rows:
		try:
			assert_ready(r)
		except Exception as exc:
			result.setdefault("blocked", []).append({"row": r, "error": str(exc), "status": STATUS_BLOCKED})
			continue
		write_sle_transaction_rates(r.get("voucher"), r.get("item"))
		sync_sabb_from_sle(r.get("voucher"), r.get("item"))
		result.setdefault("applied", []).append({**r, "written": True, "status": STATUS_REPAIRED})
	return result


def repair_manufacture_selected(rows: list[dict], *, dry_run=True) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock import CONFIDENCE_EXACT

	applied = []
	blocked = []
	log = start_run("MANUFACTURE", dry_run=dry_run)
	frappe.flags[HISTORICAL_REPAIR_FLAG] = True
	try:
		for raw in rows or []:
			vn = raw.get("voucher") or raw.get("voucher_no")
			try:
				preview = preview_manufacture_voucher(vn)
				from erpnext_extensions.iran_accounting.historical_stock.planner import assert_ready
				from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
					sync_sabb_from_sle,
					sync_sle_from_stock_entry_detail,
					write_sle_transaction_rates,
				)

				sle_drift = _manufacture_sle_disagrees_with_se(vn)
				se_healthy = preview.get("status") == "HEALTHY" or (
					not preview.get("needs_repair") and preview.get("confidence") == CONFIDENCE_EXACT
				)
				if se_healthy and not sle_drift:
					applied.append({**preview, "written": False, "status": "ALREADY_HEALTHY"})
					append_entry(log, preview, written=False)
					continue
				if se_healthy and sle_drift:
					preview = {
						**preview,
						"needs_repair": True,
						"status": STATUS_RECONSTRUCTABLE,
						"confidence": CONFIDENCE_EXACT,
						"eligible": True,
						"sle_se_drift": True,
						"source_of_truth": "5.3.0_se_to_sle_sync",
					}
				assert_ready(preview)
				if dry_run:
					applied.append({**preview, "written": False, "sle_se_drift": sle_drift})
					append_entry(log, preview, written=False)
					continue
				savepoint = f"mfg_{frappe.generate_hash(length=8)}"
				frappe.db.savepoint(savepoint)
				try:
					doc = frappe.get_doc("Stock Entry", vn)
					if not preview.get("sle_se_drift"):
						apply_manufacture_preview_to_doc(doc)
						from erpnext_extensions.iran_accounting.stock_entry import (
							persist_irr_stock_entry_header_and_rows,
						)

						persist_irr_stock_entry_header_and_rows(doc)

					targets = []
					for change in preview.get("changed_rows") or []:
						after = (change or {}).get("after") or {}
						item_code = after.get("item_code") or change.get("item")
						wh = after.get("t_warehouse") or after.get("s_warehouse")
						batch = after.get("batch_no")
						if item_code and wh:
							targets.append((item_code, wh, batch))
					if not targets:
						for d in doc.items or []:
							if d.is_finished_item or getattr(d, "secondary_item_type", None) == "Scrap" or d.t_warehouse:
								if d.item_code and (d.t_warehouse or d.s_warehouse):
									targets.append(
										(d.item_code, d.t_warehouse or d.s_warehouse, d.batch_no)
									)

					sle_sync = sync_sle_from_stock_entry_detail(vn)
					replayed = []
					seen = set()
					for item_code, wh, batch in targets:
						key = (item_code, wh, batch or "")
						if key in seen:
							continue
						seen.add(key)
						neg_before = _count_neg_valuation(item_code, wh)
						replay = replay_from_patient_zero(item_code, wh, batch, from_dt=doc.posting_date)
						sync_sle_from_stock_entry_detail(vn, item_code)
						write_sle_transaction_rates(vn, item_code)
						sync_sabb_from_sle(vn, item_code)
						neg_after = _count_neg_valuation(item_code, wh)
						if neg_after > neg_before:
							raise frappe.ValidationError(
								f"Manufacture replay introduced negative valuation_rate "
								f"on {item_code} / {wh} (before={neg_before}, after={neg_after}). "
								"Apply aborted — identity must be healed before retry."
							)
						replayed.append(
							{"item": item_code, "warehouse": wh, "batch": batch, "replay": replay}
						)
					applied.append(
						{
							**preview,
							"written": True,
							"status": STATUS_REPAIRED,
							"sle_synced": len(sle_sync),
							"replay": replayed,
							"replay_identities": len(replayed),
						}
					)
					append_entry(log, preview, written=True)
				except Exception:
					frappe.db.rollback(save_point=savepoint)
					raise
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


def _count_neg_valuation(item_code, warehouse) -> int:
	if not item_code or not warehouse:
		return 0
	return int(
		frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			  AND valuation_rate < -0.0001
			""",
			(item_code, warehouse),
		)[0][0]
	)


def _manufacture_sle_disagrees_with_se(voucher_no: str) -> bool:
	"""True when any inbound SLE rate materially disagrees with SE Detail amount/qty."""
	from erpnext_extensions.iran_accounting.historical_stock import RATE_EPS

	rows = frappe.db.sql(
		"""
		SELECT sle.actual_qty, sle.incoming_rate, sle.outgoing_rate,
		       sed.amount, sed.basic_amount, sed.qty, sed.valuation_rate, sed.basic_rate
		FROM `tabStock Ledger Entry` sle
		JOIN `tabStock Entry Detail` sed ON sed.name = sle.voucher_detail_no
		WHERE sle.voucher_type='Stock Entry' AND sle.voucher_no=%s AND sle.is_cancelled=0
		  AND ABS(sle.actual_qty) > 0
		""",
		(voucher_no,),
		as_dict=True,
	)
	for r in rows:
		qty = abs(flt(r.qty) or flt(r.actual_qty))
		if qty <= 0:
			continue
		amount = abs(flt(r.amount if r.amount is not None else r.basic_amount))
		expected = amount / qty if amount else abs(flt(r.valuation_rate or r.basic_rate))
		current = abs(flt(r.incoming_rate if flt(r.actual_qty) > 0 else r.outgoing_rate))
		if abs(expected - current) > max(1.0, RATE_EPS):
			return True
	return False


def _ensure_pz_row_in_cache(pz_voucher: str, cache: dict) -> None:
	"""Load a patient-zero voucher into rows_by_voucher for clearance checks."""
	by_v = cache.setdefault("rows_by_voucher", {})
	if not pz_voucher or pz_voucher in by_v:
		return
	try:
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import _fetch_se_details
		from erpnext_extensions.iran_accounting.historical_stock.zero_rate import classify_zero_row

		raws = _fetch_se_details(pz_voucher, limit=5)
		if not raws:
			by_v[pz_voucher] = {"voucher": pz_voucher, "status": "UNKNOWN"}
			return
		row = classify_zero_row(raws[0])
		row["source"] = row.get("source_of_truth")
		by_v[pz_voucher] = row
	except Exception:
		by_v[pz_voucher] = {"voucher": pz_voucher, "status": "UNKNOWN"}


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
