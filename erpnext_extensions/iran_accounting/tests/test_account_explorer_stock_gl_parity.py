# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Stock Ledger valuation vs GL stock-account movement parity checks."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.tests.test_account_explorer_inventory_axes import (
	enable_inventory_analysis,
	require_inventory_company,
)


class TestAccountExplorerStockGlParity(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		enable_inventory_analysis()
		# Prefer a company that has real SLE↔Stock-GL vouchers (not SLE-only fixtures).
		candidates = []
		if frappe.db.exists("Company", "_Test Company"):
			candidates.append("_Test Company")
		candidates.extend(frappe.get_all("Company", pluck="name", limit=20))
		cls.company = None
		for company in candidates:
			if not frappe.get_cached_value("Company", company, "enable_perpetual_inventory"):
				continue
			has_pair = frappe.db.sql(
				"""
				select 1
				from `tabStock Ledger Entry` sle
				inner join `tabGL Entry` gle
					on gle.company = sle.company
					and gle.voucher_type = sle.voucher_type
					and gle.voucher_no = sle.voucher_no
					and gle.is_cancelled = 0
				inner join `tabAccount` acc
					on acc.name = gle.account and acc.account_type = 'Stock'
				where sle.company = %s and sle.is_cancelled = 0
				  and sle.voucher_no not like 'AE-INV-TEST-%%'
				  and sle.voucher_no not like 'AE-OP-%%'
				limit 1
				""",
				company,
			)
			if has_pair:
				cls.company = company
				break
		if not cls.company:
			# Fall back: ensure synthetic paired voucher on _Test Company
			cls.company = require_inventory_company(cls)
			if not frappe.get_cached_value("Company", cls.company, "enable_perpetual_inventory"):
				raise unittest.SkipTest("Perpetual inventory disabled — GL parity N/A")

	def test_sle_value_movement_reconciles_to_stock_gl(self):
		"""Σ SLE stock_value_difference ≈ Σ GL debit-credit on inventory accounts for same vouchers."""
		# Only vouchers that have Stock GL — excludes synthetic SLE-only fixtures
		# (AE-INV-TEST / AE-OP-*) that intentionally insert SLE without full perpetual GL.
		vouchers = frappe.db.sql(
			"""
			select distinct sle.voucher_type, sle.voucher_no
			from `tabStock Ledger Entry` sle
			inner join `tabGL Entry` gle
				on gle.company = sle.company
				and gle.voucher_type = sle.voucher_type
				and gle.voucher_no = sle.voucher_no
				and gle.is_cancelled = 0
			inner join `tabAccount` acc
				on acc.name = gle.account and acc.account_type = 'Stock'
			where sle.company = %s and sle.is_cancelled = 0
			  and sle.voucher_no not like 'AE-INV-TEST-%%'
			  and sle.voucher_no not like 'AE-OP-%%'
			order by sle.posting_datetime desc
			limit 20
			""",
			self.company,
			as_dict=True,
		)
		if not vouchers:
			raise unittest.SkipTest("No SLE+Stock-GL vouchers for company")

		sle_total = 0.0
		gl_total = 0.0
		for row in vouchers:
			sle_total += flt(
				frappe.db.sql(
					"""
					select sum(stock_value_difference)
					from `tabStock Ledger Entry`
					where company = %s and voucher_type = %s and voucher_no = %s and is_cancelled = 0
					""",
					(self.company, row.voucher_type, row.voucher_no),
				)[0][0]
			)
			# Inventory GL lines for the voucher (accounts with account_type Stock)
			gl_total += flt(
				frappe.db.sql(
					"""
					select sum(gle.debit - gle.credit)
					from `tabGL Entry` gle
					inner join `tabAccount` acc on acc.name = gle.account
					where gle.company = %s
					  and gle.voucher_type = %s
					  and gle.voucher_no = %s
					  and gle.is_cancelled = 0
					  and acc.account_type = 'Stock'
					""",
					(self.company, row.voucher_type, row.voucher_no),
				)[0][0]
			)

		# Material Transfer may net SLE to ~0 and GL to ~0; allow small rounding.
		self.assertAlmostEqual(sle_total, gl_total, places=2)
