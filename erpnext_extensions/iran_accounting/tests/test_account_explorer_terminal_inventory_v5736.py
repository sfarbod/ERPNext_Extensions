# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account Explorer 5.7.36 — terminal ledgers + row-level inventory attribution."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.account_explorer.api import (
	get_account_summary,
	get_item_summary,
)
from erpnext_extensions.iran_accounting.account_explorer.inventory_scope import (
	warehouses_for_account_cross_filter,
	warehouses_for_inventory_accounts,
)
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_fixtures import (
	enable_wave2a_analysis,
)
from erpnext_extensions.iran_accounting.tests.test_account_explorer_inventory_axes import (
	_ensure_item_group,
	_ensure_stock_item,
	_insert_related_gl,
	_insert_sle,
	enable_inventory_analysis,
	require_inventory_company,
)

COMPANY = "اسپاد فارمد دارو"
ACC_111601 = "111601 - موجودی مواد اولیه - E"
ACC_111604 = "111604 - موجودی مواد و ملزومات مصرفی - E"
STE = "MAT-STE-2026-37804-6"
FROM_DATE = "2026-03-21"
TO_DATE = "2027-03-20"


def _account_payload(level, *, selected=None, mode="tree", hide_zero=0):
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
			"accounting_dimensions": {},
			"voucher": {},
			"currency": {},
			"inventory": {},
		},
		"analysis_context": {
			"view_axis": "account_level",
			"detail_mode": "summary",
			"level_sequence": level,
			"account_scope": {
				"mode": mode,
				"selected_account": selected,
				"tree_root_account": selected,
				"virtual_row_key": None,
				"is_virtual_group": 0,
				"level_sequence": level,
			},
			"pagination": {
				"page": 1,
				"page_size": 100,
				"sort_field": "display_code",
				"sort_order": "asc",
			},
		},
	}


def _item_payload(*, account=None, inventory=None, voucher_no=None):
	return {
		"document_scope": {
			"company": COMPANY,
			"fiscal_year": "1405",
			"from_date": FROM_DATE,
			"to_date": TO_DATE,
			"hide_zero_rows": 0,
			"status": {
				"include_cancelled_entries": 0,
				"include_opening_entries": 0,
				"include_default_finance_book_entries": 1,
				"include_period_closing_vouchers": 0,
			},
			"accounting": {},
			"accounting_dimensions": {},
			"voucher": {"voucher_no": voucher_no} if voucher_no else {},
			"currency": {},
			"inventory": inventory or {},
		},
		"analysis_context": {
			"view_axis": "item",
			"detail_mode": "summary",
			"level_sequence": 3,
			"account_scope": {
				"mode": "account" if account else "tree",
				"selected_account": account,
				"tree_root_account": account,
				"virtual_row_key": None,
				"is_virtual_group": 0,
				"level_sequence": 3,
			},
			"voucher_scope": {"voucher_no": voucher_no} if voucher_no else {},
			"pagination": {
				"page": 1,
				"page_size": 200,
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
	net = flt(row.opening_debit) - flt(row.opening_credit) + flt(row.period_debit) - flt(row.period_credit)
	row["closing_debit"] = net if net > 0 else 0.0
	row["closing_credit"] = -net if net < 0 else 0.0
	return row


def _ste_rows_by_account(account: str) -> list[dict]:
	return frappe.db.sql(
		"""
		select sle.item_code, sle.warehouse, wh.account, sle.voucher_detail_no,
		       sle.actual_qty, sle.stock_value_difference
		from `tabStock Ledger Entry` sle
		inner join `tabWarehouse` wh on wh.name = sle.warehouse
		where sle.voucher_type='Stock Entry' and sle.voucher_no=%s
		  and ifnull(sle.is_cancelled,0)=0 and wh.account=%s
		order by sle.item_code
		""",
		(STE, account),
		as_dict=True,
	)


class TestTerminalSubsidiaryLedgerV5736(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db or not frappe.db.exists("Company", COMPANY):
			raise unittest.SkipTest("Espad development company required")
		frappe.set_user("Administrator")
		enable_wave2a_analysis()
		if not frappe.db.exists("Account", ACC_111601):
			raise unittest.SkipTest(f"{ACC_111601} missing")
		is_group = cint_safe(frappe.db.get_value("Account", ACC_111601, "is_group"))
		children = frappe.db.count("Account", {"parent_account": ACC_111601})
		if is_group or children:
			raise unittest.SkipTest("111601 is not a terminal Subsidiary Ledger leaf")

	def test_analyze_terminal_shows_own_turnover(self):
		indep = _independent_gl(ACC_111601)
		result = get_account_summary(
			_account_payload(3, selected=ACC_111601, mode="account")
		)
		self.assertEqual(len(result.get("rows") or []), 1)
		row = result["rows"][0]
		self.assertEqual(row.get("selected_account"), ACC_111601)
		self.assertEqual(cint_safe(row.get("is_terminal_account")), 1)
		self.assertAlmostEqual(flt(row.get("period_debit")), flt(indep["period_debit"]), places=2)
		self.assertAlmostEqual(flt(row.get("period_credit")), flt(indep["period_credit"]), places=2)
		self.assertFalse(result.get("warnings"))

	def test_l4_l5_under_terminal_no_false_residual(self):
		for level in (4, 5):
			result = get_account_summary(
				_account_payload(level, selected=ACC_111601, mode="account")
			)
			residual = result.get("classification_residual") or {}
			self.assertEqual(int(residual.get("excluded_account_count") or 0), 0, level)
			self.assertAlmostEqual(flt(residual.get("period_debit")), 0.0, places=2)
			self.assertTrue(result.get("rows"), f"terminal row expected at L{level}")
			self.assertEqual(result["rows"][0].get("selected_account"), ACC_111601)
			warn = " ".join(result.get("warnings") or [])
			self.assertNotIn("سلسله", warn)

	def test_company_l3_matches_independent_sql(self):
		indep = _independent_gl(ACC_111601)
		result = get_account_summary(_account_payload(3))
		row = next(
			(
				r
				for r in (result.get("rows") or [])
				if r.get("selected_account") == ACC_111601 or r.get("display_code") == "111601"
			),
			None,
		)
		self.assertIsNotNone(row)
		self.assertAlmostEqual(flt(row.get("period_debit")), flt(indep["period_debit"]), places=2)
		self.assertAlmostEqual(flt(row.get("period_credit")), flt(indep["period_credit"]), places=2)

	def test_switch_terminal_to_group_and_back(self):
		leaf = get_account_summary(
			_account_payload(3, selected=ACC_111601, mode="account")
		)
		parent = frappe.db.get_value("Account", ACC_111601, "parent_account")
		self.assertTrue(parent)
		group = get_account_summary(
			_account_payload(3, selected=parent, mode="account")
		)
		self.assertTrue(group.get("rows"))
		self.assertNotEqual(
			{r.get("selected_account") for r in leaf["rows"]},
			{r.get("selected_account") for r in group["rows"]},
		)
		back = get_account_summary(
			_account_payload(3, selected=ACC_111601, mode="account")
		)
		self.assertEqual(len(back.get("rows") or []), 1)
		self.assertEqual(back["rows"][0].get("selected_account"), ACC_111601)


class TestRowLevelInventoryAttributionV5736(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if not frappe.db or not frappe.db.exists("Stock Entry", STE):
			raise unittest.SkipTest(f"{STE} required on development")
		frappe.set_user("Administrator")
		enable_wave2a_analysis()
		enable_inventory_analysis()

	def test_mat_ste_mapping_two_accounts(self):
		rows_a = _ste_rows_by_account(ACC_111601)
		rows_b = _ste_rows_by_account(ACC_111604)
		self.assertTrue(rows_a, "expected 111601 SLE rows on STE")
		self.assertTrue(rows_b, "expected 111604 SLE rows on STE")
		codes_a = {r.item_code for r in rows_a}
		codes_b = {r.item_code for r in rows_b}
		self.assertFalse(codes_a & codes_b)

	def test_account_filter_111601_excludes_other_ste_rows(self):
		expected = {r.item_code for r in _ste_rows_by_account(ACC_111601)}
		other = {r.item_code for r in _ste_rows_by_account(ACC_111604)}
		result = get_item_summary(_item_payload(account=ACC_111601, voucher_no=STE))
		codes = {r.get("item_code") for r in (result.get("rows") or [])}
		self.assertEqual(codes, expected)
		self.assertFalse(codes & other)

	def test_account_filter_111604_excludes_raw_material_rows(self):
		expected = {r.item_code for r in _ste_rows_by_account(ACC_111604)}
		other = {r.item_code for r in _ste_rows_by_account(ACC_111601)}
		result = get_item_summary(_item_payload(account=ACC_111604, voucher_no=STE))
		codes = {r.get("item_code") for r in (result.get("rows") or [])}
		self.assertEqual(codes, expected)
		self.assertFalse(codes & other)

	def test_inventory_account_document_filter_row_level(self):
		expected = {r.item_code for r in _ste_rows_by_account(ACC_111601)}
		other = {r.item_code for r in _ste_rows_by_account(ACC_111604)}
		result = get_item_summary(
			_item_payload(inventory={"inventory_account": ACC_111601}, voucher_no=STE)
		)
		codes = {r.get("item_code") for r in (result.get("rows") or [])}
		self.assertEqual(codes, expected)
		self.assertFalse(codes & other)

	def test_warehouse_map_for_accounts(self):
		wh_a = warehouses_for_inventory_accounts(COMPANY, [ACC_111601])
		wh_b = warehouses_for_inventory_accounts(COMPANY, [ACC_111604])
		self.assertTrue(wh_a)
		self.assertTrue(wh_b)
		self.assertFalse(set(wh_a) & set(wh_b))
		spec = AccountExplorerQuerySpec_from_client(
			_item_payload(account=ACC_111601, voucher_no=STE)
		)
		mapped = warehouses_for_account_cross_filter(spec)
		self.assertEqual(set(mapped or []), set(wh_a))


class TestControlledTwoWarehouseLeakageV5736(unittest.TestCase):
	"""Isolated fixture: one voucher, two warehouses/accounts, no cross-row leakage."""

	@classmethod
	def setUpClass(cls):
		cls.company = require_inventory_company(cls)
		enable_inventory_analysis()
		enable_wave2a_analysis()
		fy = frappe.db.get_value(
			"Fiscal Year",
			{"year_start_date": ["<=", frappe.utils.today()], "year_end_date": [">=", frappe.utils.today()]},
			["name", "year_start_date", "year_end_date"],
			as_dict=True,
		)
		if not fy:
			raise unittest.SkipTest("No fiscal year")
		cls.fiscal_year = fy.name
		cls.from_date = str(fy.year_start_date)
		cls.to_date = str(fy.year_end_date)

		all_wh = frappe.db.sql(
			"""
			select name, account
			from `tabWarehouse`
			where company=%s and is_group=0 and ifnull(account,'')!=''
			order by name
			""",
			cls.company,
			as_dict=True,
		)
		picked = []
		seen_accounts = set()
		for row in all_wh:
			if row.account in seen_accounts:
				continue
			picked.append(row)
			seen_accounts.add(row.account)
			if len(picked) == 2:
				break
		if len(picked) < 2:
			raise unittest.SkipTest("Need two warehouses with distinct stock accounts")
		cls.wh_a, cls.wh_b = picked[0], picked[1]
		cls.acc_a, cls.acc_b = cls.wh_a.account, cls.wh_b.account

		cls.group = _ensure_item_group("AE V5736 Leaf", "All Item Groups", is_group=0)
		cls.item_a = _ensure_stock_item("AE-V5736-A", cls.group, cls.company, cls.wh_a.name)
		cls.item_b = _ensure_stock_item("AE-V5736-B", cls.group, cls.company, cls.wh_b.name)
		cls.mid = str(frappe.utils.add_days(fy.year_start_date, 20))
		cls.voucher = "AE-INV-TEST-V5736-MULTI"

		# Clean prior fixture SLE/GL for this voucher
		frappe.db.sql(
			"delete from `tabStock Ledger Entry` where voucher_no=%s and company=%s",
			(cls.voucher, cls.company),
		)
		frappe.db.sql(
			"delete from `tabGL Entry` where voucher_no=%s and company=%s",
			(cls.voucher, cls.company),
		)
		frappe.db.commit()

		_insert_sle(
			company=cls.company,
			item_code=cls.item_a,
			warehouse=cls.wh_a.name,
			posting_date=cls.mid,
			actual_qty=-2,
			stock_value_difference=-200,
			voucher_suffix="V5736-MULTI",
		)
		# Force same voucher_no for both rows
		frappe.db.sql(
			"""
			update `tabStock Ledger Entry`
			set voucher_no=%s, voucher_type='Stock Entry',
			    voucher_detail_no=%s
			where company=%s and item_code=%s and warehouse=%s
			  and voucher_no like 'AE-INV-TEST-V5736-MULTI%%'
			""",
			(cls.voucher, "vdn-a", cls.company, cls.item_a, cls.wh_a.name),
		)
		_insert_sle(
			company=cls.company,
			item_code=cls.item_b,
			warehouse=cls.wh_b.name,
			posting_date=cls.mid,
			actual_qty=-3,
			stock_value_difference=-300,
			voucher_suffix="V5736-MULTI-B",
		)
		frappe.db.sql(
			"""
			update `tabStock Ledger Entry`
			set voucher_no=%s, voucher_type='Stock Entry',
			    voucher_detail_no=%s
			where company=%s and item_code=%s and warehouse=%s
			  and voucher_no like 'AE-INV-TEST-V5736-MULTI%%'
			""",
			(cls.voucher, "vdn-b", cls.company, cls.item_b, cls.wh_b.name),
		)
		# EXISTS bridge only needs GLE on the voucher+account; side is irrelevant here.
		_insert_related_gl(
			company=cls.company,
			account=cls.acc_a,
			posting_date=cls.mid,
			debit=200,
			voucher_no=cls.voucher,
		)
		_insert_related_gl(
			company=cls.company,
			account=cls.acc_b,
			posting_date=cls.mid,
			debit=300,
			voucher_no=cls.voucher,
		)
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		if not getattr(cls, "voucher", None):
			return
		frappe.db.sql(
			"delete from `tabStock Ledger Entry` where voucher_no=%s and company=%s",
			(cls.voucher, cls.company),
		)
		frappe.db.sql(
			"delete from `tabGL Entry` where voucher_no=%s and company=%s",
			(cls.voucher, cls.company),
		)
		frappe.db.commit()

	def _payload(self, account):
		return {
			"document_scope": {
				"company": self.company,
				"fiscal_year": self.fiscal_year,
				"from_date": self.from_date,
				"to_date": self.to_date,
				"hide_zero_rows": 0,
				"status": {
					"include_cancelled_entries": 0,
					"include_opening_entries": 0,
					"include_default_finance_book_entries": 1,
					"include_period_closing_vouchers": 0,
				},
				"accounting": {},
				"accounting_dimensions": {},
				"voucher": {"voucher_no": self.voucher},
				"currency": {},
				"inventory": {},
			},
			"analysis_context": {
				"view_axis": "item",
				"detail_mode": "summary",
				"level_sequence": 3,
				"account_scope": {
					"mode": "account",
					"selected_account": account,
					"tree_root_account": account,
					"virtual_row_key": None,
					"is_virtual_group": 0,
					"level_sequence": 3,
				},
				"voucher_scope": {"voucher_no": self.voucher},
				"pagination": {
					"page": 1,
					"page_size": 50,
					"sort_field": "display_code",
					"sort_order": "asc",
				},
			},
		}

	def test_filter_account_a_only_item_a(self):
		result = get_item_summary(self._payload(self.acc_a))
		codes = {r.get("item_code") for r in (result.get("rows") or [])}
		self.assertIn(self.item_a, codes)
		self.assertNotIn(self.item_b, codes)

	def test_filter_account_b_only_item_b(self):
		result = get_item_summary(self._payload(self.acc_b))
		codes = {r.get("item_code") for r in (result.get("rows") or [])}
		self.assertIn(self.item_b, codes)
		self.assertNotIn(self.item_a, codes)


def cint_safe(value) -> int:
	try:
		return int(value or 0)
	except Exception:
		return 0
