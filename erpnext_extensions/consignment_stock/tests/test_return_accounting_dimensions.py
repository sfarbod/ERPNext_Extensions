# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import unittest

import frappe
from frappe.utils import random_string

from erpnext_extensions.consignment_stock.accounting import (
	copy_accounting_dimensions_from_source_row,
	get_stock_entry_detail_dimension_fields,
)
from erpnext_extensions.consignment_stock.api import (
	create_consignment_recognition_entry,
	make_consignment_return_from_receipt,
)
from erpnext_extensions.consignment_stock.material_loan.api import (
	create_material_loan_recognition_entry,
	make_material_loan_return_from_issue,
)
from erpnext_extensions.consignment_stock.material_loan.constants import F_ISSUE_DETAIL, F_ISSUE_SE
from erpnext_extensions.consignment_stock.tests.helpers import (
	ensure_customer,
	ensure_module_ready,
	ensure_settings,
	ensure_stock_entry_types,
	ensure_supplier,
	make_consignment_receipt,
)
from erpnext_extensions.consignment_stock.tests.material_loan_helpers import (
	ensure_material_loan_ready,
	ensure_material_loan_settings,
	ensure_material_loan_stock_entry_types,
	make_material_loan_issue,
	receive_stock,
)
from erpnext_extensions.iran_accounting.e2e_bootstrap import ensure_test_item, get_irr_company


def _pick_department(company: str) -> str:
	name = frappe.db.get_value("Department", {"company": company}, "name", order_by="modified desc")
	if not name:
		frappe.throw(f"No Department for company {company}")
	return name


def _pick_cost_center(company: str) -> str:
	name = frappe.db.get_value(
		"Cost Center", {"company": company, "is_group": 0}, "name", order_by="modified desc"
	)
	if not name:
		frappe.throw(f"No Cost Center for company {company}")
	return name


def _pick_project(company: str) -> str | None:
	return frappe.db.get_value("Project", {"company": company}, "name") or frappe.db.get_value(
		"Project", {}, "name"
	)


def _ensure_ar_qa_region(company: str) -> str | None:
	if not frappe.get_meta("Stock Entry Detail").has_field("ar_qa_region"):
		return None
	if not frappe.db.exists("DocType", "AR QA Region"):
		return None
	existing = frappe.db.get_value(
		"AR QA Region", {"company": company, "disabled": 0}, "name"
	) or frappe.db.get_value("AR QA Region", {"disabled": 0}, "name")
	if existing:
		return existing
	doc = frappe.get_doc(
		{
			"doctype": "AR QA Region",
			"region_name": f"EE-DIM-{random_string(5)}",
			"company": company,
			"disabled": 0,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


class TestReturnAccountingDimensionPropagation(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		ensure_module_ready()
		ensure_material_loan_ready()
		cls.company = get_irr_company("ESPAD")
		cls.settings = frappe.get_doc("Consignment Stock Settings", ensure_settings(cls.company))
		cls.types = ensure_stock_entry_types()
		cls.supplier = (
			frappe.db.get_value("Supplier", {}, "name", order_by="modified desc")
			or ensure_supplier(cls.company)
		)
		cls.wh = cls.settings.default_consignment_warehouse
		cls.department = _pick_department(cls.company)
		cls.cost_center = _pick_cost_center(cls.company)
		cls.project = _pick_project(cls.company)
		cls.ar_qa_region = _ensure_ar_qa_region(cls.company)
		cls.ml_types = ensure_material_loan_stock_entry_types()
		_, _ml_accounts, cls.ml_wh = ensure_material_loan_settings(cls.company)
		cls.customer = (
			frappe.db.get_value("Customer", {}, "name", order_by="modified desc")
			or ensure_customer(cls.company)
		)

	def _dims(self, **overrides) -> dict:
		dims = {
			"department": self.department,
			"cost_center": self.cost_center,
		}
		if self.project:
			dims["project"] = self.project
		if self.ar_qa_region:
			dims["ar_qa_region"] = self.ar_qa_region
		dims.update(overrides)
		return dims

	def _recognized_receipt(self, prefix: str, *, qty=10, rate=1000, item_dimensions=None, multi=False):
		item = ensure_test_item(self.company, prefix)
		dims = item_dimensions if item_dimensions is not None else self._dims()
		se = make_consignment_receipt(
			company=self.company,
			warehouse=self.wh,
			item_code=item,
			qty=qty,
			rate=rate,
			party_type="Supplier",
			party=self.supplier,
			stock_entry_type=self.types["receipt"],
			submit=False,
			item_dimensions=dims,
		)
		if multi:
			item2 = ensure_test_item(self.company, prefix + "B")
			dept2 = frappe.db.get_value(
				"Department",
				{"company": self.company, "name": ("!=", self.department)},
				"name",
			) or self.department
			cc2 = frappe.db.get_value(
				"Cost Center",
				{"company": self.company, "is_group": 0, "name": ("!=", self.cost_center)},
				"name",
			) or self.cost_center
			se.append(
				"items",
				{
					"item_code": item2,
					"qty": qty,
					"transfer_qty": qty,
					"basic_rate": rate,
					"t_warehouse": self.wh,
					"conversion_factor": 1,
					"uom": frappe.db.get_value("Item", item2, "stock_uom"),
					"stock_uom": frappe.db.get_value("Item", item2, "stock_uom"),
					"set_basic_rate_manually": 1,
					"department": dept2,
					"cost_center": cc2,
				},
			)
			se._multi_dims = (dims, {"department": dept2, "cost_center": cc2})
		se.submit()
		out = create_consignment_recognition_entry(se.name)
		frappe.get_doc("Journal Entry", out["journal_entry"]).submit()
		se.reload()
		return se

	def test_helper_lists_sed_dimension_fields(self):
		fields = get_stock_entry_detail_dimension_fields()
		self.assertIn("cost_center", fields)
		self.assertIn("project", fields)
		self.assertIn("department", fields)
		self.assertNotIn("item_code", fields)

	def test_helper_does_not_overwrite_target(self):
		source = frappe._dict(department="A", cost_center="CC-A")
		target = {"department": "B"}
		copy_accounting_dimensions_from_source_row(source, target)
		self.assertEqual(target["department"], "B")
		self.assertEqual(target["cost_center"], "CC-A")

	def test_consignment_return_preserves_dimensions_and_inserts(self):
		"""Original failure shape: mandatory Department + Cost Center on source."""
		receipt = self._recognized_receipt("CS-DIM-A")
		src = receipt.items[0]
		self.assertTrue(src.department)
		self.assertTrue(src.cost_center)

		out = make_consignment_return_from_receipt(receipt.name)
		ret = frappe.get_doc("Stock Entry", out["name"])
		self.assertEqual(ret.docstatus, 0)
		self.assertEqual(len(ret.items), 1)
		row = ret.items[0]
		self.assertEqual(row.department, src.department)
		self.assertEqual(row.cost_center, src.cost_center)
		if self.project:
			self.assertEqual(row.project, src.project)
		if self.ar_qa_region:
			self.assertEqual(row.ar_qa_region, src.ar_qa_region)

	def test_consignment_partial_return_preserves_dimensions(self):
		receipt = self._recognized_receipt("CS-DIM-P", qty=10)
		# Consume remaining via API then leave partial by reducing after? API returns full remaining.
		# Simulate partial: call API after manually creating a partial return of 4.
		from erpnext_extensions.consignment_stock.tests.helpers import make_consignment_return

		make_consignment_return(
			company=self.company,
			warehouse=self.wh,
			item_code=receipt.items[0].item_code,
			qty=4,
			party_type="Supplier",
			party=self.supplier,
			stock_entry_type=self.types["return"],
			receipt_name=receipt.name,
			receipt_detail=receipt.items[0].name,
			submit=True,
		)
		out = make_consignment_return_from_receipt(receipt.name)
		ret = frappe.get_doc("Stock Entry", out["name"])
		self.assertEqual(float(ret.items[0].qty), 6.0)
		self.assertEqual(ret.items[0].department, receipt.items[0].department)
		self.assertEqual(ret.items[0].cost_center, receipt.items[0].cost_center)

	def test_consignment_multi_row_dimensions_independent(self):
		receipt = self._recognized_receipt("CS-DIM-M", multi=True)
		self.assertEqual(len(receipt.items), 2)
		out = make_consignment_return_from_receipt(receipt.name)
		ret = frappe.get_doc("Stock Entry", out["name"])
		self.assertEqual(len(ret.items), 2)
		by_item = {r.item_code: r for r in ret.items}
		for src in receipt.items:
			row = by_item[src.item_code]
			self.assertEqual(row.department, src.department)
			self.assertEqual(row.cost_center, src.cost_center)

	def test_material_loan_return_preserves_dimensions_and_refs(self):
		item = ensure_test_item(self.company, "ML-DIM-A")
		receive_stock(
			company=self.company,
			warehouse=self.ml_wh,
			item_code=item,
			qty=10,
			rate=100,
		)
		issue = make_material_loan_issue(
			company=self.company,
			warehouse=self.ml_wh,
			item_code=item,
			qty=10,
			party_type="Customer",
			party=self.customer,
			stock_entry_type=self.ml_types["issue"],
			submit=False,
			item_dimensions=self._dims(),
		)
		issue.submit()
		out_je = create_material_loan_recognition_entry(issue.name)
		frappe.get_doc("Journal Entry", out_je["journal_entry"]).submit()
		issue.reload()

		out = make_material_loan_return_from_issue(issue.name)
		ret = frappe.get_doc("Stock Entry", out["name"])
		self.assertEqual(ret.docstatus, 0)
		self.assertEqual(len(ret.items), 1)
		row = ret.items[0]
		src = issue.items[0]
		self.assertEqual(row.department, src.department)
		self.assertEqual(row.cost_center, src.cost_center)
		if self.project:
			self.assertEqual(row.project, src.project)
		if self.ar_qa_region:
			self.assertEqual(row.ar_qa_region, src.ar_qa_region)
		self.assertEqual(row.get(F_ISSUE_SE), issue.name)
		self.assertEqual(row.get(F_ISSUE_DETAIL), src.name)

	def test_original_receipt_shape_mat_ste_scenario(self):
		"""Regression for MAT-STE-2026-24771 failure shape."""
		receipt = self._recognized_receipt(
			"CS-DIM-24771",
			item_dimensions={
				"department": self.department,
				"cost_center": self.cost_center,
			},
		)
		df = frappe.get_meta("Stock Entry Detail").get_field("department")
		self.assertTrue(df and df.reqd, "Department must be mandatory for this regression")
		out = make_consignment_return_from_receipt(receipt.name)
		ret = frappe.get_doc("Stock Entry", out["name"])
		src = receipt.items[0]
		self.assertEqual(ret.items[0].department, src.department)
		self.assertEqual(ret.items[0].cost_center, src.cost_center)
		self.assertTrue(ret.items[0].department)
		self.assertTrue(ret.items[0].cost_center)
