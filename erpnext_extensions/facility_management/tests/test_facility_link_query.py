# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Facility Link search for Facility Repayment (v5.6.20)."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import random_string, today

from erpnext_extensions.facility_management.facility_e2e_context import (
	apply_facility_test_accounts,
	ensure_bank_master,
)
from erpnext_extensions.facility_management.facility_queries import (
	_normalize_link_filters,
	facility_link_query,
)
from erpnext_extensions.facility_management.facility_settings_doc import get_facility_settings_doc
from erpnext_extensions.facility_management.tests.test_facility_debt_purchase_e2e import (
	DebtPurchaseDbFixtureMixin,
)


class TestFacilityLinkQuery(DebtPurchaseDbFixtureMixin, unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Non-DP type so Active opening activation does not require PDC allocation.
		name = "UI Test Normal Type"
		if not frappe.db.exists("Facility Type", name):
			doc = frappe.new_doc("Facility Type")
			doc.facility_type_name = name
			doc.is_debt_purchase = 0
			doc.insert(ignore_permissions=True)
		else:
			frappe.db.set_value("Facility Type", name, "is_debt_purchase", 0, update_modified=False)
		cls.normal_ft = name
		frappe.db.commit()

	def _make_facility(self, *, facility_name: str, status: str = "Draft"):
		settings = get_facility_settings_doc(self.company)
		bank = frappe.db.get_value("Bank", {}, "name", order_by="creation asc") or ensure_bank_master()
		fac = frappe.new_doc("Facility")
		fac.facility_name = facility_name
		fac.company = self.company
		fac.facility_type = self.normal_ft
		fac.bank = bank
		fac.contract_date = today()
		fac.principal_amount = 1_000_000
		fac.profit_amount = 0
		fac.is_opening_facility = 1
		apply_facility_test_accounts(fac)
		if settings and getattr(settings, "default_cost_center", None) and not fac.cost_center:
			fac.cost_center = settings.default_cost_center
		fac.insert(ignore_permissions=True)
		if status == "Active":
			fac.status = "Active"
			fac.save(ignore_permissions=True)
		frappe.db.commit()
		return fac

	def test_normalize_list_filters(self):
		d = _normalize_link_filters([["Facility", "status", "=", "Active"]])
		self.assertEqual(d.status, "Active")
		d2 = _normalize_link_filters({"repayment_select": 1})
		self.assertEqual(cint_safe(d2.repayment_select), 1)

	def test_search_by_facility_name_partial(self):
		token = f"LN{random_string(6)}"
		fac = self._make_facility(facility_name=f"{token}-SEARCH-ACTIVE", status="Active")
		rows = facility_link_query(
			"Facility", token, "name", 0, 20, {"status": "Active"}
		)
		names = [r[0] for r in rows]
		self.assertIn(fac.name, names)
		# description columns include facility_name, bank, status
		hit = next(r for r in rows if r[0] == fac.name)
		self.assertEqual(hit[1], fac.facility_name)
		self.assertEqual(hit[3], "Active")

	def test_search_by_facility_id(self):
		fac = self._make_facility(facility_name=f"IDSEARCH-{random_string(6)}", status="Active")
		fragment = fac.name[-5:]
		rows = facility_link_query(
			"Facility", fragment, "name", 0, 20, {"repayment_select": 1}
		)
		self.assertIn(fac.name, [r[0] for r in rows])

	def test_draft_excluded_for_repayment_select(self):
		token = f"DR{random_string(6)}"
		draft = self._make_facility(facility_name=f"{token}-DRAFT-ONLY", status="Draft")
		rows = facility_link_query(
			"Facility", token, "name", 0, 20, {"repayment_select": 1}
		)
		self.assertNotIn(draft.name, [r[0] for r in rows])
		# Without eligibility filter, draft is findable
		all_rows = facility_link_query("Facility", token, "name", 0, 20, {})
		self.assertIn(draft.name, [r[0] for r in all_rows])

	def test_list_style_status_filter(self):
		token = f"LS{random_string(6)}"
		fac = self._make_facility(facility_name=f"{token}-LIST-FILTER", status="Active")
		rows = facility_link_query(
			"Facility",
			token,
			"name",
			0,
			20,
			[["Facility", "status", "=", "Active"]],
		)
		self.assertIn(fac.name, [r[0] for r in rows])


def cint_safe(v):
	return int(v or 0)
