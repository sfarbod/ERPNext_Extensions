# Copyright (c) 2026, ERPNext Extensions contributors
"""Prepare Manufacture SE + 10 expense accounts for full Playwright MCA E2E."""

from __future__ import annotations

import frappe

from erpnext_extensions.manufacturing_cost_adjustment.tests.test_mca_integration_v5735 import (
	_expense_accounts,
	_make_manufacture_se,
)


def prepare():
	frappe.set_user("Administrator")
	frappe.flags.in_test = True
	from erpnext_extensions.manufacturing_cost_adjustment.custom_fields import after_migrate

	after_migrate()
	se = _make_manufacture_se()
	accounts = _expense_accounts(se.company, 10)
	frappe.db.commit()
	print("E2E_STOCK_ENTRY_FULL=" + se.name)
	print("E2E_EXPENSE_ACCOUNTS=" + ",".join(accounts))
	return {"se": se.name, "accounts": accounts}
