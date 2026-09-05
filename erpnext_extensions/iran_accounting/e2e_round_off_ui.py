# Copyright (c) 2026, ERPNext Extensions contributors
"""Whitelisted Playwright helpers for Round Off Dimension Defaults + PR Class A/B (3.8.6).

Test-support only. Does not repair historical vouchers.

Class A Purchase Receipt fixtures temporarily:
  - skip amount rewrite from rate×qty (IRR align + post-validate force)
  so a provenance-valid residual (amount 1371, qty 7, VR 196) survives submit.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, nowdate, nowtime, random_string

from erpnext_extensions.iran_accounting.domain.irr_residual_classification import (
	evaluate_irr_rate_rounding_residual,
)
from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
	is_irr_rate_rounding_residual_gl,
)

# name -> force config (process-local; Playwright uses workers=1)
_FORCE_BY_NAME: dict[str, dict] = {}
_PATCH_STATE: dict | None = None

CLASS_A_QTY = 7
CLASS_A_AMOUNT = 1371
CLASS_A_RATE = 196


def _assert_admin():
	user = frappe.session.user
	if user in (None, "Guest"):
		frappe.throw(_("Login required"), frappe.PermissionError)
	if user != "Administrator" and "System Manager" not in frappe.get_roles(user):
		frappe.throw(_("Administrator or System Manager required"), frappe.PermissionError)


def _company_or_default(company: str | None = None) -> str:
	company = company or frappe.db.get_value("Company", {"default_currency": "IRR"}, "name")
	if not company:
		frappe.throw(_("No IRR company found for E2E"))
	return company


def _apply_force_to_doc(doc) -> None:
	cfg = _FORCE_BY_NAME.get(doc.name)
	if not cfg or doc.doctype != "Purchase Receipt":
		return
	kind = cfg.get("kind")
	for row in doc.get("items") or []:
		if kind == "class_a":
			row.qty = CLASS_A_QTY
			row.rate = CLASS_A_RATE
			row.base_rate = CLASS_A_RATE
			row.amount = CLASS_A_AMOUNT
			row.base_amount = CLASS_A_AMOUNT
			# 3.8.7: PR auth is ERPNext stock numerator (base_net + item_tax + LCV…)
			row.net_amount = CLASS_A_AMOUNT
			row.base_net_amount = CLASS_A_AMOUNT
			row.item_tax_amount = 0
			row.landed_cost_voucher_amount = 0
			row.amount_difference_with_purchase_invoice = 0
			row.conversion_factor = 1
			row.valuation_rate = CLASS_A_RATE
			row.stock_qty = CLASS_A_QTY
		elif kind == "class_b":
			row.valuation_rate = 0
			if row.get("amount") in (None, "", 0) and row.get("base_amount") not in (None, "", 0):
				row.amount = row.base_amount
			# Ensure Class B VR=0 still has a non-zero ERPNext stock numerator.
			if row.get("base_net_amount") in (None, "", 0):
				row.base_net_amount = row.get("base_amount") or row.get("amount") or 0
			if row.get("net_amount") in (None, "", 0):
				row.net_amount = row.base_net_amount
	# Keep header totals consistent with Class A residual fixture
	if kind == "class_a":
		for field in (
			"total",
			"net_total",
			"base_total",
			"base_net_total",
			"grand_total",
			"base_grand_total",
			"rounded_total",
			"base_rounded_total",
			"total_qty",
		):
			if doc.meta.has_field(field):
				if field == "total_qty":
					doc.set(field, CLASS_A_QTY)
				else:
					doc.set(field, CLASS_A_AMOUNT)


def _ensure_force_patches_installed():
	"""Install once: align noop for registered vouchers + validate/on_submit force."""
	global _PATCH_STATE
	if _PATCH_STATE is not None:
		return

	import erpnext_extensions.iran_accounting.buying_selling as buying_selling
	import erpnext_extensions.iran_accounting.domain.qty_rate_amount as qty_rate_amount
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import PurchaseReceipt

	orig_align = qty_rate_amount.align_purchase_receipt_item_amounts
	orig_bs_align = buying_selling.align_purchase_receipt_item_amounts
	orig_validate = PurchaseReceipt.validate
	orig_on_submit = PurchaseReceipt.on_submit

	def _align_guard(doc, method=None):
		if doc.name in _FORCE_BY_NAME:
			# Still round integer rates; do not rewrite amount from rate×qty.
			from erpnext_extensions.iran_accounting.domain import currency as rounding

			if not rounding.is_irr_company(doc.company):
				return
			ccy = rounding.get_company_currency(doc.company)
			tx = doc.currency or ccy
			for row in doc.get("items") or []:
				if row.get("rate") is not None:
					row.rate = rounding.round_monetary_rate(row.rate, tx)
				if row.get("base_rate") is not None:
					row.base_rate = rounding.round_monetary_rate(row.base_rate, ccy)
				if row.get("valuation_rate") is not None:
					row.valuation_rate = rounding.round_monetary_rate(row.valuation_rate, ccy)
			_apply_force_to_doc(doc)
			return
		return orig_align(doc)

	def _validate(self):
		orig_validate(self)
		_apply_force_to_doc(self)

	def _on_submit(self):
		_apply_force_to_doc(self)
		return orig_on_submit(self)

	qty_rate_amount.align_purchase_receipt_item_amounts = _align_guard
	buying_selling.align_purchase_receipt_item_amounts = _align_guard
	PurchaseReceipt.validate = _validate
	PurchaseReceipt.on_submit = _on_submit

	_PATCH_STATE = {
		"qty_rate_amount": qty_rate_amount,
		"buying_selling": buying_selling,
		"PurchaseReceipt": PurchaseReceipt,
		"orig_align": orig_align,
		"orig_bs_align": orig_bs_align,
		"orig_validate": orig_validate,
		"orig_on_submit": orig_on_submit,
	}


def _register_force(name: str, kind: str):
	_ensure_force_patches_installed()
	_FORCE_BY_NAME[name] = {"kind": kind}


@frappe.whitelist()
def clear_pr_force(voucher_no: str | None = None) -> dict:
	"""Drop force registration (after cleanup)."""
	_assert_admin()
	if voucher_no:
		_FORCE_BY_NAME.pop(voucher_no, None)
	else:
		_FORCE_BY_NAME.clear()
	return {"ok": True, "remaining": list(_FORCE_BY_NAME)}


@frappe.whitelist()
def ensure_round_off_ui_prerequisites(company: str | None = None) -> dict:
	"""Ensure Department Accounting Dimension exists and is mandatory-for-PL on company."""
	_assert_admin()
	company = _company_or_default(company)

	created_ad = False
	if not frappe.db.exists("Accounting Dimension", {"document_type": "Department"}):
		ad = frappe.get_doc(
			{
				"doctype": "Accounting Dimension",
				"document_type": "Department",
				"label": "Department",
			}
		)
		ad.insert(ignore_permissions=True)
		created_ad = True
		frappe.clear_cache()

	ad_name = frappe.db.get_value("Accounting Dimension", {"document_type": "Department"}, "name")
	ad = frappe.get_doc("Accounting Dimension", ad_name)
	detail = None
	for row in ad.dimension_defaults or []:
		if row.company == company:
			detail = row
			break
	snapshot = {
		"company": company,
		"ad_name": ad_name,
		"created_ad": created_ad,
		"mandatory_for_pl": 0,
		"mandatory_for_bs": 0,
		"default_dimension": None,
	}
	if detail:
		snapshot["mandatory_for_pl"] = cint(detail.mandatory_for_pl)
		snapshot["mandatory_for_bs"] = cint(detail.mandatory_for_bs)
		snapshot["default_dimension"] = detail.default_dimension
		# Prefer row-level update: full Accounting Dimension.save() re-validates
		# every child link and can fail closed on stale/duplicate company rows in
		# long-lived Desk sessions (Playwright). Do not change accounting policy.
		frappe.db.set_value(
			"Accounting Dimension Detail",
			detail.name,
			{
				"mandatory_for_pl": 1,
				"mandatory_for_bs": 0,
				"default_dimension": None,
			},
			update_modified=False,
		)
	else:
		if not frappe.db.exists("Company", company):
			frappe.throw(_("Company {0} not found").format(frappe.bold(company)))
		ad.append(
			"dimension_defaults",
			{
				"company": company,
				"mandatory_for_pl": 1,
				"mandatory_for_bs": 0,
				"default_dimension": None,
			},
		)
		ad.flags.ignore_permissions = True
		ad.save()
	frappe.clear_cache()
	frappe.db.commit()

	dept = None
	# Site Server Script on this bench requires this exact Department on PR rows.
	if frappe.db.exists("Department", "واحد انبار - E"):
		dept = "واحد انبار - E"
	if not dept:
		dept = frappe.db.get_value("Department", {"company": company}, "name", order_by="creation asc")
	if not dept:
		dept = frappe.db.get_value("Department", {}, "name", order_by="creation asc")
	if not dept:
		d = frappe.get_doc(
			{
				"doctype": "Department",
				"department_name": f"E2E RO Dept {random_string(4)}",
				"company": company,
			}
		)
		d.insert(ignore_permissions=True)
		dept = d.name
		frappe.db.commit()

	ro = frappe.db.get_value(
		"Company",
		company,
		["round_off_account", "round_off_cost_center", "stock_adjustment_account"],
		as_dict=True,
	)
	return {
		"company": company,
		"department": dept,
		"round_off_account": ro.round_off_account,
		"round_off_cost_center": ro.round_off_cost_center,
		"stock_adjustment_account": ro.stock_adjustment_account,
		"ad_snapshot": snapshot,
		"child_rows_before": frappe.get_all(
			"Round Off Dimension Default",
			filters={"parent": company, "parenttype": "Company"},
			fields=["name", "accounting_dimension", "reference_doctype", "default_value", "idx"],
			order_by="idx asc",
		),
	}


@frappe.whitelist()
def restore_round_off_ui_prerequisites(payload: str | dict) -> dict:
	"""Restore AD mandatory flags and Company child rows to snapshot.

	Always clears process-local PR force registrations.
	"""
	_assert_admin()
	data = frappe.parse_json(payload) if isinstance(payload, str) else payload
	company = data.get("company")
	snap = data.get("ad_snapshot") or {}
	ad_name = snap.get("ad_name")
	if ad_name and frappe.db.exists("Accounting Dimension", ad_name):
		ad = frappe.get_doc("Accounting Dimension", ad_name)
		for row in ad.dimension_defaults or []:
			if row.company != company:
				continue
			# Prefer snapshot; if AD was created for E2E, force non-mandatory.
			if snap.get("created_ad"):
				vals = {
					"mandatory_for_pl": 0,
					"mandatory_for_bs": 0,
					"default_dimension": None,
				}
			else:
				vals = {
					"mandatory_for_pl": cint(snap.get("mandatory_for_pl")),
					"mandatory_for_bs": cint(snap.get("mandatory_for_bs")),
					"default_dimension": snap.get("default_dimension"),
				}
			frappe.db.set_value(
				"Accounting Dimension Detail",
				row.name,
				vals,
				update_modified=False,
			)

	frappe.db.sql(
		"delete from `tabRound Off Dimension Default` where parent=%s and parenttype='Company'",
		company,
	)
	for row in data.get("child_rows_before") or []:
		frappe.get_doc(
			{
				"doctype": "Round Off Dimension Default",
				"parent": company,
				"parenttype": "Company",
				"parentfield": "round_off_dimension_defaults",
				"accounting_dimension": row.get("accounting_dimension"),
				"reference_doctype": row.get("reference_doctype"),
				"default_value": row.get("default_value"),
			}
		).insert(ignore_permissions=True)

	frappe.clear_cache()
	frappe.db.commit()
	_FORCE_BY_NAME.clear()
	return {"restored": True, "company": company}



@frappe.whitelist()
def get_company_round_off_child_rows(company: str) -> list:
	_assert_admin()
	return frappe.get_all(
		"Round Off Dimension Default",
		filters={"parent": company, "parenttype": "Company"},
		fields=["name", "accounting_dimension", "reference_doctype", "default_value", "idx"],
		order_by="idx asc",
	)


@frappe.whitelist()
def get_ad_default_dimension(company: str, document_type: str = "Department") -> str | None:
	_assert_admin()
	ad = frappe.db.get_value("Accounting Dimension", {"document_type": document_type}, "name")
	if not ad:
		return None
	return frappe.db.get_value(
		"Accounting Dimension Detail",
		{"parent": ad, "company": company},
		"default_dimension",
	)


@frappe.whitelist()
def set_company_round_off_department_default(company: str, department: str | None = None) -> dict:
	"""Replace Company child table with a single Department default, or clear if empty/None."""
	_assert_admin()
	doc = frappe.get_doc("Company", company)
	doc.set("round_off_dimension_defaults", [])
	if department not in (None, ""):
		doc.append(
			"round_off_dimension_defaults",
			{
				"accounting_dimension": "department",
				"reference_doctype": "Department",
				"default_value": department,
			},
		)
	doc.flags.ignore_permissions = True
	doc.save()
	frappe.db.commit()
	return {"rows": get_company_round_off_child_rows(company)}


def _pick_pr_masters(company: str) -> dict:
	wh = frappe.db.get_value(
		"Warehouse",
		{"company": company, "is_group": 0, "disabled": 0},
		"name",
		order_by="creation asc",
	)
	item = frappe.db.get_value(
		"Item", {"is_stock_item": 1, "disabled": 0, "has_serial_no": 0, "has_batch_no": 0}, "name"
	)
	supplier = frappe.db.get_value("Supplier", {"disabled": 0}, "name")
	if not (wh and item and supplier):
		frappe.throw(_("Missing warehouse/item/supplier for PR E2E on {0}").format(company))
	return {"warehouse": wh, "item": item, "supplier": supplier}


@frappe.whitelist()
def create_class_b_pr_draft(company: str | None = None) -> dict:
	"""Draft PR: non-zero amount with valuation_rate=0 → Class B on submit/validate."""
	_assert_admin()
	company = _company_or_default(company)
	m = _pick_pr_masters(company)
	_ensure_force_patches_installed()
	pr = frappe.new_doc("Purchase Receipt")
	pr.company = company
	pr.supplier = m["supplier"]
	pr.posting_date = nowdate()
	pr.posting_time = nowtime()
	pr.set_posting_time = 1
	pr.currency = "IRR"
	pr.conversion_rate = 1
	dept = (
		"واحد انبار - E"
		if frappe.db.exists("Department", "واحد انبار - E")
		else frappe.db.get_value("Department", {"company": company}, "name")
	)
	item_row = {
		"item_code": m["item"],
		"qty": 1,
		"uom": frappe.db.get_value("Item", m["item"], "stock_uom"),
		"stock_uom": frappe.db.get_value("Item", m["item"], "stock_uom"),
		"conversion_factor": 1,
		"warehouse": m["warehouse"],
		"rate": 1_000_000,
		"amount": 1_000_000,
		"base_rate": 1_000_000,
		"base_amount": 1_000_000,
		"net_amount": 1_000_000,
		"base_net_amount": 1_000_000,
		"item_tax_amount": 0,
		"valuation_rate": 0,
	}
	if dept and pr.meta.has_field("department"):
		pr.department = dept
	if dept and frappe.get_meta("Purchase Receipt Item").has_field("department"):
		item_row["department"] = dept
	pr.append("items", item_row)
	pr.insert(ignore_permissions=True)
	_register_force(pr.name, "class_b")
	frappe.db.set_value("Purchase Receipt Item", pr.items[0].name, "valuation_rate", 0)
	frappe.db.commit()
	pr.reload()
	_apply_force_to_doc(pr)
	decision = evaluate_irr_rate_rounding_residual(pr)
	return {
		"name": pr.name,
		"docstatus": pr.docstatus,
		"decision_status": decision.status,
		"class_b_reason": decision.class_b_rows[0].get("reason") if decision.class_b_rows else None,
		"messages": decision.messages,
	}


@frappe.whitelist()
def create_class_a_pr(
	company: str | None = None,
	mode: str = "header",
	department: str | None = None,
	submit: int = 1,
) -> dict:
	"""Create Class A PR (stock auth 1371, qty 7, VR 196).

	3.8.7 authoritative stock amount is ERPNext's valuation numerator
	(base_net_amount + item_tax_amount + LCV …), not gross amount.

	mode:
	  - header: set PR.department (+ item)
	  - company_default: no dept on header; item gets Company Round Off Dimension
	    Default so ERPNext divisional-loss PL legs can post when Department is
	    mandatory_for_pl; Round Off residual still resolves via Company defaults
	  - missing: no dept anywhere → config_error on submit when dims mandatory
	"""
	_assert_admin()
	company = _company_or_default(company)
	m = _pick_pr_masters(company)
	submit = cint(submit)
	_ensure_force_patches_installed()

	pr = frappe.new_doc("Purchase Receipt")
	pr.company = company
	pr.supplier = m["supplier"]
	pr.posting_date = nowdate()
	pr.posting_time = nowtime()
	pr.set_posting_time = 1
	pr.currency = "IRR"
	pr.conversion_rate = 1
	if mode == "header" and department and pr.meta.has_field("department"):
		pr.department = department

	item_department = None
	if mode == "header" and department:
		item_department = department
	elif mode == "company_default":
		# Native ERPNext divisional loss may post to a PL expense when Class A
		# stock auth ≠ VR×stock_qty. Supply the same Department value configured
		# on Company Round Off Dimension Defaults so that leg can post without
		# inventing AD default_dimension.
		item_department = frappe.db.get_value(
			"Round Off Dimension Default",
			{
				"parent": company,
				"parenttype": "Company",
				"accounting_dimension": "department",
			},
			"default_value",
		)

	item = {
		"item_code": m["item"],
		"qty": CLASS_A_QTY,
		"uom": frappe.db.get_value("Item", m["item"], "stock_uom"),
		"stock_uom": frappe.db.get_value("Item", m["item"], "stock_uom"),
		"conversion_factor": 1,
		"warehouse": m["warehouse"],
		"rate": CLASS_A_RATE,
		"base_rate": CLASS_A_RATE,
		"amount": CLASS_A_AMOUNT,
		"base_amount": CLASS_A_AMOUNT,
		"net_amount": CLASS_A_AMOUNT,
		"base_net_amount": CLASS_A_AMOUNT,
		"item_tax_amount": 0,
		"landed_cost_voucher_amount": 0,
		"valuation_rate": CLASS_A_RATE,
		"stock_qty": CLASS_A_QTY,
	}
	if item_department and frappe.get_meta("Purchase Receipt Item").has_field("department"):
		item["department"] = item_department
	pr.append("items", item)
	pr.insert(ignore_permissions=True)
	_register_force(pr.name, "class_a")
	pr.reload()
	_apply_force_to_doc(pr)
	pr.flags.ignore_validate_update_after_submit = True
	# Persist forced stock-auth fields without full validate rewrite
	frappe.db.sql(
		"""
		update `tabPurchase Receipt Item`
		set qty=%s, rate=%s, base_rate=%s, amount=%s, base_amount=%s,
			net_amount=%s, base_net_amount=%s, item_tax_amount=0,
			landed_cost_voucher_amount=0, valuation_rate=%s, stock_qty=%s,
			conversion_factor=1
		where parent=%s
		""",
		(
			CLASS_A_QTY,
			CLASS_A_RATE,
			CLASS_A_RATE,
			CLASS_A_AMOUNT,
			CLASS_A_AMOUNT,
			CLASS_A_AMOUNT,
			CLASS_A_AMOUNT,
			CLASS_A_RATE,
			CLASS_A_QTY,
			pr.name,
		),
	)
	if item_department and frappe.get_meta("Purchase Receipt Item").has_field("department"):
		frappe.db.sql(
			"update `tabPurchase Receipt Item` set department=%s where parent=%s",
			(item_department, pr.name),
		)
	frappe.db.commit()
	pr.reload()
	_apply_force_to_doc(pr)
	decision = evaluate_irr_rate_rounding_residual(pr)
	result = {
		"name": pr.name,
		"docstatus": pr.docstatus,
		"mode": mode,
		"pre_submit_status": decision.status,
		"net_signed_debit": decision.net_signed_debit,
		"class_a": len(decision.class_a_rows),
		"class_b": len(decision.class_b_rows),
		"dimensions": decision.dimensions,
		"submit_error": None,
		"messages": decision.messages,
	}
	if submit:
		try:
			pr.submit()
			frappe.db.commit()
			pr.reload()
			result["docstatus"] = pr.docstatus
			result["ledger"] = get_pr_ledger_snapshot(pr.name)
			result["post_submit_decision"] = evaluate_irr_rate_rounding_residual(pr).status
		except Exception as e:
			frappe.db.rollback()
			result["submit_error"] = str(e)
			result["docstatus"] = frappe.db.get_value("Purchase Receipt", pr.name, "docstatus") or 0
	return result


@frappe.whitelist()
def get_pr_ledger_snapshot(voucher_no: str) -> dict:
	_assert_admin()
	company = frappe.db.get_value("Purchase Receipt", voucher_no, "company")
	ro = frappe.db.get_value("Company", company, "round_off_account")
	sa = frappe.db.get_value("Company", company, "stock_adjustment_account")
	cc = frappe.db.get_value("Company", company, "round_off_cost_center")
	fields = [
		"account",
		"debit",
		"credit",
		"cost_center",
		"remarks",
		"is_cancelled",
	]
	if frappe.get_meta("GL Entry").has_field("department"):
		fields.append("department")
	gl = frappe.get_all(
		"GL Entry",
		filters={"voucher_type": "Purchase Receipt", "voucher_no": voucher_no, "is_cancelled": 0},
		fields=fields,
	)
	sle = frappe.get_all(
		"Stock Ledger Entry",
		filters={"voucher_type": "Purchase Receipt", "voucher_no": voucher_no, "is_cancelled": 0},
		fields=["item_code", "actual_qty", "stock_value_difference", "warehouse"],
	)
	residual = [
		r for r in gl if is_irr_rate_rounding_residual_gl(r, company=company, round_off_account=ro)
	]
	debit = sum(flt(r.debit) for r in gl)
	credit = sum(flt(r.credit) for r in gl)
	return {
		"docstatus": frappe.db.get_value("Purchase Receipt", voucher_no, "docstatus"),
		"gl_count": len(gl),
		"sle_count": len(sle),
		"gl_balanced": abs(debit - credit) < 1e-6,
		"gl_debit": debit,
		"gl_credit": credit,
		"residual_rows": residual,
		"residual_count": len(residual),
		"sa_rows": [r for r in gl if r.account == sa],
		"expected_ro_account": ro,
		"expected_ro_cc": cc,
		"department_on_residual": residual[0].get("department") if residual else None,
		"cost_center_on_residual": residual[0].get("cost_center") if residual else None,
		"account_on_residual": residual[0].get("account") if residual else None,
		"sle": sle,
	}


@frappe.whitelist()
def cancel_and_delete_pr(voucher_no: str) -> dict:
	_assert_admin()
	_FORCE_BY_NAME.pop(voucher_no, None)
	if not frappe.db.exists("Purchase Receipt", voucher_no):
		return {"ok": True, "missing": True}
	doc = frappe.get_doc("Purchase Receipt", voucher_no)
	if doc.docstatus == 1:
		doc.cancel()
	frappe.delete_doc("Purchase Receipt", voucher_no, force=True, ignore_permissions=True)
	frappe.db.commit()
	return {"ok": True}


@frappe.whitelist()
def run_riv_for_pr(voucher_no: str) -> dict:
	"""Create and execute RIV for the Purchase Receipt transaction; return status + ledger."""
	_assert_admin()
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import repost

	pr = frappe.get_doc("Purchase Receipt", voucher_no)
	riv = frappe.get_doc(
		{
			"doctype": "Repost Item Valuation",
			"company": pr.company,
			"based_on": "Transaction",
			"voucher_type": "Purchase Receipt",
			"voucher_no": voucher_no,
			"posting_date": pr.posting_date,
			"posting_time": pr.posting_time,
			"allow_negative_stock": 1,
		}
	)
	riv.insert(ignore_permissions=True)
	riv.submit()
	error = None
	try:
		if hasattr(riv, "repost_now"):
			riv.repost_now()
		else:
			repost(riv)
	except Exception as e:
		error = str(e)[:800]
	riv.reload()
	if not error:
		error = (riv.get("error_log") or riv.get("error_message") or "")[:800] or None
	frappe.db.commit()
	return {
		"rivs": [
			{
				"name": riv.name,
				"status": riv.status,
				"docstatus": riv.docstatus,
				"error": error,
			}
		],
		"ledger": get_pr_ledger_snapshot(voucher_no),
	}
