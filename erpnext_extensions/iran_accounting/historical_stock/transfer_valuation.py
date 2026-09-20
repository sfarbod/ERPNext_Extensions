# Copyright (c) 2026, ERPNext Extensions contributors
"""Authoritative Transfer valuation reconstruction (v5.3.0 Phase 5B).

A Material Transfer does not create new economic value. Target valuation must
originate from the source stock movement at the transaction point.

Returns structured evidence — never invents a rate from live Bin or unrelated
later moving-average.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_AMBIGUOUS,
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	CONFIDENCE_MANUAL,
	QTY_EPS,
	RATE_EPS,
	VALUE_EPS,
)
from erpnext_extensions.iran_accounting.historical_stock.util import g
from erpnext_extensions.iran_accounting.stock_posting_order.replay import sle_poison_reason

TRANSFER_PURPOSES = frozenset(
	{
		"Material Transfer",
		"Material Transfer for Manufacture",
		"Send to Subcontractor",
	}
)

WAITING_UPSTREAM = "WAITING_UPSTREAM"
EXACT = "EXACT"
RECONSTRUCTABLE = "RECONSTRUCTABLE"
USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
TOOL_LIMIT = "TOOL_LIMIT"
NO_ACTION = "NO_ACTION"


def reconstruct_transfer_valuation(row: dict, *, cache: dict | None = None) -> dict[str, Any]:
	"""Reconstruct expected target valuation from authoritative source movement.

	``row`` may be a Stock Entry Detail dict or a classified Wrong/Zero row.
	"""
	purpose = g(row, "purpose") or ""
	voucher = g(row, "parent") or g(row, "voucher") or g(row, "voucher_no")
	item = g(row, "item_code") or g(row, "item")
	s_wh = g(row, "s_warehouse")
	t_wh = g(row, "t_warehouse")
	# Wrong Rate target identity may already be collapsed to warehouse.
	if not s_wh and not t_wh:
		t_wh = g(row, "warehouse")
	qty = flt(g(row, "qty") or g(row, "transfer_qty") or g(row, "actual_qty"))
	batch = g(row, "batch_no") or g(row, "batch")
	posting_date = g(row, "posting_date")
	posting_time = g(row, "posting_time") or "00:00:00"
	posting_dt = g(row, "posting_datetime") or f"{posting_date} {posting_time}"
	current_rate = flt(
		g(row, "basic_rate")
		if g(row, "basic_rate") is not None
		else g(row, "current_rate")
		if g(row, "current_rate") is not None
		else g(row, "valuation_rate")
		if g(row, "valuation_rate") is not None
		else g(row, "incoming_rate")
	)

	out: dict[str, Any] = {
		"purpose": purpose,
		"voucher": voucher,
		"item": item,
		"s_warehouse": s_wh,
		"t_warehouse": t_wh,
		"batch": batch,
		"qty": qty,
		"posting_datetime": posting_dt,
		"current_rate": current_rate,
		"expected_rate": 0.0,
		"expected_stock_value_difference": 0.0,
		"authoritative_source": None,
		"confidence": CONFIDENCE_MANUAL,
		"classification": TOOL_LIMIT,
		"root_voucher": voucher,
		"upstream_health": "unknown",
		"dependency_closure": [],
		"reason": "",
		"evidence": {},
	}

	if purpose and purpose not in TRANSFER_PURPOSES and purpose not in (
		"Material Issue",
		"Material Consumption for Manufacture",
		"Repack",
	):
		out["reason"] = f"not a transfer-family purpose ({purpose})"
		out["classification"] = NO_ACTION
		return out

	if not voucher or not item:
		out["reason"] = "missing voucher or item"
		out["classification"] = TOOL_LIMIT
		return out

	# --- Outgoing SLE on this voucher (source warehouse) ---
	out_sle = _outgoing_sle(voucher, item, s_wh, batch=batch)
	in_sle = _incoming_sle(voucher, item, t_wh, batch=batch)

	# Source rate from outgoing SVD / qty (authoritative economic value leaving source).
	source_rate = 0.0
	source_kind = None
	if out_sle:
		oq = flt(out_sle.actual_qty)
		svd = flt(out_sle.stock_value_difference)
		orate = flt(out_sle.outgoing_rate)
		if abs(oq) > QTY_EPS and abs(svd) > VALUE_EPS:
			source_rate = abs(svd / oq)
			source_kind = "outgoing_sle_svd"
		elif abs(orate) > RATE_EPS:
			source_rate = abs(orate)
			source_kind = "outgoing_sle_rate"
		out["evidence"]["outgoing_sle"] = {
			"name": out_sle.name,
			"warehouse": out_sle.warehouse,
			"actual_qty": oq,
			"outgoing_rate": orate,
			"stock_value_difference": svd,
			"qty_after": out_sle.qty_after_transaction,
			"stock_value": out_sle.stock_value,
		}

	# Fallback: previous healthy SLE at source warehouse before posting.
	if abs(source_rate) <= RATE_EPS and s_wh and posting_date:
		prev = _previous_healthy(item, s_wh, posting_date, posting_time)
		if prev and abs(flt(prev.get("rate"))) > RATE_EPS:
			source_rate = flt(prev["rate"])
			source_kind = "previous_healthy_source_sle"
			out["evidence"]["previous_source_sle"] = prev

	# Upstream health at source warehouse
	upstream = _source_upstream_health(item, s_wh, posting_dt, batch=batch, cache=cache)
	out["upstream_health"] = upstream.get("status") or "unknown"
	out["dependency_closure"] = list(upstream.get("dependencies") or [])
	if upstream.get("root_voucher"):
		out["root_voucher"] = upstream["root_voucher"]

	if upstream.get("status") == "poisoned":
		out["classification"] = WAITING_UPSTREAM
		out["confidence"] = CONFIDENCE_AMBIGUOUS
		out["reason"] = (
			f"source warehouse valuation unhealthy before transfer "
			f"(root={upstream.get('root_voucher')}: {upstream.get('reason')})"
		)
		out["authoritative_source"] = source_kind
		out["expected_rate"] = 0.0
		return out

	if abs(source_rate) <= RATE_EPS:
		# No inventable source — may be Material Issue with empty stock (user) or tool gap.
		out["classification"] = (
			USER_ACTION_REQUIRED
			if purpose == "Material Issue"
			else WAITING_UPSTREAM
			if upstream.get("status") == "missing"
			else TOOL_LIMIT
		)
		out["confidence"] = CONFIDENCE_MANUAL
		out["reason"] = "no authoritative source valuation found for transfer/issue"
		return out

	# Expected target
	expected = source_rate
	out["expected_rate"] = expected
	out["expected_stock_value_difference"] = expected * abs(qty) if abs(qty) > QTY_EPS else 0.0
	out["authoritative_source"] = source_kind

	if in_sle:
		out["evidence"]["incoming_sle"] = {
			"name": in_sle.name,
			"warehouse": in_sle.warehouse,
			"actual_qty": in_sle.actual_qty,
			"incoming_rate": in_sle.incoming_rate,
			"stock_value_difference": in_sle.stock_value_difference,
		}

	diff = abs(current_rate - expected)
	if diff <= 1:
		out["classification"] = NO_ACTION
		out["confidence"] = CONFIDENCE_EXACT
		out["reason"] = "current rate already matches authoritative transfer source"
		return out

	# Confidence: SVD-based outgoing is EXACT; previous_healthy alone is RECONSTRUCTABLE.
	if source_kind == "outgoing_sle_svd":
		out["confidence"] = CONFIDENCE_EXACT
		out["classification"] = EXACT
		out["reason"] = (
			f"target rate {current_rate} ≠ source outgoing SVD rate {expected} "
			f"(authoritative transfer propagation)"
		)
	elif source_kind == "outgoing_sle_rate":
		out["confidence"] = CONFIDENCE_EXACT
		out["classification"] = EXACT
		out["reason"] = f"target rate {current_rate} ≠ source outgoing_rate {expected}"
	else:
		out["confidence"] = CONFIDENCE_EXACT
		out["classification"] = RECONSTRUCTABLE
		out["reason"] = (
			f"target rate {current_rate} ≠ previous healthy source SLE rate {expected}"
		)
	return out


def apply_transfer_reconstruction_to_row(row: dict, *, cache: dict | None = None) -> dict:
	"""Upgrade a Wrong/Zero classified row using transfer reconstruction evidence."""
	purpose = g(row, "purpose") or ""
	if purpose not in TRANSFER_PURPOSES and purpose not in (
		"Material Issue",
		"Material Consumption for Manufacture",
	):
		return row
	# Need source warehouse — load from SE detail if missing.
	if not g(row, "s_warehouse") and (g(row, "voucher_detail") or g(row, "name")):
		detail = g(row, "voucher_detail") or g(row, "name")
		sw = frappe.db.get_value("Stock Entry Detail", detail, ["s_warehouse", "t_warehouse"], as_dict=True)
		if sw:
			row = dict(row)
			row["s_warehouse"] = sw.s_warehouse
			row["t_warehouse"] = row.get("t_warehouse") or sw.t_warehouse

	evidence = reconstruct_transfer_valuation(row, cache=cache)
	row = dict(row)
	row["transfer_reconstruction"] = evidence
	row["manual_reason"] = None  # clear generic until re-stamped

	cls = evidence.get("classification")
	exp = flt(evidence.get("expected_rate"))
	if cls == WAITING_UPSTREAM:
		row["status"] = "DEPENDENCY_REPAIR_REQUIRED"
		row["confidence"] = CONFIDENCE_AMBIGUOUS
		row["eligible"] = False
		row["proposed_rate"] = 0.0
		row["source_of_truth"] = "waiting_upstream_transfer_source"
		row["message"] = evidence.get("reason")
		if evidence.get("root_voucher") and evidence["root_voucher"] != evidence.get("voucher"):
			row["patient_zero"] = {
				"voucher_no": evidence["root_voucher"],
				"reason": evidence.get("reason"),
			}
		row["kpi_bucket"] = "WAITING"
		row["wrong_reason"] = "MANUAL_TRANSFER_PROPAGATION"
		row["manual_lane"] = WAITING_UPSTREAM
		return row

	if cls in (EXACT, RECONSTRUCTABLE) and abs(exp) > RATE_EPS:
		row["proposed_rate"] = exp
		row["proposed_amount"] = exp * abs(flt(row.get("qty") or 0))
		row["expected"] = exp
		row["expected_rate"] = exp
		row["source_of_truth"] = evidence.get("authoritative_source")
		row["rate_source"] = evidence.get("authoritative_source")
		row["confidence"] = evidence.get("confidence") or CONFIDENCE_EXACT
		row["status"] = "RECONSTRUCTABLE"
		row["eligible"] = True
		row["difference"] = exp - flt(row.get("current_rate") or row.get("current") or 0)
		row["message"] = evidence.get("reason")
		row["wrong_reason"] = "TRANSFER_AUTHORITATIVE_RECONSTRUCTION"
		row["manual_reason"] = None
		# MATCHED_BUT_CORRUPT when SE==SLE but both disagree with expected.
		cur = flt(row.get("current_rate") or row.get("basic_rate") or 0)
		if abs(cur - exp) > 1 and abs(cur) > RATE_EPS:
			flags = list(row.get("flags") or [])
			if "MATCHED_BUT_CORRUPT" not in flags and abs(cur) > RATE_EPS:
				# only flag when SE and SLE already agreed (caller may set)
				pass
			row["matched_but_corrupt_candidate"] = True
		return row

	if cls == USER_ACTION_REQUIRED:
		row["status"] = "MANUAL_REVIEW"
		row["confidence"] = CONFIDENCE_MANUAL
		row["eligible"] = False
		row["user_action_required"] = True
		row["manual_reason"] = "MANUAL_NO_AUTHORITATIVE_SOURCE"
		row["manual_lane"] = USER_ACTION_REQUIRED
		row["message"] = evidence.get("reason")
		return row

	# TOOL_LIMIT / residual
	row["manual_reason"] = "MANUAL_TRANSFER_PROPAGATION"
	row["manual_lane"] = TOOL_LIMIT
	row["message"] = evidence.get("reason") or row.get("message")
	return row


def collapse_transfer_roots(rows: list[dict]) -> dict:
	"""Collapse transfer findings into independent root chains."""
	from collections import Counter, defaultdict

	by_root: dict[str, list] = defaultdict(list)
	identities = set()
	batches = set()
	for r in rows or []:
		purpose = g(r, "purpose") or ""
		if purpose not in TRANSFER_PURPOSES and purpose not in ("Material Issue",):
			continue
		ev = r.get("transfer_reconstruction") or reconstruct_transfer_valuation(r)
		root = ev.get("root_voucher") or g(r, "voucher") or g(r, "voucher_no")
		by_root[str(root)].append(r)
		item = g(r, "item") or g(r, "item_code")
		wh = g(r, "s_warehouse") or g(r, "warehouse") or g(r, "t_warehouse")
		if item and wh:
			identities.add((item, wh))
		b = g(r, "batch") or g(r, "batch_no")
		if b:
			batches.add(b)
	class_counts = Counter()
	for r in rows or []:
		ev = r.get("transfer_reconstruction") or {}
		class_counts[ev.get("classification") or "UNCLASSIFIED"] += 1
	return {
		"findings": len(rows or []),
		"root_chains": len(by_root),
		"unique_item_warehouse": len(identities),
		"unique_batches": len(batches),
		"by_classification": dict(class_counts),
		"earliest_roots": sorted(
			(
				{
					"root": k,
					"n": len(v),
					"earliest": min(str(g(x, "posting_date") or "9999") for x in v),
					"purposes": sorted({g(x, "purpose") for x in v if g(x, "purpose")}),
				}
				for k, v in by_root.items()
			),
			key=lambda x: (x["earliest"], -x["n"]),
		)[:30],
	}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _outgoing_sle(voucher, item, warehouse=None, batch=None):
	conds = [
		"voucher_type='Stock Entry'",
		"voucher_no=%s",
		"item_code=%s",
		"is_cancelled=0",
		"actual_qty < 0",
	]
	args: list = [voucher, item]
	if warehouse:
		conds.append("warehouse=%s")
		args.append(warehouse)
	if batch:
		conds.append("(batch_no=%s OR IFNULL(batch_no,'')='')")
		args.append(batch)
	rows = frappe.db.sql(
		f"""
		SELECT name, warehouse, actual_qty, outgoing_rate, incoming_rate, valuation_rate,
		       stock_value_difference, stock_value, qty_after_transaction, posting_datetime, batch_no
		FROM `tabStock Ledger Entry`
		WHERE {" AND ".join(conds)}
		ORDER BY posting_datetime, creation
		LIMIT 1
		""",
		args,
		as_dict=True,
	)
	return rows[0] if rows else None


def _incoming_sle(voucher, item, warehouse=None, batch=None):
	conds = [
		"voucher_type='Stock Entry'",
		"voucher_no=%s",
		"item_code=%s",
		"is_cancelled=0",
		"actual_qty > 0",
	]
	args: list = [voucher, item]
	if warehouse:
		conds.append("warehouse=%s")
		args.append(warehouse)
	rows = frappe.db.sql(
		f"""
		SELECT name, warehouse, actual_qty, outgoing_rate, incoming_rate, valuation_rate,
		       stock_value_difference, stock_value, qty_after_transaction, posting_datetime, batch_no
		FROM `tabStock Ledger Entry`
		WHERE {" AND ".join(conds)}
		ORDER BY posting_datetime, creation
		LIMIT 1
		""",
		args,
		as_dict=True,
	)
	return rows[0] if rows else None


def _previous_healthy(item, warehouse, posting_date, posting_time) -> dict | None:
	row = frappe.db.sql(
		"""
		SELECT name, voucher_no, incoming_rate, valuation_rate, outgoing_rate,
		       stock_value_difference, actual_qty, posting_datetime
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND posting_datetime < TIMESTAMP(%s, %s)
		  AND (
		        ABS(IFNULL(incoming_rate,0)) > %s
		     OR ABS(IFNULL(valuation_rate,0)) > %s
		     OR ABS(IFNULL(outgoing_rate,0)) > %s
		  )
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 1
		""",
		(item, warehouse, posting_date, posting_time or "00:00:00", RATE_EPS, RATE_EPS, RATE_EPS),
		as_dict=True,
	)
	if not row:
		return None
	r = row[0]
	rate = flt(r.incoming_rate or r.outgoing_rate or r.valuation_rate)
	return {"name": r.name, "voucher_no": r.voucher_no, "rate": rate, "posting_datetime": str(r.posting_datetime)}


def _source_upstream_health(item, warehouse, posting_dt, batch=None, cache=None) -> dict:
	if not item or not warehouse:
		return {"status": "missing", "reason": "no source warehouse", "dependencies": []}
	key = ("up", item, warehouse, str(posting_dt), batch or "")
	if cache is not None and key in cache:
		return cache[key]
	# Look for poison / zero-value leftovers before this posting on the source identity.
	rows = frappe.db.sql(
		"""
		SELECT name, voucher_no, actual_qty, incoming_rate, outgoing_rate, valuation_rate,
		       stock_value, stock_value_difference, qty_after_transaction, posting_datetime
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND posting_datetime < %s
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 40
		""",
		(item, warehouse, posting_dt),
		as_dict=True,
	)
	deps = []
	for r in rows:
		reason = sle_poison_reason(r)
		if reason:
			result = {
				"status": "poisoned",
				"reason": reason,
				"root_voucher": r.voucher_no,
				"dependencies": [r.voucher_no],
			}
			if cache is not None:
				cache[key] = result
			return result
		# Zero qty leftover with value
		if abs(flt(r.qty_after_transaction)) <= QTY_EPS and abs(flt(r.stock_value)) > 1:
			result = {
				"status": "poisoned",
				"reason": "qty_after_zero_nonzero_value",
				"root_voucher": r.voucher_no,
				"dependencies": [r.voucher_no],
			}
			if cache is not None:
				cache[key] = result
			return result
		rate = flt(r.incoming_rate or r.outgoing_rate or r.valuation_rate)
		if abs(flt(r.actual_qty)) > QTY_EPS and abs(rate) <= RATE_EPS and abs(flt(r.stock_value_difference)) > VALUE_EPS:
			deps.append(r.voucher_no)
			result = {
				"status": "poisoned",
				"reason": "zero_rate_with_value_movement",
				"root_voucher": r.voucher_no,
				"dependencies": deps,
			}
			if cache is not None:
				cache[key] = result
			return result
	result = {"status": "healthy" if rows else "missing", "reason": None, "dependencies": [], "root_voucher": None}
	if cache is not None:
		cache[key] = result
	return result
