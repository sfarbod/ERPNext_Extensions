# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Currency discovery must not full-scan GL Entry on Account Explorer open."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import frappe

from erpnext_extensions.iran_accounting.account_explorer.currency_discovery import (
	clear_company_currency_cache,
	discover_company_currencies,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import require_site


class TestAccountExplorerCurrencyDiscoveryV5621(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.company = require_site(self)
		clear_company_currency_cache(self.company)

	def test_discover_uses_account_master_not_gl_scan(self):
		calls = []

		def spy(query, *args, **kwargs):
			sql = query if isinstance(query, str) else str(query)
			calls.append(sql)
			return real_sql(query, *args, **kwargs)

		real_sql = frappe.db.sql
		with patch.object(frappe.db, "sql", side_effect=spy):
			currencies = discover_company_currencies(self.company)
		self.assertTrue(isinstance(currencies, list))
		joined = "\n".join(calls).lower()
		self.assertIn("tabaccount", joined)
		self.assertNotIn("tabgl entry", joined)
