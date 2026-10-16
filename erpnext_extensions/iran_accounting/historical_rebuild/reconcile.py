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
	"""Latest SLE stock_value per item+warehouse (posting chronology, not max name)."""
	return frappe.db.sql(
		"""
		select count(*) c,
		       coalesce(sum(qty_after_transaction),0) qty,
		       coalesce(sum(stock_value),0) value
		from (
		  select qty_after_transaction, stock_value,
		         row_number() over (
		           partition by item_code, warehouse
		           order by posting_date desc, posting_time desc, creation desc
		         ) as rn
		  from `tabStock Ledger Entry`
		  where is_cancelled=0 and company=%s
		    and item_code is not null and item_code!=''
		    and warehouse is not null and warehouse!=''
		) t
		where rn=1
		""",
		company,
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
	by_cat = frappe.db.sql(
		"""
		select coalesce(accounting_category, '') cat, count(*) c
		from `tabTemporary Valuation Adjustment`
		where company=%s
		group by coalesce(accounting_category, '')
		""",
		company,
		as_dict=True,
	)
	by_repair = frappe.db.sql(
		"""
		select coalesce(repair_status, '') st, count(*) c
		from `tabTemporary Valuation Adjustment`
		where company=%s
		group by coalesce(repair_status, '')
		""",
		company,
		as_dict=True,
	)
	return {
		"exists": True,
		"by_accounting_category": {r.cat: cint(r.c) for r in by_cat},
		"by_repair_status": {r.st: cint(r.c) for r in by_repair},
		"total": cint(
			frappe.db.count("Temporary Valuation Adjustment", {"company": company})
		),
	}


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
	latest_val = flt(latest.get("value") if isinstance(latest, dict) else latest.value)
	bin_val = flt(sle_bin.get("bin_value"))
	sle_svd = flt(sle_bin.get("sle_svd"))
	gl_bal = flt(gl["balance"])
	# Primary stock↔GL check: cumulative SLE SVD vs inventory GL (warehouse accounts).
	svd_vs_gl = sle_svd - gl_bal
	bin_vs_latest = bin_val - latest_val
	terminal_ok = bool(term.get("ok"))
	# IRR sites accumulate sub-IRR/posting noise across 100k+ SLE rows.
	# Bin↔latest SLE must be exact; SLE SVD↔warehouse GL tolerates ≤1e5 IRR.
	layers_ok = abs(svd_vs_gl) <= 100_000 and abs(bin_vs_latest) < 1
	return {
		"campaign": campaign,
		"company": company,
		"terminal": term,
		"riv": riv_snapshot(),
		"sle_bin": sle_bin,
		"sle_latest_stock_value": latest_val,
		"gl_inventory": gl,
		"sle_svd_vs_gl_delta": svd_vs_gl,
		"bin_vs_sle_latest_delta": bin_vs_latest,
		"tva": tva_snapshot(company),
		"verdict": "OK" if terminal_ok and layers_ok else "BLOCKERS",
	}
