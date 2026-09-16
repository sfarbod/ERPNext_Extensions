# Copyright (c) 2026, ERPNext Extensions contributors
"""Unit tests — resolve_company never hard-codes a tenant."""

from __future__ import annotations

import unittest
from unittest.mock import patch


class TestResolveCompany(unittest.TestCase):
	def test_explicit_company_wins(self):
		from erpnext_extensions.iran_accounting.historical_stock.util import resolve_company

		self.assertEqual(resolve_company("Acme"), "Acme")
		self.assertEqual(resolve_company("  Acme  "), "Acme")

	def test_empty_falls_back_to_defaults_not_espad(self):
		from erpnext_extensions.iran_accounting.historical_stock.util import resolve_company

		with (
			patch("frappe.defaults.get_user_default", return_value="Site Co"),
			patch("frappe.db.get_single_value", return_value=None),
		):
			self.assertEqual(resolve_company(None), "Site Co")
			self.assertEqual(resolve_company(""), "Site Co")

	def test_missing_company_throws(self):
		from erpnext_extensions.iran_accounting.historical_stock.util import resolve_company
		import frappe

		with (
			patch("frappe.defaults.get_user_default", return_value=None),
			patch("frappe.db.get_single_value", return_value=None),
			patch("frappe.throw", side_effect=frappe.ValidationError("Company is required")),
		):
			with self.assertRaises(frappe.ValidationError):
				resolve_company(None)


class TestDumpArtifact(unittest.TestCase):
	def test_dump_noop_without_env(self):
		from erpnext_extensions.iran_accounting.historical_stock.util import dump_artifact
		import os

		os.environ.pop("HISTORICAL_REPAIR_ARTIFACT_DIR", None)
		self.assertIsNone(dump_artifact("x", "y.json", {"a": 1}))


if __name__ == "__main__":
	unittest.main()
