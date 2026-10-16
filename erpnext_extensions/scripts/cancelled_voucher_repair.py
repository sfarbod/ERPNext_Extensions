#!/usr/bin/env python3
"""Cancelled voucher SLE/GL orphan audit + field-limited repair.

Standalone, copy into Production container and load via bench console:

    exec(open("/tmp/cancelled_voucher_repair.py", encoding="utf-8").read())
    run(mode="audit")
    run(mode="dry_run", vouchers=["MAT-STE-2026-36965"])
    run(mode="repair", vouchers=["MAT-STE-2026-36965"], allow_gl=False)

Modes:
  audit          — site-wide orphan discovery (read-only)
  dry_run        — validate candidates; no writes (blocks on RIV conflicts)
  repair         — field-limited SLE (and optional GL); blocks on RIV conflicts
  riv_check      — conflicting RIV for candidate item/warehouse chains
  verify         — post-repair verification for explicit vouchers
  direct_dry_run — allowlisted MAT-STE-2026-36965 SLE flag dry-run (no RIV/GL)
  direct_repair  — allowlisted SLE flag repair without RIV queue dependency

direct_repair / direct_dry_run:
  - Only MAT-SLE-2026-260822 and MAT-SLE-2026-260823
  - Never creates/modifies RIV or GL
  - Does NOT empty the RIV queue requirement; reports conflicts explicitly
  - Requires accept_concurrency_risk=True when conflicts exist
  - Does NOT repair Bin / Batch / downstream valuation

Default: audit. Mutates only for mode in (repair, direct_repair) when not blocked.
Does not use Document.db_update on SLE/GL.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import frappe
from frappe.utils import cint, flt, now

# Voucher types that appear in SLE / GL on this ERPNext version
SLE_PARENT_MAP = {
	"Stock Entry": "Stock Entry",
	"Purchase Receipt": "Purchase Receipt",
	"Delivery Note": "Delivery Note",
	"Stock Reconciliation": "Stock Reconciliation",
	"Sales Invoice": "Sales Invoice",
	"Purchase Invoice": "Purchase Invoice",
	"Subcontracting Receipt": "Subcontracting Receipt",
}

GL_PARENT_MAP = {
	**SLE_PARENT_MAP,
	"Journal Entry": "Journal Entry",
	"Payment Entry": "Payment Entry",
}

KNOWN_INCIDENT = {
	"voucher": "MAT-STE-2026-36965",
	"sles": ("MAT-SLE-2026-260822", "MAT-SLE-2026-260823"),
	"expected": {
		"MAT-SLE-2026-260822": {"item_code": "30200016", "actual_qty": -1992.0},
		"MAT-SLE-2026-260823": {"item_code": "30300014", "actual_qty": 1991.0},
	},
}

# Explicit allowlist for mode=direct_repair / direct_dry_run.
# Only these voucher+SLE rows may be mutated. No RIV, no GL, no Bin/Batch.
DIRECT_REPAIR_ALLOWLIST = {
	"voucher_type": "Stock Entry",
	"voucher_no": "MAT-STE-2026-36965",
	"sles": {
		"MAT-SLE-2026-260822": {
			"item_code": "30200016",
			"actual_qty": -1992.0,
			"expected_bundle": "ab853ae8f8cb263d2ed3",
		},
		"MAT-SLE-2026-260823": {
			"item_code": "30300014",
			"actual_qty": 1991.0,
			"expected_bundle": "3a8927501d992a57854a",
		},
	},
	"scope_note": (
		"This operation ONLY sets SLE.is_cancelled=1 and clears "
		"SLE.serial_and_batch_bundle. It does NOT repair Bin.actual_qty, "
		"Bin.stock_value, Batch.batch_qty, downstream SLE valuation fields, "
		"or General Ledger. Targeted RIV / batch recalc remain separate steps."
	),
}


def _j(obj: Any) -> str:
	return json.dumps(obj, indent=2, default=str, ensure_ascii=False)


def _parent_docstatus(voucher_type: str, voucher_no: str) -> int | None:
	doctype = SLE_PARENT_MAP.get(voucher_type) or GL_PARENT_MAP.get(voucher_type)
	if not doctype or not frappe.db.exists("DocType", doctype):
		return None
	if not frappe.db.exists(doctype, voucher_no):
		return None
	return cint(frappe.db.get_value(doctype, voucher_no, "docstatus"))


def _discover_sle_voucher_types() -> list[str]:
	rows = frappe.db.sql(
		"select distinct voucher_type from `tabStock Ledger Entry` where voucher_type is not null",
		pluck=True,
	)
	return sorted(rows or [])


def _discover_gl_voucher_types() -> list[str]:
	rows = frappe.db.sql(
		"select distinct voucher_type from `tabGL Entry` where voucher_type is not null",
		pluck=True,
	)
	return sorted(rows or [])


def audit_orphan_sles(voucher_filter: list[str] | None = None) -> list[dict]:
	"""Parent docstatus=2 AND SLE.is_cancelled=0."""
	out: list[dict] = []
	for vt, doctype in SLE_PARENT_MAP.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		params: list[Any] = []
		extra = ""
		if voucher_filter:
			extra = " and sle.voucher_no in %s"
			params.append(tuple(voucher_filter))
		rows = frappe.db.sql(
			f"""
			select sle.name, sle.voucher_type, sle.voucher_no, sle.voucher_detail_no,
			       sle.item_code, sle.warehouse, sle.actual_qty, sle.qty_after_transaction,
			       sle.stock_value_difference, sle.valuation_rate, sle.is_cancelled,
			       sle.serial_and_batch_bundle, sle.posting_datetime, sle.creation, sle.modified,
			       p.docstatus as parent_docstatus, p.modified as parent_modified
			from `tabStock Ledger Entry` sle
			inner join `tab{doctype}` p on p.name = sle.voucher_no and p.docstatus = 2
			where sle.voucher_type = %s and ifnull(sle.is_cancelled, 0) = 0
			{extra}
			order by sle.voucher_no, sle.creation
			""",
			tuple([vt] + params),
			as_dict=True,
		)
		out.extend(rows or [])
	return out


def audit_orphan_gl(voucher_filter: list[str] | None = None) -> list[dict]:
	"""Parent docstatus=2 AND GL.is_cancelled=0.

	NOTE: With immutable ledger enabled, reverse GLs intentionally keep
	is_cancelled=0. Always classify before repairing.
	"""
	out: list[dict] = []
	immutable = cint(
		frappe.db.get_single_value("Accounts Settings", "enable_immutable_ledger") or 0
	)
	for vt, doctype in GL_PARENT_MAP.items():
		if not frappe.db.exists("DocType", doctype):
			continue
		params: list[Any] = []
		extra = ""
		if voucher_filter:
			extra = " and gle.voucher_no in %s"
			params.append(tuple(voucher_filter))
		rows = frappe.db.sql(
			f"""
			select gle.name, gle.voucher_type, gle.voucher_no, gle.account,
			       gle.debit, gle.credit, gle.is_cancelled, gle.remarks,
			       gle.posting_date, gle.creation, gle.modified,
			       p.docstatus as parent_docstatus, p.modified as parent_modified
			from `tabGL Entry` gle
			inner join `tab{doctype}` p on p.name = gle.voucher_no and p.docstatus = 2
			where gle.voucher_type = %s and ifnull(gle.is_cancelled, 0) = 0
			{extra}
			order by gle.voucher_no, gle.creation
			""",
			tuple([vt] + params),
			as_dict=True,
		)
		for r in rows or []:
			r["immutable_ledger"] = immutable
			out.append(r)
	return out


def _sle_peers(voucher_type: str, voucher_no: str) -> list[dict]:
	return frappe.db.sql(
		"""
		select name, voucher_detail_no, item_code, warehouse, actual_qty,
		       is_cancelled, serial_and_batch_bundle, stock_value_difference,
		       posting_datetime, creation, modified
		from `tabStock Ledger Entry`
		where voucher_type=%s and voucher_no=%s
		order by creation
		""",
		(voucher_type, voucher_no),
		as_dict=True,
	)


def _bundle_info(bundle: str | None) -> dict | None:
	if not bundle:
		return None
	if not frappe.db.exists("Serial and Batch Bundle", bundle):
		return {"name": bundle, "missing": True}
	return frappe.db.get_value(
		"Serial and Batch Bundle",
		bundle,
		["name", "docstatus", "is_cancelled", "item_code", "warehouse", "voucher_no"],
		as_dict=True,
	)


def classify_voucher(voucher_type: str, voucher_no: str) -> dict:
	"""Classify a cancelled voucher's ledger inconsistency."""
	parent_ds = _parent_docstatus(voucher_type, voucher_no)
	result: dict[str, Any] = {
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"parent_docstatus": parent_ds,
		"classification": "MANUAL_REVIEW_REQUIRED",
		"repairable_sle": [],
		"repairable_gl": [],
		"evidence": {},
		"reasons": [],
	}
	if parent_ds != 2:
		result["reasons"].append(f"parent docstatus={parent_ds}, expected 2")
		return result

	sles = _sle_peers(voucher_type, voucher_no)
	active = [s for s in sles if not cint(s.is_cancelled)]
	cancelled = [s for s in sles if cint(s.is_cancelled)]
	result["evidence"]["sle_total"] = len(sles)
	result["evidence"]["sle_active"] = len(active)
	result["evidence"]["sle_cancelled"] = len(cancelled)

	# Group by voucher_detail_no: expect original + reverse for cancel
	by_detail: dict[str, list] = defaultdict(list)
	for s in sles:
		by_detail[s.voucher_detail_no or ""].append(s)

	partial_sle = False
	bundle_issues = False
	stale_repost = False

	for detail, peers in by_detail.items():
		act = [p for p in peers if not cint(p.is_cancelled)]
		can = [p for p in peers if cint(p.is_cancelled)]
		if not act:
			continue
		# Repairable pattern: active original + cancelled reverse with opposite qty
		for a in act:
			opp = [
				c
				for c in can
				if flt(c.actual_qty) == -flt(a.actual_qty)
				and c.item_code == a.item_code
				and c.warehouse == a.warehouse
			]
			binfo = _bundle_info(a.serial_and_batch_bundle)
			cand = {
				"sle": a.name,
				"item_code": a.item_code,
				"warehouse": a.warehouse,
				"actual_qty": a.actual_qty,
				"bundle": a.serial_and_batch_bundle,
				"bundle_info": binfo,
				"reverse_sles": [o.name for o in opp],
				"detail": detail,
			}
			if opp and binfo and cint(binfo.get("is_cancelled") or 0) == 1 and cint(binfo.get("docstatus") or 0) == 2:
				cand["pattern"] = "PARTIAL_CANCEL_SLE"
				partial_sle = True
				result["repairable_sle"].append(cand)
			elif opp and not a.serial_and_batch_bundle:
				cand["pattern"] = "PARTIAL_CANCEL_SLE"
				partial_sle = True
				result["repairable_sle"].append(cand)
			elif opp and binfo and not cint(binfo.get("is_cancelled") or 0):
				cand["pattern"] = "BUNDLE_INCONSISTENCY"
				bundle_issues = True
				result["reasons"].append(f"{a.name}: reverse exists but bundle not cancelled")
			else:
				cand["pattern"] = "MANUAL_REVIEW_REQUIRED"
				result["reasons"].append(f"{a.name}: no clear reverse pair")

			# Stale reactivation heuristic: reverse older cancel flag but active modified later
			if opp and a.modified and max(o.modified for o in opp) and str(a.modified) > str(max(o.modified for o in opp)):
				if cint(a.is_cancelled) == 0 and all(cint(o.is_cancelled) == 1 for o in opp):
					stale_repost = True
					cand["stale_repost_suspect"] = True

	# GL side
	immutable = cint(frappe.db.get_single_value("Accounts Settings", "enable_immutable_ledger") or 0)
	gl_rows = frappe.db.sql(
		"""
		select name, account, debit, credit, is_cancelled, remarks, creation, modified
		from `tabGL Entry`
		where voucher_type=%s and voucher_no=%s
		order by creation
		""",
		(voucher_type, voucher_no),
		as_dict=True,
	)
	gl_active = [g for g in gl_rows if not cint(g.is_cancelled)]
	gl_cancelled = [g for g in gl_rows if cint(g.is_cancelled)]
	result["evidence"]["gl_total"] = len(gl_rows)
	result["evidence"]["gl_active"] = len(gl_active)
	result["evidence"]["gl_cancelled"] = len(gl_cancelled)
	result["evidence"]["immutable_ledger"] = immutable

	partial_gl = False
	if gl_active and not immutable:
		# Non-immutable: cancelled parent should have originals is_cancelled=1
		# and reverse rows also is_cancelled=1. Any is_cancelled=0 is orphan.
		# Only auto-flag if reverse remarks exist OR cancelled peers exist for same accounts.
		has_cancel_remarks = any(
			(g.remarks or "").startswith("On cancellation of") for g in gl_rows
		)
		if gl_cancelled or has_cancel_remarks:
			# Some cancel happened; remaining active are orphans
			partial_gl = True
			for g in gl_active:
				if (g.remarks or "").startswith("On cancellation of"):
					# reverse row incorrectly left active under non-immutable — unusual
					result["reasons"].append(f"GL {g.name}: reverse remark but is_cancelled=0")
				else:
					result["repairable_gl"].append(
						{
							"gl": g.name,
							"account": g.account,
							"debit": g.debit,
							"credit": g.credit,
							"pattern": "PARTIAL_CANCEL_GL",
						}
					)
		elif len(gl_active) == len(gl_rows) and len(gl_rows) > 0:
			# Cancelled parent, zero cancelled GL, no reverse remarks → cancel never touched GL
			# Could be stock voucher without accounting, or incomplete cancel.
			result["reasons"].append(
				"GL all active on cancelled parent with no reverse remarks — manual review"
			)

	if immutable and gl_active:
		result["reasons"].append(
			"immutable ledger ON: active GL on cancelled parent may be legitimate; do not auto-repair GL"
		)

	# Final classification
	if partial_sle and partial_gl:
		result["classification"] = "PARTIAL_CANCEL_BOTH"
	elif partial_sle and stale_repost:
		result["classification"] = "STALE_REPOST_REACTIVATION"
	elif partial_sle:
		result["classification"] = "PARTIAL_CANCEL_SLE"
	elif partial_gl:
		result["classification"] = "PARTIAL_CANCEL_GL"
	elif bundle_issues:
		result["classification"] = "BUNDLE_INCONSISTENCY"
	elif not active and not (gl_active and not immutable):
		result["classification"] = "HEALTHY"
		result["reasons"].append("no active SLE orphans")
	else:
		result["classification"] = "MANUAL_REVIEW_REQUIRED"

	return result


def riv_conflicts_for_chains(chains: list[dict]) -> list[dict]:
	"""Return Queued/In Progress RIVs that overlap item/warehouse chains.

	A RIV conflicts if:
	  - status in Queued/In Progress AND
	  - (item_code matches chain OR item_code is empty/null) AND
	  - (warehouse matches chain OR warehouse is empty/null)
	Empty item/warehouse means transaction/company-scoped and is treated as conflict
	when company matches or company is empty.
	"""
	if not chains:
		return []
	items = {c["item_code"] for c in chains}
	warehouses = {c["warehouse"] for c in chains}
	rows = frappe.db.sql(
		"""
		select name, item_code, warehouse, status, posting_date, posting_time,
		       company, based_on, voucher_type, voucher_no, creation, modified
		from `tabRepost Item Valuation`
		where status in ('Queued', 'In Progress')
		order by creation
		""",
		as_dict=True,
	)
	conflicts = []
	for r in rows or []:
		item_ok = (not r.item_code) or (r.item_code in items)
		wh_ok = (not r.warehouse) or (r.warehouse in warehouses)
		if item_ok and wh_ok:
			r["conflict_reason"] = (
				"broad_scope" if (not r.item_code or not r.warehouse) else "exact_chain"
			)
			conflicts.append(r)
	return conflicts


def fingerprint_sle(name: str) -> dict:
	fields = [
		"name",
		"item_code",
		"warehouse",
		"actual_qty",
		"qty_after_transaction",
		"valuation_rate",
		"stock_value",
		"stock_value_difference",
		"is_cancelled",
		"serial_and_batch_bundle",
		"voucher_type",
		"voucher_no",
		"voucher_detail_no",
		"posting_date",
		"posting_time",
		"posting_datetime",
		"modified",
		"creation",
	]
	return frappe.db.get_value("Stock Ledger Entry", name, fields, as_dict=True) or {}


def validate_sle_candidate(sle_name: str, classification: dict) -> tuple[bool, str]:
	repairable = {c["sle"]: c for c in classification.get("repairable_sle") or []}
	if sle_name not in repairable:
		return False, "not in repairable_sle list"
	row = fingerprint_sle(sle_name)
	if not row:
		return False, "SLE missing"
	if cint(row.get("is_cancelled")) == 1 and not row.get("serial_and_batch_bundle"):
		return False, "already repaired (idempotent skip)"
	if cint(row.get("is_cancelled")) == 1 and row.get("serial_and_batch_bundle"):
		return False, "is_cancelled=1 but bundle still linked — manual review"
	if _parent_docstatus(row["voucher_type"], row["voucher_no"]) != 2:
		return False, "parent not cancelled"
	meta = repairable[sle_name]
	if not meta.get("reverse_sles"):
		return False, "no reverse SLE evidence"
	if flt(row.get("actual_qty")) != flt(meta.get("actual_qty")):
		return False, "actual_qty changed since classification"
	return True, "ok"


def repair_sle(sle_name: str, dry_run: bool = True) -> dict:
	before = fingerprint_sle(sle_name)
	change = {
		"name": sle_name,
		"before": before,
		"fields": {"is_cancelled": 1, "serial_and_batch_bundle": None},
		"dry_run": dry_run,
	}
	if dry_run:
		change["after"] = {**before, "is_cancelled": 1, "serial_and_batch_bundle": None}
		return change

	# Field-limited only
	frappe.db.set_value(
		"Stock Ledger Entry",
		sle_name,
		{
			"is_cancelled": 1,
			"serial_and_batch_bundle": None,
			"modified": now(),
			"modified_by": frappe.session.user or "Administrator",
		},
		update_modified=False,
	)
	after = fingerprint_sle(sle_name)
	# Preserve movement/accounting amounts
	for k in (
		"actual_qty",
		"stock_value_difference",
		"valuation_rate",
		"posting_date",
		"posting_time",
		"voucher_no",
		"voucher_detail_no",
	):
		if flt(before.get(k) or 0) != flt(after.get(k) or 0) and before.get(k) != after.get(k):
			frappe.throw(f"Unexpected field change on {sle_name}.{k}: {before.get(k)} -> {after.get(k)}")
	if cint(after.get("is_cancelled")) != 1:
		frappe.throw(f"Failed to set is_cancelled on {sle_name}")
	if after.get("serial_and_batch_bundle"):
		frappe.throw(f"Failed to clear bundle on {sle_name}")
	change["after"] = after
	return change


def repair_gl(gl_name: str, dry_run: bool = True) -> dict:
	"""Set is_cancelled=1 only. Never touch debit/credit."""
	before = frappe.db.get_value(
		"GL Entry",
		gl_name,
		["name", "voucher_type", "voucher_no", "account", "debit", "credit", "is_cancelled", "remarks"],
		as_dict=True,
	)
	change = {
		"name": gl_name,
		"before": before,
		"fields": {"is_cancelled": 1},
		"dry_run": dry_run,
	}
	if dry_run:
		change["after"] = {**before, "is_cancelled": 1}
		return change
	frappe.db.set_value(
		"GL Entry",
		gl_name,
		{
			"is_cancelled": 1,
			"modified": now(),
			"modified_by": frappe.session.user or "Administrator",
		},
		update_modified=False,
	)
	after = frappe.db.get_value(
		"GL Entry",
		gl_name,
		["name", "voucher_type", "voucher_no", "account", "debit", "credit", "is_cancelled", "remarks"],
		as_dict=True,
	)
	for k in ("debit", "credit", "account", "voucher_no"):
		if before.get(k) != after.get(k):
			frappe.throw(f"Unexpected GL field change {gl_name}.{k}")
	if cint(after.is_cancelled) != 1:
		frappe.throw(f"Failed GL cancel flag {gl_name}")
	change["after"] = after
	return change


def create_targeted_riv(
	item_code: str,
	warehouse: str,
	posting_date: str,
	posting_time: str,
	execute_now: bool = False,
) -> str:
	"""Create+submit ERPNext-native RIV for one item-warehouse chain. Returns name.

	If execute_now=True, runs execute_reposting_entry synchronously (useful on
	dev/clone; on Production prefer queue workers after submit).
	"""
	existing = frappe.db.get_value(
		"Repost Item Valuation",
		{
			"item_code": item_code,
			"warehouse": warehouse,
			"posting_date": posting_date,
			"posting_time": posting_time,
			"status": ("in", ["Queued", "In Progress"]),
			"docstatus": 1,
		},
		"name",
	)
	if existing:
		return existing

	company = frappe.db.get_value("Warehouse", warehouse, "company")
	doc = frappe.new_doc("Repost Item Valuation")
	doc.based_on = "Item and Warehouse"
	doc.item_code = item_code
	doc.warehouse = warehouse
	doc.posting_date = posting_date
	doc.posting_time = posting_time
	doc.company = company
	doc.allow_negative_stock = 1
	doc.allow_zero_rate = 0
	doc.recreate_stock_ledgers = 0
	doc.flags.ignore_permissions = True
	doc.insert()
	doc.submit()
	frappe.db.commit()

	if execute_now:
		from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
			execute_reposting_entry,
		)

		execute_reposting_entry(doc.name)
		frappe.db.commit()
	return doc.name


def verify_voucher(voucher_type: str, voucher_no: str) -> dict:
	sles = _sle_peers(voucher_type, voucher_no)
	active = [s for s in sles if not cint(s.is_cancelled)]
	gl_active = frappe.db.sql(
		"""
		select name from `tabGL Entry`
		where voucher_type=%s and voucher_no=%s and ifnull(is_cancelled,0)=0
		""",
		(voucher_type, voucher_no),
		pluck=True,
	)
	return {
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"parent_docstatus": _parent_docstatus(voucher_type, voucher_no),
		"active_sle_count": len(active),
		"active_sles": [s.name for s in active],
		"active_gl_count": len(gl_active or []),
		"active_gls": gl_active or [],
		"pass_sle": len(active) == 0,
	}


def summarize_audit(sle_orphans: list[dict], gl_orphans: list[dict]) -> dict:
	by_voucher: dict[str, dict] = {}
	for s in sle_orphans:
		key = f"{s.voucher_type}::{s.voucher_no}"
		by_voucher.setdefault(
			key,
			{"voucher_type": s.voucher_type, "voucher_no": s.voucher_no, "sle": [], "gl": [], "items": set(), "warehouses": set(), "batches": set()},
		)
		by_voucher[key]["sle"].append(s.name)
		by_voucher[key]["items"].add(s.item_code)
		by_voucher[key]["warehouses"].add(s.warehouse)
		b = _bundle_info(s.serial_and_batch_bundle)
		if b and not b.get("missing"):
			entries = frappe.db.sql(
				"select batch_no from `tabSerial and Batch Entry` where parent=%s",
				s.serial_and_batch_bundle,
				pluck=True,
			)
			for bn in entries or []:
				if bn:
					by_voucher[key]["batches"].add(bn)

	for g in gl_orphans:
		key = f"{g.voucher_type}::{g.voucher_no}"
		by_voucher.setdefault(
			key,
			{"voucher_type": g.voucher_type, "voucher_no": g.voucher_no, "sle": [], "gl": [], "items": set(), "warehouses": set(), "batches": set()},
		)
		by_voucher[key]["gl"].append(g.name)

	voucher_summary = []
	for v in by_voucher.values():
		voucher_summary.append(
			{
				"voucher_type": v["voucher_type"],
				"voucher_no": v["voucher_no"],
				"orphan_sle_count": len(v["sle"]),
				"orphan_gl_count": len(v["gl"]),
				"items": sorted(v["items"]),
				"warehouses": sorted(v["warehouses"]),
				"batches": sorted(v["batches"]),
			}
		)
	return {
		"orphan_sle_count": len(sle_orphans),
		"orphan_gl_count": len(gl_orphans),
		"voucher_count": len(voucher_summary),
		"vouchers": voucher_summary,
	}


def direct_repair_orphan_sles(
	*,
	dry_run: bool = True,
	accept_concurrency_risk: bool = False,
) -> dict:
	"""Allowlisted SLE flag repair for MAT-STE-2026-36965 only.

	- Does NOT create/enqueue/execute/cancel/modify RIVs.
	- Does NOT modify GL, Bin, Batch, or other SLEs.
	- Does NOT require the RIV queue to be empty.
	- Reports Queued/In Progress RIV conflicts as an explicit risk.
	- Requires accept_concurrency_risk=True to proceed when conflicts exist.
	- Idempotent: already-repaired rows are skipped.
	- On any validation failure: rollback and raise (no partial commit).
	"""
	al = DIRECT_REPAIR_ALLOWLIST
	voucher_type = al["voucher_type"]
	voucher_no = al["voucher_no"]
	sle_names = tuple(al["sles"].keys())

	report: dict[str, Any] = {
		"mode": "direct_dry_run" if dry_run else "direct_repair",
		"dry_run": dry_run,
		"accept_concurrency_risk": accept_concurrency_risk,
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"allowlist_sles": list(sle_names),
		"scope_note": al["scope_note"],
		"mutation": False,
		"riv_created": False,
		"gl_modified": False,
		"bin_modified": False,
		"batch_modified": False,
		"sle_changes": [],
		"skipped": [],
		"blocked": False,
		"risks": [],
	}

	# --- concurrency risk report (never silent) ---
	live_for_chains = []
	for name in sle_names:
		row = fingerprint_sle(name)
		if row and row.get("item_code") and row.get("warehouse"):
			live_for_chains.append(
				{"item_code": row.get("item_code"), "warehouse": row.get("warehouse")}
			)
	conflicts = riv_conflicts_for_chains(live_for_chains)
	report["riv_conflict_count"] = len(conflicts)
	report["riv_conflict_sample"] = conflicts[:5]
	if conflicts:
		report["risks"].append(
			{
				"type": "ACTIVE_RIV_CONCURRENCY",
				"detail": (
					f"{len(conflicts)} Queued/In Progress RIV(s) overlap the "
					"affected item/warehouse chains (including broad-scope). "
					"direct_repair does not wait for them. Concurrent RIV may "
					"rewrite SLE rows after this flag repair."
				),
			}
		)
		if not accept_concurrency_risk:
			report["blocked"] = True
			report["block_reason"] = (
				"riv_conflicts_present; pass accept_concurrency_risk=True to "
				"proceed explicitly (user accepts concurrency risk)"
			)
			return report

	report["risks"].append(
		{
			"type": "SCOPE_LIMIT",
			"detail": al["scope_note"],
		}
	)

	# --- validate inside a transaction; rollback on failure ---
	frappe.db.begin()
	try:
		parent_ds = _parent_docstatus(voucher_type, voucher_no)
		if parent_ds != 2:
			frappe.throw(
				f"Allowlisted voucher {voucher_no} docstatus={parent_ds}, expected 2"
			)

		# Refuse any non-allowlisted SLE on this call path (defense in depth)
		for name, meta in al["sles"].items():
			row = fingerprint_sle(name)
			if not row:
				frappe.throw(f"Allowlisted SLE missing: {name}")
			if row.get("voucher_type") != voucher_type or row.get("voucher_no") != voucher_no:
				frappe.throw(f"{name} does not belong to {voucher_type} {voucher_no}")
			if row.get("item_code") != meta["item_code"]:
				frappe.throw(f"{name} item_code mismatch: {row.get('item_code')}")
			if flt(row.get("actual_qty")) != flt(meta["actual_qty"]):
				frappe.throw(
					f"{name} actual_qty mismatch: {row.get('actual_qty')} != {meta['actual_qty']}"
				)

			# Idempotent success path
			if cint(row.get("is_cancelled")) == 1 and not row.get("serial_and_batch_bundle"):
				report["skipped"].append(
					{"sle": name, "reason": "already_repaired_idempotent"}
				)
				continue

			# Must still be active orphan with cancelled bundle
			if cint(row.get("is_cancelled")) != 0:
				frappe.throw(
					f"{name} unexpected is_cancelled={row.get('is_cancelled')} "
					"(expected 0 for repair, or already repaired)"
				)
			bundle = row.get("serial_and_batch_bundle")
			if bundle != meta["expected_bundle"]:
				frappe.throw(
					f"{name} bundle mismatch: {bundle} != {meta['expected_bundle']}"
				)
			binfo = _bundle_info(bundle)
			if not binfo or binfo.get("missing"):
				frappe.throw(f"{name}: bundle {bundle} missing")
			if cint(binfo.get("is_cancelled")) != 1 or cint(binfo.get("docstatus")) != 2:
				frappe.throw(
					f"{name}: bundle {bundle} not cancelled "
					f"(is_cancelled={binfo.get('is_cancelled')}, docstatus={binfo.get('docstatus')})"
				)

			# Preserve snapshot of valuation fields before write
			preserved = {
				k: row.get(k)
				for k in (
					"actual_qty",
					"stock_value_difference",
					"valuation_rate",
					"stock_value",
					"qty_after_transaction",
					"posting_date",
					"posting_time",
					"voucher_no",
					"voucher_detail_no",
				)
			}

			if dry_run:
				report["sle_changes"].append(
					{
						"name": name,
						"before": row,
						"fields": {"is_cancelled": 1, "serial_and_batch_bundle": None},
						"preserved": preserved,
						"dry_run": True,
					}
				)
				continue

			frappe.db.set_value(
				"Stock Ledger Entry",
				name,
				{
					"is_cancelled": 1,
					"serial_and_batch_bundle": None,
					"modified": now(),
					"modified_by": frappe.session.user or "Administrator",
				},
				update_modified=False,
			)
			after = fingerprint_sle(name)
			for k, v in preserved.items():
				if k in ("actual_qty", "stock_value_difference", "valuation_rate", "stock_value", "qty_after_transaction"):
					if flt(after.get(k) or 0) != flt(v or 0):
						frappe.throw(f"{name}: preserved field {k} changed {v} -> {after.get(k)}")
				elif after.get(k) != v:
					frappe.throw(f"{name}: preserved field {k} changed {v} -> {after.get(k)}")
			if cint(after.get("is_cancelled")) != 1:
				frappe.throw(f"{name}: failed to set is_cancelled=1")
			if after.get("serial_and_batch_bundle"):
				frappe.throw(f"{name}: failed to clear serial_and_batch_bundle")

			report["sle_changes"].append(
				{
					"name": name,
					"before": {**row, **{"_preserved": preserved}},
					"after": after,
					"fields": {"is_cancelled": 1, "serial_and_batch_bundle": None},
					"dry_run": False,
				}
			)

		# Ensure we never touched non-allowlisted SLEs in this voucher beyond the two
		# (no writes issued to others; record count for audit)
		report["unrelated_sle_write_count"] = 0

		if dry_run:
			frappe.db.rollback()
			report["mutation"] = False
			report["verify"] = None
		elif not report["sle_changes"]:
			# Idempotent no-op: nothing written
			frappe.db.rollback()
			report["mutation"] = False
			report["verify"] = verify_voucher(voucher_type, voucher_no)
		else:
			# Pre-commit fingerprint check (still in same transaction)
			for name in sle_names:
				aft = fingerprint_sle(name)
				if cint(aft.get("is_cancelled")) != 1 or aft.get("serial_and_batch_bundle"):
					frappe.throw(f"Pre-commit verify failed for {name}: {aft}")
			frappe.db.commit()
			report["mutation"] = True
			report["verify"] = verify_voucher(voucher_type, voucher_no)

	except Exception:
		frappe.db.rollback()
		raise

	return report


def run(
	mode: str = "audit",
	vouchers: list[str] | None = None,
	voucher_type: str = "Stock Entry",
	allow_gl: bool = False,
	create_riv: bool = False,
	riv_posting_date: str | None = None,
	riv_posting_time: str | None = None,
	accept_concurrency_risk: bool = False,
) -> dict:
	"""Main entrypoint for bench console / execute."""
	mode = (mode or "audit").strip().lower()
	report: dict[str, Any] = {
		"site": getattr(frappe.local, "site", None),
		"mode": mode,
		"allow_gl": allow_gl,
		"mutation": False,
	}

	if mode in ("direct_dry_run", "direct_repair"):
		# Hard-scoped to DIRECT_REPAIR_ALLOWLIST only. Ignores vouchers= arg for safety.
		if vouchers and set(vouchers) != {DIRECT_REPAIR_ALLOWLIST["voucher_no"]}:
			frappe.throw(
				"direct_repair only allows voucher "
				f"{DIRECT_REPAIR_ALLOWLIST['voucher_no']}; got {vouchers}"
			)
		if allow_gl or create_riv:
			frappe.throw("direct_repair forbids allow_gl=True and create_riv=True")
		out = direct_repair_orphan_sles(
			dry_run=(mode == "direct_dry_run"),
			accept_concurrency_risk=accept_concurrency_risk,
		)
		out["site"] = report["site"]
		return out

	if mode == "audit":
		sle = audit_orphan_sles(vouchers)
		gl = audit_orphan_gl(vouchers)
		summary = summarize_audit(sle, gl)
		classifications = []
		for v in summary["vouchers"]:
			classifications.append(classify_voucher(v["voucher_type"], v["voucher_no"]))
		report.update(
			{
				"sle_voucher_types_present": _discover_sle_voucher_types(),
				"gl_voucher_types_present": _discover_gl_voucher_types(),
				"summary": summary,
				"orphan_sles": sle,
				"orphan_gls": gl,
				"classifications": classifications,
			}
		)
		return report

	if mode == "riv_check":
		if not vouchers:
			vouchers = [KNOWN_INCIDENT["voucher"]]
		chains = []
		for vno in vouchers:
			clf = classify_voucher(voucher_type, vno)
			for c in clf.get("repairable_sle") or []:
				chains.append({"item_code": c["item_code"], "warehouse": c["warehouse"]})
			# also include already-known orphans even if classification empty
			for s in audit_orphan_sles([vno]):
				chains.append({"item_code": s.item_code, "warehouse": s.warehouse})
		# dedupe
		uniq = {(c["item_code"], c["warehouse"]): c for c in chains}
		chains = list(uniq.values())
		conflicts = riv_conflicts_for_chains(chains)
		report.update({"chains": chains, "conflicts": conflicts, "conflict_count": len(conflicts)})
		return report

	if mode in ("dry_run", "repair"):
		if not vouchers:
			frappe.throw("vouchers= required for dry_run/repair")
		dry = mode == "dry_run"
		report["mutation"] = not dry
		results = []
		all_chains = []
		for vno in vouchers:
			clf = classify_voucher(voucher_type, vno)
			# RIV gate
			chains = [
				{"item_code": c["item_code"], "warehouse": c["warehouse"]}
				for c in clf.get("repairable_sle") or []
			]
			all_chains.extend(chains)
			conflicts = riv_conflicts_for_chains(chains)
			entry = {
				"voucher": vno,
				"classification": clf["classification"],
				"riv_conflicts": len(conflicts),
				"sle_changes": [],
				"gl_changes": [],
				"skipped": [],
				"blocked": False,
			}
			if conflicts:
				entry["blocked"] = True
				entry["block_reason"] = f"{len(conflicts)} conflicting RIV(s)"
				entry["conflict_sample"] = conflicts[:5]
				results.append(entry)
				continue

			for cand in clf.get("repairable_sle") or []:
				ok, reason = validate_sle_candidate(cand["sle"], clf)
				if not ok:
					entry["skipped"].append({"sle": cand["sle"], "reason": reason})
					continue
				entry["sle_changes"].append(repair_sle(cand["sle"], dry_run=dry))

			if allow_gl:
				if cint(frappe.db.get_single_value("Accounts Settings", "enable_immutable_ledger") or 0):
					entry["skipped"].append({"gl": "*", "reason": "immutable ledger — GL auto-repair disabled"})
				elif clf["classification"] in ("PARTIAL_CANCEL_GL", "PARTIAL_CANCEL_BOTH"):
					for g in clf.get("repairable_gl") or []:
						entry["gl_changes"].append(repair_gl(g["gl"], dry_run=dry))
				else:
					entry["skipped"].append({"gl": "*", "reason": f"classification {clf['classification']} — GL not auto-repaired"})

			if not dry:
				frappe.db.commit()
				entry["verify"] = verify_voucher(voucher_type, vno)

				if create_riv:
					# posting boundary from earliest repaired SLE
					pdate = riv_posting_date
					ptime = riv_posting_time
					if not pdate:
						for ch in entry["sle_changes"]:
							b = ch.get("before") or {}
							pdate = pdate or b.get("posting_date")
							ptime = ptime or str(b.get("posting_time") or "00:00:01")
					entry["rivs"] = []
					seen = set()
					for cand in clf.get("repairable_sle") or []:
						key = (cand["item_code"], cand["warehouse"])
						if key in seen:
							continue
						seen.add(key)
						riv_name = create_targeted_riv(
							cand["item_code"], cand["warehouse"], str(pdate), str(ptime)[:8]
						)
						entry["rivs"].append(riv_name)
			results.append(entry)

		report["results"] = results
		report["chains"] = all_chains
		return report

	if mode == "create_riv":
		"""Create targeted RIVs for item/warehouse chains of a voucher's SLE rows.

		Uses posting boundary from the earliest SLE on the voucher (active or not).
		Does not create duplicates while Queued/In Progress exist.
		"""
		if not vouchers:
			frappe.throw("vouchers= required for create_riv")
		pdate = riv_posting_date
		ptime = riv_posting_time or "11:00:01"
		created = []
		for vno in vouchers:
			sles = _sle_peers(voucher_type, vno)
			if not sles:
				continue
			if not pdate:
				# earliest posting_date among peers
				pdate = min(str(s.posting_datetime or s.creation)[:10] for s in sles)
				# matching time from that row
				earliest = min(sles, key=lambda s: str(s.posting_datetime or s.creation))
				ptime = str(earliest.posting_time or "11:00:01")[:8]
			chains = {}
			for s in sles:
				chains[(s.item_code, s.warehouse)] = {
					"item_code": s.item_code,
					"warehouse": s.warehouse,
				}
			conflicts = riv_conflicts_for_chains(list(chains.values()))
			if conflicts:
				report["blocked"] = True
				report["conflicts"] = conflicts[:10]
				report["conflict_count"] = len(conflicts)
				return report
			for ch in chains.values():
				name = create_targeted_riv(ch["item_code"], ch["warehouse"], str(pdate), str(ptime)[:8])
				created.append(
					{"riv": name, "item_code": ch["item_code"], "warehouse": ch["warehouse"],
					 "posting_date": pdate, "posting_time": ptime}
				)
		report["rivs"] = created
		report["mutation"] = True
		return report

	if mode == "verify":
		if not vouchers:
			frappe.throw("vouchers= required for verify")
		report["results"] = [verify_voucher(voucher_type, v) for v in vouchers]
		# also re-audit those vouchers
		report["remaining_orphan_sles"] = audit_orphan_sles(vouchers)
		return report

	frappe.throw(f"Unknown mode: {mode}")


# Convenience aliases for console
audit = lambda **kw: run(mode="audit", **kw)  # noqa: E731
dry_run = lambda **kw: run(mode="dry_run", **kw)  # noqa: E731
repair = lambda **kw: run(mode="repair", **kw)  # noqa: E731
