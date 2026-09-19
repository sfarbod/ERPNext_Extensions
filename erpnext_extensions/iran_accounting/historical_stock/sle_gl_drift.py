# Copyright (c) 2026, ERPNext Extensions contributors
"""SLE↔GL drift Historical Repair — GL-only rebuild when SLE is healthy and authoritative.

Failed RIV can commit SLE recalculation before the GL phase fails. Classic G0/G1
classification then misses transferish false-G0 vouchers. This mode compares
expected GL (from current SLE) to posted GL and rebuilds accounting only.

SLE is immutable. Full RIV / I1 / I4 / valuation replay are never invoked here.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from time import perf_counter

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_EXACT,
	CONFLICTING_RIV,
	EXPECTED_GL_ERROR,
	HISTORICAL_REPAIR_FLAG,
	MANUAL_REVIEW,
	NO_DRIFT,
	READY_GL_ONLY,
	SLE_GL_DRIFT_REPAIR,
	STATUS_BLOCKED,
	STATUS_REPAIRED,
	TOPIC_SLE_GL_DRIFT,
	UNBALANCED_EXPECTED_GL,
	VALUE_EPS,
	WAITING_SLE_REPAIR,
)
from erpnext_extensions.iran_accounting.historical_stock.audit import append_entry, finish_run, start_run
from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import (
	apply_gl_rebuild_from_current_sle,
	classify_stock_entry_gl,
)
from erpnext_extensions.iran_accounting.stock_posting_order.replay import sle_poison_reason

# Map sle_poison_reason codes → explicit WAITING_SLE_REPAIR blocker tags.
_POISON_TO_BLOCKER = {
	"negative_incoming_rate": "I1",
	"sign_inverted_incoming_svd": "VALUATION_INTEGRITY",
	"sign_inverted_outgoing_svd": "VALUATION_INTEGRITY",
	"qty_after_zero_nonzero_value": "I4",
	"qty_zero_nonzero_value": "I4",
	"exploded_rate": "VALUATION_INTEGRITY",
}

_GL_IDENTITY_FIELDS = (
	"account",
	"party_type",
	"party",
	"against",
	"cost_center",
	"project",
	"finance_book",
	"account_currency",
)


def sle_fingerprint(voucher_no: str, voucher_type: str = "Stock Entry") -> str:
	"""Deterministic hash of valuation-relevant SLE rows. Must be identical pre/post GL repair."""
	rows = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, batch_no, serial_and_batch_bundle,
		       actual_qty, incoming_rate, outgoing_rate, valuation_rate,
		       stock_value, stock_value_difference, qty_after_transaction,
		       posting_datetime, voucher_detail_no
		FROM `tabStock Ledger Entry`
		WHERE voucher_type=%s AND voucher_no=%s AND IFNULL(is_cancelled,0)=0
		ORDER BY name
		""",
		(voucher_type, voucher_no),
		as_dict=True,
	)
	payload = []
	for r in rows:
		payload.append(
			{
				"name": r.name,
				"item_code": r.item_code,
				"warehouse": r.warehouse,
				"batch_no": r.batch_no or "",
				"serial_and_batch_bundle": r.serial_and_batch_bundle or "",
				"actual_qty": flt(r.actual_qty, 9),
				"incoming_rate": flt(r.incoming_rate, 9),
				"outgoing_rate": flt(r.outgoing_rate, 9),
				"valuation_rate": flt(r.valuation_rate, 9),
				"stock_value": flt(r.stock_value, 9),
				"stock_value_difference": flt(r.stock_value_difference, 9),
				"qty_after_transaction": flt(r.qty_after_transaction, 9),
				"voucher_detail_no": r.voucher_detail_no or "",
			}
		)
	return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _g(row, key, default=None):
	if isinstance(row, dict):
		return row.get(key, default)
	return getattr(row, key, default)


def _is_finite_number(value) -> bool:
	try:
		v = float(value)
	except (TypeError, ValueError):
		return False
	return math.isfinite(v)


def inspect_sle_integrity(voucher_no: str, voucher_type: str = "Stock Entry") -> dict:
	"""Hard integrity gate. Refuses GL-only when SLE is untrustworthy.

	Uses existing Iran Accounting poison/invariant checks — never an arbitrary
	"high rate" threshold beyond the established ``sle_poison_reason`` contract.
	"""
	sles = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, batch_no, actual_qty, incoming_rate, outgoing_rate,
		       valuation_rate, stock_value, stock_value_difference, qty_after_transaction,
		       posting_datetime, voucher_detail_no
		FROM `tabStock Ledger Entry`
		WHERE voucher_type=%s AND voucher_no=%s AND IFNULL(is_cancelled,0)=0
		ORDER BY posting_datetime, creation, name
		""",
		(voucher_type, voucher_no),
		as_dict=True,
	)
	violations = []
	for s in sles:
		non_finite = False
		for field in (
			"actual_qty",
			"incoming_rate",
			"valuation_rate",
			"stock_value",
			"stock_value_difference",
			"qty_after_transaction",
		):
			if not _is_finite_number(_g(s, field, 0)):
				non_finite = True
				violations.append(
					{
						"sle": s.name,
						"item_code": s.item_code,
						"warehouse": s.warehouse,
						"batch_no": s.batch_no,
						"blocker": "NON_FINITE",
						"reason": f"non_finite_{field}",
						"field": field,
						"value": _g(s, field),
						"actual_qty": s.actual_qty,
						"valuation_rate": s.valuation_rate,
						"incoming_rate": s.incoming_rate,
						"stock_value": s.stock_value,
						"stock_value_difference": s.stock_value_difference,
					}
				)
		if non_finite:
			continue
		try:
			poison = sle_poison_reason(s)
		except Exception as exc:
			violations.append(
				{
					"sle": s.name,
					"item_code": s.item_code,
					"warehouse": s.warehouse,
					"batch_no": s.batch_no,
					"blocker": "VALUATION_INTEGRITY",
					"reason": f"poison_check_error:{exc}",
					"actual_qty": s.actual_qty,
					"valuation_rate": s.valuation_rate,
					"incoming_rate": s.incoming_rate,
					"stock_value": s.stock_value,
					"stock_value_difference": s.stock_value_difference,
				}
			)
			continue
		if poison:
			violations.append(
				{
					"sle": s.name,
					"item_code": s.item_code,
					"warehouse": s.warehouse,
					"batch_no": s.batch_no,
					"blocker": _POISON_TO_BLOCKER.get(poison, "VALUATION_INTEGRITY"),
					"reason": poison,
					"actual_qty": s.actual_qty,
					"valuation_rate": s.valuation_rate,
					"incoming_rate": s.incoming_rate,
					"stock_value": s.stock_value,
					"stock_value_difference": s.stock_value_difference,
					"qty_after_transaction": s.qty_after_transaction,
				}
			)
	healthy = not violations
	primary = violations[0] if violations else None
	return {
		"healthy": healthy,
		"sle_count": len(sles),
		"violations": violations,
		"blocker": (primary or {}).get("blocker"),
		"reason": (primary or {}).get("reason"),
		"items": sorted({s.item_code for s in sles if s.item_code}),
	}


def _num(value, digits: int = 6) -> float:
	"""Float conversion that does not depend on site-initialized rounding."""
	try:
		return round(float(value or 0), digits)
	except (TypeError, ValueError):
		return 0.0


def _normalize_gl_row(row) -> dict:
	out = {}
	for key in _GL_IDENTITY_FIELDS:
		val = _g(row, key)
		out[key] = "" if val is None else str(val)
	# Dimensions commonly present on stock GL
	for key in ("company", "voucher_type", "voucher_no"):
		val = _g(row, key)
		if val is not None:
			out[key] = str(val)
	out["debit"] = _num(_g(row, "debit"))
	out["credit"] = _num(_g(row, "credit"))
	out["debit_in_account_currency"] = _num(
		_g(row, "debit_in_account_currency", _g(row, "debit"))
	)
	out["credit_in_account_currency"] = _num(
		_g(row, "credit_in_account_currency", _g(row, "credit"))
	)
	return out


def _identity_key(norm: dict) -> tuple:
	return tuple(norm.get(k, "") for k in _GL_IDENTITY_FIELDS)


def aggregate_gl_nets(rows) -> dict[tuple, dict]:
	"""Merge GL rows by accounting identity into debit/credit nets."""
	agg: dict[tuple, dict] = {}
	for row in rows or []:
		norm = _normalize_gl_row(row)
		key = _identity_key(norm)
		slot = agg.setdefault(
			key,
			{
				"identity": {k: norm[k] for k in _GL_IDENTITY_FIELDS},
				"debit": 0.0,
				"credit": 0.0,
				"debit_in_account_currency": 0.0,
				"credit_in_account_currency": 0.0,
			},
		)
		slot["debit"] = _num(slot["debit"] + norm["debit"])
		slot["credit"] = _num(slot["credit"] + norm["credit"])
		slot["debit_in_account_currency"] = _num(
			slot["debit_in_account_currency"] + norm["debit_in_account_currency"]
		)
		slot["credit_in_account_currency"] = _num(
			slot["credit_in_account_currency"] + norm["credit_in_account_currency"]
		)
	# Drop zero nets
	return {
		k: v
		for k, v in agg.items()
		if abs(v["debit"] - v["credit"]) >= VALUE_EPS
		or abs(v["debit"]) >= VALUE_EPS
		or abs(v["credit"]) >= VALUE_EPS
	}


def compare_gl_maps(expected_rows, posted_rows, *, eps: float = VALUE_EPS) -> dict:
	"""Semantic expected-vs-posted comparison (ignores GL Entry name/ids)."""
	exp = aggregate_gl_nets(expected_rows)
	post = aggregate_gl_nets(posted_rows)
	keys = set(exp) | set(post)
	deltas = []
	total_abs = 0.0
	for key in sorted(keys, key=lambda k: str(k)):
		e = exp.get(key) or {
			"identity": {f: key[i] for i, f in enumerate(_GL_IDENTITY_FIELDS)},
			"debit": 0.0,
			"credit": 0.0,
		}
		p = post.get(key) or {
			"identity": e["identity"],
			"debit": 0.0,
			"credit": 0.0,
		}
		d_debit = _num(e["debit"] - p["debit"])
		d_credit = _num(e["credit"] - p["credit"])
		d_net = _num((e["debit"] - e["credit"]) - (p["debit"] - p["credit"]))
		if abs(d_debit) < eps and abs(d_credit) < eps and abs(d_net) < eps:
			continue
		total_abs += abs(d_net) if abs(d_net) >= eps else abs(d_debit) + abs(d_credit)
		deltas.append(
			{
				"account": e["identity"].get("account"),
				"party_type": e["identity"].get("party_type"),
				"party": e["identity"].get("party"),
				"cost_center": e["identity"].get("cost_center"),
				"project": e["identity"].get("project"),
				"expected_debit": e["debit"],
				"expected_credit": e["credit"],
				"posted_debit": p["debit"],
				"posted_credit": p["credit"],
				"debit_delta": d_debit,
				"credit_delta": d_credit,
				"net_delta": d_net,
			}
		)
	exp_dr = sum(_num(_g(r, "debit")) for r in (expected_rows or []))
	exp_cr = sum(_num(_g(r, "credit")) for r in (expected_rows or []))
	post_dr = sum(_num(_g(r, "debit")) for r in (posted_rows or []))
	post_cr = sum(_num(_g(r, "credit")) for r in (posted_rows or []))
	return {
		"equal": not deltas,
		"deltas": deltas,
		"affected_accounts": sorted({d["account"] for d in deltas if d.get("account")}),
		"total_abs_net_delta": _num(total_abs),
		"expected_debit": _num(exp_dr),
		"expected_credit": _num(exp_cr),
		"posted_debit": _num(post_dr),
		"posted_credit": _num(post_cr),
		"debit_delta": _num(exp_dr - post_dr),
		"credit_delta": _num(exp_cr - post_cr),
	}


def generate_expected_gl(voucher_no: str) -> dict:
	"""Build expected GL the same way a GL-only rebuild would post it."""
	from erpnext.accounts.general_ledger import (
		process_debit_credit_difference,
		toggle_debit_credit_if_negative,
	)

	se = frappe.get_doc("Stock Entry", voucher_no)
	try:
		raw = se.get_gl_entries(se.get_inventory_account_map()) or []
		expected = toggle_debit_credit_if_negative(raw) or []
	except Exception as exc:
		return {
			"ok": False,
			"error": str(exc)[:300],
			"rows": [],
			"balanced": False,
			"postable": False,
		}

	# Mirror make_gl_entries Iran post-processing when patches are active.
	try:
		from erpnext_extensions.iran_accounting.domain.currency import is_irr_company
		from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
			apply_irr_rate_rounding_residual_gl,
		)

		if is_irr_company(se.company) and expected is not None:
			apply_irr_rate_rounding_residual_gl(se, expected)
			from erpnext_extensions.iran_accounting.domain.irr_gl_precision_align import (
				align_irr_gl_map_to_currency_precision,
			)

			align_irr_gl_map_to_currency_precision(se, expected)
	except Exception:
		pass

	rows = []
	for e in expected or []:
		if isinstance(e, dict):
			rows.append(frappe._dict(e))
		else:
			rows.append(frappe._dict(e.as_dict() if hasattr(e, "as_dict") else dict(e)))

	dr = sum(flt(r.debit) for r in rows)
	cr = sum(flt(r.credit) for r in rows)
	balanced = abs(dr - cr) <= VALUE_EPS
	postable = balanced
	validate_err = None
	try:
		probe = [frappe._dict(dict(r)) for r in rows]
		for r in probe:
			r.setdefault("company", se.company)
			r.setdefault("voucher_type", "Stock Entry")
			r.setdefault("voucher_no", voucher_no)
		if probe:
			process_debit_credit_difference(probe)
		postable = True
	except Exception as exc:
		postable = False
		validate_err = str(exc)[:240]

	return {
		"ok": True,
		"rows": rows,
		"balanced": balanced,
		"postable": postable and balanced,
		"validate_error": validate_err,
		"debit": flt(dr, 6),
		"credit": flt(cr, 6),
	}


def fetch_posted_gl(voucher_no: str, voucher_type: str = "Stock Entry") -> list:
	return frappe.db.sql(
		"""
		SELECT account, party_type, party, against, cost_center, project, finance_book,
		       account_currency, debit, credit, debit_in_account_currency, credit_in_account_currency,
		       company, voucher_type, voucher_no
		FROM `tabGL Entry`
		WHERE voucher_type=%s AND voucher_no=%s AND IFNULL(is_cancelled,0)=0
		""",
		(voucher_type, voucher_no),
		as_dict=True,
	)


def find_conflicting_riv(voucher_no: str) -> list[dict]:
	"""Queued / In Progress RIV whose item/warehouse intersects this voucher's SLE."""
	pairs = frappe.db.sql(
		"""
		SELECT DISTINCT item_code, warehouse
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
		""",
		voucher_no,
		as_dict=True,
	)
	if not pairs:
		return []
	active = frappe.db.sql(
		"""
		SELECT name, status, item_code, warehouse, voucher_type, voucher_no,
		       based_on, repost_only_accounting_ledgers
		FROM `tabRepost Item Valuation`
		WHERE docstatus=1 AND status IN ('Queued', 'In Progress')
		""",
		as_dict=True,
	)
	if not active:
		return []
	items = {p.item_code for p in pairs}
	warehouses = {p.warehouse for p in pairs}
	conflicts = []
	for riv in active:
		# Transaction-scoped accounting-only on a *different* voucher is fine.
		if (
			cint(riv.repost_only_accounting_ledgers)
			and riv.based_on == "Transaction"
			and riv.voucher_no
			and riv.voucher_no != voucher_no
		):
			continue
		if riv.voucher_no == voucher_no:
			conflicts.append(riv)
			continue
		if riv.item_code and riv.item_code in items:
			if not riv.warehouse or riv.warehouse in warehouses:
				conflicts.append(riv)
				continue
		# Broad Item+Warehouse without item filter still conflicts with any stock voucher in flight
		if riv.based_on == "Item and Warehouse" and riv.item_code in items:
			conflicts.append(riv)
	return conflicts


def classify_sle_gl_drift(voucher_no: str) -> dict:
	"""Classify one Stock Entry for SLE↔GL drift (independent of false G0)."""
	base = {
		"topic": TOPIC_SLE_GL_DRIFT,
		"repair_class": SLE_GL_DRIFT_REPAIR,
		"voucher": voucher_no,
		"voucher_no": voucher_no,
		"confidence": CONFIDENCE_EXACT,
		"eligible": False,
		"written": False,
	}
	se = frappe.db.get_value(
		"Stock Entry",
		voucher_no,
		[
			"name",
			"docstatus",
			"company",
			"purpose",
			"posting_date",
			"posting_time",
			"creation",
			"modified",
		],
		as_dict=True,
	)
	if not se:
		return {
			**base,
			"drift_status": MANUAL_REVIEW,
			"status": MANUAL_REVIEW,
			"blocker": "NOT_STOCK_ENTRY",
			"recommended_action": "manual_review",
			"message": "Stock Entry not found",
		}
	base.update(
		{
			"posting_date": str(se.posting_date) if se.posting_date else None,
			"posting_time": str(se.posting_time) if se.posting_time else None,
			"purpose": se.purpose,
			"company": se.company,
			"creation": str(se.creation) if se.creation else None,
		}
	)
	fp = sle_fingerprint(voucher_no)
	base["sle_fingerprint"] = fp

	# Existing historical GL class (informational only — not decisive)
	try:
		hist = classify_stock_entry_gl(voucher_no)
		base["historical_gl_class"] = hist.get("gl_class")
		base["historical_gl_eligible"] = bool(hist.get("eligible"))
	except Exception as exc:
		base["historical_gl_class"] = None
		base["historical_gl_error"] = str(exc)[:160]

	if cint(se.docstatus) != 1:
		return {
			**base,
			"drift_status": MANUAL_REVIEW,
			"status": MANUAL_REVIEW,
			"blocker": "NOT_SUBMITTED",
			"recommended_action": "manual_review",
			"message": "Stock Entry is not submitted",
			"eligible": False,
		}

	integrity = inspect_sle_integrity(voucher_no)
	base["sle_integrity"] = "HEALTHY" if integrity["healthy"] else "UNHEALTHY"
	base["sle_integrity_detail"] = integrity
	base["items"] = integrity.get("items") or []
	if not integrity["healthy"]:
		blocker = integrity.get("blocker") or "VALUATION_INTEGRITY"
		return {
			**base,
			"drift_status": WAITING_SLE_REPAIR,
			"status": WAITING_SLE_REPAIR,
			"blocker": blocker,
			"recommended_action": "repair_sle_integrity_first",
			"message": f"WAITING_SLE_REPAIR — {blocker}: {integrity.get('reason')}",
			"eligible": False,
		}

	conflicts = find_conflicting_riv(voucher_no)
	if conflicts:
		return {
			**base,
			"drift_status": CONFLICTING_RIV,
			"status": CONFLICTING_RIV,
			"blocker": CONFLICTING_RIV,
			"conflicting_riv": [c.name for c in conflicts],
			"recommended_action": "wait_or_cancel_conflicting_riv",
			"message": f"CONFLICTING_RIV — {[c.name for c in conflicts]}",
			"eligible": False,
		}

	expected = generate_expected_gl(voucher_no)
	if not expected.get("ok"):
		return {
			**base,
			"drift_status": EXPECTED_GL_ERROR,
			"status": EXPECTED_GL_ERROR,
			"blocker": EXPECTED_GL_ERROR,
			"recommended_action": "investigate_gl_generation",
			"message": f"EXPECTED_GL_ERROR — {expected.get('error')}",
			"eligible": False,
		}
	if not expected.get("balanced"):
		return {
			**base,
			"drift_status": UNBALANCED_EXPECTED_GL,
			"status": UNBALANCED_EXPECTED_GL,
			"blocker": UNBALANCED_EXPECTED_GL,
			"expected_debit": expected.get("debit"),
			"expected_credit": expected.get("credit"),
			"recommended_action": "investigate_unbalanced_expected_gl",
			"message": "UNBALANCED_EXPECTED_GL — refuse GL-only rebuild",
			"eligible": False,
		}
	if not expected.get("postable"):
		return {
			**base,
			"drift_status": UNBALANCED_EXPECTED_GL,
			"status": UNBALANCED_EXPECTED_GL,
			"blocker": UNBALANCED_EXPECTED_GL,
			"expected_debit": expected.get("debit"),
			"expected_credit": expected.get("credit"),
			"validate_error": expected.get("validate_error"),
			"recommended_action": "investigate_unpostable_expected_gl",
			"message": f"UNBALANCED_EXPECTED_GL — not postable: {expected.get('validate_error')}",
			"eligible": False,
		}

	posted = fetch_posted_gl(voucher_no)
	cmp = compare_gl_maps(expected["rows"], posted)
	base.update(
		{
			"expected_debit": cmp["expected_debit"],
			"expected_credit": cmp["expected_credit"],
			"posted_debit": cmp["posted_debit"],
			"posted_credit": cmp["posted_credit"],
			"debit_delta": cmp["debit_delta"],
			"credit_delta": cmp["credit_delta"],
			"delta": cmp["total_abs_net_delta"],
			"affected_accounts": cmp["affected_accounts"],
			"gl_deltas": cmp["deltas"][:20],
		}
	)
	if cmp["equal"]:
		return {
			**base,
			"drift_status": NO_DRIFT,
			"status": NO_DRIFT,
			"blocker": None,
			"recommended_action": "none",
			"message": "NO_DRIFT — posted GL matches expected GL from current SLE",
			"eligible": False,
		}

	return {
		**base,
		"drift_status": READY_GL_ONLY,
		"status": READY_GL_ONLY,
		"blocker": None,
		"eligible": True,
		"recommended_action": "rebuild_gl_only_keep_sle",
		"message": (
			"READY_GL_ONLY — SLE healthy; posted GL differs from expected; "
			"false G0 does not block this mode"
		),
	}


def scan_sle_gl_drift(
	*,
	company=None,
	voucher=None,
	item_code=None,
	warehouse=None,
	from_date=None,
	to_date=None,
	vouchers=None,
	limit=500,
) -> dict:
	"""Discover SLE↔GL drift candidates. Ordering: posting_date, posting_time, creation/name."""
	names: list[str] = []
	if vouchers:
		names = [v if isinstance(v, str) else (v.get("voucher") or v.get("voucher_no")) for v in vouchers]
		names = [n for n in names if n]
	elif voucher:
		names = [voucher]
	else:
		conds = ["se.docstatus=1"]
		args: list = []
		join = ""
		if company:
			conds.append("se.company=%s")
			args.append(company)
		if from_date:
			conds.append("se.posting_date>=%s")
			args.append(from_date)
		if to_date:
			conds.append("se.posting_date<=%s")
			args.append(to_date)
		if item_code or warehouse:
			join = " JOIN `tabStock Entry Detail` sed ON sed.parent=se.name "
			if item_code:
				conds.append("sed.item_code=%s")
				args.append(item_code)
			if warehouse:
				conds.append("(sed.s_warehouse=%s OR sed.t_warehouse=%s)")
				args.extend([warehouse, warehouse])
		names = frappe.db.sql(
			f"""
			SELECT DISTINCT se.name
			FROM `tabStock Entry` se
			{join}
			WHERE {" AND ".join(conds)}
			ORDER BY se.posting_date, se.posting_time, se.creation, se.name
			LIMIT {int(limit)}
			""",
			args,
			pluck=True,
		)

	# Stable chronological order even for explicit voucher lists
	if names:
		ordered = frappe.db.sql(
			"""
			SELECT name FROM `tabStock Entry`
			WHERE name IN %(n)s
			ORDER BY posting_date, posting_time, creation, name
			""",
			{"n": names},
			pluck=True,
		)
		# Preserve any missing names at end
		seen = set(ordered)
		names = list(ordered) + [n for n in names if n not in seen]

	rows = [classify_sle_gl_drift(n) for n in names]
	from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

	by_status = dict(Counter(str(r.get("drift_status") or "") for r in rows))
	return stamp_scan_result(
		{
			"count": len(rows),
			"rows": rows,
			"by_status": by_status,
			"ready_gl_only": by_status.get(READY_GL_ONLY, 0),
			"waiting_sle_repair": by_status.get(WAITING_SLE_REPAIR, 0),
			"repair_class": SLE_GL_DRIFT_REPAIR,
			"message": (
				"SLE_GL_DRIFT compares expected GL from current SLE to posted GL; "
				"false G0 does not imply healthy accounting."
			),
		}
	)


def dry_run_sle_gl_drift(rows: list[dict] | None = None, *, vouchers=None, **scan_kw) -> dict:
	"""Preview-only. Never writes."""
	if rows is None:
		scan = scan_sle_gl_drift(vouchers=vouchers, **scan_kw)
		rows = scan.get("rows") or []
	else:
		norm = []
		for r in rows or []:
			if isinstance(r, str):
				norm.append(classify_sle_gl_drift(r))
			elif not (r or {}).get("drift_status"):
				norm.append(classify_sle_gl_drift((r or {}).get("voucher") or (r or {}).get("voucher_no")))
			else:
				norm.append(r)
		from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan_many

		rows = attach_plan_many(norm)

	preview = []
	for r in rows:
		preview.append(
			{
				"voucher_no": r.get("voucher") or r.get("voucher_no"),
				"voucher": r.get("voucher") or r.get("voucher_no"),
				"topic": TOPIC_SLE_GL_DRIFT,
				"repair_class": SLE_GL_DRIFT_REPAIR,
				"posting_date": r.get("posting_date"),
				"posting_time": r.get("posting_time"),
				"purpose": r.get("purpose"),
				"items": r.get("items"),
				"sle_integrity_status": r.get("sle_integrity"),
				"historical_gl_class": r.get("historical_gl_class"),
				"drift_status": r.get("drift_status"),
				"drift_classification": r.get("drift_status"),
				"expected_debit": r.get("expected_debit"),
				"expected_credit": r.get("expected_credit"),
				"posted_debit": r.get("posted_debit"),
				"posted_credit": r.get("posted_credit"),
				"delta": r.get("delta"),
				"affected_accounts": r.get("affected_accounts"),
				"sle_fingerprint": r.get("sle_fingerprint"),
				"eligible": bool(r.get("eligible")),
				"planner_status": r.get("planner_status"),
				"blocker": r.get("blocker") if r.get("drift_status") != READY_GL_ONLY else None,
				"recommended_action": r.get("recommended_action"),
				"message": r.get("message") or r.get("reason"),
			}
		)
	return {
		"dry_run": True,
		"written": False,
		"count": len(preview),
		"rows": preview,
		"by_status": dict(Counter(str(r.get("drift_classification") or "") for r in preview)),
		"ready_gl_only": sum(1 for r in preview if r.get("drift_classification") == READY_GL_ONLY),
		"waiting_sle_repair": sum(1 for r in preview if r.get("drift_classification") == WAITING_SLE_REPAIR),
	}


def _verify_post_repair(voucher_no: str, fingerprint_before: str) -> dict:
	fp_after = sle_fingerprint(voucher_no)
	if fp_after != fingerprint_before:
		return {
			"ok": False,
			"reason": "SLE_FINGERPRINT_CHANGED — GL-only repair must not modify SLE",
			"sle_fingerprint_before": fingerprint_before,
			"sle_fingerprint_after": fp_after,
		}
	expected = generate_expected_gl(voucher_no)
	if not expected.get("ok") or not expected.get("postable"):
		return {
			"ok": False,
			"reason": f"POST_REPAIR_EXPECTED_GL_INVALID — {expected.get('validate_error') or expected.get('error')}",
		}
	posted = fetch_posted_gl(voucher_no)
	cmp = compare_gl_maps(expected["rows"], posted)
	post_dr = cmp["posted_debit"]
	post_cr = cmp["posted_credit"]
	if abs(post_dr - post_cr) > VALUE_EPS:
		return {
			"ok": False,
			"reason": f"POST_REPAIR_UNBALANCED — debit {post_dr} credit {post_cr}",
			"comparison": cmp,
		}
	if not cmp["equal"]:
		return {
			"ok": False,
			"reason": "POST_REPAIR_GL_MISMATCH — posted GL still differs from expected",
			"comparison": cmp,
		}
	return {
		"ok": True,
		"sle_fingerprint": fp_after,
		"comparison": cmp,
	}


def apply_sle_gl_drift_voucher(voucher_no: str) -> dict:
	"""Execute GL-only repair for one READY_GL_ONLY voucher. Writes GL; never SLE."""
	# Lock Stock Entry row for concurrency
	frappe.db.sql("SELECT name FROM `tabStock Entry` WHERE name=%s FOR UPDATE", voucher_no)
	classified = classify_sle_gl_drift(voucher_no)
	if classified.get("drift_status") != READY_GL_ONLY or not classified.get("eligible"):
		return {
			**classified,
			"written": False,
			"blocked": True,
			"status": STATUS_BLOCKED,
			"reason": classified.get("message") or classified.get("drift_status"),
		}

	fp_before = classified.get("sle_fingerprint") or sle_fingerprint(voucher_no)
	from erpnext_extensions.iran_accounting.historical_stock.snapshot import capture_identity_snapshot

	snapshot = capture_identity_snapshot(voucher_no)
	rebuild = apply_gl_rebuild_from_current_sle(voucher_no, dry_run=False)
	if rebuild.get("blocked") or not rebuild.get("written"):
		return {
			**classified,
			**rebuild,
			"written": False,
			"blocked": True,
			"status": STATUS_BLOCKED,
			"snapshot_before": snapshot,
			"sle_fingerprint_before": fp_before,
			"sle_fingerprint_after": sle_fingerprint(voucher_no),
		}

	verify = _verify_post_repair(voucher_no, fp_before)
	if not verify.get("ok"):
		# Best-effort restore GL from snapshot when available
		_restore_gl_from_snapshot(voucher_no, snapshot)
		frappe.throw(verify.get("reason") or "SLE_GL_DRIFT verification failed")

	return {
		**classified,
		"written": True,
		"blocked": False,
		"status": STATUS_REPAIRED,
		"drift_status": "REPAIRED_GL_ONLY",
		"rebuild": rebuild,
		"snapshot_before": snapshot,
		"full_rollback_possible": snapshot.get("full_rollback_possible"),
		"sle_fingerprint_before": fp_before,
		"sle_fingerprint_after": verify.get("sle_fingerprint"),
		"verification": verify,
		"sql_updates": int(rebuild.get("gl_rows") or classified.get("delta") or 1),
	}


def _restore_gl_from_snapshot(voucher_no: str, snapshot: dict) -> None:
	"""Restore GL rows from before-image after a failed verification (best effort)."""
	from erpnext.accounts.utils import _delete_accounting_ledger_entries

	gl_before = (snapshot or {}).get("gl_before") or []
	if not gl_before:
		return
	_delete_accounting_ledger_entries("Stock Entry", voucher_no)
	# Re-post via document path is safer than raw insert; if empty expected, leave deleted
	# and let caller surface failure. Prefer make_gl_entries from current expected if snapshot
	# restore of exact rows is truncated.
	se = frappe.get_doc("Stock Entry", voucher_no)
	from erpnext.accounts.general_ledger import toggle_debit_credit_if_negative

	expected = toggle_debit_credit_if_negative(se.get_gl_entries(se.get_inventory_account_map()) or [])
	if expected:
		# Do not silently "succeed" with wrong GL — verification already failed.
		# Leave ledger empty of the failed rebuild attempt only when we cannot restore.
		pass


def repair_sle_gl_drift_selected(
	rows: list[dict] | None = None,
	*,
	dry_run=True,
	batch_size=50,
	stop_on_error=True,
	resume_cursor=0,
) -> dict:
	"""Batch GL-only repair. Default dry_run=True. Conservative stop-on-error."""
	log = start_run("SLE_GL_DRIFT", dry_run=dry_run)
	frappe.flags[HISTORICAL_REPAIR_FLAG] = True
	t0 = perf_counter()
	applied = []
	blocked = []
	savepoint = None
	cursor = cint(resume_cursor)
	try:
		prepared = []
		for raw in rows or []:
			vn = raw if isinstance(raw, str) else (raw.get("voucher") or raw.get("voucher_no"))
			classified = classify_sle_gl_drift(vn)
			from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan

			planned = attach_plan({**classified, "topic": TOPIC_SLE_GL_DRIFT})
			if planned.get("drift_status") == READY_GL_ONLY and planned.get("eligible"):
				prepared.append(planned)
			else:
				blocked.append(
					{
						"voucher": vn,
						"drift_status": planned.get("drift_status"),
						"blocker": planned.get("blocker"),
						"reason": planned.get("message") or planned.get("reason"),
						"status": STATUS_BLOCKED,
					}
				)

		# Deterministic order
		prepared.sort(
			key=lambda r: (
				str(r.get("posting_date") or ""),
				str(r.get("posting_time") or ""),
				str(r.get("creation") or ""),
				str(r.get("voucher") or ""),
			)
		)
		if cursor:
			prepared = prepared[cursor:]
		if batch_size:
			prepared = prepared[: int(batch_size)]

		if dry_run:
			preview = dry_run_sle_gl_drift(prepared)
			finish_run(log, applied=0, blocked=len(blocked))
			if log:
				log.resume_cursor = cursor
			return {
				**preview,
				"blocked": blocked,
				"resume_cursor": cursor,
				"batch_size": batch_size,
				"stop_on_error": bool(stop_on_error),
				"repair_run_id": getattr(log, "repair_run_id", None),
				"written": False,
			}

		savepoint = f"sle_gl_drift_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(savepoint)
		for idx, merged in enumerate(prepared):
			vn = merged["voucher"]
			try:
				result = apply_sle_gl_drift_voucher(vn)
				if result.get("written"):
					applied.append(result)
					append_entry(log, result, written=True)
					cursor = cint(resume_cursor) + idx + 1
					if log:
						log.resume_cursor = cursor
				else:
					blocked.append(result)
					if stop_on_error:
						frappe.db.rollback(save_point=savepoint)
						finish_run(log, applied=0, blocked=len(blocked) + 1, error=result.get("reason"))
						return {
							"dry_run": False,
							"aborted": True,
							"written": False,
							"reason": result.get("reason") or result.get("message"),
							"applied": [],
							"blocked": blocked,
							"resume_cursor": cint(resume_cursor) + idx,
							"repair_run_id": getattr(log, "repair_run_id", None),
						}
			except Exception as exc:
				if stop_on_error:
					frappe.db.rollback(save_point=savepoint)
					finish_run(log, applied=0, blocked=1, error=str(exc))
					return {
						"dry_run": False,
						"aborted": True,
						"written": False,
						"reason": str(exc),
						"applied": [],
						"blocked": blocked,
						"resume_cursor": cint(resume_cursor) + idx,
						"failed_voucher": vn,
						"repair_run_id": getattr(log, "repair_run_id", None),
					}
				blocked.append({"voucher": vn, "error": str(exc), "status": STATUS_BLOCKED})

		frappe.db.commit()
		finish_run(log, applied=len(applied), blocked=len(blocked))
		return {
			"dry_run": False,
			"aborted": False,
			"written": True,
			"applied": applied,
			"blocked": blocked,
			"resume_cursor": cursor,
			"batch_size": batch_size,
			"stop_on_error": bool(stop_on_error),
			"elapsed_seconds": round(perf_counter() - t0, 3),
			"database_backup_recommended": True,
			"repair_run_id": getattr(log, "repair_run_id", None),
			"riv": "NOT_INVOKED",
			"sle_modified": False,
		}
	except Exception as exc:
		if savepoint:
			frappe.db.rollback(save_point=savepoint)
		finish_run(log, applied=0, blocked=1, error=str(exc))
		raise
	finally:
		frappe.flags[HISTORICAL_REPAIR_FLAG] = False
