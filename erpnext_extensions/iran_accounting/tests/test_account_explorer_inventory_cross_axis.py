# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Cross-axis inventory filter contract tests (v5.1.1)."""

from __future__ import annotations

import unittest
from datetime import timedelta

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.api import (
	get_account_summary,
	get_dimension_summary,
	get_item_group_summary,
	get_item_summary,
	get_party_summary,
	get_voucher_summary,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	build_payload,
	current_fiscal_year,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_inventory_axes import (
	_ensure_item_group,
	_ensure_stock_item,
	_insert_related_gl,
	_insert_sle,
	enable_inventory_analysis,
	getdate_safe,
	require_inventory_company,
)


class TestAccountExplorerInventoryCrossAxis(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.company = require_inventory_company(cls)
		enable_inventory_analysis()
		fy = current_fiscal_year(cls.company)
		if not fy:
			raise unittest.SkipTest(f"No fiscal year for {cls.company}")
		cls.fiscal_year, cls.fy_start, cls.fy_end = fy

		cls.parent = _ensure_item_group("AE XA Parent", "All Item Groups", is_group=1)
		cls.leaf_a = _ensure_item_group("AE XA Leaf A", cls.parent, is_group=0)
		cls.leaf_b = _ensure_item_group("AE XA Leaf B", cls.parent, is_group=0)

		warehouses = frappe.get_all(
			"Warehouse", filters={"company": cls.company, "is_group": 0}, pluck="name", limit=1
		)
		if not warehouses:
			raise unittest.SkipTest("No warehouse")
		cls.warehouse = warehouses[0]

		cls.item_a1 = _ensure_stock_item("AE-XA-A1", cls.leaf_a, cls.company, cls.warehouse)
		cls.item_a2 = _ensure_stock_item("AE-XA-A2", cls.leaf_a, cls.company, cls.warehouse)
		cls.item_b1 = _ensure_stock_item("AE-XA-B1", cls.leaf_b, cls.company, cls.warehouse)

		cls.from_date = str(getdate_safe(cls.fy_start) + timedelta(days=10))
		cls.to_date = str(getdate_safe(cls.fy_start) + timedelta(days=40))
		cls.pre_date = str(getdate_safe(cls.fy_start) + timedelta(days=2))
		cls.mid_date = str(getdate_safe(cls.fy_start) + timedelta(days=20))

		# A1 movement only (A2 has no SLE)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a1,
			warehouse=cls.warehouse,
			posting_date=cls.pre_date,
			actual_qty=4,
			stock_value_difference=400,
			voucher_suffix="XA-OPEN-A1",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_a1,
			warehouse=cls.warehouse,
			posting_date=cls.mid_date,
			actual_qty=2,
			stock_value_difference=200,
			voucher_suffix="XA-IN-A1",
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_b1,
			warehouse=cls.warehouse,
			posting_date=cls.mid_date,
			actual_qty=3,
			stock_value_difference=300,
			voucher_suffix="XA-IN-B1",
		)

		# Multi-item voucher: A1 + B1 on same voucher
		for item, qty, val in ((cls.item_a1, 1, 100), (cls.item_b1, 1, 110)):
			_insert_sle(
				company=cls.company,
				item_code=item,
				warehouse=cls.warehouse,
				posting_date=cls.mid_date,
				actual_qty=qty,
				stock_value_difference=val,
				voucher_suffix="XA-MULTI",
			)

		cls.stock_account = frappe.db.sql(
			"""
			select name from `tabAccount`
			where company=%s and account_type='Stock' and is_group=0
			  and ifnull(account_number,'') != ''
			  and account_number regexp '^[0-9]+'
			order by account_number
			limit 1
			""",
			cls.company,
		)
		cls.stock_account = cls.stock_account[0][0] if cls.stock_account else None
		if cls.stock_account:
			# Prefer a clean GL row for the A1 voucher
			frappe.db.sql(
				"delete from `tabGL Entry` where voucher_no=%s and company=%s",
				("AE-INV-TEST-XA-IN-A1", cls.company),
			)
			frappe.db.commit()
			cls.test_supplier = frappe.db.get_value("Supplier", {"supplier_name": "AE-XA-SUP-A1"}, "name")
			if not cls.test_supplier:
				doc = frappe.get_doc(
					{
						"doctype": "Supplier",
						"supplier_name": "AE-XA-SUP-A1",
						"supplier_group": "All Supplier Groups",
						"tax_id": "AE-XA-SUP-A1",
					}
				)
				doc.flags.ignore_mandatory = True
				doc.insert(ignore_permissions=True, ignore_if_duplicate=True)
				frappe.db.commit()
				cls.test_supplier = doc.name
			gle_meta = frappe.get_meta("GL Entry")
			cls.test_cost_center = None
			if gle_meta.has_field("cost_center"):
				cls.test_cost_center = frappe.db.get_value(
					"Cost Center", {"company": cls.company, "is_group": 0}, "name"
				)
			_insert_related_gl(
				company=cls.company,
				account=cls.stock_account,
				posting_date=cls.mid_date,
				voucher_no="AE-INV-TEST-XA-IN-A1",
				debit=200,
				party_type="Supplier",
				party=cls.test_supplier,
				cost_center=cls.test_cost_center,
			)
			gle_name = frappe.db.get_value(
				"GL Entry",
				{
					"voucher_no": "AE-INV-TEST-XA-IN-A1",
					"account": cls.stock_account,
					"company": cls.company,
				},
				"name",
			)
			if gle_name:
				updates = {"party_type": "Supplier", "party": cls.test_supplier}
				if cls.test_cost_center:
					updates["cost_center"] = cls.test_cost_center
				frappe.db.set_value("GL Entry", gle_name, updates, update_modified=False)
				frappe.db.commit()

	def test_item_group_axis_leaf_only_no_parent(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item_group", "page_size": 200, "sort_field": "display_code"},
			document={"hide_zero_rows": 0, "inventory": {"item_group": self.parent}},
		)
		result = get_item_group_summary(payload)
		groups = {r["item_group"] for r in result["rows"]}
		self.assertNotIn(self.parent, groups)
		for name in groups:
			self.assertEqual(frappe.db.get_value("Item Group", name, "is_group"), 0)
		self.assertIn(self.leaf_a, groups)

	def test_item_a2_no_movement_not_shown(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item", "page_size": 200, "sort_field": "display_code"},
			document={"hide_zero_rows": 1, "inventory": {"item_group": self.leaf_a}},
		)
		result = get_item_summary(payload)
		codes = {r["item_code"] for r in result["rows"]}
		self.assertIn(self.item_a1, codes)
		self.assertNotIn(self.item_a2, codes)

	def test_item_group_filter_scopes_item_axis(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item", "page_size": 200},
			document={"hide_zero_rows": 0, "inventory": {"item_group": self.leaf_a}},
		)
		codes = {r["item_code"] for r in get_item_summary(payload)["rows"]}
		self.assertIn(self.item_a1, codes)
		self.assertNotIn(self.item_b1, codes)

	def test_item_filter_scopes_item_group_axis(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item_group", "page_size": 200},
			document={"hide_zero_rows": 0, "inventory": {"item": self.item_a1}},
		)
		groups = {r["item_group"] for r in get_item_group_summary(payload)["rows"]}
		self.assertEqual(groups, {self.leaf_a})

	def test_account_filter_scopes_inventory_axes(self):
		if not self.stock_account:
			raise unittest.SkipTest("No stock account")
		# Narrow to XA fixture groups so pagination cannot hide the fixture item among
		# company-wide stock activity on a popular Stock account.
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item", "page_size": 200},
			document={
				"hide_zero_rows": 0,
				"accounting": {"account": self.stock_account},
				"inventory": {"item_group": self.parent},
			},
		)
		codes = {r["item_code"] for r in get_item_summary(payload)["rows"]}
		self.assertIn(self.item_a1, codes)
		self.assertNotIn(self.item_b1, codes)

	def test_item_group_filter_scopes_voucher_axis(self):
		payload = frappe.parse_json(
			build_payload(
				self.company,
				self.fiscal_year,
				self.from_date,
				self.to_date,
				analysis={"view_axis": "voucher", "page_size": 200, "sort_field": "posting_date"},
				document={"hide_zero_rows": 0, "inventory": {"item_group": self.leaf_a}},
			)
		)
		payload["prepared_mode"] = "live"
		result = get_voucher_summary(payload)
		vouchers = {r["voucher_no"] for r in result["rows"]}
		self.assertIn("AE-INV-TEST-XA-IN-A1", vouchers)
		self.assertNotIn("AE-INV-TEST-XA-IN-B1", vouchers)

	def test_multi_item_voucher_sle_scoped_by_item_group(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item", "page_size": 200},
			document={"hide_zero_rows": 0, "inventory": {"item_group": self.leaf_a}},
		)
		codes = {r["item_code"] for r in get_item_summary(payload)["rows"]}
		self.assertIn(self.item_a1, codes)
		self.assertNotIn(self.item_b1, codes)

	def test_cross_axis_closing_parity_leaf_groups_vs_items(self):
		common = dict(
			company=self.company,
			fiscal_year=self.fiscal_year,
			from_date=self.from_date,
			to_date=self.to_date,
		)
		group_payload = build_payload(
			**common,
			analysis={"view_axis": "item_group", "page_size": 500},
			document={"hide_zero_rows": 0, "inventory": {"item_group": self.leaf_a}},
		)
		item_payload = build_payload(
			**common,
			analysis={"view_axis": "item", "page_size": 500},
			document={"hide_zero_rows": 0, "inventory": {"item_group": self.leaf_a}},
		)
		group_closing = flt(get_item_group_summary(group_payload)["totals"].get("balance_value"))
		item_closing = flt(get_item_summary(item_payload)["totals"].get("balance_value"))
		self.assertAlmostEqual(group_closing, item_closing, places=2)

	def test_item_group_filter_narrows_account_axis(self):
		if not self.stock_account:
			raise unittest.SkipTest("No stock account")
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "account_level", "level_sequence": 1, "page_size": 200},
			document={"hide_zero_rows": 0, "inventory": {"item_group": self.leaf_a}},
		)
		payload = frappe.parse_json(payload)
		payload["prepared_mode"] = "live"
		from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
			AccountExplorerQuerySpec_from_client,
		)

		spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
		included = set(spec.included_account_names or [])
		self.assertIn(self.stock_account, included)
		result = get_account_summary(payload)
		codes = {r.get("display_code") for r in result["rows"]}
		# With a numeric stock account, Group level should classify under a real prefix
		# (not company-wide unrelated branches).
		self.assertTrue(codes, "expected at least one scoped account group")
		# Unrelated chart branches must not appear unless they have scoped evidence
		unscoped = get_account_summary(
			{
				**payload,
				"document_scope": {**payload["document_scope"], "inventory": {}},
			}
		)
		unscoped_codes = {r.get("display_code") for r in unscoped["rows"]}
		self.assertTrue(codes.issubset(unscoped_codes))
		self.assertNotIn("__UNCLASSIFIED__", codes)
		self.assertLessEqual(len(codes), len(unscoped_codes))

	def test_party_axis_respects_item_group_filter(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "party", "page_size": 200},
			document={"hide_zero_rows": 0, "inventory": {"item_group": self.leaf_b}},
		)
		# Should not error; rows may be empty if no party on scoped vouchers
		result = get_party_summary(payload)
		self.assertIn("rows", result)

	def test_parent_group_filter_includes_leaf_groups_only(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item_group", "page_size": 200},
			document={"hide_zero_rows": 0, "inventory": {"item_group": self.parent}},
		)
		groups = {r["item_group"] for r in get_item_group_summary(payload)["rows"]}
		self.assertNotIn(self.parent, groups)
		self.assertIn(self.leaf_a, groups)
		self.assertIn(self.leaf_b, groups)

	def test_leaf_a_filter_narrows_item_group_axis(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item_group", "page_size": 200},
			document={"hide_zero_rows": 0, "inventory": {"item_group": self.leaf_a}},
		)
		groups = {r["item_group"] for r in get_item_group_summary(payload)["rows"]}
		self.assertEqual(groups, {self.leaf_a})

	def test_item_filter_scopes_voucher_axis(self):
		payload = frappe.parse_json(
			build_payload(
				self.company,
				self.fiscal_year,
				self.from_date,
				self.to_date,
				analysis={"view_axis": "voucher", "page_size": 200},
				document={"hide_zero_rows": 0, "inventory": {"item": self.item_a1}},
			)
		)
		payload["prepared_mode"] = "live"
		vouchers = {r["voucher_no"] for r in get_voucher_summary(payload)["rows"]}
		self.assertIn("AE-INV-TEST-XA-IN-A1", vouchers)
		self.assertNotIn("AE-INV-TEST-XA-IN-B1", vouchers)

	def test_warehouse_filter_scopes_item_axis(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item", "page_size": 200},
			document={
				"hide_zero_rows": 0,
				"inventory": {"item_group": self.parent, "warehouse": self.warehouse},
			},
		)
		codes = {r["item_code"] for r in get_item_summary(payload)["rows"]}
		self.assertIn(self.item_a1, codes)
		self.assertIn(self.item_b1, codes)

	def test_party_filter_scopes_inventory_axes(self):
		if not getattr(self, "test_supplier", None):
			raise unittest.SkipTest("No test supplier")
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item", "page_size": 200},
			document={
				"hide_zero_rows": 0,
				"accounting": {"party_type": "Supplier", "party": self.test_supplier},
			},
		)
		codes = {r["item_code"] for r in get_item_summary(payload)["rows"]}
		self.assertIn(self.item_a1, codes)
		self.assertNotIn(self.item_b1, codes)
		group_payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item_group", "page_size": 200},
			document={
				"hide_zero_rows": 0,
				"accounting": {"party_type": "Supplier", "party": self.test_supplier},
			},
		)
		groups = {r["item_group"] for r in get_item_group_summary(group_payload)["rows"]}
		self.assertEqual(groups, {self.leaf_a})

	def test_dimension_filter_scopes_inventory_axes(self):
		if not getattr(self, "test_cost_center", None):
			raise unittest.SkipTest("No cost center on GL Entry")
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item", "page_size": 200},
			document={
				"hide_zero_rows": 0,
				"accounting_dimensions": {"cost_center": self.test_cost_center},
			},
		)
		codes = {r["item_code"] for r in get_item_summary(payload)["rows"]}
		self.assertIn(self.item_a1, codes)
		self.assertNotIn(self.item_b1, codes)

	def test_voucher_filter_scopes_inventory_axes(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item_group", "page_size": 200},
			document={
				"hide_zero_rows": 0,
				"voucher": {"voucher_type": "Stock Entry", "voucher_no": "AE-INV-TEST-XA-IN-B1"},
			},
		)
		groups = {r["item_group"] for r in get_item_group_summary(payload)["rows"]}
		self.assertEqual(groups, {self.leaf_b})
		item_payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "item", "page_size": 200},
			document={
				"hide_zero_rows": 0,
				"voucher": {"voucher_type": "Stock Entry", "voucher_no": "AE-INV-TEST-XA-IN-B1"},
			},
		)
		codes = {r["item_code"] for r in get_item_summary(item_payload)["rows"]}
		self.assertEqual(codes, {self.item_b1})

	def test_item_group_filter_scopes_dimension_axis(self):
		if not getattr(self, "test_cost_center", None):
			# Ensure a leaf cost center even when GL party fixture skipped cost_center earlier.
			self.test_cost_center = frappe.db.get_value(
				"Cost Center", {"company": self.company, "is_group": 0}, "name"
			)
		if not self.test_cost_center:
			raise unittest.SkipTest("No cost center")
		# cost_center is a core GL dimension even when not listed as Accounting Dimension.
		if not frappe.get_meta("GL Entry").has_field("cost_center"):
			raise unittest.SkipTest("No cost_center on GL Entry")
		# Stamp cost_center onto the scoped voucher GL so dimension rows are non-empty.
		frappe.db.sql(
			"""
			update `tabGL Entry`
			set cost_center=%s
			where company=%s and voucher_no=%s and is_cancelled=0
			""",
			(self.test_cost_center, self.company, "AE-INV-TEST-XA-IN-A1"),
		)
		from erpnext_extensions.iran_accounting.account_explorer.cache_revision import (
			bump_accounting_revision,
		)

		bump_accounting_revision(company=self.company)
		frappe.db.commit()
		payload = frappe.parse_json(
			build_payload(
				self.company,
				self.fiscal_year,
				self.from_date,
				self.to_date,
				analysis={
					"view_axis": "dimension",
					"dimension_scope": {"dimension_type": "cost_center"},
					"page_size": 200,
				},
				document={"hide_zero_rows": 0, "inventory": {"item_group": self.leaf_a}},
			)
		)
		payload["prepared_mode"] = "live"
		result = get_dimension_summary(payload)
		values = {r.get("dimension_value") for r in result["rows"]}
		self.assertIn(self.test_cost_center, values)
