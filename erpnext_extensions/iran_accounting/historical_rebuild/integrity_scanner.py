# Copyright (c) 2026, ERPNext Extensions contributors
"""Historical stock valuation integrity scanner (v5.6.1 Phase 2).

Discovers Item/Warehouse identities with proven or suspected valuation
corruption. Does not mutate ledger data — read-only discovery + JSON report.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.iran_accounting.historical_rebuild.coverage_verify import (
	has_unresolved_failed_riv,
	inspect_sle_chain,
	open_tva_for_identity,
)
from erpnext_extensions.iran_accounting.historical_rebuild.integrity_types import (
	ANOMALY_BIN,
	ANOMALY_INVALID_INCOMING,
	ANOMALY_INVALID_OUTGOING,
	ANOMALY_NEGATIVE_STOCK_VALUE,
	ANOMALY_QTY_CONTINUITY,
	ANOMALY_RIV_FAILURE,
	ANOMALY_SLE_GL,
	ANOMALY_TVA,
	ANOMALY_VALUATION_CONTINUITY,
	ANOMALY_ZERO_QTY_RESIDUAL,
	CONFIRMED_CORRUPTION,
	DEFAULT_QTY_TOLERANCE,
	DEFAULT_VALUE_TOLERANCE,
	LEGITIMATE_ACCOUNTING_BEHAVIOR,
	NEEDS_MANUAL_REVIEW,
	SEVERITY_CRITICAL,
	SEVERITY_HIGH,
	SEVERITY_MEDIUM,
	SUSPECTED_CORRUPTION,
)


def _default_report_dir() -> str:
	bench = frappe.utils.get_bench_path()
	path = os.path.join(bench, "sites", frappe.local.site, "private", "files", "hrc_integrity")
	os.makedirs(path, exist_ok=True)
	return path


def _severity_for(anomaly_type: str, amount: float | None = None) -> str:
	if anomaly_type in (
		ANOMALY_ZERO_QTY_RESIDUAL,
		ANOMALY_INVALID_OUTGOING,
		ANOMALY_NEGATIVE_STOCK_VALUE,
		ANOMALY_VALUATION_CONTINUITY,
	):
		if amount is not None and abs(flt(amount)) >= 1e9:
			return SEVERITY_CRITICAL
		return SEVERITY_HIGH
	if anomaly_type in (ANOMALY_QTY_CONTINUITY, ANOMALY_SLE_GL, ANOMALY_BIN, ANOMALY_RIV_FAILURE):
		return SEVERITY_HIGH
	if anomaly_type == ANOMALY_INVALID_INCOMING:
		return SEVERITY_MEDIUM
	return SEVERITY_MEDIUM


def _finding(
	*,
	company: str,
	item_code: str,
	warehouse: str,
	anomaly_type: str,
	classification: str,
	sle: str | None = None,
	voucher_type: str | None = None,
	voucher_no: str | None = None,
	posting_date=None,
	posting_time=None,
	qty=None,
	stock_value=None,
	stock_value_difference=None,
	expected=None,
	actual=None,
	difference=None,
	earliest_corrupting=None,
	safe_anchor=None,
	related_rivs=None,
	campaign_membership=None,
	tva_refs=None,
	recommendation: str = "",
) -> dict[str, Any]:
	amt = difference if difference is not None else stock_value
	return {
		"company": company,
		"item_code": item_code,
		"warehouse": warehouse,
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"sle": sle,
		"posting_date": str(posting_date) if posting_date else None,
		"posting_time": str(posting_time) if posting_time else None,
		"anomaly_type": anomaly_type,
		"quantity": qty,
		"stock_value": stock_value,
		"stock_value_difference": stock_value_difference,
		"expected_value": expected,
		"actual_value": actual,
		"difference_amount": difference if difference is not None else amt,
		"earliest_suspected_corrupting_transaction": earliest_corrupting,
		"earliest_safe_repost_anchor": safe_anchor,
		"related_rivs": related_rivs or [],
		"campaign_membership": campaign_membership or [],
		"tva_references": tva_refs or [],
		"financial_impact": abs(flt(amt or 0)),
		"severity": _severity_for(anomaly_type, amt),
		"classification": classification,
		"repair_recommendation": recommendation,
	}


def scan_zero_qty_residuals(company: str, tol: float = DEFAULT_VALUE_TOLERANCE) -> list[dict]:
	rows = frappe.db.sql(
		"""
		select name, item_code, warehouse, company, voucher_type, voucher_no,
		       posting_date, posting_time, qty_after_transaction, stock_value,
		       stock_value_difference
		from `tabStock Ledger Entry`
		where is_cancelled=0 and company=%s
		  and abs(qty_after_transaction) <= %s
		  and abs(stock_value) > %s
		""",
		(company, DEFAULT_QTY_TOLERANCE, tol),
		as_dict=True,
	) or []
	out = []
	for r in rows:
		out.append(
			_finding(
				company=company,
				item_code=r.item_code,
				warehouse=r.warehouse,
				anomaly_type=ANOMALY_ZERO_QTY_RESIDUAL,
				classification=CONFIRMED_CORRUPTION,
				sle=r.name,
				voucher_type=r.voucher_type,
				voucher_no=r.voucher_no,
				posting_date=r.posting_date,
				posting_time=r.posting_time,
				qty=flt(r.qty_after_transaction),
				stock_value=flt(r.stock_value),
				stock_value_difference=flt(r.stock_value_difference),
				actual=flt(r.stock_value),
				expected=0,
				difference=flt(r.stock_value),
				recommendation="Repost from earliest safe mid-chain anchor; absorb residual if transfer.",
			)
		)
	return out


def scan_i3_outgoing(company: str, tol: float = DEFAULT_VALUE_TOLERANCE) -> list[dict]:
	rows = frappe.db.sql(
		"""
		select name, item_code, warehouse, voucher_type, voucher_no,
		       posting_date, posting_time, actual_qty, stock_value, stock_value_difference
		from `tabStock Ledger Entry`
		where is_cancelled=0 and company=%s
		  and actual_qty < %s
		  and stock_value_difference > %s
		""",
		(company, -DEFAULT_QTY_TOLERANCE, tol),
		as_dict=True,
	) or []
	return [
		_finding(
			company=company,
			item_code=r.item_code,
			warehouse=r.warehouse,
			anomaly_type=ANOMALY_INVALID_OUTGOING,
			classification=CONFIRMED_CORRUPTION,
			sle=r.name,
			voucher_type=r.voucher_type,
			voucher_no=r.voucher_no,
			posting_date=r.posting_date,
			posting_time=r.posting_time,
			qty=flt(r.actual_qty),
			stock_value=flt(r.stock_value),
			stock_value_difference=flt(r.stock_value_difference),
			recommendation="I3 violation — repair valuation chain; do not bypass guard.",
		)
		for r in rows
	]


def scan_i4_incoming(company: str, tol: float = DEFAULT_VALUE_TOLERANCE) -> list[dict]:
	rows = frappe.db.sql(
		"""
		select name, item_code, warehouse, voucher_type, voucher_no,
		       posting_date, posting_time, actual_qty, stock_value, stock_value_difference
		from `tabStock Ledger Entry`
		where is_cancelled=0 and company=%s
		  and actual_qty > %s
		  and stock_value_difference < %s
		""",
		(company, DEFAULT_QTY_TOLERANCE, -tol),
		as_dict=True,
	) or []
	# Incoming negative SVD can be legitimate (rate correction). Classify suspected.
	return [
		_finding(
			company=company,
			item_code=r.item_code,
			warehouse=r.warehouse,
			anomaly_type=ANOMALY_INVALID_INCOMING,
			classification=SUSPECTED_CORRUPTION,
			sle=r.name,
			voucher_type=r.voucher_type,
			voucher_no=r.voucher_no,
			posting_date=r.posting_date,
			posting_time=r.posting_time,
			qty=flt(r.actual_qty),
			stock_value=flt(r.stock_value),
			stock_value_difference=flt(r.stock_value_difference),
			recommendation="Review whether negative incoming SVD is legitimate ERPNext behavior.",
		)
		for r in rows
	]


def scan_negative_stock_value(company: str, tol: float = DEFAULT_VALUE_TOLERANCE) -> list[dict]:
	rows = frappe.db.sql(
		"""
		select name, item_code, warehouse, voucher_type, voucher_no,
		       posting_date, posting_time, qty_after_transaction, stock_value,
		       stock_value_difference
		from `tabStock Ledger Entry`
		where is_cancelled=0 and company=%s
		  and qty_after_transaction > %s
		  and stock_value < %s
		""",
		(company, DEFAULT_QTY_TOLERANCE, -tol),
		as_dict=True,
	) or []
	return [
		_finding(
			company=company,
			item_code=r.item_code,
			warehouse=r.warehouse,
			anomaly_type=ANOMALY_NEGATIVE_STOCK_VALUE,
			classification=CONFIRMED_CORRUPTION,
			sle=r.name,
			voucher_type=r.voucher_type,
			voucher_no=r.voucher_no,
			posting_date=r.posting_date,
			posting_time=r.posting_time,
			qty=flt(r.qty_after_transaction),
			stock_value=flt(r.stock_value),
			stock_value_difference=flt(r.stock_value_difference),
			difference=flt(r.stock_value),
			recommendation="Repost identity; classify mega residuals for manual review.",
		)
		for r in rows
	]


def scan_continuity_sql(company: str, tol: float = DEFAULT_VALUE_TOLERANCE) -> list[dict]:
	"""Company-wide valuation/qty continuity breaks via window functions (bounded)."""
	rows = frappe.db.sql(
		"""
		select name, item_code, warehouse, voucher_type, voucher_no,
		       posting_date, posting_time, actual_qty, qty_after_transaction,
		       stock_value, stock_value_difference, prev_sv, prev_qty,
		       (prev_sv + stock_value_difference) as expected_sv,
		       (prev_qty + actual_qty) as expected_qty
		from (
		  select name, item_code, warehouse, voucher_type, voucher_no,
		         posting_date, posting_time, actual_qty, qty_after_transaction,
		         stock_value, stock_value_difference,
		         lag(stock_value) over (
		           partition by item_code, warehouse
		           order by posting_date, posting_time, creation
		         ) as prev_sv,
		         lag(qty_after_transaction) over (
		           partition by item_code, warehouse
		           order by posting_date, posting_time, creation
		         ) as prev_qty
		  from `tabStock Ledger Entry`
		  where is_cancelled=0 and company=%s
		) t
		where prev_sv is not null
		  and (
		    abs(stock_value - (prev_sv + stock_value_difference)) > %s
		    or abs(qty_after_transaction - (prev_qty + actual_qty)) > %s
		  )
		""",
		(company, tol, DEFAULT_QTY_TOLERANCE),
		as_dict=True,
	) or []
	out = []
	for r in rows:
		val_break = abs(flt(r.stock_value) - flt(r.expected_sv)) > tol
		out.append(
			_finding(
				company=company,
				item_code=r.item_code,
				warehouse=r.warehouse,
				anomaly_type=ANOMALY_VALUATION_CONTINUITY if val_break else ANOMALY_QTY_CONTINUITY,
				classification=CONFIRMED_CORRUPTION,
				sle=r.name,
				voucher_type=r.voucher_type,
				voucher_no=r.voucher_no,
				posting_date=r.posting_date,
				posting_time=r.posting_time,
				qty=flt(r.qty_after_transaction),
				stock_value=flt(r.stock_value),
				stock_value_difference=flt(r.stock_value_difference),
				expected=flt(r.expected_sv) if val_break else flt(r.expected_qty),
				actual=flt(r.stock_value) if val_break else flt(r.qty_after_transaction),
				difference=(
					flt(r.stock_value) - flt(r.expected_sv)
					if val_break
					else flt(r.qty_after_transaction) - flt(r.expected_qty)
				),
				recommendation="Repost identity from earliest safe anchor covering the break.",
			)
		)
	return out


def _candidate_identities(company: str) -> list[tuple[str, str]]:
	"""Identities that appear in any quick anomaly SQL — for deep continuity scan."""
	rows = frappe.db.sql(
		"""
		select distinct item_code, warehouse from `tabStock Ledger Entry`
		where is_cancelled=0 and company=%s
		  and (
		    (abs(qty_after_transaction) <= %s and abs(stock_value) > %s)
		    or (qty_after_transaction > %s and stock_value < %s)
		    or (actual_qty < %s and stock_value_difference > %s)
		    or (actual_qty > %s and stock_value_difference < %s)
		  )
		""",
		(
			company,
			DEFAULT_QTY_TOLERANCE,
			DEFAULT_VALUE_TOLERANCE,
			DEFAULT_QTY_TOLERANCE,
			-DEFAULT_VALUE_TOLERANCE,
			-DEFAULT_QTY_TOLERANCE,
			DEFAULT_VALUE_TOLERANCE,
			DEFAULT_QTY_TOLERANCE,
			-DEFAULT_VALUE_TOLERANCE,
		),
	)
	idents = [(r[0], r[1]) for r in rows]
	# Merge SQL continuity-break identities
	for f in scan_continuity_sql(company):
		key = (f["item_code"], f["warehouse"])
		if key not in idents:
			idents.append(key)
	return idents

def scan_continuity_for_identities(
	company: str, identities: list[tuple[str, str]] | None = None
) -> list[dict]:
	idents = identities or _candidate_identities(company)
	# Also include identities with known Failed RIVs
	failed = frappe.db.sql(
		"""
		select distinct item_code, warehouse from `tabRepost Item Valuation`
		where docstatus=1 and based_on='Item and Warehouse' and status='Failed'
		""",
	) or []
	seen = set(idents)
	for item_code, warehouse in failed:
		if (item_code, warehouse) not in seen:
			idents.append((item_code, warehouse))
			seen.add((item_code, warehouse))

	findings: list[dict] = []
	for item_code, warehouse in idents:
		chain = inspect_sle_chain(item_code, warehouse, company=company)
		earliest = None
		safe_anchor = None
		if chain["issues"]:
			first = chain["issues"][0]
			earliest = first.get("voucher")
			# Mid-chain: first clean stretch after issue — leave None for executor
		for issue in chain["issues"]:
			findings.append(
				_finding(
					company=company,
					item_code=item_code,
					warehouse=warehouse,
					anomaly_type=issue["type"],
					classification=issue.get("classification") or CONFIRMED_CORRUPTION,
					sle=issue.get("sle"),
					voucher_type=(issue.get("voucher") or "/").split("/")[0] or None,
					voucher_no=(issue.get("voucher") or "/").split("/")[-1] or None,
					qty=issue.get("qty") or issue.get("actual_qty"),
					stock_value=issue.get("stock_value"),
					stock_value_difference=issue.get("stock_value_difference"),
					expected=issue.get("expected"),
					actual=issue.get("actual"),
					difference=issue.get("difference"),
					earliest_corrupting=earliest,
					safe_anchor=safe_anchor,
					recommendation="Dependency-aware RIV from justified mid-chain or earliest safe anchor.",
				)
			)
		failed_riv = has_unresolved_failed_riv(item_code, warehouse)
		if failed_riv:
			findings.append(
				_finding(
					company=company,
					item_code=item_code,
					warehouse=warehouse,
					anomaly_type=ANOMALY_RIV_FAILURE,
					classification=CONFIRMED_CORRUPTION,
					related_rivs=[failed_riv],
					recommendation="Repair or clean Failed RIV before new auto-seed.",
				)
			)
		tva = open_tva_for_identity(item_code, warehouse)
		if tva:
			findings.append(
				_finding(
					company=company,
					item_code=item_code,
					warehouse=warehouse,
					anomaly_type=ANOMALY_TVA,
					classification=NEEDS_MANUAL_REVIEW,
					tva_refs=[t.name for t in tva],
					recommendation="Reconcile or reverse obsolete TVA after valuation repair.",
				)
			)
	return findings


def scan_sle_gl_sample(company: str, limit: int = 500) -> list[dict]:
	"""Flag Stock Entries whose SLE SVD sum and inventory GL net diverge materially."""
	rows = frappe.db.sql(
		"""
		select sle.voucher_no,
		       sum(sle.stock_value_difference) sle_svd,
		       (
		         select coalesce(sum(gl.debit - gl.credit), 0)
		         from `tabGL Entry` gl
		         inner join `tabAccount` a on a.name=gl.account
		         where gl.is_cancelled=0
		           and gl.voucher_type='Stock Entry'
		           and gl.voucher_no=sle.voucher_no
		           and a.account_type='Stock'
		           and gl.company=%s
		       ) gl_stock_net
		from `tabStock Ledger Entry` sle
		where sle.is_cancelled=0 and sle.company=%s and sle.voucher_type='Stock Entry'
		group by sle.voucher_no
		having abs(sum(sle.stock_value_difference) - (
		         select coalesce(sum(gl.debit - gl.credit), 0)
		         from `tabGL Entry` gl
		         inner join `tabAccount` a on a.name=gl.account
		         where gl.is_cancelled=0
		           and gl.voucher_type='Stock Entry'
		           and gl.voucher_no=sle.voucher_no
		           and a.account_type='Stock'
		           and gl.company=%s
		       )) > %s
		limit %s
		""",
		(company, company, company, DEFAULT_VALUE_TOLERANCE, cint(limit) or 500),
		as_dict=True,
	) or []
	out = []
	for r in rows:
		diff = flt(r.sle_svd) - flt(r.gl_stock_net)
		out.append(
			_finding(
				company=company,
				item_code="",
				warehouse="",
				anomaly_type=ANOMALY_SLE_GL,
				classification=SUSPECTED_CORRUPTION,
				voucher_type="Stock Entry",
				voucher_no=r.voucher_no,
				expected=flt(r.sle_svd),
				actual=flt(r.gl_stock_net),
				difference=diff,
				recommendation="Reconcile inventory GL vs SLE SVD at voucher level (not net-zero alone).",
			)
		)
	return out


def _attach_campaign_membership(findings: list[dict], campaign: str | None) -> None:
	if not campaign or not findings:
		return
	vouchers = {(f.get("voucher_type"), f.get("voucher_no")) for f in findings if f.get("voucher_no")}
	if not vouchers:
		return
	# Batch lookup
	membership: dict[tuple, list] = defaultdict(list)
	for vt, vn in vouchers:
		if not vt or not vn:
			continue
		rows = frappe.db.sql(
			"""
			select name, item_status from `tabHistorical Rebuild Campaign Item`
			where campaign=%s and voucher_type=%s and voucher_no=%s
			""",
			(campaign, vt, vn),
			as_dict=True,
		)
		for r in rows or []:
			membership[(vt, vn)].append(f"{r.name}:{r.item_status}")
	for f in findings:
		key = (f.get("voucher_type"), f.get("voucher_no"))
		f["campaign_membership"] = membership.get(key, [])


def _related_rivs_for_findings(findings: list[dict]) -> None:
	for f in findings:
		if not f.get("item_code") or not f.get("warehouse"):
			continue
		if f.get("related_rivs"):
			continue
		rows = frappe.db.sql(
			"""
			select name, status from `tabRepost Item Valuation`
			where docstatus=1 and based_on='Item and Warehouse'
			  and item_code=%s and warehouse=%s
			order by creation desc
			limit 5
			""",
			(f["item_code"], f["warehouse"]),
			as_dict=True,
		)
		f["related_rivs"] = [f"{r.name}:{r.status}" for r in (rows or [])]


def dirty_identity_set(findings: list[dict]) -> set[tuple[str, str]]:
	dirty = set()
	for f in findings:
		if f.get("classification") in (
			CONFIRMED_CORRUPTION,
			SUSPECTED_CORRUPTION,
			NEEDS_MANUAL_REVIEW,
		):
			ic, wh = f.get("item_code"), f.get("warehouse")
			if ic and wh:
				dirty.add((ic, wh))
	return dirty


def scan_open_tva_identities(company: str | None = None) -> list[dict]:
	"""All open TVA rows as findings (identity-level)."""
	if not frappe.db.exists("DocType", "Temporary Valuation Adjustment"):
		return []
	cols = {c.fieldname for c in frappe.get_meta("Temporary Valuation Adjustment").fields}
	if "item_code" not in cols or "warehouse" not in cols:
		return []
	status_field = "repair_status" if "repair_status" in cols else None
	filters: dict[str, Any] = {}
	if status_field:
		filters[status_field] = (
			"in",
			["OPEN", "SUBMITTED", "REPOST_PENDING", "MANUAL_REVIEW"],
		)
	fields = ["name", "item_code", "warehouse"]
	for f in ("stock_entry", "adjustment_delta", "repair_status"):
		if f in cols:
			fields.append(f)
	rows = frappe.get_all(
		"Temporary Valuation Adjustment",
		filters=filters,
		fields=fields,
		limit_page_length=5000,
	) or []
	out = []
	for r in rows:
		if not r.item_code or not r.warehouse:
			continue
		out.append(
			_finding(
				company=company or "",
				item_code=r.item_code,
				warehouse=r.warehouse,
				anomaly_type=ANOMALY_TVA,
				classification=NEEDS_MANUAL_REVIEW,
				voucher_type="Stock Entry",
				voucher_no=getattr(r, "stock_entry", None),
				tva_refs=[r.name],
				difference=flt(getattr(r, "adjustment_delta", 0) or 0),
				recommendation="Reconcile TVA after valuation repair; do not dual-post.",
			)
		)
	return out

def run_full_scan(
	company: str,
	*,
	campaign: str | None = "HRC-00001",
	report_dir: str | None = None,
	include_sle_gl: bool = True,
) -> dict[str, Any]:
	"""Run all scanner checks and write structured JSON report."""
	t0 = time.time()
	findings: list[dict] = []
	buckets: dict[str, list] = {
		"zero_qty_residual": scan_zero_qty_residuals(company),
		"invalid_outgoing": scan_i3_outgoing(company),
		"invalid_incoming": scan_i4_incoming(company),
		"negative_stock_value": scan_negative_stock_value(company),
		"continuity_sql": scan_continuity_sql(company),
	}
	for key, rows in buckets.items():
		findings.extend(rows)

	# Deep continuity / TVA / Failed RIV on candidate + failed identities
	continuity = scan_continuity_for_identities(company)
	buckets["continuity_and_related"] = continuity
	findings.extend(continuity)
	if include_sle_gl:
		sle_gl = scan_sle_gl_sample(company)
		buckets["sle_gl"] = sle_gl
		findings.extend(sle_gl)

	tva_findings = scan_open_tva_identities(company)
	buckets["open_tva"] = tva_findings
	findings.extend(tva_findings)

	# v5.6.25 purchase valuation / LCV / GRNI / transfer / RIV detection
	from erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_detection import (
		dry_run_purchase_valuation_scan,
	)

	purchase_scan = dry_run_purchase_valuation_scan(company, campaign=campaign)
	buckets["purchase_valuation_v5625"] = purchase_scan.get("findings") or []
	findings.extend(buckets["purchase_valuation_v5625"])

	_attach_campaign_membership(findings, campaign)
	_related_rivs_for_findings(findings)

	# Deduplicate by (anomaly, sle, item, wh)
	seen = set()
	unique: list[dict] = []
	for f in findings:
		key = (
			f.get("anomaly_type"),
			f.get("sle"),
			f.get("item_code"),
			f.get("warehouse"),
			f.get("voucher_no"),
		)
		if key in seen:
			continue
		seen.add(key)
		unique.append(f)

	by_class: dict[str, int] = defaultdict(int)
	by_type: dict[str, int] = defaultdict(int)
	for f in unique:
		by_class[f.get("classification") or "UNKNOWN"] += 1
		by_type[f.get("anomaly_type") or "UNKNOWN"] += 1

	dirty = dirty_identity_set(unique)
	elapsed = time.time() - t0

	report = {
		"generated_at": str(now_datetime()),
		"company": company,
		"campaign": campaign,
		"scanner_version": "5.6.25",
		"runtime_seconds": round(elapsed, 3),
		"finding_count": len(unique),
		"dirty_identity_count": len(dirty),
		"dirty_identities": [{"item_code": i, "warehouse": w} for i, w in sorted(dirty)],
		"by_classification": dict(by_class),
		"by_anomaly_type": dict(by_type),
		"bucket_counts": {k: len(v) for k, v in buckets.items()},
		"findings": unique,
		"notes": [
			"LEGITIMATE_ACCOUNTING_BEHAVIOR reserved for verified-clean false positives.",
			"Incoming negative SVD classified SUSPECTED until purpose-level review.",
			"Completed RIV is never treated as clean without continuity evidence.",
		],
	}

	out_dir = report_dir or _default_report_dir()
	path = os.path.join(out_dir, f"integrity_scan_{now_datetime().strftime('%Y%m%d_%H%M%S')}.json")
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
	report["report_path"] = path

	# Compact summary without full findings for return callers
	summary = {k: v for k, v in report.items() if k != "findings"}
	summary["findings_sample"] = unique[:30]
	summary["report_path"] = path
	frappe.cache().set_value("hrc_integrity_last_report", path, expires_in_sec=60 * 60 * 24 * 7)
	frappe.cache().set_value(
		"hrc_integrity_dirty_identities",
		list(dirty),
		expires_in_sec=60 * 60 * 24 * 7,
	)
	return summary
