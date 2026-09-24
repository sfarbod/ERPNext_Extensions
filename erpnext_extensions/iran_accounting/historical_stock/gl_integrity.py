# Copyright (c) 2026, ERPNext Extensions contributors
"""Stock Entry GL classification G0–G4. Rebuild only G1–G4 after SLE is healthy."""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	G0_HEALTHY,
	G1_ECONOMICALLY_WRONG,
	G2_MISSING,
	G3_UNBALANCED,
	G4_POISONED_SLE,
	VALUE_EPS,
)
from erpnext_extensions.iran_accounting.stock_posting_order.replay import sle_poison_reason


def _sle_abs_inventory_value(voucher_no: str) -> float:
	"""SLE economic magnitude for GL G1 comparison.

	Uses absolute net SVD (not gross sum of abs) so Manufacture / Repack
	vouchers that post both outbound RM and inbound FG are not double-counted.
	"""
	row = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(stock_value_difference), 0)
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND IFNULL(is_cancelled,0)=0
		""",
		voucher_no,
	)
	return abs(flt(row[0][0] if row else 0))


def _inventory_account(amap: dict, warehouse: str | None) -> str | None:
	if not warehouse:
		return None
	info = amap.get(warehouse) if amap else None
	if isinstance(info, dict):
		return info.get("account")
	return info


def _empty_gl_is_legitimate(voucher_no: str, purpose: str = "") -> dict:
	"""True when ERPNext correctly posts no GL (same stock account / zero value).

	Material Transfer / Repack / Manufacture between warehouses that share one
	inventory account produce an empty GL map — that is G0, not G2_MISSING.
	Zero-SVD manufactures with no posted GL are also healthy.
	"""
	out = {
		"legitimate": False,
		"reason": None,
		"inventory_accounts": [],
		"expected_gl_rows": 0,
	}
	if not voucher_no or not frappe.db.exists("Stock Entry", voucher_no):
		return out
	se = frappe.get_doc("Stock Entry", voucher_no)
	try:
		amap = se.get_inventory_account_map() or {}
		expected = se.get_gl_entries(amap) or []
	except Exception as exc:
		out["reason"] = f"expected_gl_error:{exc}"
		return out
	out["expected_gl_rows"] = len(expected)
	accounts: set[str] = set()
	for row in se.items:
		for wh in (row.s_warehouse, row.t_warehouse):
			acc = _inventory_account(amap, wh)
			if acc:
				accounts.add(str(acc))
	out["inventory_accounts"] = sorted(accounts)
	sle_abs = _sle_abs_inventory_value(voucher_no)
	if expected:
		out["reason"] = "expected_gl_nonempty"
		return out
	if sle_abs <= VALUE_EPS:
		out["legitimate"] = True
		out["reason"] = "zero_sle_value_no_gl_expected"
		return out
	if len(accounts) <= 1:
		out["legitimate"] = True
		out["reason"] = "single_inventory_account_nets_to_zero_gl"
		return out
	out["reason"] = "cross_account_but_empty_expected_gl"
	return out


def classify_stock_entry_gl(voucher_no: str) -> dict:
	se = frappe.db.get_value(
		"Stock Entry",
		voucher_no,
		["name", "docstatus", "company", "posting_date", "purpose", "total_outgoing_value", "total_incoming_value"],
		as_dict=True,
	)
	if not se:
		return {"voucher": voucher_no, "gl_class": G2_MISSING, "status": G2_MISSING}
	gl = frappe.db.sql(
		"""
		SELECT name, account, debit, credit, cost_center, project, against, party, party_type,
		       is_cancelled
		FROM `tabGL Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND IFNULL(is_cancelled,0)=0
		""",
		voucher_no,
		as_dict=True,
	)
	debit = sum(flt(r.debit) for r in gl)
	credit = sum(flt(r.credit) for r in gl)
	se_header = max(flt(se.total_outgoing_value), flt(se.total_incoming_value))
	gl_inventory = max(debit, credit)
	poisoned = _voucher_has_poison_sle(voucher_no)
	purpose = str(getattr(se, "purpose", None) or "")
	# Material Transfer GL is the net account movement, not SE header totals.
	transferish = purpose in (
		"Material Transfer",
		"Material Transfer for Manufacture",
		"Send to Subcontractor",
	)
	if transferish:
		expected = gl_inventory  # balanced transfer with matching GL → not G1 vs SE header
	else:
		expected = se_header
	sle_abs = _sle_abs_inventory_value(voucher_no)

	def _near(a, b) -> bool:
		diff = abs(flt(a) - flt(b))
		scale = max(abs(flt(a)), abs(flt(b)), 1.0)
		if diff / scale <= 1e-6:
			return True
		# Micro IRR tolerance only for large economic magnitudes (not tiny GL stubs).
		return scale >= 10_000 and diff <= 1000

	empty_legit = None
	if se.docstatus == 1 and not gl:
		empty_legit = _empty_gl_is_legitimate(voucher_no, purpose)
		if empty_legit.get("legitimate"):
			klass = G0_HEALTHY
		else:
			klass = G2_MISSING
	elif abs(debit - credit) > VALUE_EPS:
		klass = G3_UNBALANCED
	elif poisoned:
		klass = G4_POISONED_SLE
	elif sle_abs > VALUE_EPS and _near(gl_inventory, sle_abs):
		# GL matches SLE economic truth even when SE header totals are stale.
		klass = G0_HEALTHY
	elif transferish:
		klass = G0_HEALTHY
	elif expected > VALUE_EPS and not _near(gl_inventory, expected):
		klass = G1_ECONOMICALLY_WRONG
	else:
		klass = G0_HEALTHY

	# Phase 2 role: root vs downstream / waiting
	from erpnext_extensions.iran_accounting.historical_stock import (
		GL_DOWNSTREAM,
		GL_MANUAL,
		GL_ROOT,
		GL_WAITING_RATE,
		GL_WAITING_REPLAY,
		GL_WAITING_SLE,
	)

	if poisoned or klass == G4_POISONED_SLE:
		role = GL_WAITING_SLE
	elif klass == G0_HEALTHY:
		role = G0_HEALTHY
	elif klass in (G1_ECONOMICALLY_WRONG, G2_MISSING, G3_UNBALANCED):
		role = GL_ROOT
	else:
		role = GL_MANUAL

	reason = klass
	if empty_legit and empty_legit.get("legitimate"):
		reason = f"G0_HEALTHY — {empty_legit.get('reason')}"

	return {
		"topic": "GL",
		"voucher": voucher_no,
		"gl_class": klass,
		"gl_role": role,
		"status": klass,
		"stored_debit": debit,
		"stored_credit": credit,
		"expected_debit": expected,
		"expected_credit": expected,
		"sle_abs_value": sle_abs,
		"difference": abs(debit - credit) if klass == G3_UNBALANCED else abs(gl_inventory - (sle_abs if sle_abs > VALUE_EPS else expected)),
		"reason": reason,
		"eligible": klass in (G1_ECONOMICALLY_WRONG, G2_MISSING, G3_UNBALANCED) and not poisoned,
		"confidence": CONFIDENCE_LIKELY if klass == G1_ECONOMICALLY_WRONG else CONFIDENCE_EXACT,
		"gl_rows": len(gl),
		"sle_poisoned": bool(poisoned),
		"is_root": role == GL_ROOT,
		"empty_gl_legitimacy": empty_legit,
		"dimensions": [
			{
				"account": r.account,
				"debit": r.debit,
				"credit": r.credit,
				"cost_center": r.cost_center,
				"project": r.project,
				"party": r.party,
				"party_type": r.party_type,
			}
			for r in gl
		],
		"has_stock_adjustment": any("Stock Adjustment" in str(r.account or "") for r in gl),
		"has_round_off": any("Round Off" in str(r.account or "") for r in gl),
	}



def scan_gl_integrity(
	company=None,
	voucher=None,
	item_code=None,
	warehouse=None,
	work_order=None,
	from_date=None,
	to_date=None,
	limit=200,
) -> dict:
	conds = ["se.docstatus=1"]
	args: list = []
	join = ""
	if company:
		conds.append("se.company=%s")
		args.append(company)
	if voucher:
		conds.append("se.name=%s")
		args.append(voucher)
	if work_order:
		conds.append("se.work_order=%s")
		args.append(work_order)
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
	# Unbalanced first, then sample recent submitted SE.
	unbal = frappe.db.sql(
		"""
		SELECT voucher_no, SUM(debit) d, SUM(credit) c, ABS(SUM(debit)-SUM(credit)) diff
		FROM `tabGL Entry`
		WHERE voucher_type='Stock Entry' AND IFNULL(is_cancelled,0)=0
		GROUP BY voucher_no
		HAVING ABS(SUM(debit)-SUM(credit)) > %s
		ORDER BY diff DESC
		LIMIT %s
		""",
		(VALUE_EPS, int(limit)),
		as_dict=True,
	)
	rows = [classify_stock_entry_gl(r.voucher_no) for r in unbal]
	seen = {r["voucher"] for r in rows}
	names = frappe.db.sql(
		f"""
		SELECT DISTINCT se.name FROM `tabStock Entry` se
		{join}
		WHERE {" AND ".join(conds)}
		ORDER BY se.modified DESC
		LIMIT {int(limit)}
		""",
		args,
		pluck=True,
	)
	for name in names:
		if name in seen:
			continue
		klass = classify_stock_entry_gl(name)
		if klass["gl_class"] != G0_HEALTHY:
			rows.append(klass)
		if len(rows) >= limit:
			break
	from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

	return stamp_scan_result({"count": len(rows), "rows": rows, "by_class": _count(rows, "gl_class")})


def apply_gl_rebuild_from_current_sle(voucher_no: str, *, dry_run=True) -> dict:
	"""GL-only rebuild from current Stock Entry / SLE state.

	Does **not** consult G0/G1 classification. Caller must enforce SLE integrity.
	Never writes SLE.
	"""
	if not frappe.db.exists("Stock Entry", voucher_no):
		return {
			"voucher": voucher_no,
			"dry_run": dry_run,
			"written": False,
			"blocked": True,
			"reason": "not_stock_entry",
		}
	se = frappe.get_doc("Stock Entry", voucher_no)
	from erpnext.accounts.general_ledger import toggle_debit_credit_if_negative
	from erpnext.accounts.utils import _delete_accounting_ledger_entries

	inventory_account_map = se.get_inventory_account_map()
	expected = toggle_debit_credit_if_negative(se.get_gl_entries(inventory_account_map)) or []
	if not expected:
		return {
			"voucher": voucher_no,
			"dry_run": dry_run,
			"written": False,
			"blocked": True,
			"reason": "NO_EXPECTED_GL — Stock Entry produced empty GL map (not auto-rebuilt)",
		}
	from erpnext_extensions.iran_accounting.historical_stock.planner import _gl_expected_map_state

	map_state = _gl_expected_map_state(voucher_no)
	exp_debit = sum(flt(e.get("debit") if isinstance(e, dict) else getattr(e, "debit", 0)) for e in expected)
	exp_credit = sum(flt(e.get("credit") if isinstance(e, dict) else getattr(e, "credit", 0)) for e in expected)
	if not map_state.get("postable"):
		return {
			"voucher": voucher_no,
			"dry_run": dry_run,
			"written": False,
			"blocked": True,
			"eligible": False,
			"reason": (
				f"UNBALANCED_EXPECTED_GL — SE GL map not postable at currency precision "
				f"(raw debit {exp_debit} credit {exp_credit}; precision-diff {map_state.get('diff')}; "
				f"allowance {map_state.get('allowance')}); refuse auto-rebuild"
			),
			"map_state": map_state,
			"expected_debit": exp_debit,
			"expected_credit": exp_credit,
		}
	if dry_run:
		return {
			"voucher": voucher_no,
			"dry_run": True,
			"written": False,
			"blocked": False,
			"eligible": True,
			"expected_debit": exp_debit,
			"expected_credit": exp_credit,
			"map_state": map_state,
		}
	before = classify_stock_entry_gl(voucher_no)
	before_dims = list(before.get("dimensions") or [])
	_delete_accounting_ledger_entries("Stock Entry", voucher_no)
	try:
		se.make_gl_entries(gl_entries=expected, from_repost=True)
	except frappe.ValidationError as exc:
		return {
			"voucher": voucher_no,
			"dry_run": False,
			"written": False,
			"blocked": True,
			"eligible": False,
			"reason": f"GL_POST_REFUSED — {exc}",
			"map_state": map_state,
			"before": before,
		}
	after = classify_stock_entry_gl(voucher_no)
	if after["gl_class"] == G3_UNBALANCED:
		frappe.throw(f"GL rebuild failed: {voucher_no} still unbalanced")
	if before.get("gl_class") == G2_MISSING and after["gl_class"] == G2_MISSING:
		return {
			**after,
			"written": False,
			"blocked": True,
			"reason": "G2_STILL_MISSING — repost produced no GL rows",
			"before": before,
		}
	if after["gl_class"] in (G2_MISSING, G4_POISONED_SLE):
		return {
			**after,
			"written": False,
			"blocked": True,
			"reason": f"rebuild residual {after['gl_class']}",
			"before": before,
		}
	return {
		**after,
		"voucher": voucher_no,
		"written": True,
		"blocked": False,
		"before": before,
		"before_dimensions": before_dims,
		"expected_debit": exp_debit,
		"expected_credit": exp_credit,
		"map_state": map_state,
		"dimension_cost_centers_preserved": _dims_field_ok(before_dims, after.get("dimensions"), "cost_center"),
		"no_unexpected_stock_adjustment": not after.get("has_stock_adjustment")
		or any("Stock Adjustment" in str(d.get("account") or "") for d in before_dims),
		"no_unexpected_round_off": not after.get("has_round_off")
		or any("Round Off" in str(d.get("account") or "") for d in before_dims),
	}


def rebuild_gl_for_voucher(voucher_no: str, *, dry_run=True) -> dict:
	"""Classic G1–G3 GL rebuild. G0 remains refused (use SLE_GL_DRIFT for false-G0 drift)."""
	if not frappe.db.exists("Stock Entry", voucher_no):
		return {
			"voucher": voucher_no,
			"gl_class": G2_MISSING,
			"dry_run": dry_run,
			"written": False,
			"blocked": True,
			"reason": "not_stock_entry",
		}
	preview = classify_stock_entry_gl(voucher_no)
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan

	planned = attach_plan(preview)
	from erpnext_extensions.iran_accounting.historical_stock.planner import READY_STATUSES

	if planned["planner_status"] not in READY_STATUSES and planned["planner_status"] != "READY":
		return {**planned, "dry_run": dry_run, "written": False, "blocked": True}
	result = apply_gl_rebuild_from_current_sle(voucher_no, dry_run=dry_run)
	return {**planned, **result}


def _dims_field_ok(before, after, field) -> bool:
	b = sorted(str(d.get(field) or "") for d in (before or []) if d.get(field))
	a = sorted(str(d.get(field) or "") for d in (after or []) if d.get(field))
	# Soft: all before values still present
	return all(x in a for x in b)


def _voucher_has_poison_sle(voucher_no: str) -> bool:
	sles = frappe.db.sql(
		"""
		SELECT actual_qty, incoming_rate, valuation_rate, stock_value,
		       stock_value_difference, qty_after_transaction
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND is_cancelled=0
		""",
		voucher_no,
		as_dict=True,
	)
	return any(sle_poison_reason(s) for s in sles)


def _count(rows, key):
	out = defaultdict(int)
	for row in rows:
		out[str(row.get(key) or "")] += 1
	return dict(out)
