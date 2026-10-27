# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Consistent Party Name + Party ID display across Account Explorer (5.6.21)."""

from __future__ import annotations

import unittest

import frappe

from erpnext_extensions.iran_accounting.account_explorer.api import (
	get_item_summary,
	get_party_identity_analysis,
	get_party_summary,
	get_voucher_summary,
)
from erpnext_extensions.iran_accounting.account_explorer.export import get_export_columns
from erpnext_extensions.iran_accounting.account_explorer.party_sources import (
	batch_party_display_titles,
	enrich_rows_with_party_names,
	format_party_name_with_id,
	resolve_party_display_name,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)


COMPANY = "اسپاد فارمد دارو"
FY1405 = {
	"company": COMPANY,
	"fiscal_year": "1405",
	"from_date": "2026-03-21",
	"to_date": "2027-03-20",
}


def _payload(axis="item", party_scope=None, **extra):
	ctx = {
		"view_axis": axis,
		"detail_mode": "summary",
		"page": 1,
		"page_size": 50,
		"party_scope": party_scope or {},
	}
	ctx.update(extra)
	return {"document_scope": dict(FY1405), "analysis_context": ctx}


class TestPartyDisplayV5621(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.customer = "CUS-00003"
		cls.supplier = "SUP-00204"
		cls.shareholder = "ACC-SH-00029"
		cls.customer_name = frappe.db.get_value("Customer", cls.customer, "customer_name")
		cls.supplier_name = frappe.db.get_value("Supplier", cls.supplier, "supplier_name")
		cls.shareholder_title = frappe.db.get_value("Shareholder", cls.shareholder, "title")

	def test_customer_display_name(self):
		self.assertTrue(self.customer_name)
		self.assertNotEqual(self.customer_name, self.customer)
		self.assertEqual(
			resolve_party_display_name("Customer", self.customer), self.customer_name
		)

	def test_supplier_display_name(self):
		self.assertTrue(self.supplier_name)
		self.assertEqual(
			resolve_party_display_name("Supplier", self.supplier), self.supplier_name
		)

	def test_shareholder_display_name(self):
		self.assertTrue(self.shareholder_title)
		self.assertEqual(
			resolve_party_display_name("Shareholder", self.shareholder), self.shareholder_title
		)

	def test_employee_display_field_supported(self):
		# Field map must prefer employee_name when Employee master exists.
		from erpnext_extensions.iran_accounting.account_explorer.party_sources import (
			PARTY_DISPLAY_FIELD_MAP,
		)

		self.assertEqual(PARTY_DISPLAY_FIELD_MAP["Employee"], "employee_name")
		emp = frappe.db.get_value("Employee", {"status": "Active"}, "name")
		if not emp:
			self.skipTest("No Employee master on restored site")
		name = resolve_party_display_name("Employee", emp)
		self.assertTrue(name)
		# Either resolved master name or fallback to id — never empty.
		self.assertEqual(name, frappe.db.get_value("Employee", emp, "employee_name") or emp)

	def test_missing_master_name_fallback(self):
		self.assertEqual(
			resolve_party_display_name("Customer", "___no_such_customer___"),
			"___no_such_customer___",
		)

	def test_format_party_name_with_id(self):
		self.assertEqual(
			format_party_name_with_id(self.customer_name, self.customer),
			f"{self.customer_name} ({self.customer})",
		)
		self.assertEqual(format_party_name_with_id(None, "CUS-1"), "CUS-1")
		self.assertEqual(format_party_name_with_id("Same", "Same"), "Same")

	def test_batch_enrich_no_n_plus_one_shape(self):
		titles = batch_party_display_titles(
			"Customer", [self.customer, self.customer, "___missing___"]
		)
		self.assertEqual(titles[self.customer], self.customer_name)
		self.assertEqual(titles["___missing___"], "___missing___")
		rows = [
			{"party_type": "Customer", "party": self.customer},
			{"party_type": "Supplier", "party": self.supplier},
			{"party_type": "Shareholder", "party": self.shareholder},
		]
		enrich_rows_with_party_names(rows)
		self.assertEqual(rows[0]["party_name"], self.customer_name)
		self.assertEqual(rows[1]["party_name"], self.supplier_name)
		self.assertEqual(rows[2]["party_name"], self.shareholder_title)

	def test_commercial_items_party_name(self):
		data = get_item_summary(
			_payload(
				"item",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Customer", self.customer]],
				},
			)
		)
		self.assertGreater(len(data["rows"]), 0)
		cols = {c["id"] for c in data["columns"]}
		self.assertIn("party_name", cols)
		self.assertIn("party", cols)
		for row in data["rows"]:
			self.assertEqual(row["party"], self.customer)
			self.assertEqual(row["party_name"], self.customer_name)
			self.assertNotEqual(row["party_name"], row["party"])

	def test_commercial_totals_unchanged_by_party_name(self):
		data = get_item_summary(
			_payload(
				"item",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Customer", self.customer]],
				},
			)
		)
		# Financial totals still match independent SI base net.
		expected = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(sii.base_net_amount), 0)
			FROM `tabSales Invoice` si
			INNER JOIN `tabSales Invoice Item` sii ON sii.parent = si.name
			WHERE si.docstatus=1 AND si.company=%s AND si.customer=%s
				AND si.posting_date BETWEEN %s AND %s
			""",
			(COMPANY, self.customer, FY1405["from_date"], FY1405["to_date"]),
		)[0][0]
		self.assertAlmostEqual(float(data["totals"]["base_net_amount"]), float(expected), places=2)

	def test_voucher_party_name(self):
		data = get_voucher_summary(
			_payload(
				"voucher",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Customer", self.customer]],
				},
			)
		)
		cols = {c["id"]: c.get("label") for c in data["columns"]}
		self.assertEqual(cols.get("party_name"), "Party Name")
		self.assertEqual(cols.get("party"), "Party")
		named = [r for r in data["rows"] if r.get("party") == self.customer]
		self.assertGreater(len(named), 0)
		for row in named:
			self.assertEqual(row["party_name"], self.customer_name)
			self.assertEqual(row["party"], self.customer)

	def test_unified_party_member_names(self):
		data = get_party_identity_analysis(
			{
				"document_scope": dict(FY1405),
				"analysis_context": {
					"view_axis": "party",
					"detail_mode": "summary",
					"page": 1,
					"page_size": 50,
					"party_scope": {
						"party_type": "Customer",
						"selected_party": self.customer,
					},
				},
			}
		)
		identity = data.get("identity") or {}
		self.assertEqual(identity.get("display_title"), self.customer_name)
		self.assertEqual(identity.get("party"), self.customer)
		members = data.get("roles") or []
		self.assertGreaterEqual(len(members), 3)
		by_type = {m["party_type"]: m for m in members}
		self.assertEqual(by_type["Customer"]["display_title"], self.customer_name)
		self.assertEqual(by_type["Customer"]["party"], self.customer)
		self.assertEqual(by_type["Supplier"]["display_title"], self.supplier_name)
		self.assertEqual(by_type["Supplier"]["party"], self.supplier)
		self.assertEqual(by_type["Shareholder"]["display_title"], self.shareholder_title)
		self.assertEqual(by_type["Shareholder"]["party"], self.shareholder)

	def test_parties_axis_keeps_separate_name_and_id(self):
		data = get_party_summary(
			_payload(
				"party",
				party_scope={"party_type": "Customer", "selected_party": self.customer},
			)
		)
		row = next((r for r in data["rows"] if r.get("party") == self.customer), None)
		self.assertIsNotNone(row)
		self.assertEqual(row["display_code"], self.customer)
		self.assertEqual(row["display_title"], self.customer_name)
		self.assertNotEqual(row["display_title"], row["display_code"])

	def test_export_columns_include_party_name(self):
		spec = AccountExplorerQuerySpec_from_client(
			_payload(
				"item",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Customer", self.customer]],
				},
			)
		)
		cols = {c["fieldname"] for c in get_export_columns(spec)}
		self.assertIn("party_name", cols)
		self.assertIn("party", cols)

		vspec = AccountExplorerQuerySpec_from_client(_payload("voucher"))
		vcols = {c["fieldname"] for c in get_export_columns(vspec)}
		self.assertIn("party_name", vcols)
		self.assertIn("party", vcols)
		self.assertIn("party_type", vcols)
