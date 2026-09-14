# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only expected-rate analysis. Never uses live Bin. Never writes."""

from __future__ import annotations

from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_AMBIGUOUS,
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	CONFIDENCE_MANUAL,
	QTY_EPS,
	RATE_EPS,
	RECONSTRUCTION_PRIORITY,
	STATUS_VALUATION_POISON_DEPENDENCY,
)
from erpnext_extensions.iran_accounting.historical_stock.util import g


def sources_disagree(sources: dict) -> bool:
	vals = [flt(v) for v in (sources or {}).values() if abs(flt(v)) > RATE_EPS]
	if len(vals) < 2:
		return False
	return (max(vals) - min(vals)) > 1


def attach_rate_analysis(row: dict, raw=None) -> dict:
	"""Fill Scan-grid current/expected columns from an already classified row."""
	raw = raw or {}
	qty = flt(row.get("qty") or g(raw, "qty") or g(raw, "actual_qty"))
	current_basic = flt(g(raw, "basic_rate") if g(raw, "basic_rate") is not None else row.get("current_rate"))
	current_val = flt(
		g(raw, "valuation_rate") if g(raw, "valuation_rate") is not None else row.get("current_valuation_rate", current_basic)
	)
	current_in = flt(row.get("current_incoming_rate", g(raw, "incoming_rate") if g(raw, "incoming_rate") is not None else 0))
	current_out = flt(row.get("current_outgoing_rate", g(raw, "outgoing_rate") if g(raw, "outgoing_rate") is not None else 0))
	current_amt = flt(row.get("current_amount", g(raw, "amount") if g(raw, "amount") is not None else current_basic * abs(qty)))
	expected = flt(row.get("expected") if row.get("expected") is not None else row.get("proposed_rate") or 0)
	surface = row.get("surface") or "SE"
	inbound = qty > QTY_EPS or (g(raw, "t_warehouse") and not g(raw, "s_warehouse"))
	outbound = qty < -QTY_EPS or (g(raw, "s_warehouse") and not g(raw, "t_warehouse"))
	if surface == "SLE":
		if inbound:
			current_in = flt(row.get("current") if row.get("current") is not None else current_in)
			expected_in = expected
			expected_out = current_out
			current_basic = current_in
			current_val = flt(g(raw, "valuation_rate") or row.get("valuation_rate") or current_val)
		else:
			current_out = flt(row.get("current") if row.get("current") is not None else current_out)
			expected_out = expected
			expected_in = current_in
			current_basic = current_out
			current_val = flt(g(raw, "valuation_rate") or row.get("valuation_rate") or current_val)
		expected_basic = expected
		expected_val = expected
		expected_amt = expected * abs(qty)
	else:
		expected_basic = expected
		expected_val = expected
		expected_in = expected if inbound else current_in
		expected_out = expected if outbound else current_out
		expected_amt = flt(row.get("proposed_amount") if row.get("proposed_amount") is not None else expected * abs(qty))
	current_rate = current_out if outbound else (current_in if inbound else current_basic)
	row["current_basic_rate"] = current_basic
	row["expected_basic_rate"] = expected_basic
	row["current_valuation_rate"] = current_val
	row["expected_valuation_rate"] = expected_val
	row["current_incoming_rate"] = current_in
	row["expected_incoming_rate"] = expected_in
	row["current_outgoing_rate"] = current_out
	row["expected_outgoing_rate"] = expected_out
	row["current_amount"] = current_amt
	row["expected_amount"] = expected_amt
	row["current"] = current_rate
	row["expected"] = expected
	row["difference"] = flt(expected) - flt(current_rate)
	row["rate_source"] = row.get("rate_source") or row.get("source") or row.get("source_of_truth")
	row["repair_required"] = bool(row.get("eligible")) and row.get("confidence") == CONFIDENCE_EXACT
	row["reconstruction_sources"] = row.get("reconstruction_sources") or {}
	row["sources_disagree"] = sources_disagree(row["reconstruction_sources"])
	return row


def preview_reconstruction(row: dict) -> dict:
	"""Full reconstruction preview for a selected row. Read-only. Never chooses silently."""
	sources = dict(row.get("reconstruction_sources") or {})
	if not sources:
		sources = gather_reconstruction_sources(row)
	sources = {k: flt(v) for k, v in sources.items() if k != "bin" and abs(flt(v)) > RATE_EPS}
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import pick_reconstruction

	picked = pick_reconstruction(sources)
	disagree = sources_disagree(sources)
	corroborated = "+" in str(picked.get("source") or "")
	confidence = picked.get("confidence")
	if disagree and confidence == CONFIDENCE_EXACT and not corroborated:
		confidence = CONFIDENCE_AMBIGUOUS
	elif disagree and confidence == CONFIDENCE_EXACT and corroborated:
		confidence = CONFIDENCE_LIKELY
	alternatives = [{"source": name, "rate": flt(sources.get(name))} for name in RECONSTRUCTION_PRIORITY if name in sources]
	current = flt(row.get("current") if row.get("current") is not None else row.get("current_rate") or row.get("current_basic_rate"))
	expected = flt(picked.get("expected") or row.get("expected") or row.get("proposed_rate") or 0)
	return {
		"voucher": row.get("voucher"),
		"item": row.get("item") or row.get("item_code"),
		"warehouse": row.get("warehouse"),
		"batch": row.get("batch"),
		"current": current,
		"expected": expected,
		"difference": expected - current,
		"chosen_source": picked.get("source"),
		"confidence": confidence,
		"sources_disagree": disagree,
		"alternative_sources": alternatives,
		"priority": list(RECONSTRUCTION_PRIORITY),
		"bin_used": False,
		"repair_required": confidence == CONFIDENCE_EXACT and not disagree,
		"status": row.get("status"),
		"poison": row.get("status") == STATUS_VALUATION_POISON_DEPENDENCY,
	}


def gather_reconstruction_sources(row: dict) -> dict:
	"""Look up named reconstruction sources. Skips Bin. Extra PR/SRE only here (row preview)."""
	import frappe

	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import (
		_batch_inward_rate,
		_previous_healthy_sle_rate,
		_source_transfer_sle_rate,
		_version_rate,
	)

	item = row.get("item") or row.get("item_code")
	warehouse = row.get("warehouse") or row.get("s_warehouse") or row.get("t_warehouse")
	batch = row.get("batch") or row.get("batch_no")
	voucher = row.get("voucher") or row.get("parent")
	detail = row.get("voucher_detail") or row.get("name")
	sources: dict[str, float] = {}
	if voucher and detail:
		rate, _amt = _version_rate(voucher, detail)
		if abs(rate) > RATE_EPS:
			sources["version"] = rate
	if item and warehouse:
		prev = _previous_healthy_sle_rate(item, warehouse, row.get("posting_date"), row.get("posting_time"))
		if abs(prev) > RATE_EPS:
			sources["previous_healthy_sle"] = prev
	if item and batch:
		batch_rate = _batch_inward_rate(item, batch, warehouse)
		if abs(batch_rate) > RATE_EPS:
			sources["batch_inward"] = batch_rate
	transfer = _source_transfer_sle_rate(
		{
			"purpose": row.get("purpose"),
			"parent": voucher,
			"item_code": item,
		}
	)
	if abs(transfer) > RATE_EPS:
		sources["transfer_source"] = transfer
	if (row.get("purpose") or "") == "Manufacture" and voucher and item:
		pool = _manufacture_pool_rate(voucher, item)
		if abs(pool) > RATE_EPS:
			sources["manufacture_pool"] = pool
	if item:
		pr = _voucher_type_rate(item, warehouse, "Purchase Receipt")
		if abs(pr) > RATE_EPS:
			sources["purchase_receipt"] = pr
		sre = _voucher_type_rate(item, warehouse, "Stock Reconciliation")
		if abs(sre) > RATE_EPS:
			sources["stock_reconciliation"] = sre
		if "previous_healthy_sle" in sources:
			sources["moving_average"] = sources["previous_healthy_sle"]
	if row.get("expected") and row.get("source") == "implied_svd":
		sources["implied_svd"] = flt(row.get("expected"))
	sources.pop("bin", None)
	return sources


def _manufacture_pool_rate(voucher, item) -> float:
	import frappe

	row = frappe.db.sql(
		"""
		SELECT ABS(sle.stock_value_difference / sle.actual_qty) rate
		FROM `tabStock Ledger Entry` sle
		WHERE sle.voucher_type='Stock Entry' AND sle.voucher_no=%s AND sle.item_code=%s
		  AND sle.is_cancelled=0 AND sle.actual_qty < 0
		  AND ABS(IFNULL(sle.stock_value_difference,0)) > %s
		ORDER BY sle.creation
		LIMIT 1
		""",
		(voucher, item, RATE_EPS),
		as_dict=True,
	)
	return flt(row[0].rate) if row else 0.0


def _voucher_type_rate(item, warehouse, voucher_type) -> float:
	import frappe

	conds = ["item_code=%s", "is_cancelled=0", "voucher_type=%s", "actual_qty > 0", "ABS(IFNULL(incoming_rate,0)) > %s"]
	args: list = [item, voucher_type, RATE_EPS]
	if warehouse:
		conds.append("warehouse=%s")
		args.append(warehouse)
	row = frappe.db.sql(
		f"""
		SELECT incoming_rate, valuation_rate
		FROM `tabStock Ledger Entry`
		WHERE {" AND ".join(conds)}
		ORDER BY posting_datetime DESC, creation DESC
		LIMIT 1
		""",
		args,
		as_dict=True,
	)
	if not row:
		return 0.0
	return flt(row[0].incoming_rate or row[0].valuation_rate)
