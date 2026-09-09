# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v5.1.7 — Material Request Connections must not query Asset Request.custom_asset_request."""

from __future__ import annotations

import unittest

import frappe
from frappe.desk.notifications import get_open_count
from frappe.utils import random_string

from erpnext_extensions.asset_usage_depreciation.tests import test_helpers as h


class TestMaterialRequestConnectionsV517(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		frappe.clear_cache(doctype="Material Request")
		frappe.clear_cache(doctype="Asset Request")
		cls.skip = h.skip_if_unready()
		if cls.skip:
			return
		h.ensure_settings(
			prevent_duplicate_active_requests=0,
			auto_create_asset_movement=0,
			auto_create_material_request=0,
		)
		cls.company = h.company()
		cls.employee = h.make_employee(company_name=cls.company)

	def _ready(self):
		if getattr(self, "skip", None):
			self.skipTest(self.skip)

	def test_dashboard_maps_asset_request_as_internal_link(self):
		self._ready()
		data = frappe.get_meta("Material Request").get_dashboard_data()
		self.assertEqual(data.internal_links.get("Asset Request"), "custom_asset_request")
		ns = data.get("non_standard_fieldnames") or {}
		self.assertNotEqual(ns.get("Asset Request"), "custom_asset_request")
		self.assertFalse(
			frappe.db.has_column("Asset Request", "custom_asset_request"),
			"Asset Request must not gain a fake custom_asset_request column",
		)
		self.assertTrue(frappe.db.has_column("Material Request", "custom_asset_request"))
		all_items = []
		for group in data.transactions or []:
			all_items.extend(group.get("items") or [])
		self.assertIn("Asset Request", all_items)

	def test_get_open_count_with_linked_asset_request(self):
		"""Reproduce production Connections call: Form/Material Request → get_open_count."""
		self._ready()
		item = h.make_fixed_asset_item(code=f"AUD-V517-{random_string(6)}")
		ar = h.make_request(company_name=self.company, employee=self.employee, item_code=item)
		h.submit_and_approve(ar)
		h.request_purchase(ar)
		ar.reload()
		self.assertTrue(ar.material_request)
		mr_name = ar.material_request
		self.assertEqual(
			frappe.db.get_value("Material Request", mr_name, "custom_asset_request"),
			ar.name,
		)

		# Must not raise OperationalError 1054 Unknown column custom_asset_request
		payload = get_open_count("Material Request", mr_name)
		internal = payload["count"]["internal_links_found"]
		hit = next((row for row in internal if row["doctype"] == "Asset Request"), None)
		self.assertIsNotNone(hit, payload)
		self.assertEqual(hit["count"], 1)
		self.assertEqual(hit["names"], [ar.name])

		# Asset Request → Material Request external connection still works
		ar_payload = get_open_count("Asset Request", ar.name)
		external = ar_payload["count"]["external_links_found"]
		mr_hit = next((row for row in external if row["doctype"] == "Material Request"), None)
		self.assertIsNotNone(mr_hit, ar_payload)
		self.assertGreaterEqual(int(mr_hit["count"] or 0), 1)

	def test_get_open_count_without_asset_request(self):
		self._ready()
		department = frappe.db.get_value(
			"Department", {"company": self.company, "is_group": 0}, "name"
		)
		cost_center = h.company_cost_center(self.company)
		mr = frappe.get_doc(
			{
				"doctype": "Material Request",
				"material_request_type": "Purchase",
				"company": self.company,
				"transaction_date": frappe.utils.nowdate(),
				"schedule_date": frappe.utils.nowdate(),
				"items": [
					{
						"item_code": h.make_fixed_asset_item(code=f"AUD-V517-U-{random_string(5)}"),
						"qty": 1,
						"schedule_date": frappe.utils.nowdate(),
						"uom": "Nos",
						"department": department,
						"cost_center": cost_center,
					}
				],
			}
		)
		mr.insert(ignore_permissions=True)
		self.assertFalse(mr.custom_asset_request)
		payload = get_open_count("Material Request", mr.name)
		internal = [
			row
			for row in payload["count"]["internal_links_found"]
			if row["doctype"] == "Asset Request"
		]
		self.assertFalse(internal)
		# External fallback must not crash (no Unknown column)
		external = payload["count"]["external_links_found"]
		self.assertIsInstance(external, list)

	def test_asset_request_dashboard_material_request_mapping_unchanged(self):
		self._ready()
		data = frappe.get_meta("Asset Request").get_dashboard_data()
		self.assertEqual(
			(data.get("non_standard_fieldnames") or {}).get("Material Request"),
			"custom_asset_request",
		)
