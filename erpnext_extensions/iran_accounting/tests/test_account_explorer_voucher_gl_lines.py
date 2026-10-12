# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

from decimal import Decimal

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt, getdate

from erpnext_extensions.iran_accounting.account_explorer.export import _format_export_date
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)
from erpnext_extensions.iran_accounting.account_explorer.voucher_gl_lines import (
	build_voucher_gl_line_summary,
)


def _pick_company() -> str | None:
	for name in ("اسپاد فارمد دارو", "_Test Company"):
		if frappe.db.exists("Company", name):
			return name
	return frappe.db.get_value("Company", {}, "name")


class TestAccountExplorerVoucherGLLines(FrappeTestCase):
	def setUp(self):
		self.company = _pick_company()
		if not self.company:
			self.skipTest("No Company available")
		fy = frappe.db.get_value(
			"Fiscal Year",
			{"year_start_date": ("<=", "2026-06-01"), "year_end_date": (">=", "2026-06-01")},
			["name", "year_start_date", "year_end_date"],
			as_dict=True,
		)
		if not fy:
			self.skipTest("No Fiscal Year covering 2026")
		self.fy = fy

	def _payload(self, **overrides):
		payload = {
			"document_scope": {
				"company": self.company,
				"fiscal_year": self.fy.name,
				"from_date": str(self.fy.year_start_date),
				"to_date": str(self.fy.year_end_date),
				"hide_zero_rows": 1,
				"status": {
					"include_opening_entries": 1,
					"include_cancelled_entries": 0,
					"include_default_finance_book_entries": 1,
					"include_period_closing_vouchers": 0,
				},
			},
			"analysis_context": {
				"view_axis": "voucher",
				"detail_mode": "summary",
				"page": 1,
				"page_size": 50,
				"sort_field": "posting_date",
				"sort_order": "asc",
			},
		}
		payload["document_scope"].update(overrides.get("document_scope", {}))
		payload["analysis_context"].update(overrides.get("analysis_context", {}))
		return payload

	def test_gl_line_identity_and_totals(self):
		spec = AccountExplorerQuerySpec_from_client(self._payload(), require_dates=True)
		result = build_voucher_gl_line_summary(spec)
		rows = result.get("rows") or []
		if not rows:
			self.skipTest("No GL lines in scope")
		keys = [row["row_key"] for row in rows]
		self.assertEqual(len(keys), len(set(keys)))
		self.assertTrue(all(row.get("gl_entry") for row in rows))
		self.assertTrue(all("account_code" in row for row in rows))

		page_debit = sum((Decimal(str(flt(row["debit"]))) for row in rows), Decimal("0"))
		page_credit = sum((Decimal(str(flt(row["credit"]))) for row in rows), Decimal("0"))
		# Full filtered totals must not equal page totals unless the dataset fits one page.
		totals = result["totals"]
		total_rows = result["pagination"]["total_rows"]
		if total_rows > len(rows):
			self.assertNotEqual(Decimal(str(totals["debit"])), page_debit)
		self.assertEqual(
			Decimal(str(totals["net"])),
			Decimal(str(totals["debit"])) - Decimal(str(totals["credit"])),
		)

	def test_multiple_lines_same_voucher_remain_separate(self):
		spec = AccountExplorerQuerySpec_from_client(self._payload(), require_dates=True)
		result = build_voucher_gl_line_summary(spec)
		rows = result.get("rows") or []
		by_voucher = {}
		for row in rows:
			key = (row.get("voucher_type"), row.get("voucher_no"))
			by_voucher.setdefault(key, []).append(row.get("gl_entry"))
		multi = [entries for entries in by_voucher.values() if len(entries) > 1]
		if not multi:
			self.skipTest("No multi-line voucher on first page")
		for entries in multi:
			self.assertEqual(len(entries), len(set(entries)))

	def test_export_date_format_jalali_and_gregorian(self):
		self.assertEqual(_format_export_date("2026-03-21", "Date", "jalali"), "1405-01-01")
		self.assertEqual(_format_export_date("2026-03-21", "Date", "gregorian"), "2026-03-21")
		self.assertEqual(_format_export_date("", "Date", "jalali"), "")

	def test_direct_and_prepared_gl_line_parity(self):
		"""Forced prepared materialization must keep GL Entry row identity."""
		from erpnext_extensions.iran_accounting.account_explorer.prepared_report import (
			_artifact_schema_compatible,
			_materialize_voucher,
		)

		spec = AccountExplorerQuerySpec_from_client(self._payload(), require_dates=True)
		direct = build_voucher_gl_line_summary(spec)
		direct_rows = direct.get("rows") or []
		if not direct_rows:
			self.skipTest("No GL lines in scope")

		artifact = _materialize_voucher(spec)
		self.assertTrue(_artifact_schema_compatible(spec, artifact))
		self.assertEqual((artifact.get("extras") or {}).get("voucher_view_mode"), "gl_line")
		artifact_ids = {row.get("gl_entry") for row in (artifact.get("rows") or [])}
		for row in direct_rows:
			self.assertIn(row.get("gl_entry"), artifact_ids)

		legacy_header = {
			"rows": [{"row_key": "voucher:Journal Entry:X", "voucher_no": "X"}],
			"extras": {},
		}
		self.assertFalse(_artifact_schema_compatible(spec, legacy_header))
