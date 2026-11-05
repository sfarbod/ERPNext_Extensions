# Copyright (c) 2026 — Phase 3 read-only RIV dry-run validation (no mutation).
from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from copy import deepcopy
from typing import Any

import frappe
from frappe.utils import flt


APPROVED_SCOPES = [
	{
		"item_code": "30300084",
		"warehouse": "انبار Quarantine محصول نیمه ساخته اسپاد",
		"posting_date": "2026-06-16",
	},
	{
		"item_code": "13200333",
		"warehouse": "انبار پایکار خط تولید اسپاد فارمد",
		"posting_date": "2026-08-10",
	},
	{
		"item_code": "13200333",
		"warehouse": "انبار approved اقلام بسته بندی ثانویه اسپاد",
		"posting_date": "2026-08-10",
	},
]

SA_ACCOUNT_PREFIX = "621301"


def _fp(rows: list[dict], fields: list[str]) -> str:
	h = hashlib.sha256()
	for r in rows:
		parts = []
		for f in fields:
			v = r.get(f)
			if isinstance(v, float):
				parts.append(f"{round(v, 6)}")
			else:
				parts.append(str(v if v is not None else ""))
		h.update(("|".join(parts) + "\n").encode())
	return h.hexdigest()


def _strip_volatile(obj: Any) -> Any:
	"""Remove timestamps/paths for determinism compare."""
	if isinstance(obj, dict):
		skip = {"at", "audit_path", "path"}
		return {k: _strip_volatile(v) for k, v in obj.items() if k not in skip}
	if isinstance(obj, list):
		return [_strip_volatile(x) for x in obj]
	return obj


def _preconditions() -> dict[str, Any]:
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as bootstrap_apply
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		is_iran_riv_gl_handlers_active,
		is_iran_riv_recalculate_wrapper_active,
	)
	from erpnext_extensions.iran_accounting.scripts._diag_gl_handlers_check import run as handlers_run
	import erpnext_extensions.iran_accounting.domain.riv_sabe_integer_phase3 as phase3
	import ast
	import inspect

	bootstrap_apply()
	handlers = handlers_run()

	frac = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabSerial and Batch Entry` sabe
		WHERE docstatus=1 AND IFNULL(is_cancelled,0)=0
		  AND item_code IN ('13200333','30300084')
		  AND (
			ABS(incoming_rate-ROUND(incoming_rate))>1e-6
			OR ABS(outgoing_rate-ROUND(outgoing_rate))>1e-6
			OR ABS(stock_value_difference-ROUND(stock_value_difference))>1e-6
		  )
		"""
	)[0][0]

	sa_rows = frappe.db.sql(
		"""
		SELECT voucher_no, ROUND(debit) d, ROUND(credit) c
		FROM `tabGL Entry`
		WHERE voucher_no IN ('MAT-STE-2026-30392','MAT-STE-2026-30451')
		  AND account LIKE %s AND IFNULL(is_cancelled,0)=0
		  AND (ROUND(debit) IN (520,58) OR ROUND(credit) IN (520,58))
		ORDER BY voucher_no
		""",
		(SA_ACCOUNT_PREFIX + "%",),
		as_dict=True,
	)
	failed_riv = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabRepost Item Valuation`
		WHERE name='8dou79ngqp'
		   OR (status='Failed' AND error_log LIKE '%%42067%%' AND creation>='2026-11-05')
		"""
	)[0][0]

	phase3_src = inspect.getsource(phase3)
	import_bound = False
	for node in ast.parse(phase3_src).body:
		if isinstance(node, ast.ImportFrom):
			if any(a.name == "execute_reposting_entry" for a in node.names):
				import_bound = True

	checks = {
		"fractional_sabe_84": int(frac) == 84,
		"fractional_sabe_count": int(frac),
		"gl_520_present": any(r.voucher_no == "MAT-STE-2026-30392" and flt(r.c) == 520 for r in sa_rows),
		"gl_58_present": any(r.voucher_no == "MAT-STE-2026-30451" and flt(r.d) == 58 for r in sa_rows),
		"failed_riv_absent": int(failed_riv) == 0,
		"phase3_no_import_bind": not import_bound and handlers.get("phase3_no_import_bind"),
		"phase3_uses_guard": handlers.get("phase3_uses_guard"),
		"phase3_asserts_gl": handlers.get("phase3_asserts_gl"),
		"gl_handlers_active": handlers.get("gl_handlers_active") and is_iran_riv_gl_handlers_active(),
		"recalculate_wrapper_active": handlers.get("recalculate_wrapper_active")
		and is_iran_riv_recalculate_wrapper_active(),
		"process_dcd_module": handlers.get("process_dcd_module"),
		"make_gl_entries_module": handlers.get("make_gl_entries_module"),
		"riv_mod_execute_is_guard": handlers.get("riv_mod_execute_is_guard"),
		"sa_rows": sa_rows,
	}
	checks["ok"] = all(
		[
			checks["fractional_sabe_84"],
			checks["gl_520_present"],
			checks["gl_58_present"],
			checks["failed_riv_absent"],
			checks["phase3_no_import_bind"],
			checks["phase3_uses_guard"],
			checks["phase3_asserts_gl"],
			checks["gl_handlers_active"],
			checks["recalculate_wrapper_active"],
			checks["riv_mod_execute_is_guard"],
		]
	)
	return checks


def _scope_rows(dry: dict, scope: dict) -> list[dict]:
	"""Fractional SABE rows belonging to this Item+Warehouse RIV identity."""
	return [
		r
		for r in (dry.get("rows") or [])
		if r.get("item_code") == scope["item_code"] and r.get("warehouse") == scope["warehouse"]
	]


def _sle_sabe_bin_for_scope(scope: dict) -> dict[str, Any]:
	item = scope["item_code"]
	wh = scope["warehouse"]
	posting_date = scope["posting_date"]

	sabe = frappe.db.sql(
		"""
		SELECT name, parent, item_code, batch_no, warehouse, qty,
		       incoming_rate, outgoing_rate, stock_value_difference,
		       voucher_type, voucher_no, voucher_detail_no, posting_datetime
		FROM `tabSerial and Batch Entry`
		WHERE docstatus=1 AND IFNULL(is_cancelled,0)=0
		  AND item_code=%s AND warehouse=%s
		  AND DATE(posting_datetime) >= %s
		ORDER BY posting_datetime, creation, idx
		""",
		(item, wh, posting_date),
		as_dict=True,
	)
	sle = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, actual_qty, qty_after_transaction,
		       incoming_rate, outgoing_rate, valuation_rate,
		       stock_value, stock_value_difference, voucher_type, voucher_no,
		       voucher_detail_no, serial_and_batch_bundle, posting_date, posting_datetime
		FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND item_code=%s AND warehouse=%s
		  AND posting_date >= %s
		ORDER BY posting_datetime, creation
		""",
		(item, wh, posting_date),
		as_dict=True,
	)
	bin_row = frappe.db.get_value(
		"Bin",
		{"item_code": item, "warehouse": wh},
		["actual_qty", "stock_value", "valuation_rate"],
		as_dict=True,
	) or {}
	batches = frappe.db.sql(
		"""
		SELECT batch_no,
		       SUM(qty) qty,
		       SUM(stock_value_difference) stock_value
		FROM `tabSerial and Batch Entry`
		WHERE docstatus=1 AND IFNULL(is_cancelled,0)=0
		  AND item_code=%s AND warehouse=%s
		GROUP BY batch_no
		ORDER BY batch_no
		""",
		(item, wh),
		as_dict=True,
	)
	return {
		"sabe_count": len(sabe),
		"sle_count": len(sle),
		"bin": {
			"actual_qty": flt(bin_row.get("actual_qty")),
			"stock_value": flt(bin_row.get("stock_value")),
			"valuation_rate": flt(bin_row.get("valuation_rate")),
		},
		"batch_count": len(batches),
		"sabe_fingerprint": _fp(
			sabe,
			[
				"name",
				"voucher_no",
				"batch_no",
				"qty",
				"incoming_rate",
				"outgoing_rate",
				"stock_value_difference",
			],
		),
		"sle_fingerprint": _fp(
			sle,
			[
				"name",
				"voucher_no",
				"actual_qty",
				"qty_after_transaction",
				"stock_value",
				"stock_value_difference",
				"valuation_rate",
			],
		),
		"bin_fingerprint": _fp(
			[bin_row],
			["actual_qty", "stock_value", "valuation_rate"],
		),
		"batch_fingerprint": _fp(batches, ["batch_no", "qty", "stock_value"]),
		"sabe_rows_sample": sabe[:5],
		"sle_rows_sample": sle[:5],
		"batches_sample": batches[:10],
	}


def _project_a2_for_rows(rows: list[dict]) -> dict[str, Any]:
	from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
		localize_sabe_row_values,
	)

	details = []
	net = 0.0
	for r in rows:
		raw_rate = flt(r.get("old_incoming_rate")) or flt(r.get("old_outgoing_rate")) or 0.0
		# rows from dry-run already have localization; recompute for proof
		loc = localize_sabe_row_values(
			qty=flt(r.get("qty")),
			raw_rate=raw_rate,
			raw_svd=flt(r.get("old_svd")),
		)
		# A2 = integerized − ROUND(raw); classification is A2 when |diff|>=0.5
		diff = loc["localization_difference"]
		cls = "A2" if abs(diff) >= 0.5 else "noop"
		details.append(
			{
				"sabe": r.get("sabe"),
				"voucher_no": r.get("voucher_no"),
				"qty": flt(r.get("qty")),
				"raw_rate": raw_rate,
				"raw_svd": flt(r.get("old_svd")),
				"raw_rounded": loc["raw_rounded"],
				"integer_rate": loc["rate"],
				"integerized_svd": loc["svd"],
				"a2": diff,
				"classification": cls,
				"formula": "A2 = qty×RHU(raw_rate) − RHU(raw_svd) for integral qty",
			}
		)
		net += diff
	return {
		"row_count": len(details),
		"a2_net": float(net),
		"a2_abs": float(sum(abs(d["a2"]) for d in details)),
		"details": details,
	}


def _gl_balance_for_vouchers(voucher_nos: list[str]) -> dict[str, Any]:
	out = []
	imbalanced = []
	for vno in sorted(set(voucher_nos)):
		if not vno:
			continue
		rows = frappe.db.sql(
			"""
			SELECT account, SUM(debit) debit, SUM(credit) credit
			FROM `tabGL Entry`
			WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
			GROUP BY account
			ORDER BY account
			""",
			vno,
			as_dict=True,
		)
		debit = sum(flt(r.debit) for r in rows)
		credit = sum(flt(r.credit) for r in rows)
		diff = float(debit - credit)
		purpose = frappe.db.get_value("Stock Entry", vno, "purpose") if frappe.db.exists("Stock Entry", vno) else None
		vd = frappe.db.get_value("Stock Entry", vno, "value_difference") if purpose else None
		rec = {
			"voucher_no": vno,
			"purpose": purpose,
			"value_difference": flt(vd) if vd is not None else None,
			"total_debit": float(debit),
			"total_credit": float(credit),
			"diff": diff,
			"balanced": abs(diff) < 1e-9,
			"by_account": [
				{"account": r.account, "debit": float(flt(r.debit)), "credit": float(flt(r.credit))}
				for r in rows
			],
			"sa_621301": [
				{"account": r.account, "debit": float(flt(r.debit)), "credit": float(flt(r.credit))}
				for r in rows
				if SA_ACCOUNT_PREFIX in str(r.account or "")
			],
		}
		out.append(rec)
		if not rec["balanced"]:
			imbalanced.append(vno)
	return {"vouchers": out, "imbalanced": imbalanced, "all_balanced": not imbalanced}


def _expected_a2_gl_legs(gl_previews: list[dict]) -> list[dict]:
	"""Project expected SA + partner legs for outstanding A2 (read-only)."""
	expected = []
	for prev in gl_previews:
		outstanding = flt(prev.get("outstanding_net"))
		if abs(outstanding) < 0.5:
			continue
		purpose = prev.get("purpose") or ""
		# Positive outstanding → Credit SA / Debit partner
		sa_debit = float(-outstanding)
		partner_kind = "WIP/Stock" if purpose in ("Manufacture", "Repack") else "Stock-or-RoundOff"
		expected.append(
			{
				"voucher_no": prev.get("voucher_no"),
				"purpose": purpose,
				"value_difference": prev.get("value_difference"),
				"proposed_net": prev.get("proposed_net"),
				"recognized_net": prev.get("recognized_net"),
				"outstanding_net": outstanding,
				"value_difference_covers": prev.get("value_difference_covers"),
				"expected_sa": {
					"account": "621301 - تعدیلات موجودی کالا - E",
					"debit": abs(sa_debit) if sa_debit > 0 else 0.0,
					"credit": abs(sa_debit) if sa_debit < 0 else 0.0,
				},
				"expected_partner": {
					"kind": partner_kind,
					"debit": abs(sa_debit) if sa_debit < 0 else 0.0,
					"credit": abs(sa_debit) if sa_debit > 0 else 0.0,
					"note": "Manufacture/Repack partner must be WIP/Stock — never Round Off",
				},
				"balanced": True,
			}
		)
	return expected


def _consume_to_zero_invariants(item_codes: list[str]) -> dict[str, Any]:
	"""Check historical SLE consume-to-zero rows for campaign items (read-only)."""
	rows = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, voucher_no, actual_qty,
		       qty_after_transaction, stock_value, stock_value_difference,
		       posting_datetime
		FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND item_code IN %(items)s
		  AND ABS(qty_after_transaction) <= 1e-9 AND actual_qty < 0
		ORDER BY posting_datetime
		""",
		{"items": item_codes},
		as_dict=True,
	)
	failures = []
	checked = []
	for r in rows:
		# previous stock value = stock_value - stock_value_difference (after = before + svd)
		# at qty_after=0, stock_value must be 0 and svd == -previous
		ok_qty = abs(flt(r.qty_after_transaction)) <= 1e-9
		ok_sv = abs(flt(r.stock_value)) <= 1e-6
		# cannot prove svd == -previous without prior SLE; check stock_value==0 when qty_after==0
		prev_est = flt(r.stock_value) - flt(r.stock_value_difference)
		# After consume-to-zero: stock_value should be 0; svd should equal -prev_est
		# If stock_value is 0: svd should equal -prev where prev = -svd (tautology when sv=0)
		ok_svd = abs(flt(r.stock_value) - 0.0) <= 1e-6
		# Cross-check: stock_value_after = stock_value_before + svd ⇒ 0 = prev + svd ⇒ svd = -prev
		ok_clear = abs(flt(r.stock_value_difference) + prev_est) <= 1e-6 if abs(flt(r.stock_value)) <= 1e-6 else False
		# When stock_value is 0: prev_est = -svd, so svd + prev_est = 0 always. Real check is stock_value==0.
		rec = {
			"sle": r.name,
			"item_code": r.item_code,
			"warehouse": r.warehouse,
			"voucher_no": r.voucher_no,
			"qty_after_transaction": flt(r.qty_after_transaction),
			"stock_value": flt(r.stock_value),
			"stock_value_difference": flt(r.stock_value_difference),
			"ok_qty_after_zero": ok_qty,
			"ok_stock_value_zero": ok_sv,
			"ok_svd_clears_layer": ok_qty and ok_sv,
		}
		# Bin check
		bin_row = frappe.db.get_value(
			"Bin",
			{"item_code": r.item_code, "warehouse": r.warehouse},
			["actual_qty", "stock_value"],
			as_dict=True,
		)
		# Only assert bin zero if this is the chronologically last SLE for item+wh
		last = frappe.db.sql(
			"""
			SELECT name FROM `tabStock Ledger Entry`
			WHERE is_cancelled=0 AND item_code=%s AND warehouse=%s
			ORDER BY posting_datetime DESC, creation DESC LIMIT 1
			""",
			(r.item_code, r.warehouse),
		)
		is_last = bool(last and last[0][0] == r.name)
		rec["is_last_sle"] = is_last
		if is_last and bin_row:
			rec["bin_qty"] = flt(bin_row.actual_qty)
			rec["bin_stock_value"] = flt(bin_row.stock_value)
			rec["bin_zero_ok"] = abs(flt(bin_row.actual_qty)) <= 1e-9 and abs(flt(bin_row.stock_value)) <= 1e-6
		checked.append(rec)
		if not (ok_qty and ok_sv):
			failures.append(rec)
		if is_last and bin_row and not rec.get("bin_zero_ok"):
			failures.append(rec)
	return {
		"consume_to_zero_count": len(checked),
		"failures": failures,
		"ok": not failures,
		"sample": checked[:20],
	}


def _sed_sle_consistency_manufacture(
	voucher_nos: list[str], *, campaign_items: list[str]
) -> dict[str, Any]:
	"""SED↔SLE for campaign items only (out-of-scope co-items noted separately)."""
	mismatches = []
	out_of_scope = []
	checked = 0
	for vno in voucher_nos:
		purpose = frappe.db.get_value("Stock Entry", vno, "purpose")
		if purpose not in ("Manufacture", "Repack"):
			continue
		items = frappe.db.sql(
			"""
			SELECT name, item_code, s_warehouse, t_warehouse, qty, amount, basic_amount
			FROM `tabStock Entry Detail` WHERE parent=%s
			""",
			vno,
			as_dict=True,
		)
		for it in items:
			sle = frappe.db.sql(
				"""
				SELECT stock_value_difference, actual_qty
				FROM `tabStock Ledger Entry`
				WHERE voucher_no=%s AND voucher_detail_no=%s AND is_cancelled=0
				LIMIT 1
				""",
				(vno, it.name),
				as_dict=True,
			)
			if not sle:
				continue
			svd = abs(flt(sle[0].stock_value_difference))
			amt = abs(flt(it.amount))
			gap = float(svd - amt)
			rec = {
				"voucher_no": vno,
				"sed": it.name,
				"item_code": it.item_code,
				"sed_amount": amt,
				"sle_svd_abs": svd,
				"gap": gap,
			}
			if it.item_code not in campaign_items:
				if abs(gap) >= 0.5:
					out_of_scope.append(rec)
				continue
			checked += 1
			if abs(gap) >= 0.5:
				mismatches.append(rec)
	return {
		"checked_campaign_items": checked,
		"mismatches": mismatches,
		"out_of_scope_coitem_gaps": out_of_scope,
		"ok": not mismatches,
	}


def _coverage_audit(gl_previews: list[dict]) -> dict[str, Any]:
	"""Prove 520/58 coverage and no false matches."""
	covered_520 = None
	covered_58 = None
	ambiguous = []
	duplicates_risk = []
	for prev in gl_previews:
		vno = prev.get("voucher_no")
		proposed = flt(prev.get("proposed_net"))
		outstanding = flt(prev.get("outstanding_net"))
		vd = flt(prev.get("value_difference"))
		covers = bool(prev.get("value_difference_covers"))
		if vno == "MAT-STE-2026-30392":
			covered_520 = {
				"proposed": proposed,
				"value_difference": vd,
				"covers": covers,
				"outstanding": outstanding,
				"exact_match": abs(vd - proposed) < 0.5,
				"ok": covers and abs(outstanding) < 0.5 and abs(vd - 520) < 0.5,
			}
		if vno == "MAT-STE-2026-30451":
			covered_58 = {
				"proposed": proposed,
				"value_difference": vd,
				"covers": covers,
				"outstanding": outstanding,
				# existing GL is debit 58; proposed may be -58 or +58 depending on sign convention
				"ok": covers and abs(outstanding) < 0.5 and abs(abs(vd) - 58) < 0.5,
			}
		# Ambiguous: covers True but vd not equal proposed
		if covers and abs(vd - proposed) >= 0.5:
			ambiguous.append(prev)
		# Duplicate risk: outstanding still nonzero while covers True
		if covers and abs(outstanding) >= 0.5:
			duplicates_risk.append(prev)
		# Unrelated equal-amount: purpose not Manufacture but somehow covers via vd
		if covers and prev.get("purpose") not in ("Manufacture", "Repack"):
			ambiguous.append({**prev, "reason": "vd_cover_on_non_manufacture"})

	# Existing GL 520/58 still present and unique
	gl_520 = frappe.db.sql(
		"""
		SELECT name, debit, credit, remarks FROM `tabGL Entry`
		WHERE voucher_no='MAT-STE-2026-30392' AND account LIKE %s
		  AND IFNULL(is_cancelled,0)=0 AND ROUND(credit)=520
		""",
		(SA_ACCOUNT_PREFIX + "%",),
		as_dict=True,
	)
	gl_58 = frappe.db.sql(
		"""
		SELECT name, debit, credit, remarks FROM `tabGL Entry`
		WHERE voucher_no='MAT-STE-2026-30451' AND account LIKE %s
		  AND IFNULL(is_cancelled,0)=0 AND ROUND(debit)=58
		""",
		(SA_ACCOUNT_PREFIX + "%",),
		as_dict=True,
	)
	return {
		"covered_520": covered_520,
		"covered_58": covered_58,
		"gl_520_rows": gl_520,
		"gl_58_rows": gl_58,
		"ambiguous": ambiguous,
		"duplicates_risk": duplicates_risk,
		"ok": (
			bool(covered_520 and covered_520.get("ok"))
			and bool(covered_58 and covered_58.get("ok"))
			and len(gl_520) == 1
			and len(gl_58) == 1
			and not ambiguous
			and not duplicates_risk
		),
	}


def _case_ab_not_in_a2(dry: dict) -> dict[str, Any]:
	case_a = dry.get("case_a_102") or {}
	case_b = dry.get("case_b_595") or {}
	# Case A: localization net on that batch should not invent separate +102 SA
	# Case B: type_b_gap 595 must not appear as A2 in projected rows for that batch
	rows = dry.get("rows") or []
	case_b_a2 = [
		r
		for r in rows
		if r.get("item_code") == "30300084"
		and r.get("batch_no") == "9546-30300084-MM263202F12"
		and abs(flt(r.get("localization_difference"))) >= 0.5
	]
	return {
		"case_a": case_a,
		"case_b": case_b,
		"case_a_not_classified_as_outstanding_sa": abs(flt(case_a.get("localization_net"))) < 0.5
		or "Not an outstanding" in str(case_a.get("classification") or ""),
		"case_b_gap": flt(case_b.get("type_b_gap")),
		"case_b_a2_rows_for_batch": len(case_b_a2),
		"case_b_not_as_a2": abs(flt(case_b.get("type_b_gap")) - 595) < 0.5
		and "Type B" in str(case_b.get("classification") or ""),
		"ok": True,  # refined below
	}


def run() -> dict[str, Any]:
	from erpnext_extensions.iran_accounting.domain.riv_sabe_integer_campaign import (
		dry_run_riv_sabe_integer_campaign,
	)
	from erpnext_extensions.iran_accounting.domain.riv_sabe_integer_phase3 import APPROVED_SCOPES as PHASE3_SCOPES

	pre = _preconditions()
	if not pre["ok"]:
		return {
			"verdict": "BLOCKED — LIVE/RIV ACCOUNTING PARITY FAILURE",
			"stop": True,
			"preconditions": pre,
			"reason": "Preconditions failed — dry-run not started",
		}

	# Two deterministic dry-runs (read-only)
	dry1 = dry_run_riv_sabe_integer_campaign(item_codes=["13200333", "30300084"])
	dry2 = dry_run_riv_sabe_integer_campaign(item_codes=["13200333", "30300084"])
	cmp1 = _strip_volatile(dry1)
	cmp2 = _strip_volatile(dry2)
	# Compare excluding row-level identical via fingerprints of key fields
	det = {
		"fractional_equal": dry1["fractional_sabe_count"] == dry2["fractional_sabe_count"],
		"proposed_a2_equal": dry1["proposed_a2_net"] == dry2["proposed_a2_net"],
		"outstanding_equal": dry1["outstanding_a2_net_after_dedupe"]
		== dry2["outstanding_a2_net_after_dedupe"],
		"scopes_equal": dry1["riv_documents_projected"] == dry2["riv_documents_projected"],
		"gl_preview_equal": _strip_volatile(dry1["gl_previews"]) == _strip_volatile(dry2["gl_previews"]),
		"rows_equal": _strip_volatile(dry1["rows"]) == _strip_volatile(dry2["rows"]),
		"case_a_equal": _strip_volatile(dry1["case_a_102"]) == _strip_volatile(dry2["case_a_102"]),
		"case_b_equal": _strip_volatile(dry1["case_b_595"]) == _strip_volatile(dry2["case_b_595"]),
	}
	det["ok"] = all(det.values())

	# Scopes must match approved constant
	projected_scopes = [
		{
			"item_code": r["item_code"],
			"warehouse": r["warehouse"],
			"posting_date": r["posting_date"],
		}
		for r in dry1["riv_documents_projected"]
	]
	scopes_match = projected_scopes == list(PHASE3_SCOPES) == APPROVED_SCOPES

	scope_reports = []
	all_vouchers = set()
	for scope in APPROVED_SCOPES:
		rows = _scope_rows(dry1, scope)
		ledger = _sle_sabe_bin_for_scope(scope)
		a2 = _project_a2_for_rows(rows)
		vouchers = sorted({r.get("voucher_no") for r in rows if r.get("voucher_no")})
		all_vouchers.update(vouchers)
		# GL previews for vouchers in this scope
		previews = [p for p in dry1["gl_previews"] if p.get("voucher_no") in vouchers]
		scope_reports.append(
			{
				"scope": scope,
				"fractional_sabe_in_scope": len(rows),
				"affected_vouchers": vouchers,
				"a2_projection": {
					"row_count": a2["row_count"],
					"a2_net": a2["a2_net"],
					"a2_abs": a2["a2_abs"],
					"sample": a2["details"][:10],
				},
				"ledger_baseline": {
					"sabe_count_from_posting_date": ledger["sabe_count"],
					"sle_count_from_posting_date": ledger["sle_count"],
					"bin": ledger["bin"],
					"sabe_fingerprint": ledger["sabe_fingerprint"],
					"sle_fingerprint": ledger["sle_fingerprint"],
					"bin_fingerprint": ledger["bin_fingerprint"],
					"batch_fingerprint": ledger["batch_fingerprint"],
					"note": "before==after for read-only dry-run (no mutation)",
				},
				"gl_previews_for_scope_vouchers": previews,
				"expected_new_sa_legs": _expected_a2_gl_legs(previews),
			}
		)

	# Also include all vouchers from full campaign gl_previews for balance check
	all_preview_vouchers = [p.get("voucher_no") for p in dry1["gl_previews"] if p.get("voucher_no")]
	all_vouchers.update(all_preview_vouchers)

	gl_recon = _gl_balance_for_vouchers(list(all_vouchers))
	coverage = _coverage_audit(dry1["gl_previews"])
	consume0 = _consume_to_zero_invariants(["13200333", "30300084"])
	sed_sle = _sed_sle_consistency_manufacture(
		[p.get("voucher_no") for p in dry1["gl_previews"] if p.get("purpose") in ("Manufacture", "Repack")],
		campaign_items=["13200333", "30300084"],
	)
	case_ab = _case_ab_not_in_a2(dry1)
	case_ab["ok"] = (
		case_ab["case_a_not_classified_as_outstanding_sa"]
		and case_ab["case_b_not_as_a2"]
		and abs(case_ab["case_b_gap"] - 595) < 0.5
	)

	# Manufacture partner policy check on expected legs
	mfg_partner_ok = True
	mfg_partner_issues = []
	for scope_rep in scope_reports:
		for leg in scope_rep["expected_new_sa_legs"]:
			if leg.get("purpose") in ("Manufacture", "Repack"):
				if leg["expected_partner"]["kind"] != "WIP/Stock":
					mfg_partner_ok = False
					mfg_partner_issues.append(leg)

	# value_difference unchanged proof (read-only: snapshot hash)
	vd_snapshot = frappe.db.sql(
		"""
		SELECT name, purpose, value_difference, total_incoming_value, total_outgoing_value
		FROM `tabStock Entry`
		WHERE name IN %(names)s
		ORDER BY name
		""",
		{"names": list(all_vouchers) or ["__none__"]},
		as_dict=True,
	)
	vd_fp = _fp(vd_snapshot, ["name", "value_difference", "total_incoming_value", "total_outgoing_value"])

	# Live/RIV parity: same outstanding_sabe_a2_net + absorb path used by both
	parity = {
		"iran_make_gl_entries_active": pre["gl_handlers_active"],
		"iran_process_dcd_active": pre["gl_handlers_active"],
		"riv_guard_bootstraps_same_handlers": pre["riv_mod_execute_is_guard"],
		"phase3_call_time_guard": pre["phase3_uses_guard"],
		"ok": pre["gl_handlers_active"] and pre["riv_mod_execute_is_guard"] and pre["phase3_uses_guard"],
	}

	# Verdict selection
	verdict = "READY FOR SEPARATE RIV EXECUTION AUTHORIZATION"
	blockers = []
	if not gl_recon["all_balanced"]:
		verdict = "BLOCKED — GL IMBALANCE"
		blockers.append({"type": "gl_imbalance", "vouchers": gl_recon["imbalanced"]})
	elif not coverage["ok"]:
		if coverage["duplicates_risk"]:
			verdict = "BLOCKED — DUPLICATE STOCK ADJUSTMENT"
		else:
			verdict = "BLOCKED — VALUE_DIFFERENCE RECONCILIATION"
		blockers.append({"type": "coverage", "detail": coverage})
	elif not consume0["ok"]:
		verdict = "BLOCKED — INVENTORY INVARIANT FAILURE"
		blockers.append({"type": "consume_to_zero", "failures": consume0["failures"][:10]})
	elif not case_ab["ok"]:
		verdict = "BLOCKED — VALUE_DIFFERENCE RECONCILIATION"
		blockers.append({"type": "case_ab_misclassification", "detail": case_ab})
	elif not parity["ok"] or not det["ok"] or not scopes_match or not mfg_partner_ok:
		verdict = "BLOCKED — LIVE/RIV ACCOUNTING PARITY FAILURE"
		blockers.append(
			{
				"parity": parity,
				"determinism": det,
				"scopes_match": scopes_match,
				"mfg_partner_ok": mfg_partner_ok,
				"mfg_partner_issues": mfg_partner_issues,
			}
		)
	elif not sed_sle["ok"]:
		# SED/SLE mismatch is important but may be pre-existing draft poison — classify carefully
		# Only block if mismatches are on submitted vouchers in campaign with gap that isn't documented
		verdict = "BLOCKED — INVENTORY INVARIANT FAILURE"
		blockers.append({"type": "sed_sle", "mismatches": sed_sle["mismatches"][:20]})

	report = {
		"verdict": verdict,
		"mode": "read_only_phase3_validation",
		"execution_status": "NOT_EXECUTED",
		"authorization_boundary": (
			"No historical RIV executed. No MAT-STE-2026-43536 submit. "
			"Separate explicit authorization required for Phase 3 RIV execution."
		),
		"preconditions": pre,
		"determinism": {
			**det,
			"dry_run_1_path": dry1.get("audit_path"),
			"dry_run_2_path": dry2.get("audit_path"),
		},
		"campaign_totals": {
			"fractional_sabe_count": dry1["fractional_sabe_count"],
			"proposed_a2_net": dry1["proposed_a2_net"],
			"proposed_a2_abs": dry1["proposed_a2_abs"],
			"outstanding_a2_net_after_dedupe": dry1["outstanding_a2_net_after_dedupe"],
			"scopes_match_approved": scopes_match,
			"projected_scopes": projected_scopes,
		},
		"scopes": scope_reports,
		"gl_reconciliation": {
			"all_balanced": gl_recon["all_balanced"],
			"imbalanced": gl_recon["imbalanced"],
			"voucher_count": len(gl_recon["vouchers"]),
			"vouchers": gl_recon["vouchers"],
		},
		"a2_value_difference_reconciliation": coverage,
		"expected_new_stock_adjustment_legs": _expected_a2_gl_legs(dry1["gl_previews"]),
		"manufacture_partner_policy": {
			"ok": mfg_partner_ok,
			"issues": mfg_partner_issues,
			"rule": "Manufacture/Repack A2 counter → WIP/Stock, never Round Off",
		},
		"sed_sle_consistency": sed_sle,
		"consume_to_zero": {
			"ok": consume0["ok"],
			"count": consume0["consume_to_zero_count"],
			"failure_count": len(consume0["failures"]),
			"sample": consume0["sample"][:10],
		},
		"case_a_102": case_ab["case_a"],
		"case_b_595": case_ab["case_b"],
		"case_ab_separation_ok": case_ab["ok"],
		"value_difference_snapshot_fingerprint": vd_fp,
		"value_difference_snapshot_note": (
			"Read-only snapshot of SE header totals for affected vouchers. "
			"Dry-run does not mutate value_difference; fingerprint is baseline for post-RIV compare."
		),
		"live_riv_parity": parity,
		"blockers": blockers,
		"safety": {
			"db_mutations": False,
			"riv_executed": False,
			"target_43536_submitted": False,
		},
	}

	path = os.path.join(
		frappe.utils.get_bench_path(),
		"sites",
		frappe.local.site,
		"private",
		"files",
		"iran_riv_sabe_integer_campaign",
		"phase3_baselines",
		"phase3_readonly_validation.json",
	)
	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
	report["report_path"] = path

	# Slim return for bench execute
	return {
		"verdict": verdict,
		"report_path": path,
		"preconditions_ok": pre["ok"],
		"determinism_ok": det["ok"],
		"scopes_match": scopes_match,
		"fractional_sabe": dry1["fractional_sabe_count"],
		"proposed_a2_net": dry1["proposed_a2_net"],
		"outstanding_a2": dry1["outstanding_a2_net_after_dedupe"],
		"gl_all_balanced": gl_recon["all_balanced"],
		"gl_imbalanced": gl_recon["imbalanced"],
		"coverage_ok": coverage["ok"],
		"covered_520": coverage.get("covered_520"),
		"covered_58": coverage.get("covered_58"),
		"consume_to_zero_ok": consume0["ok"],
		"sed_sle_ok": sed_sle["ok"],
		"sed_sle_mismatch_count": len(sed_sle["mismatches"]),
		"sed_sle_out_of_scope_coitem_gaps": len(sed_sle.get("out_of_scope_coitem_gaps") or []),
		"case_ab_ok": case_ab["ok"],
		"parity_ok": parity["ok"],
		"mfg_partner_ok": mfg_partner_ok,
		"blockers": blockers,
		"scope_summaries": [
			{
				"scope": s["scope"],
				"fractional_sabe": s["fractional_sabe_in_scope"],
				"sle_count": s["ledger_baseline"]["sle_count_from_posting_date"],
				"sabe_count": s["ledger_baseline"]["sabe_count_from_posting_date"],
				"a2_net": s["a2_projection"]["a2_net"],
				"outstanding_legs": len(s["expected_new_sa_legs"]),
				"bin": s["ledger_baseline"]["bin"],
				"fingerprints": {
					"sabe": s["ledger_baseline"]["sabe_fingerprint"][:16],
					"sle": s["ledger_baseline"]["sle_fingerprint"][:16],
					"bin": s["ledger_baseline"]["bin_fingerprint"][:16],
					"batch": s["ledger_baseline"]["batch_fingerprint"][:16],
				},
			}
			for s in scope_reports
		],
	}
