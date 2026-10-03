# Copyright (c) 2026, ERPNext Extensions contributors
"""Development-only canary / Playwright helpers for late Product Reject bridge (v5.3.37)."""

from __future__ import annotations

import frappe
from frappe.utils import cint, flt

CANARY_JC = "PO-JOB10094"
FG = "30100055"
RM = "30100054"
SCRAP_WH = "انبار ضایعات اقلام اسپاد"
EXPECTED_RATE = 738_730
EXPECTED_FG_QTY = 933
EXPECTED_REJECT_QTY = 7
EXPECTED_FG_AMT = 689_235_090
EXPECTED_REJECT_AMT = 5_171_110
EXPECTED_POOL = 694_406_200


def _dev_only():
	if frappe.conf.get("e2e_marker") != "DEV_SITE" and frappe.conf.get("developer_mode") != 1:
		frappe.throw("e2e_v5337 is development-only")


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
		"jc_modified": str(frappe.db.get_value("Job Card", CANARY_JC, "modified")),
		"wo": frappe.db.get_value("Job Card", CANARY_JC, "work_order"),
	}


def _draft_snapshot(name: str) -> dict:
	doc = frappe.get_doc("Stock Entry", name)
	items = []
	fg = reject = consume = None
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
			"s_warehouse": row.s_warehouse,
			"t_warehouse": row.t_warehouse,
			"custom_output_class": row.get("custom_output_class"),
		}
		items.append(payload)
		if cint(row.is_finished_item) and row.item_code == FG:
			fg = payload
		elif row.secondary_item_type == "Scrap" and row.item_code == FG:
			reject = payload
		elif row.s_warehouse and row.item_code == RM:
			consume = payload
	return {
		"name": doc.name,
		"docstatus": cint(doc.docstatus),
		"purpose": doc.purpose,
		"job_card": doc.job_card,
		"contract_version": doc.get("custom_manufacturing_costing_contract_version"),
		"value_difference": flt(doc.value_difference),
		"fg": fg,
		"product_reject": reject,
		"consume": consume,
		"items": items,
		"expected": {
			"rate": EXPECTED_RATE,
			"fg_qty": EXPECTED_FG_QTY,
			"reject_qty": EXPECTED_REJECT_QTY,
			"fg_amount": EXPECTED_FG_AMT,
			"reject_amount": EXPECTED_REJECT_AMT,
			"pool": EXPECTED_POOL,
			"scrap_wh": SCRAP_WH,
		},
	}


@frappe.whitelist()
def create_canary_draft_from_job_card(auto_submit: int = 0) -> dict:
	"""PO-JOB10094 → Make Stock Entry (same Desk path). Commits Draft for Playwright."""
	_dev_only()
	frappe.set_user("Administrator")
	from erpnext_extensions.iran_accounting.monkey_patches import apply_monkey_patches

	apply_monkey_patches()
	jc = frappe.get_doc("Job Card", CANARY_JC)
	ste_dict = jc.make_stock_entry_for_semi_fg_item(auto_submit=bool(int(auto_submit or 0)))
	name = ste_dict.get("name") if isinstance(ste_dict, dict) else getattr(ste_dict, "name", None)
	if not name:
		frappe.throw("make_stock_entry_for_semi_fg_item did not return a Stock Entry name")
	frappe.db.commit()
	return _draft_snapshot(name)


@frappe.whitelist()
def create_canary_draft_rollback() -> dict:
	"""Full Draft create + economic checks inside a rolled-back transaction."""
	_dev_only()
	frappe.set_user("Administrator")
	from erpnext_extensions.iran_accounting.monkey_patches import apply_monkey_patches
	from erpnext_extensions.iran_accounting.scrap_costing import (
		CLASS_MAIN_PRODUCT_REJECT,
		classify_manufacture_outputs,
	)

	apply_monkey_patches()
	before = baseline_counts()
	frappe.db.begin()
	out: dict = {}
	try:
		jc = frappe.get_doc("Job Card", CANARY_JC)
		ste = jc.make_stock_entry_for_semi_fg_item(auto_submit=False)
		name = ste.get("name") if isinstance(ste, dict) else ste.name
		doc = frappe.get_doc("Stock Entry", name)
		snap = _draft_snapshot(name)
		classified = classify_manufacture_outputs(doc)
		reject_rows = classified[CLASS_MAIN_PRODUCT_REJECT]
		fg = snap["fg"] or {}
		rej = snap["product_reject"] or {}
		cons = snap["consume"] or {}
		out.update(
			{
				"ok": True,
				"name": name,
				"contract_version": snap["contract_version"],
				"fg_rate": fg.get("basic_rate"),
				"fg_amount": fg.get("basic_amount"),
				"fg_qty": fg.get("qty"),
				"reject_rate": rej.get("basic_rate"),
				"reject_amount": rej.get("basic_amount"),
				"reject_qty": rej.get("qty"),
				"reject_wh": rej.get("t_warehouse"),
				"reject_allow_zero": rej.get("allow_zero_valuation_rate"),
				"consume_rate": cons.get("basic_rate"),
				"consume_amount": cons.get("basic_amount"),
				"value_difference": snap["value_difference"],
				"reject_class_count": len(reject_rows),
				"economics_ok": (
					flt(fg.get("basic_rate")) == EXPECTED_RATE
					and flt(rej.get("basic_rate")) == EXPECTED_RATE
					and flt(fg.get("basic_amount")) == EXPECTED_FG_AMT
					and flt(rej.get("basic_amount")) == EXPECTED_REJECT_AMT
					and flt(cons.get("basic_amount")) == EXPECTED_POOL
					and cint(rej.get("allow_zero_valuation_rate")) == 0
					and abs(flt(snap["value_difference"])) < 0.5
				),
			}
		)
	except Exception as exc:
		out = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
	finally:
		frappe.db.rollback()
	after = baseline_counts()
	out["baseline_unchanged"] = (
		after["stock_entry"] == before["stock_entry"]
		and after["sle"] == before["sle"]
		and after["gl"] == before["gl"]
	)
	out["before"] = before
	out["after"] = after
	return out


@frappe.whitelist()
def prepare_canary_batches(name: str) -> dict:
	_dev_only()
	frappe.set_user("Administrator")
	doc = frappe.get_doc("Stock Entry", name)
	created = []
	for row in doc.items:
		if row.s_warehouse and not row.t_warehouse:
			continue
		if not frappe.db.get_value("Item", row.item_code, "has_batch_no"):
			continue
		if row.batch_no:
			row.use_serial_batch_fields = 1
			continue
		batch_id = f"V5337-{name[-5:]}-{row.idx}-{row.item_code}"
		if not frappe.db.exists("Batch", batch_id):
			frappe.get_doc(
				{
					"doctype": "Batch",
					"batch_id": batch_id,
					"item": row.item_code,
					"custom_batch_no": f"V5337{row.idx}{row.item_code[-4:]}",
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
	return {"name": name, "rows_updated": changed}


@frappe.whitelist()
def submit_canary_rollback(name: str) -> dict:
	_dev_only()
	frappe.set_user("Administrator")
	from erpnext_extensions.iran_accounting.monkey_patches import apply_monkey_patches

	apply_monkey_patches()
	prepare_canary_expense_accounts(name)
	prepare_canary_batches(name)
	frappe.db.begin()
	out: dict = {"name": name}
	try:
		doc = frappe.get_doc("Stock Entry", name)
		doc.submit()
		sle = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_no": name, "is_cancelled": 0},
			fields=[
				"item_code",
				"warehouse",
				"actual_qty",
				"valuation_rate",
				"stock_value_difference",
				"voucher_detail_no",
			],
		)
		gl = frappe.get_all(
			"GL Entry",
			filters={"voucher_no": name, "is_cancelled": 0},
			fields=["account", "debit", "credit"],
		)
		debit = sum(flt(r.debit) for r in gl)
		credit = sum(flt(r.credit) for r in gl)
		# Match Product Reject by FG item + expected qty — destination WH is
		# Job Card / Custom-14 driven (often Quarantine), not necessarily scrap.
		reject_sle = [
			s for s in sle if s.item_code == FG and flt(s.actual_qty) == EXPECTED_REJECT_QTY
		]
		fg_sle = [s for s in sle if s.item_code == FG and flt(s.actual_qty) == EXPECTED_FG_QTY]
		rej_svd = sum(flt(s.stock_value_difference) for s in reject_sle)
		sa = sum(
			flt(r.debit) - flt(r.credit)
			for r in gl
			if "تعدیل" in (r.account or "") or "621301" in (r.account or "")
		)
		doc.reload()
		out.update(
			{
				"submit": "SUCCESS",
				"docstatus": cint(doc.docstatus),
				"contract_version": doc.get("custom_manufacturing_costing_contract_version"),
				"reject_sle_qty": sum(flt(s.actual_qty) for s in reject_sle),
				"reject_sle_svd": rej_svd,
				"reject_sle_rate": flt(reject_sle[0].valuation_rate) if reject_sle else 0,
				"reject_sle_warehouse": reject_sle[0].warehouse if reject_sle else None,
				"gl_balanced": abs(debit - credit) < 0.5,
				"gl_debit": debit,
				"gl_credit": credit,
				"stock_adjustment_net": sa,
				"economics_ok": (
					bool(reject_sle)
					and bool(fg_sle)
					and abs(rej_svd - EXPECTED_REJECT_AMT) < 0.5
					and abs(flt(reject_sle[0].valuation_rate) - EXPECTED_RATE) < 0.5
					and abs(flt(fg_sle[0].stock_value_difference) - EXPECTED_FG_AMT) < 0.5
					and abs(flt(fg_sle[0].valuation_rate) - EXPECTED_RATE) < 0.5
					and abs(sa) < 0.5
					and abs(debit - credit) < 0.5
				),
			}
		)
	except Exception as exc:
		out.update({"submit": "FAIL", "error": str(exc)})
	finally:
		frappe.db.rollback()
	out["rolled_back"] = True
	out["sle_persisted"] = frappe.db.count(
		"Stock Ledger Entry", {"voucher_no": name, "is_cancelled": 0}
	)
	out["gl_persisted"] = frappe.db.count("GL Entry", {"voucher_no": name, "is_cancelled": 0})
	return out


@frappe.whitelist()
def cancel_canary_rollback(name: str) -> dict:
	_dev_only()
	frappe.set_user("Administrator")
	from erpnext_extensions.iran_accounting.monkey_patches import apply_monkey_patches

	apply_monkey_patches()
	prepare_canary_expense_accounts(name)
	prepare_canary_batches(name)
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
				"reversed": sle_live == 0 and gl_live == 0,
			}
		)
	except Exception as exc:
		out.update({"cancel": "FAIL", "error": str(exc)})
	finally:
		frappe.db.rollback()
	out["rolled_back"] = True
	return out


@frappe.whitelist()
def cleanup_canary_draft(name: str) -> dict:
	_dev_only()
	frappe.set_user("Administrator")
	if not name or not frappe.db.exists("Stock Entry", name):
		return {"deleted": False, "reason": "missing"}
	docstatus = cint(frappe.db.get_value("Stock Entry", name, "docstatus"))
	if docstatus != 0:
		return {"deleted": False, "reason": f"docstatus={docstatus}"}
	# Delete canary batches created for this draft
	for batch_id in frappe.get_all(
		"Batch", filters={"batch_id": ["like", f"V5337-{name[-5:]}-%"]}, pluck="name"
	):
		try:
			frappe.delete_doc("Batch", batch_id, force=1, ignore_permissions=True)
		except Exception:
			pass
	frappe.delete_doc("Stock Entry", name, force=1, ignore_permissions=True)
	frappe.db.commit()
	return {"deleted": True, "name": name}
