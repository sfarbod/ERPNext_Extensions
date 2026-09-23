# Copyright (c) 2026, ERPNext Extensions contributors
"""I4 leftover repair — qty_after=0 with non-zero stock_value.

Reuses existing ``replay_series`` / Bin update / selective GL. Does not change
Moving Average policy. Identity-scoped only (item + warehouse).
"""

from __future__ import annotations

from collections import defaultdict
from time import perf_counter

import frappe
from frappe.utils import flt, get_datetime

from erpnext_extensions.iran_accounting.historical_stock import (
	HISTORICAL_REPAIR_FLAG,
	I4_LEFTOVER_REPAIR,
	I4_READY,
	I4_REPAIRED,
	I4_REPLAY_REQUIRED,
	I4_WAITING,
	QTY_EPS,
	STATUS_BLOCKED,
	STATUS_REPAIRED,
	TOPIC_I4,
	VALUE_EPS,
)
from erpnext_extensions.iran_accounting.historical_stock.audit import append_entry, finish_run, start_run
from erpnext_extensions.iran_accounting.historical_stock.patient_zero import find_patient_zero_identity
from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
	_fetch_previous,
	_fetch_sles,
	_update_bin,
	replay_series,
	sle_poison_reason,
)
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D

HARD_STOP_POISONS = (
	"negative_incoming_rate",
	"sign_inverted_incoming_svd",
	"sign_inverted_outgoing_svd",
	"exploded_rate",
)
I4_POISONS = ("qty_after_zero_nonzero_value", "qty_zero_nonzero_value")


def is_i4_leftover_row(row) -> bool:
	reason = sle_poison_reason(row) if not isinstance(row, dict) or "actual_qty" in row else row.get("reason")
	if reason in I4_POISONS:
		return True
	after = abs(flt(row.get("qty_after_transaction") if hasattr(row, "get") else getattr(row, "qty_after_transaction", 0)))
	value = abs(flt(row.get("stock_value") if hasattr(row, "get") else getattr(row, "stock_value", 0)))
	return after <= QTY_EPS and value > 1


def classify_i4_row(item, warehouse, voucher=None, sle_name=None) -> dict:
	"""Build a planner-ready I4 row for one identity (optional voucher focus)."""
	# Generic poison PZ (may be non-I4). I4 readiness uses the earliest remaining
	# qty_after≈0 / stock_value≠0 leftover on the identity.
	patient = find_patient_zero_identity(item, warehouse)
	i4_pz = _earliest_i4_leftover(item, warehouse)
	sle = None
	if sle_name:
		sle = frappe.db.get_value(
			"Stock Ledger Entry",
			sle_name,
			[
				"name",
				"voucher_no",
				"item_code",
				"warehouse",
				"actual_qty",
				"qty_after_transaction",
				"stock_value",
				"stock_value_difference",
				"valuation_rate",
				"incoming_rate",
				"outgoing_rate",
				"posting_datetime",
				"batch_no",
				"serial_and_batch_bundle",
			],
			as_dict=True,
		)
	elif voucher:
		sle = frappe.db.sql(
			"""
			SELECT name, voucher_no, item_code, warehouse, actual_qty, qty_after_transaction,
			       stock_value, stock_value_difference, valuation_rate, incoming_rate, outgoing_rate,
			       posting_datetime, batch_no, serial_and_batch_bundle
			FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND item_code=%s AND warehouse=%s AND is_cancelled=0
			ORDER BY posting_datetime, creation
			LIMIT 1
			""",
			(voucher, item, warehouse),
			as_dict=True,
		)
		sle = sle[0] if sle else None
	elif i4_pz:
		sle = i4_pz
	elif patient:
		sle = frappe.db.get_value(
			"Stock Ledger Entry",
			patient.get("sle_name"),
			[
				"name",
				"voucher_no",
				"item_code",
				"warehouse",
				"actual_qty",
				"qty_after_transaction",
				"stock_value",
				"stock_value_difference",
				"valuation_rate",
				"incoming_rate",
				"outgoing_rate",
				"posting_datetime",
				"batch_no",
				"serial_and_batch_bundle",
			],
			as_dict=True,
		)
	if not sle:
		return {
			"topic": TOPIC_I4,
			"repair_class": I4_LEFTOVER_REPAIR,
			"item": item,
			"warehouse": warehouse,
			"status": "NOT_I4",
			"planner_status": "NO_REPAIR_PATH",
			"eligible": False,
		}
	reason = sle_poison_reason(sle) or ""
	prev = _fetch_previous(item, warehouse, sle.posting_datetime)
	prev_ok = True
	prev_blocker = None
	if prev:
		prev_poison = sle_poison_reason(prev)
		prev_qty = flt(prev.qty_after_transaction)
		prev_val = flt(prev.stock_value)
		if prev_poison:
			prev_ok = False
			prev_blocker = f"previous SLE poisoned ({prev_poison}) on {prev.voucher_no}"
		elif prev_qty < -QTY_EPS:
			prev_ok = False
			prev_blocker = f"previous SLE negative qty_after={prev_qty} on {prev.voucher_no}"
		elif abs(prev_qty) <= QTY_EPS and abs(prev_val) > 1:
			prev_ok = False
			prev_blocker = f"previous SLE already I4 leftover on {prev.voucher_no}"
	# I4-specific patient zero: earliest remaining leftover on this identity.
	i4_pz_voucher = i4_pz.voucher_no if i4_pz else None
	this_is_i4_pz = bool(i4_pz and i4_pz.voucher_no == sle.voucher_no)
	if not i4_pz and abs(flt(sle.qty_after_transaction)) <= QTY_EPS and abs(flt(sle.stock_value)) > 1:
		this_is_i4_pz = True
		i4_pz_voucher = sle.voucher_no
	is_leftover = abs(flt(sle.qty_after_transaction)) <= QTY_EPS and abs(flt(sle.stock_value)) > 1
	residual = flt(sle.stock_value)
	qty_after = flt(sle.qty_after_transaction)
	expected_value = 0.0 if abs(qty_after) <= QTY_EPS else residual
	sim = preview_i4_replay(item, warehouse, from_dt=sle.posting_datetime)
	pz_clears = _simulation_clears_patient_zero(item, warehouse, sle)
	status = I4_WAITING
	if abs(qty_after) <= QTY_EPS and abs(residual) <= 1:
		status = I4_REPAIRED
	elif this_is_i4_pz and is_leftover and prev_ok and pz_clears:
		status = I4_READY
	elif this_is_i4_pz and is_leftover and (not prev_ok or not pz_clears):
		status = "MANUAL"
	elif is_leftover and i4_pz_voucher and i4_pz_voucher != sle.voucher_no:
		status = I4_WAITING
	elif is_leftover or reason in I4_POISONS:
		status = I4_REPLAY_REQUIRED
	else:
		status = "NOT_I4"
	msg = "Identity leftover detected." if status in (I4_READY, I4_WAITING, I4_REPLAY_REQUIRED) else ""
	if status == "MANUAL":
		msg = prev_blocker or (
			"Replay from previous SLE does not clear Patient Zero residual — opening is poisoned; not auto-READY_I4."
			if not pz_clears
			else "Previous SLE is not a healthy opening for I4 repair."
		)
	effective_patient = None
	if i4_pz:
		effective_patient = {
			"voucher_no": i4_pz.voucher_no,
			"sle_name": i4_pz.name,
			"item_code": item,
			"warehouse": warehouse,
			"reason": "qty_after_zero_nonzero_value",
			"posting_datetime": str(i4_pz.posting_datetime),
		}
	elif patient:
		effective_patient = patient
	return {
		"topic": TOPIC_I4,
		"repair_class": I4_LEFTOVER_REPAIR,
		"voucher": sle.voucher_no,
		"sle": sle.name,
		"item": item,
		"warehouse": warehouse,
		"batch": sle.batch_no,
		"serial_and_batch_bundle": sle.serial_and_batch_bundle,
		"reason": reason or "qty_after_zero_nonzero_value",
		"status": status,
		"i4_status": status,
		"confidence": "EXACT" if status == I4_READY else "LIKELY",
		"qty_after": qty_after,
		"current_value": residual,
		"expected_value": 0.0 if abs(qty_after) <= QTY_EPS else expected_value,
		"residual_value": residual if abs(qty_after) <= QTY_EPS else 0.0,
		"qty_becomes_zero": abs(qty_after) <= QTY_EPS,
		"previous_voucher": (prev.voucher_no if prev else None),
		"previous_sle": prev.name if prev else None,
		"previous_qty": flt(prev.qty_after_transaction) if prev else None,
		"previous_value": flt(prev.stock_value) if prev else None,
		"previous_healthy": prev_ok,
		"previous_blocker": prev_blocker,
		"pz_residual_clears_in_sim": pz_clears,
		"posting_datetime": str(sle.posting_datetime),
		"patient_zero": effective_patient
		or {
			"voucher_no": sle.voucher_no,
			"sle_name": sle.name,
			"item_code": item,
			"warehouse": warehouse,
			"reason": reason or "qty_after_zero_nonzero_value",
		},
		"generic_patient_zero": patient,
		"i4_patient_zero": effective_patient,
		"replay_count": int(sim.get("rows") or 0),
		"sql_updates_estimate": int(sim.get("sql_updates") or 0),
		"stop_before_voucher": sim.get("stop_before_voucher"),
		"stop_reason": sim.get("stop_reason"),
		"eligible": status == I4_READY,
		"message": msg,
	}


def _earliest_i4_leftover(item, warehouse):
	"""Earliest SLE on identity with qty_after≈0 and nonzero stock_value."""
	if not item or not warehouse:
		return None
	rows = frappe.db.sql(
		"""
		SELECT name, voucher_no, item_code, warehouse, actual_qty, qty_after_transaction,
		       stock_value, stock_value_difference, valuation_rate, incoming_rate, outgoing_rate,
		       posting_datetime, batch_no, serial_and_batch_bundle
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND ABS(qty_after_transaction) < %s AND ABS(IFNULL(stock_value,0)) > 1
		ORDER BY posting_datetime, creation
		LIMIT 1
		""",
		(item, warehouse, QTY_EPS),
		as_dict=True,
	)
	return rows[0] if rows else None


def _simulation_clears_patient_zero(item, warehouse, sle) -> bool:
	"""True iff replay_series would leave this SLE with qty≈0 and |stock_value|≤1."""
	from_dt = get_datetime(sle.posting_datetime)
	prev = _fetch_previous(item, warehouse, from_dt)
	rows = _fetch_sles(item, warehouse, from_dt, before=False)
	if not rows or rows[0].name != sle.name:
		# Find this SLE as first matching voucher in window
		rows = [r for r in rows if r.voucher_no == sle.voucher_no][:1] or rows[:1]
	if not rows:
		return False
	writable = []
	for row in rows:
		reason = sle_poison_reason(row)
		if reason in HARD_STOP_POISONS and reason not in I4_POISONS:
			break
		if reason in HARD_STOP_POISONS and flt(row.actual_qty) > QTY_EPS:
			break
		writable.append(row)
		if row.name == sle.name or row.voucher_no == sle.voucher_no:
			# Only need the PZ step for residual-clear proof.
			break
	if not writable:
		return False
	opening_qty = D(prev.qty_after_transaction) if prev else D(0)
	opening_value = D(prev.stock_value) if prev else D(0)
	series = replay_series(writable, opening_qty, opening_value)
	if not series:
		return False
	step = series[0]
	return abs(flt(step["qty_after_transaction"])) <= QTY_EPS and abs(flt(step["stock_value"])) <= 1


def scan_i4_leftover(
	company=None,
	voucher=None,
	item_code=None,
	warehouse=None,
	batch=None,
	serial_and_batch_bundle=None,
	work_order=None,
	from_date=None,
	to_date=None,
	limit=2000,
) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.scan_filters import (
		append_sle_scope,
		filter_rows_by_planner,
		normalize_scope,
	)

	scope = normalize_scope(
		company=company,
		voucher=voucher,
		item_code=item_code,
		warehouse=warehouse,
		batch=batch,
		serial_and_batch_bundle=serial_and_batch_bundle,
		work_order=work_order,
		from_date=from_date,
		to_date=to_date,
		repair_class=I4_LEFTOVER_REPAIR,
	)
	conds = ["sle.is_cancelled=0", f"ABS(sle.qty_after_transaction) < {QTY_EPS}", "ABS(IFNULL(sle.stock_value,0)) > 1"]
	args: list = []
	conds, args, join_sql = append_sle_scope(conds, args, scope)
	rows = frappe.db.sql(
		f"""
		SELECT sle.name, sle.item_code, sle.warehouse, sle.voucher_no, sle.voucher_type,
		       sle.actual_qty, sle.incoming_rate, sle.outgoing_rate, sle.valuation_rate, sle.stock_value,
		       sle.stock_value_difference, sle.qty_after_transaction, sle.posting_datetime,
		       sle.batch_no, sle.serial_and_batch_bundle, sle.posting_date
		FROM `tabStock Ledger Entry` sle
		{join_sql}
		WHERE {" AND ".join(conds)}
		ORDER BY sle.posting_datetime, sle.creation
		LIMIT {int(limit)}
		""",
		args,
		as_dict=True,
	)
	classified = []
	seen = set()
	for sle in rows:
		key = (sle.item_code, sle.warehouse, sle.voucher_no)
		if key in seen:
			continue
		seen.add(key)
		classified.append(classify_i4_row(sle.item_code, sle.warehouse, voucher=sle.voucher_no, sle_name=sle.name))
	from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

	stamped = stamp_scan_result({"count": len(classified), "rows": classified, "by_status": _count(classified, "i4_status")})
	stamped["rows"] = filter_rows_by_planner(stamped["rows"], repair_class=I4_LEFTOVER_REPAIR)
	stamped["count"] = len(stamped["rows"])
	# Phase 5 KPI contract
	identities = {
		(r.get("item") or r.get("item_code"), r.get("warehouse"))
		for r in stamped["rows"]
		if (r.get("item") or r.get("item_code")) and r.get("warehouse")
	}
	stamped["raw_count"] = stamped["count"]
	stamped["actionable_count"] = stamped["count"]
	stamped["root_identity_count"] = len(identities)
	stamped["ready_count"] = sum(
		1 for r in stamped["rows"] if r.get("eligible") or r.get("i4_status") == "READY_I4"
	)
	return stamped


def preview_i4_replay(item_code, warehouse, *, from_dt) -> dict:
	from_dt = get_datetime(from_dt)
	prev = _fetch_previous(item_code, warehouse, from_dt)
	rows = _fetch_sles(item_code, warehouse, from_dt, before=False)
	opening_qty = D(prev.qty_after_transaction) if prev else D(0)
	opening_value = D(prev.stock_value) if prev else D(0)
	# Preflight: stop before applying a hard-poison inbound rate from storage.
	writable = []
	stop_before = None
	stop_reason = None
	for row in rows:
		reason = sle_poison_reason(row)
		# I4 leftovers are what we are fixing; allow them in the series.
		if reason in HARD_STOP_POISONS and row.voucher_no != (rows[0].voucher_no if rows else None):
			# First row (PZ) may show leftover only — hard stop on later different poison.
			if reason not in I4_POISONS:
				stop_before = row.voucher_no
				stop_reason = reason
				break
		if reason in HARD_STOP_POISONS and abs(flt(row.actual_qty)) > QTY_EPS and flt(row.actual_qty) > 0:
			# Negative/exploded incoming on this or later row — do not write past prior.
			if writable:
				stop_before = row.voucher_no
				stop_reason = reason
				break
			# If the PZ itself has hard poison, not READY_I4 territory.
			stop_before = row.voucher_no
			stop_reason = reason
			break
		writable.append(row)
	series = replay_series(writable, opening_qty, opening_value) if writable else []
	sql = len(series)  # SLE updates
	sql += 1 if series else 0  # Bin
	return {
		"ok": True,
		"dry_run": True,
		"rows": len(series),
		"sql_updates": sql,
		"touched_vouchers": sorted({r.voucher_no for r in writable}),
		"final_qty": flt(series[-1]["qty_after_transaction"]) if series else flt(opening_qty),
		"final_value": flt(series[-1]["stock_value"]) if series else flt(opening_value),
		"stop_before_voucher": stop_before,
		"stop_reason": stop_reason,
		"previous_voucher": prev.voucher_no if prev else None,
		"previous_qty": flt(prev.qty_after_transaction) if prev else 0,
		"previous_value": flt(prev.stock_value) if prev else 0,
	}


def dry_run_i4_repair(rows: list[dict] | None = None) -> dict:
	out = []
	for raw in rows or []:
		item = raw.get("item") or raw.get("item_code")
		warehouse = raw.get("warehouse")
		voucher = raw.get("voucher") or raw.get("voucher_no")
		classified = classify_i4_row(item, warehouse, voucher=voucher, sle_name=raw.get("sle"))
		sim = preview_i4_replay(item, warehouse, from_dt=classified.get("posting_datetime"))
		next_sle = None
		if classified.get("posting_datetime"):
			nxt = frappe.db.sql(
				"""
				SELECT voucher_no, qty_after_transaction, stock_value, posting_datetime
				FROM `tabStock Ledger Entry`
				WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
				  AND posting_datetime > %s
				ORDER BY posting_datetime, creation LIMIT 1
				""",
				(item, warehouse, classified["posting_datetime"]),
				as_dict=True,
			)
			next_sle = nxt[0] if nxt else None
		gl_n = frappe.db.sql(
			"""
			SELECT COUNT(DISTINCT voucher_no) FROM `tabGL Entry`
			WHERE voucher_type='Stock Entry' AND voucher_no IN %s AND IFNULL(is_cancelled,0)=0
			""",
			(tuple(sim.get("touched_vouchers") or ["__none__"]),),
		)[0][0]
		out.append(
			{
				**classified,
				"dry_run": True,
				"residual_value": classified.get("residual_value"),
				"residual_source": "stock_value at qty_after=0",
				"previous_sle": classified.get("previous_sle"),
				"next_sle": next_sle.voucher_no if next_sle else None,
				"replay_count": sim.get("rows"),
				"affected_sle": sim.get("rows"),
				"affected_bin": 1 if sim.get("rows") else 0,
				"affected_gl": int(gl_n or 0),
				"affected_riv": 0,
				"sql_updates": sim.get("sql_updates"),
				"estimated_runtime_seconds": max(0.05, (sim.get("rows") or 0) * 0.05),
				"expected_result": {
					"qty_after": 0,
					"stock_value": 0,
					"message": "Identity leftover cleared; downstream SLE rewritten until next hard poison.",
				},
				"preview_chain": [
					f"Current value {classified.get('current_value')}",
					"↓",
					"Expected value 0",
					"↓",
					f"Replay {sim.get('rows')} SLE",
					"↓",
					"Bin from last rewritten SLE",
					"↓",
					f"Selective GL ({int(gl_n or 0)} vouchers)",
					"↓",
					"Integrity",
					"↓",
					"DATABASE BACKUP REQUIRED",
				],
				"simulation": sim,
			}
		)
	from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

	return stamp_scan_result({"dry_run": True, "count": len(out), "rows": out})


def repair_i4_selected(rows: list[dict], *, dry_run=True) -> dict:
	"""Apply READY_I4 rows: identity replay from patient zero, Bin, selective GL."""
	applied = []
	blocked = []
	log = start_run("I4_LEFTOVER", dry_run=dry_run)
	frappe.flags[HISTORICAL_REPAIR_FLAG] = True
	savepoint = None
	t0 = perf_counter()
	try:
		prepared = []
		for raw in rows or []:
			item = raw.get("item") or raw.get("item_code")
			warehouse = raw.get("warehouse")
			voucher = raw.get("voucher") or raw.get("voucher_no")
			classified = classify_i4_row(item, warehouse, voucher=voucher, sle_name=raw.get("sle"))
			merged = {**classified, **{k: v for k, v in (raw or {}).items() if v not in (None, "")}}
			merged["topic"] = TOPIC_I4
			merged["repair_class"] = I4_LEFTOVER_REPAIR
			from erpnext_extensions.iran_accounting.historical_stock.planner import assert_ready

			try:
				assert_ready(merged)
				prepared.append(merged)
			except Exception as exc:
				blocked.append({"row": raw, "error": str(exc), "status": STATUS_BLOCKED})
		if blocked and not dry_run:
			finish_run(log, applied=0, blocked=len(blocked), error="aborted: no partial commits")
			return {
				"dry_run": False,
				"aborted": True,
				"reason": blocked[0].get("error"),
				"sql_updates_executed": 0,
				"applied": [],
				"blocked": blocked,
				"repair_run_id": getattr(log, "repair_run_id", None),
			}
		if dry_run:
			preview = dry_run_i4_repair(prepared)
			finish_run(log, applied=0, blocked=0)
			return {**preview, "repair_run_id": getattr(log, "repair_run_id", None)}
		savepoint = f"i4_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(savepoint)
		sql_executed = 0
		for merged in prepared:
			result = apply_i4_identity(
				merged["item"],
				merged["warehouse"],
				from_dt=merged.get("posting_datetime"),
				patient_voucher=merged.get("voucher"),
			)
			sql_executed += int(result.get("sql_updates") or 0)
			gl = _rebuild_gl_touched(result.get("touched_vouchers") or [])
			sql_executed += int(gl.get("rebuilt") or 0)
			row_out = {
				**merged,
				**result,
				"gl": gl,
				"written": True,
				"status": STATUS_REPAIRED,
				"i4_status": I4_REPAIRED,
			}
			applied.append(row_out)
			append_entry(log, row_out, written=True)
		frappe.db.commit()
		finish_run(log, applied=len(applied), blocked=0)
		return {
			"dry_run": False,
			"aborted": False,
			"applied": applied,
			"blocked": blocked,
			"sql_updates_executed": sql_executed,
			"savepoint_created": True,
			"transaction_committed": True,
			"elapsed_seconds": round(perf_counter() - t0, 3),
			"database_backup_recommended": True,
			"repair_run_id": getattr(log, "repair_run_id", None),
			"riv": "NOT_INVOKED",
			"global_replay": False,
		}
	except Exception as exc:
		if savepoint:
			frappe.db.rollback(save_point=savepoint)
		finish_run(log, applied=0, blocked=1, error=str(exc))
		raise
	finally:
		frappe.flags[HISTORICAL_REPAIR_FLAG] = False


def apply_i4_identity(item_code, warehouse, *, from_dt, patient_voucher=None) -> dict:
	"""Rewrite SLE running balances from the patient-zero datetime using replay_series.

	Refuses to commit I4_REPAIRED semantics when the Patient Zero residual would remain.
	"""
	sim = preview_i4_replay(item_code, warehouse, from_dt=from_dt)
	from_dt = get_datetime(from_dt)
	prev = _fetch_previous(item_code, warehouse, from_dt)
	if prev:
		prev_poison = sle_poison_reason(prev)
		if prev_poison or flt(prev.qty_after_transaction) < -QTY_EPS:
			raise frappe.ValidationError(
				"Cannot apply I4: previous SLE is not a healthy opening "
				f"({prev.voucher_no}, poison={prev_poison}, qty_after={flt(prev.qty_after_transaction)}). "
				"Repair the earlier poison / negative stock first."
			)
	rows = _fetch_sles(item_code, warehouse, from_dt, before=False)
	writable_names = set()
	# Recompute writable list the same way as preview.
	writable = []
	for row in rows:
		reason = sle_poison_reason(row)
		if reason in HARD_STOP_POISONS and flt(row.actual_qty) > QTY_EPS:
			if writable:
				break
			raise frappe.ValidationError(f"Cannot apply I4: hard poison on patient zero ({reason})")
		if reason in HARD_STOP_POISONS and reason not in I4_POISONS and writable:
			break
		writable.append(row)
		writable_names.add(row.name)
	if not writable:
		raise frappe.ValidationError("Cannot apply I4: no writable SLE rows in replay window")
	opening_qty = D(prev.qty_after_transaction) if prev else D(0)
	opening_value = D(prev.stock_value) if prev else D(0)
	series = replay_series(writable, opening_qty, opening_value)
	# Gate: Patient Zero step must clear leftover before any write.
	pz_step = series[0]
	if abs(flt(pz_step["qty_after_transaction"])) <= QTY_EPS and abs(flt(pz_step["stock_value"])) > 1:
		raise frappe.ValidationError(
			"Cannot apply I4: replay preserves Patient Zero residual "
			f"(simulated stock_value={flt(pz_step['stock_value'])}). "
			"Opening value/qty are poisoned — not a READY_I4 auto-repair."
		)
	touched = []
	for i, row in enumerate(writable):
		step = series[i]
		frappe.db.set_value(
			"Stock Ledger Entry",
			row.name,
			{
				"qty_after_transaction": flt(step["qty_after_transaction"]),
				"stock_value": flt(step["stock_value"]),
				"stock_value_difference": flt(step["stock_value_difference"]),
				"valuation_rate": flt(step["valuation_rate"]),
			},
			update_modified=False,
		)
		# On full consume, keep outgoing_rate as |svd|/qty when qty leaves stock.
		if flt(row.actual_qty) < -QTY_EPS and abs(flt(step["qty_after_transaction"])) <= QTY_EPS:
			qty = abs(flt(row.actual_qty))
			if qty > QTY_EPS:
				frappe.db.set_value(
					"Stock Ledger Entry",
					row.name,
					{"outgoing_rate": flt(abs(flt(step["stock_value_difference"])) / qty)},
					update_modified=False,
				)
		touched.append(row.voucher_no)
	# Post-write verify: patient voucher (or first writable) residual must be cleared.
	pz_name = writable[0].name
	if patient_voucher:
		for row in writable:
			if row.voucher_no == patient_voucher:
				pz_name = row.name
				break
	verify = frappe.db.get_value(
		"Stock Ledger Entry",
		pz_name,
		["qty_after_transaction", "stock_value"],
		as_dict=True,
	)
	if verify and abs(flt(verify.qty_after_transaction)) <= QTY_EPS and abs(flt(verify.stock_value)) > 1:
		raise frappe.ValidationError(
			f"I4 apply failed residual check on {pz_name}: "
			f"qty_after={flt(verify.qty_after_transaction)} stock_value={flt(verify.stock_value)}"
		)
	final_qty = series[-1]["qty_after_transaction"] if series else opening_qty
	final_value = series[-1]["stock_value"] if series else opening_value
	final_rate = series[-1]["valuation_rate"] if series else D(0)
	_update_bin(item_code, warehouse, final_qty, final_value, final_rate)
	# Sync SABB avg_rate for rewritten outbound/inbound where possible.
	sabb_n = _sync_sabb(writable)
	return {
		"ok": True,
		"status": "I4_REPLAYED",
		"item_code": item_code,
		"warehouse": warehouse,
		"patient_voucher": patient_voucher,
		"rows": len(series),
		"sql_updates": len(series) + 1 + sabb_n,
		"touched_vouchers": sorted(set(touched)),
		"final_qty": flt(final_qty),
		"final_value": flt(final_value),
		"final_rate": flt(final_rate),
		"stop_before_voucher": sim.get("stop_before_voucher"),
		"stop_reason": sim.get("stop_reason"),
		"sabb_updated": sabb_n,
		"patient_zero_residual_cleared": True,
	}


def find_patient_zero_for_voucher(voucher: str, item=None, warehouse=None) -> dict:
	"""Operator helper: given a downstream voucher, locate the identity patient zero."""
	conds = ["sle.is_cancelled=0", "sle.voucher_no=%s"]
	args: list = [voucher]
	if item:
		conds.append("sle.item_code=%s")
		args.append(item)
	if warehouse:
		conds.append("sle.warehouse=%s")
		args.append(warehouse)
	legs = frappe.db.sql(
		f"""
		SELECT DISTINCT sle.item_code, sle.warehouse
		FROM `tabStock Ledger Entry` sle
		WHERE {" AND ".join(conds)}
		""",
		args,
		as_dict=True,
	)
	chains = []
	for leg in legs:
		pz = find_patient_zero_identity(leg.item_code, leg.warehouse)
		if not pz:
			continue
		downstream = pz.get("voucher_no") != voucher
		chains.append(
			{
				"item": leg.item_code,
				"warehouse": leg.warehouse,
				"selected_voucher": voucher,
				"patient_zero": pz.get("voucher_no"),
				"patient_reason": pz.get("reason"),
				"is_downstream": downstream,
				"message": (
					f"You selected a downstream voucher. Repair must begin from Patient Zero "
					f"{pz.get('voucher_no')}."
					if downstream
					else f"{voucher} is the Patient Zero for this identity."
				),
			}
		)
	return {"voucher": voucher, "chains": chains, "count": len(chains)}


def root_cause_explorer(voucher: str, item=None, warehouse=None) -> dict:
	"""Healthy → Patient Zero → replay chain → current voucher."""
	found = find_patient_zero_for_voucher(voucher, item=item, warehouse=warehouse)
	nodes = []
	edges = []
	for chain in found.get("chains") or []:
		pz = chain["patient_zero"]
		prev = frappe.db.sql(
			"""
			SELECT voucher_no FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			  AND posting_datetime < (
			    SELECT MIN(posting_datetime) FROM `tabStock Ledger Entry`
			    WHERE voucher_no=%s AND item_code=%s AND warehouse=%s AND is_cancelled=0
			  )
			ORDER BY posting_datetime DESC, creation DESC LIMIT 1
			""",
			(chain["item"], chain["warehouse"], pz, chain["item"], chain["warehouse"]),
		)
		healthy = prev[0][0] if prev else None
		seq = []
		if healthy:
			seq.append({"id": healthy, "label": "Healthy", "kind": "healthy"})
		seq.append({"id": pz, "label": "First Patient Zero", "kind": "patient_zero", "reason": chain["patient_reason"]})
		# Replay chain: SLE vouchers between PZ and selected.
		mid = frappe.db.sql(
			"""
			SELECT DISTINCT voucher_no FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			  AND posting_datetime >= (
			    SELECT MIN(posting_datetime) FROM `tabStock Ledger Entry`
			    WHERE voucher_no=%s AND item_code=%s AND warehouse=%s AND is_cancelled=0
			  )
			  AND posting_datetime <= (
			    SELECT MAX(posting_datetime) FROM `tabStock Ledger Entry`
			    WHERE voucher_no=%s AND item_code=%s AND warehouse=%s AND is_cancelled=0
			  )
			ORDER BY posting_datetime, creation
			LIMIT 40
			""",
			(
				chain["item"],
				chain["warehouse"],
				pz,
				chain["item"],
				chain["warehouse"],
				voucher,
				chain["item"],
				chain["warehouse"],
			),
		)
		for (vn,) in mid:
			if vn in (pz, voucher):
				continue
			seq.append({"id": vn, "label": "Replay chain", "kind": "replay"})
		if voucher != pz:
			seq.append({"id": voucher, "label": "Current Voucher", "kind": "current"})
		for i, node in enumerate(seq):
			nodes.append({**node, "item": chain["item"], "warehouse": chain["warehouse"], "order": i + 1})
			if i:
				edges.append({"from": seq[i - 1]["id"], "to": node["id"]})
	return {
		"voucher": voucher,
		"nodes": nodes,
		"edges": edges,
		"chains": found.get("chains") or [],
		"healthy_anchor": next((n["id"] for n in nodes if n.get("kind") == "healthy"), None),
		"patient_zero": next((n["id"] for n in nodes if n.get("kind") == "patient_zero"), None),
		"root_patient_zero": next((n["id"] for n in nodes if n.get("kind") == "patient_zero"), None),
		"root_cause": next((n.get("reason") for n in nodes if n.get("kind") == "patient_zero"), None),
		"replay_order": [n["id"] for n in nodes if n.get("kind") in ("patient_zero", "replay", "current")],
		"repair_order": [n["id"] for n in nodes if n.get("kind") == "patient_zero"][:1]
		+ ["Replay identity", "Bin", "Selective GL", "Integrity"],
		"phase2_repair_plan": _phase2_repair_plan(voucher, item=item, warehouse=warehouse),
		"dependency_summary": " → ".join(n["id"] for n in nodes[:12]),
		"message": (found.get("chains") or [{}])[0].get("message") if found.get("chains") else "No patient zero found",
	}


def _phase2_repair_plan(voucher, item=None, warehouse=None) -> dict:
	"""Operator chain for Wrong Rate / Failed RIV / GL clicks."""
	steps = []
	riv_status = None
	rate_status = None
	gl_class = None
	try:
		from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv

		riv = scan_failed_riv(voucher=voucher, limit=5)
		if riv.get("rows"):
			row = riv["rows"][0]
			riv_status = row.get("riv_status")
			steps.append({"stage": "Failed RIV", "status": riv_status, "riv": row.get("riv_name")})
			if riv_status and str(riv_status).startswith("WAITING_RATE"):
				steps.append({"stage": "Waiting", "status": "WAITING_RATE"})
				steps.append({"stage": "Required class", "status": "Wrong Rate Patient Zero"})
				steps.append({"stage": "Repair order", "status": "Repair Rate → Replay → GL → Retry RIV"})
			elif riv_status == "SAFE_TO_RETRY":
				steps.append({"stage": "Required class", "status": "SAFE_TO_RETRY"})
				steps.append({"stage": "Repair order", "status": "Retry RIV (idempotent)"})
	except Exception:
		pass
	try:
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

		wr = scan_wrong_rates(voucher=voucher, item_code=item, warehouse=warehouse, limit=20)
		if wr.get("rows"):
			row = wr["rows"][0]
			rate_status = row.get("planner_status") or row.get("rate_status")
			steps.append(
				{
					"stage": "Wrong Rate",
					"status": rate_status,
					"bucket": row.get("mismatch_class") or row.get("rate_bucket"),
					"patient_zero": (row.get("patient_zero") or {}).get("voucher_no")
					if isinstance(row.get("patient_zero"), dict)
					else row.get("patient_zero"),
				}
			)
	except Exception:
		pass
	try:
		from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import classify_stock_entry_gl

		gl = classify_stock_entry_gl(voucher)
		gl_class = gl.get("gl_class")
		steps.append({"stage": "GL", "status": gl_class, "role": gl.get("gl_role")})
		if gl.get("sle_poisoned") or gl_class == "G4_BUILT_FROM_POISONED_SLE":
			steps.append({"stage": "Required class", "status": "WAITING_SLE — repair SLE/rate before GL"})
		elif gl.get("eligible"):
			steps.append({"stage": "Required class", "status": "Selective GL rebuild"})
	except Exception:
		pass
	return {
		"selected_anomaly": voucher,
		"riv_status": riv_status,
		"rate_status": rate_status,
		"gl_class": gl_class,
		"steps": steps,
		"narrative": " → ".join(f"{s.get('stage')}:{s.get('status')}" for s in steps[:8]),
	}


def identity_health(item_code, warehouse) -> dict:
	"""Compact health panel for one identity."""
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import classify_identity
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows

	checks = {}
	# I4
	last = frappe.db.sql(
		"""
		SELECT qty_after_transaction, stock_value, voucher_no FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime DESC, creation DESC LIMIT 1
		""",
		(item_code, warehouse),
		as_dict=True,
	)
	i4_bad = bool(last and abs(flt(last[0].qty_after_transaction)) <= QTY_EPS and abs(flt(last[0].stock_value)) > 1)
	checks["I4"] = not i4_bad
	# SLE class
	sle_state = classify_identity(item_code, warehouse)
	checks["SLE"] = sle_state == "HEALTHY"
	# Bin
	binrow = frappe.db.sql(
		"SELECT actual_qty, stock_value FROM `tabBin` WHERE item_code=%s AND warehouse=%s",
		(item_code, warehouse),
		as_dict=True,
	)
	if last and binrow:
		checks["Bin"] = abs(flt(binrow[0].actual_qty) - flt(last[0].qty_after_transaction)) <= 0.5 and abs(
			flt(binrow[0].stock_value) - flt(last[0].stock_value)
		) <= 1
	else:
		checks["Bin"] = True
	# Zero rate (this item)
	zr = scan_zero_rate_rows(voucher=None)
	zr_hit = any(
		(r.get("item") or r.get("item_code")) == item_code
		and (r.get("warehouse") in (None, warehouse) or r.get("s_warehouse") == warehouse or r.get("t_warehouse") == warehouse)
		for r in (zr.get("rows") or [])
	)
	checks["Zero Rate"] = not zr_hit
	# Failed RIV
	failed = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabRepost Item Valuation`
		WHERE item_code=%s AND warehouse=%s AND status='Failed' AND docstatus=1
		""",
		(item_code, warehouse),
	)[0][0]
	checks["Failed RIV"] = int(failed or 0) == 0
	# Posting / Wrong Rate / GL — soft defaults when not scanned
	checks.setdefault("Posting Order", True)
	checks.setdefault("Wrong Rate", True)
	checks.setdefault("GL", checks["SLE"])
	ok = sum(1 for v in checks.values() if v)
	total = len(checks)
	return {
		"item": item_code,
		"warehouse": warehouse,
		"checks": checks,
		"overall_pct": round(100 * ok / total) if total else 100,
		"overall_health": round(100 * ok / total) if total else 100,
		"health_score": round(100 * ok / total) if total else 100,
		"last_voucher": last[0].voucher_no if last else None,
		"i4_leftover": i4_bad,
	}


def _rebuild_gl_touched(vouchers: list[str]) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import (
		classify_stock_entry_gl,
		rebuild_gl_for_voucher,
	)

	rebuilt = 0
	skipped = []
	for vn in vouchers or []:
		# I4 identity replay may touch Purchase Receipt / DN SLEs — never GL-rebuild those as Stock Entry.
		vtype = frappe.db.get_value(
			"Stock Ledger Entry",
			{"voucher_no": vn, "is_cancelled": 0},
			"voucher_type",
		)
		if vtype and vtype != "Stock Entry":
			skipped.append({"voucher": vn, "reason": f"not_stock_entry:{vtype}"})
			continue
		if not frappe.db.exists("Stock Entry", vn):
			skipped.append({"voucher": vn, "reason": "not_stock_entry"})
			continue
		klass = classify_stock_entry_gl(vn)
		gc = klass.get("gl_class") or ""
		if gc in (
			"G1_BALANCED_BUT_ECONOMICALLY_WRONG",
			"G2_MISSING",
			"G3_UNBALANCED",
			"G4_BUILT_FROM_POISONED_SLE",
		):
			# Only rebuild when SLE no longer poisoned for this voucher.
			poison = frappe.db.sql(
				"""
				SELECT name, actual_qty, qty_after_transaction, stock_value, incoming_rate, valuation_rate,
				       stock_value_difference
				FROM `tabStock Ledger Entry`
				WHERE voucher_no=%s AND is_cancelled=0
				""",
				vn,
				as_dict=True,
			)
			if any(sle_poison_reason(r) for r in poison):
				skipped.append({"voucher": vn, "reason": "still_poisoned"})
				continue
			try:
				rebuild_gl_for_voucher(vn, dry_run=False)
				rebuilt += 1
			except Exception as exc:
				# Never abort identity SLE repair because selective GL failed.
				skipped.append({"voucher": vn, "reason": f"gl_rebuild_error:{exc}"})
				frappe.log_error(title=f"I4 selective GL skip {vn}", message=str(exc))
		else:
			skipped.append({"voucher": vn, "reason": gc})
	return {"rebuilt": rebuilt, "skipped": skipped}


def _sync_sabb(writable_rows) -> int:
	n = 0
	for row in writable_rows or []:
		bundle = row.serial_and_batch_bundle
		if not bundle:
			continue
		rate = flt(row.outgoing_rate) if flt(row.actual_qty) < 0 else flt(row.incoming_rate)
		# After rewrite, prefer valuation derived from last set_value — re-read.
		fresh = frappe.db.get_value(
			"Stock Ledger Entry",
			row.name,
			["outgoing_rate", "incoming_rate", "actual_qty", "stock_value_difference"],
			as_dict=True,
		)
		if not fresh:
			continue
		if abs(flt(fresh.actual_qty)) > QTY_EPS:
			rate = abs(flt(fresh.stock_value_difference) / flt(fresh.actual_qty))
		if abs(rate) <= QTY_EPS:
			continue
		frappe.db.set_value("Serial and Batch Bundle", bundle, "avg_rate", rate, update_modified=False)
		n += 1
	return n


def _count(rows, key):
	out = defaultdict(int)
	for row in rows or []:
		out[str(row.get(key) or "")] += 1
	return dict(out)
