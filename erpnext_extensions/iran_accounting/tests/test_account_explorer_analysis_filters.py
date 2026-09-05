"""Wave 3B-3 Cube Navigation — Analysis Filters contract tests."""

from __future__ import annotations

import json
import os
import subprocess
import unittest

import frappe


class TestAccountExplorerAnalysisFilters(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		app_root = frappe.get_app_path("erpnext_extensions")
		core = os.path.join(app_root, "erpnext_extensions", "page", "account_explorer", "core")
		cls.af_path = os.path.join(core, "explorer_analysis_filters.js")
		cls.af_summary_path = os.path.join(core, "explorer_analysis_filter_summary.js")
		cls.store_path = os.path.join(core, "explorer_store.js")
		cls.unit_harness = os.path.join(
			app_root,
			"iran_accounting",
			"e2e",
			"playwright",
			".local_cube_navigation_unit.cjs",
		)
		with open(cls.af_path, encoding="utf-8") as handle:
			cls.af = handle.read()
		with open(cls.af_summary_path, encoding="utf-8") as handle:
			cls.af_summary = handle.read()
		cls.af_bundle = cls.af + "\n" + cls.af_summary
		with open(cls.store_path, encoding="utf-8") as handle:
			cls.store = handle.read()

	def test_module_size_within_hard_limit(self):
		self.assertLessEqual(self.af.count("\n") + 1, 800)
		self.assertLessEqual(self.af_summary.count("\n") + 1, 800)

	def test_public_api_surface(self):
		for name in (
			"normalize_entry",
			"normalize_bag",
			"set_entry",
			"remove_entry",
			"clear",
			"evaluate_lifetimes",
			"consume_temporary",
			"apply_policy",
			"build_legacy_scope_payload_from_analysis_filters",
			"hydrate_from_legacy_scopes",
			"serialize",
			"deserialize",
			"build_summary_rows",
		):
			self.assertIn(name, self.af_bundle)

	def test_lifetimes_declared(self):
		self.assertIn('"session"', self.af)
		self.assertIn('"drill"', self.af)
		self.assertIn('"temporary"', self.af)

	def test_policies_declared(self):
		for policy in (
			"append_filter",
			"replace_filter",
			"replace_dimension",
			"keep_filters",
			"clear_drill_filters",
			"consume_temporary",
		):
			self.assertIn(f'"{policy}"', self.af)

	def test_summary_origin_and_lifetime_labels(self):
		self.assertIn("origin_label", self.af_summary)
		self.assertIn("_lifetime_label", self.af_summary)
		self.assertIn('__("Account Group")', self.af_summary)
		self.assertIn('__("Session")', self.af_summary)

	def test_store_helpers(self):
		for name in (
			"set_analysis_filter",
			"remove_analysis_filter",
			"clear_analysis_filters",
			"get_active_analysis_filters",
			"evaluate_filter_lifetimes",
			"consume_temporary_filters",
		):
			self.assertIn(name, self.store)
		self.assertIn('"analysis_filters"', self.store)

	def test_no_rendering_or_sql_in_module(self):
		banned = ("DataTable", "frappe.call", "SELECT ", "build_payload", "$(")
		for token in banned:
			self.assertNotIn(token, self.af)

	@unittest.skipUnless(
		os.path.isfile(
			os.path.join(
				frappe.get_app_path("erpnext_extensions"),
				"iran_accounting",
				"e2e",
				"playwright",
				".local_cube_navigation_unit.cjs",
			)
		),
		"local unit harness absent",
	)
	def test_node_unit_harness(self):
		result = subprocess.run(
			["node", self.unit_harness],
			capture_output=True,
			text=True,
			check=False,
		)
		if not (result.stdout or "").strip():
			self.skipTest("local unit harness produced no output")
		payload = json.loads(result.stdout)
		self.assertEqual(payload["failed"], 0)
		self.assertGreaterEqual(payload["total"], 15)
		by_name = {row["name"]: row for row in payload["results"]}
		for required in (
			"account_filter_summary_parent_code_title",
			"account_filter_summary_leaf_code_title",
			"account_summary_title_only_becomes_code_title",
		):
			self.assertIn(required, by_name, required)
			self.assertTrue(by_name[required]["ok"], by_name[required])

	def test_format_account_summary_label_api_surface(self):
		self.assertIn("format_account_summary_label", self.af_summary)
		self.assertIn("${c} - ${t}", self.af_summary)
