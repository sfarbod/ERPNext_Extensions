# Copyright (c) 2026, ERPNext Extensions contributors
"""TVA reconciliation after authoritative rebuild."""

from __future__ import annotations

from typing import Any

import frappe

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	TVA_FAILED,
	TVA_NA,
	TVA_PRESERVED,
	TVA_SUPERSEDED,
)


def reconcile_tva_for_voucher(voucher_type: str, voucher_no: str) -> dict[str, Any]:
	"""Mark Phase-1 TVA rows superseded when covering reconstruction succeeded.

	TRACE_ONLY → SUPERSEDED_BY_PHASE2 (no GL reverse).
	STOCK_ADJUSTMENT_GL → preserve row; mark SUPERSEDED only when no open residual
	need is asserted; never blindly reverse GL.
	"""
	if voucher_type != "Stock Entry":
		return {"status": TVA_NA, "updated": 0}

	if not frappe.db.exists("DocType", "Temporary Valuation Adjustment"):
		return {"status": TVA_NA, "updated": 0}

	rows = frappe.get_all(
		"Temporary Valuation Adjustment",
		filters={
			"stock_entry": voucher_no,
			"repair_status": ("in", ["OPEN", "SUBMITTED"]),
			"hotfix_version": ("in", ["5.5.45", "5.5.46"]),
		},
		fields=["name", "posting_mode", "accounting_category", "adjustment_delta"],
	)
	if not rows:
		return {"status": TVA_NA, "updated": 0}

	updated = 0
	preserved = 0
	try:
		for r in rows:
			mode = (r.posting_mode or "").upper()
			if mode in ("TRACE", "") or (r.accounting_category or "") == "TRACE_ONLY":
				frappe.db.set_value(
					"Temporary Valuation Adjustment",
					r.name,
					"repair_status",
					"SUPERSEDED_BY_PHASE2",
					update_modified=False,
				)
				updated += 1
			elif mode in ("STOCK_ADJUSTMENT_GL", "ROUND_OFF_GL"):
				# Preserve audit; mark superseded for Phase-2 discovery without GL reverse.
				# Valid SA amounts remain in GL; Phase-2 does not invent reversing entries here.
				frappe.db.set_value(
					"Temporary Valuation Adjustment",
					r.name,
					{
						"repair_status": "SUPERSEDED_BY_PHASE2",
						"remarks": (
							(frappe.db.get_value("Temporary Valuation Adjustment", r.name, "remarks") or "")
							+ " | Phase2: preserved Stock Adjustment GL; TVA superseded for discovery."
						)[:1000],
					},
					update_modified=False,
				)
				preserved += 1
				updated += 1
			else:
				frappe.db.set_value(
					"Temporary Valuation Adjustment",
					r.name,
					"repair_status",
					"SUPERSEDED_BY_PHASE2",
					update_modified=False,
				)
				updated += 1
		return {
			"status": TVA_PRESERVED if preserved and not updated - preserved else TVA_SUPERSEDED,
			"updated": updated,
			"preserved_sa": preserved,
		}
	except Exception as e:
		return {"status": TVA_FAILED, "updated": updated, "error": str(e)}
