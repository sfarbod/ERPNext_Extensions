# Copyright (c) 2026, ERPNext Extensions contributors
"""Campaign 2 forensics and bounded L4 on dump 20260926_225046.

Candidate 1 (20260927_002257) is immutable. This module never writes that file.
"""

from __future__ import annotations

import json
from pathlib import Path

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock._validation import (
	correction_campaign_225046 as c1,
)
from erpnext_extensions.iran_accounting.historical_stock._validation.prod_ready_0926 import (
	_gates,
	mfg_fingerprint,
)

OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/private/files/hr_correction_225046_c2"
)


def _jdump(name: str, obj) -> None:
	OUT.mkdir(parents=True, exist_ok=True)
	(OUT / name).write_text(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def graph_30100022() -> dict:
	"""Read-only: how 30100022 earliest-SLE RIV reaches 30200016 / 26325-1."""
	item = "30100022"
	earliest = frappe.db.sql(
		"""SELECT name, voucher_type, voucher_no, warehouse, posting_datetime,
		          actual_qty, incoming_rate, outgoing_rate, valuation_rate,
		          stock_value_difference, stock_value, qty_after_transaction,
		          dependant_sle_voucher_detail_no, voucher_detail_no
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation LIMIT 12""",
		item,
		as_dict=True,
	)
	warehouses = frappe.db.sql(
		"""SELECT warehouse, COUNT(*) n, MIN(posting_datetime) first_dt,
		          MAX(posting_datetime) last_dt, MIN(qty_after_transaction) min_after
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND is_cancelled=0
		GROUP BY warehouse ORDER BY first_dt""",
		item,
		as_dict=True,
	)
	shared = frappe.db.sql(
		"""SELECT se.name voucher, se.purpose, se.posting_date, se.posting_time,
		          se.work_order, se.job_card
		FROM `tabStock Entry` se
		WHERE se.docstatus=1
		  AND EXISTS (
			SELECT 1 FROM `tabStock Entry Detail` a
			WHERE a.parent=se.name AND a.item_code='30100022'
		  )
		  AND EXISTS (
			SELECT 1 FROM `tabStock Entry Detail` b
			WHERE b.parent=se.name AND b.item_code='30200016'
		  )
		ORDER BY se.posting_date, se.name
		LIMIT 40""",
		as_dict=True,
	)
	v26325 = frappe.db.sql(
		"""SELECT sed.item_code, sed.qty, sed.basic_rate, sed.basic_amount,
		          sed.s_warehouse, sed.t_warehouse,
		          IFNULL(sed.is_finished_item,0) is_fg, IFNULL(sed.is_scrap_item,0) is_scrap,
		          sed.secondary_item_type, sed.allow_zero_valuation_rate
		FROM `tabStock Entry Detail` sed
		WHERE sed.parent='MAT-STE-2026-26325-1'
		ORDER BY sed.idx""",
		as_dict=True,
	)
	sle_26325 = frappe.db.sql(
		"""SELECT item_code, warehouse, actual_qty, qty_after_transaction,
		          incoming_rate, outgoing_rate, valuation_rate,
		          stock_value_difference, stock_value, posting_datetime,
		          dependant_sle_voucher_detail_no
		FROM `tabStock Ledger Entry`
		WHERE voucher_no='MAT-STE-2026-26325-1' AND is_cancelled=0
		ORDER BY posting_datetime, creation""",
		as_dict=True,
	)
	chain_302 = frappe.db.sql(
		"""SELECT posting_datetime, voucher_no, warehouse, actual_qty,
		          qty_after_transaction, incoming_rate, outgoing_rate, valuation_rate,
		          stock_value_difference, stock_value
		FROM `tabStock Ledger Entry`
		WHERE item_code='30200016' AND is_cancelled=0
		  AND warehouse LIKE '%%پایکار%%'
		ORDER BY posting_datetime, creation
		LIMIT 40""",
		as_dict=True,
	)
	# First 30200016 SLE at paykar vs 30100022 earliest at paykar
	first_302 = frappe.db.sql(
		"""SELECT posting_datetime, voucher_no, warehouse, actual_qty, qty_after_transaction
		FROM `tabStock Ledger Entry`
		WHERE item_code='30200016' AND is_cancelled=0
		ORDER BY posting_datetime, creation LIMIT 8""",
		as_dict=True,
	)
	settings = {
		"stock_allow_negative": frappe.db.get_single_value("Stock Settings", "allow_negative_stock"),
		"item_30100022_neg": frappe.db.get_value("Item", "30100022", "is_stock_item"),
	}
	out = {
		"earliest": earliest,
		"warehouses": warehouses,
		"shared_vouchers": shared,
		"v26325_rows": v26325,
		"sle_26325": sle_26325,
		"paykar_30200016": chain_302,
		"first_30200016": first_302,
		"settings": settings,
		"gates": _gates(),
	}
	_jdump("graph_30100022.json", out)
	return {
		"earliest_voucher": earliest[0].voucher_no if earliest else None,
		"earliest_dt": str(earliest[0].posting_datetime) if earliest else None,
		"earliest_wh": earliest[0].warehouse if earliest else None,
		"shared_n": len(shared),
		"shared_sample": [r.voucher for r in shared[:12]],
		"26325_items": [r.item_code for r in v26325],
		"first_302": first_302[:3],
		"stock_allow_negative": settings["stock_allow_negative"],
	}


def qty_conservation_30200016_paykar() -> dict:
	"""Transferred = returned + consumed + scrap + remainder. Read-only."""
	wh = "انبار پایکار خط تولید اسپاد فارمد"
	item = "30200016"
	sles = frappe.db.sql(
		"""SELECT voucher_no, posting_datetime, actual_qty, qty_after_transaction,
		          incoming_rate, outgoing_rate, stock_value_difference, stock_value
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation""",
		(item, wh),
		as_dict=True,
	)
	inbound = sum(flt(r.actual_qty) for r in sles if flt(r.actual_qty) > 0)
	outbound = sum(abs(flt(r.actual_qty)) for r in sles if flt(r.actual_qty) < 0)
	last = sles[-1] if sles else None
	remainder = flt(last.qty_after_transaction) if last else 0
	purposes = {}
	for r in sles:
		p = frappe.db.get_value("Stock Entry", r.voucher_no, "purpose") if r.voucher_no.startswith("MAT-STE") else "other"
		purposes.setdefault(p or "other", 0)
		purposes[p or "other"] += flt(r.actual_qty)
	out = {
		"inbound": inbound,
		"outbound": outbound,
		"remainder": remainder,
		"identity": inbound - outbound - remainder,
		"n": len(sles),
		"purposes": purposes,
		"min_after": min((flt(r.qty_after_transaction) for r in sles), default=0),
		"has_26325": any(r.voucher_no == "MAT-STE-2026-26325-1" for r in sles),
		"row_26325": next((r for r in sles if r.voucher_no == "MAT-STE-2026-26325-1"), None),
	}
	_jdump("qty_conservation_30200016.json", out)
	return out


def posting_order_l4_blockers() -> dict:
	"""Find READY posting-order rows on the 30100022 dependant fanout (read + classify)."""
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan

	po = run_full_history_scan(company="اسپاد فارمد دارو")
	rows = []
	for raw in po.get("rows") or []:
		r = attach_plan(dict(raw))
		item = r.get("item") or r.get("item_code")
		outb = r.get("outbound_document") or r.get("voucher") or r.get("outbound")
		inb = r.get("inbound_document") or r.get("inbound")
		if item in ("30100022", "30200016", "30300015", "30300014", "13200400") or outb in (
			"MAT-STE-2026-26325-1",
			"MAT-STE-2026-26324",
		):
			rows.append(
				{
					k: r.get(k)
					for k in (
						"item",
						"item_code",
						"warehouse",
						"outbound_document",
						"inbound_document",
						"voucher",
						"outbound",
						"inbound",
						"planner_status",
						"detection",
						"status",
						"sql_updates",
						"reason",
					)
				}
			)
	ready = [r for r in rows if str(r.get("planner_status") or "").startswith("READY")]
	out = {
		"po_count": po.get("count"),
		"fanout_rows": rows,
		"ready_n": len(ready),
		"ready": ready,
		"summary": (po.get("summary") if isinstance(po, dict) else None),
	}
	_jdump("po_l4_blockers.json", out)
	return {"po_count": out["po_count"], "fanout_n": len(rows), "ready_n": len(ready), "ready": ready}


def classify_30200016_paykar_intervals() -> dict:
	from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import (
		scan_series as scan_negative_series,
	)

	wh = "انبار پایکار خط تولید اسپاد فارمد"
	series = frappe.db.sql(
		"""SELECT name, item_code, warehouse, voucher_no, voucher_type, posting_datetime,
		          posting_date, posting_time, creation, actual_qty, qty_after_transaction,
		          stock_value, valuation_rate, batch_no
		FROM `tabStock Ledger Entry`
		WHERE item_code='30200016' AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation""",
		wh,
		as_dict=True,
	)
	for r in series:
		r["item"] = r.item_code
		r["se_docstatus"] = 1
	found = scan_negative_series(series)
	out = {"n": len(series), "found_n": len(found), "found": found[:20]}
	_jdump("intervals_30200016_paykar.json", out)
	return {
		"n": len(series),
		"found_n": len(found),
		"statuses": [f.get("status") or f.get("optimizer_status") for f in found[:15]],
		"vouchers": [f.get("negative_voucher") or f.get("outbound_document") for f in found[:15]],
	}


def apply_ready_posting_order_wave() -> dict:
	"""Apply every READY posting-order row after reconstructed-qty detection."""
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.stock_posting_order.api import (
		repair_posting_order_selected,
	)
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock._validation.prod_ready_0926 import (
		mfg_delta,
		rebuild_bins,
	)

	mfg0 = mfg_fingerprint()
	g0 = _gates()
	po = run_full_history_scan(company="اسپاد فارمد دارو")
	ready = []
	for raw in po.get("rows") or []:
		r = attach_plan(dict(raw))
		if str(r.get("planner_status") or "").startswith("READY") and int(r.get("sql_updates") or 0) > 0:
			ready.append(r)
	# Wave-1 prefers short-window batch-scoped CROSS_TIME (consume-before-inbound).
	# beyond_30min warehouse-escalation rows stay out until a later proven wave.
	short = [
		r
		for r in ready
		if str(r.get("search_window") or "") in ("60s", "5min", "30min")
		and str(r.get("planner_status") or "") == "READY_BATCH_SCOPED_REPAIR"
		and str(r.get("detection") or "") == "CROSS_TIME"
	]
	chosen = (short or ready)[:1]
	if not chosen:
		out = {"ok": True, "applied": 0, "reason": "no READY posting-order rows", "gates": _gates()}
		_jdump("po_wave.json", out)
		return out
	applied = repair_posting_order_selected(rows=chosen, dry_run=False)
	frappe.db.commit()
	rebuild_bins()
	g1 = _gates()
	out = {
		"ok": int(g1.get("neg_after") or 0) <= int(g0.get("neg_after") or 0) and int(g1.get("i1") or 0) == 0,
		"chosen_n": len(chosen),
		"chosen": [
			{
				k: r.get(k)
				for k in (
					"item",
					"item_code",
					"outbound_document",
					"inbound_document",
					"voucher",
					"planner_status",
				)
			}
			for r in chosen
		],
		"applied": applied,
		"gates0": g0,
		"gates1": g1,
		"mfg_delta": mfg_delta(mfg0, mfg_fingerprint()),
	}
	_jdump("po_wave.json", out)
	return {
		"ok": out["ok"],
		"chosen_n": len(chosen),
		"i1": g1.get("i1"),
		"neg_after": g1.get("neg_after"),
	}


def _l4_contract_ok() -> dict:
	"""Hard L4 canaries: 30470 621301 residual, leftover-MA envelope, no new negatives."""
	gl = frappe.db.sql(
		"""SELECT account, ROUND(SUM(debit),2) d, ROUND(SUM(credit),2) c
		FROM `tabGL Entry` WHERE voucher_no='MAT-STE-2026-30470' AND is_cancelled=0
		GROUP BY account""",
		as_dict=True,
	)
	uses_621301 = any("621301" in str(r.account) for r in gl)
	uses_622515 = any("622515" in str(r.account) for r in gl)
	# C1 kept an 8 IRR 621301 residual. VALUED_SOURCE reconstructs the
	# 13100134 Manufacture consume during L3; stock accounts then balance
	# without 621301. Both are valid. 622515 remains forbidden.
	ma = frappe.db.sql(
		"""SELECT valuation_rate, stock_value, qty_after_transaction
		FROM `tabStock Ledger Entry`
		WHERE item_code='16100066' AND is_cancelled=0
		  AND warehouse=%s
		ORDER BY posting_datetime DESC, creation DESC LIMIT 1""",
		("انبار ملزومات مصرفی اسپاد",),
		as_dict=True,
	)
	rate = flt(ma[0].valuation_rate) if ma else 0
	# leftover MA is ~5e6; a trillion-scale rate is fanout poison
	ma_ok = 1 < rate < 1e8
	zero = frappe.db.sql(
		"""SELECT valuation_rate, stock_value FROM `tabStock Ledger Entry`
		WHERE item_code='16100226' AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime DESC, creation DESC LIMIT 1""",
		("انبار ملزومات مصرفی اسپاد",),
		as_dict=True,
	)
	zero_ok = bool(zero) and abs(flt(zero[0].stock_value)) <= 1e-6
	return {
		"uses_621301": uses_621301,
		"uses_622515": uses_622515,
		"ma_rate": rate,
		"ma_ok": ma_ok,
		"zero_ok": zero_ok,
		"ok": (not uses_622515) and ma_ok and zero_ok,
	}


def isolated_l4_30100022() -> dict:
	"""L4: 30100022 warehouse layers after posting-order reconstruction. Stop on gates.

	Start each warehouse at the first non-opening-reco SLE so a fiscal seed
	does not walk the entire dependant Manufacture/Adjustment graph.
	"""
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
		create_and_run_isolated_riv,
		first_non_opening_repost_boundary,
	)

	whs = frappe.db.sql(
		"""SELECT DISTINCT warehouse FROM `tabStock Ledger Entry`
		WHERE item_code='30100022' AND is_cancelled=0""",
		as_dict=True,
	)
	layers = []
	for w in whs:
		b = first_non_opening_repost_boundary("30100022", w.warehouse)
		if b:
			layers.append(b)
	layers.sort(key=lambda r: str(r.posting_datetime))
	s0 = c1._watch_snapshot()
	results = []
	stopped = None
	for layer in layers:
		riv = create_and_run_isolated_riv(
			"30100022",
			layer.warehouse,
			posting_date=layer.posting_date,
			posting_time=layer.posting_time,
			allow_negative_stock=False,
			max_attempts=2,
		)
		frappe.db.commit()
		after = c1._watch_snapshot()
		min_302 = frappe.db.sql(
			"""SELECT MIN(qty_after_transaction) FROM `tabStock Ledger Entry`
			WHERE item_code='30200016' AND is_cancelled=0"""
		)[0][0]
		neg_delta = int(after["gates"].get("neg_after") or 0) - int(s0["gates"].get("neg_after") or 0)
		contract = _l4_contract_ok()
		row = {
			"warehouse": layer.warehouse,
			"first_dt": str(layer.posting_datetime),
			"start_voucher": layer.voucher_no,
			"start_type": layer.voucher_type,
			"riv_ok": riv.get("ok"),
			"riv_status": riv.get("riv_status"),
			"reason": (riv.get("reason") or "")[:400],
			"neg_delta": neg_delta,
			"i1": after["gates"].get("i1"),
			"min_30200016": min_302,
			"open_riv_n": len(after["riv_open"]),
			"contract": contract,
		}
		results.append(row)
		if (
			(not riv.get("ok"))
			or neg_delta > 0
			or int(after["gates"].get("i1") or 0)
			or flt(min_302) < -0.0001
			or not contract.get("ok")
		):
			stopped = {"at": layer.warehouse, "why": row}
			break
	s1 = c1._watch_snapshot()
	out = {
		"layers": results,
		"stopped": stopped,
		"gates0": s0["gates"],
		"gates1": s1["gates"],
		"passed": stopped is None and all(r.get("riv_ok") for r in results),
		"36933": c1.snapshot_36933().get("native") if hasattr(c1, "snapshot_36933") else None,
		"37090": c1.reconstruct_37090_class(),
	}
	_jdump("isolated_l4_30100022.json", out)
	return {
		"passed": out["passed"],
		"stopped": stopped,
		"layer_ok": [r.get("riv_ok") for r in results],
		"i1": s1["gates"].get("i1"),
		"neg_after": s1["gates"].get("neg_after"),
		"min_30200016": results[-1]["min_30200016"] if results else None,
	}


def post_l4_canaries() -> dict:
	"""L4 acceptance canaries after both isolated walks."""
	v26325 = frappe.db.sql(
		"""SELECT item_code, warehouse, actual_qty, qty_after_transaction,
		          outgoing_rate, stock_value_difference, posting_datetime
		FROM `tabStock Ledger Entry`
		WHERE voucher_no='MAT-STE-2026-26325-1' AND is_cancelled=0
		ORDER BY item_code, warehouse""",
		as_dict=True,
	)
	min_302 = frappe.db.sql(
		"""SELECT MIN(qty_after_transaction) FROM `tabStock Ledger Entry`
		WHERE item_code='30200016' AND is_cancelled=0"""
	)[0][0]
	out = {
		"gates": _gates(),
		"canaries": c1.post_l3_verify() if hasattr(c1, "post_l3_verify") else None,
		"v26325": v26325,
		"min_30200016": min_302,
		"36933": c1.snapshot_36933().get("native") if hasattr(c1, "snapshot_36933") else None,
		"37090": c1.reconstruct_37090_class(),
	}
	# compact return
	can = out["canaries"]
	_jdump(
		"post_l4_canaries.json",
		{
			"gates": out["gates"],
			"min_30200016": min_302,
			"v26325": v26325,
			"verify": can,
			"36933": out["36933"],
			"37090": out["37090"],
		},
	)
	contract = _l4_contract_ok()
	return {
		"i1": out["gates"].get("i1"),
		"neg_after": out["gates"].get("neg_after"),
		"neg_rate": out["gates"].get("neg_rate"),
		"min_30200016": min_302,
		"min_26325": min((flt(r.qty_after_transaction) for r in v26325), default=None),
		"36933": out["36933"],
		"37090": out["37090"],
		"open_riv": (can or {}).get("open_riv_n") if isinstance(can, dict) else None,
		"contract": contract,
	}


def isolated_riv_30100294_valued_source() -> dict:
	"""Replay 30100294 Paykar so 25274 consume keeps vanilla MA (valued-source guard)."""
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
		create_and_run_isolated_riv,
	)
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_scrap_fg import (
		analyze_manufacture_scrap_fg,
	)

	item, wh = "30100294", "انبار پایکار خط تولید اسپاد فارمد"
	row = frappe.db.sql(
		"""SELECT posting_date, posting_time FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation LIMIT 1""",
		(item, wh),
		as_dict=True,
	)
	if not row:
		return {"ok": False, "reason": "no SLE"}
	an0 = analyze_manufacture_scrap_fg("MAT-STE-2026-25274")
	s0 = c1._watch_snapshot()
	r1 = create_and_run_isolated_riv(
		item, wh, posting_date=row[0].posting_date, posting_time=row[0].posting_time
	)
	frappe.db.commit()
	r2 = create_and_run_isolated_riv(
		item, wh, posting_date=row[0].posting_date, posting_time=row[0].posting_time
	)
	frappe.db.commit()
	consume = frappe.db.sql(
		"""SELECT actual_qty, outgoing_rate, valuation_rate, stock_value_difference
		FROM `tabStock Ledger Entry`
		WHERE voucher_no='MAT-STE-2026-25274' AND item_code=%s AND is_cancelled=0""",
		item,
		as_dict=True,
	)
	an1 = analyze_manufacture_scrap_fg("MAT-STE-2026-25274")
	s1 = c1._watch_snapshot()
	contract = _l4_contract_ok()
	out = {
		"riv1": r1,
		"riv2": r2,
		"class_before": an0.get("classification"),
		"class_after": an1.get("classification"),
		"consume": consume,
		"gates0": s0["gates"],
		"gates1": s1["gates"],
		"contract": contract,
	}
	_jdump("riv_30100294_valued_source.json", out)
	return {
		"riv1_ok": r1.get("ok"),
		"riv2_ok": r2.get("ok"),
		"class_before": an0.get("classification"),
		"class_after": an1.get("classification"),
		"consume": consume,
		"i1": s1["gates"].get("i1"),
		"neg_delta": int(s1["gates"].get("neg_after") or 0) - int(s0["gates"].get("neg_after") or 0),
		"contract_ok": contract.get("ok"),
		"ma_rate": contract.get("ma_rate"),
	}


def chain_30100294_after_c1() -> dict:
	"""Re-prove 24993 → 25274 on the reproduced Candidate-1 state."""
	rows = frappe.db.sql(
		"""SELECT posting_datetime, voucher_no, warehouse, actual_qty,
		          incoming_rate, outgoing_rate, valuation_rate,
		          stock_value_difference, stock_value, qty_after_transaction
		FROM `tabStock Ledger Entry`
		WHERE item_code='30100294' AND is_cancelled=0
		ORDER BY posting_datetime, creation""",
		as_dict=True,
	)
	first_zero = None
	for r in rows:
		if abs(flt(r.actual_qty)) > 1e-9 and abs(flt(r.stock_value_difference)) <= 1e-6:
			if abs(flt(r.incoming_rate)) <= 1e-6 and abs(flt(r.outgoing_rate)) <= 1e-6:
				first_zero = r
				break
	se = frappe.db.sql(
		"""SELECT item_code, qty, basic_rate, basic_amount, allow_zero_valuation_rate,
		          is_finished_item, is_scrap_item, s_warehouse, t_warehouse
		FROM `tabStock Entry Detail` WHERE parent='MAT-STE-2026-25274' ORDER BY idx""",
		as_dict=True,
	)
	out = {"sles": rows, "first_zero": first_zero, "se_25274": se}
	_jdump("chain_30100294_c1state.json", out)
	return {
		"sle_n": len(rows),
		"first_zero_voucher": first_zero.voucher_no if first_zero else None,
		"first_zero_dt": str(first_zero.posting_datetime) if first_zero else None,
	}


def kpis_after_fixes() -> dict:
	scan = c1.post_scan()
	_jdump("kpis_after_l4_25274.json", scan)
	dash = scan.get("dashboard") or {}
	return {
		"score": dash.get("Integrity Score"),
		"i1": dash.get("I1") or (scan.get("gates") or {}).get("i1"),
		"i4": dash.get("I4"),
		"wrong_rate": dash.get("Wrong Rate"),
		"zero_actionable": dash.get("Zero Rate"),
		"pz": dash.get("Patient Zero"),
		"failed_riv": dash.get("Failed RIV"),
		"gates": scan.get("gates"),
		"extra": scan.get("extra"),
		"36933": scan.get("36933"),
		"37090": scan.get("37090"),
	}


def ready_root_compression() -> dict:
	from collections import Counter

	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

	scan = scan_wrong_rates(company="اسپاد فارمد دارو", limit=5000)
	rows = [attach_plan(dict(r)) for r in (scan.get("rows") or [])]
	ready = [r for r in rows if str(r.get("planner_status") or "").startswith("READY")]
	identities = {}
	for r in ready:
		key = (
			r.get("item") or r.get("item_code"),
			r.get("warehouse") or r.get("s_warehouse"),
			r.get("batch") or r.get("batch_no") or "",
		)
		identities.setdefault(key, []).append(r)
	vouchers = {r.get("voucher") or r.get("voucher_no") for r in ready}
	families = Counter()
	roots = []
	for key, group in identities.items():
		r = group[0]
		src = str(r.get("source_of_truth") or r.get("rate_source") or r.get("strategy") or "")
		purpose = str(r.get("purpose") or "")
		if "Purchase Receipt" in src or src.startswith("PR") or purpose == "Purchase Receipt":
			fam = "PR"
		elif "SABB" in src.upper() or "sabb" in src:
			fam = "SABB"
		elif "Reco" in src or "RECO" in src or purpose == "Stock Reconciliation":
			fam = "Stock Reconciliation"
		elif purpose == "Material Transfer for Manufacture" or "MTFM" in src:
			fam = "MTFM"
		elif purpose == "Material Transfer":
			fam = "Material Transfer"
		elif purpose == "Manufacture":
			fam = "Manufacture"
		elif purpose == "Repack":
			fam = "Repack"
		elif "LEFTOVER" in src.upper() or "leftover" in src:
			fam = "LEFTOVER_MA"
		elif "VALUED_SOURCE" in src.upper():
			fam = "VALUED_SOURCE_ZERO_OUTGOING"
		elif "return" in src.lower() or purpose.endswith("Return"):
			fam = "Return"
		elif "moving" in src.lower() or src.upper() == "MA":
			fam = "Moving Average"
		else:
			fam = "other"
		families[fam] += 1
		allowed = fam in (
			"PR",
			"SABB",
			"Stock Reconciliation",
			"Material Transfer",
			"MTFM",
			"Return",
			"Moving Average",
			"Manufacture",
			"Repack",
			"LEFTOVER_MA",
			"VALUED_SOURCE_ZERO_OUTGOING",
		) and str(r.get("confidence") or "") in ("EXACT", "exact")
		role = "ROOT" if allowed else "WAITING_OR_MANUAL"
		roots.append(
			{
				"item": key[0],
				"warehouse": key[1],
				"batch": key[2],
				"voucher": r.get("voucher") or r.get("voucher_no"),
				"purpose": purpose,
				"source": src,
				"confidence": r.get("confidence"),
				"planner_status": r.get("planner_status"),
				"family": fam,
				"role": role,
				"row_n": len(group),
			}
		)
	causal = [r for r in roots if r["role"] == "ROOT"]
	out = {
		"ready_rows": len(ready),
		"unique_vouchers": len(vouchers),
		"unique_identities": len(identities),
		"causal_roots": len(causal),
		"families": dict(families),
		"roots": roots,
	}
	_jdump("ready_root_compression.json", out)
	return {
		"ready_rows": len(ready),
		"unique_vouchers": len(vouchers),
		"unique_identities": len(identities),
		"causal_roots": len(causal),
		"families": dict(families),
	}


def classify_qty0_nonzero() -> dict:
	from collections import Counter

	from erpnext_extensions.iran_accounting.domain.currency import get_company_currency, round_currency

	ccy = get_company_currency("اسپاد فارمد دارو")
	rows = frappe.db.sql(
		"""SELECT sle.name, sle.item_code, sle.warehouse, sle.voucher_no, sle.voucher_type,
		          sle.posting_datetime, sle.actual_qty, sle.qty_after_transaction,
		          sle.stock_value, sle.valuation_rate, sle.stock_value_difference
		FROM `tabStock Ledger Entry` sle
		WHERE sle.is_cancelled=0
		  AND ABS(sle.qty_after_transaction) < 0.0001
		  AND ABS(sle.stock_value) > 1e-6
		ORDER BY sle.posting_datetime, sle.creation""",
		as_dict=True,
	)
	# Terminal leftover: last SLE of item+warehouse still qty=0 with nonzero value
	last = frappe.db.sql(
		"""SELECT t.item_code, t.warehouse, t.voucher_no, t.qty_after_transaction, t.stock_value,
		          t.posting_datetime
		FROM `tabStock Ledger Entry` t
		INNER JOIN (
			SELECT item_code, warehouse, MAX(CONCAT(posting_datetime, creation)) mx
			FROM `tabStock Ledger Entry` WHERE is_cancelled=0
			GROUP BY item_code, warehouse
		) x ON x.item_code=t.item_code AND x.warehouse=t.warehouse
		  AND CONCAT(t.posting_datetime, t.creation)=x.mx
		WHERE t.is_cancelled=0
		  AND ABS(t.qty_after_transaction) < 0.0001
		  AND ABS(t.stock_value) > 1e-6""",
		as_dict=True,
	)
	terminal_keys = {(r.item_code, r.warehouse) for r in last}
	cats = Counter()
	dust_n = 0
	for r in rows:
		qty = flt(r.qty_after_transaction)
		rate = flt(r.valuation_rate)
		sv = flt(r.stock_value)
		implied = qty * rate
		envelope = abs(flt(round_currency(1, ccy))) + abs(flt(rate)) * 1e-6
		if (r.item_code, r.warehouse) not in terminal_keys or r.voucher_no not in {
			t.voucher_no for t in last if t.item_code == r.item_code and t.warehouse == r.warehouse
		}:
			cats["HISTORICAL_INTERMEDIATE_ROW"] += 1
		elif abs(sv - implied) <= envelope and abs(sv) <= envelope:
			cats["PRECISION_DUST"] += 1
			dust_n += 1
		else:
			cats["REAL_TERMINAL_LEFTOVER"] += 1
	out = {
		"raw": len(rows),
		"categories": dict(cats),
		"terminal_active": len(last),
		"terminal_rows": last[:40],
		"precision_dust": dust_n,
	}
	_jdump("qty0_nonzero_classify.json", out)
	return {
		"raw": len(rows),
		"categories": dict(cats),
		"terminal_active": len(last),
	}


def pz_families() -> dict:
	from collections import Counter

	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows

	company = "اسپاد فارمد دارو"
	zero = scan_zero_rate_rows(company=company, limit=5000)
	i4 = scan_i4_leftover(company=company, from_date="2026-03-21", to_date=str(frappe.utils.nowdate()), limit=5000)
	wrong = scan_wrong_rates(company=company, limit=5000)
	patients = {}

	def _add(topic, row):
		pz = row.get("patient_zero") or {}
		key = pz.get("voucher_no") if isinstance(pz, dict) else None
		if not key and topic == "I4" and row.get("i4_status") in (
			"READY_I4",
			"WAITING_I4",
			"MANUAL",
			"I4_REPLAY_REQUIRED",
		):
			key = row.get("voucher")
		if key:
			patients.setdefault(key, {"n": 0, "topics": set(), "items": set()})
			patients[key]["n"] += 1
			patients[key]["topics"].add(topic)
			if row.get("item") or row.get("item_code"):
				patients[key]["items"].add(row.get("item") or row.get("item_code"))

	for r in zero.get("rows") or []:
		_add("ZERO_RATE", r)
	for r in i4.get("rows") or []:
		_add("I4", r)
	for r in wrong.get("rows") or []:
		_add("WRONG_RATE", r)
	serial = []
	for k, v in sorted(patients.items(), key=lambda kv: (-kv[1]["n"], kv[0])):
		serial.append(
			{
				"voucher": k,
				"n": v["n"],
				"topics": sorted(v["topics"]),
				"items": sorted(v["items"])[:8],
			}
		)
	out = {"pz_n": len(serial), "families": serial}
	_jdump("pz_families.json", out)
	return {"pz_n": len(serial), "sample": serial[:20]}


def after_fix_reports() -> dict:
	ready = ready_root_compression()
	pz = pz_families()
	qty0 = classify_qty0_nonzero()
	out = {"ready": ready, "pz": pz, "qty0": qty0}
	_jdump("after_fix_reports.json", out)
	return out


def apply_ready_causal_wave(n: int = 1) -> dict:
	"""Apply n causal READY roots (source-side first). Stop on any gate fail."""
	from erpnext_extensions.iran_accounting.historical_stock._validation.prod_ready_0926 import (
		mfg_delta,
		rebuild_bins,
	)
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import execute, plan
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates

	comp = ready_root_compression()
	# Re-read dumped roots; drop Paykar when same item+batch exists elsewhere.
	import json

	full = json.loads((OUT / "ready_root_compression.json").read_text())
	roots = list(full.get("roots") or [])
	pairs = {(r["item"], r["batch"]) for r in roots if "پایکار" not in str(r.get("warehouse") or "")}
	causal = []
	for r in roots:
		if r.get("role") != "ROOT":
			continue
		# Campaign READY set is SABB provenance only. Do not pick
		# leftover-MA / test-fixture Material Transfer descendants.
		if r.get("family") != "SABB":
			continue
		item = str(r.get("item") or "")
		if item.startswith("2301") or item.startswith("RIV-"):
			continue
		if "پایکار" in str(r.get("warehouse") or "") and (r["item"], r["batch"]) in pairs:
			continue
		causal.append(r)
	# Deterministic: voucher then item
	causal.sort(key=lambda r: (str(r.get("voucher") or ""), str(r.get("item") or "")))
	chosen = causal[: int(n)]
	mfg0 = mfg_fingerprint()
	g0 = _gates()
	log = []
	for spec in chosen:
		scan = scan_wrong_rates(
			company="اسپاد فارمد دارو", voucher=spec["voucher"], item_code=spec["item"], limit=30
		)
		rows = [r for r in (scan.get("rows") or []) if str(r.get("planner_status") or "").startswith("READY")]
		if not rows:
			log.append({"voucher": spec["voucher"], "skipped": "not_ready_anymore"})
			continue
		row = next((r for r in rows if r.get("item") == spec.get("item")), rows[0])
		live = execute(plan(dict(row)), dry_run=False)
		frappe.db.commit()
		rebuild_bins()
		g = _gates()
		md = mfg_delta(mfg0, mfg_fingerprint())
		entry = {
			"voucher": spec["voucher"],
			"item": spec["item"],
			"ok": live.get("ok"),
			"state": live.get("primary_state"),
			"source": spec.get("source"),
			"i1": g.get("i1"),
			"neg_after": g.get("neg_after"),
			"neg_rate": g.get("neg_rate"),
			"mfg_delta": md,
		}
		log.append(entry)
		if (
			g.get("i1")
			or g.get("neg_rate")
			or int(g.get("neg_after") or 0) > int(g0.get("neg_after") or 0)
			or any(abs(flt(x)) > 1e-6 for x in md.values())
		):
			_jdump("ABORT_ready_wave.json", {"entry": entry, "log": log})
			return {"ok": False, "aborted": entry, "log": log}
	g1 = _gates()
	out = {"ok": True, "chosen": chosen, "log": log, "gates0": g0, "gates1": g1}
	_jdump("ready_causal_wave.json", out)
	return {"ok": True, "applied": len(log), "i1": g1.get("i1"), "neg_after": g1.get("neg_after")}


def _earliest(item, warehouse=None):
	if warehouse:
		return frappe.db.sql(
			"""SELECT item_code, posting_date, posting_time, warehouse FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			ORDER BY posting_datetime, creation LIMIT 1""",
			(item, warehouse),
			as_dict=True,
		)
	return frappe.db.sql(
		"""SELECT item_code, posting_date, posting_time, warehouse FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation LIMIT 1""",
		item,
		as_dict=True,
	)


def isolated_l5_representative() -> dict:
	"""Broad historical subset through isolated native RIV. Stop on first gate fail."""
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
		create_and_run_isolated_riv,
		first_non_opening_repost_boundary,
	)

	# Resolve one identity per required economic family.
	# Never open RIV from a fiscal-year opening Stock Reconciliation.
	reco = first_non_opening_repost_boundary(
		"30100022", "انبار Quarantine محصول نیمه ساخته اسپاد"
	)
	pr = frappe.db.sql(
		"""SELECT item_code, warehouse, posting_date, posting_time
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Purchase Receipt' AND is_cancelled=0
		ORDER BY posting_datetime, creation LIMIT 1""",
		as_dict=True,
	)
	ret = frappe.db.sql(
		"""SELECT sle.item_code, sle.warehouse, sle.posting_date, sle.posting_time
		FROM `tabStock Ledger Entry` sle
		JOIN `tabStock Entry` se ON se.name=sle.voucher_no
		WHERE sle.is_cancelled=0
		  AND (se.purpose LIKE '%%Return%%' OR IFNULL(se.is_return,0)=1)
		  AND sle.item_code NOT BETWEEN '230111' AND '230119'
		ORDER BY sle.posting_datetime LIMIT 1""",
		as_dict=True,
	)
	repack = frappe.db.sql(
		"""SELECT sle.item_code, sle.warehouse, sle.posting_date, sle.posting_time
		FROM `tabStock Ledger Entry` sle
		JOIN `tabStock Entry` se ON se.name=sle.voucher_no
		WHERE sle.is_cancelled=0 AND se.purpose='Repack'
		  AND sle.item_code NOT BETWEEN '230111' AND '230119'
		ORDER BY sle.posting_datetime LIMIT 1""",
		as_dict=True,
	)
	mt = frappe.db.sql(
		"""SELECT sle.item_code, sle.warehouse, sle.posting_date, sle.posting_time
		FROM `tabStock Ledger Entry` sle
		JOIN `tabStock Entry` se ON se.name=sle.voucher_no
		WHERE sle.is_cancelled=0 AND se.purpose='Material Transfer'
		  AND sle.item_code NOT BETWEEN '230111' AND '230119'
		  AND sle.item_code NOT IN ('16100066','16100226')
		ORDER BY sle.posting_datetime LIMIT 1""",
		as_dict=True,
	)
	families = [
		("PR", pr[0] if pr else None),
		("RECO", reco),
		("SABB_MTFM", _earliest("13100134", "انبار approved اقلام بسته بندی اولیه اسپاد")),
		("MT", mt[0] if mt else None),
		("MTFM", _earliest("13100023", "انبار approved اقلام بسته بندی اولیه اسپاد")),
		("RETURN", ret[0] if ret else None),
		("MFG_NO_SCRAP", frappe.db.sql(
			"""SELECT item_code, warehouse, posting_date, posting_time
			FROM `tabStock Ledger Entry` WHERE voucher_no='MAT-STE-2026-28696' AND is_cancelled=0
			ORDER BY posting_datetime LIMIT 1""",
			as_dict=True,
		)),
		("MFG_SCRAP", _earliest("20100067")),
		("REPACK", repack[0] if repack else None),
		("VALUED_SOURCE_ZERO_OUTGOING", _earliest("30100294", "انبار پایکار خط تولید اسپاد فارمد")),
		("I4", _earliest("30100022", "انبار پایکار خط تولید اسپاد فارمد")),
		("LEFTOVER_MA", _earliest("16100066", "انبار ملزومات مصرفی اسپاد")),
		("LEGITIMATE_ZERO", _earliest("16100226", "انبار ملزومات مصرفی اسپاد")),
	]
	s0 = c1._watch_snapshot()
	log = []
	stopped = None
	for name, row in families:
		if isinstance(row, list):
			row = row[0] if row else None
		if not row:
			log.append({"family": name, "skipped": "no_identity"})
			continue
		riv = create_and_run_isolated_riv(
			row.item_code if hasattr(row, "item_code") else row.get("item_code"),
			row.warehouse if hasattr(row, "warehouse") else row.get("warehouse"),
			posting_date=row.posting_date if hasattr(row, "posting_date") else row.get("posting_date"),
			posting_time=row.posting_time if hasattr(row, "posting_time") else row.get("posting_time"),
			allow_negative_stock=False,
			max_attempts=2,
		)
		frappe.db.commit()
		after = c1._watch_snapshot()
		neg_delta = int(after["gates"].get("neg_after") or 0) - int(s0["gates"].get("neg_after") or 0)
		entry = {
			"family": name,
			"item": row.item_code if hasattr(row, "item_code") else row.get("item_code"),
			"warehouse": row.warehouse if hasattr(row, "warehouse") else row.get("warehouse"),
			"ok": riv.get("ok"),
			"riv_status": riv.get("riv_status"),
			"reason": (riv.get("reason") or "")[:300],
			"i1": after["gates"].get("i1"),
			"neg_delta": neg_delta,
		}
		log.append(entry)
		if (not riv.get("ok")) or after["gates"].get("i1") or neg_delta > 0:
			stopped = entry
			break
	s1 = c1._watch_snapshot()
	out = {
		"passed": stopped is None,
		"stopped": stopped,
		"log": log,
		"gates0": s0["gates"],
		"gates1": s1["gates"],
		"36933": c1.snapshot_36933().get("native"),
		"37090": c1.reconstruct_37090_class(),
	}
	_jdump("isolated_l5.json", out)
	return {
		"passed": out["passed"],
		"stopped": stopped,
		"ok": [e.get("ok") for e in log],
		"i1": s1["gates"].get("i1"),
		"neg_after": s1["gates"].get("neg_after"),
	}


def run_c1_then_l4_experiment() -> dict:
	"""C1 mutations + PO + bounded L4 + 25274. Used after a fresh 22:50 restore."""
	from erpnext_extensions.iran_accounting.historical_stock._validation.prod_ready_0926 import (
		canaries,
	)

	base = c1.apply_baseline()
	i4 = c1.apply_36853_i4()
	l1 = c1.isolated_l1_canary()
	l2 = c1.isolated_l2_bounded()
	l3a = c1.isolated_l3_13100134_layered()
	l3b = c1.isolated_l3_second_pass()
	can = canaries()
	pre = {
		"30470_621301": (can.get("30470") or {}).get("uses_621301"),
		"30470_622515": (can.get("30470") or {}).get("uses_622515_for_stock"),
		"16100066": can.get("16100066"),
	}
	po = apply_ready_posting_order_wave()
	if not po.get("ok"):
		out = {"ok": False, "stage": "po", "po": po, "pre": pre}
		_jdump("c1_l4_experiment.json", out)
		return out
	l4a = isolated_l4_30100022()
	if not l4a.get("passed"):
		out = {"ok": False, "stage": "l4a", "l4a": l4a, "pre": pre, "po": po}
		_jdump("c1_l4_experiment.json", out)
		return out
	l4b = isolated_l4_30100022()
	post = post_l4_canaries()
	v25274 = isolated_riv_30100294_valued_source()
	scan = kpis_after_fixes()
	ok = bool(
		l4a.get("passed")
		and l4b.get("passed")
		and (post.get("contract") or {}).get("ok")
		and v25274.get("riv1_ok")
		and v25274.get("neg_delta") == 0
		and int((scan.get("gates") or {}).get("i1") or 0) == 0
		and int((scan.get("gates") or {}).get("neg_after") or 0) == 0
	)
	out = {
		"ok": ok,
		"baseline": (base.get("rehearsal") or {}).get("applied_ok"),
		"i4": i4,
		"l1": l1,
		"l2": l2,
		"l3a": l3a,
		"l3b": l3b,
		"pre": pre,
		"po": po,
		"l4a": l4a,
		"l4b": l4b,
		"post": post,
		"v25274": v25274,
		"scan": scan,
	}
	_jdump("c1_l4_experiment.json", out)
	return {
		"ok": ok,
		"score": scan.get("score"),
		"l4a": l4a.get("passed"),
		"l4b": l4b.get("passed"),
		"contract": post.get("contract"),
		"class_25274": v25274.get("class_after"),
		"wrong_rate": scan.get("wrong_rate"),
		"i1": (scan.get("gates") or {}).get("i1"),
		"neg_after": (scan.get("gates") or {}).get("neg_after"),
	}


def run_clean_final_replay_c2(tag: str = "A") -> dict:
	"""Candidate-2 proven sequence from a fresh 22:50 restore. No exploration."""
	c1_out = c1.run_clean_final_replay(tag=f"{tag}_C1")
	if not c1_out.get("ok"):
		out = {"ok": False, "tag": tag, "stage": "c1_baseline", "c1": c1_out}
		_jdump(f"clean_final_replay_c2_{tag}.json", out)
		return out
	po = apply_ready_posting_order_wave()
	if not po.get("ok"):
		out = {"ok": False, "tag": tag, "stage": "po", "po": po}
		_jdump(f"clean_final_replay_c2_{tag}.json", out)
		return out
	l4a = isolated_l4_30100022()
	if not l4a.get("passed"):
		out = {"ok": False, "tag": tag, "stage": "l4a", "l4a": l4a}
		_jdump(f"clean_final_replay_c2_{tag}.json", out)
		return out
	l4b = isolated_l4_30100022()
	if not l4b.get("passed"):
		out = {"ok": False, "tag": tag, "stage": "l4b", "l4b": l4b}
		_jdump(f"clean_final_replay_c2_{tag}.json", out)
		return out
	v25274 = isolated_riv_30100294_valued_source()
	if not (v25274.get("riv1_ok") and v25274.get("riv2_ok") and v25274.get("neg_delta") == 0):
		out = {"ok": False, "tag": tag, "stage": "25274", "v25274": v25274}
		_jdump(f"clean_final_replay_c2_{tag}.json", out)
		return out
	waves = []
	for n in (1, 2, 2, 5, 10, 10):
		w = apply_ready_causal_wave(n)
		waves.append(w)
		if not w.get("ok"):
			out = {"ok": False, "tag": tag, "stage": "ready_wave", "waves": waves}
			_jdump(f"clean_final_replay_c2_{tag}.json", out)
			return out
	l5a = isolated_l5_representative()
	if not l5a.get("passed"):
		out = {"ok": False, "tag": tag, "stage": "l5a", "l5a": l5a}
		_jdump(f"clean_final_replay_c2_{tag}.json", out)
		return out
	l5b = isolated_l5_representative()
	if not l5b.get("passed"):
		out = {"ok": False, "tag": tag, "stage": "l5b", "l5b": l5b}
		_jdump(f"clean_final_replay_c2_{tag}.json", out)
		return out
	scan = kpis_after_fixes()
	ok = (
		int((scan.get("gates") or {}).get("i1") or 0) == 0
		and int((scan.get("gates") or {}).get("neg_after") or 0) == 0
		and not (scan.get("gates") or {}).get("neg_rate")
		and scan.get("36933") in ("HEALTHY",)
		and scan.get("37090") == "HEALTHY"
	)
	out = {
		"ok": ok,
		"tag": tag,
		"c1": c1_out,
		"po": po,
		"l4a": l4a,
		"l4b": l4b,
		"v25274": v25274,
		"waves": waves,
		"l5a": l5a,
		"l5b": l5b,
		"scan": scan,
	}
	_jdump(f"clean_final_replay_c2_{tag}.json", out)
	return {
		"ok": ok,
		"tag": tag,
		"score": scan.get("score"),
		"wrong_rate": scan.get("wrong_rate"),
		"zero_actionable": scan.get("zero_actionable"),
		"pz": scan.get("pz"),
		"i1": (scan.get("gates") or {}).get("i1"),
		"neg_after": (scan.get("gates") or {}).get("neg_after"),
	}


def cleanup_test_fixture_pollution() -> dict:
	"""Cancel rate-first integration fixtures accidentally posted on the campaign site."""
	ses = frappe.db.sql(
		"""SELECT name FROM `tabStock Entry`
		   WHERE name BETWEEN 'MAT-STE-2026-37771' AND 'MAT-STE-2026-37786'
		   ORDER BY name DESC""",
		as_dict=True,
	)
	cancelled = []
	for r in ses:
		doc = frappe.get_doc("Stock Entry", r.name)
		if int(doc.docstatus) == 1:
			doc.cancel()
			cancelled.append(r.name)
	frappe.db.sql(
		"""UPDATE `tabRepost Item Valuation`
		   SET status='Skipped'
		   WHERE status IN ('Queued','In Progress')
		     AND item_code BETWEEN '230111' AND '230119'"""
	)
	frappe.db.commit()
	open_n = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabRepost Item Valuation`
		   WHERE status IN ('Queued','In Progress')"""
	)[0][0]
	return {"ok": True, "cancelled": cancelled, "open_riv": open_n}
