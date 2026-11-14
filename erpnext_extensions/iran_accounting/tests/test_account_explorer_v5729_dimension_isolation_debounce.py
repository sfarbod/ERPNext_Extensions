# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer v5.7.29 — global Dimension filters + column-filter debounce."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

import frappe

from erpnext_extensions.iran_accounting.account_explorer import api
from erpnext_extensions.iran_accounting.account_explorer.query_fingerprint import (
	canonical_query_dict,
	query_hash,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	enable_wave2a_analysis,
)


COMPANY = "اسپاد فارمد دارو"
FISCAL_YEAR = "1405"


def _fy_dates():
	fy = frappe.db.get_value(
		"Fiscal Year",
		FISCAL_YEAR,
		["year_start_date", "year_end_date"],
		as_dict=True,
	)
	return {"from_date": str(fy.year_start_date), "to_date": str(fy.year_end_date)}


FY1405 = {"from_date": "2026-03-21", "to_date": "2027-03-20"}


def setUpModule():
	global FY1405
	if frappe.db.exists("Fiscal Year", FISCAL_YEAR):
		FY1405 = _fy_dates()


def _gl_dimension_fields():
	meta = frappe.get_meta("GL Entry")
	fields = []
	for df in meta.fields:
		if df.fieldname in {
			"cost_center",
			"project",
			"department",
			"facility",
			"bank_dimension",
			"bank_account_dimension",
			"foreign_purchase_order",
		} or (df.fieldtype == "Link" and df.options and df.fieldname not in {"account", "party", "against", "company", "finance_book"}):
			if meta.has_field(df.fieldname) and df.fieldname not in fields:
				# Prefer known accounting dimensions present on GL Entry.
				pass
	known = [
		f
		for f in (
			"cost_center",
			"project",
			"department",
			"facility",
			"bank_dimension",
			"bank_account_dimension",
			"foreign_purchase_order",
		)
		if meta.has_field(f)
	]
	return known


def _payload(dimension_type: str, *, selected=None, accounting_dimensions=None):
	document = {
		"company": COMPANY,
		"fiscal_year": FISCAL_YEAR,
		"from_date": FY1405["from_date"],
		"to_date": FY1405["to_date"],
		"hide_zero_rows": 1,
		"status": {
			"include_cancelled_entries": 0,
			"include_opening_entries": 0,
			"include_default_finance_book_entries": 1,
			"include_period_closing_vouchers": 0,
		},
		"accounting_dimensions": {},
		"accounting": {},
		"voucher": {},
		"currency": {},
		"inventory": {},
	}
	if accounting_dimensions is not None:
		document = {**document, "accounting_dimensions": accounting_dimensions}
	return {
		"document_scope": document,
		"analysis_context": {
			"view_axis": "dimension",
			"detail_mode": "summary",
			"dimension_scope": {
				"dimension_type": dimension_type,
				"selected_dimension_value": selected,
			},
			"pagination": {
				"page": 1,
				"page_size": 50,
				"sort_field": "display_code",
				"sort_order": "asc",
			},
		},
	}


@unittest.skipUnless(
	frappe.db.exists("Company", COMPANY) and frappe.db.exists("Fiscal Year", FISCAL_YEAR),
	"Company / FY fixtures unavailable",
)
class TestAccountExplorerV5729GlobalDimensionFilters(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db.exists("Company", COMPANY):
			raise unittest.SkipTest(f"Company {COMPANY} not available")
		enable_wave2a_analysis()
		frappe.set_user("Administrator")
		cls.dim_fields = _gl_dimension_fields()
		if len(cls.dim_fields) < 2:
			raise unittest.SkipTest("Need at least two GL dimension fields")

	def test_query_spec_keeps_all_dimension_document_filters(self):
		primary = self.dim_fields[0]
		other = self.dim_fields[1]
		payload = _payload(
			primary,
			accounting_dimensions={primary: "KEEP-ME", other: "AND-ME"},
		)
		spec = AccountExplorerQuerySpec_from_client(payload)
		self.assertEqual(spec.dimension_scope.dimension_type, primary)
		self.assertEqual(spec.document_scope.accounting_dimensions.get(primary), "KEEP-ME")
		self.assertEqual(spec.document_scope.accounting_dimensions.get(other), "AND-ME")

	def test_dimension_summaries_respect_presentation_type(self):
		for field in self.dim_fields[:3]:
			payload = _payload(field)
			result = api.get_dimension_summary(payload)
			self.assertEqual(result.get("dimension_type"), field)
			for row in result.get("rows") or []:
				self.assertEqual(row.get("dimension_type"), field)
				self.assertTrue(str(row.get("row_key") or "").startswith(f"dimension:{field}:"))

	def test_selected_dimension_analyze_scopes_sql(self):
		primary = "cost_center" if "cost_center" in self.dim_fields else self.dim_fields[0]
		base = api.get_dimension_summary(_payload(primary))
		rows = [r for r in (base.get("rows") or []) if r.get("dimension_value")]
		if not rows:
			self.skipTest(f"No {primary} rows in period")
		selected = rows[0]["dimension_value"]

		scoped = api.get_dimension_summary(
			_payload(
				primary,
				selected=selected,
				accounting_dimensions={primary: selected},
			)
		)
		self.assertEqual(scoped.get("dimension_type"), primary)
		for row in scoped.get("rows") or []:
			self.assertEqual(row.get("dimension_type"), primary)
			self.assertEqual(row.get("dimension_value"), selected)

		col = f"`{primary}`"
		count = frappe.db.sql(
			f"""
			select count(*) from `tabGL Entry`
			where company=%s and posting_date between %s and %s
			  and ifnull(is_cancelled, 0)=0 and {col}=%s
			""",
			(COMPANY, FY1405["from_date"], FY1405["to_date"], selected),
		)[0][0]
		self.assertGreaterEqual(int(count), 0)

		unscoped_selected = next(r for r in rows if r.get("dimension_value") == selected)
		scoped_row = (scoped.get("rows") or [None])[0]
		self.assertIsNotNone(scoped_row)
		self.assertAlmostEqual(
			float(scoped_row.get("period_debit") or 0),
			float(unscoped_selected.get("period_debit") or 0),
			places=3,
		)

	def test_and_of_two_dimensions_narrows_or_empties(self):
		if "department" not in self.dim_fields or "project" not in self.dim_fields:
			self.skipTest("department/project required")
		dept = "ایمنی و بهداشت - E"
		# Impossible project → empty intersection must stay empty (no silent strip).
		payload = _payload(
			"department",
			selected=dept,
			accounting_dimensions={
				"department": dept,
				"project": "__AE_GLOBAL_EMPTY_PROJECT__",
			},
		)
		spec = AccountExplorerQuerySpec_from_client(payload)
		self.assertEqual(
			spec.document_scope.accounting_dimensions.get("project"),
			"__AE_GLOBAL_EMPTY_PROJECT__",
		)
		result = api.get_dimension_summary(payload)
		self.assertEqual(len(result.get("rows") or []), 0)

	def test_fingerprints_distinguish_dimension_types(self):
		a = AccountExplorerQuerySpec_from_client(_payload(self.dim_fields[0]))
		b = AccountExplorerQuerySpec_from_client(_payload(self.dim_fields[1]))
		self.assertNotEqual(query_hash(a), query_hash(b))
		ca = canonical_query_dict(a)
		cb = canonical_query_dict(b)
		self.assertEqual(ca["dimension_scope"]["dimension_type"], self.dim_fields[0])
		self.assertEqual(cb["dimension_scope"]["dimension_type"], self.dim_fields[1])
		self.assertNotEqual(ca["dimension_scope"], cb["dimension_scope"])

	def test_js_replace_dimension_keeps_sibling_types(self):
		app_root = Path(frappe.get_app_path("erpnext_extensions"))
		harness = app_root / "iran_accounting" / "e2e" / "playwright" / "ae_v5729_unit.cjs"
		if not harness.is_file():
			self.skipTest("v5729 unit harness absent")
		result = subprocess.run(
			["node", str(harness)],
			capture_output=True,
			text=True,
			check=False,
		)
		self.assertEqual(result.returncode, 0, msg=result.stderr or result.stdout)
		payload = json.loads(result.stdout)
		self.assertEqual(payload.get("failed"), 0)
		self.assertGreaterEqual(payload.get("total", 0), 5)
		names = {c.get("name") for c in payload.get("checks") or []}
		self.assertIn("replace_dimension_keeps_sibling_types", names)
		self.assertIn("projection_keeps_all_dimension_filters", names)
		self.assertIn("filter_summary_shows_all_dimension_types", names)

	def test_source_debounce_interval_is_6000ms(self):
		app_root = Path(frappe.get_app_path("erpnext_extensions"))
		controller = (
			app_root
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "account_explorer.js"
		).read_text(encoding="utf-8")
		self.assertIn("AE_COLUMN_FILTER_DEBOUNCE_MS = 6000", controller)
		self.assertIn("setTimeout(apply, AE_COLUMN_FILTER_DEBOUNCE_MS)", controller)
		self.assertNotIn("setTimeout(apply, 500)", controller)
		self.assertNotIn("AE_COLUMN_FILTER_DEBOUNCE_MS = 3000", controller)
		self.assertIn("opts.immediate", controller)
		self.assertIn("activate_dimension_type_scope", controller)
		self.assertIn("Intended filter semantics", controller)
		self.assertIn("cancel_pending_column_filter_debounce", controller)


class TestAccountExplorerV5729DebounceContract(unittest.TestCase):
	def test_adapter_enter_flush_and_clear_hooks(self):
		app_root = Path(frappe.get_app_path("erpnext_extensions"))
		adapter = (
			app_root
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "adapters"
			/ "ae_datatable_adapter.js"
		).read_text(encoding="utf-8")
		self.assertIn("_bind_column_filter_immediate_actions", adapter)
		self.assertIn('event.key !== "Enter"', adapter)
		self.assertIn("immediate: !!immediate", adapter)
		self.assertIn('input.value = value || ""', adapter)

	def test_analysis_filters_replace_dimension_policy(self):
		app_root = Path(frappe.get_app_path("erpnext_extensions"))
		af = (
			app_root
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "core"
			/ "explorer_analysis_filters.js"
		).read_text(encoding="utf-8")
		self.assertIn("upsert this Dimension Type only", af)
		self.assertIn("keep siblings", af)
		self.assertNotIn("next.dimensions = {}", af)

	def test_dimension_type_ui_presentation_only(self):
		app_root = Path(frappe.get_app_path("erpnext_extensions"))
		controller = (
			app_root
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "account_explorer.js"
		).read_text(encoding="utf-8")
		self.assertIn("park_dimension_type_state", controller)
		self.assertIn("activate_dimension_type_scope", controller)
		self.assertIn("_dimension_type_ui", controller)
		self.assertIn("Never owns query filters", controller)
		self.assertNotIn("this.render_column_filter_chips($bar)", controller)
		css = (
			app_root
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "account_explorer.css"
		).read_text(encoding="utf-8")
		self.assertIn(".ae-pagination-bar .ae-column-filter-chips", css)
		self.assertIn("display: none !important", css)
