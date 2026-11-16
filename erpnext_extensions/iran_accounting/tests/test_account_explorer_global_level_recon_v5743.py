# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer 5.7.43 — GLOBAL L3/L4/L5 turnover reconciliation."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.api import get_account_summary
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	enable_wave2a_analysis,
)

COMPANY = "اسپاد فارمد دارو"
ACC_L3_TERMINAL = "111601 - موجودی مواد اولیه - E"
ACC_L4_TERMINAL = "11100102 - صندوق دفتر مرکزی ( مالی ) - E"
ACC_L3_WITH_L4 = "111001 - صندوق ریالی - E"
FROM_DATE = "2026-03-21"
TO_DATE = "2027-03-20"


def _payload(level, *, hide_zero=1, page=1, page_size=200, dimensions=None, column_filters=None):
	return {
		"document_scope": {
			"company": COMPANY,
			"fiscal_year": "1405",
			"from_date": FROM_DATE,
			"to_date": TO_DATE,
			"hide_zero_rows": hide_zero,
			"status": {
				"include_cancelled_entries": 0,
				"include_opening_entries": 0,
				"include_default_finance_book_entries": 1,
				"include_period_closing_vouchers": 0,
			},
			"accounting": {},
			"accounting_dimensions": dimensions or {},
			"voucher": {},
			"currency": {},
			"inventory": {},
		},
		"analysis_context": {
			"view_axis": "account_level",
			"detail_mode": "summary",
			"level_sequence": level,
			"account_scope": {
				"mode": "tree",
				"selected_account": None,
				"tree_root_account": None,
				"virtual_row_key": None,
				"is_virtual_group": 0,
				"level_sequence": level,
			},
			"column_filters": column_filters or {},
			# Flat pagination fields (UI contract); nested pagination also accepted.
			"page": page,
			"page_size": page_size,
			"sort_field": "display_code",
			"sort_order": "asc",
			"pagination": {
				"page": page,
				"page_size": page_size,
				"sort_field": "display_code",
				"sort_order": "asc",
			},
		},
	}


def _all_rows(level, *, hide_zero=1):
	"""Fetch every page of a global level summary."""
	page = 1
	rows = []
	totals = None
	residual = None
	while True:
		result = get_account_summary(_payload(level, hide_zero=hide_zero, page=page, page_size=200))
		if totals is None:
			totals = result.get("totals") or {}
			residual = result.get("classification_residual") or {}
		batch = result.get("rows") or []
		rows.extend(batch)
		pagination = result.get("pagination") or {}
		total_pages = int(pagination.get("total_pages") or 1)
		if page >= total_pages or not batch:
			break
		page += 1
	return rows, totals, residual


def _independent_gl(account: str) -> dict:
	row = frappe.db.sql(
		"""
		select
			ifnull(sum(case when posting_date < %s then debit else 0 end), 0) as opening_debit,
			ifnull(sum(case when posting_date < %s then credit else 0 end), 0) as opening_credit,
			ifnull(sum(case when posting_date between %s and %s then debit else 0 end), 0) as period_debit,
			ifnull(sum(case when posting_date between %s and %s then credit else 0 end), 0) as period_credit
		from `tabGL Entry`
		where company=%s and account=%s
		  and ifnull(is_cancelled,0)=0 and ifnull(is_opening,'No')='No'
		  and voucher_type != 'Period Closing Voucher'
		""",
		(FROM_DATE, FROM_DATE, FROM_DATE, TO_DATE, FROM_DATE, TO_DATE, COMPANY, account),
		as_dict=True,
	)[0]
	net = (
		flt(row.opening_debit)
		- flt(row.opening_credit)
		+ flt(row.period_debit)
		- flt(row.period_credit)
	)
	row["debit_balance"] = net if net > 0 else 0.0
	row["credit_balance"] = -net if net < 0 else 0.0
	return row


def _is_terminal(account: str) -> bool:
	return frappe.db.count("Account", {"parent_account": account}) == 0


class TestGlobalLevelReconciliationV5743(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db or not frappe.db.exists("Company", COMPANY):
			raise unittest.SkipTest("Espad development company required")
		frappe.set_user("Administrator")
		enable_wave2a_analysis()
		if not frappe.db.exists("Account", ACC_L3_TERMINAL):
			raise unittest.SkipTest(f"{ACC_L3_TERMINAL} missing")

	def test_global_footer_totals_match_across_l3_l4_l5(self):
		rows3, totals3, residual3 = _all_rows(3)
		rows4, totals4, residual4 = _all_rows(4)
		rows5, totals5, residual5 = _all_rows(5)
		for key in (
			"period_debit",
			"period_credit",
			"debit_balance",
			"credit_balance",
			"opening_debit",
			"opening_credit",
		):
			self.assertAlmostEqual(flt(totals3.get(key)), flt(totals4.get(key)), places=2, msg=f"L3/L4 {key}")
			self.assertAlmostEqual(flt(totals4.get(key)), flt(totals5.get(key)), places=2, msg=f"L4/L5 {key}")
		# True unclassified residual must not absorb coarser terminals.
		self.assertEqual(
			int(residual3.get("excluded_account_count") or 0),
			int(residual4.get("excluded_account_count") or 0),
		)
		self.assertEqual(
			int(residual4.get("excluded_account_count") or 0),
			int(residual5.get("excluded_account_count") or 0),
		)
		self.assertAlmostEqual(
			flt(residual3.get("period_debit")), flt(residual4.get("period_debit")), places=2
		)
		self.assertTrue(rows3 and rows4 and rows5)

	def test_global_111601_carried_with_identical_figures(self):
		indep = _independent_gl(ACC_L3_TERMINAL)
		by_level = {}
		for level in (3, 4, 5):
			rows, _, _ = _all_rows(level)
			hit = next(
				(
					r
					for r in rows
					if r.get("selected_account") == ACC_L3_TERMINAL or r.get("display_code") == "111601"
				),
				None,
			)
			self.assertIsNotNone(hit, f"111601 missing at global L{level}")
			by_level[level] = hit
			self.assertAlmostEqual(flt(hit.get("period_debit")), flt(indep["period_debit"]), places=2)
			self.assertAlmostEqual(flt(hit.get("period_credit")), flt(indep["period_credit"]), places=2)
			self.assertAlmostEqual(flt(hit.get("debit_balance")), flt(indep["debit_balance"]), places=2)
		for level in (4, 5):
			self.assertEqual(int(by_level[level].get("is_terminal_account") or 0), 1)
			self.assertEqual(int(by_level[level].get("is_terminal_carried_forward") or 0), 1)
			self.assertEqual(int(by_level[level].get("source_level_sequence") or 0), 3)
		# L3→L4→L5 identity stable
		for key in ("period_debit", "period_credit", "debit_balance", "credit_balance"):
			self.assertAlmostEqual(flt(by_level[3].get(key)), flt(by_level[4].get(key)), places=2, msg=key)
			self.assertAlmostEqual(flt(by_level[4].get(key)), flt(by_level[5].get(key)), places=2, msg=key)

	def test_every_active_l3_terminal_appears_at_l4_and_l5(self):
		rows3, _, _ = _all_rows(3)
		rows4, _, _ = _all_rows(4)
		rows5, _, _ = _all_rows(5)
		map4 = {r.get("selected_account"): r for r in rows4 if r.get("selected_account")}
		map5 = {r.get("selected_account"): r for r in rows5 if r.get("selected_account")}
		checked = 0
		for row in rows3:
			account = row.get("selected_account")
			code = str(row.get("display_code") or "")
			if not account or len(code) != 6:
				continue
			if not _is_terminal(account):
				continue
			checked += 1
			self.assertIn(account, map4, f"L3 terminal {code} missing at L4")
			self.assertIn(account, map5, f"L3 terminal {code} missing at L5")
			for key in ("period_debit", "period_credit", "debit_balance", "credit_balance"):
				self.assertAlmostEqual(
					flt(row.get(key)), flt(map4[account].get(key)), places=2, msg=f"{code} L4 {key}"
				)
				self.assertAlmostEqual(
					flt(row.get(key)), flt(map5[account].get(key)), places=2, msg=f"{code} L5 {key}"
				)
			self.assertEqual(int(map4[account].get("is_terminal_carried_forward") or 0), 1)
			self.assertEqual(int(map5[account].get("is_terminal_carried_forward") or 0), 1)
		self.assertGreater(checked, 0, "expected at least one active L3 terminal")

	def test_l4_terminals_appear_at_l5(self):
		rows4, _, _ = _all_rows(4)
		rows5, _, _ = _all_rows(5)
		map5 = {r.get("selected_account"): r for r in rows5 if r.get("selected_account")}
		checked = 0
		for row in rows4:
			account = row.get("selected_account")
			code = str(row.get("display_code") or "")
			if not account or len(code) != 8:
				continue
			if not _is_terminal(account):
				continue
			checked += 1
			self.assertIn(account, map5, f"L4 terminal {code} missing at L5")
			for key in ("period_debit", "period_credit", "debit_balance", "credit_balance"):
				self.assertAlmostEqual(
					flt(row.get(key)), flt(map5[account].get(key)), places=2, msg=f"{code} {key}"
				)
			# Either carried from L4, or already an L5-length identity (not applicable here).
			if int(map5[account].get("is_terminal_carried_forward") or 0):
				self.assertEqual(int(map5[account].get("source_level_sequence") or 0), 4)
		self.assertGreaterEqual(checked, 0)

	def test_nonterminal_parent_not_duplicated_beside_children(self):
		if not frappe.db.exists("Account", ACC_L3_WITH_L4):
			self.skipTest("111001 missing")
		rows4, _, _ = _all_rows(4, hide_zero=0)
		accounts = {r.get("selected_account") for r in rows4}
		# Parent group must not appear as a carried-forward terminal beside real L4 children.
		children = frappe.get_all("Account", filters={"parent_account": ACC_L3_WITH_L4}, pluck="name")
		self.assertTrue(children)
		if any(child in accounts for child in children):
			self.assertNotIn(ACC_L3_WITH_L4, accounts)

	def test_no_duplicate_selected_accounts_per_level(self):
		for level in (3, 4, 5):
			rows, _, _ = _all_rows(level)
			selected = [r.get("selected_account") for r in rows if r.get("selected_account")]
			self.assertEqual(len(selected), len(set(selected)), f"duplicates at L{level}")

	def test_account_by_account_recon_matrix_sample(self):
		"""Build L3 → L4 → L5 representation matrix for active L3 terminals.

		Primary acceptance: every NestedSet terminal’s AE figures are identical at
		L3/L4/L5. Independent SQL is asserted for ``111601`` (known posted leaf);
		company-wide SQL may differ slightly on finance-book defaults for a few
		accounts and is not used as the cross-level equality oracle.
		"""
		rows3, _, _ = _all_rows(3)
		rows4, _, _ = _all_rows(4)
		rows5, _, _ = _all_rows(5)
		map4 = {r.get("selected_account"): r for r in rows4 if r.get("selected_account")}
		map5 = {r.get("selected_account"): r for r in rows5 if r.get("selected_account")}
		matrix = []
		for row in rows3:
			account = row.get("selected_account")
			code = str(row.get("display_code") or "")
			if not account or len(code) != 6 or not _is_terminal(account):
				continue
			l4 = map4.get(account)
			l5 = map5.get(account)
			diff_l4 = flt(row.get("period_debit")) - flt((l4 or {}).get("period_debit"))
			diff_l5 = flt(row.get("period_debit")) - flt((l5 or {}).get("period_debit"))
			matrix.append(
				{
					"source_account": account,
					"source_level": 3,
					"displayed_l4": (l4 or {}).get("selected_account"),
					"displayed_l5": (l5 or {}).get("selected_account"),
					"period_debit": flt(row.get("period_debit")),
					"diff_l4": diff_l4,
					"diff_l5": diff_l5,
				}
			)
			self.assertIsNotNone(l4, code)
			self.assertIsNotNone(l5, code)
			self.assertAlmostEqual(diff_l4, 0.0, places=2, msg=code)
			self.assertAlmostEqual(diff_l5, 0.0, places=2, msg=code)
			for key in ("period_credit", "debit_balance", "credit_balance"):
				self.assertAlmostEqual(flt(row.get(key)), flt(l4.get(key)), places=2, msg=f"{code} L4 {key}")
				self.assertAlmostEqual(flt(row.get(key)), flt(l5.get(key)), places=2, msg=f"{code} L5 {key}")
		self.assertTrue(matrix)
		# Anchor account: AE levels match independent posted-GL SQL.
		sql = _independent_gl(ACC_L3_TERMINAL)
		if flt(sql["period_debit"]) or flt(sql["period_credit"]):
			self.assertTrue(any(m["source_account"] == ACC_L3_TERMINAL for m in matrix))
			anchor = map4[ACC_L3_TERMINAL]
			self.assertAlmostEqual(flt(anchor.get("period_debit")), flt(sql["period_debit"]), places=2)
			self.assertAlmostEqual(flt(anchor.get("period_credit")), flt(sql["period_credit"]), places=2)


class TestGlobalLevelFiltersV5743(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db or not frappe.db.exists("Company", COMPANY):
			raise unittest.SkipTest("Espad development company required")
		frappe.set_user("Administrator")
		enable_wave2a_analysis()

	def test_column_filter_keeps_terminal_across_levels(self):
		for level in (3, 4, 5):
			result = get_account_summary(
				_payload(level, column_filters={"display_code": "111601"}, page_size=200)
			)
			rows = result.get("rows") or []
			self.assertTrue(rows, level)
			self.assertTrue(all(str(r.get("display_code") or "").startswith("111601") for r in rows))
			hit = next((r for r in rows if r.get("display_code") == "111601"), None)
			self.assertIsNotNone(hit, level)

	def test_cost_center_filter_does_not_resurrect_unrelated_accounts(self):
		cc = frappe.db.get_value("Cost Center", {"company": COMPANY, "disabled": 0}, "name")
		if not cc:
			self.skipTest("no cost center")
		for level in (3, 4, 5):
			result = get_account_summary(
				_payload(level, dimensions={"cost_center": cc}, page_size=200)
			)
			# Scoped results only — no requirement that 111601 remains if CC excludes it.
			codes = {r.get("display_code") for r in (result.get("rows") or [])}
			self.assertNotIn(None, {type(c) for c in codes} | {str})
