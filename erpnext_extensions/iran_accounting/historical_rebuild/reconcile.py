# Copyright (c) 2026, ERPNext Extensions contributors
"""Post-campaign accounting reconciliation snapshots (read-only)."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, flt


def _company(campaign: str) -> str:
	return frappe.db.get_value("Historical Rebuild Campaign", campaign, "company")


def campaign_terminal_ok(campaign: str) -> dict[str, Any]:
	rows = frappe.db.sql(
		"""
		select item_status, count(*) c
		from `tabHistorical Rebuild Campaign Item`
		where campaign=%s
		group by item_status
		""",
		campaign,
		as_dict=True,
	)
	counts = {r.item_status: cint(r.c) for r in rows}
	total = sum(counts.values())
	pending = counts.get("Pending", 0) + counts.get("RIV Linked", 0) + counts.get("Processing", 0)
	failed = counts.get("Failed", 0) + counts.get("Blocked", 0)
	return {
		"total": total,
		"counts": counts,
		"pending": pending,
		"failed_or_blocked": failed,
		"accounted": total - pending,
		"ok": pending == 0 and failed == 0 and total > 0,
	}


def sle_bin_snapshot(company: str) -> dict[str, Any]:
	sle = frappe.db.sql(
		"""
		select count(*) c,
		       coalesce(sum(actual_qty),0) qty,
		       coalesce(sum(stock_value_difference),0) svd
		from `tabStock Ledger Entry`
		where is_cancelled=0 and company=%s
		""",
		company,
		as_dict=True,
	)[0]
	bins = frappe.db.sql(
		"""
		select count(*) c,
		       coalesce(sum(actual_qty),0) qty,
		       coalesce(sum(stock_value),0) value
		from `tabBin`
		""",
		as_dict=True,
	)[0]
	# Bin is global; filter via warehouse company when possible
	bins_co = frappe.db.sql(
		"""
		select count(*) c,
		       coalesce(sum(b.actual_qty),0) qty,
		       coalesce(sum(b.stock_value),0) value
		from `tabBin` b
		inner join `tabWarehouse` w on w.name=b.warehouse
		where w.company=%s
		""",
		company,
		as_dict=True,
	)[0]
	return {
		"sle_rows": cint(sle.c),
		"sle_qty": flt(sle.qty),
		"sle_svd": flt(sle.svd),
		"bin_rows": cint(bins_co.c),
		"bin_qty": flt(bins_co.qty),
		"bin_value": flt(bins_co.value),
	}


def sabe_snapshot(company: str) -> dict[str, Any]:
	if not frappe.db.exists("DocType", "Stock Account Balance Entry"):
		# Some sites use different naming — try Iran SABE table variants
		pass
	# Prefer SLE-derived warehouse stock value vs inventory GL
	return frappe.db.sql(
		"""
		select count(*) c,
		       coalesce(sum(qty_after_transaction),0) qty,
		       coalesce(sum(stock_value),0) value
		from `tabStock Ledger Entry`
		where is_cancelled=0 and company=%s
		  and name in (
		    select max(name) from `tabStock Ledger Entry`
		    where is_cancelled=0 and company=%s
		    group by item_code, warehouse
		  )
		""",
		(company, company),
		as_dict=True,
	)[0]


def gl_inventory_snapshot(company: str) -> dict[str, Any]:
	# Inventory accounts: typical stock accounts linked on warehouses / item defaults
	rows = frappe.db.sql(
		"""
		select coalesce(sum(debit),0) debit, coalesce(sum(credit),0) credit,
		       coalesce(sum(debit-credit),0) balance, count(*) entries
		from `tabGL Entry`
		where is_cancelled=0 and company=%s
		  and account in (
		    select distinct account from `tabWarehouse` where company=%s and account is not null
		    union
		    select distinct default_inventory_account from `tabItem Default`
		    where company=%s and default_inventory_account is not null
		  )
		""",
		(company, company, company),
		as_dict=True,
	)[0]
	return {
		"debit": flt(rows.debit),
		"credit": flt(rows.credit),
		"balance": flt(rows.balance),
		"entries": cint(rows.entries),
	}


def tva_snapshot(company: str) -> dict[str, Any]:
	if not frappe.db.exists("DocType", "Temporary Valuation Adjustment"):
		return {"exists": False}
	rows = frappe.db.sql(
		"""
		select coalesce(reconciliation_status, status, '') st, count(*) c
		from `tabTemporary Valuation Adjustment`
		where company=%s
		group by coalesce(reconciliation_status, status, '')
		""",
		company,
		as_dict=True,
	)
	return {"exists": True, "by_status": {r.st: cint(r.c) for r in rows}}


def riv_snapshot() -> dict[str, Any]:
	rows = frappe.db.sql(
		"""
		select status, count(*) c from `tabRepost Item Valuation`
		where docstatus=1 group by status
		""",
		as_dict=True,
	)
	return {r.status: cint(r.c) for r in rows}


def full_reconcile(campaign: str = "HRC-00001") -> dict[str, Any]:
	"""Read-only reconciliation pack for final report."""
	company = _company(campaign)
	term = campaign_terminal_ok(campaign)
	sle_bin = sle_bin_snapshot(company)
	gl = gl_inventory_snapshot(company)
	# Compare SLE stock_value (latest per identity) to inventory GL balance
	latest = sabe_snapshot(company)
	delta = flt(latest.get("value") if isinstance(latest, dict) else latest.value) - flt(gl["balance"])
	return {
		"campaign": campaign,
		"company": company,
		"terminal": term,
		"riv": riv_snapshot(),
		"sle_bin": sle_bin,
		"sle_latest_stock_value": flt(latest.get("value") if isinstance(latest, dict) else latest.value),
		"gl_inventory": gl,
		"sle_vs_gl_delta": delta,
		"tva": tva_snapshot(company),
		"verdict": (
			"OK"
			if term.get("pending") == 0 and term.get("failed_or_blocked") == 0 and abs(delta) < 1
			else "BLOCKERS"
		),
	}
