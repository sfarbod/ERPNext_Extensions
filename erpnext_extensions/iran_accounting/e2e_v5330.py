# Copyright (c) 2026, ERPNext Extensions contributors
"""Development-only Playwright fixtures for Manufacture residual submit v5.3.30."""

from __future__ import annotations

import frappe
from frappe.utils import flt, now_datetime, add_to_date

from erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract import (
	collect_ledger_contract_failures,
)
from erpnext_extensions.iran_accounting.e2e_bootstrap import (
	ensure_test_item,
	get_irr_company,
	get_second_warehouse,
	get_warehouse,
)
from erpnext_extensions.iran_accounting.stock_entry import apply_irr_manufacture_economic_finalize

PREFIX = "IA-V5330"
CANARY = "MAT-STE-2026-37736"
CANARY_FG_AMOUNT = 5219072303.0


@frappe.whitelist()
def mint_dev_sid():
	from erpnext_extensions.iran_accounting.e2e_v533 import mint_dev_sid as _mint

	return _mint()


def _stock_entry_type(purpose: str = "Manufacture") -> str | None:
	return frappe.db.get_value("Stock Entry Type", {"purpose": purpose}, "name")


def _seed_item(company: str, prefix: str, rate: float = 100) -> str:
	item = ensure_test_item(company, prefix=prefix)
	frappe.db.set_value("Item", item, "valuation_rate", rate)
	frappe.db.set_value("Item", item, "include_item_in_manufacturing", 1)
	return item


def _default_cost_center(company: str) -> str:
	return frappe.db.get_value(
		"Cost Center", {"company": company, "is_group": 0}, "name", order_by="creation asc"
	)


def _ensure_qty(company: str, item: str, warehouse: str, qty: float, rate: float) -> None:
	"""Receipt enough stock for manufacture consume (idempotent top-up)."""
	bal = flt(
		frappe.db.sql(
			"""
			select coalesce(sum(actual_qty), 0)
			from `tabStock Ledger Entry`
			where item_code=%s and warehouse=%s and is_cancelled=0
			""",
			(item, warehouse),
		)[0][0]
	)
	need = qty - bal
	if need <= 0:
		return
	cc = _default_cost_center(company)
	se = frappe.new_doc("Stock Entry")
	se.company = company
	se.purpose = "Material Receipt"
	se.stock_entry_type = _stock_entry_type("Material Receipt")
	se.set_posting_time = 1
	if cc:
		se.cost_center = cc
	se.append(
		"items",
		{
			"item_code": item,
			"qty": need,
			"transfer_qty": need,
			"uom": frappe.db.get_value("Item", item, "stock_uom"),
			"stock_uom": frappe.db.get_value("Item", item, "stock_uom"),
			"conversion_factor": 1,
			"t_warehouse": warehouse,
			"basic_rate": rate,
			"basic_amount": need * rate,
			"amount": need * rate,
			"valuation_rate": rate,
			"allow_zero_valuation_rate": 0,
			"cost_center": cc,
		},
	)
	se.insert(ignore_permissions=True, ignore_mandatory=True)
	se.submit()


def _default_expense_account(company: str) -> str | None:
	# Site Server Script requires this exact Stock Adjustment account on Manufacture rows.
	preferred = "621301 - تعدیلات موجودی کالا - E"
	if frappe.db.exists("Account", preferred):
		return preferred
	return frappe.db.get_value(
		"Account", {"company": company, "account_type": "Stock Adjustment", "is_group": 0}, "name"
	)


def _default_department(company: str) -> str | None:
	return frappe.db.get_value("Department", {"company": company}, "name", order_by="creation asc")


def _new_manufacture(company: str, items: list[dict], additional_costs: list[dict] | None = None):
	wip = get_warehouse(company)
	fg_wh = get_second_warehouse(company, wip)
	cc = _default_cost_center(company)
	expense = _default_expense_account(company)
	dept = _default_department(company)
	doc = frappe.new_doc("Stock Entry")
	doc.company = company
	doc.purpose = "Manufacture"
	doc.stock_entry_type = _stock_entry_type("Manufacture")
	doc.set_posting_time = 1
	doc.posting_date = add_to_date(now_datetime(), days=-1).date()
	if cc:
		doc.cost_center = cc
	if dept:
		doc.department = dept
	for row in items:
		payload = dict(row)
		s_wh = payload.pop("s_warehouse", None)
		t_wh = payload.pop("t_warehouse", None)
		if s_wh is True:
			payload["s_warehouse"] = wip
			payload["t_warehouse"] = None
		elif s_wh:
			payload["s_warehouse"] = s_wh
		if t_wh is True:
			payload["t_warehouse"] = fg_wh
			payload["s_warehouse"] = None
		elif t_wh:
			payload["t_warehouse"] = t_wh
		payload.setdefault("uom", frappe.db.get_value("Item", payload["item_code"], "stock_uom"))
		payload.setdefault("stock_uom", payload["uom"])
		payload.setdefault("conversion_factor", 1)
		payload.setdefault("transfer_qty", payload.get("qty"))
		payload.setdefault("cost_center", cc)
		if expense:
			payload.setdefault("expense_account", expense)
		if dept:
			payload.setdefault("department", dept)
		doc.append("items", payload)
	for cost in additional_costs or []:
		payload = dict(cost)
		payload.setdefault("cost_center", cc)
		doc.append("additional_costs", payload)
	apply_irr_manufacture_economic_finalize(doc)
	if hasattr(doc, "set_total_incoming_outgoing_value"):
		doc.set_total_incoming_outgoing_value()
	doc.flags.ignore_validate = True
	doc.insert(ignore_permissions=True, ignore_mandatory=True)
	frappe.db.commit()
	return frappe.get_doc("Stock Entry", doc.name)


def _fg_amount(doc) -> float:
	for row in doc.items:
		if row.is_finished_item:
			return flt(row.amount)
	return 0.0


def _verify_submitted(name: str) -> dict:
	doc = frappe.get_doc("Stock Entry", name)
	failures = collect_ledger_contract_failures(name, doc.company)
	gle = frappe.get_all(
		"GL Entry",
		filters={"voucher_no": name, "is_cancelled": 0},
		fields=["debit", "credit"],
	)
	debit = sum(flt(r.debit) for r in gle)
	credit = sum(flt(r.credit) for r in gle)
	sle = frappe.get_all(
		"Stock Ledger Entry",
		filters={"voucher_no": name, "is_cancelled": 0},
		fields=["voucher_detail_no", "stock_value_difference"],
	)
	return {
		"docstatus": doc.docstatus,
		"fg_amount": _fg_amount(doc),
		"ledger_failures": failures,
		"ledger_pass": not failures,
		"gl_debit": debit,
		"gl_credit": credit,
		"gl_balanced": debit == credit,
		"sle_count": len(sle),
	}


def _pick_stocked(company: str, min_qty: float = 20) -> tuple[str, str, float]:
	"""Return (item_code, warehouse, valuation_rate) with enough free qty (no batch/serial)."""
	rows = frappe.db.sql(
		"""
		select sle.item_code, sle.warehouse,
		       sum(sle.actual_qty) as qty,
		       max(abs(sle.valuation_rate)) as rate
		from `tabStock Ledger Entry` sle
		inner join `tabWarehouse` wh on wh.name = sle.warehouse
		inner join `tabItem` it on it.name = sle.item_code
		where wh.company=%s and sle.is_cancelled=0
		  and ifnull(it.has_batch_no, 0)=0
		  and ifnull(it.has_serial_no, 0)=0
		group by sle.item_code, sle.warehouse
		having sum(sle.actual_qty) >= %s
		order by sum(sle.actual_qty) desc
		limit 20
		""",
		(company, min_qty),
		as_dict=True,
	)
	if not rows:
		frappe.throw(f"No non-batch stocked item/warehouse with qty>={min_qty} for {company}")
	row = rows[0]
	rate = flt(row.rate) or flt(frappe.db.get_value("Item", row.item_code, "valuation_rate")) or 1
	return row.item_code, row.warehouse, rate


@frappe.whitelist()
def prepare_v5330_ui_scenario(scenario: str, company: str | None = None) -> dict:
	"""Build disposable Manufacture / Transfer fixtures for Playwright v5.3.30."""
	if frappe.conf.get("e2e_marker") != "DEV_SITE" and frappe.conf.get("developer_mode") != 1:
		frappe.throw("prepare_v5330_ui_scenario is development-only")
	frappe.set_user("Administrator")
	company = get_irr_company(preferred=company)

	if scenario == "canary_open":
		doc = frappe.get_doc("Stock Entry", CANARY)
		fg = next(r for r in doc.items if r.is_finished_item)
		return {
			"stock_entry": doc.name,
			"desk_url": f"/desk/stock-entry/{doc.name}",
			"docstatus": doc.docstatus,
			"expected_fg_amount": CANARY_FG_AMOUNT,
			"fg_amount": flt(fg.amount),
			"additional_cost": flt(fg.additional_cost),
			"scenario": scenario,
		}

	fg_item = _seed_item(company, f"{PREFIX}-FG", rate=1000)
	rm_item, wip, rm_rate = _pick_stocked(company, min_qty=30)
	rm_rate = int(round(rm_rate)) or 1000
	expense = frappe.db.get_value(
		"Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name"
	)

	if scenario == "mfg_no_scrap":
		qty = 10
		doc = _new_manufacture(
			company,
			[
				{
					"item_code": rm_item,
					"qty": qty,
					"basic_rate": rm_rate,
					"basic_amount": qty * rm_rate,
					"amount": qty * rm_rate,
					"valuation_rate": rm_rate,
					"s_warehouse": wip,
					"t_warehouse": None,
					"is_finished_item": 0,
				},
				{
					"item_code": fg_item,
					"qty": qty,
					"basic_rate": rm_rate,
					"basic_amount": qty * rm_rate,
					"amount": qty * rm_rate,
					"valuation_rate": rm_rate,
					"t_warehouse": True,
					"is_finished_item": 1,
				},
			],
		)
	elif scenario == "mfg_with_scrap":
		qty = 20
		scrap_qty = 2
		fg_qty = 18
		doc = _new_manufacture(
			company,
			[
				{
					"item_code": rm_item,
					"qty": qty,
					"basic_rate": rm_rate,
					"basic_amount": qty * rm_rate,
					"amount": qty * rm_rate,
					"valuation_rate": rm_rate,
					"s_warehouse": wip,
					"t_warehouse": None,
					"is_finished_item": 0,
				},
				{
					"item_code": fg_item,
					"qty": fg_qty,
					"basic_rate": rm_rate,
					"basic_amount": fg_qty * rm_rate,
					"amount": fg_qty * rm_rate,
					"valuation_rate": rm_rate,
					"t_warehouse": True,
					"is_finished_item": 1,
				},
				{
					"item_code": rm_item,
					"qty": scrap_qty,
					"basic_rate": rm_rate,
					"basic_amount": scrap_qty * rm_rate,
					"amount": scrap_qty * rm_rate,
					"valuation_rate": rm_rate,
					"t_warehouse": True,
					"is_finished_item": 0,
					"is_scrap_item": 1,
					"secondary_item_type": "Scrap",
				},
			],
		)
	elif scenario == "mfg_with_add_cost":
		qty = 10
		doc = _new_manufacture(
			company,
			[
				{
					"item_code": rm_item,
					"qty": qty,
					"basic_rate": rm_rate,
					"basic_amount": qty * rm_rate,
					"amount": qty * rm_rate,
					"valuation_rate": rm_rate,
					"s_warehouse": wip,
					"t_warehouse": None,
					"is_finished_item": 0,
				},
				{
					"item_code": fg_item,
					"qty": qty,
					"basic_rate": rm_rate,
					"basic_amount": qty * rm_rate,
					"amount": qty * rm_rate + 5,
					"valuation_rate": rm_rate,
					"additional_cost": 5,
					"t_warehouse": True,
					"is_finished_item": 1,
				},
			],
			additional_costs=[{"description": "Non stock items", "expense_account": expense, "amount": 5}],
		)
	elif scenario == "mfg_scrap_add_residual":
		# Consume 1 unit at a rate that leaves a Manufacture residual vs qty×int_rate.
		consume_qty = 1
		fg_qty = 10
		# Choose a residual-friendly amount near available rate
		material = int(rm_rate) if int(rm_rate) % 10 != 0 else int(rm_rate) + 3
		# Prefer exact stocked rate if residual already present; else bump material
		material = max(material, int(rm_rate))
		if material % fg_qty == 0:
			material += 3
		int_rate = int(round(material / fg_qty))
		doc = _new_manufacture(
			company,
			[
				{
					"item_code": rm_item,
					"qty": consume_qty,
					"basic_rate": material,
					"basic_amount": material,
					"amount": material,
					"valuation_rate": material,
					"s_warehouse": wip,
					"t_warehouse": None,
					"is_finished_item": 0,
					"set_basic_rate_manually": 1,
				},
				{
					"item_code": fg_item,
					"qty": fg_qty,
					"basic_rate": int_rate,
					"basic_amount": int_rate * fg_qty,
					"amount": int_rate * fg_qty + 5,
					"valuation_rate": int_rate,
					"additional_cost": 5,
					"t_warehouse": True,
					"is_finished_item": 1,
				},
			],
			additional_costs=[{"description": "Non stock items", "expense_account": expense, "amount": 5}],
		)
	elif scenario == "material_transfer":
		fg_wh = get_second_warehouse(company, wip)
		cc = _default_cost_center(company)
		dept = _default_department(company)
		doc = frappe.new_doc("Stock Entry")
		doc.company = company
		doc.purpose = "Material Transfer"
		doc.stock_entry_type = _stock_entry_type("Material Transfer")
		doc.set_posting_time = 1
		if cc:
			doc.cost_center = cc
		if dept:
			doc.department = dept
		doc.append(
			"items",
			{
				"item_code": rm_item,
				"qty": 5,
				"transfer_qty": 5,
				"uom": frappe.db.get_value("Item", rm_item, "stock_uom"),
				"stock_uom": frappe.db.get_value("Item", rm_item, "stock_uom"),
				"conversion_factor": 1,
				"s_warehouse": wip,
				"t_warehouse": fg_wh,
				"basic_rate": rm_rate,
				"basic_amount": 5 * rm_rate,
				"amount": 5 * rm_rate,
				"valuation_rate": rm_rate,
				"cost_center": cc,
				"department": dept,
			},
		)
		doc.insert(ignore_permissions=True, ignore_mandatory=True)
		frappe.db.commit()
		doc = frappe.get_doc("Stock Entry", doc.name)
	else:
		frappe.throw(f"Unknown scenario {scenario}")

	# Force s_warehouse absolute when we passed a concrete warehouse string
	return {
		"scenario": scenario,
		"stock_entry": doc.name,
		"desk_url": f"/desk/stock-entry/{doc.name}",
		"docstatus": doc.docstatus,
		"fg_amount": _fg_amount(doc),
		"expected_fg_amount": _fg_amount(doc),
		"purpose": doc.purpose,
	}


@frappe.whitelist()
def submit_fixture(name: str) -> dict:
	"""Submit a disposable fixture and return ledger verification (dev only)."""
	if frappe.conf.get("e2e_marker") != "DEV_SITE" and frappe.conf.get("developer_mode") != 1:
		frappe.throw("submit_fixture is development-only")
	if name == CANARY:
		frappe.throw("canary must use verify_canary_submit_rollback")
	frappe.set_user("Administrator")
	doc = frappe.get_doc("Stock Entry", name)
	if doc.docstatus == 0:
		doc.submit()
	frappe.db.commit()
	return _verify_submitted(name)


@frappe.whitelist()
def cleanup_v5330_stock_entry(name: str) -> dict:
	"""Cancel (if submitted) and delete disposable v5.3.30 fixture; never touch canary."""
	if frappe.conf.get("e2e_marker") != "DEV_SITE" and frappe.conf.get("developer_mode") != 1:
		frappe.throw("cleanup_v5330_stock_entry is development-only")
	if name == CANARY:
		return {"skipped": True, "reason": "canary protected"}
	frappe.set_user("Administrator")
	if not frappe.db.exists("Stock Entry", name):
		return {"missing": True}
	doc = frappe.get_doc("Stock Entry", name)
	if doc.docstatus == 1:
		doc.cancel()
	doc.delete(ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": name}


@frappe.whitelist()
def verify_canary_submit_rollback() -> dict:
	"""Deterministic server canary used by Playwright Scenario A companion."""
	if frappe.conf.get("e2e_marker") != "DEV_SITE" and frappe.conf.get("developer_mode") != 1:
		frappe.throw("verify_canary_submit_rollback is development-only")
	frappe.set_user("Administrator")
	before = frappe.db.get_value("Stock Entry", CANARY, "docstatus")
	frappe.db.begin()
	try:
		doc = frappe.get_doc("Stock Entry", CANARY)
		doc.submit()
		result = _verify_submitted(CANARY)
		assert result["fg_amount"] == CANARY_FG_AMOUNT, result
		assert result["ledger_pass"], result
		assert result["gl_balanced"], result
		out = {"submit": "SUCCESS", "before_docstatus": before, **result}
	except Exception as exc:
		out = {"submit": "FAIL", "error": str(exc)}
		raise
	finally:
		frappe.db.rollback()
	out["after_docstatus"] = frappe.db.get_value("Stock Entry", CANARY, "docstatus")
	out["rolled_back"] = True
	out["sle_after"] = frappe.db.count("Stock Ledger Entry", {"voucher_no": CANARY})
	return out
