# Copyright (c) 2026, ERPNext Extensions contributors
"""Leftover Moving Average after an authorized zero-value inbound (v5.3.4).

The zero inbound itself is never given an invented rate. Quantity is added with
incoming value 0. Warehouse MA becomes leftover_value / new_qty. Downstream
outgoing consumes that reconstructed MA.

Opening-state / incomplete chains stay MANUAL.
"""

from __future__ import annotations

import json
from collections import defaultdict
from time import perf_counter

import frappe
from frappe.utils import cint, flt, get_datetime

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_AMBIGUOUS,
	CONFIDENCE_EXACT,
	HISTORICAL_REPAIR_FLAG,
	LEFTOVER_MA_FAILED_POSTCONDITION,
	LEFTOVER_MA_FAILED_RIV,
	LEFTOVER_MA_MANUAL,
	LEFTOVER_MA_READY,
	LEFTOVER_MA_REPAIR,
	LEFTOVER_MA_REPAIRED,
	LEFTOVER_MA_REPORT_MISMATCH,
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


def fetch_stock_ledger_source_rows(item, warehouse) -> list[dict]:
	"""Rows the standard ERPNext Stock Ledger report reads.

	Avg Rate = ``valuation_rate``. Incoming Rate = ``incoming_rate``.
	Outgoing Rate on the report is ``stock_value_difference / actual_qty``
	(``in_out_rate``), not the SLE ``outgoing_rate`` column.
	"""
	if not item or not warehouse:
		return []
	return list(
		frappe.db.sql(
			"""SELECT name, voucher_no, voucher_type, posting_datetime, actual_qty,
			          qty_after_transaction, incoming_rate, valuation_rate,
			          stock_value, stock_value_difference
			   FROM `tabStock Ledger Entry`
			   WHERE item_code=%s AND warehouse=%s AND is_cancelled=0 AND docstatus < 2
			   ORDER BY posting_datetime, creation""",
			(item, warehouse),
			as_dict=True,
		)
		or []
	)


def leftover_ma_postcondition(
	item,
	warehouse,
	*,
	root_voucher,
	expected_ma,
	expected_stock_value,
	rows=None,
	bin_row=None,
) -> dict:
	"""Fail-closed check against the Stock Ledger report data source."""
	chain = list(rows or []) or fetch_stock_ledger_source_rows(item, warehouse)
	root = next((r for r in chain if _gv(r, "voucher_no") == root_voucher), None)
	if not root:
		return {
			"ok": False,
			"status": LEFTOVER_MA_FAILED_POSTCONDITION,
			"reason": "root SLE missing from Stock Ledger source",
		}
	vr = flt(_gv(root, "valuation_rate"))
	sv = flt(_gv(root, "stock_value"))
	ir = flt(_gv(root, "incoming_rate"))
	svd = flt(_gv(root, "stock_value_difference"))
	qa = flt(_gv(root, "qty_after_transaction"))
	if abs(ir) > RATE_EPS or abs(svd) > 1:
		return {
			"ok": False,
			"status": LEFTOVER_MA_FAILED_POSTCONDITION,
			"reason": "receipt incoming value was invented",
			"report_valuation_rate": vr,
			"report_incoming_rate": ir,
		}
	if flt(expected_stock_value) > VALUE_EPS and abs(sv - flt(expected_stock_value)) > max(1.0, abs(qa)):
		return {
			"ok": False,
			"status": LEFTOVER_MA_FAILED_POSTCONDITION,
			"reason": "stock_value not preserved on zero inbound",
			"report_stock_value": sv,
		}
	if flt(expected_ma) > VALUE_EPS and abs(vr) <= RATE_EPS:
		return {
			"ok": False,
			"status": LEFTOVER_MA_FAILED_POSTCONDITION,
			"reason": "Stock Ledger valuation_rate still zero",
			"report_valuation_rate": vr,
			"expected_ma": expected_ma,
		}
	if flt(expected_ma) > VALUE_EPS and abs(vr - flt(expected_ma)) > 1:
		return {
			"ok": False,
			"status": LEFTOVER_MA_FAILED_POSTCONDITION,
			"reason": f"Stock Ledger MA {vr} != expected {expected_ma}",
			"report_valuation_rate": vr,
			"expected_ma": expected_ma,
		}
	seen_root = False
	for row in chain:
		if _gv(row, "voucher_no") == root_voucher:
			seen_root = True
			continue
		if not seen_root:
			continue
		aq = flt(_gv(row, "actual_qty"))
		if aq >= -QTY_EPS:
			continue
		report_out = flt(_gv(row, "stock_value_difference")) / aq if aq else 0.0
		if abs(report_out) <= RATE_EPS and abs(flt(_gv(row, "stock_value"))) > VALUE_EPS:
			return {
				"ok": False,
				"status": LEFTOVER_MA_FAILED_POSTCONDITION,
				"reason": f"downstream {_gv(row, 'voucher_no')} outgoing still zero in Stock Ledger",
			}
	last = chain[-1]
	if bin_row is None:
		try:
			bin_row = frappe.db.get_value(
				"Bin",
				{"item_code": item, "warehouse": warehouse},
				["actual_qty", "stock_value", "valuation_rate"],
				as_dict=True,
			)
		except Exception:
			bin_row = None
	if bin_row:
		if abs(flt(_gv(bin_row, "actual_qty")) - flt(_gv(last, "qty_after_transaction"))) > QTY_EPS:
			return {
				"ok": False,
				"status": LEFTOVER_MA_FAILED_POSTCONDITION,
				"reason": "Bin qty != last SLE",
			}
		if abs(flt(_gv(bin_row, "stock_value")) - flt(_gv(last, "stock_value"))) > max(
			1.0, abs(flt(_gv(last, "qty_after_transaction")))
		):
			return {
				"ok": False,
				"status": LEFTOVER_MA_FAILED_POSTCONDITION,
				"reason": "Bin value != last SLE",
			}
	return {
		"ok": True,
		"status": LEFTOVER_MA_REPAIRED,
		"report_valuation_rate": vr,
		"report_stock_value": sv,
		"report_incoming_rate": ir,
		"report_qty_after": qa,
	}


def stamp_patient_zero_valuation_rate(item, warehouse, root_voucher) -> dict:
	"""Minimum correction: leftover MA on the zero inbound SLE.

	Vanilla ``update_entries_after`` / RIV can leave ``valuation_rate=0`` on an
	authorized zero-value receipt while ``stock_value`` stays the leftover
	balance. Stock Ledger Avg Rate is ``valuation_rate``, so that row must be
	stamped to leftover_value / qty. Incoming rate and SVD stay 0.
	"""
	row = frappe.db.get_value(
		"Stock Ledger Entry",
		{"voucher_no": root_voucher, "item_code": item, "warehouse": warehouse, "is_cancelled": 0},
		["name", "qty_after_transaction", "stock_value", "valuation_rate", "incoming_rate", "stock_value_difference"],
		as_dict=True,
	)
	if not row:
		return {"ok": False, "stamped": False, "reason": "root SLE missing"}
	qa = flt(row.qty_after_transaction)
	sv = flt(row.stock_value)
	vr = flt(row.valuation_rate)
	if qa <= QTY_EPS or sv <= VALUE_EPS:
		return {"ok": True, "stamped": False, "reason": "not a valued leftover position"}
	expected = sv / qa
	if abs(vr - expected) <= 1:
		return {"ok": True, "stamped": False, "valuation_rate": vr}
	if abs(flt(row.incoming_rate)) > RATE_EPS or abs(flt(row.stock_value_difference)) > 1:
		return {"ok": False, "stamped": False, "reason": "refusing to stamp a valued inbound"}
	frappe.db.set_value(
		"Stock Ledger Entry",
		row.name,
		{"valuation_rate": expected},
		update_modified=False,
	)
	return {"ok": True, "stamped": True, "valuation_rate": expected, "previous": vr}


def find_leftover_ma_patient_zeros(item, warehouse) -> list:
	"""Zero inbound + leftover value + zero Avg Rate — the RIV poison pattern."""
	if not item or not warehouse:
		return []
	return frappe.db.sql(
		"""
		SELECT name, voucher_no, qty_after_transaction, stock_value, valuation_rate,
		       incoming_rate, stock_value_difference, actual_qty
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND IFNULL(is_cancelled,0)=0
		  AND actual_qty > 0
		  AND ABS(IFNULL(incoming_rate,0)) <= %s
		  AND ABS(IFNULL(stock_value_difference,0)) <= 1
		  AND qty_after_transaction > %s
		  AND stock_value > 1
		  AND ABS(IFNULL(valuation_rate,0)) <= 1
		""",
		(item, warehouse, RATE_EPS, QTY_EPS),
		as_dict=True,
	)


def replay_downstream_after_patient_zero(item, warehouse, root_voucher) -> dict:
	"""Replay from the first SLE after the stamped leftover inbound.

	``update_entries_after`` inclusive of the patient-zero can rewrite
	``valuation_rate`` back to 0. Downstream-only replay uses the stamped
	row as previous SLE so outgoing consumes leftover MA.
	"""
	root = frappe.db.get_value(
		"Stock Ledger Entry",
		{"voucher_no": root_voucher, "item_code": item, "warehouse": warehouse, "is_cancelled": 0},
		["name", "posting_date", "posting_time", "posting_datetime"],
		as_dict=True,
	)
	if not root:
		return {"ok": False, "replayed": False, "reason": "root SLE missing"}
	nxt = frappe.db.sql(
		"""
		SELECT name, posting_date, posting_time, voucher_no
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND IFNULL(is_cancelled,0)=0
		  AND name != %s
		  AND posting_datetime > %s
		ORDER BY posting_datetime, creation
		LIMIT 1
		""",
		(item, warehouse, root.name, root.posting_datetime or f"{root.posting_date} {root.posting_time}"),
		as_dict=True,
	)
	if not nxt:
		return {"ok": True, "replayed": False}
	row = nxt[0]
	posting_time = row.posting_time
	if hasattr(posting_time, "total_seconds"):
		secs = int(posting_time.total_seconds())
		posting_time = f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"
	from erpnext.stock.stock_ledger import update_entries_after

	update_entries_after(
		{
			"item_code": item,
			"warehouse": warehouse,
			"posting_date": str(row.posting_date),
			"posting_time": str(posting_time),
		},
		allow_zero_rate=True,
		allow_negative_stock=False,
	)
	return {"ok": True, "replayed": True, "from_voucher": row.voucher_no, "from_sle": row.name}


def restore_leftover_ma_after_riv(item=None, warehouse=None) -> dict:
	"""Re-stamp leftover MA after a vanilla RIV that zeroed patient-zero VR."""
	stamped = []
	for row in find_leftover_ma_patient_zeros(item, warehouse):
		r = stamp_patient_zero_valuation_rate(item, warehouse, row.voucher_no)
		if not r.get("stamped"):
			continue
		stamped.append(row.voucher_no)
		replay_downstream_after_patient_zero(item, warehouse, row.voucher_no)
		stamp_patient_zero_valuation_rate(item, warehouse, row.voucher_no)
	if stamped:
		invalidate_stock_ledger_prepared_reports(item)
	return {"ok": True, "stamped": len(stamped), "vouchers": stamped}


def on_repost_item_valuation_update(doc, method=None):
	"""After official RIV completes, leftover-MA must survive.

	ERPNext worker completion is ``repost()`` → ``set_status("Completed")`` →
	``Document.db_set``. ``db_set`` runs ``on_change``, not ``on_update``.
	Bind this function to ``on_change`` (and keep on_update for full saves).
	"""
	if str(getattr(doc, "status", "") or "") != "Completed":
		return
	if frappe.flags.get("leftover_ma_riv_hook"):
		return
	item = getattr(doc, "item_code", None)
	warehouse = getattr(doc, "warehouse", None)
	if not item or not warehouse:
		return
	frappe.flags.leftover_ma_riv_hook = True
	try:
		restore_leftover_ma_after_riv(item, warehouse)
	finally:
		frappe.flags.leftover_ma_riv_hook = False


def leftover_ma_desk_postcondition(
	item,
	warehouse,
	*,
	root_voucher,
	expected_ma,
	expected_stock_value,
	rows=None,
	bin_row=None,
) -> dict:
	"""Compare SLE, direct execute, and the Desk report API.

	Desk default is a Prepared Report. Generate-equivalent is
	``query_report.run(..., ignore_prepared_report=True)``.
	"""
	sle_pc = leftover_ma_postcondition(
		item,
		warehouse,
		root_voucher=root_voucher,
		expected_ma=expected_ma,
		expected_stock_value=expected_stock_value,
		rows=rows,
		bin_row=bin_row,
	)
	if not sle_pc.get("ok"):
		return sle_pc
	filters = _stock_ledger_filters(item, warehouse)
	try:
		from erpnext.stock.report.stock_ledger.stock_ledger import execute as sl_execute

		_cols, exec_rows = sl_execute(frappe._dict(filters))
	except Exception as exc:
		return {
			"ok": False,
			"status": LEFTOVER_MA_FAILED_POSTCONDITION,
			"reason": f"Stock Ledger execute failed: {exc}",
		}
	exec_root = next((r for r in (exec_rows or []) if _gv(r, "voucher_no") == root_voucher), None)
	if not exec_root:
		return {
			"ok": False,
			"status": LEFTOVER_MA_FAILED_POSTCONDITION,
			"reason": "root voucher missing from Stock Ledger execute",
		}
	if flt(expected_ma) > VALUE_EPS and abs(flt(_gv(exec_root, "valuation_rate"))) <= RATE_EPS:
		return {
			"ok": False,
			"status": LEFTOVER_MA_REPORT_MISMATCH,
			"reason": "REPORT_LEDGER_MISMATCH: execute Avg Rate is zero",
		}
	try:
		from frappe.desk.query_report import run as qr_run

		api = qr_run(
			"Stock Ledger",
			filters=dict(filters),
			ignore_prepared_report=True,
			are_default_filters=False,
		)
	except Exception as exc:
		return {
			"ok": False,
			"status": LEFTOVER_MA_REPORT_MISMATCH,
			"reason": f"REPORT_LEDGER_MISMATCH: Desk API failed: {exc}",
		}
	api_root = next((r for r in (api.get("result") or []) if _gv(r, "voucher_no") == root_voucher), None)
	if not api_root:
		return {
			"ok": False,
			"status": LEFTOVER_MA_REPORT_MISMATCH,
			"reason": "REPORT_LEDGER_MISMATCH: root voucher missing from Desk API",
		}
	if flt(expected_ma) > VALUE_EPS and abs(flt(_gv(api_root, "valuation_rate"))) <= RATE_EPS:
		return {
			"ok": False,
			"status": LEFTOVER_MA_REPORT_MISMATCH,
			"reason": "REPORT_LEDGER_MISMATCH: Desk API Avg Rate is zero",
		}
	if abs(flt(_gv(api_root, "valuation_rate")) - flt(_gv(exec_root, "valuation_rate"))) > 1:
		return {
			"ok": False,
			"status": LEFTOVER_MA_REPORT_MISMATCH,
			"reason": "REPORT_LEDGER_MISMATCH: Desk API Avg Rate != execute",
		}
	return {
		**sle_pc,
		"execute_valuation_rate": flt(_gv(exec_root, "valuation_rate")),
		"api_valuation_rate": flt(_gv(api_root, "valuation_rate")),
	}


def _stock_ledger_filters(item, warehouse) -> dict:
	company = frappe.db.get_value("Warehouse", warehouse, "company")
	return {
		"company": company,
		"from_date": "2000-01-01",
		"to_date": str(frappe.utils.today()),
		"item_code": [item],
		"warehouse": [warehouse],
		"valuation_field_type": "Currency",
		"segregate_serial_batch_bundle": 1,
	}


def invalidate_stock_ledger_prepared_reports(item, company=None) -> int:
	"""Delete Stock Ledger snapshots Desk can still serve for this item.

	Item-specific and company-wide (empty item_code) Completed/Started rows
	are both removed so a fresh Generate cannot reuse a pre-repair file.
	"""
	if not item:
		return 0
	rows = frappe.db.sql(
		"""SELECT name, filters FROM `tabPrepared Report`
		   WHERE report_name='Stock Ledger'
		     AND status IN ('Completed','Started','Error','Queued')
		     AND filters LIKE %s""",
		(f"%{item}%",),
		as_dict=True,
	)
	deleted = 0
	for row in rows:
		try:
			frappe.delete_doc(
				"Prepared Report",
				row.name,
				ignore_permissions=True,
				force=True,
				delete_permanently=True,
			)
			deleted += 1
		except Exception:
			continue
	return deleted


def _riv_status_blocks_completion(status) -> str | None:
	st = str(status or "")
	if st in ("Queued", "In Progress"):
		return f"RIV still {st}"
	if st == "Failed":
		return LEFTOVER_MA_FAILED_RIV
	return None


def create_and_run_narrow_riv(item, warehouse, *, posting_date, posting_time, allow_zero_rate=True) -> dict:
	"""Official ERPNext RIV (Item + Warehouse) executed through ``repost()``.

	Submit alone leaves the document Queued — that is not a completed repair.
	"""
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
		execute_reposting_entry,
	)

	company = frappe.db.get_value("Warehouse", warehouse, "company")
	doc = frappe.get_doc(
		{
			"doctype": "Repost Item Valuation",
			"based_on": "Item and Warehouse",
			"item_code": item,
			"warehouse": warehouse,
			"company": company,
			"posting_date": str(posting_date),
			"posting_time": str(posting_time),
			"allow_negative_stock": 0,
			"allow_zero_rate": 1 if allow_zero_rate else 0,
			"recalculate_valuation_rate": 0,
			"recreate_stock_ledgers": 0,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert()
	doc.submit()
	# Normal lifecycle enqueues via scheduler. Execute the same worker target
	# so Historical Repair never marks Completed on a queued RIV.
	try:
		execute_reposting_entry(doc.name)
	except Exception as exc:
		frappe.db.set_value("Repost Item Valuation", doc.name, {"status": "Failed", "error_log": str(exc)[:1000]})
		return {
			"ok": False,
			"status": LEFTOVER_MA_FAILED_RIV,
			"riv_name": doc.name,
			"riv_status": "Failed",
			"reason": str(exc)[:500],
		}
	st = frappe.db.get_value("Repost Item Valuation", doc.name, ["status", "error_log"], as_dict=True)
	block = _riv_status_blocks_completion(st.status)
	if block:
		return {
			"ok": False,
			"status": LEFTOVER_MA_FAILED_RIV if st.status == "Failed" else LEFTOVER_MA_FAILED_POSTCONDITION,
			"riv_name": doc.name,
			"riv_status": st.status,
			"reason": block if st.status != "Failed" else (st.error_log or "FAILED_RIV"),
		}
	return {"ok": True, "riv_name": doc.name, "riv_status": st.status}


def persist_via_update_entries_after(item, warehouse, root_voucher) -> dict:
	"""ERPNext-native replay from the zero inbound forward (Moving Average)."""
	sle = frappe.db.get_value(
		"Stock Ledger Entry",
		{"voucher_no": root_voucher, "item_code": item, "warehouse": warehouse, "is_cancelled": 0},
		["posting_date", "posting_time", "name"],
		as_dict=True,
	)
	if not sle:
		return {"ok": False, "reason": "root SLE not found for update_entries_after"}
	posting_time = sle.posting_time
	if hasattr(posting_time, "total_seconds"):
		secs = int(posting_time.total_seconds())
		posting_time = f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"
	from erpnext.stock.stock_ledger import update_entries_after

	update_entries_after(
		{
			"item_code": item,
			"warehouse": warehouse,
			"posting_date": str(sle.posting_date),
			"posting_time": str(posting_time),
		},
		allow_zero_rate=True,
		allow_negative_stock=False,
	)
	stamp = stamp_patient_zero_valuation_rate(item, warehouse, root_voucher)
	return {
		"ok": True,
		"path": "update_entries_after",
		"posting_date": str(sle.posting_date),
		"posting_time": str(posting_time),
		"stamp": stamp,
	}


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
		pc = leftover_ma_postcondition(
			item,
			warehouse,
			root_voucher=root,
			expected_ma=zov.get("expected_ma"),
			expected_stock_value=zov.get("expected_stock_value"),
		)
		if pc.get("ok"):
			return {
				**base,
				**zov,
				"voucher": root,
				"leftover_ma_status": LEFTOVER_MA_REPAIRED,
				"status": LEFTOVER_MA_REPAIRED,
				"reason": "already matches leftover-MA reconstruction and Stock Ledger postcondition",
				"sql_updates": 0,
				"eligible": False,
				"postcondition": pc,
			}
		# Simulation matches but the report source still fails — do not call it Completed.
		sql = int(sim.get("rows") or 1) + 1
	else:
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
			if out.get("leftover_ma_status") == LEFTOVER_MA_FAILED_POSTCONDITION or out.get("status") == LEFTOVER_MA_FAILED_POSTCONDITION:
				if savepoint:
					frappe.db.rollback(save_point=savepoint)
				finish_run(log, applied=0, blocked=1, error="FAILED_POSTCONDITION")
				return {
					"dry_run": False,
					"aborted": True,
					"applied": [],
					"blocked": [{"row": merged, "error": out.get("reason") or "FAILED_POSTCONDITION", "status": LEFTOVER_MA_FAILED_POSTCONDITION}],
					"postcondition": out.get("postcondition"),
				}
			row_out = {**merged, **out, "written": bool(out.get("economic_writes")), "status": out.get("status") or STATUS_REPAIRED}
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
	expected_ma = sim.get("final_ma")
	# Post-inbound expected MA is leftover value / qty after the honored receipt.
	if sim.get("opening_qty") is not None and sim.get("opening_value") is not None:
		root_step = (sim.get("series") or [None])[0] or {}
		expected_ma = flt(root_step.get("valuation_rate") or expected_ma)
	expected_sv = flt((sim.get("series") or [{}])[0].get("stock_value") or sim.get("opening_value") or 0)

	def _pc():
		return leftover_ma_desk_postcondition(
			item,
			warehouse,
			root_voucher=root_voucher,
			expected_ma=expected_ma,
			expected_stock_value=expected_sv,
		)

	if sim.get("already_repaired"):
		stamp_patient_zero_valuation_rate(item, warehouse, root_voucher)
		pc = _pc()
		if pc.get("ok"):
			invalidate_stock_ledger_prepared_reports(item)
			return {
				"status": LEFTOVER_MA_REPAIRED,
				"leftover_ma_status": LEFTOVER_MA_REPAIRED,
				"economic_writes": 0,
				"applied": False,
				"message": "ALREADY_REPAIRED",
				"final_qty": sim.get("final_qty"),
				"final_value": sim.get("final_value"),
				"final_ma": sim.get("final_ma"),
				"postcondition": pc,
			}

	from erpnext_extensions.iran_accounting.historical_stock.snapshot import capture_identity_snapshot

	snap = capture_identity_snapshot(root_voucher, item=item, warehouse=warehouse)
	writable = sim["writable"]
	series = sim["series"]
	safety = series_safety(series)
	if not safety["ok"]:
		raise frappe.ValidationError(f"leftover-MA safety failed: {safety}")

	persist = {"ok": False, "path": None}
	try:
		persist = persist_via_update_entries_after(item, warehouse, root_voucher)
	except Exception as exc:
		persist = {"ok": False, "path": "update_entries_after", "reason": str(exc)[:500]}

	riv = None
	if persist.get("ok") and persist.get("posting_date"):
		riv = create_and_run_narrow_riv(
			item,
			warehouse,
			posting_date=persist.get("posting_date"),
			posting_time=persist.get("posting_time"),
		)
		persist["riv"] = riv
		if not riv.get("ok"):
			return {
				"status": riv.get("status") or LEFTOVER_MA_FAILED_RIV,
				"leftover_ma_status": riv.get("status") or LEFTOVER_MA_FAILED_RIV,
				"economic_writes": 0,
				"applied": False,
				"reason": riv.get("reason") or "FAILED_RIV",
				"persist": persist,
				"riv": riv,
			}
		# RIV can rewrite patient-zero valuation_rate back to 0. Re-stamp
		# and replay only the later issues so outgoing uses leftover MA.
		stamp_patient_zero_valuation_rate(item, warehouse, root_voucher)
		replay_downstream_after_patient_zero(item, warehouse, root_voucher)
		stamp_patient_zero_valuation_rate(item, warehouse, root_voucher)

	pc = _pc()
	writes = 0
	if not pc.get("ok"):
		# Native replay did not satisfy the Stock Ledger postcondition — reconstruct
		# the leftover-MA series onto the same SLE rows the report reads.
		for row, step in zip(writable, series):
			payload = {
				"qty_after_transaction": flt(step["qty_after_transaction"]),
				"stock_value": flt(step["stock_value"]),
				"stock_value_difference": flt(step["stock_value_difference"]),
				"valuation_rate": flt(step["valuation_rate"]),
			}
			if flt(row.actual_qty) < -QTY_EPS:
				payload["outgoing_rate"] = flt(step["outgoing_rate"])
			if flt(row.actual_qty) > QTY_EPS and row.voucher_no in (sim.get("honor_zero_vouchers") or []):
				payload["incoming_rate"] = 0
			changed = False
			for field, value in payload.items():
				if abs(flt(_gv(row, field)) - flt(value)) > (QTY_EPS if "qty" in field else 1):
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
		pc = _pc()
		persist = {**persist, "fallback": "sle_series_reconstruction", "writes": writes}

	if not pc.get("ok"):
		return {
			"status": LEFTOVER_MA_FAILED_POSTCONDITION,
			"leftover_ma_status": LEFTOVER_MA_FAILED_POSTCONDITION,
			"economic_writes": writes,
			"applied": False,
			"reason": pc.get("reason") or "FAILED_POSTCONDITION",
			"postcondition": pc,
			"persist": persist,
		}

	last = series[-1]
	gl = _rebuild_gl(sim.get("touched_vouchers") or [])
	pr_deleted = invalidate_stock_ledger_prepared_reports(item)
	return {
		"status": STATUS_REPAIRED,
		"leftover_ma_status": LEFTOVER_MA_REPAIRED,
		"economic_writes": writes or (0 if persist.get("path") == "update_entries_after" and sim.get("already_repaired") else max(writes, 1 if persist.get("ok") else 0)),
		"applied": True,
		"final_qty": flt(last["qty_after_transaction"]),
		"final_value": flt(last["stock_value"]),
		"final_ma": flt(last["valuation_rate"]),
		"touched_vouchers": sim.get("touched_vouchers"),
		"snapshot_before": snap,
		"gl": gl,
		"postcondition": pc,
		"persist": persist,
		"riv": riv,
		"prepared_reports_invalidated": pr_deleted,
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
