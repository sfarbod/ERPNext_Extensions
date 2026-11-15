# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer 5.7.43 — terminal account carry-forward across L4/L5."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.api import get_account_summary
from erpnext_extensions.iran_accounting.account_explorer.query_fingerprint import query_hash
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	enable_wave2a_analysis,
)

COMPANY = "اسپاد فارمد دارو"
ACC_L3_TERMINAL = "111601 - موجودی مواد اولیه - E"
ACC_L4_TERMINAL = "11100102 - صندوق دفتر مرکزی ( مالی ) - E"
ACC_L3_WITH_L4 = "111001 - صندوق ریالی - E"
ACC_L3_WITH_L4_L5 = "111004 - موجودی بانکهای ریالی - E"
ACC_L4_WITH_L5 = "11100401 - بانک کارآفرین - E"
FROM_DATE = "2026-03-21"
TO_DATE = "2027-03-20"


def cint_safe(value) -> int:
	try:
		return int(value or 0)
	except (TypeError, ValueError):
		return 0


def _payload(
	level,
	*,
	selected=None,
	mode="account",
	hide_zero=1,
	dimensions=None,
	column_filters=None,
	party_type=None,
	party=None,
):
	accounting = {}
	if party_type:
		accounting["party_type"] = party_type
	if party:
		accounting["party"] = party
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
			"accounting": accounting,
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
				"mode": mode if selected else "tree",
				"selected_account": selected,
				"tree_root_account": selected,
				"virtual_row_key": None,
				"is_virtual_group": 0,
				"level_sequence": level,
			},
			"column_filters": column_filters or {},
			"pagination": {
				"page": 1,
				"page_size": 100,
				"sort_field": "display_code",
				"sort_order": "asc",
			},
		},
	}


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


def _require_terminal(account: str):
	if not frappe.db.exists("Account", account):
		raise unittest.SkipTest(f"{account} missing")
	children = frappe.db.count("Account", {"parent_account": account})
	if children:
		raise unittest.SkipTest(f"{account} has NestedSet children")


class TestTerminalCarryForwardHierarchyV5743(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db or not frappe.db.exists("Company", COMPANY):
			raise unittest.SkipTest("Espad development company required")
		frappe.set_user("Administrator")
		enable_wave2a_analysis()
		_require_terminal(ACC_L3_TERMINAL)

	def test_l3_terminal_appears_at_l4(self):
		result = get_account_summary(_payload(4, selected=ACC_L3_TERMINAL))
		rows = result.get("rows") or []
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row["selected_account"], ACC_L3_TERMINAL)
		self.assertEqual(row["display_code"], "111601")
		self.assertEqual(cint_safe(row.get("is_terminal_account")), 1)
		self.assertEqual(cint_safe(row.get("is_terminal_carried_forward")), 1)
		self.assertEqual(int(row.get("source_level_sequence") or 0), 3)
		self.assertEqual(int(row.get("level_sequence") or 0), 4)
		self.assertFalse((result.get("hierarchy_empty") or {}).get("reason"))

	def test_l3_terminal_appears_at_l5(self):
		result = get_account_summary(_payload(5, selected=ACC_L3_TERMINAL))
		rows = result.get("rows") or []
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row["selected_account"], ACC_L3_TERMINAL)
		self.assertEqual(row["display_code"], "111601")
		self.assertEqual(cint_safe(row.get("is_terminal_carried_forward")), 1)
		self.assertEqual(int(row.get("source_level_sequence") or 0), 3)
		self.assertEqual(int(row.get("level_sequence") or 0), 5)

	def test_l4_terminal_appears_at_l5(self):
		_require_terminal(ACC_L4_TERMINAL)
		result = get_account_summary(_payload(5, selected=ACC_L4_TERMINAL))
		rows = result.get("rows") or []
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row["selected_account"], ACC_L4_TERMINAL)
		self.assertEqual(row["display_code"], "11100102")
		self.assertEqual(cint_safe(row.get("is_terminal_account")), 1)
		self.assertEqual(cint_safe(row.get("is_terminal_carried_forward")), 1)
		self.assertEqual(int(row.get("source_level_sequence") or 0), 4)
		self.assertEqual(int(row.get("level_sequence") or 0), 5)
		# Master hierarchy unchanged — presentation only.
		self.assertEqual(
			row.get("parent_account"),
			frappe.db.get_value("Account", ACC_L4_TERMINAL, "parent_account"),
		)

	def test_real_descendants_replace_carry_forward(self):
		if not frappe.db.exists("Account", ACC_L3_WITH_L4):
			self.skipTest("111001 missing")
		# hide_zero=0 so zero-activity children still prove NestedSet children win
		# over parent carry-forward (distinct from descendants_no_activity empty).
		result = get_account_summary(_payload(4, selected=ACC_L3_WITH_L4, hide_zero=0))
		rows = result.get("rows") or []
		self.assertTrue(rows, "expected real L4 children")
		codes = {r.get("display_code") for r in rows}
		accounts = {r.get("selected_account") for r in rows}
		self.assertNotIn("111001", codes)
		self.assertNotIn(ACC_L3_WITH_L4, accounts)
		self.assertFalse(any(cint_safe(r.get("is_terminal_carried_forward")) for r in rows))
		self.assertFalse((result.get("hierarchy_empty") or {}).get("reason"))

	def test_no_duplicate_rows_on_carry_forward(self):
		for level in (3, 4, 5):
			result = get_account_summary(_payload(level, selected=ACC_L3_TERMINAL))
			rows = result.get("rows") or []
			self.assertEqual(len(rows), 1, level)
			keys = [r.get("row_key") for r in rows]
			self.assertEqual(len(keys), len(set(keys)), level)

	def test_no_artificial_account_codes(self):
		for level in (4, 5):
			row = get_account_summary(_payload(level, selected=ACC_L3_TERMINAL))["rows"][0]
			self.assertEqual(row["display_code"], "111601")
			self.assertEqual(row["selected_account"], ACC_L3_TERMINAL)
			self.assertTrue(str(row["row_key"]).endswith(ACC_L3_TERMINAL))

	def test_l4_with_l5_children_not_duplicated_as_parent(self):
		if not frappe.db.exists("Account", ACC_L4_WITH_L5):
			self.skipTest("L4 with L5 missing")
		result = get_account_summary(_payload(5, selected=ACC_L4_WITH_L5, hide_zero=0))
		rows = result.get("rows") or []
		self.assertTrue(rows, "expected real L5 descendants")
		self.assertNotIn(ACC_L4_WITH_L5, {r.get("selected_account") for r in rows})
		self.assertFalse(any(cint_safe(r.get("is_terminal_carried_forward")) for r in rows))


class TestTerminalCarryForwardAccountingV5743(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db or not frappe.db.exists("Company", COMPANY):
			raise unittest.SkipTest("Espad development company required")
		frappe.set_user("Administrator")
		enable_wave2a_analysis()
		_require_terminal(ACC_L3_TERMINAL)

	def test_own_gl_and_balances_match_sql_across_levels(self):
		indep = _independent_gl(ACC_L3_TERMINAL)
		for level in (3, 4, 5):
			row = get_account_summary(_payload(level, selected=ACC_L3_TERMINAL))["rows"][0]
			self.assertAlmostEqual(flt(row["opening_debit"]), flt(indep["opening_debit"]), places=2, msg=level)
			self.assertAlmostEqual(flt(row["opening_credit"]), flt(indep["opening_credit"]), places=2, msg=level)
			self.assertAlmostEqual(flt(row["period_debit"]), flt(indep["period_debit"]), places=2, msg=level)
			self.assertAlmostEqual(flt(row["period_credit"]), flt(indep["period_credit"]), places=2, msg=level)
			self.assertAlmostEqual(flt(row["debit_balance"]), flt(indep["debit_balance"]), places=2, msg=level)
			self.assertAlmostEqual(flt(row["credit_balance"]), flt(indep["credit_balance"]), places=2, msg=level)

	def test_no_double_counting_across_l3_l4_l5(self):
		"""Carried-forward figures equal the single account's GL — not multiplied by depth."""
		indep = _independent_gl(ACC_L3_TERMINAL)
		totals = []
		for level in (3, 4, 5):
			row = get_account_summary(_payload(level, selected=ACC_L3_TERMINAL))["rows"][0]
			totals.append(flt(row["period_debit"]))
			self.assertAlmostEqual(flt(row["period_debit"]), flt(indep["period_debit"]), places=2)
		self.assertEqual(len(set(round(t, 2) for t in totals)), 1)

	def test_l4_terminal_sql_match(self):
		_require_terminal(ACC_L4_TERMINAL)
		indep = _independent_gl(ACC_L4_TERMINAL)
		for level in (4, 5):
			row = get_account_summary(_payload(level, selected=ACC_L4_TERMINAL, hide_zero=0))["rows"][0]
			self.assertEqual(row["selected_account"], ACC_L4_TERMINAL)
			self.assertAlmostEqual(flt(row["period_debit"]), flt(indep["period_debit"]), places=2, msg=level)
			self.assertAlmostEqual(flt(row["period_credit"]), flt(indep["period_credit"]), places=2, msg=level)

	def test_zero_activity_terminal_still_visible_under_analyze(self):
		"""hide_zero_rows must not erase the analyzed terminal identity."""
		# Find a terminal L3 with no period/opening activity in FY1405 if possible.
		candidates = frappe.db.sql(
			"""
			select a.name
			from `tabAccount` a
			left join `tabGL Entry` gle
			  on gle.account=a.name and gle.company=a.company
			 and ifnull(gle.is_cancelled,0)=0
			where a.company=%s and a.is_group=0 and a.disabled=0
			  and length(replace(replace(ifnull(a.account_number,''),' ',''),'-',''))=6
			  and not exists (select 1 from `tabAccount` c where c.parent_account=a.name)
			group by a.name
			having ifnull(sum(gle.debit),0)=0 and ifnull(sum(gle.credit),0)=0
			order by a.account_number
			limit 1
			""",
			COMPANY,
		)
		if not candidates:
			self.skipTest("no zero-activity terminal L3 available")
		account = candidates[0][0]
		result = get_account_summary(_payload(5, selected=account, hide_zero=1))
		rows = result.get("rows") or []
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["selected_account"], account)
		self.assertEqual(cint_safe(rows[0].get("is_terminal_account")), 1)


class TestTerminalCarryForwardFiltersV5743(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db or not frappe.db.exists("Company", COMPANY):
			raise unittest.SkipTest("Espad development company required")
		frappe.set_user("Administrator")
		enable_wave2a_analysis()
		_require_terminal(ACC_L3_TERMINAL)

	def test_fingerprints_distinguish_levels_same_terminal(self):
		hashes = []
		for level in (3, 4, 5):
			spec = AccountExplorerQuerySpec_from_client(_payload(level, selected=ACC_L3_TERMINAL))
			hashes.append(query_hash(spec))
		self.assertEqual(len(set(hashes)), 3)

	def test_fingerprints_distinguish_terminal_vs_nonterminal_scope(self):
		if not frappe.db.exists("Account", ACC_L3_WITH_L4):
			self.skipTest("non-terminal parent missing")
		h_term = query_hash(
			AccountExplorerQuerySpec_from_client(_payload(4, selected=ACC_L3_TERMINAL))
		)
		h_group = query_hash(
			AccountExplorerQuerySpec_from_client(_payload(4, selected=ACC_L3_WITH_L4))
		)
		self.assertNotEqual(h_term, h_group)

	def test_column_and_dimension_filters_survive_payload_across_levels(self):
		cc = frappe.db.get_value(
			"Cost Center", {"company": COMPANY, "disabled": 0}, "name"
		)
		dimensions = {"cost_center": cc} if cc else {}
		column_filters = {"display_code": "111601"}
		for level in (3, 4, 5):
			raw = _payload(
				level,
				selected=ACC_L3_TERMINAL,
				dimensions=dimensions,
				column_filters=column_filters,
			)
			spec = AccountExplorerQuerySpec_from_client(raw)
			self.assertEqual(spec.account_scope.tree_root_account, ACC_L3_TERMINAL)
			self.assertEqual(int(spec.level_sequence), level)
			self.assertEqual(spec.column_filters.get("display_code"), "111601")
			if dimensions:
				self.assertEqual(
					(spec.document_scope.accounting_dimensions or {}).get("cost_center"),
					cc,
				)
			result = get_account_summary(raw)
			# Dimension may filter out GL activity; identity/carry-forward row must remain.
			self.assertEqual(len(result.get("rows") or []), 1, level)
			self.assertEqual(result["rows"][0]["selected_account"], ACC_L3_TERMINAL)

	def test_filter_summary_scope_fields_match_request(self):
		raw = _payload(5, selected=ACC_L3_TERMINAL, column_filters={"display_code": "111601"})
		spec = AccountExplorerQuerySpec_from_client(raw)
		self.assertEqual(spec.document_scope.company, COMPANY)
		self.assertEqual(str(spec.document_scope.from_date), FROM_DATE)
		self.assertEqual(str(spec.document_scope.to_date), TO_DATE)
		self.assertEqual(spec.account_scope.selected_account, ACC_L3_TERMINAL)
		self.assertEqual(int(spec.level_sequence), 5)
		result = get_account_summary(raw)
		self.assertEqual(result.get("level_sequence"), 5)
		self.assertEqual(result["rows"][0]["display_code"], "111601")


class TestTerminalCarryForwardSourceGuardsV5743(unittest.TestCase):
	def test_frontend_no_longer_short_circuits_terminal_levels(self):
		from pathlib import Path

		controller = (
			Path(frappe.get_app_path("erpnext_extensions"))
			/ "erpnext_extensions"
			/ "page"
			/ "account_explorer"
			/ "account_explorer.js"
		).read_text(encoding="utf-8")
		self.assertIn("v5.7.43: terminal accounts are carried forward", controller)
		# Dead helper must not invent empty Persian state for deeper levels.
		self.assertIn(
			"never short-circuit to an empty Persian state",
			controller,
		)
		self.assertNotIn(
			"paint Persian empty-state without another summary round-trip",
			controller,
		)

	def test_backend_carry_forward_docstring(self):
		from pathlib import Path

		qb = (
			Path(frappe.get_app_path("erpnext_extensions"))
			/ "iran_accounting"
			/ "account_explorer"
			/ "query_builder.py"
		).read_text(encoding="utf-8")
		self.assertIn("Terminal ledger carry-forward (v5.7.43)", qb)
		self.assertIn("is_terminal_carried_forward", qb)
