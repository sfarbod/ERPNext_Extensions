# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer v5.7.29 — global filter delete + sticky filter CSS."""

from __future__ import annotations

import unittest
from pathlib import Path

import frappe


class TestAccountExplorerV5729FilterDeleteAlignment(unittest.TestCase):
	def test_chip_remove_clears_global_dimension_ui(self):
		app_root = Path(frappe.get_app_path("erpnext_extensions"))
		controller = (
			app_root
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "account_explorer.js"
		).read_text(encoding="utf-8")
		self.assertIn("remove_analysis_filter_chip", controller)
		self.assertIn("clear_dimension_type_ui", controller)
		self.assertIn("clear_all_analysis_filters", controller)
		# Explicit delete drops Dimension Type UI leftovers for that key.
		self.assertIn('clear_dimension_type_ui(String(key).slice("dimensions.".length))', controller)
		# Parked query ownership is obsolete; get_parked returns empty.
		self.assertIn("get_parked_dimension_summary_entries", controller)
		self.assertIn("Never owns query filters", controller)

	def test_sticky_filter_cell_css_not_overridden_by_relative(self):
		app_root = Path(frappe.get_app_path("erpnext_extensions"))
		css = (
			app_root
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "account_explorer.css"
		).read_text(encoding="utf-8")
		self.assertIn(".ae-dt-filter-cell.dt-cell--sticky", css)
		self.assertIn("position: sticky", css)
		self.assertIn(".ae-dt-filter-cell:not(.dt-cell--sticky)", css)
		marker = ".account-explorer-page .ae-dt-filter-cell {"
		block_start = css.find(marker)
		self.assertGreaterEqual(block_start, 0)
		block_end = css.find("}", block_start)
		base_block = css[block_start:block_end]
		self.assertNotIn("position:", base_block)

	def test_summary_ignores_parked_dimension_rows(self):
		app_root = Path(frappe.get_app_path("erpnext_extensions"))
		summary = (
			app_root
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "core"
			/ "explorer_analysis_filter_summary.js"
		).read_text(encoding="utf-8")
		# Compatibility param retained but ignored; no parked group rendering.
		self.assertIn("parked_dimensions", summary)
		self.assertIn("``parked_dimensions`` is ignored", summary)
		self.assertNotIn('group: "parked_dimension_filters"', summary)
