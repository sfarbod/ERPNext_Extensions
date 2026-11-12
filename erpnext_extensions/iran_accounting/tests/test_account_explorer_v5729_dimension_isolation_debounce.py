# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer v5.7.29 — Dimension Type isolation + column-filter debounce contracts."""

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
FY1405 = {
	"company": COMPANY,
	"fiscal_year": "1405",
	"from_date": "2026-03-21",
	"to_date": "2027-03-20",
	"status": {
		"include_opening_entries": 1,
		"include_cancelled_entries": 0,
		"include_default_finance_book_entries": 1,
		"include_period_closing_vouchers": 0,
	},
	"accounting": {},
	"voucher": {},
	"currency": {"currency_type": "account_currency"},
	"accounting_dimensions": {},
	"inventory": {},
	"hide_zero_rows": 0,
}


def _gl_dimension_fields() -> list[str]:
	meta = frappe.get_meta("GL Entry")
	candidates = [
		"cost_center",
		"project",
		"department",
		"bank_dimension",
		"bank_account_dimension",
		"facility",
		"foreign_purchase_order",
	]
	return [field for field in candidates if meta.has_field(field)]


def _payload(dimension_type: str, *, selected=None, accounting_dimensions=None):
	document = dict(FY1405)
	if accounting_dimensions is not None:
		document = {**document, "accounting_dimensions": accounting_dimensions}
	analysis = {
		"view_axis": "dimension",
		"dimension_scope": {
			"dimension_type": dimension_type,
			"selected_dimension_value": selected,
		},
	}
	return json.dumps({"document_scope": document, "analysis_context": analysis})


class TestAccountExplorerV5729DimensionIsolation(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db:
			raise unittest.SkipTest("Database not available")
		if not frappe.db.exists("Company", COMPANY):
			raise unittest.SkipTest(f"Company {COMPANY} not available")
		enable_wave2a_analysis()
		frappe.set_user("Administrator")
		cls.dim_fields = _gl_dimension_fields()
		if len(cls.dim_fields) < 2:
			raise unittest.SkipTest("Need at least two GL dimension fields")

	def test_query_spec_strips_stale_dimension_document_filters(self):
		primary = self.dim_fields[0]
		other = self.dim_fields[1]
		payload = _payload(
			primary,
			accounting_dimensions={primary: "KEEP-ME", other: "LEAK-ME"},
		)
		spec = AccountExplorerQuerySpec_from_client(payload)
		self.assertEqual(spec.dimension_scope.dimension_type, primary)
		self.assertIn(primary, spec.document_scope.accounting_dimensions)
		self.assertNotIn(other, spec.document_scope.accounting_dimensions)
		self.assertEqual(spec.document_scope.accounting_dimensions.get(primary), "KEEP-ME")

	def test_dimension_summaries_are_type_isolated(self):
		for field in self.dim_fields:
			other = next(f for f in self.dim_fields if f != field)
			payload = _payload(
				field,
				accounting_dimensions={other: "SHOULD-NOT-APPLY"},
			)
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
		other = next(f for f in self.dim_fields if f != primary)

		scoped = api.get_dimension_summary(
			_payload(
				primary,
				selected=selected,
				accounting_dimensions={other: "LEAK"},
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

		# Totals must not be diluted by the stale other-type document filter.
		unscoped_selected = next(r for r in rows if r.get("dimension_value") == selected)
		scoped_row = (scoped.get("rows") or [None])[0]
		self.assertIsNotNone(scoped_row)
		self.assertAlmostEqual(
			float(scoped_row.get("period_debit") or 0),
			float(unscoped_selected.get("period_debit") or 0),
			places=3,
		)

	def test_fingerprints_distinguish_dimension_types(self):
		a = AccountExplorerQuerySpec_from_client(_payload(self.dim_fields[0]))
		b = AccountExplorerQuerySpec_from_client(_payload(self.dim_fields[1]))
		self.assertNotEqual(query_hash(a), query_hash(b))
		ca = canonical_query_dict(a)
		cb = canonical_query_dict(b)
		self.assertEqual(ca["dimension_scope"]["dimension_type"], self.dim_fields[0])
		self.assertEqual(cb["dimension_scope"]["dimension_type"], self.dim_fields[1])
		self.assertNotEqual(ca["dimension_scope"], cb["dimension_scope"])

	def test_js_replace_dimension_clears_other_types(self):
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

	def test_source_debounce_interval_is_500ms(self):
		app_root = Path(frappe.get_app_path("erpnext_extensions"))
		controller = (
			app_root
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "account_explorer.js"
		).read_text(encoding="utf-8")
		self.assertIn("setTimeout(apply, 500)", controller)
		self.assertIn("opts.immediate", controller)
		self.assertIn("isolate_dimension_type_analysis_scope", controller)


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
		self.assertIn("next.dimensions = {}", af)
		self.assertIn('view_axis === "dimension"', af)

	def test_dimension_type_park_activate_and_no_pagination_chips(self):
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
		self.assertIn("_dimension_type_parked", controller)
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
