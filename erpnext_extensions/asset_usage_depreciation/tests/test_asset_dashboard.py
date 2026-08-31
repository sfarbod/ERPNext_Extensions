# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Asset dashboard Connections — Asset Usage Period."""

from __future__ import annotations

import unittest

import frappe
from frappe import _
from frappe.desk.notifications import get_external_links, get_open_count


class TestAssetUsageDashboard(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		# Ensure hooks are loaded for this process
		frappe.clear_cache(doctype="Asset")

	def test_dashboard_includes_usage_period_additively(self):
		data = frappe.get_meta("Asset").get_dashboard_data()
		self.assertEqual(data.non_standard_fieldnames.get("Asset Usage Period"), "asset")

		# Core groups still present
		labels = [_(g.get("label")) for g in data.transactions]
		self.assertIn(_("Movement"), labels)
		self.assertIn(_("Repair"), labels)
		self.assertIn(_("Usage"), labels)

		usage_group = next(g for g in data.transactions if _(g.get("label")) == _("Usage"))
		self.assertIn("Asset Usage Period", usage_group["items"])

		# Existing connection still listed
		all_items = []
		for g in data.transactions:
			all_items.extend(g.get("items") or [])
		self.assertIn("Asset Movement", all_items)
		self.assertIn("Asset Repair", all_items)
		self.assertIn("Asset Usage Period", all_items)

	def test_connection_count_and_filter_field(self):
		from frappe.utils import random_string

		from erpnext_extensions.asset_usage_depreciation.custom_fields import ensure_custom_fields
		from erpnext_extensions.asset_usage_depreciation.tests import test_helpers as h

		ensure_custom_fields()
		company_name = h.company()
		if not company_name:
			self.skipTest("No Company")
		if not h.ensure_asset_category():
			self.skipTest("No Asset Category")

		def make_asset(suffix):
			# Isolated company/item/category/location — this site has no
			# `_Test Company` / `Macbook Pro` / `Computers` fixtures.
			item_code = h.make_fixed_asset_item(
				code=f"AUD-Dash-Item-{suffix}-{random_string(4)}"
			)
			asset_name = f"AUD-Dash-{suffix}-{random_string(4)}"
			asset = frappe.get_doc(
				{
					"doctype": "Asset",
					"asset_name": asset_name,
					"asset_category": frappe.db.get_value("Item", item_code, "asset_category")
					or h.ensure_asset_category(),
					"item_code": item_code,
					"company": company_name,
					"purchase_date": "2026-01-01",
					"available_for_use_date": "2026-01-01",
					"calculate_depreciation": 1,
					"net_purchase_amount": 120000,
					"purchase_amount": 120000,
					"location": h.ensure_location(),
					"cost_center": h.company_cost_center(company_name),
					"asset_owner": "Company",
					"asset_type": "Existing Asset",
					"asset_quantity": 1,
					"finance_books": [
						{
							"depreciation_method": "Straight Line",
							"frequency_of_depreciation": 1,
							"total_number_of_depreciations": 12,
							"expected_value_after_useful_life": 0,
							"depreciation_start_date": "2026-01-31",
							"daily_prorata_based": 0,
							"shift_based": 0,
						}
					],
				}
			)
			asset.name = asset_name
			asset.flags.name_set = True
			asset.insert(ignore_permissions=True)
			asset.submit()
			return asset

		def make_usage(asset_name, from_date):
			doc = frappe.get_doc(
				{
					"doctype": "Asset Usage Period",
					"asset": asset_name,
					"from_date": from_date,
					"to_date": from_date,
					"depreciation_mode": "Normal",
					"reason": "dashboard test",
				}
			)
			doc.insert()
			return doc

		try:
			a = make_asset("A")
			b = make_asset("B")

			links = frappe.get_meta("Asset").get_dashboard_data()
			zero = get_external_links("Asset Usage Period", a.name, links)
			self.assertEqual(zero["count"], 0)
			self.assertEqual(zero["doctype"], "Asset Usage Period")

			make_usage(a.name, "2026-01-01")
			make_usage(a.name, "2026-02-01")
			make_usage(b.name, "2026-01-01")

			count_a = get_external_links("Asset Usage Period", a.name, links)["count"]
			count_b = get_external_links("Asset Usage Period", b.name, links)["count"]
			self.assertEqual(count_a, 2)
			self.assertEqual(count_b, 1)

			# Native open-count payload includes Asset Usage Period
			open_count = get_open_count("Asset", a.name)
			found = [
				d
				for d in open_count["count"]["external_links_found"]
				if d["doctype"] == "Asset Usage Period"
			]
			self.assertEqual(len(found), 1)
			self.assertEqual(found[0]["count"], 2)
		finally:
			frappe.db.rollback()
