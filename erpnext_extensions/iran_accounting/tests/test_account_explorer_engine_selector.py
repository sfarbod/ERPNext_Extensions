# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Case A/B Account fact-engine selector (v5.1.1 asymmetric contract)."""

from __future__ import annotations

import unittest

import frappe

from erpnext_extensions.iran_accounting.account_explorer.sle_scoped_account import (
	ACCOUNT_FACT_ENGINE_POSTED,
	ACCOUNT_FACT_ENGINE_SLE_SCOPED,
	select_account_fact_engine,
	select_sle_scoped_account_engine,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	build_payload,
	current_fiscal_year,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_inventory_axes import (
	enable_inventory_analysis,
	require_inventory_company,
)


class TestAccountFactEngineSelector(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.company = require_inventory_company(cls)
		enable_inventory_analysis()
		fy = current_fiscal_year(cls.company)
		if not fy:
			raise unittest.SkipTest("No FY")
		cls.fiscal_year, cls.fy_start, cls.fy_end = fy

	def _spec(self, inventory=None, view_axis="account_level"):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			str(self.fy_start),
			str(self.fy_end),
			analysis={"view_axis": view_axis, "level_sequence": 3, "page_size": 50},
			document={"inventory": inventory or {}},
		)
		return AccountExplorerQuerySpec_from_client(payload, require_dates=True)

	def test_case_a_item_group_filter_selects_sle_scoped(self):
		spec = self._spec({"item_group": "All Item Groups"})
		self.assertTrue(select_sle_scoped_account_engine(spec))
		self.assertEqual(select_account_fact_engine(spec), ACCOUNT_FACT_ENGINE_SLE_SCOPED)

	def test_case_a_item_filter_selects_sle_scoped(self):
		spec = self._spec({"item": "__engine_selector_probe__"})
		self.assertEqual(select_account_fact_engine(spec), ACCOUNT_FACT_ENGINE_SLE_SCOPED)

	def test_case_b_no_inventory_selects_posted_gl(self):
		spec = self._spec({})
		self.assertFalse(select_sle_scoped_account_engine(spec))
		self.assertEqual(select_account_fact_engine(spec), ACCOUNT_FACT_ENGINE_POSTED)

	def test_case_a_response_engine_not_e3(self):
		from erpnext_extensions.iran_accounting.account_explorer.api import get_account_summary

		groups = frappe.get_all("Item Group", filters={"is_group": 0}, pluck="name", limit=1)
		ig = groups[0] if groups else "All Item Groups"
		payload = build_payload(
			self.company,
			self.fiscal_year,
			str(self.fy_start),
			str(self.fy_end),
			analysis={"view_axis": "account_level", "level_sequence": 3, "page_size": 50},
			document={"inventory": {"item_group": ig}},
		)
		res = get_account_summary(payload)
		self.assertEqual(res.get("account_fact_engine"), ACCOUNT_FACT_ENGINE_SLE_SCOPED)
		self.assertEqual(res.get("account_axis_engine"), ACCOUNT_FACT_ENGINE_SLE_SCOPED)
		self.assertNotEqual(res.get("account_fact_engine"), "E3")
		self.assertNotEqual(res.get("account_fact_engine"), "voucher_scoped_gl")
		self.assertNotEqual(res.get("account_fact_engine"), "posted_gl")
		self.assertNotEqual(res.get("account_fact_engine"), "stock_construction_replay")

	def test_case_b_response_engine_posted(self):
		from erpnext_extensions.iran_accounting.account_explorer.api import get_account_summary

		payload = build_payload(
			self.company,
			self.fiscal_year,
			str(self.fy_start),
			str(self.fy_end),
			analysis={"view_axis": "account_level", "level_sequence": 3, "page_size": 50},
			document={"inventory": {}},
		)
		res = get_account_summary(payload)
		self.assertEqual(res.get("account_fact_engine"), ACCOUNT_FACT_ENGINE_POSTED)
