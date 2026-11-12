# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer v5.7.29 — Dimension GL filter data accuracy (read-only)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.api import (
	get_account_summary,
	get_dimension_summary,
	get_voucher_summary,
)
from erpnext_extensions.iran_accounting.account_explorer.gle_filters import (
	apply_period_turnover_filters,
	apply_scoped_gle_filters,
	spec_has_advanced_gle_filters,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)


COMPANY = "اسپاد فارمد دارو"
FISCAL_YEAR = "1405"
DEPT_A = "ایمنی و بهداشت - E"  # has activity, no loan accounts
DEPT_WAREHOUSE = "واحد انبار - E"  # high activity


def _fy_dates():
	return frappe.db.get_value(
		"Fiscal Year", FISCAL_YEAR, ["year_start_date", "year_end_date"], as_dict=True
	)


def _base_doc(**dims):
	fy = _fy_dates()
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


def _indep_dept_period(department: str) -> dict:
	fy = _fy_dates()
	return frappe.db.sql(
		"""
		select ifnull(sum(debit),0) as d, ifnull(sum(credit),0) as c, count(*) as n
		from `tabGL Entry`
		where company=%s
		  and ifnull(is_cancelled,0)=0
		  and posting_date between %s and %s
		  and department=%s
		  and voucher_type != 'Period Closing Voucher'
		  and ifnull(is_opening,'No')='No'
		""",
		(COMPANY, fy.year_start_date, fy.year_end_date, department),
		as_dict=True,
	)[0]


@unittest.skipUnless(
	frappe.db.exists("Company", COMPANY) and frappe.db.exists("Fiscal Year", FISCAL_YEAR),
	"Company / FY fixtures unavailable",
)
class TestAccountExplorerV5729DimensionDataAccuracy(unittest.TestCase):
	def test_department_field_maps_to_gl_entry(self):
		meta = frappe.get_meta("GL Entry")
		self.assertTrue(meta.has_field("department"))
		self.assertEqual(meta.get_field("department").options, "Department")

	def test_query_spec_strips_cross_dimension_contamination(self):
		spec = AccountExplorerQuerySpec_from_client(
			{
				"document_scope": _base_doc(
					department=DEPT_A, cost_center="SHOULD-NOT-APPLY"
				),
				"analysis_context": {
					"view_axis": "dimension",
					"detail_mode": "summary",
					"dimension_scope": {
						"dimension_type": "department",
						"selected_dimension_value": DEPT_A,
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
		self.assertEqual(spec.document_scope.accounting_dimensions, {"department": DEPT_A})
		self.assertNotIn("cost_center", spec.document_scope.accounting_dimensions)

	def test_scoped_gle_matches_independent_sql_for_department(self):
		indep = _indep_dept_period(DEPT_A)
		self.assertGreater(flt(indep.n), 0)
		spec = AccountExplorerQuerySpec_from_client(
			{
				"document_scope": _base_doc(department=DEPT_A),
				"analysis_context": {
					"view_axis": "account_level",
					"detail_mode": "summary",
					"level_sequence": 1,
					"dimension_scope": {
						"dimension_type": "department",
						"selected_dimension_value": DEPT_A,
					},
					"account_scope": {"mode": "tree"},
					"pagination": {
						"page": 1,
						"page_size": 50,
						"sort_field": "display_code",
						"sort_order": "asc",
					},
				},
			}
		)
		self.assertTrue(spec_has_advanced_gle_filters(spec))
		gle = frappe.qb.DocType("GL Entry")
		from frappe.query_builder.functions import Count, Sum

		q = frappe.qb.from_(gle).select(
			Sum(gle.debit).as_("d"), Sum(gle.credit).as_("c"), Count("*").as_("n")
		)
		q = apply_scoped_gle_filters(q, gle, spec)
		q = apply_period_turnover_filters(q, gle, spec)
		row = q.run(as_dict=True)[0]
		self.assertAlmostEqual(flt(row.d), flt(indep.d), places=2)
		self.assertAlmostEqual(flt(row.c), flt(indep.c), places=2)
		self.assertEqual(int(row.n), int(indep.n))

	def test_account_summary_totals_match_independent_department(self):
		indep = _indep_dept_period(DEPT_A)
		out = get_account_summary(
			json.dumps(
				{
					"document_scope": _base_doc(department=DEPT_A),
					"analysis_context": {
						"view_axis": "account_level",
						"detail_mode": "summary",
						"level_sequence": 1,
						"dimension_scope": {
							"dimension_type": "department",
							"selected_dimension_value": DEPT_A,
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
		# Loan / facility liability groups must not appear for this department.
		codes = {str(r.get("display_code") or "") for r in (out.get("rows") or [])}
		self.assertNotIn("21", codes)
		self.assertNotIn("22", codes)
		loanish = [
			r
			for r in (out.get("rows") or [])
			if any(
				token in f"{r.get('display_title') or ''}{r.get('display_code') or ''}"
				for token in ("وام", "تسهیلات", "loan", "Loan")
			)
		]
		self.assertEqual(loanish, [])

	def test_empty_department_returns_zero_activity(self):
		empty_name = "__AE_V5729_EMPTY_DEPARTMENT__"
		indep = _indep_dept_period(empty_name)
		self.assertEqual(int(indep.n), 0)
		out = get_dimension_summary(
			json.dumps(
				{
					"document_scope": _base_doc(department=empty_name),
					"analysis_context": {
						"view_axis": "dimension",
						"detail_mode": "summary",
						"dimension_scope": {
							"dimension_type": "department",
							"selected_dimension_value": empty_name,
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
		)
		self.assertEqual(out.get("rows") or [], [])

	def test_department_a_and_b_are_isolated(self):
		a = _indep_dept_period(DEPT_A)
		b = _indep_dept_period(DEPT_WAREHOUSE)
		self.assertGreater(flt(b.d), flt(a.d))
		out_a = get_dimension_summary(
			json.dumps(
				{
					"document_scope": _base_doc(department=DEPT_A),
					"analysis_context": {
						"view_axis": "dimension",
						"detail_mode": "summary",
						"dimension_scope": {
							"dimension_type": "department",
							"selected_dimension_value": DEPT_A,
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
		)
		values = [r.get("dimension_value") for r in (out_a.get("rows") or [])]
		self.assertEqual(values, [DEPT_A])
		row = out_a["rows"][0]
		self.assertAlmostEqual(flt(row.get("period_debit")), flt(a.d), places=2)
		self.assertNotAlmostEqual(flt(row.get("period_debit")), flt(b.d), places=2)

	def test_vouchers_for_department_all_have_matching_gl_lines(self):
		out = get_voucher_summary(
			json.dumps(
				{
					"document_scope": _base_doc(department=DEPT_A),
					"analysis_context": {
						"view_axis": "voucher",
						"detail_mode": "summary",
						"dimension_scope": {
							"dimension_type": "department",
							"selected_dimension_value": DEPT_A,
						},
						"pagination": {
							"page": 1,
							"page_size": 40,
							"sort_field": "posting_date",
							"sort_order": "desc",
						},
					},
				}
			)
		)
		rows = out.get("rows") or []
		self.assertGreater(len(rows), 0)
		for row in rows:
			match = frappe.db.sql(
				"""
				select count(*) from `tabGL Entry`
				where company=%s and voucher_type=%s and voucher_no=%s
				  and ifnull(is_cancelled,0)=0 and department=%s
				""",
				(COMPANY, row.get("voucher_type"), row.get("voucher_no"), DEPT_A),
			)[0][0]
			self.assertGreater(int(match), 0, msg=row.get("voucher_no"))

	def test_controller_restores_parked_dims_when_leaving_dimensions_axis(self):
		app_root = Path(frappe.get_app_path("erpnext_extensions"))
		controller = (
			app_root
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "account_explorer.js"
		).read_text(encoding="utf-8")
		self.assertIn("restore_parked_dimension_filters_to_active_bag", controller)
		self.assertIn("leaving_dimension_axis", controller)
		# Restore must run when leaving Dimensions, not when switching Dimension Types only.
		self.assertIn(
			"if (leaving_dimension_axis) {\n\t\t\tthis.restore_parked_dimension_filters_to_active_bag();",
			controller.replace("\r\n", "\n"),
		)
