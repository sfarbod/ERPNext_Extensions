# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer 5.6.21: Party Name + Identifier Field + flat Parties UX."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import frappe

from erpnext_extensions.iran_accounting.account_explorer.export import rows_to_matrix
from erpnext_extensions.iran_accounting.account_explorer.party_sources import (
	PARTY_DISPLAY_FIELD_MAP,
	batch_party_display_titles,
	batch_party_identifiers,
	enrich_party_rows,
	get_party_display_title,
	get_party_source_config,
)
from erpnext_extensions.iran_accounting.account_explorer.party_summary import build_party_summary
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	build_payload,
	require_site,
)


class TestAccountExplorerPartyIdentityV5621(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.company = require_site(cls)

	def setUp(self):
		frappe.set_user("Administrator")
		if hasattr(frappe.local, "request_cache"):
			frappe.local.request_cache.clear()

	def _first(self, doctype: str) -> str | None:
		return frappe.db.get_value(doctype, {}, "name", order_by="creation asc")

	def test_customer_display_uses_customer_name_not_naming_setting(self):
		name = self._first("Customer")
		if not name:
			self.skipTest("No Customer")
		expected = frappe.db.get_value("Customer", name, "customer_name") or name
		# Even when Selling Settings say "Customer Name", series IDs must still resolve.
		with patch(
			"erpnext_extensions.iran_accounting.account_explorer.request_cache_helpers.get_selling_customer_naming_mode",
			return_value="Customer Name",
		):
			self.assertEqual(get_party_display_title("Customer", name), expected)
			self.assertEqual(batch_party_display_titles("Customer", [name])[name], expected)

	def test_supplier_employee_shareholder_display_fields(self):
		cases = [
			("Supplier", "supplier_name"),
			("Employee", "employee_name"),
			("Shareholder", "title"),
		]
		for doctype, field in cases:
			self.assertEqual(PARTY_DISPLAY_FIELD_MAP[doctype], field)
			name = self._first(doctype)
			if not name:
				continue
			expected = frappe.db.get_value(doctype, name, field) or name
			self.assertEqual(get_party_display_title(doctype, name), expected)

	def test_configured_identifier_field_per_party_type(self):
		for party_type in ("Customer", "Supplier", "Employee", "Shareholder"):
			source = get_party_source_config(party_type)
			if not source or not source.identifier_field:
				continue
			name = self._first(party_type)
			if not name:
				continue
			meta = frappe.get_meta(party_type)
			if not meta.has_field(source.identifier_field):
				continue
			expected = frappe.db.get_value(party_type, name, source.identifier_field)
			batch = batch_party_identifiers(party_type, [name], source.identifier_field)
			self.assertEqual(batch[name], expected)

	def test_missing_identifier_field_returns_none_without_crash(self):
		name = self._first("Customer")
		if not name:
			self.skipTest("No Customer")
		batch = batch_party_identifiers("Customer", [name], "this_field_does_not_exist")
		self.assertIsNone(batch[name])

	def test_missing_master_falls_back_to_party_id(self):
		self.assertEqual(get_party_display_title("Customer", "MISSING-PARTY-XYZ"), "MISSING-PARTY-XYZ")

	def test_party_summary_sets_display_identifier_and_disables_drill(self):
		fy = frappe.db.sql(
			"""
			select fy.name, fy.year_start_date, fy.year_end_date
			from `tabFiscal Year` fy
			inner join `tabFiscal Year Company` fyc on fyc.parent = fy.name
			where fyc.company = %s and fy.disabled = 0
			order by fy.year_start_date desc
			limit 1
			""",
			self.company,
			as_dict=True,
		)
		if not fy:
			self.skipTest("No fiscal year")
		payload = build_payload(
			self.company,
			fy[0].name,
			str(fy[0].year_start_date),
			str(fy[0].year_end_date),
			analysis={
				"view_axis": "party",
				"page": 1,
				"page_size": 50,
				"sort_field": "party_type",
				"sort_order": "asc",
				"detail_mode": "summary",
			},
		)
		from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
			AccountExplorerQuerySpec_from_client,
		)

		spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
		result = build_party_summary(spec)
		self.assertTrue(result.get("rows"))
		for row in result["rows"]:
			self.assertEqual(row.get("drill_down_enabled"), 0)
			self.assertEqual(row.get("display_code"), row.get("party"))
			if row.get("party_type") in PARTY_DISPLAY_FIELD_MAP and row.get("party"):
				# Display title should prefer master name when the master exists.
				self.assertTrue(row.get("display_title"))

	def test_same_identifier_keeps_party_type_isolation(self):
		"""Matching identifiers must not merge Customer/Supplier/Shareholder balances."""
		rows = [
			{
				"party_type": "Customer",
				"party": "CUS-ISO-1",
				"display_code": "CUS-ISO-1",
				"display_title": "CUS-ISO-1",
				"party_identifier": None,
				"is_virtual_group": 0,
				"period_debit": 100,
				"period_credit": 0,
			},
			{
				"party_type": "Supplier",
				"party": "SUP-ISO-1",
				"display_code": "SUP-ISO-1",
				"display_title": "SUP-ISO-1",
				"party_identifier": None,
				"is_virtual_group": 0,
				"period_debit": 0,
				"period_credit": 50,
			},
		]
		with patch(
			"erpnext_extensions.iran_accounting.account_explorer.party_sources.batch_party_display_titles",
			side_effect=lambda party_type, parties: {p: f"Name-{p}" for p in parties},
		), patch(
			"erpnext_extensions.iran_accounting.account_explorer.party_sources.batch_party_identifiers",
			side_effect=lambda party_type, parties, identifier_field: {
				p: "SAME-ID" for p in parties
			},
		), patch(
			"erpnext_extensions.iran_accounting.account_explorer.party_sources.get_party_source_config_map",
			return_value={
				"Customer": frappe._dict(identifier_field="tax_id"),
				"Supplier": frappe._dict(identifier_field="tax_id"),
			},
		):
			enrich_party_rows(rows)
		self.assertEqual(rows[0]["party_identifier"], "SAME-ID")
		self.assertEqual(rows[1]["party_identifier"], "SAME-ID")
		self.assertEqual(rows[0]["period_debit"], 100)
		self.assertEqual(rows[1]["period_credit"], 50)
		self.assertNotEqual(rows[0]["party"], rows[1]["party"])

	def test_export_identifier_is_text(self):
		rows = [
			{
				"party_type": "Customer",
				"display_code": "CUS-00001",
				"display_title": "Acme",
				"party_identifier": "001234",
			}
		]
		columns = [
			{"fieldname": "party_type", "label": "Party Type"},
			{"fieldname": "display_code", "label": "Party"},
			{"fieldname": "display_title", "label": "Party Name"},
			{"fieldname": "party_identifier", "label": "Identifier Field"},
		]
		_headers, data = rows_to_matrix(rows, columns)
		self.assertEqual(data[0][3], "001234")
		self.assertIsInstance(data[0][3], str)
