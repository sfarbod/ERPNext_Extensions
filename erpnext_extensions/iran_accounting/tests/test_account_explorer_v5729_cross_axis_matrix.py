# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer v5.7.29 — cross-axis Dimension filter matrix (read-only)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.api import (
	get_account_summary,
	get_item_summary,
	get_party_summary,
	get_voucher_summary,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)


COMPANY = "اسپاد فارمد دارو"
FISCAL_YEAR = "1405"
DEPT = "ایمنی و بهداشت - E"


def _fy():
	return frappe.db.get_value(
		"Fiscal Year", FISCAL_YEAR, ["year_start_date", "year_end_date"], as_dict=True
	)


def _doc(**dims):
	fy = _fy()
	return {
		"company": COMPANY,
		"fiscal_year": FISCAL_YEAR,
		"from_date": str(fy.year_start_date),
		"to_date": str(fy.year_end_date),
		"hide_zero_rows": 1,
		"status": {
			"include_cancelled_entries": 0,
			"include_opening_entries": 0,
			"include_default_finance_book_entries": 1,
			"include_period_closing_vouchers": 0,
		},
		"accounting_dimensions": dict(dims),
		"accounting": {},
		"voucher": {},
		"currency": {},
		"inventory": {},
	}


def _indep(department: str | None = None, **extra_dims) -> dict:
	fy = _fy()
	clauses = [
		"company=%s",
		"ifnull(is_cancelled,0)=0",
		"posting_date between %s and %s",
		"voucher_type != 'Period Closing Voucher'",
		"ifnull(is_opening,'No')='No'",
	]
	params: list = [COMPANY, fy.year_start_date, fy.year_end_date]
	if department is not None:
		clauses.append("department=%s")
		params.append(department)
	for field, value in extra_dims.items():
		clauses.append(f"`{field}`=%s")
		params.append(value)
	return frappe.db.sql(
		f"""
		select ifnull(sum(debit),0) d, ifnull(sum(credit),0) c, count(*) n
		from `tabGL Entry`
		where {' and '.join(clauses)}
		""",
		tuple(params),
		as_dict=True,
	)[0]


@unittest.skipUnless(
	frappe.db.exists("Company", COMPANY) and frappe.db.exists("Fiscal Year", FISCAL_YEAR),
	"Company / FY fixtures unavailable",
)
class TestAccountExplorerV5729CrossAxisMatrix(unittest.TestCase):
	def test_controller_documents_restore_semantics(self):
		js = (
			Path(frappe.get_app_path("erpnext_extensions"))
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "account_explorer.js"
		).read_text(encoding="utf-8")
		self.assertIn("restore_parked_dimension_filters_to_active_bag", js)
		self.assertIn("Intended filter semantics", js)
		self.assertIn("Clear Selection: row-checkbox UI only", js)
		self.assertIn("Reset All Axes: grid column preferences only", js)
		self.assertIn("leaving_dimension_axis", js)

	def test_account_levels_with_department_match_sql(self):
		indep = _indep(DEPT)
		out = get_account_summary(
			json.dumps(
				{
					"document_scope": _doc(department=DEPT),
					"analysis_context": {
						"view_axis": "account_level",
						"detail_mode": "summary",
						"level_sequence": 1,
						"dimension_scope": {
							"dimension_type": "department",
							"selected_dimension_value": DEPT,
						},
						"account_scope": {"mode": "tree"},
						"pagination": {
							"page": 1,
							"page_size": 100,
							"sort_field": "display_code",
							"sort_order": "asc",
						},
					},
				}
			)
		)
		totals = out.get("totals") or {}
		self.assertAlmostEqual(flt(totals.get("period_debit")), flt(indep.d), places=2)
		self.assertAlmostEqual(flt(totals.get("period_credit")), flt(indep.c), places=2)

	def test_dimension_axis_isolation_strips_other_types(self):
		spec = AccountExplorerQuerySpec_from_client(
			{
				"document_scope": _doc(department=DEPT, cost_center="X", project="Y"),
				"analysis_context": {
					"view_axis": "dimension",
					"detail_mode": "summary",
					"dimension_scope": {
						"dimension_type": "cost_center",
						"selected_dimension_value": None,
					},
					"pagination": {
						"page": 1,
						"page_size": 20,
						"sort_field": "display_code",
						"sort_order": "asc",
					},
				},
			}
		)
		# Active Dimension Type may keep its own document dim; other types are stripped.
		self.assertEqual(spec.document_scope.accounting_dimensions, {"cost_center": "X"})
		self.assertNotIn("department", spec.document_scope.accounting_dimensions)
		self.assertNotIn("project", spec.document_scope.accounting_dimensions)

	def test_gl_axes_accept_department_document_filter(self):
		indep = _indep(DEPT)
		for axis, method, extra in (
			(
				"party",
				get_party_summary,
				{"sort_field": "party_type"},
			),
			(
				"voucher",
				get_voucher_summary,
				{"sort_field": "posting_date", "sort_order": "desc"},
			),
		):
			out = method(
				json.dumps(
					{
						"document_scope": _doc(department=DEPT),
						"analysis_context": {
							"view_axis": axis,
							"detail_mode": "summary",
							"dimension_scope": {
								"dimension_type": "department",
								"selected_dimension_value": DEPT,
							},
							"pagination": {
								"page": 1,
								"page_size": 50,
								"sort_field": extra.get("sort_field", "display_code"),
								"sort_order": extra.get("sort_order", "asc"),
							},
						},
					}
				)
			)
			self.assertIsInstance(out.get("rows"), list)
			# Party/voucher row sums are not leaf GL totals; ensure request accepted and non-empty
			# for this department which has activity.
			if axis == "voucher":
				self.assertGreater(len(out.get("rows") or []), 0, msg=axis)
				for row in (out.get("rows") or [])[:20]:
					match = frappe.db.sql(
						"""
						select count(*) from `tabGL Entry`
						where company=%s and voucher_type=%s and voucher_no=%s
						  and ifnull(is_cancelled,0)=0 and department=%s
						""",
						(COMPANY, row.get("voucher_type"), row.get("voucher_no"), DEPT),
					)[0][0]
					self.assertGreater(int(match), 0)
			else:
				# party axis may be empty if no party-linked lines for this dept; still must not 500
				self.assertIn("rows", out)
			self.assertGreater(flt(indep.n), 0)

	def test_and_of_department_and_cost_center_stricter_than_department(self):
		cc = frappe.db.sql(
			"""
			select cost_center from `tabGL Entry`
			where company=%s and department=%s and ifnull(cost_center,'')!=''
			  and ifnull(is_cancelled,0)=0
			limit 1
			""",
			(COMPANY, DEPT),
		)
		if not cc:
			self.skipTest("no shared department+cost_center lines")
		cost_center = cc[0][0]
		dept_only = _indep(DEPT)
		both = _indep(DEPT, cost_center=cost_center)
		self.assertLessEqual(flt(both.n), flt(dept_only.n))
		out = get_account_summary(
			json.dumps(
				{
					"document_scope": _doc(department=DEPT, cost_center=cost_center),
					"analysis_context": {
						"view_axis": "account_level",
						"detail_mode": "summary",
						"level_sequence": 1,
						"dimension_scope": {
							"dimension_type": "department",
							"selected_dimension_value": DEPT,
						},
						"account_scope": {"mode": "tree"},
						"pagination": {
							"page": 1,
							"page_size": 100,
							"sort_field": "display_code",
							"sort_order": "asc",
						},
					},
				}
			)
		)
		totals = out.get("totals") or {}
		self.assertAlmostEqual(flt(totals.get("period_debit")), flt(both.d), places=2)

	def test_items_axis_accepts_department_filter_without_error(self):
		out = get_item_summary(
			json.dumps(
				{
					"document_scope": _doc(department=DEPT),
					"analysis_context": {
						"view_axis": "item",
						"detail_mode": "summary",
						"dimension_scope": {
							"dimension_type": "department",
							"selected_dimension_value": DEPT,
						},
						"pagination": {
							"page": 1,
							"page_size": 50,
							"sort_field": "display_code",
							"sort_order": "asc",
						},
					},
				}
			)
		)
		self.assertIn("rows", out)

	def test_unrelated_department_not_in_filtered_dimension_summary(self):
		from erpnext_extensions.iran_accounting.account_explorer.api import get_dimension_summary

		out = get_dimension_summary(
			json.dumps(
				{
					"document_scope": _doc(department=DEPT),
					"analysis_context": {
						"view_axis": "dimension",
						"detail_mode": "summary",
						"dimension_scope": {
							"dimension_type": "department",
							"selected_dimension_value": DEPT,
						},
						"pagination": {
							"page": 1,
							"page_size": 50,
							"sort_field": "display_code",
							"sort_order": "asc",
						},
					},
				}
			)
		)
		values = [r.get("dimension_value") for r in (out.get("rows") or [])]
		self.assertEqual(values, [DEPT])
		self.assertNotIn("واحد انبار - E", values)
