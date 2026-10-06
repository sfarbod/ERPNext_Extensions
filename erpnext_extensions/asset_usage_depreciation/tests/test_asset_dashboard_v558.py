# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v5.5.8 — Asset Connections Asset Movement dedupe + duplicate request isolation."""

from __future__ import annotations

import unittest

import frappe
from frappe import _
from frappe.desk.notifications import get_external_links, get_open_count
from frappe.utils import cint, random_string

from erpnext_extensions.asset_usage_depreciation import asset_dashboard as aud_dashboard
from erpnext_extensions.asset_usage_depreciation.tests import test_helpers as h
from erpnext_extensions.patches.post_model_sync import (
	remove_duplicate_asset_movement_doctype_link_v558 as cleanup_patch,
)


class TestAssetDashboardV558(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		frappe.clear_cache(doctype="Asset")

	def test_settings_default_prevent_duplicate_is_off(self):
		meta = frappe.get_meta("Asset Request Settings")
		field = meta.get_field("prevent_duplicate_active_requests")
		self.assertIsNotNone(field)
		self.assertEqual(cint(field.default), 0)

	def test_dashboard_payload_asset_movement_once_under_movement(self):
		# Simulate the proven-bad merge: core Movement + duplicate Asset Movement group.
		raw = frappe._dict(
			{
				"non_standard_fieldnames": {
					"Asset Movement": "asset_name",
					"Asset Maintenance": "asset_name",
				},
				"transactions": [
					{"label": _("Movement"), "items": ["Asset Movement"]},
					{"label": "Equpment Profile", "items": ["Equipment Profile"]},
					{"label": _("Asset Movement"), "items": ["Asset Movement"]},
					{"label": _("Maintenance"), "items": ["Asset Maintenance"]},
					{"label": _("Repair"), "items": ["Asset Repair"]},
				],
				"internal_links": {},
			}
		)
		data = aud_dashboard.get_data(raw)

		self.assertEqual(data.non_standard_fieldnames.get("Asset Movement"), "asset")
		self.assertEqual(data.non_standard_fieldnames.get("Asset Usage Period"), "asset")
		self.assertEqual(
			data.internal_links.get("Asset Request"),
			["allocations", "allocated_asset"],
		)

		am_groups = [
			g for g in data.transactions if "Asset Movement" in (g.get("items") or [])
		]
		self.assertEqual(len(am_groups), 1)
		self.assertEqual(_(am_groups[0].get("label")), _("Movement"))

		labels = [_(g.get("label")) for g in data.transactions]
		self.assertIn(_("Usage"), labels)
		self.assertIn(_("Request"), labels)
		self.assertIn("Equpment Profile", labels)
		self.assertNotIn(_("Asset Movement"), labels)

		all_items = []
		for g in data.transactions:
			all_items.extend(g.get("items") or [])
		self.assertEqual(all_items.count("Asset Movement"), 1)
		self.assertIn("Asset Usage Period", all_items)
		self.assertIn("Asset Request", all_items)
		self.assertIn("Equipment Profile", all_items)
		self.assertIn("Asset Maintenance", all_items)

	def test_live_meta_dashboard_has_single_asset_movement(self):
		frappe.clear_cache(doctype="Asset")
		data = frappe.get_meta("Asset").get_dashboard_data()
		self.assertEqual(data.non_standard_fieldnames.get("Asset Movement"), "asset")
		am_groups = [
			g for g in data.transactions if "Asset Movement" in (g.get("items") or [])
		]
		self.assertEqual(len(am_groups), 1)
		self.assertEqual(_(am_groups[0].get("label")), _("Movement"))
		open_count_am = [
			d
			for d in get_open_count("Asset", _any_asset_name())["count"]["external_links_found"]
			if d["doctype"] == "Asset Movement"
		]
		self.assertEqual(len(open_count_am), 1)

	def test_asset_movement_count_uses_asset_filter(self):
		skip = h.skip_if_unready()
		if skip:
			self.skipTest(skip)
		company = h.company()
		tag = random_string(5)
		item = h.make_fixed_asset_item(code=f"AUD-Dash558-{tag}")
		asset_name = h.make_pool_asset(item_code=item, company_name=company)

		# Two draft Issue movements for the same asset (child table asset link).
		# Draft is enough for Connections count/filter; avoid double-Issue status fights.
		employee = h.make_employee(company_name=company)
		src = frappe.db.get_value("Asset", asset_name, "location") or h.ensure_location()
		for idx in range(2):
			am = frappe.get_doc(
				{
					"doctype": "Asset Movement",
					"company": company,
					"purpose": "Issue",
					"transaction_date": f"2026-0{idx + 1}-15 10:00:00",
					"assets": [
						{
							"asset": asset_name,
							"source_location": src,
							"to_employee": employee,
						}
					],
				}
			)
			am.insert(ignore_permissions=True)

		frappe.clear_cache(doctype="Asset")
		links = frappe.get_meta("Asset").get_dashboard_data()
		self.assertEqual(links.non_standard_fieldnames.get("Asset Movement"), "asset")
		count = get_external_links("Asset Movement", asset_name, links)["count"]
		self.assertGreaterEqual(count, 2)

		open_count = get_open_count("Asset", asset_name)
		am_entries = [
			d
			for d in open_count["count"]["external_links_found"]
			if d["doctype"] == "Asset Movement"
		]
		self.assertEqual(len(am_entries), 1)
		self.assertGreaterEqual(am_entries[0]["count"], 2)

		via_child = set(
			frappe.get_all(
				"Asset Movement Item",
				filters={"asset": asset_name},
				pluck="parent",
			)
		)
		self.assertGreaterEqual(len(via_child), 2)

	def test_issue_from_pool_movement_visible_on_asset_connection(self):
		skip = h.skip_if_unready()
		if skip:
			self.skipTest(skip)
		h.ensure_settings(
			prevent_duplicate_active_requests=0,
			require_named_manager_approver=0,
			auto_create_asset_movement=1,
			auto_submit_asset_movement=0,
		)
		company = h.company()
		employee = h.make_employee(company_name=company)
		tag = random_string(5)
		item = h.make_fixed_asset_item(code=f"AUD-Pool558-{tag}")
		asset_name = h.make_pool_asset(item_code=item, company_name=company)
		req = h.make_request(company_name=company, employee=employee, item_code=item)
		h.submit_and_approve(req)
		h.issue_from_pool(req)
		req.reload()
		alloc = req.allocations[0]
		self.assertEqual(alloc.allocated_asset, asset_name)
		self.assertTrue(alloc.asset_movement)

		frappe.clear_cache(doctype="Asset")
		links = frappe.get_meta("Asset").get_dashboard_data()
		self.assertEqual(links.non_standard_fieldnames.get("Asset Movement"), "asset")
		found = get_external_links("Asset Movement", asset_name, links)
		self.assertGreaterEqual(found["count"], 1)
		names = frappe.get_all(
			"Asset Movement",
			filters={"asset": asset_name},
			pluck="name",
		)
		self.assertIn(alloc.asset_movement, names)

	def test_duplicate_requests_remain_isolated_on_fulfillment(self):
		skip = h.skip_if_unready()
		if skip:
			self.skipTest(skip)
		h.ensure_settings(
			prevent_duplicate_active_requests=0,
			require_named_manager_approver=0,
			auto_create_asset_movement=1,
			auto_submit_asset_movement=0,
		)
		company = h.company()
		employee = h.make_employee(company_name=company)
		tag = random_string(5)
		item = h.make_fixed_asset_item(code=f"AUD-Iso558-{tag}")
		asset_a = h.make_pool_asset(item_code=item, company_name=company, asset_name=f"ISO-A-{tag}")
		asset_b = h.make_pool_asset(item_code=item, company_name=company, asset_name=f"ISO-B-{tag}")

		req_a = h.make_request(company_name=company, employee=employee, item_code=item)
		req_b = h.make_request(company_name=company, employee=employee, item_code=item)
		h.submit_and_approve(req_a)
		h.submit_and_approve(req_b)

		h.issue_from_pool(req_a, selections=[{"item_row": req_a.items[0].name, "asset": asset_a}])
		req_a.reload()
		req_b.reload()

		self.assertTrue(req_a.allocations)
		self.assertEqual(req_a.allocations[0].allocated_asset, asset_a)
		self.assertTrue(req_a.allocations[0].asset_movement)
		self.assertFalse(req_b.allocations)
		self.assertNotEqual(
			frappe.db.get_value("Asset Movement", req_a.allocations[0].asset_movement, "reference_name"),
			req_b.name,
		)

		# B can still be fulfilled independently with the other pool asset.
		h.issue_from_pool(req_b, selections=[{"item_row": req_b.items[0].name, "asset": asset_b}])
		req_b.reload()
		self.assertEqual(req_b.allocations[0].allocated_asset, asset_b)
		self.assertNotEqual(req_a.allocations[0].asset_movement, req_b.allocations[0].asset_movement)


class TestRemoveDuplicateAssetMovementDocTypeLinkV558(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def _insert_custom_link(
		self, *, link_doctype, group, link_fieldname, custom=1, parenttype="Customize Form"
	):
		"""Insert a DocType Link child row (Customize Form style, as in real sites)."""
		name = f"tmp-v558-{random_string(8)}"
		now = frappe.utils.now()
		frappe.db.sql(
			"""
			insert into `tabDocType Link`
				(name, creation, modified, owner, modified_by, docstatus, idx,
				 parent, parenttype, parentfield, link_doctype, link_fieldname,
				 `group`, custom, hidden, is_child_table)
			values
				(%s, %s, %s, %s, %s, 0, 99,
				 'Asset', %s, 'links', %s, %s,
				 %s, %s, 0, 0)
			""",
			(
				name,
				now,
				now,
				"Administrator",
				"Administrator",
				parenttype,
				link_doctype,
				link_fieldname,
				group,
				cint(custom),
			),
		)
		return name

	def test_patch_removes_bad_link_and_is_idempotent(self):
		bad = self._insert_custom_link(
			link_doctype="Asset Movement",
			group="Asset Movement",
			link_fieldname="asset_name",
			custom=1,
		)
		equip = self._insert_custom_link(
			link_doctype="Equipment Profile",
			group="Equpment Profile",
			link_fieldname="asset",
			custom=1,
		)
		# Different semantics — must NOT be deleted.
		other_am = self._insert_custom_link(
			link_doctype="Asset Movement",
			group="Movement Extra",
			link_fieldname="asset",
			custom=1,
		)
		try:
			frappe.clear_cache(doctype="Asset")
			cleanup_patch.execute()
			self.assertFalse(frappe.db.exists("DocType Link", bad))
			self.assertTrue(frappe.db.exists("DocType Link", equip))
			self.assertTrue(frappe.db.exists("DocType Link", other_am))

			# Idempotent second run
			cleanup_patch.execute()
			self.assertFalse(frappe.db.exists("DocType Link", bad))
			self.assertTrue(frappe.db.exists("DocType Link", equip))
			self.assertTrue(frappe.db.exists("DocType Link", other_am))
		finally:
			for name in (bad, equip, other_am):
				if frappe.db.exists("DocType Link", name):
					frappe.db.delete("DocType Link", {"name": name})
			frappe.clear_cache(doctype="Asset")

	def test_patch_noop_when_absent(self):
		# Ensure no matching bad row (either parenttype)
		existing = frappe.get_all(
			"DocType Link",
			filters={
				"parent": "Asset",
				"custom": 1,
				"link_doctype": "Asset Movement",
				"group": "Asset Movement",
				"link_fieldname": "asset_name",
			},
			pluck="name",
		)
		for name in existing:
			frappe.db.delete("DocType Link", {"name": name})
		frappe.clear_cache(doctype="Asset")
		cleanup_patch.execute()  # must not raise
		cleanup_patch.execute()
		self.assertFalse(
			frappe.get_all(
				"DocType Link",
				filters={
					"parent": "Asset",
					"custom": 1,
					"link_doctype": "Asset Movement",
					"group": "Asset Movement",
					"link_fieldname": "asset_name",
				},
			)
		)

	def test_matcher_requires_exact_signature(self):
		self.assertTrue(
			cleanup_patch._matches_bad_link(
				frappe._dict(
					parent="Asset",
					parenttype="Customize Form",
					custom=1,
					link_doctype="Asset Movement",
					group="Asset Movement",
					link_fieldname="asset_name",
				)
			)
		)
		self.assertTrue(
			cleanup_patch._matches_bad_link(
				frappe._dict(
					parent="Asset",
					parenttype="DocType",
					custom=1,
					link_doctype="Asset Movement",
					group="Asset Movement",
					link_fieldname="asset_name",
				)
			)
		)
		self.assertFalse(
			cleanup_patch._matches_bad_link(
				frappe._dict(
					parent="Asset",
					parenttype="Customize Form",
					custom=1,
					link_doctype="Asset Movement",
					group="Asset Movement",
					link_fieldname="asset",
				)
			)
		)
		self.assertFalse(
			cleanup_patch._matches_bad_link(
				frappe._dict(
					parent="Asset",
					parenttype="Customize Form",
					custom=0,
					link_doctype="Asset Movement",
					group="Asset Movement",
					link_fieldname="asset_name",
				)
			)
		)
		self.assertFalse(
			cleanup_patch._matches_bad_link(
				frappe._dict(
					parent="Asset",
					parenttype="Customize Form",
					custom=1,
					link_doctype="Equipment Profile",
					group="Equpment Profile",
					link_fieldname="asset",
				)
			)
		)


def _any_asset_name() -> str:
	name = frappe.db.get_value("Asset", {"docstatus": 1}, "name")
	if name:
		return name
	# Fallback: any asset
	name = frappe.db.get_value("Asset", {}, "name")
	if not name:
		raise unittest.SkipTest("No Asset on site")
	return name
