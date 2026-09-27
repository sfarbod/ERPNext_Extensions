# Copyright (c) 2026, ERPNext Extensions contributors
"""General CROSS_TIME planner. No voucher/item hard-codes."""

from __future__ import annotations

from datetime import datetime, timedelta

LEGITIMATE_ORDER = "LEGITIMATE_ORDER"
CROSS_TIME_EXACT = "CROSS_TIME_EXACT"
WAITING_UPSTREAM = "WAITING_UPSTREAM"
MANUAL_BUSINESS_EVIDENCE_REQUIRED = "MANUAL_BUSINESS_EVIDENCE_REQUIRED"
CROSS_ITEM_CONFLICT = "CROSS_ITEM_CONFLICT"
TECHNICAL_TOOL_GAP = "TECHNICAL_TOOL_GAP"

_MIN_SHIFT = timedelta(seconds=1)


def _parse_dt(value) -> datetime | None:
	if value is None:
		return None
	if isinstance(value, datetime):
		return value
	text = str(value).strip()
	for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
		try:
			return datetime.strptime(text[:26], fmt)
		except ValueError:
			continue
	return None


def same_manufacturing_family(outbound: dict, inbound: dict) -> bool:
	"""True when both legs share a Work Order, Job Card, or canonical batch."""
	ow = str(outbound.get("work_order") or "").strip()
	iw = str(inbound.get("work_order") or "").strip()
	if ow and iw and ow == iw:
		return True
	oj = str(outbound.get("job_card") or "").strip()
	ij = str(inbound.get("job_card") or "").strip()
	if oj and ij and oj == ij:
		return True
	ob = str(outbound.get("batch") or outbound.get("batch_no") or "").strip()
	ib = str(inbound.get("batch") or inbound.get("batch_no") or "").strip()
	if ob and ib and ob == ib:
		return True
	return False


def quantity_conserved(inbound_qty, outbound_qty, remainder=0, eps=1e-6) -> bool:
	try:
		inn = abs(float(inbound_qty or 0))
		out = abs(float(outbound_qty or 0))
		rem = float(remainder or 0)
	except (TypeError, ValueError):
		return False
	return abs(inn - out - rem) <= eps


def minimum_timestamp_shift(outbound_dt, inbound_dt) -> dict:
	"""Smallest +1s move that places outbound strictly after inbound."""
	out = _parse_dt(outbound_dt)
	inn = _parse_dt(inbound_dt)
	if not out or not inn:
		return {"ok": False, "seconds": 0, "new_outbound": None, "reason": "unparseable_datetime"}
	if out > inn:
		return {
			"ok": True,
			"seconds": 0,
			"old_outbound": out.isoformat(sep=" "),
			"new_outbound": out.isoformat(sep=" "),
			"reason": "already_after_inbound",
		}
	new = inn + _MIN_SHIFT
	delta = int((new - out).total_seconds())
	return {
		"ok": True,
		"seconds": delta,
		"old_outbound": out.isoformat(sep=" "),
		"new_outbound": new.isoformat(sep=" "),
		"reason": "minimum_plus_one_second_after_inbound",
	}


def classify_cross_time(row: dict) -> dict:
	"""Classify one posting-order finding without item/voucher literals."""
	detection = str(row.get("detection") or "")
	status = str(row.get("status") or row.get("optimizer_status") or "")
	planner = str(row.get("planner_status") or "")
	if detection != "CROSS_TIME" or status in ("NO_REPAIR_NEEDED",):
		return {"class": LEGITIMATE_ORDER, "repair": False}
	outbound = {
		"work_order": row.get("outbound_work_order") or row.get("work_order"),
		"job_card": row.get("outbound_job_card") or row.get("job_card"),
		"batch": row.get("batch") or row.get("batch_no"),
	}
	inbound = {
		"work_order": row.get("inbound_work_order"),
		"job_card": row.get("inbound_job_card"),
		"batch": row.get("inbound_batch") or row.get("batch") or row.get("batch_no"),
	}
	family = same_manufacturing_family(outbound, inbound)
	conserved = quantity_conserved(
		row.get("inbound_qty"),
		row.get("outbound_qty"),
		row.get("remainder_qty") or 0,
	)
	if status == "CROSS_ITEM_CONFLICT" and not family:
		return {
			"class": TECHNICAL_TOOL_GAP,
			"repair": False,
			"reason": "cross_item_or_unrelated_job_pairing",
		}
	if not family:
		owo, iwo = outbound.get("work_order"), inbound.get("work_order")
		if owo and iwo and owo != iwo:
			return {
				"class": MANUAL_BUSINESS_EVIDENCE_REQUIRED,
				"repair": False,
				"reason": "different_work_orders_unproven_same_lot",
			}
		return {"class": LEGITIMATE_ORDER, "repair": False, "reason": "no_shared_family"}
	if planner.startswith("WAITING") or status == "WAITING_UPSTREAM":
		return {"class": WAITING_UPSTREAM, "repair": False}
	if family and conserved and planner.startswith("READY"):
		shift = minimum_timestamp_shift(row.get("outbound_datetime"), row.get("inbound_datetime"))
		return {
			"class": CROSS_TIME_EXACT,
			"repair": True,
			"shift": shift,
			"reason": "shared_family_and_qty_conserved",
		}
	if family and planner.startswith("READY"):
		shift = minimum_timestamp_shift(row.get("outbound_datetime"), row.get("inbound_datetime"))
		return {
			"class": CROSS_TIME_EXACT if shift.get("ok") else TECHNICAL_TOOL_GAP,
			"repair": bool(shift.get("ok")),
			"shift": shift,
			"reason": "shared_family_ready_window",
		}
	if "beyond_30min" in str(row.get("search_window") or "") and family:
		return {"class": TECHNICAL_TOOL_GAP, "repair": False, "reason": "beyond_30min_needs_warehouse_replay"}
	return {"class": TECHNICAL_TOOL_GAP, "repair": False, "reason": planner or status or "unclassified"}
