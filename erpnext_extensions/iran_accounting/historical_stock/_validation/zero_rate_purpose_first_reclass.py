# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only OLD→NEW Zero Rate reclassification (purpose-first semantics).

OLD: scrap/reject warehouse incoming zero → LEGITIMATE_SCRAP_ZERO / NO_ACTION.
NEW: purpose + authoritative source (Material Receipt → USER_REVIEW; never invent).

Must not write any business data. Run via:
  bench --site development.localhost execute \\
    erpnext_extensions.iran_accounting.historical_stock._validation.zero_rate_purpose_first_reclass.run
"""

from __future__ import annotations

import json
from collections import Counter

import frappe


class _WriteGuard:
	"""Fail loudly on any mutating SQL during reclassification."""

	def __init__(self):
		self.writes = []
		self._orig = None

	def __enter__(self):
		self._orig = frappe.db.sql

		def guarded(query, *args, **kwargs):
			q = (query or "").lstrip().lower()
			if q.startswith(
				(
					"insert",
					"update",
					"delete",
					"replace",
					"alter",
					"drop",
					"create",
					"truncate",
					"rename",
				)
			):
				self.writes.append(query[:200])
				raise RuntimeError(f"WRITE_DETECTED_DURING_RECLASS: {query[:120]}")
			return self._orig(query, *args, **kwargs)

		frappe.db.sql = guarded
		return self

	def __exit__(self, *exc):
		frappe.db.sql = self._orig
		return False


def _old_bucket(row: dict) -> str:
	"""Simulate pre-purpose-first primary scrap exemption (warehouse-first)."""
	from erpnext_extensions.iran_accounting.historical_stock.scrap_warehouse import (
		is_legitimate_scrap_zero_rate,
	)

	# Rebuild a minimal row shape for the legacy helper.
	legacy_row = {
		"t_warehouse": row.get("t_warehouse"),
		"s_warehouse": row.get("s_warehouse"),
		"basic_rate": row.get("current_rate") if row.get("current_rate") is not None else row.get("basic_rate"),
		"valuation_rate": row.get("valuation_rate")
		if row.get("valuation_rate") is not None
		else row.get("current_rate"),
		"company": row.get("company"),
	}
	if is_legitimate_scrap_zero_rate(legacy_row, company=row.get("company")):
		return "OLD_LEGITIMATE_SCRAP_ZERO_NO_ACTION"
	purpose = row.get("purpose") or ""
	if purpose == "Material Receipt":
		return "OLD_MATERIAL_RECEIPT_WAS_ACTIONABLE"
	if purpose in (
		"Material Transfer",
		"Material Transfer for Manufacture",
		"Send to Subcontractor",
	):
		return "OLD_TRANSFER_FAMILY"
	if purpose == "Manufacture":
		return "OLD_MANUFACTURE"
	return "OLD_OTHER_PURPOSE"


def _new_bucket(row: dict) -> str:
	kb = row.get("kpi_bucket") or ""
	if kb:
		return kb
	st = row.get("status") or ""
	if st == "MATERIAL_RECEIPT_ZERO_RATE_USER_REVIEW":
		return "MATERIAL_RECEIPT_ZERO_USER_REVIEW"
	if row.get("eligible") or st == "RECONSTRUCTABLE":
		return "ZERO_RATE_RECONSTRUCTABLE"
	if st in ("DEPENDENCY_REPAIR_REQUIRED", "VALUATION_POISON_DEPENDENCY"):
		return "ZERO_RATE_WAITING_UPSTREAM"
	if row.get("no_action_required"):
		return "NO_ACTION"
	return "ZERO_RATE_BLOCKED"


def run(company: str | None = None, limit: int = 8000) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows

	# Prefer campaign company when present.
	if not company:
		try:
			company = frappe.db.get_value(
				"Company", {"name": ["like", "%اسپاد%"]}, "name"
			) or frappe.db.get_value("Company", {}, "name", order_by="creation")
		except Exception:
			company = None

	with _WriteGuard() as guard:
		scan = scan_zero_rate_rows(company=company, limit=limit)
		rows = scan.get("rows") or []

		by_purpose = Counter()
		by_new = Counter()
		by_old = Counter()
		old_to_new = Counter()
		mr_rows = []
		transfer_rows = []
		tfm_rows = []
		mfg_rows = []
		other_purpose = Counter()

		for r in rows:
			purpose = r.get("purpose") or "Unknown"
			by_purpose[purpose] += 1
			nb = _new_bucket(r)
			ob = _old_bucket(r)
			by_new[nb] += 1
			by_old[ob] += 1
			old_to_new[f"{ob} → {nb}"] += 1

			if purpose == "Material Receipt":
				mr_rows.append(r)
			elif purpose == "Material Transfer":
				transfer_rows.append(r)
			elif purpose == "Material Transfer for Manufacture":
				tfm_rows.append(r)
			elif purpose == "Manufacture":
				mfg_rows.append(r)
			else:
				other_purpose[purpose] += 1

		def _status_counts(subset):
			c = Counter()
			for r in subset:
				c[r.get("status") or "?"] += 1
			return dict(c)

		def _exact_recon(subset):
			return sum(
				1
				for r in subset
				if r.get("status") == "RECONSTRUCTABLE"
				or r.get("kpi_bucket") == "ZERO_RATE_RECONSTRUCTABLE"
				or r.get("eligible")
			)

		report = {
			"verdict_candidate": "ZERO_RATE_SEMANTICS_READY_FOR_PHASE_4",
			"company": company,
			"data_writes": len(guard.writes),
			"write_samples": guard.writes[:5],
			"ZERO_RATE_RAW": int(scan.get("raw_count") or len(rows)),
			"MATERIAL_RECEIPT_ZERO_count": len(mr_rows),
			"MATERIAL_RECEIPT_ZERO_USER_REVIEW": sum(
				1
				for r in mr_rows
				if r.get("status") == "MATERIAL_RECEIPT_ZERO_RATE_USER_REVIEW"
				or r.get("kpi_bucket") == "MATERIAL_RECEIPT_ZERO_USER_REVIEW"
			),
			"TRANSFER_ZERO_count": len(transfer_rows),
			"TRANSFER_ZERO_EXACT_RECONSTRUCTABLE": _exact_recon(transfer_rows),
			"TRANSFER_ZERO_by_status": _status_counts(transfer_rows),
			"TFM_ZERO_count": len(tfm_rows),
			"TFM_ZERO_EXACT_RECONSTRUCTABLE": _exact_recon(tfm_rows),
			"TFM_ZERO_by_status": _status_counts(tfm_rows),
			"MANUFACTURE_ZERO_count": len(mfg_rows),
			"MANUFACTURE_ZERO_EXACT_RECONSTRUCTABLE": _exact_recon(mfg_rows),
			"MANUFACTURE_ZERO_by_status": _status_counts(mfg_rows),
			"OTHER_ZERO_by_purpose": dict(other_purpose),
			"by_kpi_bucket": dict(scan.get("by_kpi_bucket") or by_new),
			"by_purpose": dict(by_purpose),
			"by_status": dict(scan.get("by_status") or {}),
			"actionable_count": int(scan.get("actionable_count") or 0),
			"reconstructable_count": int(scan.get("reconstructable_count") or 0),
			"waiting_upstream_count": int(scan.get("waiting_upstream_count") or 0),
			"material_receipt_user_review_count": int(
				scan.get("material_receipt_user_review_count") or 0
			),
			"legitimate_scrap_zero_count_legacy_field": int(
				scan.get("legitimate_scrap_zero_count") or 0
			),
			"no_action_required_count": int(scan.get("no_action_required_count") or 0),
			"OLD_buckets": dict(by_old),
			"NEW_buckets": dict(by_new),
			"OLD_to_NEW_transitions": dict(old_to_new.most_common(40)),
			"newly_user_review": sum(
				1
				for r in rows
				if r.get("status") == "MATERIAL_RECEIPT_ZERO_RATE_USER_REVIEW"
				and _old_bucket(r) != "OLD_LEGITIMATE_SCRAP_ZERO_NO_ACTION"
			),
			"scrap_wh_context_still_flagged": sum(
				1 for r in rows if r.get("scrap_warehouse_context")
			),
			"old_scrap_no_action_now_reclassified": sum(
				1
				for r in rows
				if _old_bucket(r) == "OLD_LEGITIMATE_SCRAP_ZERO_NO_ACTION"
			),
			"old_scrap_now_user_review": sum(
				1
				for r in rows
				if _old_bucket(r) == "OLD_LEGITIMATE_SCRAP_ZERO_NO_ACTION"
				and (
					r.get("status") == "MATERIAL_RECEIPT_ZERO_RATE_USER_REVIEW"
					or r.get("kpi_bucket") == "MATERIAL_RECEIPT_ZERO_USER_REVIEW"
				)
			),
			"old_scrap_now_reconstructable": sum(
				1
				for r in rows
				if _old_bucket(r) == "OLD_LEGITIMATE_SCRAP_ZERO_NO_ACTION"
				and (r.get("eligible") or r.get("status") == "RECONSTRUCTABLE")
			),
			"tool_limit": sum(
				1 for r in rows if (r.get("kpi_bucket") or "") == "ZERO_RATE_TOOL_LIMIT"
			),
			"blocked": sum(
				1 for r in rows if (r.get("kpi_bucket") or "") == "ZERO_RATE_BLOCKED"
			),
			"downstream_symptom": sum(
				1
				for r in rows
				if (r.get("kpi_bucket") or "") == "ZERO_RATE_DOWNSTREAM_SYMPTOM"
				or "DOWNSTREAM" in str(r.get("status") or "")
			),
		}

		# Sanity gates for verdict
		writes_ok = report["data_writes"] == 0
		mr_not_invented = all(
			(r.get("proposed_rate") or 0) == 0
			for r in mr_rows
			if r.get("status") == "MATERIAL_RECEIPT_ZERO_RATE_USER_REVIEW"
		)
		scrap_legacy_zero = report["legitimate_scrap_zero_count_legacy_field"] == 0
		if writes_ok and mr_not_invented and scrap_legacy_zero:
			report["verdict"] = "ZERO_RATE_SEMANTICS_READY_FOR_PHASE_4"
		else:
			report["verdict"] = "ZERO_RATE_SEMANTICS_NEEDS_FURTHER_DEVELOPMENT"
			report["verdict_reasons"] = {
				"writes_ok": writes_ok,
				"mr_not_invented": mr_not_invented,
				"scrap_legacy_zero": scrap_legacy_zero,
			}

	print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
	return report
