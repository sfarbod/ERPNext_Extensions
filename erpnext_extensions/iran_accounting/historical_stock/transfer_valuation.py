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
	detail = g(row, "voucher_detail") or g(row, "voucher_detail_no") or g(row, "name")
	# Resolve posting + warehouses from Stock Entry / Detail when row stamps are incomplete.
	# Incomplete stamps make upstream health return "missing" → false EXACT eligibility.
	_bad_dt = (
		not posting_date
		or str(posting_date).lower() in ("none", "null")
		or str(posting_dt).lower().startswith("none")
	)
	if voucher and (_bad_dt or not s_wh or not t_wh or not batch):
		if detail:
			sed = frappe.db.get_value(
				"Stock Entry Detail",
				detail,
				["s_warehouse", "t_warehouse", "batch_no", "qty", "basic_rate"],
				as_dict=True,
			)
			if sed:
				s_wh = s_wh or sed.s_warehouse
				t_wh = t_wh or sed.t_warehouse
				batch = batch or sed.batch_no
				if not qty:
					qty = flt(sed.qty)
		if _bad_dt:
			pd = frappe.db.get_value(
				"Stock Entry",
				voucher,
				["posting_date", "posting_time"],
				as_dict=True,
			)
			if pd and pd.get("posting_date"):
				posting_date = pd.posting_date
				posting_time = pd.posting_time or "00:00:00"
				posting_dt = f"{posting_date} {posting_time}"
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

	# --- Outgoing SLE for THIS detail only (never a sibling row) ---
	# Multi-batch Material Issue / Transfer often leave SLE.batch_no empty while
	# Serial and Batch Bundle carries identity. Matching empty batch_no + LIMIT 1
	# previously stole a sibling row's SVD (e.g. 223282) and stamped false EXACT.
	out_sle = _outgoing_sle(voucher, item, s_wh, batch=batch, voucher_detail=detail)
	in_sle = _incoming_sle(voucher, item, t_wh, batch=batch, voucher_detail=detail)

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
		elif abs(oq) > QTY_EPS and abs(svd) <= VALUE_EPS and abs(orate) <= RATE_EPS:
			# Qty left source with zero economic value on THIS SLE.
			# Material Transfer: previous healthy tip at the same s_warehouse is the
			# ERPNext native rate to restore (lost outgoing rate) — never a sibling detail.
			# Material Issue / Consumption: refuse invention (may be user zero / other root).
			out["evidence"]["outgoing_sle"] = {
				"name": out_sle.name,
				"warehouse": out_sle.warehouse,
				"actual_qty": oq,
				"outgoing_rate": orate,
				"stock_value_difference": svd,
				"qty_after": out_sle.qty_after_transaction,
				"stock_value": out_sle.stock_value,
				"voucher_detail_no": getattr(out_sle, "voucher_detail_no", None),
			}
			if purpose not in (
				"Material Transfer",
				"Material Transfer for Manufacture",
				"Send to Subcontractor",
			):
				out["classification"] = WAITING_UPSTREAM
				out["confidence"] = CONFIDENCE_AMBIGUOUS
				out["reason"] = (
					"outgoing qty moved with zero stock_value_difference / zero outgoing_rate "
					"— refuse sibling/previous_healthy invention; repair this identity upstream first"
				)
				out["authoritative_source"] = None
				out["expected_rate"] = 0.0
				upstream = _source_upstream_health(item, s_wh, posting_dt, batch=batch, cache=cache)
				out["upstream_health"] = upstream.get("status") or "unknown"
				out["dependency_closure"] = list(upstream.get("dependencies") or [])
				if upstream.get("root_voucher"):
					out["root_voucher"] = upstream["root_voucher"]
				return out
			# Transfer-family: fall through to previous_healthy_source_sle below.
		out["evidence"]["outgoing_sle"] = {
			"name": out_sle.name,
			"warehouse": out_sle.warehouse,
			"actual_qty": oq,
			"outgoing_rate": orate,
			"stock_value_difference": svd,
			"qty_after": out_sle.qty_after_transaction,
			"stock_value": out_sle.stock_value,
			"voucher_detail_no": getattr(out_sle, "voucher_detail_no", None),
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

	# Material Issue has no transfer target identity. A nonzero outgoing SVD on
	# *this* detail merely restates the current SLE — it is not independent
	# provenance for a zero-rate sibling. Refuse EXACT when purpose is Issue
	# and current is already zero/corrupt relative to warehouse MA disagreement.
	if purpose == "Material Issue" and source_kind in ("outgoing_sle_svd", "outgoing_sle_rate"):
		# Issue reconstruction: outgoing SLE of this detail is the defect itself
		# when current_rate≈0 and source_rate came from the same zero/nonzero row
		# we are classifying. Independent proof must come from elsewhere.
		if abs(current_rate) <= RATE_EPS:
			out["classification"] = WAITING_UPSTREAM
			out["confidence"] = CONFIDENCE_AMBIGUOUS
			out["expected_rate"] = 0.0
			out["authoritative_source"] = None
			out["reason"] = (
				"Material Issue zero rate — refuse treating this/sibling outgoing SVD as "
				"independent EXACT provenance; require batch-inward or proven MA evidence"
			)
			return out

	# Confidence: SVD-based outgoing is EXACT only for true transfers (source→target).
	if purpose in TRANSFER_PURPOSES and source_kind == "outgoing_sle_svd":
		out["confidence"] = CONFIDENCE_EXACT
		out["classification"] = EXACT
		out["reason"] = (
			f"target rate {current_rate} ≠ source outgoing SVD rate {expected} "
			f"(authoritative transfer propagation)"
		)
	elif purpose in TRANSFER_PURPOSES and source_kind == "outgoing_sle_rate":
		out["confidence"] = CONFIDENCE_EXACT
		out["classification"] = EXACT
		out["reason"] = f"target rate {current_rate} ≠ source outgoing_rate {expected}"
	elif purpose in TRANSFER_PURPOSES and source_kind == "previous_healthy_source_sle":
		# Evidence is deterministic, but auto-apply still unsafe: source RIV can fail
		# GL balance on unrelated vouchers and broad target replay inflates I1.
		# Keep reconstructable for operators; do not stamp Wrong Rate READY.
		out["confidence"] = CONFIDENCE_EXACT
		out["classification"] = RECONSTRUCTABLE
		out["reason"] = (
			f"transfer lost outgoing rate; previous healthy source SLE {expected} "
			"(not sibling) — MANUAL until source+target RIV-stable apply is proven"
		)
	elif purpose in TRANSFER_PURPOSES:
		out["confidence"] = CONFIDENCE_EXACT
		out["classification"] = RECONSTRUCTABLE
		out["reason"] = (
			f"target rate {current_rate} ≠ previous healthy source SLE rate {expected}"
		)
	else:
		# Material Issue / Consumption: previous_healthy may help but is not EXACT alone
		# when warehouse MA and batch-inward disagree (checked by callers via sources).
		out["confidence"] = CONFIDENCE_LIKELY
		out["classification"] = RECONSTRUCTABLE
		out["reason"] = (
			f"issue/consumption rate {current_rate} ≠ candidate source rate {expected} "
			f"({source_kind}); not EXACT without independent corroboration"
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

	# Only true EXACT transfer provenance is auto-repairable.
	# RECONSTRUCTABLE / LIKELY (e.g. Material Issue candidate rates) stay non-eligible.
	if cls == EXACT and abs(exp) > RATE_EPS:
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
		cur = flt(row.get("current_rate") or row.get("basic_rate") or 0)
		if abs(cur - exp) > 1 and abs(cur) > RATE_EPS:
			row["matched_but_corrupt_candidate"] = True
		return row

	if cls == RECONSTRUCTABLE and abs(exp) > RATE_EPS:
		row["proposed_rate"] = exp
		row["expected"] = exp
		row["expected_rate"] = exp
		row["source_of_truth"] = evidence.get("authoritative_source")
		row["rate_source"] = evidence.get("authoritative_source")
		row["confidence"] = evidence.get("confidence") or CONFIDENCE_LIKELY
		row["status"] = "MANUAL_REVIEW"
		row["eligible"] = False
		row["difference"] = exp - flt(row.get("current_rate") or row.get("current") or 0)
		row["message"] = evidence.get("reason")
		row["wrong_reason"] = "ISSUE_RATE_NEEDS_CORROBORATION"
		row["manual_lane"] = USER_ACTION_REQUIRED
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


def _outgoing_sle(voucher, item, warehouse=None, batch=None, voucher_detail=None):
	"""Resolve the outgoing SLE for one Stock Entry Detail.

	Prefer ``voucher_detail_no``. Never fall back to a sibling detail when the
	detail name is known — empty SLE.batch_no must not OR-match unrelated rows.
	"""
	base = [
		"voucher_type='Stock Entry'",
		"voucher_no=%s",
		"item_code=%s",
		"is_cancelled=0",
		"actual_qty < 0",
	]
	args: list = [voucher, item]
	if warehouse:
		base.append("warehouse=%s")
		args.append(warehouse)

	select = """
		SELECT name, warehouse, actual_qty, outgoing_rate, incoming_rate, valuation_rate,
		       stock_value_difference, stock_value, qty_after_transaction, posting_datetime,
		       batch_no, voucher_detail_no
		FROM `tabStock Ledger Entry`
		WHERE {where}
		ORDER BY posting_datetime, creation
		LIMIT 1
	"""

	if voucher_detail:
		conds = base + ["voucher_detail_no=%s"]
		rows = frappe.db.sql(select.format(where=" AND ".join(conds)), args + [voucher_detail], as_dict=True)
		if rows:
			return rows[0]
		# Detail known but no SLE linked — do not steal a sibling.
		return None

	if batch:
		# Strict batch match only. Empty batch_no is not a wildcard for siblings.
		conds = base + ["batch_no=%s"]
		rows = frappe.db.sql(select.format(where=" AND ".join(conds)), args + [batch], as_dict=True)
		if rows:
			return rows[0]
		return None

	rows = frappe.db.sql(select.format(where=" AND ".join(base)), args, as_dict=True)
	return rows[0] if rows else None


def _incoming_sle(voucher, item, warehouse=None, batch=None, voucher_detail=None):
	base = [
		"voucher_type='Stock Entry'",
		"voucher_no=%s",
		"item_code=%s",
		"is_cancelled=0",
		"actual_qty > 0",
	]
	args: list = [voucher, item]
	if warehouse:
		base.append("warehouse=%s")
		args.append(warehouse)

	select = """
		SELECT name, warehouse, actual_qty, outgoing_rate, incoming_rate, valuation_rate,
		       stock_value_difference, stock_value, qty_after_transaction, posting_datetime,
		       batch_no, voucher_detail_no
		FROM `tabStock Ledger Entry`
		WHERE {where}
		ORDER BY posting_datetime, creation
		LIMIT 1
	"""

	if voucher_detail:
		conds = base + ["voucher_detail_no=%s"]
		rows = frappe.db.sql(select.format(where=" AND ".join(conds)), args + [voucher_detail], as_dict=True)
		if rows:
			return rows[0]
		return None

	if batch:
		conds = base + ["batch_no=%s"]
		rows = frappe.db.sql(select.format(where=" AND ".join(conds)), args + [batch], as_dict=True)
		if rows:
			return rows[0]
		return None

	rows = frappe.db.sql(select.format(where=" AND ".join(base)), args, as_dict=True)
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
