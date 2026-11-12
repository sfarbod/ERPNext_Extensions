# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer v5.7.30 — deleted Dimension filters stay deleted; filter sticky CSS."""

from __future__ import annotations

import unittest
from pathlib import Path

import frappe


class TestAccountExplorerV5730FilterDeleteAlignment(unittest.TestCase):
	def test_forget_parked_dimension_analysis_on_chip_remove(self):
		app_root = Path(frappe.get_app_path("erpnext_extensions"))
		controller = (
			app_root
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "account_explorer.js"
		).read_text(encoding="utf-8")
		self.assertIn("forget_parked_dimension_analysis", controller)
		self.assertIn("_dimension_type_cleared", controller)
		self.assertIn("can_restore_analysis", controller)
		# Explicit delete must clear parked analysis rather than keep prior.analysis_entry.
		self.assertIn("forget_parked_dimension_analysis(String(key).slice", controller)
		self.assertIn("analysis_cleared", controller)
		# Active park must null analysis when bag entry is absent (no prior revive).
		self.assertIn("analysis_entry = entry ? JSON.parse(JSON.stringify(entry)) : null", controller)

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
		# Base .ae-dt-filter-cell block must not force relative (sticky left would shift).
		marker = ".account-explorer-page .ae-dt-filter-cell {"
		block_start = css.find(marker)
		self.assertGreaterEqual(block_start, 0)
		block_end = css.find("}", block_start)
		base_block = css[block_start:block_end]
		self.assertNotIn("position:", base_block)
