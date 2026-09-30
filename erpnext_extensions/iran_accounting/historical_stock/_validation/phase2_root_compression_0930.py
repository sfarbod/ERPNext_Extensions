# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase-2 root compression on fresh Sep-30 restore (read-only)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock._validation.campaign_225046_c2 import (
	classify_qty0_nonzero,
)
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.cross_time import (
	classify_cross_time,
)

OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/"
	"private/files/hr_correction_20260930"
)


def compress_broken_bin() -> dict:
	rows = frappe.db.sql(
		"""
		SELECT b.item_code, b.warehouse,
		       ROUND(IFNULL(b.actual_qty,0),6) bin_qty,
		       ROUND(IFNULL(b.stock_value,0),2) bin_sv,
		       ROUND(IFNULL(b.valuation_rate,0),2) bin_vr,
		       ROUND(IFNULL(sle.qty_after_transaction,0),6) sle_qty,
		       ROUND(IFNULL(sle.stock_value,0),2) sle_sv,
		       ROUND(IFNULL(sle.valuation_rate,0),2) sle_vr,
		       sle.voucher_no last_voucher,
		       sle.posting_datetime last_posting
		FROM `tabBin` b
		LEFT JOIN (
		  SELECT t.item_code, t.warehouse, t.qty_after_transaction, t.stock_value, t.valuation_rate,
		         t.voucher_no, t.posting_datetime
		  FROM `tabStock Ledger Entry` t
		  INNER JOIN (
		    SELECT item_code, warehouse,
		           MAX(CONCAT(DATE_FORMAT(posting_datetime,'%%Y-%%m-%%d %%H:%%i:%%s'), '#', creation)) mx
		    FROM `tabStock Ledger Entry` WHERE is_cancelled=0
		    GROUP BY item_code, warehouse
		  ) x ON x.item_code=t.item_code AND x.warehouse=t.warehouse
		     AND CONCAT(DATE_FORMAT(t.posting_datetime,'%%Y-%%m-%%d %%H:%%i:%%s'), '#', t.creation)=x.mx
		  WHERE t.is_cancelled=0
		) sle ON sle.item_code=b.item_code AND sle.warehouse=b.warehouse
		WHERE sle.item_code IS NOT NULL
		  AND (
		    ABS(IFNULL(b.actual_qty,0) - IFNULL(sle.qty_after_transaction,0)) > 0.0001
		    OR ABS(IFNULL(b.stock_value,0) - IFNULL(sle.stock_value,0)) > 1
		  )
		""",
		as_dict=True,
	)
	kinds = Counter()
	statuses = Counter()
	roots = []
	for r in rows:
		qty_bad = abs(flt(r.bin_qty) - flt(r.sle_qty)) > 0.0001
		sv_bad = abs(flt(r.bin_sv) - flt(r.sle_sv)) > 1
		if qty_bad and sv_bad:
			kind = "QTY_AND_VALUE_STALE"
		elif qty_bad:
			kind = "QTY_STALE"
		else:
			kind = "VALUE_STALE"
		sle_unhealthy = (abs(flt(r.sle_qty)) < 1e-9 and abs(flt(r.sle_sv)) > 1) or (
			flt(r.sle_qty) > 0 and abs(flt(r.sle_sv)) > 1 and abs(flt(r.sle_vr)) < 1e-9
		)
		status = "WAITING_UPSTREAM" if sle_unhealthy else "AUTO_REPAIRABLE"
		kinds[kind] += 1
		statuses[status] += 1
		roots.append({**r, "kind": kind, "status": status})
	return {
		"raw_pairs": len(rows),
		"unique_item_wh": len({(r.item_code, r.warehouse) for r in rows}),
		"by_kind": dict(kinds),
		"by_status": dict(statuses),
		"sample": roots[:20],
	}


def leftover_ma_terminals(limit=30) -> list:
	return frappe.db.sql(
		"""
		SELECT t.item_code, t.warehouse,
		       ROUND(t.qty_after_transaction,6) q,
		       ROUND(t.stock_value,2) sv,
		       ROUND(t.valuation_rate,2) vr,
		       t.voucher_no
		FROM `tabStock Ledger Entry` t
		INNER JOIN (
		  SELECT item_code, warehouse,
		         MAX(CONCAT(DATE_FORMAT(posting_datetime,'%%Y-%%m-%%d %%H:%%i:%%s'), '#', creation)) mx
		  FROM `tabStock Ledger Entry` WHERE is_cancelled=0
		  GROUP BY item_code, warehouse
		) x ON x.item_code=t.item_code AND x.warehouse=t.warehouse
		   AND CONCAT(DATE_FORMAT(t.posting_datetime,'%%Y-%%m-%%d %%H:%%i:%%s'), '#', t.creation)=x.mx
		WHERE t.is_cancelled=0
		  AND t.qty_after_transaction > 0
		  AND t.stock_value > 1
		  AND ABS(t.valuation_rate) < 1e-9
		LIMIT %(limit)s
		""",
		{"limit": limit},
		as_dict=True,
	)


def last_sle(item: str) -> list:
	return frappe.db.sql(
		"""
		SELECT warehouse,
		       ROUND(qty_after_transaction,6) q,
		       ROUND(stock_value,2) sv,
		       ROUND(valuation_rate,2) vr,
		       voucher_no, posting_datetime
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND is_cancelled=0
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 1
		""",
		item,
		as_dict=True,
	)


def run() -> dict:
	OUT.mkdir(parents=True, exist_ok=True)
	i4 = classify_qty0_nonzero()
	ct = classify_cross_time(
		{
			"detection": "CROSS_TIME",
			"status": "ELIGIBLE",
			"planner_status": "READY",
			"outbound_work_order": "MFG-WO-2026-00731",
			"inbound_work_order": "MFG-WO-2026-00727",
			"batch": "938-13100057-2218002482",
			"inbound_qty": 1052,
			"outbound_qty": 6203,
		}
	)
	out = {
		"code": {"version": "5.3.37", "head": "e8b7a92"},
		"broken_bin": compress_broken_bin(),
		"i4_qty0": i4,
		"cross_time_33163_33338": ct,
		"leftover_ma_terminal_sample": leftover_ma_terminals(),
		"canary_16100066": last_sle("16100066"),
		"canary_16100226": last_sle("16100226"),
		"source_36934": frappe.db.sql(
			"""
			SELECT item_code, qty, ROUND(basic_rate) br, valuation_type,
			       secondary_item_type, is_finished_item
			FROM `tabStock Entry Detail`
			WHERE parent='MAT-STE-2026-36934'
			  AND item_code IN ('20100008','30500009')
			""",
			as_dict=True,
		),
		"user_business_rule_30500009": {
			"satisfied": False,
			"classification": "BUSINESS_RULE_MISMATCH",
			"detail": "literal stock-UOM rate equality NO; Iran equivalent-unit YES",
		},
	}
	(OUT / "phase2_root_compression.json").write_text(
		json.dumps(out, indent=2, default=str, ensure_ascii=False),
		encoding="utf-8",
	)
	return {
		"broken_bin": {
			k: out["broken_bin"][k]
			for k in ("raw_pairs", "unique_item_wh", "by_kind", "by_status")
		},
		"i4": i4,
		"cross_time": ct,
		"lma_n": len(out["leftover_ma_terminal_sample"]),
		"canary_16100066": out["canary_16100066"],
		"canary_16100226": out["canary_16100226"],
		"source_36934": out["source_36934"],
	}
