# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""READ-ONLY global Facility duplicate audit for Finance review.

Does not modify, delete, cancel, or merge any Facility or Journal Entry.

Usage::

	bench --site development.localhost execute \\
	  erpnext_extensions.facility_management.facility_duplicate_audit.run_duplicate_audit \\
	  --kwargs '{"output_dir": "/path/to/out"}'
"""

from __future__ import annotations

import csv
import os
import re
from collections import defaultdict
from datetime import date
from difflib import SequenceMatcher
from typing import Any

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.facility_management.facility_identity import normalize_facility_number

AUDIT_FIELDS = [
	"name",
	"facility_name",
	"company",
	"bank",
	"status",
	"principal_amount",
	"profit_amount",
	"contract_date",
	"receive_date",
	"bank_account",
	"receipt_journal_entry",
	"received_amount",
	"is_opening_facility",
]


def _je_status(je_name: str | None) -> str:
	if not je_name:
		return ""
	ds = frappe.db.get_value("Journal Entry", je_name, "docstatus")
	if ds is None:
		return "Missing"
	return {0: "Draft", 1: "Submitted", 2: "Cancelled"}.get(cint(ds), str(ds))


def _repayment_stats(facility: str) -> tuple[int, float]:
	rows = frappe.get_all(
		"Facility Repayment",
		filters={"facility": facility, "docstatus": 1},
		fields=["total_payment_amount"],
	)
	return len(rows), sum(flt(r.total_payment_amount) for r in rows)


_TEST_NAME_RE = re.compile(
	r"^(INT-|DUP|DUPB-|DUPC-|UPDA-|UPDB-|CONC-|COPY-|WS-|JE Rec |DISTINCT-|771004835|888004835|"
	r"661004835|662004835|000004835-404-50-)",
	re.IGNORECASE,
)


def _is_test_artifact(facility_name: str | None) -> bool:
	"""Skip ephemeral regression-test Facilities when auditing restored Dev data."""
	return bool(_TEST_NAME_RE.match((facility_name or "").strip()))


def _load_facilities(*, exclude_test_artifacts: bool = True) -> list[dict[str, Any]]:
	rows = frappe.get_all("Facility", fields=AUDIT_FIELDS, order_by="company, bank, name", limit=50000)
	out = []
	for r in rows:
		if exclude_test_artifacts and _is_test_artifact(r.facility_name):
			continue
		norm = normalize_facility_number(r.facility_name)
		rep_count, paid = _repayment_stats(r.name)
		out.append(
			{
				**dict(r),
				"normalized_number": norm,
				"repayment_count": rep_count,
				"paid_amount": paid,
				"je_status": _je_status(r.receipt_journal_entry),
			}
		)
	return out


def _exact_groups(facilities: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
	buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
	for f in facilities:
		if not f["normalized_number"] or not f.get("company") or not f.get("bank"):
			continue
		key = (f["company"], f["bank"], f["normalized_number"])
		buckets[key].append(f)
	return [v for v in buckets.values() if len(v) >= 2]


def _similarity(a: str, b: str) -> float:
	if not a or not b:
		return 0.0
	return SequenceMatcher(None, a, b).ratio()


def _digits_only(s: str) -> str:
	return "".join(ch for ch in (s or "") if ch.isdigit())


CLASS_EXACT = "EXACT_DUPLICATE"
CLASS_HIGH = "HIGH_CONFIDENCE_POTENTIAL"
CLASS_REVIEW = "NEEDS_FINANCE_REVIEW"


def _potential_groups(facilities: list[dict[str, Any]], exact: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
	"""Rank near-duplicates using multiple signals (never amount-only).

	Primary identity signal required (number similarity / typo / shared JE).
	Secondary amount/date signals only reinforce. Low-confidence noise is dropped
	unless reinforced by same principal+profit+contract or Draft+Active.
	"""
	exact_names = {f["name"] for g in exact for f in g}
	by_cb: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
	for f in facilities:
		if f["name"] in exact_names:
			continue
		if not f.get("company") or not f.get("bank"):
			continue
		by_cb[(f["company"], f["bank"])].append(f)

	groups: list[dict[str, Any]] = []
	seen_pairs: set[frozenset[str]] = set()

	for (_company, _bank), rows in by_cb.items():
		n = len(rows)
		for i in range(n):
			for j in range(i + 1, n):
				a, b = rows[i], rows[j]
				pair = frozenset({a["name"], b["name"]})
				if pair in seen_pairs:
					continue
				na, nb = a["normalized_number"], b["normalized_number"]
				if not na or not nb or na == nb:
					continue

				sim = _similarity(na, nb)
				da, db = _digits_only(na), _digits_only(nb)
				digit_sim = _similarity(da, db) if da and db else 0.0

				reasons: list[str] = []
				score = 0.0
				primary = False
				strong_number = False

				if sim >= 0.95 or digit_sim >= 0.97:
					score += 50
					reasons.append(f"Near-identical facility number (sim={sim:.2f})")
					primary = True
					strong_number = True
				elif sim >= 0.90 or digit_sim >= 0.93:
					score += 35
					reasons.append(f"Similar facility number (sim={sim:.2f})")
					primary = True
					strong_number = True
				elif (
					da
					and db
					and min(len(da), len(db)) >= 8
					and (da in db or db in da)
					and abs(len(da) - len(db)) <= 3
				):
					score += 40
					reasons.append("Facility number substring / possible transcription error")
					primary = True
					strong_number = True
				elif da and db and len(da) >= 12 and len(db) >= 12:
					if digit_sim >= 0.88 and abs(len(da) - len(db)) <= 2:
						score += 32
						reasons.append(f"Possible typo in facility number digits (sim={digit_sim:.2f})")
						primary = True
						strong_number = True

				same_je = bool(
					a.get("receipt_journal_entry")
					and a.get("receipt_journal_entry") == b.get("receipt_journal_entry")
				)
				if same_je:
					score += 45
					reasons.append("Same receipt Journal Entry")
					primary = True

				if not primary:
					continue

				same_principal = (
					abs(flt(a["principal_amount"]) - flt(b["principal_amount"])) < 0.01
					and flt(a["principal_amount"]) > 0
				)
				same_profit = (
					abs(flt(a["profit_amount"]) - flt(b["profit_amount"])) < 0.01
					and flt(a["profit_amount"]) > 0
				)
				same_contract = bool(a.get("contract_date") and a.get("contract_date") == b.get("contract_date"))
				same_receive = bool(a.get("receive_date") and a.get("receive_date") == b.get("receive_date"))
				same_bank_acct = bool(a.get("bank_account") and a.get("bank_account") == b.get("bank_account"))
				draft_active = {a.get("status"), b.get("status")} == {"Draft", "Active"}

				reinforce = 0
				if same_principal:
					reinforce += 12
					reasons.append("Same principal amount")
				if same_profit:
					reinforce += 10
					reasons.append("Same profit amount")
				if same_contract:
					reinforce += 10
					reasons.append("Same contract date")
				if same_receive:
					reinforce += 8
					reasons.append("Same receive date")
				if same_bank_acct:
					reinforce += 6
					reasons.append("Same bank account")
				if draft_active:
					reinforce += 15
					reasons.append("Draft + Active pair")
				score += reinforce

				# Drop weak number-only noise (no reinforcement, not shared JE)
				if not same_je and not strong_number:
					continue
				if not same_je and reinforce < 12 and digit_sim < 0.93:
					continue
				if score < 50:
					continue

				seen_pairs.add(pair)
				if same_je or (strong_number and reinforce >= 20) or score >= 75:
					confidence = CLASS_HIGH
					similarity = "High"
				else:
					confidence = CLASS_REVIEW
					similarity = "Medium" if score >= 60 else "Low"
				groups.append(
					{
						"members": [a, b],
						"similarity": similarity,
						"confidence": confidence,
						"score": score,
						"reasons": reasons,
					}
				)

	groups.sort(key=lambda g: -g["score"])
	return groups


def _suggest_owner(members: list[dict[str, Any]]) -> str:
	"""Heuristic only — Finance decides. Prefer Active with valid JE + repayments."""
	def rank(m: dict[str, Any]) -> tuple:
		active = 1 if m.get("status") == "Active" else 0
		je_ok = 1 if m.get("je_status") == "Submitted" else 0
		reps = cint(m.get("repayment_count"))
		return (active, je_ok, reps, m.get("name") or "")

	best = sorted(members, key=rank, reverse=True)[0]
	return best["name"]


def _recommended_action(members: list[dict[str, Any]], *, exact: bool) -> str:
	statuses = {m.get("status") for m in members}
	has_draft = "Draft" in statuses
	has_active = "Active" in statuses
	if exact and has_draft and has_active:
		return "Review — likely keep Active; Draft may be duplicate"
	if exact:
		return "Review — exact duplicate group"
	return "Potential duplicate — verify manually"


def build_audit_rows(*, exclude_test_artifacts: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
	facilities = _load_facilities(exclude_test_artifacts=exclude_test_artifacts)
	exact = _exact_groups(facilities)
	potential = _potential_groups(facilities, exact)

	rows: list[dict[str, Any]] = []
	gid = 0
	for group in exact:
		gid += 1
		group_id = f"EXACT-{gid:04d}"
		owner = _suggest_owner(group)
		action = _recommended_action(group, exact=True)
		for m in sorted(group, key=lambda x: x["name"]):
			rows.append(
				_row(
					group_id,
					m,
					"Exact",
					CLASS_EXACT,
					"Exact match: company+bank+normalized facility_name",
					owner,
					action,
				)
			)

	for group in potential:
		gid += 1
		confidence = group.get("confidence") or CLASS_REVIEW
		prefix = "HIGH" if confidence == CLASS_HIGH else "REVIEW"
		group_id = f"{prefix}-{gid:04d}"
		owner = _suggest_owner(group["members"])
		action = _recommended_action(group["members"], exact=False)
		reason = "; ".join(group["reasons"])
		for m in sorted(group["members"], key=lambda x: x["name"]):
			rows.append(
				_row(group_id, m, group["similarity"], confidence, reason, owner, action)
			)

	high = [g for g in potential if g.get("confidence") == CLASS_HIGH]
	review = [g for g in potential if g.get("confidence") != CLASS_HIGH]
	summary = {
		"facilities_scanned": len(facilities),
		"exact_groups": len(exact),
		"exact_facilities": sum(len(g) for g in exact),
		"high_confidence_groups": len(high),
		"needs_finance_review_groups": len(review),
		"potential_groups": len(potential),
		"potential_facilities": sum(len(g["members"]) for g in potential),
		"report_rows": len(rows),
	}
	return rows, summary


def _row(
	group_id: str,
	m: dict[str, Any],
	similarity: str,
	confidence: str,
	reason: str,
	owner: str,
	action: str,
) -> dict[str, Any]:
	return {
		"group_id": group_id,
		"facility_id": m["name"],
		"company": m.get("company") or "",
		"bank": m.get("bank") or "",
		"facility_number": m.get("facility_name") or "",
		"normalized_number": m.get("normalized_number") or "",
		"principal": flt(m.get("principal_amount")),
		"profit": flt(m.get("profit_amount")),
		"contract_date": str(m.get("contract_date") or ""),
		"status": m.get("status") or "",
		"receipt_je": m.get("receipt_journal_entry") or "",
		"je_status": m.get("je_status") or "",
		"repayment_count": cint(m.get("repayment_count")),
		"paid_amount": flt(m.get("paid_amount")),
		"similarity": similarity,
		"confidence": confidence,
		"reason": reason,
		"suggested_owner": owner,
		"recommended_action": action,
		"finance_decision": "",
		"finance_notes": "",
		"is_opening": cint(m.get("is_opening_facility")),
	}


CSV_COLUMNS = [
	"group_id",
	"facility_id",
	"company",
	"bank",
	"facility_number",
	"normalized_number",
	"principal",
	"profit",
	"contract_date",
	"status",
	"receipt_je",
	"je_status",
	"repayment_count",
	"paid_amount",
	"similarity",
	"confidence",
	"reason",
	"suggested_owner",
	"recommended_action",
	"finance_decision",
	"finance_notes",
]


def write_csv(rows: list[dict[str, Any]], path: str) -> None:
	os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
	with open(path, "w", newline="", encoding="utf-8-sig") as fh:
		writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
		writer.writeheader()
		for r in rows:
			writer.writerow({k: r.get(k, "") for k in CSV_COLUMNS})


def write_markdown(rows: list[dict[str, Any]], summary: dict[str, Any], path: str) -> None:
	os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
	lines = [
		"# Facility Duplicate Audit — Finance Review",
		"",
		f"**Date:** {date.today().isoformat()}",
		f"**Site (READ-ONLY scan):** `{frappe.local.site}`",
		"",
		"## Important",
		"",
		"- This report is **READ-ONLY**. No Facility, Journal Entry, repayment, schedule, or GL was modified.",
		"- The agent does **not** decide deletions. Finance completes `Finance Decision` / `Finance Notes`.",
		"- Suggested Owner is a heuristic only.",
		"",
		"## Summary",
		"",
		"| Metric | Value |",
		"|---|---|",
		f"| Facilities scanned | {summary['facilities_scanned']} |",
		f"| EXACT_DUPLICATE groups | {summary['exact_groups']} |",
		f"| Facilities in exact groups | {summary['exact_facilities']} |",
		f"| HIGH_CONFIDENCE_POTENTIAL groups | {summary.get('high_confidence_groups', 0)} |",
		f"| NEEDS_FINANCE_REVIEW groups | {summary.get('needs_finance_review_groups', 0)} |",
		f"| Potential groups (total) | {summary['potential_groups']} |",
		f"| Report rows | {summary['report_rows']} |",
		"",
		"## Legend",
		"",
		"| Confidence | Meaning |",
		"|---|---|",
		"| EXACT_DUPLICATE | Same Company + Bank + normalized facility number |",
		"| HIGH_CONFIDENCE_POTENTIAL | Strong number/JE match reinforced by contract signals |",
		"| NEEDS_FINANCE_REVIEW | Suspicious but not conclusive — verify manually |",
		"",
		"## Groups",
		"",
	]

	current = None
	for r in rows:
		if r["group_id"] != current:
			current = r["group_id"]
			lines.append(f"### {current} — {r.get('confidence') or r['similarity']}")
			lines.append("")
			lines.append(f"**Confidence:** `{r.get('confidence') or ''}`")
			lines.append("")
			lines.append(f"**Reason:** {r['reason']}")
			lines.append("")
			lines.append(f"**Suggested owner (heuristic):** `{r['suggested_owner']}`")
			lines.append("")
			lines.append(f"**Recommended action:** {r['recommended_action']}")
			lines.append("")
			lines.append(
				"| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |"
			)
			lines.append("|---|---|---|---|---|---|---|---|---|")
		lines.append(
			f"| `{r['facility_id']}` | {r['status']} | `{r['facility_number']}` | "
			f"{r['principal']:,.0f} | {r['profit']:,.0f} | `{r['receipt_je'] or '—'}` | "
			f"{r['je_status'] or '—'} | {r['repayment_count']} | {r['paid_amount']:,.0f} |"
		)
		# blank line after group handled when group changes; add at end of each row is noisy
	# Close last group spacing
	lines.append("")
	lines.append("## Finance decision columns")
	lines.append("")
	lines.append("Use the CSV companion file to fill `finance_decision` and `finance_notes`.")
	lines.append("")
	lines.append("---")
	lines.append("")
	lines.append("*Generated by `facility_duplicate_audit.run_duplicate_audit` — no deletions performed.*")
	lines.append("")

	with open(path, "w", encoding="utf-8") as fh:
		fh.write("\n".join(lines))


def run_duplicate_audit(
	output_dir: str | None = None,
	*,
	exclude_test_artifacts: bool = True,
) -> dict[str, Any]:
	"""Generate Markdown + CSV Finance review reports. READ-ONLY."""
	rows, summary = build_audit_rows(exclude_test_artifacts=exclude_test_artifacts)
	if not output_dir:
		output_dir = frappe.get_site_path("private", "files", "facility_duplicate_audit")
	os.makedirs(output_dir, exist_ok=True)
	stamp = date.today().strftime("%Y%m%d")
	md_path = os.path.join(output_dir, f"facility_duplicate_audit_{stamp}.md")
	csv_path = os.path.join(output_dir, f"facility_duplicate_audit_{stamp}.csv")
	write_markdown(rows, summary, md_path)
	write_csv(rows, csv_path)
	return {
		**summary,
		"markdown": md_path,
		"csv": csv_path,
		"read_only": True,
		"site": frappe.local.site,
	}
