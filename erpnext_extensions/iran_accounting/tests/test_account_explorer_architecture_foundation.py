"""Wave 3B-0 architecture foundation guards."""

from __future__ import annotations

import os
import unittest

import frappe


class TestAccountExplorerArchitectureFoundation(unittest.TestCase):
	def setUp(self):
		self.app_root = frappe.get_app_path("erpnext_extensions")

	def test_adr_documents_exist(self):
		for name in (
			"ADR-3B-001-datatable.md",
			"ADR-3B-002-workspace-url.md",
			"ADR-3B-003-page-lifecycle.md",
			"ADR-3B-004-cube-navigation.md",
		):
			path = os.path.join(self.app_root, "iran_accounting", "docs", "adr", name)
			self.assertTrue(os.path.isfile(path), f"missing ADR: {name}")

	def test_core_modules_exist(self):
		base = os.path.join(
			self.app_root,
			"erpnext_extensions",
			"page",
			"account_explorer",
		)
		hard_limit_modules = {
			"core/explorer_events.js",
			"core/explorer_analysis_filters.js",
			"core/explorer_analysis_filter_summary.js",
			"core/explorer_drill_graph.js",
			"core/explorer_store.js",
			"core/explorer_plugins.js",
			"core/explorer_workspace_codec.js",
			"core/explorer_workspace_tokens.js",
			"core/explorer_workspace_state.js",
			"core/ae_user_preferences.js",
		}
		for rel in (
			"core/explorer_events.js",
			"core/explorer_analysis_filters.js",
			"core/explorer_analysis_filter_summary.js",
			"core/explorer_drill_graph.js",
			"core/explorer_store.js",
			"core/explorer_plugins.js",
			"core/explorer_workspace_codec.js",
			"core/explorer_workspace_tokens.js",
			"core/explorer_workspace_state.js",
			"core/ae_user_preferences.js",
			"adapters/ae_datatable_adapter.js",
		):
			path = os.path.join(base, rel)
			self.assertTrue(os.path.isfile(path), f"missing module: {rel}")
			if rel in hard_limit_modules:
				with open(path, encoding="utf-8") as handle:
					lines = handle.read().count("\n") + 1
				self.assertLessEqual(lines, 800, f"{rel} exceeds 800-line hard limit ({lines})")

	def test_page_entry_includes_core_modules(self):
		page_js = os.path.join(
			self.app_root,
			"erpnext_extensions",
			"page",
			"account_explorer",
			"account_explorer.js",
		)
		with open(page_js, encoding="utf-8") as handle:
			content = handle.read()
		for fragment in (
			'{% include "erpnext_extensions/erpnext_extensions/page/account_explorer/core/explorer_events.js" %}',
			'{% include "erpnext_extensions/erpnext_extensions/page/account_explorer/core/explorer_analysis_filters.js" %}',
			'{% include "erpnext_extensions/erpnext_extensions/page/account_explorer/core/explorer_analysis_filter_summary.js" %}',
			'{% include "erpnext_extensions/erpnext_extensions/page/account_explorer/core/explorer_drill_graph.js" %}',
			'{% include "erpnext_extensions/erpnext_extensions/page/account_explorer/core/explorer_workspace_codec.js" %}',
			'{% include "erpnext_extensions/erpnext_extensions/page/account_explorer/core/explorer_workspace_tokens.js" %}',
			'{% include "erpnext_extensions/erpnext_extensions/page/account_explorer/core/explorer_workspace_state.js" %}',
			'{% include "erpnext_extensions/erpnext_extensions/page/account_explorer/core/ae_user_preferences.js" %}',
			'{% include "erpnext_extensions/erpnext_extensions/page/account_explorer/core/ae_date_format.js" %}',
			'{% include "erpnext_extensions/erpnext_extensions/page/account_explorer/adapters/ae_datatable_adapter.js" %}',
			'$(wrapper).bind("show"',
			"_init_explorer_architecture",
		):
			self.assertIn(fragment, content, f"missing entry hook: {fragment}")
		# Deterministic include order: codec → tokens → state
		codec_i = content.index("explorer_workspace_codec.js")
		tokens_i = content.index("explorer_workspace_tokens.js")
		state_i = content.index("explorer_workspace_state.js")
		self.assertLess(codec_i, tokens_i)
		self.assertLess(tokens_i, state_i)

	def test_page_registration_unchanged(self):
		page = frappe.get_doc("Page", "account-explorer")
		self.assertEqual(page.module, "erpnext_extensions")
		self.assertEqual(page.page_name, "account-explorer")
