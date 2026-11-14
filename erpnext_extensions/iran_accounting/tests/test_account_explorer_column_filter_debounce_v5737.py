# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer v5.7.37 — 3000 ms trailing-edge column-filter debounce."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

import frappe


class TestColumnFilterDebounceSourceV5737(unittest.TestCase):
	def test_debounce_constant_is_3000ms(self):
		app_root = Path(frappe.get_app_path("erpnext_extensions"))
		controller = (
			app_root
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "account_explorer.js"
		).read_text(encoding="utf-8")
		self.assertIn("AE_COLUMN_FILTER_DEBOUNCE_MS = 3000", controller)
		self.assertIn("setTimeout(apply, AE_COLUMN_FILTER_DEBOUNCE_MS)", controller)
		self.assertNotIn("setTimeout(apply, 500)", controller)
		self.assertIn("cancel_pending_column_filter_debounce", controller)
		self.assertIn("trailing-edge debounce", controller)

	def test_enter_and_clear_cancel_pending_timer(self):
		app_root = Path(frappe.get_app_path("erpnext_extensions"))
		controller = (
			app_root
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "account_explorer.js"
		).read_text(encoding="utf-8")
		self.assertIn("opts.immediate", controller)
		# Clear paths cancel the shared timer before applying.
		self.assertGreaterEqual(controller.count("cancel_pending_column_filter_debounce"), 3)
		adapter = (
			app_root
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "adapters"
			/ "ae_datatable_adapter.js"
		).read_text(encoding="utf-8")
		self.assertIn("immediate: !!immediate", adapter)
		self.assertIn('event.key !== "Enter"', adapter)
		self.assertIn("3000ms trailing", adapter)

	def test_axis_switch_cancels_pending_debounce(self):
		app_root = Path(frappe.get_app_path("erpnext_extensions"))
		controller = (
			app_root
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "account_explorer.js"
		).read_text(encoding="utf-8")
		# switch_axis must cancel before navigating so stale timers cannot fire.
		switch_idx = controller.find("switch_axis(view_axis, level_or_dimension = null)")
		self.assertGreater(switch_idx, 0)
		snippet = controller[switch_idx : switch_idx + 1200]
		self.assertIn("cancel_pending_column_filter_debounce()", snippet)


class TestColumnFilterDebounceHarnessV5737(unittest.TestCase):
	def test_fake_clock_harness_covers_3000ms_contract(self):
		app_root = Path(frappe.get_app_path("erpnext_extensions"))
		harness = app_root / "iran_accounting" / "e2e" / "playwright" / "ae_v5729_unit.cjs"
		if not harness.is_file():
			self.skipTest("debounce unit harness absent")
		result = subprocess.run(
			["node", str(harness)],
			capture_output=True,
			text=True,
			check=False,
		)
		self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
		payload = json.loads(result.stdout)
		self.assertEqual(payload.get("failed"), 0)
		names = {c.get("name") for c in payload.get("checks") or []}
		self.assertIn("debounce_3000_enter_clear", names)
		self.assertIn("debounce_timer_reset_3000", names)
		self.assertIn("stale_response_guard", names)
		self.assertNotIn("debounce_500_enter_clear", names)
