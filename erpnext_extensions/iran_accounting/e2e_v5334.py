# Copyright (c) 2026, ERPNext Extensions contributors
"""Development-only Playwright fixtures for Manufacture TYPE C residual (v5.3.34)."""

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
from erpnext_extensions.iran_accounting.scrap_costing import _integer_rate_pair

PREFIX = "IA-V5334"
CANARY = "MAT-STE-2026-37762"
CANARY_FG_AMOUNT = 2045097670.0
CANARY_REJECT_AMOUNT = 717752.0
CANARY_RESIDUAL = 1.0
SA_ACCOUNT_SNIPPET = "621301"


@frappe.whitelist()
def mint_dev_sid():
	from erpnext_extensions.iran_accounting.e2e_v533 import mint_dev_sid as _mint

	return _mint()


def _stock_entry_type(purpose: str = "Manufacture") -> str | None:
	return frappe.db.get_value("Stock Entry Type", {"purpose": purpose}, "name")


def _default_cost_center(company: str) -> str:
	return frappe.db.get_value(
		"Cost Center", {"company": company, "is_group": 0}, "name", order_by="creation asc"
	)


def _default_expense_account(company: str) -> str | None:
	preferred = "621301 - تعدیلات موجودی کالا - E"
	if frappe.db.exists("Account", preferred):
		return preferred
	return frappe.db.get_value(
		"Account", {"company": company, "account_type": "Stock Adjustment", "is_group": 0}, "name"
	)


def _default_department(company: str) -> str | None:
	return frappe.db.get_value("Department", {"company": company}, "name", order_by="creation asc")


def _seed_item(company: str, prefix: str, rate: float = 100) -> str:
	item = ensure_test_item(company, prefix=prefix)
	frappe.db.set_value("Item", item, "valuation_rate", rate)
	frappe.db.set_value("Item", item, "include_item_in_manufacturing", 1)
	return item


def _pick_stocked(company: str, min_qty: float = 30):
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
		if payload.get("t_warehouse") is True:
			payload["t_warehouse"] = fg_wh
		if payload.get("s_warehouse") is True:
			payload["s_warehouse"] = wip
		payload.setdefault("uom", frappe.db.get_value("Item", payload["item_code"], "stock_uom"))
		payload.setdefault("stock_uom", payload["uom"])
		payload.setdefault("conversion_factor", 1)
		payload.setdefault("transfer_qty", payload.get("qty"))
		payload.setdefault("cost_center", cc)
		payload.setdefault("expense_account", expense)
		payload.setdefault("department", dept)
		doc.append("items", payload)
	for ac in additional_costs or []:
		doc.append("additional_costs", ac)
	doc.insert(ignore_permissions=True, ignore_mandatory=True)
	frappe.db.commit()
	return frappe.get_doc("Stock Entry", doc.name)


def _verify_submitted(name: str) -> dict:
	doc = frappe.get_doc("Stock Entry", name)
	fg = next((r for r in doc.items if r.is_finished_item), None)
	reject = next(
		(
			r
			for r in doc.items
			if r.t_warehouse and not r.is_finished_item and (r.get("secondary_item_type") or r.get("type")) == "Scrap"
		),
		None,
	)
	failures = collect_ledger_contract_failures(name, doc.company)
	sle = frappe.get_all(
		"Stock Ledger Entry",
		filters={"voucher_no": name, "is_cancelled": 0},
		fields=["stock_value_difference"],
	)
	sigma = sum(flt(s.stock_value_difference) for s in sle)
	gl = frappe.get_all(
		"GL Entry",
		filters={"voucher_no": name, "is_cancelled": 0},
		fields=["account", "debit", "credit"],
	)
	td = sum(flt(g.debit) for g in gl)
	tc = sum(flt(g.credit) for g in gl)
	sa_debit = sum(flt(g.debit) for g in gl if SA_ACCOUNT_SNIPPET in (g.account or ""))
	sa_credit = sum(flt(g.credit) for g in gl if SA_ACCOUNT_SNIPPET in (g.account or ""))
	return {
		"docstatus": doc.docstatus,
		"contract_version": doc.get("custom_manufacturing_costing_contract_version"),
		"fg_amount": flt(fg.amount) if fg else None,
		"fg_additional_cost": flt(fg.additional_cost) if fg else None,
		"reject_amount": flt(reject.amount) if reject else None,
		"ledger_pass": not failures,
		"ledger_failures": failures,
		"sle_sigma": sigma,
		"gl_debit": td,
		"gl_credit": tc,
		"gl_balanced": abs(td - tc) < 0.5,
		"sa_debit": sa_debit,
		"sa_credit": sa_credit,
	}


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
	se.posting_date = add_to_date(now_datetime(), days=-2).date()
	if cc:
		se.cost_center = cc
	expense = _default_expense_account(company)
	dept = _default_department(company)
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
			"expense_account": expense,
			"department": dept,
		},
	)
	se.insert(ignore_permissions=True, ignore_mandatory=True)
	se.submit()


def _pool_with_leftover(fg_qty: float, rej_qty: float, prefer: float = 1200.0) -> tuple[float, float, float, float]:
	"""Return (pool, gr, sr, leftover) with leftover != 0 within bound."""
	start = int(prefer)
	for pool in range(start, start + 5000):
		pair = _integer_rate_pair(pool, fg_qty, rej_qty)
		if not pair:
			continue
		gr, sr, leftover = pair
		if leftover:
			return float(pool), float(gr), float(sr), float(leftover)
	frappe.throw("Could not find TYPE C pool with non-zero leftover")


def _make_type_c_manufacture(company: str, header_add: float = 0.0) -> tuple:
	"""Build Manufacture FG+Product-Reject with proven TYPE C residual via stocked pool."""
	import time

	fg_qty, rej_qty = 10.0, 2.0
	pool, gr, sr, leftover = _pool_with_leftover(fg_qty, rej_qty)
	wip = get_warehouse(company)
	suffix = str(int(time.time() * 1000))[-8:]
	rm_item = _seed_item(company, f"{PREFIX}-RM-{suffix}", rate=pool)
	fg_item = _seed_item(company, f"{PREFIX}-FG-{suffix}", rate=gr)
	# Seed consume stock at exact pool amount (qty=1 → valuation = pool)
	_ensure_qty(company, rm_item, wip, qty=1, rate=pool)
	expense = frappe.db.get_value(
		"Account", {"company": company, "root_type": "Expense", "is_group": 0}, "name"
	)
	items = [
		{
			"item_code": rm_item,
			"qty": 1,
			"basic_rate": pool,
			"basic_amount": pool,
			"amount": pool,
			"valuation_rate": pool,
			"s_warehouse": wip,
			"t_warehouse": None,
			"is_finished_item": 0,
			"set_basic_rate_manually": 1,
		},
		{
			"item_code": fg_item,
			"qty": fg_qty,
			"basic_rate": gr,
			"basic_amount": fg_qty * gr,
			"amount": fg_qty * gr + header_add,
			"additional_cost": header_add,
			"valuation_rate": (fg_qty * gr + header_add) / fg_qty if fg_qty else gr,
			"t_warehouse": True,
			"is_finished_item": 1,
		},
		{
			"item_code": fg_item,
			"qty": rej_qty,
			"basic_rate": sr,
			"basic_amount": rej_qty * sr,
			"amount": rej_qty * sr,
			"valuation_rate": sr,
			"t_warehouse": True,
			"is_finished_item": 0,
			"is_scrap_item": 1,
			"secondary_item_type": "Scrap",
		},
	]
	additional_costs = None
	if header_add:
		additional_costs = [
			{"description": "Overhead", "expense_account": expense, "amount": header_add}
		]
	doc = _new_manufacture(company, items, additional_costs=additional_costs)
	# Force TYPE C economics + valuation_rate == amount/qty after insert.
	from erpnext_extensions.iran_accounting.scrap_costing import allocate_scrap_absorbed_cost

	allocate_scrap_absorbed_cost(doc)
	for row in doc.items:
		qty = flt(row.transfer_qty or row.qty)
		if qty and row.t_warehouse:
			row.valuation_rate = round(flt(row.amount) / qty)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	doc = frappe.get_doc("Stock Entry", doc.name)
	return doc, leftover, fg_qty * gr + header_add, rej_qty * sr, header_add


@frappe.whitelist()
def prepare_v5334_ui_scenario(scenario: str, company: str | None = None) -> dict:
	"""Build disposable Manufacture TYPE C fixtures for Playwright v5.3.34."""
	if frappe.conf.get("e2e_marker") != "DEV_SITE" and frappe.conf.get("developer_mode") != 1:
		frappe.throw("prepare_v5334_ui_scenario is development-only")
	frappe.set_user("Administrator")
	company = get_irr_company(preferred=company)

	if scenario == "canary_open":
		doc = frappe.get_doc("Stock Entry", CANARY)
		fg = next(r for r in doc.items if r.is_finished_item)
		rej = next(
			r
			for r in doc.items
			if r.t_warehouse and not r.is_finished_item
		)
		return {
			"stock_entry": doc.name,
			"desk_url": f"/desk/stock-entry/{doc.name}",
			"docstatus": doc.docstatus,
			"expected_fg_amount": CANARY_FG_AMOUNT,
			"expected_reject_amount": CANARY_REJECT_AMOUNT,
			"fg_amount": flt(fg.amount),
			"reject_amount": flt(rej.amount),
			"additional_cost": flt(fg.additional_cost),
			"scenario": scenario,
		}

	if scenario == "type_c_product_reject":
		doc, leftover, fg_amt, rej_amt, _ = _make_type_c_manufacture(company, header_add=0)
		return {
			"scenario": scenario,
			"stock_entry": doc.name,
			"desk_url": f"/desk/stock-entry/{doc.name}",
			"docstatus": doc.docstatus,
			"fg_amount": fg_amt,
			"reject_amount": rej_amt,
			"expected_residual": leftover,
			"purpose": doc.purpose,
		}

	if scenario == "type_c_with_add_cost":
		doc, leftover, fg_amt, rej_amt, add = _make_type_c_manufacture(company, header_add=100)
		return {
			"scenario": scenario,
			"stock_entry": doc.name,
			"desk_url": f"/desk/stock-entry/{doc.name}",
			"docstatus": doc.docstatus,
			"fg_amount": fg_amt,
			"reject_amount": rej_amt,
			"expected_add_cost": add,
			"expected_residual": leftover,
			"purpose": doc.purpose,
		}

	if scenario == "invalid_row_sle_drift":
		doc, leftover, fg_amt, rej_amt, _ = _make_type_c_manufacture(company, header_add=0)
		return {
			"scenario": scenario,
			"stock_entry": doc.name,
			"desk_url": f"/desk/stock-entry/{doc.name}",
			"docstatus": doc.docstatus,
			"expected_residual": leftover,
			"purpose": doc.purpose,
		}

	frappe.throw(f"Unknown scenario {scenario}")



@frappe.whitelist()
def submit_fixture(name: str) -> dict:
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
def cancel_fixture(name: str) -> dict:
	if frappe.conf.get("e2e_marker") != "DEV_SITE" and frappe.conf.get("developer_mode") != 1:
		frappe.throw("cancel_fixture is development-only")
	if name == CANARY:
		frappe.throw("canary protected")
	frappe.set_user("Administrator")
	doc = frappe.get_doc("Stock Entry", name)
	if doc.docstatus == 1:
		doc.cancel()
		frappe.db.commit()
	active_sle = frappe.db.count("Stock Ledger Entry", {"voucher_no": name, "is_cancelled": 0})
	active_gl = frappe.db.count("GL Entry", {"voucher_no": name, "is_cancelled": 0})
	return {
		"docstatus": frappe.db.get_value("Stock Entry", name, "docstatus"),
		"active_sle": active_sle,
		"active_gl": active_gl,
	}


@frappe.whitelist()
def cleanup_v5334_stock_entry(name: str) -> dict:
	if frappe.conf.get("e2e_marker") != "DEV_SITE" and frappe.conf.get("developer_mode") != 1:
		frappe.throw("cleanup_v5334_stock_entry is development-only")
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
	"""Rollback-safe TYPE C canary for MAT-STE-2026-37762."""
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
		assert result["reject_amount"] == CANARY_REJECT_AMOUNT, result
		assert result["fg_additional_cost"] == 0, result
		assert result["sle_sigma"] == -CANARY_RESIDUAL, result
		assert result["sa_debit"] == CANARY_RESIDUAL, result
		assert result["ledger_pass"], result
		assert result["gl_balanced"], result
		assert result["contract_version"] == "5.3.37", result
		out = {"submit": "SUCCESS", "before_docstatus": before, **result}
	except Exception as exc:
		out = {"submit": "FAIL", "error": str(exc)}
		raise
	finally:
		frappe.db.rollback()
	out["after_docstatus"] = frappe.db.get_value("Stock Entry", CANARY, "docstatus")
	out["rolled_back"] = True
	out["sle_after"] = frappe.db.count("Stock Ledger Entry", {"voucher_no": CANARY, "is_cancelled": 0})
	out["gl_after"] = frappe.db.count("GL Entry", {"voucher_no": CANARY, "is_cancelled": 0})
	return out


@frappe.whitelist()
def verify_invalid_submit_fails(name: str) -> dict:
	"""After a valid submit, corrupt FG amount vs SLE and assert ledger contract fails closed."""
	if frappe.conf.get("e2e_marker") != "DEV_SITE" and frappe.conf.get("developer_mode") != 1:
		frappe.throw("verify_invalid_submit_fails is development-only")
	frappe.set_user("Administrator")
	frappe.db.begin()
	failed = False
	error = ""
	try:
		doc = frappe.get_doc("Stock Entry", name)
		if doc.docstatus == 0:
			doc.submit()
		# Corrupt persisted FG amount so row ↔ SLE diverges by 1.
		fg = next(r for r in doc.items if r.is_finished_item)
		frappe.db.set_value(
			"Stock Entry Detail",
			fg.name,
			{
				"amount": flt(fg.amount) + 1,
				"basic_amount": flt(fg.basic_amount) + 1,
			},
			update_modified=False,
		)
		fails = collect_ledger_contract_failures(name, doc.company)
		if fails:
			failed = True
			error = "; ".join(fails)
		else:
			error = "ledger contract accepted corrupted row amount"
	except Exception as exc:
		failed = True
		error = str(exc)
	finally:
		frappe.db.rollback()
	return {"failed_closed": failed, "error": error}
