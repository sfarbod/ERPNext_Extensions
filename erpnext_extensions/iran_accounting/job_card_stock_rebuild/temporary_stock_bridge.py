# Copyright (c) 2026, ERPNext Extensions contributors
"""Temporary Material Receipt bridge for atomic historical repair (v5.5.0).

Technical scaffolding only: create → submit → (repair) → cancel → delete.
Must leave zero stock/accounting effect after cleanup. Not Job Card evidence.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.shared_logistics import (
	batch_qty,
	se_lines,
)

TEMP_REMARK_MARKER = "JC_REPAIR_TEMP_BRIDGE"
TEMP_BRIDGE_FLAG = "jc_temp_stock_bridge"


def compute_cancel_shortages(cancel_names_desc: list[str]) -> list[dict[str, Any]]:
	"""Minimum Item×Batch×Warehouse shortages to cancel the set in order.

	Simulates reverse-chrono cancel stock math without foreign docs.
	Credits computed shortages into the running balance so later rows see them.
	"""
	balances: dict[tuple[str, str, str], float] = {}
	shortages: dict[tuple[str, str, str], float] = {}

	def _key(item, batch, wh):
		return (item, batch or "", wh or "")

	def _bal(item, batch, wh):
		k = _key(item, batch, wh)
		if k not in balances:
			balances[k] = batch_qty(item, batch or "", wh or "")
		return balances[k]

	def _add(item, batch, wh, qty):
		k = _key(item, batch, wh)
		balances[k] = _bal(item, batch, wh) + qty

	for name in cancel_names_desc:
		purpose = frappe.db.get_value("Stock Entry", name, "purpose")
		if purpose != "Material Transfer":
			continue
		for ln in se_lines(name):
			qty = flt(ln.transfer_qty or ln.qty)
			if qty <= 0 or not ln.t_warehouse or not ln.s_warehouse:
				continue
			have = _bal(ln.item_code, ln.batch_no, ln.t_warehouse)
			if have + 1e-9 < qty:
				short = qty - have
				k = _key(ln.item_code, ln.batch_no, ln.t_warehouse)
				shortages[k] = flt(shortages.get(k)) + short
				_add(ln.item_code, ln.batch_no, ln.t_warehouse, short)
			_add(ln.item_code, ln.batch_no, ln.t_warehouse, -qty)
			_add(ln.item_code, ln.batch_no, ln.s_warehouse, qty)

	out = []
	for (item, batch, wh), qty in shortages.items():
		if qty <= 1e-9:
			continue
		out.append(
			{
				"item_code": item,
				"batch_no": batch,
				"warehouse": wh,
				"required_qty": None,  # filled by caller context if needed
				"current_qty": batch_qty(item, batch, wh),
				"shortage_qty": flt(qty),
				"valuation_rate": _bridge_rate(item, batch, wh),
			}
		)
	out.sort(key=lambda r: (r["warehouse"], r["item_code"], r["batch_no"]))
	return out


def _bridge_rate(item_code: str, batch_no: str, warehouse: str) -> float:
	row = frappe.db.sql(
		"""
		select valuation_rate, incoming_rate, outgoing_rate
		from `tabStock Ledger Entry`
		where item_code=%s and warehouse=%s and is_cancelled=0
		  and (batch_no=%s or serial_and_batch_bundle in (
		    select parent from `tabSerial and Batch Entry` where batch_no=%s
		  ))
		order by posting_date desc, posting_time desc, creation desc
		limit 1
		""",
		(item_code, warehouse, batch_no or "", batch_no or ""),
		as_dict=1,
	)
	if not row:
		row = frappe.db.sql(
			"""
			select valuation_rate, incoming_rate, outgoing_rate
			from `tabStock Ledger Entry`
			where item_code=%s and is_cancelled=0
			order by posting_date desc, posting_time desc
			limit 1
			""",
			(item_code,),
			as_dict=1,
		)
	if not row:
		return 0.0
	return flt(row[0].incoming_rate or row[0].outgoing_rate or row[0].valuation_rate)


def _earliest_posting(cancel_names: list[str]) -> tuple[str, str]:
	rows = frappe.db.sql(
		"""
		select posting_date, posting_time from `tabStock Entry`
		where name in %s
		order by posting_date, posting_time, creation
		limit 1
		""",
		(cancel_names or ["__none__"],),
		as_dict=1,
	)
	if not rows:
		return str(frappe.utils.nowdate()), "00:00:01"
	# One second before earliest logistics so SLE chronology stays coherent.
	from frappe.utils import add_to_date, get_datetime

	dt = get_datetime(f"{rows[0].posting_date} {rows[0].posting_time}")
	earlier = add_to_date(dt, seconds=-1)
	return str(earlier.date()), earlier.strftime("%H:%M:%S")


def _bridge_department(cancel_names: list[str], company: str) -> str | None:
	"""Reuse department from logistics rows (site mandates SED.department)."""
	for name in cancel_names or []:
		dept = frappe.db.get_value(
			"Stock Entry Detail",
			{"parent": name, "department": ("is", "set")},
			"department",
		)
		if dept:
			return dept
	return frappe.db.get_value(
		"Department", {"company": company, "is_group": 0}, "name", order_by="creation asc"
	)


def plan_temporary_bridge(cancel_names_desc: list[str], company: str | None = None) -> dict[str, Any]:
	"""Build bridge plan (shortages). Empty shortages → bridge not required."""
	shortages = compute_cancel_shortages(cancel_names_desc)
	if not company and cancel_names_desc:
		company = frappe.db.get_value("Stock Entry", cancel_names_desc[0], "company")
	posting_date, posting_time = _earliest_posting(cancel_names_desc)
	return {
		"required": bool(shortages),
		"company": company,
		"posting_date": posting_date,
		"posting_time": posting_time,
		"shortages": shortages,
		"cancel_names": list(cancel_names_desc),
		"department": _bridge_department(cancel_names_desc, company) if company else None,
	}


def create_and_submit_temp_receipt(
	bridge_plan: dict,
	repair_run_id: str,
) -> str:
	"""Create + submit Material Receipt for exact shortage rows."""
	shortages = bridge_plan.get("shortages") or []
	if not shortages:
		return ""
	company = bridge_plan.get("company")
	if not company:
		frappe.throw("Temporary bridge missing company")

	# Stock Entry Type for Material Receipt
	se_type = frappe.db.get_value("Stock Entry Type", {"purpose": "Material Receipt"}, "name")
	if not se_type:
		se_type = "Material Receipt"

	adj = frappe.db.get_value("Company", company, "stock_adjustment_account")
	cost_center = frappe.db.get_value(
		"Company", company, "cost_center"
	) or frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")
	department = bridge_plan.get("department") or _bridge_department(
		bridge_plan.get("cancel_names") or [], company
	)

	doc = frappe.new_doc("Stock Entry")
	doc.purpose = "Material Receipt"
	doc.stock_entry_type = se_type
	doc.company = company
	doc.set_posting_time = 1
	doc.posting_date = bridge_plan["posting_date"]
	doc.posting_time = bridge_plan["posting_time"]
	doc.remarks = f"{TEMP_REMARK_MARKER} repair_run_id={repair_run_id}"
	# Do not link Job Card — must not enter JC evidence/Golden Rule
	doc.job_card = None
	doc.work_order = None
	if department and doc.meta.has_field("department"):
		doc.department = department

	for row in shortages:
		rate = flt(row.get("valuation_rate"))
		if rate <= 0:
			frappe.throw(
				frappe._("Bridge rate missing for {0} / {1}").format(
					row["item_code"], row.get("batch_no")
				)
			)
		child = {
			"item_code": row["item_code"],
			"qty": flt(row["shortage_qty"]),
			"t_warehouse": row["warehouse"],
			"basic_rate": rate,
			"valuation_rate": rate,
			"allow_zero_valuation_rate": 0,
			"set_basic_rate_manually": 1,
			"use_serial_batch_fields": 1 if row.get("batch_no") else 0,
			"batch_no": row.get("batch_no") or None,
			"expense_account": adj,
			"cost_center": cost_center,
		}
		if department:
			child["department"] = department
		doc.append("items", child)

	doc.flags.ignore_permissions = True
	doc.flags[TEMP_BRIDGE_FLAG] = True
	frappe.flags[TEMP_BRIDGE_FLAG] = True
	doc.insert(ignore_permissions=True)
	doc.flags[TEMP_BRIDGE_FLAG] = True
	doc.submit()
	return doc.name


def cancel_temp_receipt(name: str) -> None:
	if not name or not frappe.db.exists("Stock Entry", name):
		return
	doc = frappe.get_doc("Stock Entry", name)
	if cint(doc.docstatus) == 1:
		doc.flags.ignore_permissions = True
		doc.flags[TEMP_BRIDGE_FLAG] = True
		doc.cancel()


def delete_temp_receipt(name: str) -> dict[str, Any]:
	"""Cancel-state delete + narrow cleanup of temp-only cancelled ledger rows."""
	if not name:
		return {"deleted": False, "reason": "empty"}
	if not frappe.db.exists("Stock Entry", name):
		return {"deleted": True, "already_absent": True}

	ds = cint(frappe.db.get_value("Stock Entry", name, "docstatus"))
	if ds == 1:
		frappe.throw(f"Refusing to delete submitted temp receipt {name}")

	# Capture bundle names belonging only to this voucher before delete
	bundles = frappe.db.sql(
		"""
		select distinct serial_and_batch_bundle from `tabStock Entry Detail`
		where parent=%s and ifnull(serial_and_batch_bundle,'')!=''
		""",
		name,
		pluck=True,
	) or []
	sle_bundles = frappe.db.sql(
		"""
		select distinct serial_and_batch_bundle from `tabStock Ledger Entry`
		where voucher_no=%s and ifnull(serial_and_batch_bundle,'')!=''
		""",
		name,
		pluck=True,
	) or []
	bundles = list({*bundles, *sle_bundles})

	frappe.delete_doc("Stock Entry", name, force=1, ignore_permissions=True)

	# Narrow cleanup: cancelled SLE/GL left behind for this voucher only
	frappe.db.sql("delete from `tabStock Ledger Entry` where voucher_no=%s", name)
	frappe.db.sql(
		"delete from `tabGL Entry` where voucher_type='Stock Entry' and voucher_no=%s",
		name,
	)
	# Temp-only bundles no longer referenced
	for b in bundles:
		still = frappe.db.exists("Stock Ledger Entry", {"serial_and_batch_bundle": b})
		still_sed = frappe.db.exists("Stock Entry Detail", {"serial_and_batch_bundle": b})
		if not still and not still_sed and frappe.db.exists("Serial and Batch Bundle", b):
			frappe.db.sql("delete from `tabSerial and Batch Entry` where parent=%s", b)
			frappe.db.sql("delete from `tabSerial and Batch Bundle` where name=%s", b)

	return {"deleted": True, "name": name, "cleaned_bundles": bundles}


def verify_temp_absent(name: str) -> dict[str, Any]:
	"""Assert temp receipt left no document / effective stock / GL impact."""
	errors = []
	if name and frappe.db.exists("Stock Entry", name):
		errors.append(f"Stock Entry {name} still exists")
	if name and frappe.db.exists("Stock Entry Detail", {"parent": name}):
		errors.append(f"Stock Entry Detail rows remain for {name}")
	if name:
		sle_n = frappe.db.sql(
			"select count(*) from `tabStock Ledger Entry` where voucher_no=%s", name
		)[0][0]
		if cint(sle_n):
			errors.append(f"SLE rows remain for {name}: {sle_n}")
		gl_n = frappe.db.sql(
			"""
			select count(*) from `tabGL Entry`
			where voucher_type='Stock Entry' and voucher_no=%s
			""",
			name,
		)[0][0]
		if cint(gl_n):
			errors.append(f"GL rows remain for {name}: {gl_n}")
	return {"ok": not errors, "errors": errors}


def is_temp_bridge_voucher(name: str) -> bool:
	if not name:
		return False
	remarks = frappe.db.get_value("Stock Entry", name, "remarks") or ""
	return TEMP_REMARK_MARKER in remarks
