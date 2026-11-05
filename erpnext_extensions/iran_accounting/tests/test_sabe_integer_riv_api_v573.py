# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.38 — SABE integer RIV API allowlist + dry-run default."""

from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

import frappe

from erpnext_extensions.iran_accounting.api import sabe_integer_riv as api


class TestSabeIntegerRivApiV573(unittest.TestCase):
	def test_execute_defaults_to_dry_run_without_confirm(self):
		src = inspect.getsource(api.execute_scope)
		self.assertIn("confirm_execution", src)
		self.assertIn("iran_sabe_integer_riv_allow_execute", src)
		self.assertIn("EXECUTE", src)

	def test_no_hardcoded_api_secrets(self):
		src = inspect.getsource(api)
		for bad in ("api_secret=", "sk_live", "Bearer eyJ", 'api_key = "'):
			self.assertNotIn(bad, src)

	def test_assert_scope_allowed_rejects_unknown(self):
		with patch.object(api, "_allowlist", return_value=[]), patch.object(
			api, "_require_riv_api_permission"
		):
			with self.assertRaises(frappe.PermissionError):
				api._assert_scope_allowed(
					{
						"item_code": "X",
						"warehouse": "W",
						"posting_date": "2026-01-01",
						"company": "C",
					}
				)

	def test_uses_riv_execute_guard_not_import_bound_core(self):
		src = inspect.getsource(api)
		self.assertIn("riv_execute_guard", src)
		self.assertIn("ensure_iran_riv_gl_handlers_active", src)
		self.assertNotIn(
			"from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import",
			src,
		)


if __name__ == "__main__":
	unittest.main()
