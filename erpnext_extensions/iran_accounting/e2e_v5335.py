# Copyright (c) 2026, ERPNext Extensions contributors
"""Development-only canary / Playwright helpers for stage-equivalent By-Product (v5.3.35)."""

from __future__ import annotations

import frappe
from frappe.utils import flt

CANARY_JC = "PO-JOB07352"
FG = "20100064"
BY = "30500006"


def _dev_only():
	if frappe.conf.get("e2e_marker") != "DEV_SITE" and frappe.conf.get("developer_mode") != 1:
		frappe.throw("e2e_v5335 is development-only")


@frappe.whitelist()
def mint_dev_sid() -> dict:
	_dev_only()
	from erpnext_extensions.iran_accounting.e2e_v533 import mint_dev_sid as _mint

	return _mint()


@frappe.whitelist()
def baseline_counts() -> dict:
	_dev_only()
	frappe.set_user("Administrator")
	return {
		"stock_entry": frappe.db.count("Stock Entry"),
		"sle": frappe.db.count("Stock Ledger Entry"),
		"gl": frappe.db.count("GL Entry"),
		"jc_docstatus": frappe.db.get_value("Job Card", CANARY_JC, "docstatus"),
		"wo": frappe.db.get_value("Job Card", CANARY_JC, "work_order"),
	}


@frappe.whitelist()
def delete_investigation_zero_draft() -> dict:
	"""Remove known investigation artifact MAT-STE-2026-39522 if still Draft."""
	_dev_only()
	frappe.set_user("Administrator")
	name = "MAT-STE-2026-39522"
	if not frappe.db.exists("Stock Entry", name):
		return {"deleted": False, "reason": "missing"}
	docstatus = frappe.db.get_value("Stock Entry", name, "docstatus")
	if cint(docstatus) != 0:
		return {"deleted": False, "reason": f"docstatus={docstatus}"}
	frappe.delete_doc("Stock Entry", name, force=1, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": True, "name": name}


def cint(v):
	from frappe.utils import cint as _c

	return _c(v)


@frappe.whitelist()
def create_canary_draft_from_job_card(auto_submit: int = 0) -> dict:
	"""PO-JOB07352 → Make Stock Entry Manufacture / Semi-FG (same path as Desk)."""
	_dev_only()
	frappe.set_user("Administrator")
	jc = frappe.get_doc("Job Card", CANARY_JC)
	ste_dict = jc.make_stock_entry_for_semi_fg_item(auto_submit=bool(int(auto_submit or 0)))
	name = ste_dict.get("name") if isinstance(ste_dict, dict) else getattr(ste_dict, "name", None)
	if not name:
		frappe.throw("make_stock_entry_for_semi_fg_item did not return a Stock Entry name")
	frappe.db.commit()
	return _draft_snapshot(name)


def _draft_snapshot(name: str) -> dict:
	doc = frappe.get_doc("Stock Entry", name)
	items = []
	fg = by_row = None
	for row in doc.items:
		payload = {
			"item_code": row.item_code,
			"qty": flt(row.qty),
			"stock_uom": row.stock_uom,
			"basic_rate": flt(row.basic_rate),
			"basic_amount": flt(row.basic_amount),
			"amount": flt(row.amount),
			"additional_cost": flt(row.additional_cost),
			"valuation_type": row.valuation_type,
			"secondary_item_type": row.secondary_item_type,
			"is_finished_item": cint(row.is_finished_item),
			"allow_zero_valuation_rate": cint(row.allow_zero_valuation_rate),
			"custom_equivalent_qty": flt(row.get("custom_equivalent_qty")),
			"custom_physical_conversion": flt(row.get("custom_physical_conversion")),
			"s_warehouse": row.s_warehouse,
			"t_warehouse": row.t_warehouse,
		}
		items.append(payload)
		if cint(row.is_finished_item) and row.item_code == FG:
			fg = payload
		if row.item_code == BY and row.secondary_item_type == "By-Product":
			by_row = payload
	return {
		"name": doc.name,
		"docstatus": cint(doc.docstatus),
		"purpose": doc.purpose,
		"job_card": doc.job_card,
		"contract_version": doc.get("custom_manufacturing_costing_contract_version"),
		"total_additional_costs": flt(doc.total_additional_costs),
		"value_difference": flt(doc.value_difference),
		"fg": fg,
		"by_product": by_row,
		"items": items,
	}


@frappe.whitelist()
def prepare_canary_batches(name: str) -> dict:
	"""Create disposable Batch + use_serial_batch_fields for incoming batch items."""
	_dev_only()
	frappe.set_user("Administrator")
	doc = frappe.get_doc("Stock Entry", name)
	created = []
	# Prefer reusing consumed batch for Component Scrap of the same item.
	consume_batch = {
		r.item_code: r.batch_no
		for r in doc.items
		if r.s_warehouse and not r.t_warehouse and r.batch_no
	}
	for row in doc.items:
		if row.s_warehouse and not row.t_warehouse:
			continue
		if not frappe.db.get_value("Item", row.item_code, "has_batch_no"):
			continue
		if row.batch_no:
			row.use_serial_batch_fields = 1
			continue
		if row.secondary_item_type == "Scrap" and row.item_code in consume_batch:
			row.batch_no = consume_batch[row.item_code]
			row.use_serial_batch_fields = 1
			continue
		custom_batch_no = f"V5335{row.idx}{row.item_code[-4:]}"
		batch_id = f"V5335-{name[-5:]}-{row.idx}-{row.item_code}"
		if not frappe.db.exists("Batch", batch_id):
			frappe.get_doc(
				{
					"doctype": "Batch",
					"batch_id": batch_id,
					"item": row.item_code,
					"custom_batch_no": custom_batch_no,
					"disabled": 0,
				}
			).insert(ignore_permissions=True)
			created.append(batch_id)
		row.batch_no = batch_id
		row.use_serial_batch_fields = 1
	doc.save()
	frappe.db.commit()
	return {"name": name, "batches_created": created}


@frappe.whitelist()
def prepare_canary_expense_accounts(name: str) -> dict:
	"""Site Server Script requires expense_account=621301 on every Manufacture row."""
	_dev_only()
	frappe.set_user("Administrator")
	target = "621301 - تعدیلات موجودی کالا - E"
	doc = frappe.get_doc("Stock Entry", name)
	changed = 0
	for row in doc.items:
		if row.expense_account != target:
			row.expense_account = target
			changed += 1
	doc.save()
	frappe.db.commit()
	return {"name": name, "rows_updated": changed, "expense_account": target}


@frappe.whitelist()
def submit_canary_rollback(name: str) -> dict:
	"""Submit draft Manufacture SE then rollback — report SLE/GL."""
	_dev_only()
	frappe.set_user("Administrator")
	# Ensure site difference-account policy is satisfied (Desk users set this).
	prepare_canary_expense_accounts(name)
	prepare_canary_batches(name)
	frappe.db.begin()
	out: dict = {"name": name}
	try:
		doc = frappe.get_doc("Stock Entry", name)
		before = cint(doc.docstatus)
		doc.submit()
		sle = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_no": name, "is_cancelled": 0},
			fields=[
				"item_code",
				"warehouse",
				"actual_qty",
				"qty_after_transaction",
				"valuation_rate",
				"stock_value_difference",
				"voucher_detail_no",
			],
			order_by="creation, name",
		)
		gl = frappe.get_all(
			"GL Entry",
			filters={"voucher_no": name, "is_cancelled": 0},
			fields=["account", "debit", "credit", "is_cancelled"],
			order_by="account",
		)
		debit = sum(flt(r.debit) for r in gl)
		credit = sum(flt(r.credit) for r in gl)
		row_sle = []
		doc.reload()
		for row in doc.items:
			if not row.t_warehouse and not row.s_warehouse:
				continue
			matches = [s for s in sle if s.voucher_detail_no == row.name]
			svd = sum(flt(s.stock_value_difference) for s in matches)
			# incoming positive amount vs SLE stock_value_difference
			expected = flt(row.amount) if row.t_warehouse and not row.s_warehouse else -flt(row.amount)
			# For outgoing (consume), SLE svd is typically negative of issued value
			if row.s_warehouse and not row.t_warehouse:
				expected = -flt(row.amount)
			elif row.t_warehouse and not row.s_warehouse:
				expected = flt(row.amount)
			row_sle.append(
				{
					"item_code": row.item_code,
					"row_amount": flt(row.amount),
					"sle_svd": svd,
					"match": abs(svd - expected) < 0.5 or abs(svd + expected) < 0.5 or abs(abs(svd) - abs(flt(row.amount))) < 0.5,
					"exact_abs": abs(abs(svd) - abs(flt(row.amount))) < 0.5,
				}
			)
		sa_debit = sum(
			flt(r.debit)
			for r in gl
			if "تعدیل" in (r.account or "") or "Stock Adjustment" in (r.account or "") or "621301" in (r.account or "")
		)
		out.update(
			{
				"submit": "SUCCESS",
				"before_docstatus": before,
				"docstatus": cint(doc.docstatus),
				"contract_version": doc.get("custom_manufacturing_costing_contract_version"),
				"fg": {
					"rate": flt(next((i.basic_rate for i in doc.items if i.item_code == FG and i.is_finished_item), 0)),
					"amount": flt(next((i.basic_amount for i in doc.items if i.item_code == FG and i.is_finished_item), 0)),
					"additional_cost": flt(
						next((i.additional_cost for i in doc.items if i.item_code == FG and i.is_finished_item), 0)
					),
				},
				"by_product": {
					"rate": flt(next((i.basic_rate for i in doc.items if i.item_code == BY), 0)),
					"amount": flt(next((i.basic_amount for i in doc.items if i.item_code == BY), 0)),
				},
				"sle": sle,
				"gl": gl,
				"gl_debit": debit,
				"gl_credit": credit,
				"gl_balanced": abs(debit - credit) < 0.5,
				"sa_debit": sa_debit,
				"row_sle": row_sle,
				"row_sle_all_exact": all(r["exact_abs"] for r in row_sle),
			}
		)
	except Exception as exc:
		out.update({"submit": "FAIL", "error": str(exc)})
		raise
	finally:
		frappe.db.rollback()
	out["after_docstatus"] = cint(frappe.db.get_value("Stock Entry", name, "docstatus"))
	out["sle_after"] = frappe.db.count(
		"Stock Ledger Entry", {"voucher_no": name, "is_cancelled": 0}
	)
	out["gl_after"] = frappe.db.count("GL Entry", {"voucher_no": name, "is_cancelled": 0})
	out["rolled_back"] = True
	return out


@frappe.whitelist()
def cancel_canary_rollback(name: str) -> dict:
	"""Submit + cancel inside one transaction and rollback."""
	_dev_only()
	frappe.set_user("Administrator")
	frappe.db.begin()
	out: dict = {"name": name}
	try:
		doc = frappe.get_doc("Stock Entry", name)
		doc.submit()
		sle_sub = frappe.db.count("Stock Ledger Entry", {"voucher_no": name, "is_cancelled": 0})
		gl_sub = frappe.db.count("GL Entry", {"voucher_no": name, "is_cancelled": 0})
		doc.cancel()
		sle_live = frappe.db.count("Stock Ledger Entry", {"voucher_no": name, "is_cancelled": 0})
		gl_live = frappe.db.count("GL Entry", {"voucher_no": name, "is_cancelled": 0})
		out.update(
			{
				"cancel": "SUCCESS",
				"sle_on_submit": sle_sub,
				"gl_on_submit": gl_sub,
				"sle_live_after_cancel": sle_live,
				"gl_live_after_cancel": gl_live,
				"final_docstatus": cint(doc.docstatus),
			}
		)
	except Exception as exc:
		out.update({"cancel": "FAIL", "error": str(exc)})
		raise
	finally:
		frappe.db.rollback()
	out["rolled_back"] = True
	out["persisted_docstatus"] = cint(frappe.db.get_value("Stock Entry", name, "docstatus"))
	return out


@frappe.whitelist()
def cleanup_canary_draft(name: str) -> dict:
	_dev_only()
	frappe.set_user("Administrator")
	if not name or not frappe.db.exists("Stock Entry", name):
		return {"deleted": False}
	if cint(frappe.db.get_value("Stock Entry", name, "docstatus")) != 0:
		frappe.throw(f"Refusing to delete non-draft {name}")
	frappe.delete_doc("Stock Entry", name, force=1, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": True, "name": name}


@frappe.whitelist()
def idempotency_validate(name: str) -> dict:
	_dev_only()
	frappe.set_user("Administrator")
	from erpnext_extensions.iran_accounting.scrap_costing import apply_iran_manufacture_output_contract

	doc = frappe.get_doc("Stock Entry", name)
	snapshots = []
	for _ in range(3):
		doc.reload()
		# Re-run Iran contract only (full Document.validate needs controller _action).
		apply_iran_manufacture_output_contract(doc)
		snapshots.append(
			{
				"fg_rate": flt(next((i.basic_rate for i in doc.items if i.item_code == FG and i.is_finished_item), 0)),
				"by_rate": flt(next((i.basic_rate for i in doc.items if i.item_code == BY), 0)),
				"by_ac": flt(next((i.additional_cost for i in doc.items if i.item_code == BY), 0)),
				"fg_ac": flt(next((i.additional_cost for i in doc.items if i.item_code == FG and i.is_finished_item), 0)),
				"fg_amt": flt(next((i.basic_amount for i in doc.items if i.item_code == FG and i.is_finished_item), 0)),
				"by_amt": flt(next((i.basic_amount for i in doc.items if i.item_code == BY), 0)),
			}
		)
	return {"snapshots": snapshots, "identical": snapshots[0] == snapshots[1] == snapshots[2]}


@frappe.whitelist()
def draft_economics(name: str) -> dict:
	_dev_only()
	frappe.set_user("Administrator")
	doc = frappe.get_doc("Stock Entry", name)
	consumed = sum(flt(r.basic_amount) for r in doc.items if r.s_warehouse and not r.t_warehouse)
	scrap = sum(
		flt(r.basic_amount)
		for r in doc.items
		if r.secondary_item_type == "Scrap" and r.t_warehouse and not r.s_warehouse
	)
	fg = next(r for r in doc.items if r.is_finished_item)
	by = next(r for r in doc.items if r.item_code == BY)
	pool = consumed - scrap
	total_eq = flt(fg.custom_equivalent_qty) + flt(by.custom_equivalent_qty)
	return {
		"consumed_pool": consumed,
		"component_scrap": scrap,
		"distributable_pool": pool,
		"fg_eq_qty": flt(fg.custom_equivalent_qty),
		"by_eq_qty": flt(by.custom_equivalent_qty),
		"common_eq_rate": (pool / total_eq) if total_eq else None,
		"fg_rate": flt(fg.basic_rate),
		"fg_amount": flt(fg.basic_amount),
		"by_rate": flt(by.basic_rate),
		"by_amount": flt(by.basic_amount),
		"material_residual": pool - flt(fg.basic_amount) - flt(by.basic_amount),
		"value_difference": flt(doc.value_difference),
		"contract_version": doc.get("custom_manufacturing_costing_contract_version"),
	}
