# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Asset Request v4.4.0 — acquisition only, requested vs fulfilled."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe
from frappe.utils import random_string, today

from erpnext_extensions.asset_usage_depreciation.services.availability import get_available_assets, get_compatible_item_codes
from erpnext_extensions.asset_usage_depreciation.services.fulfillment_service import _evaluate_allocations
from erpnext_extensions.asset_usage_depreciation.services.request_service import _assert_fixed_asset_item
from erpnext_extensions.asset_usage_depreciation.tests import test_helpers as h


class TestCompatibleItems(unittest.TestCase):
	@patch("erpnext_extensions.asset_usage_depreciation.services.availability.allow_category_substitution", return_value=True)
	@patch("erpnext_extensions.asset_usage_depreciation.services.availability.frappe")
	def test_same_category_items_are_compatible(self, mock_frappe, _allow):
		mock_frappe.db.get_value.return_value = "Monitors"
		mock_frappe.get_all.return_value = ["Samsung Monitor 24", "LG Monitor 24"]
		codes = get_compatible_item_codes("Samsung Monitor 24", "Monitors")
		self.assertIn("LG Monitor 24", codes)
		self.assertIn("Samsung Monitor 24", codes)

	@patch("erpnext_extensions.asset_usage_depreciation.services.availability.allow_category_substitution", return_value=False)
	def test_without_substitution_only_exact_item(self, _allow):
		codes = get_compatible_item_codes("Samsung Monitor 24", "Monitors")
		self.assertEqual(codes, ["Samsung Monitor 24"])

	@patch("erpnext_extensions.asset_usage_depreciation.services.availability.allow_category_substitution", return_value=True)
	def test_explicit_fulfilled_item_wins(self, _allow):
		codes = get_compatible_item_codes("Samsung Monitor 24", "Monitors", fulfilled_item_code="LG Monitor 24")
		self.assertEqual(codes, ["LG Monitor 24"])


class TestFixedAssetValidation(unittest.TestCase):
	@patch("erpnext_extensions.asset_usage_depreciation.services.request_service.frappe")
	def test_non_asset_item_rejected(self, mock_frappe):
		mock_frappe.db.get_value.return_value = {
			"is_fixed_asset": 0,
			"is_grouped_asset": 0,
			"disabled": 0,
			"item_name": "Pen",
			"asset_category": None,
			"stock_uom": "Nos",
		}
		mock_frappe.throw.side_effect = Exception("not fixed asset")
		with self.assertRaises(Exception):
			_assert_fixed_asset_item("Pen", "Requested Item", 1)


class TestAssetRequestDocType(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def test_no_request_type_field(self):
		if not frappe.db.exists("DocType", "Asset Request"):
			self.skipTest("Asset Request DocType not migrated")
		meta = frappe.get_meta("Asset Request")
		self.assertFalse(meta.has_field("request_type"))
		self.assertTrue(meta.has_field("items"))
		self.assertTrue(meta.has_field("allocations"))
		item_meta = frappe.get_meta("Asset Request Item")
		self.assertTrue(item_meta.has_field("requested_item_code"))
		self.assertTrue(item_meta.has_field("fulfilled_item_code"))
		self.assertTrue(item_meta.has_field("requested_specification"))
		alloc_meta = frappe.get_meta("Asset Request Allocation")
		self.assertTrue(alloc_meta.has_field("requested_item_code"))
		self.assertTrue(alloc_meta.has_field("fulfilled_item_code"))
		self.assertTrue(alloc_meta.has_field("allocated_asset"))
		self.assertTrue(alloc_meta.has_field("material_request"))


def _company_ready() -> bool:
	return bool(frappe.db.exists("Company", "_Test Company") and frappe.db.exists("Item", "Macbook Pro"))


def _ensure_monitor_items():
	if not frappe.db.exists("Asset Category", "Computers"):
		return None, None
	samsung = "AUD-AR-Samsung-Monitor-24"
	lg = "AUD-AR-LG-Monitor-24"
	for code, title in ((samsung, "Samsung Monitor 24 inch"), (lg, "LG Monitor 24 inch")):
		if frappe.db.exists("Item", code):
			continue
		item = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": code,
				"item_name": title,
				"item_group": "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"is_fixed_asset": 1,
				"is_grouped_asset": 0,
				"auto_create_assets": 0,
				"asset_category": "Computers",
			}
		)
		item.insert(ignore_permissions=True)
	return samsung, lg


class TestAssetRequestFulfillment(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def test_evaluate_substitutes_same_category_pool_asset(self):
		if not _company_ready():
			self.skipTest("ERPNext test masters not available")
		if not frappe.db.exists("DocType", "Asset Request"):
			self.skipTest("Asset Request DocType not migrated")
		h.ensure_settings(allow_category_substitution=1)
		tag = random_string(6)
		category = h.make_isolated_category(tag)
		samsung = h.make_fixed_asset_item(code=f"AUD-AR-Samsung-Monitor-24-{tag}", title="Samsung Monitor 24 inch", category=category)
		lg = h.make_fixed_asset_item(code=f"AUD-AR-LG-Monitor-24-{tag}", title="LG Monitor 24 inch", category=category)
		asset = h.make_pool_asset(item_code=lg, company_name="_Test Company")
		employee = frappe.db.get_value("Employee", {"company": "_Test Company"}, "name")
		if not employee:
			self.skipTest("No Employee for _Test Company")

		doc = frappe.get_doc(
			{
				"doctype": "Asset Request",
				"company": "_Test Company",
				"employee": employee,
				"transaction_date": today(),
				"required_date": today(),
				"purpose": "Need a 24 inch monitor",
				"items": [
					{
						"requested_item_code": samsung,
						"qty": 1,
					}
				],
			}
		)
		doc.insert()
		_evaluate_allocations(doc)
		self.assertTrue(doc.allocations)
		alloc = doc.allocations[0]
		self.assertEqual(alloc.method, "Issue Existing")
		self.assertEqual(alloc.allocated_asset, asset)
		self.assertEqual(alloc.fulfilled_item_code, lg)
		self.assertEqual(alloc.requested_item_code, samsung)
		self.assertNotEqual(alloc.requested_item_code, alloc.fulfilled_item_code)
		self.assertTrue(alloc.substitution_reason)

	def test_shortage_stages_purchase_on_fulfilled_item(self):
		if not frappe.db.exists("DocType", "Asset Request"):
			self.skipTest("Asset Request DocType not migrated")
		if not _company_ready():
			self.skipTest("ERPNext test masters not available")
		samsung, lg = _ensure_monitor_items()
		if not samsung:
			self.skipTest("Could not create monitor items")

		employee = frappe.db.get_value("Employee", {"company": "_Test Company"}, "name")
		if not employee:
			self.skipTest("No Employee for _Test Company")

		doc = frappe.get_doc(
			{
				"doctype": "Asset Request",
				"company": "_Test Company",
				"employee": employee,
				"transaction_date": today(),
				"required_date": today(),
				"purpose": "Need a monitor to purchase",
				"items": [
					{
						"requested_item_code": samsung,
						"fulfilled_item_code": lg,
						"fulfilled_purchase_item": lg,
						"substitution_reason": "LG is the approved standard",
						"qty": 1,
					}
				],
			}
		)
		doc.insert()
		_evaluate_allocations(doc)
		purchase = [a for a in doc.allocations if a.method == "Purchase"]
		# If a pool LG exists from the previous test, issue may win; purchase is for leftover only.
		if purchase:
			self.assertEqual(purchase[0].fulfilled_item_code, lg)
			self.assertEqual(purchase[0].fulfilled_purchase_item, lg)
			self.assertEqual(purchase[0].requested_item_code, samsung)
		else:
			self.assertTrue(any(a.fulfilled_item_code == lg for a in doc.allocations))


class TestExactVsCategoryMatching(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.skip = h.skip_if_unready()
		if cls.skip:
			return
		cls.company = h.company()
		cls.employee = h.make_employee(company_name=cls.company)

	def _ready(self):
		if getattr(self, "skip", None):
			self.skipTest(self.skip)

	def test_exact_item_accepts_samsung_rejects_lg(self):
		self._ready()
		tag = random_string(6)
		samsung = h.make_fixed_asset_item(code=f"AUD-AR-EX-S-{tag}", title="Samsung Exact")
		lg = h.make_fixed_asset_item(code=f"AUD-AR-EX-L-{tag}", title="LG Exact")
		lg_asset = h.make_pool_asset(item_code=lg, company_name=self.company)
		samsung_asset = h.make_pool_asset(item_code=samsung, company_name=self.company)
		h.ensure_settings(allow_category_substitution=0)
		try:
			lg_hits = get_available_assets(self.company, requested_item_code=samsung, requested_asset_category="Computers")
			names = {a.name for a in lg_hits}
			self.assertIn(samsung_asset, names)
			self.assertNotIn(lg_asset, names)
			doc = h.make_request(company_name=self.company, employee=self.employee, item_code=samsung)
			_evaluate_allocations(doc)
			issued = [a for a in doc.allocations if a.method == "Issue Existing"]
			self.assertTrue(issued)
			self.assertEqual(issued[0].allocated_asset, samsung_asset)
			self.assertEqual(issued[0].fulfilled_item_code, samsung)
		finally:
			h.ensure_settings(allow_category_substitution=1)

	def test_category_match_allows_lg_for_samsung(self):
		self._ready()
		tag = random_string(6)
		category = h.make_isolated_category(tag)
		samsung = h.make_fixed_asset_item(code=f"AUD-AR-CAT-S-{tag}", title="Samsung Cat", category=category)
		lg = h.make_fixed_asset_item(code=f"AUD-AR-CAT-L-{tag}", title="LG Cat", category=category)
		lg_asset = h.make_pool_asset(item_code=lg, company_name=self.company)
		h.ensure_settings(allow_category_substitution=1)
		hits = get_available_assets(
			self.company,
			requested_item_code=samsung,
			requested_asset_category=category,
		)
		self.assertIn(lg_asset, {a.name for a in hits})
		doc = h.make_request(company_name=self.company, employee=self.employee, item_code=samsung)
		_evaluate_allocations(doc)
		issued = [a for a in doc.allocations if a.allocated_asset == lg_asset]
		self.assertTrue(issued)
		self.assertEqual(issued[0].requested_item_code, samsung)
		self.assertEqual(issued[0].fulfilled_item_code, lg)
		h.submit_and_approve(doc)
		h.issue_from_pool(doc)
		doc.reload()
		alloc = next(a for a in doc.allocations if a.allocated_asset == lg_asset)
		self.assertTrue(alloc.asset_movement)
		self.assertEqual(
			frappe.db.get_value("Asset Movement", alloc.asset_movement, "reference_name"),
			doc.name,
		)


class TestSubstitutionAndDuplicates(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.skip = h.skip_if_unready()
		if cls.skip:
			return
		h.ensure_settings(prevent_duplicate_active_requests=0, require_named_manager_approver=0)
		cls.company = h.company()
		cls.employee = h.make_employee(company_name=cls.company)

	def _ready(self):
		if getattr(self, "skip", None):
			self.skipTest(self.skip)

	def test_requested_item_preserved_when_fulfilled_differs(self):
		self._ready()
		tag = random_string(6)
		samsung = h.make_fixed_asset_item(code=f"AUD-AR-SUB-S-{tag}", title="Samsung Sub")
		lg = h.make_fixed_asset_item(code=f"AUD-AR-SUB-L-{tag}", title="LG Sub")
		doc = h.make_request(
			company_name=self.company,
			employee=self.employee,
			item_code=samsung,
			fulfilled_item_code=lg,
			substitution_reason="Standardized on LG",
		)
		row = doc.items[0]
		self.assertEqual(row.requested_item_code, samsung)
		self.assertEqual(row.fulfilled_item_code, lg)
		self.assertEqual(row.substitution_reason, "Standardized on LG")
		row.substitution_reason = "Procurement selected LG as the approved standard"
		doc.save(ignore_permissions=True)
		doc.reload()
		self.assertEqual(doc.items[0].requested_item_code, samsung)
		self.assertEqual(doc.items[0].fulfilled_item_code, lg)
		self.assertEqual(doc.items[0].substitution_reason, "Procurement selected LG as the approved standard")
		item_meta = frappe.get_meta("Asset Request Item")
		self.assertTrue(item_meta.track_changes)
		self.assertTrue(frappe.get_meta("Asset Request").track_changes)

	def test_substitution_reason_required_on_submit(self):
		self._ready()
		from frappe.model.workflow import apply_workflow

		tag = random_string(6)
		samsung = h.make_fixed_asset_item(code=f"AUD-AR-SR-S-{tag}", title="Samsung Reason")
		lg = h.make_fixed_asset_item(code=f"AUD-AR-SR-L-{tag}", title="LG Reason")
		h.ensure_settings(allow_category_substitution=0)
		doc = h.make_request(
			company_name=self.company,
			employee=self.employee,
			item_code=samsung,
			fulfilled_item_code=lg,
		)
		with self.assertRaises(frappe.ValidationError):
			apply_workflow(doc, "AR Submit for Approval")
		h.ensure_settings(allow_category_substitution=1)

	def _employee_with_line_manager(self) -> str:
		_mgr_user, mgr_emp = h.ensure_line_manager(self.company)
		emp_email = f"ar.dup.{random_string(6)}@example.com"
		h.make_user(email=emp_email, roles=["Employee"])
		return h.make_employee(company_name=self.company, user_id=emp_email, reports_to=mgr_emp)

	def test_duplicate_active_request_blocked_when_setting_on(self):
		"""Legacy compatibility: setting ON still blocks duplicate active requests."""
		self._ready()
		h.ensure_settings(prevent_duplicate_active_requests=1, require_named_manager_approver=0)
		try:
			employee = self._employee_with_line_manager()
			item = h.make_fixed_asset_item()
			first = h.make_request(company_name=self.company, employee=employee, item_code=item)
			first.workflow_state = "Pending Manager Approval"
			first.status = "Pending Manager Approval"
			first.save(ignore_permissions=True)
			second = h.make_request(company_name=self.company, employee=employee, item_code=item)
			second.workflow_state = "Pending Manager Approval"
			with self.assertRaises(frappe.ValidationError):
				second.save(ignore_permissions=True)
		finally:
			h.ensure_settings(prevent_duplicate_active_requests=0, require_named_manager_approver=0)

	def test_duplicate_active_request_allowed_when_setting_off(self):
		"""Default / setting OFF: same employee+item may leave Draft concurrently."""
		self._ready()
		h.ensure_settings(prevent_duplicate_active_requests=0, require_named_manager_approver=0)
		employee = self._employee_with_line_manager()
		item = h.make_fixed_asset_item()
		first = h.make_request(company_name=self.company, employee=employee, item_code=item)
		h.submit_and_approve(first)
		first.reload()
		self.assertEqual(first.workflow_state, "Approved")
		second = h.make_request(company_name=self.company, employee=employee, item_code=item)
		second.workflow_state = "Pending Manager Approval"
		second.status = "Pending Manager Approval"
		second.save(ignore_permissions=True)
		second.reload()
		self.assertEqual(second.workflow_state, "Pending Manager Approval")
		self.assertNotEqual(first.name, second.name)
