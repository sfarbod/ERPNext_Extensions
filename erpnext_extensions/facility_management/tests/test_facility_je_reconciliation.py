# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Regression tests for Facility JE reconciliation (v5.6.3)."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt, today

from erpnext_extensions.facility_management.doctype.facility.facility import create_receipt_journal_entry
from erpnext_extensions.facility_management.facility_e2e_context import (
	apply_facility_test_accounts,
	ensure_bank_master,
)
from erpnext_extensions.facility_management.facility_je_reconciliation import (
	CLASS_BLOCKED,
	CLASS_SAFE,
	repair_facility,
	repair_repayment,
	run_global_scan,
	scan_facility,
	verify_facility,
)


def _company() -> str:
	company = frappe.db.get_value("Company", {}, "name", order_by="creation asc")
	if not company:
		raise unittest.SkipTest("No Company")
	return company


def _make_facility(*, principal: float = 1_000_000, profit: float = 100_000) -> str:
	company = _company()
	bank = ensure_bank_master()
	doc = frappe.new_doc("Facility")
	doc.facility_name = f"JE Rec Test {frappe.generate_hash(length=6)}"
	doc.company = company
	doc.bank = bank
	doc.status = "Draft"
	doc.contract_date = today()
	doc.receive_date = today()
	doc.principal_amount = principal
	doc.profit_amount = profit
	doc.installment_count = 1
	apply_facility_test_accounts(doc, company=company)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc.name


def _submit_repayment(facility: str, *, principal: float, profit: float = 0, penalty: float = 0) -> str:
	fac = frappe.get_doc("Facility", facility)
	if (profit or penalty) and not fac.department:
		dept = frappe.db.get_value("Department", {"company": fac.company}, "name") or frappe.db.get_value(
			"Department", {}, "name"
		)
		if dept:
			frappe.db.set_value("Facility", facility, "department", dept, update_modified=False)
			fac.department = dept
	rep = frappe.new_doc("Facility Repayment")
	rep.facility = facility
	rep.company = fac.company
	rep.posting_date = today()
	rep.repayment_method = "Bank Account"
	rep.principal_amount = principal
	rep.profit_amount = profit
	rep.penalty_amount = penalty
	rep.bank_account = fac.bank_account
	rep.loan_payable_account = fac.loan_payable_account
	rep.deferred_loan_interest_account = fac.deferred_loan_interest_account
	rep.interest_expense_account = fac.interest_expense_account
	rep.penalty_expense_account = fac.penalty_expense_account
	rep.cost_center = fac.cost_center
	rep.department = fac.department
	rep.insert(ignore_permissions=True)
	rep.submit()
	frappe.db.commit()
	return rep.name


class TestFacilityJEReconciliation(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def test_01_02_normal_receipt_create_and_submit(self):
		name = _make_facility()
		out = create_receipt_journal_entry(name)
		fac = frappe.get_doc("Facility", name)
		self.assertEqual(fac.status, "Active")
		self.assertEqual(fac.receipt_journal_entry, out["journal_entry"])
		self.assertEqual(frappe.db.get_value("Journal Entry", out["journal_entry"], "docstatus"), 1)
		self.assertAlmostEqual(flt(fac.received_amount), flt(fac.principal_amount))

	def test_03_04_05_receipt_cancel_restores_facility(self):
		name = _make_facility()
		out = create_receipt_journal_entry(name)
		je_name = out["journal_entry"]
		je = frappe.get_doc("Journal Entry", je_name)
		je.cancel()
		frappe.db.commit()
		fac = frappe.get_doc("Facility", name)
		self.assertFalse(fac.receipt_journal_entry)
		self.assertEqual(fac.status, "Draft")
		self.assertEqual(flt(fac.received_amount), 0)

	def test_06_07_replacement_and_duplicate_prevention(self):
		name = _make_facility()
		out = create_receipt_journal_entry(name)
		je = frappe.get_doc("Journal Entry", out["journal_entry"])
		je.cancel()
		frappe.db.commit()
		# Replacement allowed
		out2 = create_receipt_journal_entry(name)
		self.assertTrue(out2["journal_entry"])
		self.assertNotEqual(out2["journal_entry"], out["journal_entry"])
		# Duplicate blocked
		with self.assertRaises(frappe.ValidationError):
			create_receipt_journal_entry(name)

	def test_08_09_10_11_12_repayment_lifecycle(self):
		name = _make_facility(principal=500_000, profit=50_000)
		create_receipt_journal_entry(name)
		rep_name = _submit_repayment(name, principal=100_000, profit=10_000)
		rep = frappe.get_doc("Facility Repayment", rep_name)
		self.assertEqual(rep.docstatus, 1)
		self.assertTrue(rep.journal_entry)
		je_name = rep.journal_entry
		rep.cancel()
		frappe.db.commit()
		rep.reload()
		self.assertEqual(rep.docstatus, 2)
		self.assertFalse(rep.journal_entry)
		self.assertEqual(frappe.db.get_value("Journal Entry", je_name, "docstatus"), 2)
		fac = frappe.get_doc("Facility", name)
		self.assertEqual(flt(fac.paid_principal_amount), 0)

	def test_13_replacement_repayment_after_cancel(self):
		name = _make_facility(principal=500_000, profit=0)
		create_receipt_journal_entry(name)
		rep1 = _submit_repayment(name, principal=100_000)
		frappe.get_doc("Facility Repayment", rep1).cancel()
		frappe.db.commit()
		rep2 = _submit_repayment(name, principal=100_000)
		self.assertNotEqual(
			frappe.db.get_value("Facility Repayment", rep1, "journal_entry"),
			frappe.db.get_value("Facility Repayment", rep2, "journal_entry"),
		)
		self.assertEqual(frappe.db.get_value("Facility Repayment", rep2, "docstatus"), 1)

	def test_14_15_partial_and_full_repayment_cancel(self):
		name = _make_facility(principal=200_000, profit=0)
		create_receipt_journal_entry(name)
		a = _submit_repayment(name, principal=80_000)
		b = _submit_repayment(name, principal=120_000)
		frappe.get_doc("Facility Repayment", b).cancel()
		frappe.db.commit()
		fac = frappe.get_doc("Facility", name)
		self.assertAlmostEqual(flt(fac.paid_principal_amount), 80_000)
		self.assertAlmostEqual(flt(fac.remaining_principal_amount), 120_000)
		# cancel remaining
		frappe.get_doc("Facility Repayment", a).cancel()
		frappe.db.commit()
		fac.reload()
		self.assertEqual(flt(fac.paid_principal_amount), 0)

	def test_16_schedule_has_no_je_refs(self):
		"""Repayment Schedule child table is contractual only."""
		meta = frappe.get_meta("Facility Repayment Schedule")
		self.assertFalse(meta.has_field("journal_entry"))

	def test_17_cancel_receipt_blocked_with_active_repayments(self):
		name = _make_facility(principal=300_000, profit=0)
		out = create_receipt_journal_entry(name)
		_submit_repayment(name, principal=50_000)
		je = frappe.get_doc("Journal Entry", out["journal_entry"])
		with self.assertRaises(frappe.ValidationError):
			je.cancel()

	def test_18_direct_repayment_je_cancel_blocked(self):
		name = _make_facility(principal=300_000, profit=0)
		create_receipt_journal_entry(name)
		rep_name = _submit_repayment(name, principal=50_000)
		je_name = frappe.db.get_value("Facility Repayment", rep_name, "journal_entry")
		je = frappe.get_doc("Journal Entry", je_name)
		with self.assertRaises(frappe.ValidationError):
			je.cancel()

	def test_19_multiple_historical_jes_detected(self):
		name = _make_facility()
		out = create_receipt_journal_entry(name)
		# Simulate historical: cancel via SQL-style stale state after cancel+manual re-link mess
		je = frappe.get_doc("Journal Entry", out["journal_entry"])
		# Use flag path: cancel clears facility; create second; then force stale cancelled ref
		je.cancel()
		frappe.db.commit()
		out2 = create_receipt_journal_entry(name)
		# Point at cancelled original while keeping Active — scanner should flag
		frappe.db.set_value(
			"Facility",
			name,
			{"receipt_journal_entry": out["journal_entry"], "status": "Active"},
			update_modified=False,
		)
		issues = scan_facility(name)
		cats = {i["category"] for i in issues}
		self.assertTrue(any("CANCELLED_RECEIPT" in c or "REPLACEMENT" in c for c in cats))
		# cleanup: relink to valid
		frappe.db.set_value("Facility", name, "receipt_journal_entry", out2["journal_entry"])

	def test_20_missing_je_reference(self):
		name = _make_facility()
		frappe.db.set_value(
			"Facility",
			name,
			{"receipt_journal_entry": "ACC-JV-DOES-NOT-EXIST", "status": "Draft", "received_amount": 1},
		)
		issues = scan_facility(name)
		self.assertTrue(any(i["classification"] == CLASS_SAFE for i in issues))

	def test_21_unrelated_je_cancel_noop(self):
		# Create a plain JE not linked to facility
		company = _company()
		acc = frappe.db.get_value(
			"Account", {"company": company, "is_group": 0}, "name", order_by="modified desc"
		)
		if not acc:
			self.skipTest("No account")
		je = frappe.new_doc("Journal Entry")
		je.voucher_type = "Journal Entry"
		je.company = company
		je.posting_date = today()
		je.append(
			"accounts",
			{"account": acc, "debit_in_account_currency": 1, "credit_in_account_currency": 0},
		)
		je.append(
			"accounts",
			{"account": acc, "debit_in_account_currency": 0, "credit_in_account_currency": 1},
		)
		try:
			je.insert(ignore_permissions=True)
			je.submit()
			je.cancel()
		except frappe.ValidationError:
			# Some sites reject same-account JE; treat as skip
			self.skipTest("Could not create unrelated JE")

	def test_25_repair_utility_idempotency(self):
		name = _make_facility()
		out = create_receipt_journal_entry(name)
		# Force historical stale cancelled receipt without going through hook
		frappe.db.set_value("Journal Entry", out["journal_entry"], "docstatus", 2)
		# Also mark GL cancelled-ish not required for scanner
		frappe.db.set_value(
			"Facility",
			name,
			{"receipt_journal_entry": out["journal_entry"], "status": "Active", "received_amount": 1_000_000},
		)
		r1 = repair_facility(name, dry_run=False, confirm=True)
		self.assertTrue(r1.get("repaired"))
		r2 = repair_facility(name, dry_run=False, confirm=True)
		# Second pass: no safe clear left
		v = verify_facility(name)
		self.assertTrue(v["replacement_receipt_eligible"])
		self.assertFalse(v["snapshot"]["receipt_journal_entry"])

	def test_28_repair_does_not_touch_unrelated(self):
		a = _make_facility()
		b = _make_facility()
		create_receipt_journal_entry(a)
		create_receipt_journal_entry(b)
		je_a = frappe.db.get_value("Facility", a, "receipt_journal_entry")
		frappe.db.set_value("Journal Entry", je_a, "docstatus", 2)
		before_b = frappe.db.get_value(
			"Facility", b, ["receipt_journal_entry", "status", "modified"], as_dict=True
		)
		repair_facility(a, dry_run=False, confirm=True)
		after_b = frappe.db.get_value(
			"Facility", b, ["receipt_journal_entry", "status", "modified"], as_dict=True
		)
		self.assertEqual(before_b.receipt_journal_entry, after_b.receipt_journal_entry)
		self.assertEqual(before_b.status, after_b.status)

	def test_29_scanner_completeness_categories(self):
		report = run_global_scan(dry_run=True)
		self.assertIn("totals", report)
		self.assertIn("by_category", report["totals"])
		self.assertIn("safe_auto_repair", report["totals"])

	def test_30_dry_run_safety(self):
		name = _make_facility()
		out = create_receipt_journal_entry(name)
		frappe.db.set_value("Journal Entry", out["journal_entry"], "docstatus", 2)
		before = frappe.db.get_value("Facility", name, "receipt_journal_entry")
		res = repair_facility(name, dry_run=True, confirm=False)
		self.assertTrue(res["dry_run"])
		self.assertEqual(frappe.db.get_value("Facility", name, "receipt_journal_entry"), before)

	def test_blocked_repair_with_repayments(self):
		name = _make_facility(principal=400_000, profit=0)
		out = create_receipt_journal_entry(name)
		_submit_repayment(name, principal=10_000)
		frappe.db.set_value("Journal Entry", out["journal_entry"], "docstatus", 2)
		issues = scan_facility(name)
		self.assertTrue(any(i["classification"] == CLASS_BLOCKED for i in issues))
		res = repair_facility(name, dry_run=False, confirm=True)
		self.assertFalse(res.get("repaired"))
		self.assertEqual(res.get("reason"), "BLOCKED_BY_DEPENDENCIES")

	def test_repayment_repair_cancels_orphan(self):
		name = _make_facility(principal=400_000, profit=0)
		create_receipt_journal_entry(name)
		rep_name = _submit_repayment(name, principal=25_000)
		je = frappe.db.get_value("Facility Repayment", rep_name, "journal_entry")
		# Simulate JE cancelled outside normal path (bypass before_cancel by direct db)
		frappe.flags.in_facility_repayment_je_cancel = True
		try:
			# Direct docstatus flip to simulate historical orphan
			frappe.db.set_value("Journal Entry", je, "docstatus", 2)
		finally:
			frappe.flags.in_facility_repayment_je_cancel = False
		res = repair_repayment(rep_name, dry_run=False, confirm=True)
		self.assertTrue(res.get("repaired"))
		self.assertEqual(frappe.db.get_value("Facility Repayment", rep_name, "docstatus"), 2)
		fac = frappe.get_doc("Facility", name)
		self.assertEqual(flt(fac.paid_principal_amount), 0)

	def test_repeated_cancel_reissue(self):
		name = _make_facility(principal=250_000, profit=0)
		for _ in range(2):
			out = create_receipt_journal_entry(name)
			frappe.get_doc("Journal Entry", out["journal_entry"]).cancel()
			frappe.db.commit()
		out3 = create_receipt_journal_entry(name)
		self.assertTrue(out3["journal_entry"])
		fac = frappe.get_doc("Facility", name)
		self.assertEqual(fac.status, "Active")


if __name__ == "__main__":
	unittest.main()
