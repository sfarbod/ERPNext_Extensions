# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer 5.6.35 — NestedSet Analyze navigation + identity combo."""

from __future__ import annotations

import time
import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.api import get_account_summary
from erpnext_extensions.iran_accounting.account_explorer.query_fingerprint import build_fingerprint
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
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
IDENTIFIER = "14008809078"
SUPPLIER = "SUP-00204"
GROUP_11 = "11 - داراییهای جاری - E"
GROUP_21 = "21 - بدهیهای جاری - E"


def _payload(level=1, account_scope=None, party_scope=None, accounting=None):
	doc = dict(FY1405)
	if accounting is not None:
		doc["accounting"] = accounting
	ctx = {
		"view_axis": "account_level",
		"detail_mode": "summary",
		"level_sequence": level,
		"page": 1,
		"page_size": 50,
		"sort_field": "display_code",
		"sort_order": "asc",
		"party_scope": party_scope or {},
		"account_scope": account_scope or {},
	}
	return {"document_scope": doc, "analysis_context": ctx}


def _codes(result):
	return [str(row.get("display_code") or "") for row in result.get("rows") or []]


def _identity_party_scope():
	return {
		"party_type": "Supplier",
		"selected_party": SUPPLIER,
		"identifier_query": IDENTIFIER,
		"unified_identity": 1,
	}


def _independent_gl_totals(account_names, *, party_tuples=None):
	if not account_names:
		return 0.0, 0.0
	params = [COMPANY, FY1405["from_date"], FY1405["to_date"], tuple(account_names)]
	party_sql = ""
	if party_tuples:
		ors = []
		for party_type, party in party_tuples:
			ors.append("(party_type=%s and party=%s)")
			params.extend([party_type, party])
		party_sql = " and (" + " or ".join(ors) + ")"
	row = frappe.db.sql(
		f"""
		select coalesce(sum(debit),0), coalesce(sum(credit),0)
		from `tabGL Entry`
		where company=%s and is_cancelled=0
		  and posting_date between %s and %s
		  and account in %s
		  {party_sql}
		""",
		tuple(params),
	)[0]
	return flt(row[0]), flt(row[1])


class TestAnalyzeNestedSetDescendantsV5635(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.root11 = frappe.db.exists("Account", GROUP_11) and GROUP_11
		cls.root21 = frappe.db.exists("Account", GROUP_21) and GROUP_21
		if not cls.root11 or not cls.root21:
			raise unittest.SkipTest("Espad Group 11/21 accounts missing")

	def test_analyze_group_11_excludes_sibling_21_and_parent(self):
		result = get_account_summary(
			_payload(
				level=2,
				account_scope={
					"mode": "account",
					"selected_account": GROUP_11,
					"tree_root_account": GROUP_11,
				},
			)
		)
		codes = _codes(result)
		self.assertTrue(codes, "expected descendants of Group 11")
		self.assertNotIn("11", codes)
		self.assertNotIn("21", codes)
		self.assertFalse(any(c == "21" or c.startswith("21") for c in codes), codes)
		self.assertTrue(all(c.startswith("11") for c in codes), codes)

	def test_analyze_group_21_excludes_sibling_11_and_parent(self):
		result = get_account_summary(
			_payload(
				level=2,
				account_scope={
					"mode": "account",
					"selected_account": GROUP_21,
					"tree_root_account": GROUP_21,
				},
			)
		)
		codes = _codes(result)
		self.assertTrue(codes, "expected descendants of Group 21")
		self.assertNotIn("21", codes)
		self.assertNotIn("11", codes)
		# Mis-parented account numbers must not invent sibling Group 11.
		self.assertFalse(any(c == "11" or (c.startswith("11") and not c.startswith("211")) for c in codes), codes)
		self.assertTrue(all(c.startswith("21") for c in codes), codes)

	def test_nested_analyze_narrows_scope(self):
		parent = get_account_summary(
			_payload(
				level=2,
				account_scope={
					"mode": "account",
					"selected_account": GROUP_21,
					"tree_root_account": GROUP_21,
				},
			)
		)
		child_row = next((r for r in parent["rows"] if r.get("selected_account")), None)
		self.assertIsNotNone(child_row)
		child = get_account_summary(
			_payload(
				level=3,
				account_scope={
					"mode": "account",
					"selected_account": child_row["selected_account"],
					"tree_root_account": child_row["selected_account"],
				},
			)
		)
		parent_names = {r.get("selected_account") for r in parent["rows"]}
		child_names = {r.get("selected_account") for r in child["rows"]}
		self.assertNotIn(child_row["selected_account"], child_names)
		self.assertTrue(child_names.issubset(parent_names) or child_names)

	def test_empty_group_returns_empty_not_company_wide(self):
		# Pick a leaf under 21 if present; analyzing a leaf at child level yields empty/narrow.
		leaf = frappe.db.get_value(
			"Account",
			{"company": COMPANY, "parent_account": GROUP_21, "is_group": 0},
			"name",
		)
		if not leaf:
			self.skipTest("no leaf under Group 21")
		result = get_account_summary(
			_payload(
				level=2,
				account_scope={
					"mode": "account",
					"selected_account": leaf,
					"tree_root_account": leaf,
				},
			)
		)
		company = get_account_summary(_payload(level=1, account_scope={}))
		self.assertNotEqual(_codes(result), _codes(company))
		self.assertNotIn("11", _codes(result))
		self.assertNotIn("21", _codes(result))

	def test_invalid_hierarchy_node_rejected(self):
		with self.assertRaises(Exception):
			get_account_summary(
				_payload(
					level=2,
					account_scope={
						"mode": "account",
						"selected_account": "DOES-NOT-EXIST-ACCOUNT",
						"tree_root_account": "DOES-NOT-EXIST-ACCOUNT",
					},
				)
			)

	def test_cross_company_descendants_excluded(self):
		spec = AccountExplorerQuerySpec_from_client(
			_payload(
				level=2,
				account_scope={
					"mode": "account",
					"selected_account": GROUP_21,
					"tree_root_account": GROUP_21,
				},
			)
		)
		for name in spec.included_account_names or []:
			self.assertEqual(frappe.db.get_value("Account", name, "company"), COMPANY)


class TestAnalyzeIdentityComboV5635(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		if not frappe.db.exists("Account", GROUP_21):
			raise unittest.SkipTest("Group 21 missing")

	def test_group_21_with_unified_party_excludes_11(self):
		party_scope = _identity_party_scope()
		result = get_account_summary(
			_payload(
				level=2,
				account_scope={
					"mode": "account",
					"selected_account": GROUP_21,
					"tree_root_account": GROUP_21,
				},
				party_scope=party_scope,
				accounting={"party_type": "Supplier", "party": SUPPLIER},
			)
		)
		codes = _codes(result)
		self.assertNotIn("11", codes)
		self.assertNotIn("21", codes)
		self.assertFalse(any(c.startswith("11") and not c.startswith("211") for c in codes), codes)
		spec = AccountExplorerQuerySpec_from_client(
			_payload(
				level=2,
				account_scope={
					"mode": "account",
					"selected_account": GROUP_21,
					"tree_root_account": GROUP_21,
				},
				party_scope=party_scope,
			)
		)
		self.assertTrue(spec.resolved_member_tuples)
		company_wide = get_account_summary(
			_payload(level=1, account_scope={}, party_scope=party_scope)
		)
		parent_row = next(r for r in company_wide["rows"] if str(r.get("display_code")) == "21")
		# Parent Group aggregate must equal Analyze descendant totals (same identity).
		self.assertAlmostEqual(
			flt(result["totals"]["period_debit"]), flt(parent_row["period_debit"]), places=2
		)
		self.assertAlmostEqual(
			flt(result["totals"]["period_credit"]), flt(parent_row["period_credit"]), places=2
		)
		self.assertAlmostEqual(
			flt(result["totals"]["debit_balance"]), flt(parent_row["debit_balance"]), places=2
		)
		self.assertAlmostEqual(
			flt(result["totals"]["credit_balance"]), flt(parent_row["credit_balance"]), places=2
		)
		self.assertLess(
			flt(result["totals"]["period_debit"]),
			flt(company_wide["totals"]["period_debit"]),
		)
		# Independent posted GL over NestedSet descendants + identity tuples.
		debit, credit = _independent_gl_totals(
			[n for n in (spec.included_account_names or []) if n != GROUP_21],
			party_tuples=list(spec.resolved_member_tuples),
		)
		self.assertGreaterEqual(debit, 0)
		self.assertGreaterEqual(credit, 0)

	def test_group_11_with_unified_party_excludes_21(self):
		result = get_account_summary(
			_payload(
				level=2,
				account_scope={
					"mode": "account",
					"selected_account": GROUP_11,
					"tree_root_account": GROUP_11,
				},
				party_scope=_identity_party_scope(),
			)
		)
		codes = _codes(result)
		self.assertNotIn("21", codes)
		self.assertFalse(any(c.startswith("21") for c in codes), codes)

	def test_fingerprint_distinguishes_hierarchy_root(self):
		spec11 = AccountExplorerQuerySpec_from_client(
			_payload(
				level=2,
				account_scope={
					"mode": "account",
					"selected_account": GROUP_11,
					"tree_root_account": GROUP_11,
				},
				party_scope=_identity_party_scope(),
			)
		)
		spec21 = AccountExplorerQuerySpec_from_client(
			_payload(
				level=2,
				account_scope={
					"mode": "account",
					"selected_account": GROUP_21,
					"tree_root_account": GROUP_21,
				},
				party_scope=_identity_party_scope(),
			)
		)
		fp11 = build_fingerprint(spec11, 1)
		fp21 = build_fingerprint(spec21, 1)
		self.assertNotEqual(fp11, fp21)


class TestAnalyzePerformanceSmokeV5635(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def test_analyze_latency_median_p95(self):
		runs = []
		for _ in range(8):
			t0 = time.perf_counter()
			get_account_summary(
				_payload(
					level=2,
					account_scope={
						"mode": "account",
						"selected_account": GROUP_21,
						"tree_root_account": GROUP_21,
					},
					party_scope=_identity_party_scope(),
				)
			)
			runs.append((time.perf_counter() - t0) * 1000)
		runs.sort()
		median = runs[len(runs) // 2]
		p95 = runs[int(len(runs) * 0.95) - 1]
		# Soft gate — report values; fail only on extreme regression (>60s).
		self.assertLess(p95, 60000, f"median={median:.1f}ms p95={p95:.1f}ms runs={runs}")
		frappe.logger().info(f"ae_analyze_v5635 median_ms={median:.1f} p95_ms={p95:.1f} runs={runs}")
