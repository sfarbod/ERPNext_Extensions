# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Regression: Costing sidebar Link To targets must exist before Workspace Sidebar save."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe

from erpnext_extensions.desk_navigation import (
	assert_sidebar_link_targets_exist,
	collect_sidebar_link_targets,
)
from erpnext_extensions.patches.post_model_sync.ensure_costing_english_desk_v5727 import (
	REQUIRED_COSTING_DOCTYPES,
	_SIDEBAR_ITEMS,
	_ensure_costing_doctypes,
)


class TestCostingSidebarLinkTargets(unittest.TestCase):
	def test_sidebar_items_cover_required_doctypes(self):
		targets = {
			link_to
			for link_type, link_to in collect_sidebar_link_targets(_SIDEBAR_ITEMS)
			if link_type == "DocType"
		}
		for dt in REQUIRED_COSTING_DOCTYPES:
			self.assertIn(dt, targets)

	def test_assert_passes_when_all_targets_exist(self):
		with patch(
			"erpnext_extensions.desk_navigation.frappe.db.exists", return_value=True
		):
			assert_sidebar_link_targets_exist(_SIDEBAR_ITEMS)

	def test_assert_lists_every_missing_doctype_target(self):
		def exists(doctype, name=None):
			if doctype == "DocType" and name == "MFG Cost Expense Pool":
				return None
			if doctype == "MFG Cost Expense Pool":
				return None
			return True

		with patch(
			"erpnext_extensions.desk_navigation.frappe.db.exists", side_effect=exists
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				assert_sidebar_link_targets_exist(_SIDEBAR_ITEMS)
			self.assertIn("MFG Cost Expense Pool", str(ctx.exception))

	def test_required_doctype_constant_matches_json_names(self):
		# Guard against typos that would pass unit mocks but fail migrate.
		self.assertEqual(
			REQUIRED_COSTING_DOCTYPES[0], "Finished Goods Cost Allocation Settings"
		)
		self.assertIn("Monthly FG Cost Calculation", REQUIRED_COSTING_DOCTYPES)


class TestEnsureCostingDocTypes(unittest.TestCase):
	def test_missing_file_raises_explicit_error(self):
		with patch(
			"erpnext_extensions.patches.post_model_sync.ensure_costing_english_desk_v5727.get_doc_path",
			return_value="/tmp/does-not-exist-costing-dt.json",
		), patch(
			"erpnext_extensions.patches.post_model_sync.ensure_costing_english_desk_v5727.frappe.clear_cache"
		), patch(
			"erpnext_extensions.patches.post_model_sync.ensure_costing_english_desk_v5727.frappe.db.exists",
			return_value=False,
		):
			with self.assertRaises(frappe.ValidationError) as ctx:
				_ensure_costing_doctypes()
			msg = str(ctx.exception)
			self.assertIn("Missing files", msg)
			self.assertIn("Finished Goods Cost Allocation Settings", msg)


if __name__ == "__main__":
	unittest.main()
