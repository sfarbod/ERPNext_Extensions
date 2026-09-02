# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer v5.1.0 — Jalali date display (frontend) + canonical backend dates."""

from __future__ import annotations

import os
import re
import subprocess
import unittest

import frappe

from erpnext_extensions.iran_accounting.account_explorer import api
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	build_payload,
	current_fiscal_year,
	enable_wave2b_voucher,
	require_site,
)

ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TestAccountExplorerDateDisplay(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		cls.app_root = frappe.get_app_path("erpnext_extensions")
		cls.page_root = os.path.join(cls.app_root, "erpnext_extensions", "page", "account_explorer")
		cls.date_js = os.path.join(cls.page_root, "core", "ae_date_format.js")
		cls.adapter_js = os.path.join(cls.page_root, "adapters", "ae_datatable_adapter.js")
		cls.page_js = os.path.join(cls.page_root, "account_explorer.js")
		cls.mjs_test = os.path.join(cls.page_root, "core", "test_ae_date_format.mjs")

	def _read(self, path: str) -> str:
		with open(path, encoding="utf-8") as handle:
			return handle.read()

	def test_shared_formatter_module_present(self):
		content = self._read(self.date_js)
		self.assertIn("function format_ae_date", content)
		self.assertIn("frappe.datetime.str_to_user", content)
		self.assertNotIn("toshamshi", content)

	def test_page_includes_shared_formatter_before_adapter(self):
		content = self._read(self.page_js)
		date_i = content.index("ae_date_format.js")
		adapter_i = content.index("ae_datatable_adapter.js")
		self.assertLess(date_i, adapter_i)

	def test_datatable_formats_date_fieldtype(self):
		content = self._read(self.adapter_js)
		self.assertIn('source_col?.fieldtype === "Date"', content)
		self.assertIn("format_ae_date(value)", content)

	def test_legacy_and_gl_detail_use_shared_formatter(self):
		content = self._read(self.page_js)
		self.assertIn('col.fieldtype === "Date" ? format_ae_date(value)', content)
		self.assertIn("format_ae_date(header.posting_date", content)

	def test_clipboard_still_uses_raw_row_value(self):
		content = self._read(self.adapter_js)
		self.assertIn("\tcopy_cell_value(row, column_id)", content)
		copy_block = content.split("\tcopy_cell_value(row, column_id)", 1)[1].split("\tcopy_row_tsv(", 1)[0]
		self.assertIn("frappe.utils.copy_to_clipboard(String(value)", copy_block)
		self.assertNotIn("format_ae_date", copy_block)

	def test_node_date_formatter_suite(self):
		if not os.path.isfile(self.mjs_test):
			self.skipTest("missing test_ae_date_format.mjs")
		result = subprocess.run(
			["node", self.mjs_test],
			capture_output=True,
			text=True,
			check=False,
			timeout=30,
		)
		self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class TestAccountExplorerCanonicalPostingDates(unittest.TestCase):
	def setUp(self):
		self.company = require_site(self)
		enable_wave2b_voucher()
		frappe.set_user("Administrator")
		fy = current_fiscal_year(self.company)
		if not fy:
			self.skipTest("No fiscal year")
		self.fiscal_year, self.from_date, self.to_date = fy

	def test_voucher_summary_returns_canonical_iso_posting_date(self):
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={"view_axis": "voucher", "detail_mode": "summary", "sort_field": "posting_date"},
		)
		result = api.get_voucher_summary(payload)
		for row in result.get("rows") or []:
			posting = row.get("posting_date")
			if not posting:
				continue
			self.assertIsInstance(posting, str)
			self.assertRegex(posting, ISO_DATE_RE, f"expected ISO date, got {posting!r}")
			self.assertNotIn("/", posting, "API must not return Jalali slash dates")

	def test_grouped_gl_returns_canonical_iso_posting_date(self):
		summary = api.get_voucher_summary(
			build_payload(
				self.company,
				self.fiscal_year,
				self.from_date,
				self.to_date,
				analysis={"view_axis": "voucher", "detail_mode": "summary", "page_size": 5},
			)
		)
		rows = summary.get("rows") or []
		if not rows:
			self.skipTest("No voucher rows in test company")
		target = rows[0]
		payload = build_payload(
			self.company,
			self.fiscal_year,
			self.from_date,
			self.to_date,
			analysis={
				"view_axis": "voucher",
				"detail_mode": "grouped_gl",
				"sort_field": "posting_date",
				"sort_order": "asc",
				"voucher_scope": {
					"voucher_type": target["voucher_type"],
					"voucher_no": target["voucher_no"],
				},
			},
		)
		result = api.get_grouped_gl_entries(payload)
		header = result.get("voucher_header") or {}
		if header.get("posting_date"):
			self.assertRegex(header["posting_date"], ISO_DATE_RE)
		for row in result.get("rows") or []:
			posting = row.get("posting_date")
			if posting:
				self.assertRegex(posting, ISO_DATE_RE)
