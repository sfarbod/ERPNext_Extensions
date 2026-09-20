# Copyright (c) 2026, ERPNext Extensions contributors
"""Negative Stock Root Report — human investigation aid (read-only, v5.3.0).

Groups historical negative qty_after_transaction chains by ITEM → BATCH →
WAREHOUSE → FIRST NEGATIVE EVENT. Classifies AUTO_REPAIRABLE /
REPOST_HEALABLE / POSTING_ORDER_REPAIRABLE / BIN_STALE /
HISTORICAL_NEGATIVE_REQUIRES_USER / AMBIGUOUS.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

import frappe
from frappe.utils import flt, get_datetime

from erpnext_extensions.iran_accounting.historical_stock import QTY_EPS, VALUE_EPS

AUTO_REPAIRABLE = "AUTO_REPAIRABLE"
REPOST_HEALABLE = "REPOST_HEALABLE"
POSTING_ORDER_REPAIRABLE = "POSTING_ORDER_REPAIRABLE"
BIN_STALE = "BIN_STALE"
HISTORICAL_NEGATIVE_REQUIRES_USER = "HISTORICAL_NEGATIVE_REQUIRES_USER"
AMBIGUOUS = "AMBIGUOUS"


def build_negative_stock_root_report(company=None, limit_chains: int = 500) -> dict:
	"""Read-only report of unresolved negative-stock chains."""
	conds = ["sle.is_cancelled=0", "sle.qty_after_transaction < -%s"]
	args: list = [QTY_EPS]
	if company:
		conds.append("sle.company=%s")
		args.append(company)

	# First negative SLE per (item, warehouse, batch)
	rows = frappe.db.sql(
		f"""
		SELECT sle.name, sle.item_code, sle.warehouse, sle.batch_no,
		       sle.posting_date, sle.posting_time, sle.posting_datetime,
		       sle.voucher_type, sle.voucher_no, sle.actual_qty,
		       sle.qty_after_transaction, sle.stock_value, sle.stock_value_difference,
		       sle.incoming_rate, sle.outgoing_rate, sle.valuation_rate,
		       i.item_name
		FROM `tabStock Ledger Entry` sle
		LEFT JOIN `tabItem` i ON i.name = sle.item_code
		WHERE {" AND ".join(conds)}
		ORDER BY sle.item_code, sle.warehouse, IFNULL(sle.batch_no,''),
		         sle.posting_datetime, sle.creation
		LIMIT {int(limit_chains) * 20}
		""",
		args,
		as_dict=True,
	)

	chains: dict[tuple, dict] = {}
	for r in rows:
		key = (r.item_code, r.warehouse, r.batch_no or "")
		if key in chains:
			chains[key]["negative_sle_count"] += 1
			continue
		prev = _previous_sle(r)
		nxt = _next_inbound(r)
		bin_row = frappe.db.get_value(
			"Bin",
			{"item_code": r.item_code, "warehouse": r.warehouse},
			["actual_qty", "stock_value", "valuation_rate"],
			as_dict=True,
		) or {}
		qty_before = flt(prev.qty_after_transaction) if prev else None
		gap = None
		if nxt and r.posting_datetime and nxt.posting_datetime:
			try:
				gap = (
					get_datetime(nxt.posting_datetime) - get_datetime(r.posting_datetime)
				).total_seconds()
			except Exception:
				gap = None
		chronology_candidate = bool(
			nxt
			and gap is not None
			and 0 < gap <= 120
			and flt(nxt.actual_qty) > QTY_EPS
			and qty_before is not None
			and qty_before + flt(r.actual_qty) < -QTY_EPS
			and qty_before + flt(nxt.actual_qty) >= -QTY_EPS
		)
		bin_qty = flt(bin_row.get("actual_qty"))
		historical_still_neg = True  # by construction of query
		bin_merely_stale = historical_still_neg and bin_qty >= -QTY_EPS
		classification = _classify_chain(
			chronology_candidate=chronology_candidate,
			bin_merely_stale=bin_merely_stale,
			next_inbound=bool(nxt),
			gap=gap,
			qty_before=qty_before,
			txn_qty=flt(r.actual_qty),
		)
		chains[key] = {
			"item_code": r.item_code,
			"item_name": r.item_name,
			"batch_no": r.batch_no,
			"warehouse": r.warehouse,
			"first_negative_posting_date": str(r.posting_date),
			"first_negative_posting_time": str(r.posting_time),
			"first_negative_datetime": str(r.posting_datetime),
			"voucher_type": r.voucher_type,
			"voucher_no": r.voucher_no,
			"qty_before": qty_before,
			"transaction_qty": flt(r.actual_qty),
			"qty_after": flt(r.qty_after_transaction),
			"previous_voucher": prev.voucher_no if prev else None,
			"previous_voucher_type": prev.voucher_type if prev else None,
			"next_inbound_voucher": nxt.voucher_no if nxt else None,
			"next_inbound_qty": flt(nxt.actual_qty) if nxt else None,
			"time_gap_to_next_inbound_seconds": gap,
			"chronology_reorder_could_solve": chronology_candidate,
			"controlled_repost_could_solve": classification in (REPOST_HEALABLE, AUTO_REPAIRABLE),
			"bin_merely_stale": bin_merely_stale,
			"bin_actual_qty": bin_qty,
			"bin_stock_value": flt(bin_row.get("stock_value")),
			"historical_sle_truly_negative": historical_still_neg,
			"user_action_required": classification == HISTORICAL_NEGATIVE_REQUIRES_USER,
			"classification": classification,
			"negative_sle_count": 1,
			"first_sle": r.name,
		}
		if len(chains) >= int(limit_chains):
			break

	by_class = defaultdict(int)
	for c in chains.values():
		by_class[c["classification"]] += 1

	return {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"company": company,
		"chain_count": len(chains),
		"by_classification": dict(by_class),
		"auto_repairable": by_class[AUTO_REPAIRABLE],
		"repost_healable": by_class[REPOST_HEALABLE],
		"posting_order_repairable": by_class[POSTING_ORDER_REPAIRABLE],
		"bin_stale": by_class[BIN_STALE],
		"requires_user": by_class[HISTORICAL_NEGATIVE_REQUIRES_USER],
		"ambiguous": by_class[AMBIGUOUS],
		"chains": list(chains.values()),
		"read_only": True,
	}


def _previous_sle(r) -> dict | None:
	rows = frappe.db.sql(
		"""
		SELECT name, voucher_type, voucher_no, actual_qty, qty_after_transaction,
		       posting_datetime, posting_date, posting_time
		FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0
		  AND item_code=%s AND warehouse=%s
		  AND IFNULL(batch_no,'')=IFNULL(%s,'')
		  AND (posting_datetime < %s
		       OR (posting_datetime = %s AND creation < (
		             SELECT creation FROM `tabStock Ledger Entry` WHERE name=%s
		           )))
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 1
		""",
		(r.item_code, r.warehouse, r.batch_no or "", r.posting_datetime, r.posting_datetime, r.name),
		as_dict=True,
	)
	return rows[0] if rows else None


def _next_inbound(r) -> dict | None:
	rows = frappe.db.sql(
		"""
		SELECT name, voucher_type, voucher_no, actual_qty, qty_after_transaction,
		       posting_datetime, posting_date, posting_time
		FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0
		  AND item_code=%s AND warehouse=%s
		  AND IFNULL(batch_no,'')=IFNULL(%s,'')
		  AND actual_qty > %s
		  AND posting_datetime >= %s
		  AND name != %s
		ORDER BY posting_datetime ASC, creation ASC
		LIMIT 1
		""",
		(r.item_code, r.warehouse, r.batch_no or "", QTY_EPS, r.posting_datetime, r.name),
		as_dict=True,
	)
	return rows[0] if rows else None


def _classify_chain(
	*,
	chronology_candidate: bool,
	bin_merely_stale: bool,
	next_inbound: bool,
	gap,
	qty_before,
	txn_qty,
) -> str:
	if bin_merely_stale and not chronology_candidate:
		# Historical SLE negative but Bin recovered — often stale Bin or healed chain.
		# Prefer BIN_STALE when current Bin is non-negative.
		return BIN_STALE
	if chronology_candidate:
		return POSTING_ORDER_REPAIRABLE
	if next_inbound and gap is not None and gap <= 3600:
		return REPOST_HEALABLE
	if qty_before is not None and qty_before >= -QTY_EPS and txn_qty < 0 and abs(qty_before + txn_qty) > QTY_EPS:
		# First outbound exceeded available — true shortage / missing inbound.
		return HISTORICAL_NEGATIVE_REQUIRES_USER
	if next_inbound:
		return AMBIGUOUS
	return HISTORICAL_NEGATIVE_REQUIRES_USER
