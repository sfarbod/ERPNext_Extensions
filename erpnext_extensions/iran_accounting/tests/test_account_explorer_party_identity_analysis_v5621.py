# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""5.6.21 follow-up: Related Roles, Unified Identity filter, Party Analyze isolation."""

from __future__ import annotations

import json
import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.party_identity import (
	build_party_identity_analysis,
	format_related_roles,
	identity_match_key,
	resolve_identity_members,
)
from erpnext_extensions.iran_accounting.account_explorer.party_summary import build_party_summary
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import build_payload


COMPANY = "اسپاد فارمد دارو"


class TestAccountExplorerPartyIdentityAnalysisV5621(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db.exists("Company", COMPANY):
			raise unittest.SkipTest("Espad company not available")
		cls.fy = frappe.db.get_value(
			"Fiscal Year",
			{"name": "1405"},
			["name", "year_start_date", "year_end_date"],
			as_dict=True,
		)
		if not cls.fy:
			raise unittest.SkipTest("FY1405 required")
		cls.customer = "CUS-00003"
		cls.supplier = "SUP-00204"
		cls.shareholder = "ACC-SH-00029"
		cls.tax_id = "14008809078"
		if not frappe.db.exists("Customer", cls.customer):
			raise unittest.SkipTest("Expected Customer CUS-00003 missing")

	def setUp(self):
		frappe.set_user("Administrator")
		if hasattr(frappe.local, "request_cache"):
			frappe.local.request_cache.clear()

	def _payload(self, **party_scope):
		analysis = {
			"view_axis": "party",
			"page": 1,
			"page_size": 200,
			"sort_field": "party_type",
			"sort_order": "asc",
			"detail_mode": "summary",
			"party_scope": party_scope,
		}
		return build_payload(
			COMPANY,
			self.fy.name,
			str(self.fy.year_start_date),
			str(self.fy.year_end_date),
			analysis=analysis,
		)

	def test_identity_match_key_ignores_blank(self):
		self.assertEqual(identity_match_key(""), "")
		self.assertEqual(identity_match_key(None), "")
		self.assertEqual(identity_match_key("  "), "")
		self.assertTrue(identity_match_key(self.tax_id).startswith("d:"))

	def test_milan_multi_role_discovery(self):
		members = resolve_identity_members(party_type="Customer", party=self.customer)
		types = {m["party_type"] for m in members}
		self.assertIn("Customer", types)
		self.assertIn("Supplier", types)
		self.assertIn("Shareholder", types)
		for m in members:
			self.assertEqual(m.get("party_identifier"), self.tax_id)

	def test_related_roles_on_party_summary_row(self):
		spec = AccountExplorerQuerySpec_from_client(self._payload(), require_dates=True)
		result = build_party_summary(spec)
		row = next((r for r in result["rows"] if r.get("party") == self.customer), None)
		self.assertIsNotNone(row)
		roles = row.get("related_roles") or []
		self.assertIn("Customer", roles)
		self.assertIn("Supplier", roles)
		self.assertIn("Shareholder", roles)
		self.assertEqual(row.get("related_roles_label"), format_related_roles(roles))
		self.assertEqual(row.get("drill_down_enabled"), 0)

	def test_filter_by_party_type_customer_only(self):
		spec = AccountExplorerQuerySpec_from_client(
			self._payload(party_type="Customer"), require_dates=True
		)
		result = build_party_summary(spec)
		self.assertTrue(result["rows"])
		self.assertTrue(all(r["party_type"] == "Customer" for r in result["rows"]))

	def test_filter_by_exact_party_id(self):
		spec = AccountExplorerQuerySpec_from_client(
			self._payload(party_type="Customer", selected_party=self.customer),
			require_dates=True,
		)
		result = build_party_summary(spec)
		self.assertEqual(len(result["rows"]), 1)
		self.assertEqual(result["rows"][0]["party"], self.customer)

	def test_filter_by_party_name(self):
		spec = AccountExplorerQuerySpec_from_client(
			self._payload(party_name_query="میلان"),
			require_dates=True,
		)
		result = build_party_summary(spec)
		self.assertTrue(result["rows"])
		self.assertTrue(any("میلان" in (r.get("display_title") or "") for r in result["rows"]))

	def test_unified_identity_expands_roles(self):
		spec = AccountExplorerQuerySpec_from_client(
			self._payload(
				party_type="Customer",
				selected_party=self.customer,
				unified_identity=1,
			),
			require_dates=True,
		)
		self.assertTrue(spec.resolved_member_tuples)
		types = {pt for pt, _ in spec.resolved_member_tuples}
		self.assertIn("Customer", types)
		self.assertIn("Supplier", types)
		result = build_party_summary(spec)
		found_types = {r["party_type"] for r in result["rows"]}
		self.assertIn("Customer", found_types)
		self.assertIn("Supplier", found_types)

	def test_party_analyze_per_role_isolation_and_combined(self):
		spec = AccountExplorerQuerySpec_from_client(self._payload(), require_dates=True)
		data = build_party_identity_analysis(spec, party_type="Customer", party=self.customer)
		roles = data["roles"]
		self.assertGreaterEqual(len(roles), 2)
		# Distinct party IDs — never merged into one GL identity.
		pairs = {(r["party_type"], r["party"]) for r in roles}
		self.assertEqual(len(pairs), len(roles))
		od = sum(flt(r["opening_debit"]) for r in roles)
		oc = sum(flt(r["opening_credit"]) for r in roles)
		pd = sum(flt(r["period_debit"]) for r in roles)
		pc = sum(flt(r["period_credit"]) for r in roles)
		combined = data["combined"]
		self.assertEqual(flt(combined["opening_debit"]), od)
		self.assertEqual(flt(combined["opening_credit"]), oc)
		self.assertEqual(flt(combined["period_debit"]), pd)
		self.assertEqual(flt(combined["period_credit"]), pc)
		# Seed customer still present with its own balances.
		seed = next(r for r in roles if r["party"] == self.customer)
		self.assertEqual(seed["party_type"], "Customer")

	def test_missing_identifier_does_not_fabricate_relations(self):
		# Pick a customer without tax_id if available; otherwise skip.
		row = frappe.db.sql(
			"""
			select name from `tabCustomer`
			where ifnull(tax_id,'')='' and name != %s
			order by creation asc limit 1
			""",
			self.customer,
		)
		if not row:
			self.skipTest("No Customer without tax_id")
		members = resolve_identity_members(party_type="Customer", party=row[0][0])
		self.assertEqual(len(members), 1)
		self.assertEqual(members[0]["party"], row[0][0])

	def test_different_identifiers_do_not_match(self):
		members = resolve_identity_members(identifier_value="___no_such_identifier___")
		self.assertEqual(members, [])

	def test_api_party_identity_analysis_whitelisted(self):
		from erpnext_extensions.iran_accounting.account_explorer.api import get_party_identity_analysis

		payload = json.loads(self._payload(party_type="Customer", selected_party=self.customer))
		data = get_party_identity_analysis(payload)
		self.assertIn("roles", data)
		self.assertIn("combined", data)
		self.assertIn("member_tuples", data)
