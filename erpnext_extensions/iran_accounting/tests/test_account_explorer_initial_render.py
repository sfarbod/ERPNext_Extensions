# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Structural tests: Account Explorer toolbar must not block on heavy metadata."""

from __future__ import annotations

import os
import unittest

import frappe


class TestAccountExplorerInitialRender(unittest.TestCase):
	def setUp(self):
		self.page_js = os.path.join(
			frappe.get_app_path("erpnext_extensions"),
			"erpnext_extensions",
			"page",
			"account_explorer",
			"account_explorer.js",
		)

	def test_primary_toolbar_skeleton_before_metadata(self):
		with open(self.page_js, encoding="utf-8") as handle:
			content = handle.read()
		self.assertIn("setup_primary_toolbar_skeleton", content)
		# Skeleton must be invoked before / independently of waiting for metadata callback
		ctor = content.split("constructor(page)", 1)[1].split("_init_explorer_architecture", 1)[0]
		# Fallback: check constructor calls skeleton before load_metadata
		ctor_full = content.split("constructor(page) {", 1)[1].split("\n\t}", 1)[0]
		self.assertIn("setup_primary_toolbar_skeleton", ctor_full)
		self.assertIn("load_metadata", ctor_full)
		self.assertLess(
			ctor_full.index("setup_primary_toolbar_skeleton"),
			ctor_full.index("load_metadata"),
		)

	def test_metadata_defers_currency_discovery(self):
		api_path = os.path.join(
			frappe.get_app_path("erpnext_extensions"),
			"iran_accounting",
			"account_explorer",
			"api.py",
		)
		with open(api_path, encoding="utf-8") as handle:
			api = handle.read()
		# get_metadata must not call discover_company_currencies inline for first paint
		meta_fn = api.split("def get_metadata()", 1)[1].split("\ndef ", 1)[0]
		self.assertNotIn("discover_company_currencies(", meta_fn)
		self.assertIn("currencies_deferred", meta_fn)
		self.assertIn("def get_metadata_enrichment", api)

	def test_frontend_loads_enrichment(self):
		with open(self.page_js, encoding="utf-8") as handle:
			content = handle.read()
		self.assertIn("get_account_explorer_metadata_enrichment", content)
		self.assertIn("_load_metadata_enrichment", content)
