# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""v5.1.1 — forbidden synthetic classification rows excluded before totals."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer import api
from erpnext_extensions.iran_accounting.account_explorer.constants import VIRTUAL_UNCLASSIFIED_KEY
from erpnext_extensions.iran_accounting.account_explorer.measures import (
	measures_from_opening_period,
	sum_measure_rows,
)
from erpnext_extensions.iran_accounting.account_explorer.pagination import (
	is_empty_classification_presentation_row,
	paginate_summary_rows,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import (
	AccountExplorerQuerySpec,
	AnalysisContext,
	DocumentScope,
	PaginationState,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	build_payload,
	current_fiscal_year,
	enable_wave2a_analysis,
	require_site,
)

FORBIDDEN_CODES = frozenset(
	{"__UNCLASSIFIED__", "__UNSPECIFIED__", "__UNMAPPED__", "-", "Unclassified", "Unspecified", "Unassigned", "Unmapped"}
)


def _spec() -> AccountExplorerQuerySpec:
	return AccountExplorerQuerySpec(
		document_scope=DocumentScope(
			company="_Test Company",
			from_date="2026-01-01",
			to_date="2026-12-31",
			hide_zero_rows=False,
		),
		analysis=AnalysisContext(
			view_axis="account_level",
			pagination=PaginationState(page=1, page_size=50),
		),
	)


class TestSyntheticRowContractUnit(unittest.TestCase):
	def test_forbidden_labels_match(self):
		for code, title in (
			("__UNCLASSIFIED__", "Unclassified"),
			("__UNSPECIFIED__", "Unspecified"),
			("-", "Unassigned"),
			("__UNMAPPED__", "Unmapped"),
		):
			self.assertTrue(
				is_empty_classification_presentation_row(
					{"row_key": "x", "display_code": code, "display_title": title, "is_virtual_group": 1}
				),
				code,
			)

	def test_account_unclassified_excluded_from_totals(self):
		real = {
			"row_key": "account:11",
			"display_code": "11",
			"display_title": "Current Assets",
			"is_virtual_group": 0,
			**measures_from_opening_period(0, 0, 100, 40),
		}
		synthetic = {
			"row_key": VIRTUAL_UNCLASSIFIED_KEY,
			"display_code": "__UNCLASSIFIED__",
			"display_title": "Unclassified",
			"is_virtual_group": 1,
			**measures_from_opening_period(0, 0, 25, 5),
		}
		result = paginate_summary_rows([real, synthetic], _spec())
		self.assertEqual(len(result["rows"]), 1)
		self.assertEqual(result["rows"][0]["display_code"], "11")
		self.assertEqual(flt(result["totals"]["period_debit"]), 100.0)
		self.assertEqual(flt(result["totals"]["period_credit"]), 40.0)
		self.assertEqual(
			flt(result["totals"]["period_debit"]),
			flt(sum_measure_rows(result["rows"])["period_debit"]),
		)

	def test_zero_movement_real_row_kept_when_hide_zero_off(self):
		row = {
			"row_key": "account:11",
			"display_code": "11",
			"is_virtual_group": 0,
			**measures_from_opening_period(0, 0, 0, 0),
		}
		result = paginate_summary_rows([row], _spec())
		self.assertEqual(len(result["rows"]), 1)


class TestSyntheticRowContractApi(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.company = require_site(cls)
		enable_wave2a_analysis()
		frappe.set_user("Administrator")
		fy = current_fiscal_year(cls.company)
		if not fy:
			raise unittest.SkipTest("No fiscal year")
		cls.fiscal_year, cls.from_date, cls.to_date = fy

	def _assert_no_forbidden(self, rows: list[dict], axis: str):
		for row in rows:
			code = str(row.get("display_code") or "")
			title = str(row.get("display_title") or "")
			key = str(row.get("row_key") or "")
			self.assertNotEqual(key, VIRTUAL_UNCLASSIFIED_KEY, axis)
			self.assertNotIn(code, FORBIDDEN_CODES, f"{axis} code={code}")
			self.assertNotIn(title, FORBIDDEN_CODES, f"{axis} title={title}")

	def test_account_root_has_no_unclassified(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "account_level", "level_sequence": 1, "page_size": 200},
			document={"hide_zero_rows": 1},
		)
		result = api.get_account_summary(payload)
		self._assert_no_forbidden(result.get("rows") or [], "account")
		self.assertNotIn("__UNCLASSIFIED__", [r.get("display_code") for r in result.get("rows") or []])
		# Footer equals returned rows
		leaf_debit = sum(flt(r.get("period_debit")) for r in result.get("rows") or [])
		self.assertAlmostEqual(leaf_debit, flt((result.get("totals") or {}).get("period_debit")), places=2)

	def test_case_a_item_group_api_no_synthetic_and_parity(self):
		live = "اسپاد فارمد دارو"
		if not frappe.db.exists("Company", live):
			raise unittest.SkipTest("Live company required for Case A API fixture")
		if not frappe.db.exists("Item Group", "API"):
			raise unittest.SkipTest("Item Group API missing")
		fy = current_fiscal_year(live)
		if not fy:
			raise unittest.SkipTest("No fiscal year on live company")
		fiscal_year, from_date, to_date = fy
		base_doc = {
			"hide_zero_rows": 1,
			"inventory": {"item_group": "API"},
			"status": {
				"include_opening_entries": 1,
				"include_cancelled_entries": 0,
				"include_default_finance_book_entries": 1,
				"include_period_closing_vouchers": 0,
			},
		}
		ig = api.get_item_group_summary(
			build_payload(
				live,
				fiscal_year,
				from_date,
				to_date,
				analysis={"view_axis": "item_group", "page_size": 50},
				document=base_doc,
			)
		)
		ac = api.get_account_summary(
			build_payload(
				live,
				fiscal_year,
				from_date,
				to_date,
				analysis={"view_axis": "account_level", "level_sequence": 3, "page_size": 100},
				document=base_doc,
			)
		)
		self._assert_no_forbidden(ac.get("rows") or [], "account_case_a")
		self.assertEqual(ac.get("account_fact_engine"), "sle_scoped_stock")
		codes = {
			str(r.get("display_code"))
			for r in (ac.get("rows") or [])
			if not int(r.get("is_group") or 0) and not int(r.get("has_children") or 0)
		}
		self.assertTrue(codes.issubset({"111601", "111701"}), codes)
		self.assertAlmostEqual(
			flt((ig.get("totals") or {}).get("inward_value")),
			flt((ac.get("totals") or {}).get("period_debit")),
			places=2,
		)
		self.assertAlmostEqual(
			flt((ig.get("totals") or {}).get("outward_value")),
			flt((ac.get("totals") or {}).get("period_credit")),
			places=2,
		)
		residual = ac.get("classification_residual") or {}
		self.assertEqual(flt(residual.get("period_debit")), 0.0)
		self.assertEqual(flt(residual.get("period_credit")), 0.0)
