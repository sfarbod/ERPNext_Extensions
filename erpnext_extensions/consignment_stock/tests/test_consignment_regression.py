# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import unittest

import frappe
from frappe.utils import cint, today

from erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract import (
	enforce_stock_entry_ledger_contract,
)
from erpnext_extensions.iran_accounting.e2e_bootstrap import (
	enable_perpetual_inventory,
	ensure_test_item,
	get_irr_company,
	get_warehouse,
)
from erpnext_extensions.consignment_stock.tests.helpers import ensure_module_ready


def _make_standard_stock_entry(*, company, purpose, item_code, qty, rate=None, source=None, target=None):
	"""Create a standard Stock Entry (non-consignment) compatible with site AD rules."""
	se = frappe.new_doc("Stock Entry")
	se.company = company
	se.purpose = purpose
	se.stock_entry_type = purpose
	se.posting_date = today()
	row = {
		"item_code": item_code,
		"qty": qty,
		"transfer_qty": qty,
		"conversion_factor": 1,
		"uom": frappe.db.get_value("Item", item_code, "stock_uom"),
		"stock_uom": frappe.db.get_value("Item", item_code, "stock_uom"),
	}
	if target:
		row["t_warehouse"] = target
	if source:
		row["s_warehouse"] = source
	if rate is not None:
		row["basic_rate"] = rate
		row["set_basic_rate_manually"] = 1
	dept_df = frappe.get_meta("Stock Entry Detail").get_field("department")
	if dept_df and cint(dept_df.reqd):
		dept = frappe.db.get_value("Department", {"company": company}, "name")
		if dept:
			row["department"] = dept
	se.append("items", row)
	se.insert()
	return se


class TestConsignmentRegression(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		ensure_module_ready()
		cls.company = get_irr_company("ESPAD")
		enable_perpetual_inventory(cls.company)
		cls.wh = get_warehouse(cls.company)

	def test_standard_material_receipt_unchanged(self):
		item = ensure_test_item(self.company, "CS-REG-MR")
		se = _make_standard_stock_entry(
			company=self.company,
			purpose="Material Receipt",
			item_code=item,
			qty=3,
			rate=1111,
			target=self.wh,
		)
		se.submit()
		contract = enforce_stock_entry_ledger_contract(se.name, self.company, raise_on_fail=True)
		self.assertEqual(contract["status"], "PASS", contract)
		self.assertFalse(se.get("custom_is_consignment_receipt"))

	def test_standard_material_issue_unchanged(self):
		item = ensure_test_item(self.company, "CS-REG-MI")
		_make_standard_stock_entry(
			company=self.company,
			purpose="Material Receipt",
			item_code=item,
			qty=5,
			rate=2000,
			target=self.wh,
		).submit()
		se = _make_standard_stock_entry(
			company=self.company,
			purpose="Material Issue",
			item_code=item,
			qty=2,
			source=self.wh,
		)
		se.submit()
		contract = enforce_stock_entry_ledger_contract(se.name, self.company, raise_on_fail=True)
		self.assertEqual(contract["status"], "PASS", contract)
		self.assertFalse(se.get("custom_is_consignment_return"))
