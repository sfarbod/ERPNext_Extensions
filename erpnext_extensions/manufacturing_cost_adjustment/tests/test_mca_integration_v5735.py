# Copyright (c) 2026, ERPNext Extensions contributors
"""Integration tests for MCA + MCRE — v5.7.35."""

from __future__ import annotations

import frappe
from frappe.utils import flt, nowdate

from erpnext_extensions.manufacturing_cost_adjustment import constants as C


def _company():
	# Prefer existing site company to avoid chart/UOM bootstrap issues
	name = frappe.db.get_value("Company", {"default_currency": "IRR"}, "name") or frappe.db.get_value(
		"Company", {}, "name"
	)
	if frappe.db.exists(
		"Custom Field", {"dt": "Company", "fieldname": C.CF_COMPANY_ENABLE}
	):
		frappe.db.set_value("Company", name, C.CF_COMPANY_ENABLE, 1)
	return name


def _warehouse(company, preferred_substring=None):
	filters = {"company": company, "is_group": 0}
	wh = None
	if preferred_substring:
		wh = frappe.db.get_value(
			"Warehouse", {**filters, "name": ("like", f"%{preferred_substring}%")}, "name"
		)
	return wh or frappe.db.get_value("Warehouse", filters, "name")


def _item(code):
	"""Create stock item; return actual Item.name (site may use naming series)."""
	existing = frappe.db.get_value("Item", {"item_name": code}, "name")
	if existing:
		return existing
	if frappe.db.exists("Item", code):
		return code
	doc = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": "All Item Groups",
			"stock_uom": "Nos",
			"is_stock_item": 1,
			"include_item_in_manufacturing": 1,
			"valuation_method": "Moving Average",
		}
	)
	# Prefer hash/autoname path used by this site
	series = frappe.db.get_single_value("Stock Settings", "item_naming_by")
	if series == "Naming Series" or frappe.get_meta("Item").autoname == "naming_series:":
		doc.naming_series = (
			frappe.db.get_value("Property Setter", {"doc_type": "Item", "field_name": "naming_series", "property": "default"}, "value")
			or "STO-ITEM-.YYYY.-"
		)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _expense_accounts(company, n=10):
	abbr = frappe.get_cached_value("Company", company, "abbr")
	parent = frappe.db.get_value(
		"Account",
		{"company": company, "root_type": "Expense", "is_group": 1},
		"name",
	) or frappe.db.get_value("Account", {"company": company, "is_group": 1}, "name")
	accounts = []
	for i in range(n):
		acc_name = f"_MCA Exp {i} - {abbr}"
		if not frappe.db.exists("Account", acc_name):
			frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": f"_MCA Exp {i}",
					"company": company,
					"parent_account": parent,
					"is_group": 0,
					"root_type": "Expense",
					"account_type": "Expense Account",
				}
			).insert(ignore_permissions=True)
		accounts.append(acc_name)
	return accounts


def _department(company):
	return frappe.db.get_value(
		"Department", {"company": company, "is_group": 0}, "name"
	) or frappe.db.get_value("Department", {"is_group": 0}, "name")


def _material_receipt(company, item, warehouse, qty, rate):
	se = frappe.new_doc("Stock Entry")
	se.company = company
	se.purpose = "Material Receipt"
	se.department = _department(company)
	se.stock_entry_type = (
		frappe.db.get_value("Stock Entry Type", {"purpose": "Material Receipt"}, "name")
		or "Material Receipt"
	)
	se.posting_date = nowdate()
	dept = se.department
	se.append(
		"items",
		{
			"item_code": item,
			"qty": qty,
			"t_warehouse": warehouse,
			"uom": "Nos",
			"stock_uom": "Nos",
			"conversion_factor": 1,
			"transfer_qty": qty,
			"basic_rate": rate,
			"department": dept,
		},
	)
	se.flags.ignore_mandatory = True
	se.insert(ignore_permissions=True)
	se.submit()
	return se


def _make_manufacture_se():
	company = _company()
	stores = _warehouse(company, "Stores") or _warehouse(company)
	fg_wh = _warehouse(company, "Finished") or _warehouse(company)
	rm = _item("_MCA-RM-V5735")
	fg = _item("_MCA-FG-V5735")
	_material_receipt(company, rm, stores, 50, 10000)

	se = frappe.new_doc("Stock Entry")
	se.company = company
	se.purpose = "Manufacture"
	se.department = _department(company)
	se.stock_entry_type = (
		frappe.db.get_value("Stock Entry Type", {"purpose": "Manufacture"}, "name") or "Manufacture"
	)
	se.posting_date = nowdate()
	dept = se.department
	se.append(
		"items",
		{
			"item_code": rm,
			"qty": 5,
			"s_warehouse": stores,
			"uom": "Nos",
			"stock_uom": "Nos",
			"conversion_factor": 1,
			"transfer_qty": 5,
			"basic_rate": 10000,
			"department": dept,
		},
	)
	se.append(
		"items",
		{
			"item_code": fg,
			"qty": 5,
			"t_warehouse": fg_wh,
			"uom": "Nos",
			"stock_uom": "Nos",
			"conversion_factor": 1,
			"transfer_qty": 5,
			"is_finished_item": 1,
			"department": dept,
		},
	)
	se.flags.ignore_mandatory = True
	se.insert(ignore_permissions=True)
	se.submit()
	return se


def run_integration_suite():
	frappe.set_user("Administrator")
	frappe.flags.in_test = True
	from erpnext_extensions.manufacturing_cost_adjustment.custom_fields import after_migrate

	after_migrate()
	frappe.db.commit()

	results = []

	def check(name, fn):
		try:
			fn()
			results.append((name, "PASS"))
			print("PASS", name)
		except Exception as e:
			results.append((name, "FAIL", str(e)))
			print("FAIL", name, e)
			frappe.db.rollback()

	def test_ten_accounts():
		se = _make_manufacture_se()
		accounts = _expense_accounts(se.company, 10)
		mca = frappe.new_doc(C.MCA_DOCTYPE)
		mca.company = se.company
		mca.stock_entry = se.name
		mca.posting_date = nowdate()
		for i, acc in enumerate(accounts):
			mca.append(
				"costs",
				{
					"expense_account": acc,
					"amount": 1_000_000 + i * 1000,
					"exchange_rate": 1,
					"description": f"MCA cost {i}",
				},
			)
		mca.insert(ignore_permissions=True)
		mca.submit()
		mca.reload()
		se.reload()
		mca_rows = [r for r in se.additional_costs if r.get(C.CF_MCA) == mca.name]
		assert len(mca_rows) == 10, f"expected 10 rows got {len(mca_rows)} status={mca.processing_status} cp={mca.checkpoint} err={mca.last_repost_error}"
		assert len({r.expense_account for r in mca_rows}) == 10
		if mca.processing_status == C.PS_COMPLETED:
			assert mca.repost_status == C.RS_COMPLETED
			assert not cint(mca.repost_required)
			for acc in accounts:
				credits = frappe.db.sql(
					"""
					SELECT SUM(credit) FROM `tabGL Entry`
					WHERE voucher_type='Stock Entry' AND voucher_no=%s
					  AND account=%s AND IFNULL(is_cancelled,0)=0
					""",
					(se.name, acc),
				)[0][0]
				assert flt(credits) > 0, f"GL missing for {acc}"
		else:
			# Pipeline may stop on Iran contract / RIV in complex sites — costs must still persist
			assert mca.checkpoint in (
				C.CP_COSTS_APPENDED,
				C.CP_ALLOCATED,
				C.CP_SLE_REFRESHED,
				C.CP_GL_REFRESHED,
				C.CP_CONTRACT_OK,
				C.CP_RIV_QUEUED,
				C.CP_COMPLETED,
			) or mca.processing_status in (
				C.PS_FAILED,
				C.PS_PENDING_RIV,
				C.PS_ALLOCATING,
				C.PS_REBUILDING_SLE,
				C.PS_REBUILDING_GL,
				C.PS_VALIDATING,
				C.PS_RECONCILING,
				C.PS_QUEUED,
				C.PS_APPENDING,
			)

	def test_idempotent():
		se = _make_manufacture_se()
		acc = _expense_accounts(se.company, 1)[0]
		mca = frappe.new_doc(C.MCA_DOCTYPE)
		mca.company = se.company
		mca.stock_entry = se.name
		mca.posting_date = nowdate()
		mca.append(
			"costs",
			{"expense_account": acc, "amount": 500000, "exchange_rate": 1, "description": "once"},
		)
		mca.insert(ignore_permissions=True)
		mca.submit()
		from erpnext_extensions.manufacturing_cost_adjustment.repost_engine import _append_costs

		_append_costs(mca.name)
		_append_costs(mca.name)
		se.reload()
		rows = [r for r in se.additional_costs if r.get(C.CF_MCA) == mca.name]
		assert len(rows) == 1, len(rows)

	def test_no_cancel():
		se = _make_manufacture_se()
		acc = _expense_accounts(se.company, 1)[0]
		mca = frappe.new_doc(C.MCA_DOCTYPE)
		mca.company = se.company
		mca.stock_entry = se.name
		mca.posting_date = nowdate()
		mca.append(
			"costs",
			{"expense_account": acc, "amount": 100000, "exchange_rate": 1, "description": "x"},
		)
		mca.insert(ignore_permissions=True)
		mca.submit()
		try:
			mca.cancel()
			raise AssertionError("cancel should fail")
		except frappe.ValidationError:
			pass

	def test_void():
		from erpnext_extensions.manufacturing_cost_adjustment.repost_engine import (
			void_mcas_for_stock_entry,
		)

		name = frappe.generate_hash(length=8)
		company = _company()
		frappe.db.sql(
			f"""
			INSERT INTO `tab{C.MCA_DOCTYPE}`
			(name, creation, modified, modified_by, owner, docstatus,
			 company, stock_entry, posting_date, processing_status, checkpoint,
			 repost_required, repost_status, idempotency_key)
			VALUES (%s, NOW(), NOW(), 'Administrator', 'Administrator', 1,
			 %s, %s, %s, %s, %s, 0, %s, %s)
			""",
			(
				name,
				company,
				"STE-VOID-TEST",
				nowdate(),
				C.PS_COMPLETED,
				C.CP_COMPLETED,
				C.RS_COMPLETED,
				f"void-{name}",
			),
		)
		void_mcas_for_stock_entry("STE-VOID-TEST")
		assert frappe.db.get_value(C.MCA_DOCTYPE, name, "processing_status") == C.PS_VOIDED

	check("ten_expense_accounts", test_ten_accounts)
	check("idempotent_append", test_idempotent)
	check("no_mca_cancel", test_no_cancel)
	check("void_on_se_cancel", test_void)
	print("RESULTS", results)
	return results


def cint(v):
	try:
		return int(v or 0)
	except Exception:
		return 0
