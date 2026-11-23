# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Company-wide MCA valuation serialization gate.

Business rule: only one unresolved MCA valuation chain may be active per company.
A subsequent MCA submit is blocked until the prior MCA reaches genuine Completed
(or VoidedByStockEntryCancel) after Core RIV completion — not merely RQ Job OK.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint

from erpnext_extensions.manufacturing_cost_adjustment import constants as C

# Statuses that block the next MCA submit for the same company.
BLOCKING_PROCESSING_STATUSES = frozenset(
	{
		C.PS_QUEUED,
		C.PS_APPENDING,
		C.PS_ALLOCATING,
		C.PS_REBUILDING_SLE,
		C.PS_REBUILDING_GL,
		C.PS_VALIDATING,
		C.PS_PENDING_RIV,
		C.PS_RECONCILING,
		C.PS_FAILED,
	}
)

ACTIVE_RIV_STATUSES = ("Queued", "In Progress")


def _company_lock_name(company: str) -> str:
	return f"mca_serial:{company}"


def _acquire_db_lock(company: str, *, timeout: int = 30) -> None:
	"""MariaDB named lock — serializes concurrent Submit API requests for a company."""
	got = cint(frappe.db.sql("SELECT GET_LOCK(%s, %s)", (_company_lock_name(company), timeout))[0][0])
	if not got:
		frappe.throw(
			_(
				"Could not acquire MCA serialization lock for Company {0}. "
				"Another submit may be in progress — retry shortly."
			).format(company),
			frappe.ValidationError,
		)

	def _release():
		try:
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (_company_lock_name(company),))
		except Exception:
			pass

	frappe.db.after_commit.add(_release)
	frappe.db.after_rollback.add(_release)


def _riv_summary(riv_text: str | None) -> list[dict[str, Any]]:
	from erpnext_extensions.manufacturing_cost_adjustment.repost_engine import _parse_riv_list

	out = []
	for riv in _parse_riv_list(riv_text or ""):
		if not frappe.db.exists("Repost Item Valuation", riv):
			out.append({"riv": riv, "exists": False, "status": None})
			continue
		st = frappe.db.get_value("Repost Item Valuation", riv, "status")
		out.append({"riv": riv, "exists": True, "status": st})
	return out


def find_blocking_mcas(company: str, *, exclude_name: str | None = None) -> list[dict[str, Any]]:
	"""Return submitted MCAs whose valuation chain is not yet successfully complete."""
	if not company:
		return []
	filters: dict[str, Any] = {
		"company": company,
		"docstatus": 1,
		"processing_status": ("in", list(BLOCKING_PROCESSING_STATUSES)),
	}
	if exclude_name:
		filters["name"] = ("!=", exclude_name)
	rows = frappe.get_all(
		C.MCA_DOCTYPE,
		filters=filters,
		fields=["name", "processing_status", "stock_entry", "riv_voucher_nos", "checkpoint"],
		order_by="modified desc",
		limit_page_length=50,
	)
	out = []
	for r in rows:
		out.append(
			{
				"mca": r.name,
				"processing_status": r.processing_status,
				"stock_entry": r.stock_entry,
				"checkpoint": r.checkpoint,
				"rivs": _riv_summary(r.riv_voucher_nos),
			}
		)
	return out


def assert_mca_serialization_gate(company: str, *, exclude_name: str | None = None) -> None:
	"""Server-side gate: block MCA submit while any earlier company MCA is unresolved.

	Uses GET_LOCK so concurrent API submits cannot race past an empty blocker set.
	"""
	if not company:
		frappe.throw(_("Company is required for MCA serialization"), frappe.ValidationError)
	_acquire_db_lock(company)
	blockers = find_blocking_mcas(company, exclude_name=exclude_name)
	if not blockers:
		return
	b0 = blockers[0]
	riv_bits = []
	for rv in b0.get("rivs") or []:
		riv_bits.append(f"{rv.get('riv')}={rv.get('status') or 'missing'}")
	riv_txt = ", ".join(riv_bits) if riv_bits else _("(no RIV linked yet)")
	frappe.throw(
		_(
			"Cannot submit Manufacturing Cost Adjustment while {0} is unresolved "
			"(status={1}, Stock Entry={2}, RIV={3}). "
			"Wait until that MCA reaches Completed after genuine Core RIV completion, "
			"or resolve the Failed/orphaned RIV via the supported retry path first."
		).format(b0["mca"], b0["processing_status"], b0["stock_entry"], riv_txt),
		frappe.ValidationError,
	)


def get_serialization_status(company: str | None = None) -> dict[str, Any]:
	"""Read-only probe for operators / API health."""
	company = company or frappe.db.get_single_value("Global Defaults", "default_company")
	blockers = find_blocking_mcas(company) if company else []
	return {
		"ok": True,
		"company": company,
		"gate_clear": not blockers,
		"blockers": blockers,
		"blocking_statuses": sorted(BLOCKING_PROCESSING_STATUSES),
		"architecture": "option_a_serialized_transaction_riv",
		"note": (
			"Standard ERPNext Transaction RIV propagates future SLE chains including "
			"cross-warehouse transfer inbounds via dependant_sle_voucher_detail_no. "
			"HRC is reserved for multi-identity historical rebuild, not per-MCA submit."
		),
	}
