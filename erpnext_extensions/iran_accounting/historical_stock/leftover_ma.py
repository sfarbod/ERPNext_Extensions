# Copyright (c) 2026, ERPNext Extensions contributors
"""Leftover Moving Average after an authorized zero-value inbound (v5.3.4).

The zero inbound itself is never given an invented rate. Quantity is added with
incoming value 0. Warehouse MA becomes leftover_value / new_qty. Downstream
outgoing consumes that reconstructed MA.

Opening-state / incomplete chains stay MANUAL.
"""

from __future__ import annotations

from collections import defaultdict
from time import perf_counter

import frappe
from frappe.utils import cint, flt, get_datetime

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_AMBIGUOUS,
	CONFIDENCE_EXACT,
	HISTORICAL_REPAIR_FLAG,
	LEFTOVER_MA_MANUAL,
	LEFTOVER_MA_READY,
	LEFTOVER_MA_REPAIR,
	LEFTOVER_MA_REPAIRED,
	LEFTOVER_MA_WAITING,
	QTY_EPS,
	RATE_EPS,
	STATUS_BLOCKED,
	STATUS_REPAIRED,
	TOPIC_LEFTOVER_MA,
	VALUE_EPS,
	ZP_PROVEN_LEGITIMATE_ZERO,
	ZP_UNPROVEN_ZERO,
	ZP_VALUED_STOCK_ZERO_STAMP,
	ZP_ZERO_INBOUND_ON_VALUED_POSITION,
)
from erpnext_extensions.iran_accounting.historical_stock.audit import append_entry, finish_run, start_run
from erpnext_extensions.iran_accounting.historical_stock.zero_provenance import (
	classify_zero_provenance,
	fetch_identity_chain,
)
from erpnext_extensions.iran_accounting.stock_posting_order.replay import _update_bin
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D


def replay_leftover_ma_series(rows: list, opening_qty=0, opening_value=0, *, honor_zero_vouchers=None) -> list[dict]:
	"""Qty-only for honored zero inbounds; outgoing uses leftover MA.

	Unlike generic replay_series, a zero incoming_rate is NOT replaced by the
	current MA on honored zero-inbound vouchers (that would invent receipt value).
	"""
	honor = set(honor_zero_vouchers or [])
	running_qty = D(opening_qty)
	running_value = D(opening_value)
	out = []
	for row in rows:
		qty = D(_gv(row, "actual_qty"))
		voucher = str(_gv(row, "voucher_no") or "")
		incoming = D(_gv(row, "incoming_rate") or 0)
		allow_zero = cint(_gv(row, "allow_zero_valuation_rate") or 0)
		stored_svd = D(_gv(row, "stock_value_difference") or 0)
		honor_this = bool(voucher in honor or allow_zero or (abs(incoming) <= D(str(RATE_EPS)) and abs(stored_svd) <= D("1")))
		if qty > 0:
			if honor_this and abs(incoming) <= D(str(RATE_EPS)):
				svd = D(0)
				running_qty += qty
			else:
				rate = incoming
				if rate == 0 and running_qty:
					rate = running_value / running_qty
				svd = qty * rate
				running_qty += qty
				running_value += svd
			outgoing = D(0)
			incoming_out = incoming
		else:
			if running_qty and abs(running_qty + qty) <= D(str(QTY_EPS)):
				svd = -running_value
				outgoing = (abs(svd / qty) if qty else D(0))
				running_qty = D(0)
				running_value = D(0)
			elif running_qty:
				rate = running_value / running_qty
				svd = qty * rate
				outgoing = abs(rate)
				running_qty += qty
				running_value += svd
				if abs(running_qty) <= D(str(QTY_EPS)):
					running_qty = D(0)
					if abs(running_value) <= D("1"):
						running_value = D(0)
			else:
				svd = D(0)
				outgoing = D(0)
				running_qty += qty
			incoming_out = D(0)
		val_rate = (running_value / running_qty) if running_qty else D(0)
		out.append(
			{
				"name": _gv(row, "name"),
				"voucher_no": voucher,
				"qty_after_transaction": running_qty,
				"stock_value": running_value,
				"stock_value_difference": svd,
				"valuation_rate": val_rate,
				"outgoing_rate": outgoing,
				"incoming_rate": incoming_out,
				"old_stock_value_difference": stored_svd,
			}
		)
	return out


def _gv(row, key, default=None):
	if isinstance(row, dict):
		return row.get(key, default)
	return getattr(row, key, default)


def series_safety(series: list[dict]) -> dict:
	"""Return first safety failure or ok."""
	neg_qty = 0
	i1 = 0
	i4 = 0
	for step in series:
		qa = flt(step["qty_after_transaction"])
		sv = flt(step["stock_value"])
		ir = flt(step.get("incoming_rate") or 0)
		if qa < -QTY_EPS:
			neg_qty += 1
		if ir < -RATE_EPS:
			i1 += 1
		if abs(qa) <= QTY_EPS and abs(sv) > 1:
			i4 += 1
	ok = neg_qty == 0 and i1 == 0 and i4 == 0
	return {"ok": ok, "neg_qty": neg_qty, "i1": i1, "i4": i4}


def already_matches(current_rows: list, series: list[dict]) -> bool:
	if len(current_rows) != len(series):
		return False
	for row, step in zip(current_rows, series):
		if abs(flt(_gv(row, "qty_after_transaction")) - flt(step["qty_after_transaction"])) > QTY_EPS:
			return False
		if abs(flt(_gv(row, "valuation_rate")) - flt(step["valuation_rate"])) > 1:
			return False
		# ERPNext integer-rate rounding can leave a few IRR of stock_value drift
		# on a multi-billion position. 1 IRR per unit is still economically same MA.
		qty = abs(flt(_gv(row, "qty_after_transaction")))
		value_tol = max(1.0, qty)
		if abs(flt(_gv(row, "stock_value")) - flt(step["stock_value"])) > value_tol:
			return False
		if flt(_gv(row, "actual_qty")) < -QTY_EPS:
			if abs(flt(_gv(row, "outgoing_rate")) - flt(step["outgoing_rate"])) > 1:
				return False
	return True


def classify_leftover_ma_identity(item, warehouse) -> dict:
	prov = classify_zero_provenance(item=item, warehouse=warehouse)
	base = {
		"topic": TOPIC_LEFTOVER_MA,
		"repair_class": LEFTOVER_MA_REPAIR,
		"item": item,
		"item_code": item,
		"warehouse": warehouse,
		"provenance": prov.get("provenance"),
		"secondary_provenance": prov.get("secondary_provenance"),
		"current_qty": prov.get("current_qty"),
		"current_stock_value": prov.get("current_stock_value"),
		"current_effective_ma": prov.get("current_effective_ma"),
		"current_valuation_rate": prov.get("current_valuation_rate"),
		"confidence": prov.get("confidence"),
		"eligible": False,
		"leftover_ma_status": LEFTOVER_MA_MANUAL,
	}
	if prov.get("provenance") == ZP_PROVEN_LEGITIMATE_ZERO:
		return {
			**base,
			"leftover_ma_status": "NO_ACTION",
			"status": "NO_ACTION",
			"reason": "current lot is economically zero — no leftover-MA repair",
			"sql_updates": 0,
		}
	zov = prov.get("zero_on_valued")
	if not zov or prov.get("provenance") not in (
		ZP_VALUED_STOCK_ZERO_STAMP,
		ZP_ZERO_INBOUND_ON_VALUED_POSITION,
	):
		return {
			**base,
			"leftover_ma_status": LEFTOVER_MA_MANUAL if prov.get("provenance") == ZP_UNPROVEN_ZERO else LEFTOVER_MA_WAITING,
			"status": LEFTOVER_MA_MANUAL,
			"reason": prov.get("reason") or "not leftover-MA",
			"sql_updates": 0,
		}

	root = zov["voucher"]
	if not _zero_inbound_authorized(root, item):
		return {
			**base,
			**zov,
			"voucher": root,
			"leftover_ma_status": LEFTOVER_MA_MANUAL,
			"status": LEFTOVER_MA_MANUAL,
			"reason": "zero inbound is not document-authorized (allow_zero_valuation_rate)",
			"sql_updates": 0,
		}

	sim = simulate_leftover_ma(item, warehouse, root_voucher=root)
	if not sim.get("ok"):
		return {
			**base,
			**zov,
			"voucher": root,
			"leftover_ma_status": LEFTOVER_MA_MANUAL,
			"status": LEFTOVER_MA_MANUAL,
			"reason": sim.get("reason") or "simulation failed safety",
			"simulation": {k: sim.get(k) for k in ("neg_qty", "i1", "i4", "reason")},
			"sql_updates": 0,
		}
	if sim.get("already_repaired"):
		return {
			**base,
			**zov,
			"voucher": root,
			"leftover_ma_status": LEFTOVER_MA_REPAIRED,
			"status": LEFTOVER_MA_REPAIRED,
			"reason": "already matches leftover-MA reconstruction",
			"sql_updates": 0,
			"eligible": False,
		}
	sql = int(sim.get("sql_updates") or 0)
	return {
		**base,
		**zov,
		"voucher": root,
		"patient_zero": {"voucher_no": root},
		"leftover_ma_status": LEFTOVER_MA_READY,
		"status": LEFTOVER_MA_READY,
		"eligible": True,
		"confidence": CONFIDENCE_EXACT,
		"reason": "READY_LEFTOVER_MA — authorized zero inbound on valued stock",
		"sql_updates": sql,
		"replay_count": sim.get("rows"),
		"expected_ma": zov.get("expected_ma"),
		"simulation": {
			"final_qty": sim.get("final_qty"),
			"final_value": sim.get("final_value"),
			"final_ma": sim.get("final_ma"),
			"touched": sim.get("touched_vouchers"),
		},
	}


def _zero_inbound_authorized(voucher, item) -> bool:
	if not voucher:
		return False
	flag = frappe.db.get_value(
		"Stock Entry Detail",
		{"parent": voucher, "item_code": item},
		"allow_zero_valuation_rate",
	)
	return bool(cint(flag))


def simulate_leftover_ma(item, warehouse, *, root_voucher) -> dict:
	chain = fetch_identity_chain(item, warehouse)
	root_idx = next((i for i, r in enumerate(chain) if r.voucher_no == root_voucher), None)
	if root_idx is None:
		return {"ok": False, "reason": "root voucher not on identity"}
	if root_idx == 0:
		return {"ok": False, "reason": "opening-state zero inbound — MANUAL"}
	prev = chain[root_idx - 1]
	opening_qty = flt(prev.qty_after_transaction)
	opening_value = flt(prev.stock_value)
	if opening_qty <= QTY_EPS or opening_value <= VALUE_EPS:
		return {"ok": False, "reason": "previous position is not valued stock"}
	if flt(prev.qty_after_transaction) < -QTY_EPS:
		return {"ok": False, "reason": "previous SLE is historically negative"}
	writable = chain[root_idx:]
	# Annotate allow_zero from SE detail
	honor = set()
	for row in writable:
		if flt(row.actual_qty) > QTY_EPS and abs(flt(row.incoming_rate)) <= RATE_EPS:
			if _zero_inbound_authorized(row.voucher_no, item):
				honor.add(row.voucher_no)
				row["allow_zero_valuation_rate"] = 1
	if root_voucher not in honor:
		return {"ok": False, "reason": "root inbound is not authorized zero"}
	series = replay_leftover_ma_series(writable, opening_qty, opening_value, honor_zero_vouchers=honor)
	safety = series_safety(series)
	if not safety["ok"]:
		return {"ok": False, "reason": "simulation safety failed", **safety}
	# Receipt incoming must stay 0
	root_step = series[0]
	if abs(flt(root_step["incoming_rate"])) > RATE_EPS or abs(flt(root_step["stock_value_difference"])) > 1:
		return {"ok": False, "reason": "simulation invented receipt value"}
	matched = already_matches(writable, series)
	sql = 0 if matched else (len(series) + 1)
	last = series[-1]
	lq = flt(last["qty_after_transaction"])
	lv = flt(last["stock_value"])
	return {
		"ok": True,
		"already_repaired": matched,
		"rows": len(series),
		"sql_updates": sql,
		"opening_qty": opening_qty,
		"opening_value": opening_value,
		"final_qty": lq,
		"final_value": lv,
		"final_ma": (lv / lq) if abs(lq) > QTY_EPS else 0.0,
		"touched_vouchers": [r.voucher_no for r in writable],
		"honor_zero_vouchers": sorted(honor),
		"series": series,
		"writable": writable,
		"neg_qty": 0,
		"i1": 0,
		"i4": 0,
	}


def scan_leftover_ma(company=None, item_code=None, warehouse=None, limit=2000) -> dict:
	conds = [
		"sle.is_cancelled=0",
		"sle.actual_qty > %s",
		"ABS(IFNULL(sle.incoming_rate,0)) < %s",
		"ABS(IFNULL(sle.stock_value_difference,0)) <= 1",
		"ABS(IFNULL(sle.stock_value,0)) > 1",
	]
	args: list = [QTY_EPS, RATE_EPS]
	if item_code:
		conds.append("sle.item_code=%s")
		args.append(item_code)
	if warehouse:
		conds.append("sle.warehouse=%s")
		args.append(warehouse)
	if company:
		conds.append(
			"(sle.voucher_type<>'Stock Entry' OR EXISTS (SELECT 1 FROM `tabStock Entry` se WHERE se.name=sle.voucher_no AND se.company=%s))"
		)
		args.append(company)
	rows = frappe.db.sql(
		f"""
		SELECT sle.item_code, sle.warehouse, sle.voucher_no, sle.posting_datetime
		FROM `tabStock Ledger Entry` sle
		WHERE {" AND ".join(conds)}
		ORDER BY sle.posting_datetime, sle.creation
		LIMIT {int(limit)}
		""",
		args,
		as_dict=True,
	)
	seen = set()
	classified = []
	for sle in rows:
		key = (sle.item_code, sle.warehouse)
		if key in seen:
			continue
		seen.add(key)
		classified.append(classify_leftover_ma_identity(sle.item_code, sle.warehouse))
	from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

	stamped = stamp_scan_result(
		{
			"count": len(classified),
			"rows": classified,
			"by_status": _count(classified, "leftover_ma_status"),
		}
	)
	stamped["ready_count"] = sum(1 for r in stamped["rows"] if r.get("leftover_ma_status") == LEFTOVER_MA_READY)
	stamped["manual_count"] = sum(1 for r in stamped["rows"] if r.get("leftover_ma_status") == LEFTOVER_MA_MANUAL)
	stamped["no_action_count"] = sum(1 for r in stamped["rows"] if r.get("leftover_ma_status") == "NO_ACTION")
	return stamped


def repair_leftover_ma_selected(rows: list[dict], *, dry_run=True) -> dict:
	applied = []
	blocked = []
	log = start_run("LEFTOVER_MA", dry_run=dry_run)
	frappe.flags[HISTORICAL_REPAIR_FLAG] = True
	t0 = perf_counter()
	savepoint = None
	try:
		prepared = []
		for raw in rows or []:
			item = raw.get("item") or raw.get("item_code")
			warehouse = raw.get("warehouse")
			classified = classify_leftover_ma_identity(item, warehouse)
			merged = {**classified, **{k: v for k, v in (raw or {}).items() if v not in (None, "")}}
			merged["topic"] = TOPIC_LEFTOVER_MA
			merged["repair_class"] = LEFTOVER_MA_REPAIR
			from erpnext_extensions.iran_accounting.historical_stock.planner import assert_ready

			if classified.get("leftover_ma_status") == LEFTOVER_MA_REPAIRED:
				merged["already_repaired"] = True
				prepared.append(merged)
				continue
			try:
				assert_ready(merged)
				prepared.append(merged)
			except Exception as exc:
				blocked.append({"row": raw, "error": str(exc), "status": STATUS_BLOCKED})
		if blocked and not dry_run:
			finish_run(log, applied=0, blocked=len(blocked), error="aborted: no partial commits")
			return {"dry_run": False, "aborted": True, "applied": [], "blocked": blocked}
		if dry_run:
			finish_run(log, applied=0, blocked=len(blocked))
			return {
				"dry_run": True,
				"applied": [
					{**r, "expected_ma": r.get("expected_ma"), "sql_updates": r.get("sql_updates")} for r in prepared
				],
				"blocked": blocked,
				"count": len(prepared),
			}
		savepoint = f"lma_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(savepoint)
		for merged in prepared:
			out = apply_leftover_ma_identity(merged["item"], merged["warehouse"], root_voucher=merged.get("voucher"))
			row_out = {**merged, **out, "written": bool(out.get("economic_writes")), "status": STATUS_REPAIRED}
			applied.append(row_out)
			append_entry(log, row_out, written=True)
		frappe.db.commit()
		finish_run(log, applied=len(applied), blocked=0)
		return {
			"dry_run": False,
			"aborted": False,
			"applied": applied,
			"blocked": blocked,
			"elapsed_seconds": round(perf_counter() - t0, 3),
			"repair_run_id": getattr(log, "repair_run_id", None),
			"riv": "NOT_INVOKED",
		}
	except Exception:
		if savepoint:
			frappe.db.rollback(save_point=savepoint)
		raise
	finally:
		frappe.flags[HISTORICAL_REPAIR_FLAG] = False


def apply_leftover_ma_identity(item, warehouse, *, root_voucher) -> dict:
	sim = simulate_leftover_ma(item, warehouse, root_voucher=root_voucher)
	if not sim.get("ok"):
		raise frappe.ValidationError(sim.get("reason") or "leftover-MA simulation failed")
	if sim.get("already_repaired"):
		return {
			"status": LEFTOVER_MA_REPAIRED,
			"economic_writes": 0,
			"applied": False,
			"message": "ALREADY_REPAIRED",
			"final_qty": sim.get("final_qty"),
			"final_value": sim.get("final_value"),
			"final_ma": sim.get("final_ma"),
		}
	from erpnext_extensions.iran_accounting.historical_stock.snapshot import capture_identity_snapshot

	snap = capture_identity_snapshot(root_voucher, item=item, warehouse=warehouse)
	writable = sim["writable"]
	series = sim["series"]
	# Safety re-check immediately before write
	safety = series_safety(series)
	if not safety["ok"]:
		raise frappe.ValidationError(f"leftover-MA safety failed: {safety}")
	writes = 0
	for row, step in zip(writable, series):
		payload = {
			"qty_after_transaction": flt(step["qty_after_transaction"]),
			"stock_value": flt(step["stock_value"]),
			"stock_value_difference": flt(step["stock_value_difference"]),
			"valuation_rate": flt(step["valuation_rate"]),
		}
		if flt(row.actual_qty) < -QTY_EPS:
			payload["outgoing_rate"] = flt(step["outgoing_rate"])
		# Never invent incoming_rate on the honored zero inbound
		if flt(row.actual_qty) > QTY_EPS and row.voucher_no in (sim.get("honor_zero_vouchers") or []):
			payload["incoming_rate"] = 0
		changed = False
		for field, value in payload.items():
			if abs(flt(_gv(row, field)) - flt(value)) > (QTY_EPS if "qty" in field else 1 if "rate" in field or "value" in field else 1):
				changed = True
				break
		if changed:
			frappe.db.set_value("Stock Ledger Entry", row.name, payload, update_modified=False)
			writes += 1
		if flt(row.actual_qty) < -QTY_EPS and abs(flt(step["outgoing_rate"])) > RATE_EPS:
			_stamp_se_outgoing(row.voucher_no, item, flt(step["outgoing_rate"]))
	last = series[-1]
	_update_bin(item, warehouse, flt(last["qty_after_transaction"]), flt(last["stock_value"]), flt(last["valuation_rate"]))
	writes += 1
	gl = _rebuild_gl(sim.get("touched_vouchers") or [])
	return {
		"status": STATUS_REPAIRED,
		"leftover_ma_status": LEFTOVER_MA_REPAIRED,
		"economic_writes": writes,
		"applied": True,
		"final_qty": flt(last["qty_after_transaction"]),
		"final_value": flt(last["stock_value"]),
		"final_ma": flt(last["valuation_rate"]),
		"touched_vouchers": sim.get("touched_vouchers"),
		"snapshot_before": snap,
		"gl": gl,
	}


def _stamp_se_outgoing(voucher, item, rate) -> None:
	name = frappe.db.get_value("Stock Entry Detail", {"parent": voucher, "item_code": item}, "name")
	if not name:
		return
	qty = flt(frappe.db.get_value("Stock Entry Detail", name, "qty"))
	frappe.db.set_value(
		"Stock Entry Detail",
		name,
		{"basic_rate": rate, "valuation_rate": rate, "amount": rate * qty, "basic_amount": rate * qty},
		update_modified=False,
	)


def _rebuild_gl(vouchers: list[str]) -> dict:
	rebuilt = 0
	try:
		from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import rebuild_gl_for_voucher

		for vn in vouchers or []:
			try:
				rebuild_gl_for_voucher(vn, dry_run=False)
				rebuilt += 1
			except Exception:
				continue
	except Exception:
		pass
	return {"rebuilt": rebuilt}


def _count(rows, key):
	out = defaultdict(int)
	for r in rows:
		out[str(r.get(key) or "")] += 1
	return dict(out)
