# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Regression: Account Control field + Persian Costing Desk cleanup (v5.7.55)."""

from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import patch

import frappe

from erpnext_extensions.iran_accounting.fg_cost_allocation.allocate import (
	BatchAllocationState,
	allocate_pools_to_batches,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.gl_extract import (
	ExpensePoolSnapshot,
	GLLineSnapshot,
	RuleSpec,
	assign_gl_lines_to_rules,
	rule_matches_gl,
)
from erpnext_extensions.patches.post_model_sync.remove_legacy_persian_costing_desk_v5755 import (
	ENGLISH,
	LEGACY,
	_safe_delete,
	execute as cleanup_persian_desk,
)


class TestAccountControlFieldMeta(unittest.TestCase):
	def test_expense_rule_account_control_is_optional_link(self):
		meta = frappe.get_meta("Expense Account Dimension Rule")
		df = meta.get_field("account_control")
		self.assertIsNotNone(df)
		self.assertEqual(df.fieldtype, "Link")
		self.assertEqual(df.options, "Account")
		self.assertFalse(int(df.reqd or 0))
		self.assertTrue(int(df.in_list_view or 0))

	def test_detail_doctypes_expose_account_control_filter(self):
		for dt in (
			"MFG Cost Expense Pool",
			"MFG Cost GL Line",
			"MFG Cost Batch Expense Allocation",
			"MFG Cost Account Dimension Summary",
		):
			meta = frappe.get_meta(dt)
			df = meta.get_field("account_control")
			self.assertIsNotNone(df, msg=dt)
			self.assertEqual(df.options, "Account", msg=dt)
			self.assertTrue(int(df.in_standard_filter or 0), msg=dt)


class TestAccountControlPropagation(unittest.TestCase):
	def test_gl_matching_ignores_account_control(self):
		rule = RuleSpec(
			name="R1",
			account="611004",
			cost_center="",
			department="Dept A",
			enabled=1,
			account_control="211001",
		)
		self.assertTrue(
			rule_matches_gl(rule, account="611004", department="Dept A", cost_center="CC1")
		)
		# Control account must never be used for GL matching.
		self.assertFalse(
			rule_matches_gl(rule, account="211001", department="Dept A", cost_center="CC1")
		)

	def test_pool_carries_account_control_into_batch_expense(self):
		pool = ExpensePoolSnapshot(
			settings_rule="R1",
			account="611028",
			cost_center="",
			department="بسته بندی",
			monthly_net_expense=Decimal("1000"),
			account_control="211001",
		)
		batch = BatchAllocationState(
			batch_no="B1",
			finished_item="FG-1",
			actual_qty=Decimal("10"),
			uom="Nos",
			normalization_factor=Decimal("1"),
			normalized_qty=Decimal("10"),
			total_monthly_normalized_qty=Decimal("10"),
			calculated_share=Decimal("1"),
			manual_share=None,
			is_manual_override=False,
			applied_share=Decimal("1"),
		)
		rows = allocate_pools_to_batches([pool], [batch], currency="IRR")
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].account, "611028")
		self.assertEqual(rows[0].account_control, "211001")
		self.assertEqual(rows[0].allocated_amount, Decimal("1000"))

	def test_blank_account_control_propagates_as_blank(self):
		pool = ExpensePoolSnapshot(
			settings_rule="R1",
			account="611004",
			cost_center="",
			department="بسته بندی",
			monthly_net_expense=Decimal("50"),
			account_control="",
		)
		batch = BatchAllocationState(
			batch_no="B1",
			finished_item="FG-1",
			actual_qty=Decimal("1"),
			uom="Nos",
			normalization_factor=Decimal("1"),
			normalized_qty=Decimal("1"),
			total_monthly_normalized_qty=Decimal("1"),
			calculated_share=Decimal("1"),
			manual_share=None,
			is_manual_override=False,
			applied_share=Decimal("1"),
		)
		rows = allocate_pools_to_batches([pool], [batch], currency="IRR")
		self.assertEqual(rows[0].account_control, "")

	def test_assign_gl_unchanged_with_account_control_on_rule(self):
		rules = [
			RuleSpec(
				name="R1",
				account="611030",
				cost_center="",
				department="تولید",
				enabled=1,
				account_control="211002",
			)
		]
		lines = [
			GLLineSnapshot(
				gl_entry="GLE-1",
				voucher_type="Journal Entry",
				voucher_no="JV-1",
				posting_date="2026-04-01",
				account="611030",
				cost_center="CC",
				department="تولید",
				debit=Decimal("100"),
				credit=Decimal("0"),
				net_amount=Decimal("100"),
			)
		]
		by_rule, overlaps, matched, unmatched = assign_gl_lines_to_rules(rules, lines)
		self.assertEqual(len(overlaps), 0)
		self.assertEqual(matched, Decimal("100"))
		self.assertEqual(unmatched, Decimal("0"))
		self.assertEqual(len(by_rule["R1"]), 1)


class TestPersianDeskCleanup(unittest.TestCase):
	def test_safe_delete_skips_missing(self):
		with patch(
			"erpnext_extensions.patches.post_model_sync.remove_legacy_persian_costing_desk_v5755.frappe.db.exists",
			return_value=False,
		):
			self.assertFalse(_safe_delete("Workspace", LEGACY))

	def test_safe_delete_never_removes_english(self):
		with patch(
			"erpnext_extensions.patches.post_model_sync.remove_legacy_persian_costing_desk_v5755.frappe.db.exists",
			return_value=True,
		), patch(
			"erpnext_extensions.patches.post_model_sync.remove_legacy_persian_costing_desk_v5755.frappe.delete_doc"
		) as delete_doc:
			self.assertFalse(_safe_delete("Workspace", ENGLISH))
			delete_doc.assert_not_called()

	def test_cleanup_idempotent_when_legacy_absent(self):
		with patch(
			"erpnext_extensions.patches.post_model_sync.remove_legacy_persian_costing_desk_v5755.frappe.db.exists",
			return_value=False,
		), patch(
			"erpnext_extensions.patches.post_model_sync.remove_legacy_persian_costing_desk_v5755.frappe.get_all",
			return_value=[],
		), patch(
			"erpnext_extensions.patches.post_model_sync.remove_legacy_persian_costing_desk_v5755.clear_every_users_desktop_icons"
		), patch(
			"erpnext_extensions.patches.post_model_sync.remove_legacy_persian_costing_desk_v5755.frappe.db.commit"
		):
			# First and second run must not raise.
			cleanup_persian_desk()
			cleanup_persian_desk()


class TestExistingRulesAccountControlIntegrity(unittest.TestCase):
	def test_existing_rules_intact_with_optional_account_control(self):
		if not frappe.db.exists("DocType", "Expense Account Dimension Rule"):
			self.skipTest("Expense Account Dimension Rule missing")
		if not frappe.db.has_column("Expense Account Dimension Rule", "account_control"):
			self.skipTest("account_control column not migrated yet")
		total = frappe.db.count("Expense Account Dimension Rule")
		self.assertEqual(total, 3026)
		# No duplicate Account × Cost Center × Department keys.
		dups = frappe.db.sql(
			"""
			SELECT account, IFNULL(cost_center,''), IFNULL(department,''), COUNT(*) c
			FROM `tabExpense Account Dimension Rule`
			GROUP BY 1,2,3 HAVING c > 1
			"""
		)
		self.assertEqual(len(dups), 0)
		# Account Control, when set, must resolve to an Account (never orphaned).
		unresolved = frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabExpense Account Dimension Rule` r
			LEFT JOIN `tabAccount` a ON a.name = r.account_control
			WHERE IFNULL(r.account_control,'') != '' AND a.name IS NULL
			"""
		)[0][0]
		self.assertEqual(unresolved, 0)


if __name__ == "__main__":
	unittest.main()
