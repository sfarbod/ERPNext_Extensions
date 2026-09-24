# Copyright (c) 2026, ERPNext Extensions contributors
"""Transfer repair convergence, grouping, and idempotency (v5.3.0 Phase 5D).

Contract:
  classify → preview → apply → verify → rescan → NO_ACTION / COMPLETE

A repaired Transfer root must not remain EXACT for the same economic reason.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	QTY_EPS,
	RATE_EPS,
	STATUS_REPAIRED,
	VALUE_EPS,
)
from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
	EXACT,
	NO_ACTION,
	RECONSTRUCTABLE,
	TRANSFER_PURPOSES,
	WAITING_UPSTREAM,
	TOOL_LIMIT,
	apply_transfer_reconstruction_to_row,
	reconstruct_transfer_valuation,
)
from erpnext_extensions.iran_accounting.historical_stock.util import g

# Outcome buckets (Phase 5D audit)
REPAIRED_TO_NO_ACTION = "REPAIRED_TO_NO_ACTION"
REPAIRED_BUT_STILL_EXACT = "REPAIRED_BUT_STILL_EXACT"
REPAIRED_BUT_NEW_IDENTITY_EXACT = "REPAIRED_BUT_NEW_IDENTITY_EXACT"
REPAIRED_BUT_SIBLING_EXACT = "REPAIRED_BUT_SIBLING_EXACT"
REPAIRED_BUT_DOWNSTREAM_EXACT = "REPAIRED_BUT_DOWNSTREAM_EXACT"
REPAIRED_BUT_RESIDUAL_DIFF = "REPAIRED_BUT_RESIDUAL_DIFF"
REPAIRED_BUT_WRONG_RATE = "REPAIRED_BUT_WRONG_RATE"
REPAIR_REVERTED = "REPAIR_REVERTED"
BLOCKED = "BLOCKED"
ALREADY_REPAIRED = "ALREADY_REPAIRED"

# Residual policy
ROUNDING_NO_ACTION = "ROUNDING_NO_ACTION"
ROUNDING_REQUIRES_BALANCE_ACCOUNT = "ROUNDING_REQUIRES_BALANCE_ACCOUNT"
ECONOMIC_DIFFERENCE = "ECONOMIC_DIFFERENCE"
UNKNOWN_RESIDUAL = "UNKNOWN"


def company_rate_precision(company: str | None = None) -> int:
	"""IRR / company float precision for rate residual classification."""
	try:
		# ERPNext Currency.fraction units for IRR is typically 0 decimals for amounts;
		# rates often keep 2–6. Prefer Stock Settings / default.
		prec = frappe.db.get_single_value("System Settings", "float_precision")
		return int(prec or 6)
	except Exception:
		return 6


def residual_class(diff: float, *, company: str | None = None) -> str:
	"""Classify |current-expected| without hiding economic mismatch."""
	d = abs(flt(diff))
	prec = company_rate_precision(company)
	# One unit in the last float place for rates at this precision, floored at 0.01 IRR.
	unit = max(10 ** (-prec), 0.01)
	if d <= unit + 1e-12:
		return ROUNDING_NO_ACTION
	if d <= 1.0:
		# Sub-IRR residual: still economic if above float unit — report explicitly.
		return ECONOMIC_DIFFERENCE
	return ECONOMIC_DIFFERENCE


def sle_fingerprint(voucher: str, item: str) -> list[dict]:
	"""Stable SLE economic fingerprint for idempotency checks."""
	rows = frappe.db.sql(
		"""
		SELECT name, warehouse, batch_no, serial_and_batch_bundle, voucher_detail_no,
		       actual_qty, qty_after_transaction, incoming_rate, outgoing_rate,
		       valuation_rate, stock_value, stock_value_difference,
		       posting_datetime, creation
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND item_code=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation, name
		""",
		(voucher, item),
		as_dict=True,
	)
	out = []
	for r in rows:
		out.append(
			{
				"name": r.name,
				"warehouse": r.warehouse,
				"batch_no": r.batch_no,
				"voucher_detail_no": r.voucher_detail_no,
				"actual_qty": flt(r.actual_qty),
				"qty_after": flt(r.qty_after_transaction),
				"incoming_rate": flt(r.incoming_rate),
				"outgoing_rate": flt(r.outgoing_rate),
				"valuation_rate": flt(r.valuation_rate),
				"stock_value": flt(r.stock_value),
				"svd": flt(r.stock_value_difference),
			}
		)
	return out


def se_detail_fingerprint(voucher: str, item: str | None = None) -> list[dict]:
	conds = ["parent=%s"]
	args: list = [voucher]
	if item:
		conds.append("item_code=%s")
		args.append(item)
	rows = frappe.db.sql(
		f"""
		SELECT name, item_code, s_warehouse, t_warehouse, batch_no, qty, transfer_qty,
		       basic_rate, valuation_rate, amount, basic_amount, serial_and_batch_bundle
		FROM `tabStock Entry Detail`
		WHERE {" AND ".join(conds)}
		ORDER BY idx, name
		""",
		args,
		as_dict=True,
	)
	return [
		{
			"name": r.name,
			"item_code": r.item_code,
			"s_warehouse": r.s_warehouse,
			"t_warehouse": r.t_warehouse,
			"batch_no": r.batch_no,
			"qty": flt(r.qty),
			"basic_rate": flt(r.basic_rate),
			"valuation_rate": flt(r.valuation_rate),
			"amount": flt(r.amount),
		}
		for r in rows
	]


def fingerprints_equal(a: list[dict], b: list[dict], *, keys: tuple[str, ...] | None = None) -> bool:
	if len(a) != len(b):
		return False
	keys = keys or (
		"warehouse",
		"actual_qty",
		"incoming_rate",
		"outgoing_rate",
		"valuation_rate",
		"stock_value",
		"svd",
		"qty_after",
	)
	for x, y in zip(a, b, strict=True):
		for k in keys:
			if k not in x and k not in y:
				continue
			if abs(flt(x.get(k)) - flt(y.get(k))) > 1e-6 and str(x.get(k)) != str(y.get(k)):
				# Allow tiny float noise on rates/values
				if isinstance(x.get(k), (int, float)) or isinstance(y.get(k), (int, float)):
					if abs(flt(x.get(k)) - flt(y.get(k))) <= 0.01:
						continue
				return False
	return True


def build_transfer_repair_group(row: dict) -> dict:
	"""Deterministic Transfer Repair Group for one finding.

	Membership = economically coupled SLE/SE-Detail legs for the SAME
	(item, batch, voucher) transfer pair — not the entire multi-item voucher.
	"""
	voucher = g(row, "voucher") or g(row, "voucher_no") or g(row, "parent")
	item = g(row, "item") or g(row, "item_code")
	batch = g(row, "batch") or g(row, "batch_no") or ""
	detail = g(row, "voucher_detail") or g(row, "voucher_detail_no") or g(row, "name")
	purpose = g(row, "purpose") or frappe.db.get_value("Stock Entry", voucher, "purpose")

	# Prefer SE Detail for s/t warehouses.
	sed = None
	if detail:
		sed = frappe.db.get_value(
			"Stock Entry Detail",
			detail,
			[
				"name",
				"item_code",
				"s_warehouse",
				"t_warehouse",
				"batch_no",
				"qty",
				"transfer_qty",
				"basic_rate",
				"valuation_rate",
				"amount",
				"serial_and_batch_bundle",
			],
			as_dict=True,
		)
	if not sed and voucher and item:
		# Resolve detail by item (+ batch when present)
		sed = frappe.db.sql(
			"""
			SELECT name, item_code, s_warehouse, t_warehouse, batch_no, qty, transfer_qty,
			       basic_rate, valuation_rate, amount, serial_and_batch_bundle
			FROM `tabStock Entry Detail`
			WHERE parent=%s AND item_code=%s
			  AND IFNULL(batch_no,'')=IFNULL(%s,'')
			ORDER BY idx LIMIT 1
			""",
			(voucher, item, batch or ""),
			as_dict=True,
		)
		sed = sed[0] if sed else None

	s_wh = (sed.s_warehouse if sed else None) or g(row, "s_warehouse")
	t_wh = (sed.t_warehouse if sed else None) or g(row, "t_warehouse") or g(row, "warehouse")
	batch = (sed.batch_no if sed else None) or batch
	detail = (sed.name if sed else None) or detail
	item = (sed.item_code if sed else None) or item

	sles = frappe.db.sql(
		"""
		SELECT name, warehouse, batch_no, serial_and_batch_bundle, voucher_detail_no,
		       actual_qty, qty_after_transaction, incoming_rate, outgoing_rate,
		       valuation_rate, stock_value, stock_value_difference, posting_datetime
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND item_code=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation, name
		""",
		(voucher, item),
		as_dict=True,
	)

	# Couple only legs that share this detail (or all item legs when detail blank).
	# Soft batch: ignore SLE batch NULL vs SE batch (common with SABB).
	members = []
	for sle in sles:
		if detail and sle.voucher_detail_no and sle.voucher_detail_no != detail:
			continue
		if batch and sle.batch_no and str(sle.batch_no) != str(batch) and not detail:
			continue
		members.append(sle)

	# Sibling items on same voucher (not in group — for audit only)
	siblings = frappe.db.sql(
		"""
		SELECT name, item_code, s_warehouse, t_warehouse, batch_no, basic_rate, qty
		FROM `tabStock Entry Detail`
		WHERE parent=%s AND name!=IFNULL(%s,'')
		ORDER BY idx
		""",
		(voucher, detail or ""),
		as_dict=True,
	)

	group_key = f"{voucher}|{item}|{batch or ''}|{detail or ''}"
	return {
		"group_key": group_key,
		"voucher": voucher,
		"item": item,
		"batch": batch or "",
		"voucher_detail": detail,
		"purpose": purpose,
		"s_warehouse": s_wh,
		"t_warehouse": t_wh,
		"se_detail": sed,
		"sle_members": members,
		"sibling_details": siblings,
		"scope": "ITEM_BATCH_VOUCHER_DETAIL",
	}


def capture_transfer_evidence(row: dict) -> dict:
	"""BEFORE/AFTER evidence block for convergence audit."""
	group = build_transfer_repair_group(row)
	recon = reconstruct_transfer_valuation(
		{
			**row,
			"voucher": group["voucher"],
			"item": group["item"],
			"s_warehouse": group["s_warehouse"],
			"t_warehouse": group["t_warehouse"],
			"batch": group["batch"],
			"voucher_detail": group["voucher_detail"],
			"purpose": group["purpose"],
			"qty": (group["se_detail"].qty if group.get("se_detail") else row.get("qty")),
			"basic_rate": (
				group["se_detail"].basic_rate if group.get("se_detail") else row.get("basic_rate")
			),
			"current_rate": row.get("current_rate") or row.get("basic_rate"),
		}
	)
	out_leg = None
	in_leg = None
	for sle in group["sle_members"]:
		if flt(sle.actual_qty) < 0:
			out_leg = sle
		elif flt(sle.actual_qty) > 0:
			in_leg = sle
	expected = flt(recon.get("expected_rate"))
	current = flt(recon.get("current_rate") or row.get("current_rate") or row.get("basic_rate"))
	diff = abs(current - expected)
	return {
		"group": {
			"group_key": group["group_key"],
			"voucher": group["voucher"],
			"item": group["item"],
			"batch": group["batch"],
			"voucher_detail": group["voucher_detail"],
			"purpose": group["purpose"],
			"s_warehouse": group["s_warehouse"],
			"t_warehouse": group["t_warehouse"],
			"sibling_count": len(group["sibling_details"] or []),
			"sle_member_count": len(group["sle_members"] or []),
		},
		"reconstruction": {
			"classification": recon.get("classification"),
			"authoritative_source": recon.get("authoritative_source"),
			"expected_rate": expected,
			"current_rate": current,
			"diff": diff,
			"residual_class": residual_class(diff),
			"upstream_health": recon.get("upstream_health"),
			"root_voucher": recon.get("root_voucher"),
			"reason": recon.get("reason"),
			"evidence": recon.get("evidence"),
		},
		"outgoing_sle": _sle_brief(out_leg),
		"incoming_sle": _sle_brief(in_leg),
		"se_detail": (
			{
				"name": group["se_detail"].name,
				"basic_rate": flt(group["se_detail"].basic_rate),
				"valuation_rate": flt(group["se_detail"].valuation_rate),
				"qty": flt(group["se_detail"].qty),
				"amount": flt(group["se_detail"].amount),
			}
			if group.get("se_detail")
			else None
		),
		"sle_fp": sle_fingerprint(group["voucher"], group["item"]),
		"se_fp": se_detail_fingerprint(group["voucher"], group["item"]),
	}


def _sle_brief(sle) -> dict | None:
	if not sle:
		return None
	return {
		"name": sle.name,
		"warehouse": sle.warehouse,
		"actual_qty": flt(sle.actual_qty),
		"qty_after": flt(sle.qty_after_transaction),
		"incoming_rate": flt(sle.incoming_rate),
		"outgoing_rate": flt(sle.outgoing_rate),
		"valuation_rate": flt(sle.valuation_rate),
		"stock_value": flt(sle.stock_value),
		"svd": flt(sle.stock_value_difference),
	}


def pair_value_consistent(out_leg, in_leg, *, tol: float = 1.0) -> bool:
	"""Source outgoing value must equal target incoming value (transfer creates no value)."""
	if not out_leg or not in_leg:
		return False
	return abs(abs(flt(out_leg.get("svd"))) - abs(flt(in_leg.get("svd")))) <= tol


def diagnose_non_convergence(before: dict, after: dict, *, written: dict | None = None) -> dict:
	"""Pinpoint mathematical cause of REPAIRED_BUT_STILL_EXACT."""
	b = before.get("reconstruction") or {}
	a = after.get("reconstruction") or {}
	causes = []
	# A: scanner stale — after classification still EXACT with same expected/current as before
	if a.get("classification") == EXACT and abs(flt(a.get("diff")) - flt(b.get("diff"))) < 0.01:
		if abs(flt(a.get("expected_rate")) - flt(b.get("expected_rate"))) < 0.01:
			causes.append("A_OR_H_SAME_EXPECTED_AFTER_REPAIR")
	# D/H: expected moved after replay
	if abs(flt(a.get("expected_rate")) - flt(b.get("expected_rate"))) > 1:
		causes.append("H_REPLAY_CHANGED_AUTHORITATIVE_EXPECTED")
	# current moved toward expected then drifted
	if written:
		w_rate = flt(written.get("proposed_rate"))
		if abs(flt(a.get("current_rate")) - w_rate) > 1:
			causes.append("H_REPLAY_REWROTE_CURRENT_AWAY_FROM_WRITTEN")
	# pair inconsistency
	if after.get("outgoing_sle") and after.get("incoming_sle"):
		if not pair_value_consistent(after["outgoing_sle"], after["incoming_sle"]):
			causes.append("D_SOURCE_TARGET_VALUE_MISMATCH")
	# SE vs SLE
	se = after.get("se_detail") or {}
	inn = after.get("incoming_sle") or {}
	if se and inn and abs(flt(se.get("basic_rate")) - flt(inn.get("incoming_rate"))) > 1:
		causes.append("C_SE_SLE_RATE_DESYNC")
	# residual
	rc = a.get("residual_class") or residual_class(a.get("diff") or 0)
	if a.get("classification") == EXACT and rc == ECONOMIC_DIFFERENCE:
		causes.append("N_ECONOMIC_DIFF_REMAINS")
	if a.get("classification") == EXACT and flt(a.get("diff") or 0) <= 1:
		causes.append("G_SHOULD_HAVE_BEEN_NO_ACTION")  # classifier bug
	# sibling
	if (after.get("group") or {}).get("sibling_count", 0) > 0:
		causes.append("NOTE_HAS_SIBLINGS_INDEPENDENT")
	return {
		"causes": causes or ["N_UNKNOWN"],
		"before_expected": b.get("expected_rate"),
		"after_expected": a.get("expected_rate"),
		"before_current": b.get("current_rate"),
		"after_current": a.get("current_rate"),
		"before_diff": b.get("diff"),
		"after_diff": a.get("diff"),
		"after_cls": a.get("classification"),
		"written_rate": (written or {}).get("proposed_rate"),
	}


def classify_convergence_outcome(
	*,
	before: dict,
	after: dict,
	apply_result: dict | None,
	second_fp_equal: bool | None = None,
	sibling_still_exact: bool = False,
	new_identity_exact: bool = False,
	downstream_exact: bool = False,
) -> str:
	if apply_result and apply_result.get("aborted"):
		return BLOCKED
	if apply_result and apply_result.get("reverted"):
		return REPAIR_REVERTED
	# Apply returned non-writing WAITING / TOOL_LIMIT / ALREADY — not a failed repair.
	applied_rows = (apply_result or {}).get("applied") or []
	if applied_rows and all(
		(not a.get("written"))
		and str(a.get("status") or "")
		in (ALREADY_REPAIRED, WAITING_UPSTREAM, TOOL_LIMIT, "WAITING_UPSTREAM", "DRY_RUN")
		for a in applied_rows
	):
		statuses = {str(a.get("status") or "") for a in applied_rows}
		if WAITING_UPSTREAM in statuses or "WAITING_UPSTREAM" in statuses:
			return BLOCKED
		if TOOL_LIMIT in statuses:
			return BLOCKED
		if ALREADY_REPAIRED in statuses:
			a_cls = (after.get("reconstruction") or {}).get("classification")
			if a_cls == NO_ACTION:
				return REPAIRED_TO_NO_ACTION
			return BLOCKED
	a_cls = (after.get("reconstruction") or {}).get("classification")
	diff = flt((after.get("reconstruction") or {}).get("diff"))
	if a_cls == NO_ACTION or (a_cls == EXACT and diff <= 1):
		# Exact with diff<=1 should be NO_ACTION — treat as success if classifier lag
		if a_cls == NO_ACTION or residual_class(diff) == ROUNDING_NO_ACTION:
			return REPAIRED_TO_NO_ACTION
	if sibling_still_exact:
		return REPAIRED_BUT_SIBLING_EXACT
	if new_identity_exact:
		return REPAIRED_BUT_NEW_IDENTITY_EXACT
	if downstream_exact:
		return REPAIRED_BUT_DOWNSTREAM_EXACT
	if a_cls == WAITING_UPSTREAM:
		return BLOCKED
	if a_cls == EXACT:
		if residual_class(diff) == ECONOMIC_DIFFERENCE and diff <= 1:
			return REPAIRED_BUT_RESIDUAL_DIFF
		return REPAIRED_BUT_STILL_EXACT
	if a_cls in (RECONSTRUCTABLE, TOOL_LIMIT):
		return REPAIRED_BUT_WRONG_RATE
	return REPAIRED_TO_NO_ACTION


def prepare_repair_row_from_group(group: dict, evidence: dict) -> dict:
	"""Build a row dict suitable for repair_wrong_rates_selected."""
	recon = evidence.get("reconstruction") or {}
	sed = group.get("se_detail")
	exp = flt(recon.get("expected_rate"))
	qty = flt(sed.qty) if sed else flt(evidence.get("group", {}).get("qty") or 0)
	return {
		"voucher": group["voucher"],
		"voucher_no": group["voucher"],
		"item": group["item"],
		"item_code": group["item"],
		"warehouse": group["t_warehouse"] or group["s_warehouse"],
		"s_warehouse": group["s_warehouse"],
		"t_warehouse": group["t_warehouse"],
		"batch": group["batch"],
		"batch_no": group["batch"],
		"voucher_detail": group["voucher_detail"],
		"purpose": group["purpose"],
		"qty": qty,
		"proposed_rate": exp,
		"expected_rate": exp,
		"proposed_amount": exp * abs(qty),
		"current_rate": recon.get("current_rate"),
		"basic_rate": (sed.basic_rate if sed else None),
		"source_of_truth": recon.get("authoritative_source") or "outgoing_sle_svd",
		"rate_source": recon.get("authoritative_source") or "outgoing_sle_svd",
		"source": "transfer_authoritative_reconstruction",
		"eligible": True,
		"confidence": "EXACT",
		"status": "RECONSTRUCTABLE",
		"planner_status": "READY_WRONG_RATE",
		"sql_updates": 1,
		"patient_zero": {"voucher_no": group["voucher"]},
		"transfer_reconstruction": {
			"classification": recon.get("classification"),
			"expected_rate": exp,
			"authoritative_source": recon.get("authoritative_source"),
			"root_voucher": recon.get("root_voucher") or group["voucher"],
		},
		"sabb": (sed.serial_and_batch_bundle if sed else None),
	}


def transfer_pair_legs(voucher: str, item: str, *, voucher_detail: str | None = None, batch: str | None = None):
	"""Return (outgoing_sle, incoming_sle) for one Transfer Repair Group identity.

	Prefer voucher_detail coupling. Batch is a soft hint — many Transfer SLEs carry
	NULL batch_no while SE Detail has the batch (SABB path). Do not drop legs solely
	for batch mismatch when voucher_detail matches.
	"""
	rows = frappe.db.sql(
		"""
		SELECT name, warehouse, batch_no, voucher_detail_no, actual_qty,
		       qty_after_transaction, incoming_rate, outgoing_rate,
		       valuation_rate, stock_value, stock_value_difference, posting_datetime
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND item_code=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation, name
		""",
		(voucher, item),
		as_dict=True,
	)
	out_leg = in_leg = None
	for sle in rows:
		if voucher_detail and sle.voucher_detail_no and sle.voucher_detail_no != voucher_detail:
			continue
		# Soft batch filter only when BOTH sides have a batch and they disagree.
		if (
			batch
			and sle.batch_no
			and str(sle.batch_no) != str(batch)
			and not voucher_detail
		):
			continue
		if flt(sle.actual_qty) < 0 and out_leg is None:
			out_leg = sle
		elif flt(sle.actual_qty) > 0 and in_leg is None:
			in_leg = sle
	return out_leg, in_leg


def transfer_already_balanced(row: dict) -> dict | None:
	"""If Transfer identity already matches authoritative outgoing SVD, return ALREADY_REPAIRED."""
	purpose = str(row.get("purpose") or "")
	if purpose not in TRANSFER_PURPOSES:
		return None
	group = build_transfer_repair_group(row)
	out_leg, in_leg = transfer_pair_legs(
		group["voucher"],
		group["item"],
		voucher_detail=group.get("voucher_detail"),
		batch=group.get("batch"),
	)
	if not out_leg or not in_leg:
		return None
	oq = abs(flt(out_leg.actual_qty))
	if oq <= QTY_EPS:
		return None
	auth = abs(flt(out_leg.stock_value_difference) / oq)
	in_rate = abs(flt(in_leg.incoming_rate))
	pair_ok = abs(abs(flt(out_leg.stock_value_difference)) - abs(flt(in_leg.stock_value_difference))) <= 1.0
	se_rate = None
	if group.get("se_detail"):
		se_rate = flt(group["se_detail"].basic_rate or group["se_detail"].valuation_rate)
	se_ok = se_rate is None or abs(se_rate - auth) <= 1.0
	if pair_ok and abs(in_rate - auth) <= 1.0 and se_ok and abs(auth) > RATE_EPS:
		return {
			"status": ALREADY_REPAIRED,
			"written": False,
			"economic_writes": 0,
			"auth_rate": auth,
			"message": "ALREADY_REPAIRED — transfer pair already matches outgoing SVD",
			"group_key": group["group_key"],
		}
	return None


def finalize_transfer_repair_group(row: dict, *, from_dt=None) -> dict:
	"""Post-write convergence: force target incoming economics = source outgoing SVD.

	Do NOT replay the entire source warehouse chronology here — that rewrites unrelated
	vouchers and can poison MA. Outgoing SVD on this voucher is the authority; only the
	incoming leg (+ SE Detail) and a bounded target replay are adjusted.
	"""
	from erpnext_extensions.iran_accounting.historical_stock.replay import replay_from_patient_zero

	voucher = row.get("voucher") or row.get("voucher_no")
	item = row.get("item") or row.get("item_code")
	detail = row.get("voucher_detail")
	batch = row.get("batch") or row.get("batch_no") or ""
	group = build_transfer_repair_group(row)
	t_wh = group.get("t_warehouse")
	out_leg, in_leg = transfer_pair_legs(voucher, item, voucher_detail=detail, batch=batch)
	result = {
		"voucher": voucher,
		"item": item,
		"group_key": group["group_key"],
		"rebalanced": False,
		"economic_writes": 0,
		"auth_rate": None,
		"pair_ok": False,
		"replay_source": None,
		"replay_target": None,
	}
	if not out_leg or not in_leg:
		result["status"] = "MISSING_PAIR"
		result["message"] = "transfer pair incomplete — cannot finalize"
		return result

	oq = abs(flt(out_leg.actual_qty))
	if oq <= QTY_EPS:
		result["status"] = "ZERO_QTY"
		return result
	auth = abs(flt(out_leg.stock_value_difference) / oq)
	if abs(auth) <= RATE_EPS or auth < -0.0001:
		result["status"] = WAITING_UPSTREAM
		result["message"] = f"source outgoing rate unusable ({auth})"
		return result
	result["auth_rate"] = auth

	in_rate = abs(flt(in_leg.incoming_rate))
	in_svd = flt(in_leg.stock_value_difference)
	expected_in_svd = abs(flt(out_leg.stock_value_difference))
	if flt(in_leg.actual_qty) <= 0:
		expected_in_svd = -abs(expected_in_svd)

	need = abs(in_rate - auth) > 1.0 or abs(abs(in_svd) - abs(flt(out_leg.stock_value_difference))) > 1.0
	sed_name = detail or (group["se_detail"].name if group.get("se_detail") else None)
	if sed_name:
		se_rate = flt(frappe.db.get_value("Stock Entry Detail", sed_name, "basic_rate"))
		if abs((se_rate or 0) - auth) > 1.0:
			need = True
	# Also fix stale valuation_rate on the inbound leg when SVD already matches.
	if abs(flt(in_leg.valuation_rate) - auth) > 1.0:
		need = True

	if not need:
		result["status"] = ALREADY_REPAIRED
		result["pair_ok"] = True
		result["message"] = "pair already balanced"
		return result

	qty = abs(flt(in_leg.actual_qty)) or oq
	if sed_name:
		frappe.db.set_value(
			"Stock Entry Detail",
			sed_name,
			{
				"basic_rate": auth,
				"valuation_rate": auth,
				"amount": auth * qty,
				"basic_amount": auth * qty,
			},
			update_modified=False,
		)
		result["economic_writes"] += 1
	frappe.db.set_value(
		"Stock Ledger Entry",
		in_leg.name,
		{
			"incoming_rate": auth,
			"stock_value_difference": expected_in_svd,
			"valuation_rate": auth,
		},
		update_modified=False,
	)
	result["economic_writes"] += 1
	out_rate = abs(flt(out_leg.outgoing_rate) or 0)
	if abs(out_rate - auth) > 1.0:
		frappe.db.set_value(
			"Stock Ledger Entry",
			out_leg.name,
			{"outgoing_rate": auth},
			update_modified=False,
		)
		result["economic_writes"] += 1

	# Do NOT full-replay the target warehouse chronology here.
	# Prior canary (28696) rewrote 1400+ vouchers and inflated I1 while source RIV
	# failed on unrelated GL imbalance. Pair is finalized on THIS voucher only;
	# callers may enqueue a scoped official RIV afterward.
	result["replay_target"] = {
		"skipped": True,
		"reason": "unbounded_target_replay_disabled_pending_i1_safe_path",
		"from_dt": str(replay_from) if replay_from else None,
	}

	out_leg, in_leg = transfer_pair_legs(voucher, item, voucher_detail=detail, batch=batch)
	result["pair_ok"] = pair_value_consistent(
		{"svd": out_leg.stock_value_difference} if out_leg else None,
		{"svd": in_leg.stock_value_difference} if in_leg else None,
	)
	if out_leg and in_leg and not result["pair_ok"]:
		auth2 = abs(flt(out_leg.stock_value_difference) / max(abs(flt(out_leg.actual_qty)), QTY_EPS))
		frappe.db.set_value(
			"Stock Ledger Entry",
			in_leg.name,
			{
				"incoming_rate": auth2,
				"stock_value_difference": abs(flt(out_leg.stock_value_difference)),
				"valuation_rate": auth2,
			},
			update_modified=False,
		)
		if sed_name:
			q2 = abs(flt(in_leg.actual_qty)) or abs(flt(out_leg.actual_qty))
			frappe.db.set_value(
				"Stock Entry Detail",
				sed_name,
				{
					"basic_rate": auth2,
					"valuation_rate": auth2,
					"amount": auth2 * q2,
					"basic_amount": auth2 * q2,
				},
				update_modified=False,
			)
		result["auth_rate"] = auth2
		result["economic_writes"] += 1
		out_leg, in_leg = transfer_pair_legs(voucher, item, voucher_detail=detail, batch=batch)
		result["pair_ok"] = pair_value_consistent(
			{"svd": out_leg.stock_value_difference} if out_leg else None,
			{"svd": in_leg.stock_value_difference} if in_leg else None,
		)

	result["rebalanced"] = True
	result["status"] = "REBALANCED" if result["pair_ok"] else "REBALANCED_RESIDUAL"
	result["residual_class"] = residual_class(
		abs(
			abs(flt(out_leg.stock_value_difference if out_leg else 0))
			- abs(flt(in_leg.stock_value_difference if in_leg else 0))
		)
	)
	return result


def apply_transfer_repair_group(row: dict, *, patient: dict | None = None, dry_run: bool = True) -> dict:
	"""Full Transfer Repair Group apply with convergence + idempotency.

	Sequence:
	  idempotency check → write SE/SLE proposed → finalize pair (incoming↔outgoing)
	  → bounded target replay → txn-rate/SABB sync → verify

	Never full-replay the source warehouse from a distant patient-zero as part of
	ordinary Transfer repair — that rewrites unrelated chronology.
	"""
	from erpnext_extensions.iran_accounting.historical_stock.reconstruct import (
		_write_se_row,
		_write_sle_incoming,
	)
	from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
		sync_sabb_from_sle,
		write_sle_transaction_rates,
	)

	already = transfer_already_balanced(row)
	if already:
		return {**already, "dry_run": dry_run, "applied": False}

	recon = reconstruct_transfer_valuation(row)
	exp = flt(recon.get("expected_rate"))
	cur = flt(recon.get("current_rate") or row.get("current_rate") or row.get("basic_rate"))
	# Idempotent: classifier already says NO_ACTION / within 1 IRR of expected.
	if recon.get("classification") == NO_ACTION or (
		abs(cur - exp) <= 1.0 and abs(exp) > RATE_EPS
	):
		# Confirm pair before claiming complete — residual pair mismatch still needs finalize.
		group = build_transfer_repair_group(row)
		out_leg, in_leg = transfer_pair_legs(
			group["voucher"],
			group["item"],
			voucher_detail=group.get("voucher_detail"),
			batch=group.get("batch"),
		)
		if out_leg and in_leg and pair_value_consistent(
			{"svd": out_leg.stock_value_difference},
			{"svd": in_leg.stock_value_difference},
		):
			return {
				"status": ALREADY_REPAIRED,
				"written": False,
				"economic_writes": 0,
				"auth_rate": exp or abs(flt(out_leg.stock_value_difference) / max(abs(flt(out_leg.actual_qty)), QTY_EPS)),
				"message": "ALREADY_REPAIRED — transfer reconstruct NO_ACTION / pair balanced",
				"dry_run": dry_run,
				"applied": False,
			}
		# SE rate matches expected but pair cannot be finalized from outgoing SVD
		# (e.g. outgoing qty with zero SVD). Do not rewrite SE repeatedly.
		oq = abs(flt(out_leg.actual_qty)) if out_leg else 0.0
		osvd = abs(flt(out_leg.stock_value_difference)) if out_leg else 0.0
		if out_leg and oq > QTY_EPS and osvd <= VALUE_EPS:
			return {
				"status": WAITING_UPSTREAM,
				"written": False,
				"economic_writes": 0,
				"message": (
					"NO_ACTION rate match but outgoing SVD is zero — "
					"cannot finalize pair; waiting upstream source valuation"
				),
				"reconstruction": recon,
				"dry_run": dry_run,
				"applied": False,
			}

	if recon.get("classification") == WAITING_UPSTREAM:
		return {
			"status": WAITING_UPSTREAM,
			"written": False,
			"economic_writes": 0,
			"message": recon.get("reason") or "WAITING_UPSTREAM",
			"reconstruction": recon,
			"dry_run": dry_run,
			"applied": False,
		}

	if abs(exp) <= RATE_EPS:
		return {
			"status": WAITING_UPSTREAM,
			"written": False,
			"economic_writes": 0,
			"message": recon.get("reason") or "no authoritative transfer rate",
			"reconstruction": recon,
			"dry_run": dry_run,
		}
	merged = dict(row)
	merged["proposed_rate"] = exp
	merged["expected_rate"] = exp
	merged["proposed_amount"] = exp * abs(flt(merged.get("qty") or 0))
	if dry_run:
		return {
			"status": "DRY_RUN",
			"written": False,
			"economic_writes": 0,
			"proposed_rate": exp,
			"reconstruction": recon,
			"dry_run": True,
			"applied": True,
		}

	patient = patient or merged.get("patient_zero") or {}
	if isinstance(patient, str):
		patient = {"voucher_no": patient}
	# Bound finalize/target replay to this voucher's posting time.
	from_dt = patient.get("posting_datetime") or merged.get("posting_datetime")
	if not from_dt:
		pd = frappe.db.get_value(
			"Stock Entry",
			merged["voucher"],
			["posting_date", "posting_time"],
			as_dict=True,
		)
		if pd and pd.get("posting_date"):
			from_dt = f"{pd.posting_date} {pd.posting_time or '00:00:00'}"
	elif isinstance(from_dt, (tuple, list)) and len(from_dt) >= 2:
		from_dt = f"{from_dt[0]} {from_dt[1]}"
	elif isinstance(from_dt, dict):
		from_dt = f"{from_dt.get('posting_date')} {from_dt.get('posting_time') or '00:00:00'}"

	sp = f"xfer_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp)

	_write_se_row(merged)
	# When this transfer lost its outgoing rate (zero SVD), establish source debit
	# BEFORE target credit. Never credit the target alone — that invents value (I1/PZ).
	out_leg, in_leg = transfer_pair_legs(
		merged["voucher"],
		merged["item"],
		voucher_detail=merged.get("voucher_detail"),
		batch=merged.get("batch") or merged.get("batch_no") or "",
	)
	wrote_source = False
	if out_leg and abs(flt(out_leg.stock_value_difference)) <= VALUE_EPS and abs(exp) > RATE_EPS:
		oq = flt(out_leg.actual_qty)
		if abs(oq) > QTY_EPS:
			# Write every outgoing SLE for this voucher+item (multi-row transfers).
			outs = frappe.db.sql(
				"""
				SELECT name, actual_qty, voucher_detail_no
				FROM `tabStock Ledger Entry`
				WHERE voucher_no=%s AND item_code=%s AND actual_qty < 0 AND IFNULL(is_cancelled,0)=0
				""",
				(merged["voucher"], merged["item"]),
				as_dict=True,
			)
			for sle in outs:
				aq = flt(sle.actual_qty)
				frappe.db.set_value(
					"Stock Ledger Entry",
					sle.name,
					{
						"outgoing_rate": abs(exp),
						"incoming_rate": 0,
						"stock_value_difference": abs(exp) * aq,  # aq negative → negative SVD
					},
					update_modified=False,
				)
			wrote_source = True
	if not wrote_source and out_leg and abs(flt(out_leg.stock_value_difference)) <= VALUE_EPS:
		frappe.db.rollback(save_point=sp)
		return {
			"status": WAITING_UPSTREAM,
			"written": False,
			"economic_writes": 0,
			"message": "refuse target-only transfer write — source outgoing SVD still zero",
			"reconstruction": recon,
			"dry_run": False,
			"applied": False,
			"aborted": True,
		}
	_write_sle_incoming(merged)
	fin = finalize_transfer_repair_group(merged, from_dt=from_dt)
	if not fin.get("pair_ok") and fin.get("status") == WAITING_UPSTREAM:
		frappe.db.rollback(save_point=sp)
		return {
			"status": WAITING_UPSTREAM,
			"written": False,
			"economic_writes": 0,
			"message": fin.get("message") or "pair finalize failed after source write — rolled back",
			"finalize": fin,
			"reconstruction": recon,
			"dry_run": False,
			"applied": False,
			"aborted": True,
		}
	write_sle_transaction_rates(merged["voucher"], merged["item"])
	sync_sabb_from_sle(merged["voucher"], merged["item"])

	after = transfer_already_balanced({**merged, "proposed_rate": fin.get("auth_rate") or exp})
	return {
		"status": STATUS_REPAIRED if (fin.get("pair_ok") or after) else REPAIRED_BUT_STILL_EXACT,
		"written": True,
		"economic_writes": 1 + int(fin.get("economic_writes") or 0) + (1 if wrote_source else 0),
		"finalize": fin,
		"replays": {"target": fin.get("replay_target")},
		"auth_rate": fin.get("auth_rate") or exp,
		"pair_ok": bool(fin.get("pair_ok") or after),
		"already_after": after,
		"dry_run": False,
		"applied": True,
		"proposed_rate": fin.get("auth_rate") or exp,
		"source_debit_written": wrote_source,
	}
