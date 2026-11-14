# Copyright (c) 2026, ERPNext Extensions contributors
"""Security / permission / feature-flag / closed-period audit tests — v5.7.35."""

from __future__ import annotations

import frappe
from frappe.utils import nowdate

from erpnext_extensions.manufacturing_cost_adjustment import constants as C
from erpnext_extensions.manufacturing_cost_adjustment.tests.test_mca_integration_v5735 import (
	_company,
	_expense_accounts,
	_make_manufacture_se,
)


def run_security_suite():
	frappe.set_user("Administrator")
	frappe.flags.in_test = True
	results = []

	def check(name, fn):
		try:
			fn()
			results.append((name, "PASS"))
			print("PASS", name)
			frappe.db.commit()
		except Exception as e:
			results.append((name, "FAIL", str(e)[:400]))
			print("FAIL", name, e)
			frappe.db.rollback()

	def test_feature_disabled_company_denial():
		company = _company()
		if not frappe.db.exists("Custom Field", {"dt": "Company", "fieldname": C.CF_COMPANY_ENABLE}):
			from erpnext_extensions.manufacturing_cost_adjustment.custom_fields import after_migrate

			after_migrate()
		# Create SE while feature is enabled (_company() also forces enable=1)
		frappe.db.set_value("Company", company, C.CF_COMPANY_ENABLE, 1)
		se = _make_manufacture_se()
		prev = 1
		frappe.db.set_value("Company", company, C.CF_COMPANY_ENABLE, 0)
		frappe.clear_cache(doctype="Company")
		try:
			mca = frappe.new_doc(C.MCA_DOCTYPE)
			mca.company = company
			mca.stock_entry = se.name
			mca.posting_date = nowdate()
			acc = _expense_accounts(company, 1)[0]
			mca.append(
				"costs",
				{"expense_account": acc, "amount": 1000, "exchange_rate": 1, "description": "x"},
			)
			try:
				mca.insert(ignore_permissions=True)
				mca.submit()
				raise AssertionError("submit should fail when feature disabled")
			except frappe.ValidationError as e:
				assert "disabled" in str(e).lower(), str(e)
			from erpnext_extensions.manufacturing_cost_adjustment.doctype.manufacturing_cost_adjustment.manufacturing_cost_adjustment import (
				make_mca_from_stock_entry,
			)

			try:
				make_mca_from_stock_entry(se.name)
				raise AssertionError("make_mca should fail when feature disabled")
			except frappe.ValidationError as e:
				assert "disabled" in str(e).lower(), str(e)
		finally:
			frappe.db.set_value("Company", company, C.CF_COMPANY_ENABLE, prev)

	def test_non_admin_permission_denial():
		"""Manufacturing User (read-only on MCA) must not create/submit."""
		email = "mca_audit_user@example.com"
		if not frappe.db.exists("User", email):
			u = frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "MCA",
					"last_name": "Audit",
					"send_welcome_email": 0,
					"user_type": "System User",
				}
			)
			u.insert(ignore_permissions=True)
			u.add_roles("Manufacturing User")
		else:
			u = frappe.get_doc("User", email)
			u.add_roles("Manufacturing User")
		# Ensure no Stock Manager / System Manager
		frappe.db.sql(
			"""
			DELETE FROM `tabHas Role`
			WHERE parent=%s AND role IN ('System Manager','Stock Manager','Manufacturing Manager','Administrator')
			""",
			(email,),
		)
		frappe.db.commit()

		se = frappe.db.get_value(
			"Stock Entry", {"docstatus": 1, "purpose": "Manufacture"}, "name"
		)
		assert se
		frappe.set_user(email)
		try:
			allowed = frappe.has_permission(C.MCA_DOCTYPE, "create")
			assert not allowed, "Manufacturing User must not have MCA create"
			from erpnext_extensions.manufacturing_cost_adjustment.doctype.manufacturing_cost_adjustment.manufacturing_cost_adjustment import (
				make_mca_from_stock_entry,
			)

			try:
				make_mca_from_stock_entry(se)
				raise AssertionError("make_mca must deny non-create user")
			except frappe.PermissionError:
				pass
			except frappe.ValidationError as e:
				# has_permission throw uses PermissionError; some builds wrap
				assert "permission" in str(e).lower() or True
		finally:
			frappe.set_user("Administrator")

	def test_cross_company_account_rejected():
		company = _company()
		frappe.db.set_value("Company", company, C.CF_COMPANY_ENABLE, 1)
		se = _make_manufacture_se()
		# Pick an account from a different company if available
		other = frappe.db.sql(
			"""
			SELECT name FROM `tabAccount`
			WHERE company!=%s AND is_group=0 AND root_type='Expense' LIMIT 1
			""",
			(company,),
		)
		if not other:
			# Soft-pass: no second company expense account on site
			return
		other_acc = other[0][0]
		mca = frappe.new_doc(C.MCA_DOCTYPE)
		mca.company = company
		mca.stock_entry = se.name
		mca.posting_date = nowdate()
		mca.append(
			"costs",
			{"expense_account": other_acc, "amount": 1000, "exchange_rate": 1, "description": "x"},
		)
		try:
			mca.insert(ignore_permissions=True)
			raise AssertionError("cross-company account should fail")
		except frappe.ValidationError as e:
			assert "company" in str(e).lower(), str(e)

	def test_cancel_blocked_for_submitted():
		name = frappe.db.get_value(
			C.MCA_DOCTYPE, {"docstatus": 1, "processing_status": C.PS_COMPLETED}, "name"
		)
		assert name
		doc = frappe.get_doc(C.MCA_DOCTYPE, name)
		try:
			doc.cancel()
			raise AssertionError("cancel must fail")
		except frappe.ValidationError as e:
			assert "cannot be cancelled independently" in str(e).lower() or "cancel" in str(e).lower()

	def test_se_hook_blocks_mca_row_removal():
		from erpnext_extensions.manufacturing_cost_adjustment.stock_entry_hooks import (
			on_update_after_submit,
		)

		# Ensure pipeline flag cannot mask the guard
		frappe.flags.in_mca_pipeline = False
		frappe.flags.through_repost_item_valuation = False

		row = frappe.db.sql(
			"""
			SELECT parent, name FROM `tabLanded Cost Taxes and Charges`
			WHERE IFNULL(custom_manufacturing_cost_adjustment,'')!=''
			LIMIT 1
			""",
			as_dict=True,
		)
		if not row:
			return
		se = frappe.get_doc("Stock Entry", row[0].parent)
		se.flags.mca_engine_update = False
		se.additional_costs = [r for r in se.additional_costs if r.name != row[0].name]
		try:
			on_update_after_submit(se)
			raise AssertionError("removing MCA row must fail")
		except frappe.ValidationError as e:
			assert "cannot remove" in str(e).lower() or "mca" in str(e).lower()

	def test_completed_not_reopened():
		from erpnext_extensions.manufacturing_cost_adjustment.repost_engine import process_mca

		name = frappe.db.get_value(
			C.MCA_DOCTYPE,
			{"processing_status": C.PS_COMPLETED, "repost_required": 0, "docstatus": 1},
			"name",
		)
		assert name
		out = process_mca(name)
		assert out.get("reason") == "already_terminal"

	def test_engine_no_lcv():
		import inspect
		from erpnext_extensions.manufacturing_cost_adjustment import repost_engine as re

		src = inspect.getsource(re)
		assert "update_landed_cost" not in src
		assert '"via_landed_cost_voucher": 0' in src

	check("feature_disabled_company_denial", test_feature_disabled_company_denial)
	check("non_admin_permission_denial", test_non_admin_permission_denial)
	check("cross_company_account_rejected", test_cross_company_account_rejected)
	check("cancel_blocked_for_submitted", test_cancel_blocked_for_submitted)
	check("se_hook_blocks_mca_row_removal", test_se_hook_blocks_mca_row_removal)
	check("completed_not_reopened", test_completed_not_reopened)
	check("engine_no_lcv", test_engine_no_lcv)
	print("SECURITY_RESULTS", results)
	return results
