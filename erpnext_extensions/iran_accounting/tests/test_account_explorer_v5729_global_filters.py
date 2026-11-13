# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer v5.7.29 — global cross-axis filter contract (read-only)."""

from __future__ import annotations

import json
import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.api import (
	get_account_summary,
	get_currency_summary,
	get_dimension_summary,
	get_item_group_summary,
	get_item_summary,
	get_party_summary,
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
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	enable_wave2a_analysis,
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


def _indep(**dims) -> dict:
	fy = _fy()
	clauses = [
		"company=%s",
		"ifnull(is_cancelled,0)=0",
		"posting_date between %s and %s",
		"voucher_type != 'Period Closing Voucher'",
		"ifnull(is_opening,'No')='No'",
	]
	params: list = [COMPANY, fy.year_start_date, fy.year_end_date]
	for field, value in dims.items():
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


def _axis_ctx(axis: str, *, dim_type="department", selected=None, **extra):
	ctx = {
		"view_axis": axis,
		"detail_mode": "summary",
		"dimension_scope": {
			"dimension_type": dim_type,
			"selected_dimension_value": selected,
		},
		"account_scope": {"mode": "tree"},
		"pagination": {
			"page": 1,
			"page_size": 100,
			"sort_field": "display_code",
			"sort_order": "asc",
		},
	}
	if axis == "account_level":
		ctx["level_sequence"] = extra.get("level_sequence", 1)
	if axis == "party":
		ctx["pagination"]["sort_field"] = "party_type"
	if axis == "voucher":
		ctx["pagination"]["sort_field"] = "posting_date"
		ctx["pagination"]["sort_order"] = "desc"
	if axis == "currency":
		ctx["pagination"]["sort_field"] = "currency"
	return ctx


def _summary(method, doc, axis, **kw):
	return method(
		json.dumps({"document_scope": doc, "analysis_context": _axis_ctx(axis, **kw)})
	)


@unittest.skipUnless(
	frappe.db.exists("Company", COMPANY) and frappe.db.exists("Fiscal Year", FISCAL_YEAR),
	"Company / FY fixtures unavailable",
)
class TestAccountExplorerV5729GlobalFilters(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		enable_wave2a_analysis()
		frappe.set_user("Administrator")
		cls.indep_dept = _indep(department=DEPT)
		if flt(cls.indep_dept.n) <= 0:
			raise unittest.SkipTest(f"No GL for department {DEPT}")

	def test_department_only_sql_recon(self):
		indep = self.indep_dept
		out = _summary(get_account_summary, _doc(department=DEPT), "account_level", selected=DEPT)
		totals = out.get("totals") or {}
		self.assertAlmostEqual(flt(totals.get("period_debit")), flt(indep.d), places=2)
		self.assertAlmostEqual(flt(totals.get("period_credit")), flt(indep.c), places=2)

	def test_empty_project_intersection_stays_empty_on_all_axes(self):
		doc = _doc(department=DEPT, project="__AE_NO_SUCH_PROJECT__")
		indep = _indep(department=DEPT, project="__AE_NO_SUCH_PROJECT__")
		self.assertEqual(int(indep.n), 0)
		for axis, method in (
			("account_level", get_account_summary),
			("party", get_party_summary),
			("voucher", get_voucher_summary),
			("item", get_item_summary),
			("item_group", get_item_group_summary),
			("currency", get_currency_summary),
			("dimension", get_dimension_summary),
		):
			out = _summary(method, doc, axis, selected=DEPT)
			rows = out.get("rows") or []
			totals = out.get("totals") or {}
			# Zero matching GLE → empty grid / zero turnover (never company-wide fallback).
			self.assertEqual(len(rows), 0, msg=axis)
			self.assertAlmostEqual(flt(totals.get("period_debit") or 0), 0.0, places=2, msg=axis)
			self.assertAlmostEqual(flt(totals.get("period_credit") or 0), 0.0, places=2, msg=axis)

	def test_department_and_cost_center_and(self):
		row = frappe.db.sql(
			"""
			select cost_center from `tabGL Entry`
			where company=%s and department=%s and ifnull(cost_center,'')!=''
			  and ifnull(is_cancelled,0)=0
			limit 1
			""",
			(COMPANY, DEPT),
		)
		if not row:
			self.skipTest("no department+cost_center overlap")
		cc = row[0][0]
		indep = _indep(department=DEPT, cost_center=cc)
		out = _summary(
			get_account_summary,
			_doc(department=DEPT, cost_center=cc),
			"account_level",
			selected=DEPT,
		)
		totals = out.get("totals") or {}
		self.assertAlmostEqual(flt(totals.get("period_debit")), flt(indep.d), places=2)
		self.assertLessEqual(flt(indep.n), flt(self.indep_dept.n))

	def test_three_filters_preserve_in_query_spec(self):
		doc = _doc(department=DEPT, cost_center="CC-X", project="PRJ-Y")
		spec = AccountExplorerQuerySpec_from_client(
			{
				"document_scope": doc,
				"analysis_context": _axis_ctx("account_level", selected=DEPT),
			}
		)
		self.assertEqual(
			spec.document_scope.accounting_dimensions,
			{"department": DEPT, "cost_center": "CC-X", "project": "PRJ-Y"},
		)
		self.assertTrue(spec_has_advanced_gle_filters(spec))

	def test_scoped_gle_matches_independent_for_department(self):
		indep = self.indep_dept
		spec = AccountExplorerQuerySpec_from_client(
			{
				"document_scope": _doc(department=DEPT),
				"analysis_context": _axis_ctx("account_level", selected=DEPT),
			}
		)
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

	def test_party_and_voucher_respect_department(self):
		indep = self.indep_dept
		for axis, method in (("party", get_party_summary), ("voucher", get_voucher_summary)):
			out = _summary(method, _doc(department=DEPT), axis, selected=DEPT)
			self.assertIn("rows", out)
			if axis == "voucher":
				self.assertGreater(len(out.get("rows") or []), 0)
				for row in (out.get("rows") or [])[:15]:
					match = frappe.db.sql(
						"""
						select count(*) from `tabGL Entry`
						where company=%s and voucher_type=%s and voucher_no=%s
						  and ifnull(is_cancelled,0)=0 and department=%s
						""",
						(COMPANY, row.get("voucher_type"), row.get("voucher_no"), DEPT),
					)[0][0]
					self.assertGreater(int(match), 0)
			self.assertGreater(flt(indep.n), 0)

	def test_baseline_no_filter_wider_than_department(self):
		baseline = _indep()
		self.assertGreater(flt(baseline.n), flt(self.indep_dept.n))
		out = _summary(get_account_summary, _doc(), "account_level")
		totals = out.get("totals") or {}
		self.assertGreater(flt(totals.get("period_debit") or 0), flt(self.indep_dept.d))
