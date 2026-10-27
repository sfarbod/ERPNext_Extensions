# Copyright (c) 2026, Farbod Siyahpoosh and contributors

"""Persistent Unified Party identity OR-filter + exclusion transparency (5.6.21)."""

from __future__ import annotations

import unittest

import frappe

from erpnext_extensions.iran_accounting.account_explorer.api import (
	get_account_summary,
	get_classification_exclusion_details,
	get_item_summary,
	get_party_summary,
	get_voucher_summary,
)
from erpnext_extensions.iran_accounting.account_explorer.gle_filters import apply_member_tuple_filter
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


def _payload(axis="party", party_scope=None, level=1, **extra):
	ctx = {
		"view_axis": axis,
		"detail_mode": "summary",
		"page": 1,
		"page_size": 50,
		"party_scope": party_scope or {},
	}
	if axis == "account_level":
		ctx["level_sequence"] = level
	ctx.update(extra)
	return {"document_scope": dict(FY1405), "analysis_context": ctx}


class TestPartyIdentityPersistenceV5621(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
	def test_exact_tuple_or_predicate_not_cross_product(self):
		from frappe.query_builder import DocType

		gle = DocType("GL Entry")
		q = frappe.qb.from_(gle).select(gle.name)
		q = apply_member_tuple_filter(
			q,
			gle,
			[("Customer", "CUS-00003"), ("Supplier", "SUP-00204")],
		)
		sql = str(q)
		self.assertIn("party_type", sql.lower())
		# Must OR exact pairs — never party_type IN + party IN cross product alone.
		self.assertIn(" or ", sql.lower())

	def test_customer_only_identity_scope(self):
		data = get_account_summary(
			_payload(
				"account_level",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Customer", "CUS-00003"]],
					"identity_display_title": "Milan",
					"identity_identifier": "14008809078",
				},
			)
		)
		self.assertTrue(data["totals"]["period_debit"] > 0)
		self.assertEqual(
			data["context"]["party_scope"]["identity_members"],
			[{"party_type": "Customer", "party": "CUS-00003"}],
		)

	def test_customer_or_supplier_identity(self):
		data = get_account_summary(
			_payload(
				"account_level",
				party_scope={
					"unified_identity": 1,
					"identity_members": [
						["Customer", "CUS-00003"],
						["Supplier", "SUP-00204"],
					],
				},
			)
		)
		cust = get_account_summary(
			_payload(
				"account_level",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Customer", "CUS-00003"]],
				},
			)
		)
		supp = get_account_summary(
			_payload(
				"account_level",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Supplier", "SUP-00204"]],
				},
			)
		)
		self.assertAlmostEqual(
			float(data["totals"]["period_debit"]),
			float(cust["totals"]["period_debit"]) + float(supp["totals"]["period_debit"]),
			places=2,
		)

	def test_all_three_roles_party_axis(self):
		data = get_party_summary(
			_payload(
				"party",
				party_scope={
					"unified_identity": 1,
					"identity_members": [
						["Customer", "CUS-00003"],
						["Supplier", "SUP-00204"],
						["Shareholder", "ACC-SH-00029"],
					],
				},
			)
		)
		parties = {(r["party_type"], r["party"]) for r in data["rows"]}
		self.assertIn(("Customer", "CUS-00003"), parties)
		self.assertIn(("Supplier", "SUP-00204"), parties)

	def test_voucher_scope_uses_selected_tuples(self):
		data = get_voucher_summary(
			_payload(
				"voucher",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Customer", "CUS-00003"]],
				},
			)
		)
		for row in data.get("rows") or []:
			self.assertEqual(row.get("party_type"), "Customer")
			self.assertEqual(row.get("party"), "CUS-00003")

	def test_stock_identity_unsupported(self):
		data = get_item_summary(
			_payload(
				"item",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Customer", "CUS-00003"]],
				},
			)
		)
		self.assertEqual(data.get("rows"), [])
		self.assertTrue(data.get("unsupported_filter_combination"))
		self.assertEqual(
			data["unsupported_filter_combination"]["reason"], "sle_has_no_party_attribution"
		)

	def test_query_spec_resolves_selected_members(self):
		spec = AccountExplorerQuerySpec_from_client(
			_payload(
				"account_level",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Customer", "CUS-00003"], ["Supplier", "SUP-00204"]],
					"identity_available_members": [
						{"party_type": "Customer", "party": "CUS-00003"},
						{"party_type": "Supplier", "party": "SUP-00204"},
						{"party_type": "Shareholder", "party": "ACC-SH-00029"},
					],
				},
			)
		)
		self.assertEqual(
			set(spec.resolved_member_tuples),
			{("Customer", "CUS-00003"), ("Supplier", "SUP-00204")},
		)

	def test_exclusion_details_discover_missing_account_number(self):
		data = get_classification_exclusion_details(
			_payload("account_level", party_scope={})
		)
		self.assertGreaterEqual(int(data.get("excluded_account_count") or 0), 1)
		for row in data.get("rows") or []:
			self.assertEqual(row.get("reason"), "non_numeric_or_missing_account_number")
			self.assertTrue(row.get("account"))
			self.assertTrue(row.get("account_name"))
		recon = data.get("reconciliation") or {}
		self.assertTrue(recon.get("ok"))
		self.assertAlmostEqual(
			float(recon["included"]["period_debit"]) + float(recon["excluded"]["period_debit"]),
			float(recon["full_scope"]["period_debit"]),
			places=2,
		)

	def test_exclusion_under_identity_respects_scope(self):
		company_wide = get_classification_exclusion_details(_payload("account_level"))
		scoped = get_account_summary(
			_payload(
				"account_level",
				party_scope={
					"unified_identity": 1,
					"identity_members": [["Customer", "CUS-00003"], ["Supplier", "SUP-00204"]],
				},
			)
		)
		# Identity-scoped residual must not exceed company-wide residual activity.
		self.assertLessEqual(
			float((scoped.get("classification_residual") or {}).get("period_debit") or 0),
			float((company_wide.get("reconciliation") or {}).get("excluded", {}).get("period_debit") or 0)
			+ float((company_wide.get("reconciliation") or {}).get("included", {}).get("period_debit") or 0),
		)
