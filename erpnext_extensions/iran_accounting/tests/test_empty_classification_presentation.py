# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""v4.6.2 — empty classification excluded from grid rows and totals."""

from __future__ import annotations

import unittest
from pathlib import Path

from erpnext_extensions.iran_accounting.account_explorer.constants import (
	VIRTUAL_DIMENSION_UNSPECIFIED_PREFIX,
	VIRTUAL_PARTY_UNSPECIFIED_KEY,
	VIRTUAL_UNCLASSIFIED_KEY,
	VIRTUAL_UNIFIED_UNMAPPED_KEY,
)
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


def _spec(*, page=1, page_size=50, hide_zero_rows=0) -> AccountExplorerQuerySpec:
	return AccountExplorerQuerySpec(
		document_scope=DocumentScope(
			company="_Test Company",
			fiscal_year=None,
			from_date="2026-01-01",
			to_date="2026-12-31",
			hide_zero_rows=hide_zero_rows,
		),
		analysis=AnalysisContext(
			view_axis="party",
			pagination=PaginationState(
				page=page, page_size=page_size, sort_field="display_code", sort_order="asc"
			),
		),
	)


class TestEmptyClassificationPresentation(unittest.TestCase):
	def test_helper_matches_empty_buckets_including_account_unclassified(self):
		self.assertTrue(
			is_empty_classification_presentation_row(
				{"row_key": VIRTUAL_PARTY_UNSPECIFIED_KEY, "is_virtual_group": 1}
			)
		)
		self.assertTrue(
			is_empty_classification_presentation_row(
				{
					"row_key": f"{VIRTUAL_DIMENSION_UNSPECIFIED_PREFIX}:cost_center",
					"dimension_value": "",
					"is_virtual_group": 1,
				}
			)
		)
		self.assertTrue(
			is_empty_classification_presentation_row(
				{"row_key": VIRTUAL_UNIFIED_UNMAPPED_KEY, "display_code": "__UNMAPPED__", "is_virtual_group": 1}
			)
		)
		# v5.1.1: Account Unclassified is also forbidden on the grid.
		self.assertTrue(
			is_empty_classification_presentation_row(
				{
					"row_key": VIRTUAL_UNCLASSIFIED_KEY,
					"display_code": "__UNCLASSIFIED__",
					"display_title": "Unclassified",
					"is_virtual_group": 1,
				}
			)
		)
		self.assertFalse(
			is_empty_classification_presentation_row(
				{
					"row_key": "party:Customer:Alpha",
					"party_type": "Customer",
					"party": "Alpha",
					"is_virtual_group": 0,
				}
			)
		)

	def test_classified_rows_remain_visible_and_totals_exclude_empty(self):
		classified = {
			"row_key": "party:Customer:Alpha",
			"party_type": "Customer",
			"party": "Alpha",
			"display_code": "Alpha",
			"display_title": "Alpha",
			"is_virtual_group": 0,
			**measures_from_opening_period(10, 0, 20, 5),
		}
		unspecified = {
			"row_key": VIRTUAL_PARTY_UNSPECIFIED_KEY,
			"party_type": "",
			"party": "",
			"display_code": "__UNSPECIFIED__",
			"display_title": "Unspecified Party",
			"is_virtual_group": 1,
			**measures_from_opening_period(0, 0, 7, 3),
		}
		rows = [dict(classified), dict(unspecified)]
		classified_totals = sum_measure_rows([{**measures_from_opening_period(10, 0, 20, 5)}])
		result = paginate_summary_rows(rows, _spec())
		self.assertEqual(len(result["rows"]), 1)
		self.assertEqual(result["rows"][0]["row_key"], "party:Customer:Alpha")
		self.assertFalse(any(r.get("row_key") == VIRTUAL_PARTY_UNSPECIFIED_KEY for r in result["rows"]))
		self.assertEqual(result["pagination"]["total_rows"], 1)
		# Totals must match visible classified rows only (empty bucket excluded).
		for field in ("period_debit", "period_credit", "opening_debit", "opening_credit"):
			self.assertEqual(
				float(result["totals"].get(field) or 0),
				float(classified_totals.get(field) or 0),
				field,
			)
		self.assertEqual(float(result["totals"]["period_debit"]), 20.0)
		self.assertEqual(float(result["totals"]["period_credit"]), 5.0)

	def test_unified_parties_tab_skipped_in_page_js(self):
		page = Path(
			"/workspace/development/frappe-bench/apps/erpnext_extensions/"
			"erpnext_extensions/erpnext_extensions/page/account_explorer/account_explorer.js"
		).read_text()
		self.assertIn('axis.id === "unified_party"', page)
		self.assertIn('if (view_axis === "unified_party")', page)
