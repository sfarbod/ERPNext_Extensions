# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Facility Management — cancelled Journal Entry reconciliation (scan + repair).

Production-ready utility for detecting and repairing stale Facility / Facility
Repayment references to cancelled or missing Journal Entries.

Usage (bench execute)::

	bench --site <site> execute \\
	  erpnext_extensions.facility_management.facility_je_reconciliation.run_global_scan \\
	  --kwargs '{"dry_run": true}'

	bench --site <site> execute \\
	  erpnext_extensions.facility_management.facility_je_reconciliation.repair_facility \\
	  --kwargs '{"facility": "FAC-2026-11810", "dry_run": false, "confirm": true}'
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.facility_management.facility_accounting import refresh_facility_paid_fields
from erpnext_extensions.facility_management.facility_balances import get_facility_balance_row

CLASS_SAFE = "SAFE_AUTO_REPAIR"
CLASS_MANUAL = "MANUAL_REVIEW_REQUIRED"
CLASS_BLOCKED = "BLOCKED_BY_DEPENDENCIES"
CLASS_OK = "NO_CHANGE_REQUIRED"

CATEGORY_A = "A_CANCELLED_RECEIPT_REFERENCE"
CATEGORY_B = "B_CANCELLED_REPAYMENT_REFERENCE"
CATEGORY_C = "C_INVALID_REFERENCE"
CATEGORY_D = "D_STATUS_INCONSISTENCY"
CATEGORY_E = "E_AMOUNT_INCONSISTENCY"
CATEGORY_F = "F_REPLACEMENT_DOCUMENT"
CATEGORY_G = "G_DOWNSTREAM_DEPENDENCY"
CATEGORY_H = "H_SCHEDULE_INCONSISTENCY"

# Set while Facility Repayment.on_cancel cancels its own JE (avoid double work / recursion).
FLAG_IN_REPAYMENT_CANCEL = "in_facility_repayment_je_cancel"
FLAG_IN_RECEIPT_REPAIR = "in_facility_receipt_je_reconcile"


def _log(msg: str) -> None:
	frappe.logger("facility_je_reconciliation").info(msg)
	print(msg)


def _je_meta(je_name: str | None) -> dict[str, Any] | None:
	if not je_name:
		return None
	if not frappe.db.exists("Journal Entry", je_name):
		return None
	return frappe.db.get_value(
		"Journal Entry",
		je_name,
		["name", "docstatus", "company", "voucher_type", "cheque_no", "posting_date"],
		as_dict=True,
	)


def _submitted_repayments(facility: str) -> list[dict[str, Any]]:
	return frappe.get_all(
		"Facility Repayment",
		filters={"facility": facility, "docstatus": 1},
		fields=[
			"name",
			"journal_entry",
			"principal_amount",
			"profit_amount",
			"penalty_amount",
			"total_payment_amount",
			"posting_date",
			"repayment_method",
		],
		order_by="posting_date asc, creation asc",
	)


def _draft_repayments(facility: str) -> list[dict[str, Any]]:
	return frappe.get_all(
		"Facility Repayment",
		filters={"facility": facility, "docstatus": 0},
		fields=["name", "journal_entry", "principal_amount", "profit_amount"],
	)


def _candidate_replacement_receipt_jes(facility_name: str, *, exclude: str | None = None) -> list[str]:
	"""Submitted Bank Entry JEs whose cheque_no matches the facility name."""
	filters: dict[str, Any] = {
		"cheque_no": facility_name,
		"docstatus": 1,
		"voucher_type": "Bank Entry",
	}
	names = frappe.get_all("Journal Entry", filters=filters, pluck="name", order_by="creation asc")
	if exclude:
		names = [n for n in names if n != exclude]
	# Exclude JEs already linked as repayment JEs
	repay_jes = set(
		frappe.get_all(
			"Facility Repayment",
			filters={"facility": facility_name, "journal_entry": ("is", "set")},
			pluck="journal_entry",
		)
	)
	return [n for n in names if n not in repay_jes]


def _issue(
	*,
	category: str,
	classification: str,
	facility: str | None = None,
	repayment: str | None = None,
	reference_field: str | None = None,
	journal_entry: str | None = None,
	je_docstatus: int | None = None,
	current_status: str | None = None,
	expected_status: str | None = None,
	current_amounts: dict | None = None,
	expected_amounts: dict | None = None,
	dependencies: list | None = None,
	recommended_action: str = "",
	details: dict | None = None,
) -> dict[str, Any]:
	return {
		"category": category,
		"classification": classification,
		"facility": facility,
		"repayment": repayment,
		"reference_field": reference_field,
		"journal_entry": journal_entry,
		"je_docstatus": je_docstatus,
		"current_status": current_status,
		"expected_status": expected_status,
		"current_amounts": current_amounts or {},
		"expected_amounts": expected_amounts or {},
		"dependencies": dependencies or [],
		"recommended_action": recommended_action,
		"details": details or {},
	}


def scan_facility(facility_name: str) -> list[dict[str, Any]]:
	"""Scan a single Facility for JE reconciliation issues."""
	if not frappe.db.exists("Facility", facility_name):
		return [
			_issue(
				category=CATEGORY_C,
				classification=CLASS_MANUAL,
				facility=facility_name,
				recommended_action="Facility not found",
			)
		]

	fac = frappe.get_doc("Facility", facility_name)
	issues: list[dict[str, Any]] = []
	reps = _submitted_repayments(facility_name)
	draft_reps = _draft_repayments(facility_name)
	receipt_je = (fac.receipt_journal_entry or "").strip() or None
	je = _je_meta(receipt_je)
	replacements = _candidate_replacement_receipt_jes(facility_name, exclude=receipt_je)

	# --- Receipt reference categories ---
	if receipt_je:
		if je is None:
			issues.append(
				_issue(
					category=CATEGORY_C,
					classification=CLASS_SAFE if not reps else CLASS_BLOCKED,
					facility=facility_name,
					reference_field="receipt_journal_entry",
					journal_entry=receipt_je,
					je_docstatus=None,
					current_status=fac.status,
					expected_status="Draft" if not reps else fac.status,
					dependencies=[r["name"] for r in reps],
					recommended_action=(
						"Clear missing receipt JE reference and restore Draft"
						if not reps
						else "Blocked: submitted repayments exist with missing receipt JE"
					),
				)
			)
		elif cint(je.docstatus) == 2:
			if reps and not replacements:
				issues.append(
					_issue(
						category=CATEGORY_G,
						classification=CLASS_BLOCKED,
						facility=facility_name,
						reference_field="receipt_journal_entry",
						journal_entry=receipt_je,
						je_docstatus=2,
						current_status=fac.status,
						expected_status=fac.status,
						dependencies=[r["name"] for r in reps],
						recommended_action=(
							"Blocked: Active repayments depend on cancelled receipt; "
							"manual accounting review required"
						),
						details={"submitted_repayments": reps},
					)
				)
			elif len(replacements) == 1:
				issues.append(
					_issue(
						category=CATEGORY_F,
						classification=CLASS_SAFE,
						facility=facility_name,
						reference_field="receipt_journal_entry",
						journal_entry=receipt_je,
						je_docstatus=2,
						current_status=fac.status,
						expected_status="Active",
						dependencies=[r["name"] for r in reps],
						recommended_action=f"Relink receipt_journal_entry to replacement {replacements[0]}",
						details={"replacement_je": replacements[0], "all_replacements": replacements},
					)
				)
			elif len(replacements) > 1:
				issues.append(
					_issue(
						category=CATEGORY_F,
						classification=CLASS_MANUAL,
						facility=facility_name,
						reference_field="receipt_journal_entry",
						journal_entry=receipt_je,
						je_docstatus=2,
						current_status=fac.status,
						expected_status=None,
						dependencies=[r["name"] for r in reps],
						recommended_action="Multiple submitted candidate receipt JEs — choose manually",
						details={"all_replacements": replacements},
					)
				)
			else:
				# No replacement, no (or only draft) repayments → clear + Draft
				expected = "Draft" if not cint(fac.is_opening_facility) else fac.status
				issues.append(
					_issue(
						category=CATEGORY_A,
						classification=CLASS_SAFE if not reps else CLASS_BLOCKED,
						facility=facility_name,
						reference_field="receipt_journal_entry",
						journal_entry=receipt_je,
						je_docstatus=2,
						current_status=fac.status,
						expected_status=expected,
						current_amounts={
							"received_amount": flt(fac.received_amount),
							"principal_amount": flt(fac.principal_amount),
						},
						expected_amounts={"received_amount": 0.0},
						dependencies=[r["name"] for r in reps] + [d["name"] for d in draft_reps],
						recommended_action=(
							"Clear cancelled receipt JE, zero received_amount, set status Draft"
							if not reps
							else "Blocked by submitted repayments"
						),
					)
				)
			if fac.status in ("Active", "Closed") and cint(je.docstatus) == 2 and not replacements:
				issues.append(
					_issue(
						category=CATEGORY_D,
						classification=CLASS_SAFE if not reps else CLASS_BLOCKED,
						facility=facility_name,
						reference_field="status",
						journal_entry=receipt_je,
						je_docstatus=2,
						current_status=fac.status,
						expected_status="Draft",
						recommended_action="Restore Facility status after cancelled receipt",
					)
				)
		elif cint(je.docstatus) == 1:
			# Valid submitted receipt — check company mismatch / cheque_no
			if je.company and je.company != fac.company:
				issues.append(
					_issue(
						category=CATEGORY_C,
						classification=CLASS_MANUAL,
						facility=facility_name,
						reference_field="receipt_journal_entry",
						journal_entry=receipt_je,
						je_docstatus=1,
						recommended_action="Receipt JE company differs from Facility company",
						details={"je_company": je.company, "facility_company": fac.company},
					)
				)
			if je.cheque_no and je.cheque_no != facility_name:
				issues.append(
					_issue(
						category=CATEGORY_C,
						classification=CLASS_MANUAL,
						facility=facility_name,
						reference_field="receipt_journal_entry",
						journal_entry=receipt_je,
						je_docstatus=1,
						recommended_action="Receipt JE cheque_no does not match Facility name",
						details={"cheque_no": je.cheque_no},
					)
				)
			if len(replacements) >= 1:
				# Multiple submitted receipt-like JEs while one is linked
				extras = [n for n in replacements if n != receipt_je]
				if extras:
					issues.append(
						_issue(
							category=CATEGORY_F,
							classification=CLASS_MANUAL,
							facility=facility_name,
							reference_field="receipt_journal_entry",
							journal_entry=receipt_je,
							je_docstatus=1,
							recommended_action="Additional submitted receipt-like JEs found",
							details={"extra_jes": extras},
						)
					)
		elif cint(je.docstatus) == 0:
			issues.append(
				_issue(
					category=CATEGORY_C,
					classification=CLASS_MANUAL,
					facility=facility_name,
					reference_field="receipt_journal_entry",
					journal_entry=receipt_je,
					je_docstatus=0,
					recommended_action="Facility references a Draft receipt JE — review",
				)
			)

	# Shared receipt JE across facilities
	if receipt_je:
		others = frappe.get_all(
			"Facility",
			filters={"receipt_journal_entry": receipt_je, "name": ("!=", facility_name)},
			pluck="name",
		)
		if others:
			issues.append(
				_issue(
					category=CATEGORY_F,
					classification=CLASS_MANUAL,
					facility=facility_name,
					reference_field="receipt_journal_entry",
					journal_entry=receipt_je,
					recommended_action="Receipt JE shared by multiple Facilities",
					details={"other_facilities": others},
				)
			)

	# --- Amount consistency (existing balance rules) ---
	bal = get_facility_balance_row(fac)
	amount_fields = (
		("paid_principal_amount", "paid_principal"),
		("paid_profit_amount", "paid_profit"),
		("paid_penalty_amount", "paid_penalty"),
		("remaining_principal_amount", "remaining_principal"),
		("remaining_profit_amount", "remaining_profit"),
		("remaining_total_amount", "remaining_total"),
	)
	mismatches = {}
	for fac_fn, bal_fn in amount_fields:
		cur = flt(fac.get(fac_fn))
		exp = flt(bal.get(bal_fn))
		if abs(cur - exp) > 0.01:
			mismatches[fac_fn] = {"current": cur, "expected": exp}
	if receipt_je and je and cint(je.docstatus) == 1:
		exp_received = flt(fac.principal_amount)
		if abs(flt(fac.received_amount) - exp_received) > 0.01:
			mismatches["received_amount"] = {
				"current": flt(fac.received_amount),
				"expected": exp_received,
			}
	elif not cint(fac.is_opening_facility) and (
		not receipt_je or (je and cint(je.docstatus) == 2) or (receipt_je and je is None)
	):
		if flt(fac.received_amount) and not (je and cint(je.docstatus) == 1):
			# stale received_amount when no valid receipt (non-opening facilities only)
			if "received_amount" not in mismatches and abs(flt(fac.received_amount)) > 0.01:
				mismatches["received_amount"] = {
					"current": flt(fac.received_amount),
					"expected": 0.0,
				}
	if mismatches:
		issues.append(
			_issue(
				category=CATEGORY_E,
				classification=CLASS_SAFE if not any(
					i["classification"] == CLASS_BLOCKED for i in issues
				)
				else CLASS_BLOCKED,
				facility=facility_name,
				current_status=fac.status,
				current_amounts={k: v["current"] for k, v in mismatches.items()},
				expected_amounts={k: v["expected"] for k, v in mismatches.items()},
				recommended_action="Refresh paid/remaining fields from submitted repayments",
				details={"mismatches": mismatches},
			)
		)

	# --- Schedule: contractual child table (no JE refs in this model) ---
	sched = fac.get("repayment_schedule") or []
	if sched:
		sum_p = sum(flt(r.principal_amount) for r in sched)
		sum_profit = sum(flt(r.profit_amount) for r in sched)
		# Informational only when totals diverge materially from facility contract
		if abs(sum_p - flt(fac.principal_amount)) > 0.01 or abs(sum_profit - flt(fac.profit_amount)) > 0.01:
			issues.append(
				_issue(
					category=CATEGORY_H,
					classification=CLASS_MANUAL,
					facility=facility_name,
					recommended_action=(
						"Repayment Schedule contractual totals differ from Facility amounts "
						"(schedule has no JE references; do not auto-regenerate)"
					),
					current_amounts={
						"schedule_principal": sum_p,
						"schedule_profit": sum_profit,
					},
					expected_amounts={
						"principal_amount": flt(fac.principal_amount),
						"profit_amount": flt(fac.profit_amount),
					},
					details={"note": "Facility Repayment Schedule is contractual; JE refs live on Facility Repayment"},
				)
			)

	# --- Repayments under this facility ---
	for rep in frappe.get_all(
		"Facility Repayment",
		filters={"facility": facility_name},
		fields=[
			"name",
			"docstatus",
			"journal_entry",
			"principal_amount",
			"profit_amount",
			"penalty_amount",
			"total_payment_amount",
		],
	):
		issues.extend(scan_repayment(rep.name, facility_doc=fac))

	if not issues:
		issues.append(
			_issue(
				category=CATEGORY_A,
				classification=CLASS_OK,
				facility=facility_name,
				current_status=fac.status,
				recommended_action="No reconciliation action required",
			)
		)
	return issues


def scan_repayment(repayment_name: str, *, facility_doc=None) -> list[dict[str, Any]]:
	"""Scan a single Facility Repayment for cancelled/invalid JE references."""
	if not frappe.db.exists("Facility Repayment", repayment_name):
		return [
			_issue(
				category=CATEGORY_C,
				classification=CLASS_MANUAL,
				repayment=repayment_name,
				recommended_action="Facility Repayment not found",
			)
		]
	rep = frappe.get_doc("Facility Repayment", repayment_name)
	facility_name = rep.facility
	if facility_doc is None and facility_name:
		facility_doc = frappe.get_doc("Facility", facility_name)

	issues: list[dict[str, Any]] = []
	je_name = (rep.journal_entry or "").strip() or None
	je = _je_meta(je_name)

	if cint(rep.docstatus) == 1 and je_name:
		if je is None:
			issues.append(
				_issue(
					category=CATEGORY_C,
					classification=CLASS_MANUAL,
					facility=facility_name,
					repayment=repayment_name,
					reference_field="journal_entry",
					journal_entry=je_name,
					current_status="Submitted",
					expected_status="Cancelled or reissued",
					recommended_action="Submitted repayment references missing JE — manual review",
				)
			)
		elif cint(je.docstatus) == 2:
			issues.append(
				_issue(
					category=CATEGORY_B,
					classification=CLASS_SAFE,
					facility=facility_name,
					repayment=repayment_name,
					reference_field="journal_entry",
					journal_entry=je_name,
					je_docstatus=2,
					current_status="Submitted",
					expected_status="Cancelled",
					current_amounts={
						"principal_amount": flt(rep.principal_amount),
						"profit_amount": flt(rep.profit_amount),
						"penalty_amount": flt(rep.penalty_amount),
					},
					recommended_action=(
						"Cancel Facility Repayment (clears JE ref) and refresh Facility paid fields; "
						"preserve other valid submitted repayments"
					),
				)
			)
			issues.append(
				_issue(
					category=CATEGORY_D,
					classification=CLASS_SAFE,
					facility=facility_name,
					repayment=repayment_name,
					reference_field="docstatus",
					journal_entry=je_name,
					je_docstatus=2,
					current_status="Submitted",
					expected_status="Cancelled",
					recommended_action="Repayment still submitted while JE cancelled",
				)
			)
		elif cint(je.docstatus) == 0:
			issues.append(
				_issue(
					category=CATEGORY_C,
					classification=CLASS_MANUAL,
					facility=facility_name,
					repayment=repayment_name,
					reference_field="journal_entry",
					journal_entry=je_name,
					je_docstatus=0,
					recommended_action="Submitted repayment references Draft JE",
				)
			)
		elif je.company and facility_doc and je.company != facility_doc.company:
			issues.append(
				_issue(
					category=CATEGORY_C,
					classification=CLASS_MANUAL,
					facility=facility_name,
					repayment=repayment_name,
					reference_field="journal_entry",
					journal_entry=je_name,
					je_docstatus=1,
					recommended_action="Repayment JE company mismatch",
					details={"je_company": je.company, "facility_company": facility_doc.company},
				)
			)

	if cint(rep.docstatus) == 2 and je_name:
		# Cancelled repayment should not keep active JE pointer (on_cancel clears it)
		if je and cint(je.docstatus) == 1:
			issues.append(
				_issue(
					category=CATEGORY_F,
					classification=CLASS_MANUAL,
					facility=facility_name,
					repayment=repayment_name,
					reference_field="journal_entry",
					journal_entry=je_name,
					je_docstatus=1,
					recommended_action="Cancelled repayment still points at submitted JE",
				)
			)
		elif je and cint(je.docstatus) == 2:
			# Stale pointer after cancel — safe clear
			issues.append(
				_issue(
					category=CATEGORY_B,
					classification=CLASS_SAFE,
					facility=facility_name,
					repayment=repayment_name,
					reference_field="journal_entry",
					journal_entry=je_name,
					je_docstatus=2,
					recommended_action="Clear stale journal_entry on cancelled repayment",
				)
			)

	return issues


def run_global_scan(*, dry_run: bool = True, facility: str | None = None) -> dict[str, Any]:
	"""Scan all (or one) Facilities and repayments. Always read-only."""
	_ = dry_run  # scan never mutates; parameter kept for CLI symmetry
	started = now_datetime()
	facility_names = [facility] if facility else frappe.get_all("Facility", pluck="name", order_by="name")
	issues: list[dict[str, Any]] = []
	for name in facility_names:
		fac_issues = scan_facility(name)
		# Drop pure OK noise from global totals unless sole result
		for iss in fac_issues:
			if iss["classification"] == CLASS_OK and len(fac_issues) > 1:
				continue
			if iss["classification"] == CLASS_OK and not facility:
				continue
			issues.append(iss)

	# Also scan repayments not covered (orphans without facility) — skip if facility filter
	if not facility:
		orphan_reps = frappe.db.sql(
			"""
			SELECT r.name FROM `tabFacility Repayment` r
			LEFT JOIN `tabFacility` f ON f.name = r.facility
			WHERE f.name IS NULL
			""",
			pluck=True,
		)
		for rn in orphan_reps or []:
			issues.extend(scan_repayment(rn))

	totals = _summarize(issues, facility_count=len(facility_names))
	report = {
		"dry_run": True,
		"started": str(started),
		"finished": str(now_datetime()),
		"totals": totals,
		"issues": issues,
	}
	_log(f"[facility_je_reconciliation] scan complete: {json.dumps(totals, default=str)}")
	return report


def _summarize(issues: list[dict[str, Any]], *, facility_count: int) -> dict[str, Any]:
	by_cat: dict[str, int] = {}
	by_class: dict[str, int] = {}
	facilities = set()
	repayments = set()
	for i in issues:
		by_cat[i["category"]] = by_cat.get(i["category"], 0) + 1
		by_class[i["classification"]] = by_class.get(i["classification"], 0) + 1
		if i.get("facility"):
			facilities.add(i["facility"])
		if i.get("repayment"):
			repayments.add(i["repayment"])
	return {
		"facility_count_scanned": facility_count,
		"issue_count": len(issues),
		"facilities_with_issues": len(facilities),
		"repayments_with_issues": len(repayments),
		"by_category": by_cat,
		"by_classification": by_class,
		"cancelled_receipt_refs": by_cat.get(CATEGORY_A, 0),
		"cancelled_repayment_refs": by_cat.get(CATEGORY_B, 0),
		"invalid_refs": by_cat.get(CATEGORY_C, 0),
		"status_inconsistencies": by_cat.get(CATEGORY_D, 0),
		"amount_inconsistencies": by_cat.get(CATEGORY_E, 0),
		"replacement_conflicts": by_cat.get(CATEGORY_F, 0),
		"downstream_deps": by_cat.get(CATEGORY_G, 0),
		"schedule_inconsistencies": by_cat.get(CATEGORY_H, 0),
		"safe_auto_repair": by_class.get(CLASS_SAFE, 0),
		"manual_review": by_class.get(CLASS_MANUAL, 0),
		"blocked": by_class.get(CLASS_BLOCKED, 0),
		"no_change": by_class.get(CLASS_OK, 0),
	}


def _snapshot_facility(name: str) -> dict[str, Any]:
	row = frappe.db.get_value(
		"Facility",
		name,
		[
			"name",
			"status",
			"receipt_journal_entry",
			"received_amount",
			"principal_amount",
			"profit_amount",
			"paid_principal_amount",
			"paid_profit_amount",
			"paid_penalty_amount",
			"remaining_principal_amount",
			"remaining_profit_amount",
			"remaining_total_amount",
			"is_opening_facility",
			"company",
		],
		as_dict=True,
	)
	return dict(row) if row else {}


def _snapshot_repayment(name: str) -> dict[str, Any]:
	row = frappe.db.get_value(
		"Facility Repayment",
		name,
		[
			"name",
			"facility",
			"docstatus",
			"journal_entry",
			"principal_amount",
			"profit_amount",
			"penalty_amount",
			"total_payment_amount",
		],
		as_dict=True,
	)
	return dict(row) if row else {}


def repair_facility(
	facility: str,
	*,
	dry_run: bool = True,
	confirm: bool = False,
	allow_relink: bool = True,
) -> dict[str, Any]:
	"""Repair a single Facility's cancelled/missing receipt JE reference.

	Requires confirm=True when dry_run=False.
	"""
	if not dry_run and not confirm:
		frappe.throw("Production/live repair requires confirm=True (and dry_run=False).")

	before = _snapshot_facility(facility)
	issues = [i for i in scan_facility(facility) if i["classification"] != CLASS_OK]
	actions: list[dict[str, Any]] = []

	blocked = [i for i in issues if i["classification"] == CLASS_BLOCKED]
	if blocked:
		return {
			"facility": facility,
			"dry_run": dry_run,
			"repaired": False,
			"reason": "BLOCKED_BY_DEPENDENCIES",
			"before": before,
			"after": before,
			"blocked_issues": blocked,
			"actions": [],
		}

	safe = [i for i in issues if i["classification"] == CLASS_SAFE]
	manual = [i for i in issues if i["classification"] == CLASS_MANUAL]
	# Prefer relink (F) over clear (A)
	relink = next(
		(i for i in safe if i["category"] == CATEGORY_F and (i.get("details") or {}).get("replacement_je")),
		None,
	)
	clear = next((i for i in safe if i["category"] in (CATEGORY_A, CATEGORY_C)), None)
	amount_fix = next((i for i in safe if i["category"] == CATEGORY_E), None)

	updates: dict[str, Any] = {}
	if relink and allow_relink:
		replacement = relink["details"]["replacement_je"]
		updates["receipt_journal_entry"] = replacement
		updates["status"] = "Active"
		updates["received_amount"] = flt(before.get("principal_amount") or frappe.db.get_value("Facility", facility, "principal_amount"))
		actions.append({"action": "relink_receipt_je", "to": replacement})
	elif clear and clear["category"] in (CATEGORY_A, CATEGORY_C):
		# Only clear when recommended action is clear (not company mismatch manuals)
		if clear["classification"] == CLASS_SAFE:
			updates["receipt_journal_entry"] = None
			updates["received_amount"] = 0
			if not cint(before.get("is_opening_facility")):
				updates["status"] = "Draft"
			actions.append({"action": "clear_cancelled_receipt_je", "from": before.get("receipt_journal_entry")})

	if dry_run:
		return {
			"facility": facility,
			"dry_run": True,
			"repaired": False,
			"planned_updates": updates,
			"actions": actions,
			"before": before,
			"after": {**before, **updates} if updates else before,
			"safe_issues": safe,
			"manual_issues": manual,
			"blocked_issues": blocked,
		}

	if not updates and not amount_fix:
		# Still refresh amounts if only E
		if amount_fix:
			refresh_facility_paid_fields(facility)
			actions.append({"action": "refresh_paid_fields"})
		after = _snapshot_facility(facility)
		return {
			"facility": facility,
			"dry_run": False,
			"repaired": bool(actions),
			"actions": actions,
			"before": before,
			"after": after,
			"manual_issues": manual,
		}

	frappe.flags[FLAG_IN_RECEIPT_REPAIR] = True
	try:
		if updates:
			# Narrow db_set — avoid full validate which may reject Active without JE mid-flight
			frappe.db.set_value("Facility", facility, updates, update_modified=True)
			actions.append({"action": "db_set", "fields": list(updates.keys())})
		refresh_facility_paid_fields(facility)
		# After clear, received_amount must stay 0
		if updates.get("receipt_journal_entry") is None and "received_amount" in updates:
			frappe.db.set_value("Facility", facility, "received_amount", 0, update_modified=False)
		actions.append({"action": "refresh_paid_fields"})
	finally:
		frappe.flags[FLAG_IN_RECEIPT_REPAIR] = False

	after = _snapshot_facility(facility)
	_log(f"[facility_je_reconciliation] repaired facility {facility}: {before} -> {after}")
	return {
		"facility": facility,
		"dry_run": False,
		"repaired": True,
		"actions": actions,
		"before": before,
		"after": after,
		"manual_issues": manual,
	}


def repair_repayment(
	repayment: str,
	*,
	dry_run: bool = True,
	confirm: bool = False,
) -> dict[str, Any]:
	"""Repair a Facility Repayment that still references a cancelled JE.

	Safe path: cancel the submitted repayment (idempotent w.r.t. already-cancelled JE)
	so paid totals exclude it. Does not create replacement JEs.
	"""
	if not dry_run and not confirm:
		frappe.throw("Live repayment repair requires confirm=True (and dry_run=False).")

	before = _snapshot_repayment(repayment)
	issues = scan_repayment(repayment)
	safe = [i for i in issues if i["classification"] == CLASS_SAFE]
	blocked = [i for i in issues if i["classification"] == CLASS_BLOCKED]
	manual = [i for i in issues if i["classification"] == CLASS_MANUAL]

	if blocked:
		return {
			"repayment": repayment,
			"dry_run": dry_run,
			"repaired": False,
			"reason": "BLOCKED_BY_DEPENDENCIES",
			"before": before,
			"after": before,
			"blocked_issues": blocked,
		}

	needs_cancel = any(
		i["category"] in (CATEGORY_B, CATEGORY_D) and before.get("docstatus") == 1 for i in safe
	)
	needs_clear_pointer = any(
		i["category"] == CATEGORY_B and before.get("docstatus") == 2 for i in safe
	)

	if dry_run:
		return {
			"repayment": repayment,
			"dry_run": True,
			"repaired": False,
			"planned": {
				"cancel_repayment": needs_cancel,
				"clear_journal_entry": needs_clear_pointer or needs_cancel,
			},
			"before": before,
			"safe_issues": safe,
			"manual_issues": manual,
		}

	actions: list[dict[str, Any]] = []
	if needs_cancel:
		doc = frappe.get_doc("Facility Repayment", repayment)
		if doc.docstatus == 1:
			# on_cancel will try to cancel JE (already cancelled → no-op) and clear journal_entry
			doc.cancel()
			actions.append({"action": "cancel_facility_repayment"})
	elif needs_clear_pointer:
		frappe.db.set_value("Facility Repayment", repayment, "journal_entry", None, update_modified=False)
		actions.append({"action": "clear_journal_entry"})

	facility = before.get("facility")
	if facility:
		refresh_facility_paid_fields(facility)
		actions.append({"action": "refresh_facility_paid_fields", "facility": facility})

	after = _snapshot_repayment(repayment)
	return {
		"repayment": repayment,
		"dry_run": False,
		"repaired": bool(actions),
		"actions": actions,
		"before": before,
		"after": after,
		"manual_issues": manual,
	}


def repair_batch(
	facilities: list[str] | None = None,
	*,
	dry_run: bool = True,
	confirm: bool = False,
	only_safe: bool = True,
) -> dict[str, Any]:
	"""Repair multiple facilities. Uses a savepoint per facility."""
	if not dry_run and not confirm:
		frappe.throw("Batch live repair requires confirm=True.")

	if not facilities:
		report = run_global_scan(dry_run=True)
		facilities = sorted(
			{
				i["facility"]
				for i in report["issues"]
				if i.get("facility") and i["classification"] == CLASS_SAFE
			}
		)

	results = []
	for name in facilities:
		if only_safe:
			iss = scan_facility(name)
			if any(i["classification"] == CLASS_BLOCKED for i in iss):
				results.append({"facility": name, "skipped": True, "reason": CLASS_BLOCKED})
				continue
			if not any(i["classification"] == CLASS_SAFE for i in iss):
				results.append({"facility": name, "skipped": True, "reason": "no_safe_issues"})
				continue
		try:
			if not dry_run:
				frappe.db.savepoint(f"fac_je_repair_{name.replace('-', '_')[:40]}")
			res = repair_facility(name, dry_run=dry_run, confirm=confirm)
			results.append(res)
		except Exception as exc:
			if not dry_run:
				frappe.db.rollback(save_point=f"fac_je_repair_{name.replace('-', '_')[:40]}")
			results.append({"facility": name, "error": str(exc), "repaired": False})
			_log(f"[facility_je_reconciliation] batch error {name}: {exc}")

	return {
		"dry_run": dry_run,
		"count": len(results),
		"results": results,
	}


def verify_facility(facility: str) -> dict[str, Any]:
	"""Post-repair verification for one Facility."""
	issues = scan_facility(facility)
	actionable = [i for i in issues if i["classification"] != CLASS_OK]
	fac = _snapshot_facility(facility)
	receipt = fac.get("receipt_journal_entry")
	je = _je_meta(receipt) if receipt else None
	eligible = (
		not cint(fac.get("is_opening_facility"))
		and not receipt
		and fac.get("status") != "Closed"
	)
	return {
		"facility": facility,
		"snapshot": fac,
		"receipt_je_docstatus": je.docstatus if je else None,
		"replacement_receipt_eligible": bool(eligible),
		"actionable_issues": actionable,
		"ok": not actionable
		or all(i["classification"] == CLASS_MANUAL for i in actionable),
	}


def get_valid_receipt_journal_entry(facility_name: str) -> str | None:
	"""Return receipt JE only if it exists and is submitted."""
	je_name = frappe.db.get_value("Facility", facility_name, "receipt_journal_entry")
	if not je_name:
		return None
	je = _je_meta(je_name)
	if je and cint(je.docstatus) == 1:
		return je_name
	return None


def facility_has_valid_receipt(facility_name: str) -> bool:
	return bool(get_valid_receipt_journal_entry(facility_name))
