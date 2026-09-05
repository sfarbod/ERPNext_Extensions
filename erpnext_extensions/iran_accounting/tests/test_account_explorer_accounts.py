# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import json
import unittest

import frappe

from erpnext_extensions.iran_accounting.account_explorer import api
from erpnext_extensions.iran_accounting.account_explorer.account_hierarchy import normalize_account_number
from erpnext_extensions.iran_accounting.account_explorer.constants import VIRTUAL_UNCLASSIFIED_KEY
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	current_fiscal_year,
	enable_account_explorer,
	require_site,
)


class TestAccountExplorerAccounts(unittest.TestCase):
	def setUp(self):
		self.company = require_site(self)
		enable_account_explorer()
		frappe.set_user("Administrator")
		fy = current_fiscal_year(self.company)
		if not fy:
			self.skipTest("No fiscal year")
		self.fiscal_year, self.from_date, self.to_date = fy

	def test_unclassified_for_alphanumeric_code(self):
		acc = frappe.db.get_value(
			"Account",
			{"company": self.company, "is_group": 0},
			["name", "account_number"],
			as_dict=True,
		)
		if not acc:
			self.skipTest("No account")
		original = acc.account_number
		frappe.db.set_value("Account", acc.name, "account_number", "11-A")
		frappe.db.commit()
		try:
			payload = json.dumps(
				{
					"document_scope": {
						"company": self.company,
						"fiscal_year": self.fiscal_year,
						"from_date": self.from_date,
						"to_date": self.to_date,
						"hide_zero_rows": 0,
					}
				}
			)
			result = api.get_account_summary(payload)
			# v5.1.1: non-numeric account codes are excluded — never grid __UNCLASSIFIED__.
			unclassified = [row for row in result["rows"] if row.get("row_key") == VIRTUAL_UNCLASSIFIED_KEY]
			self.assertEqual(unclassified, [])
			self.assertFalse(
				any(str(row.get("display_code") or "") == "__UNCLASSIFIED__" for row in result["rows"])
			)
			residual = result.get("classification_residual") or {}
			# Residual may or may not include this account depending on activity in range.
			self.assertIn("excluded_account_count", residual)
		finally:
			frappe.db.set_value("Account", acc.name, "account_number", original)
			frappe.db.commit()

	def test_virtual_row_has_no_selected_account(self):
		payload = json.dumps(
			{
				"document_scope": {
					"company": self.company,
					"fiscal_year": self.fiscal_year,
					"from_date": self.from_date,
					"to_date": self.to_date,
					"hide_zero_rows": 0,
				}
			}
		)
		result = api.get_account_summary(payload)
		for row in result["rows"]:
			if row.get("is_virtual_group"):
				self.assertEqual(row.get("selected_account"), None)

	def test_response_excludes_included_account_names(self):
		payload = json.dumps(
			{
				"document_scope": {
					"company": self.company,
					"fiscal_year": self.fiscal_year,
					"from_date": self.from_date,
					"to_date": self.to_date,
				}
			}
		)
		result = api.get_account_summary(payload)
		self.assertNotIn("included_account_names", json.dumps(result))

	def test_persian_digit_code_normalization(self):
		self.assertEqual(normalize_account_number("\u06f1\u06f1"), "11")
