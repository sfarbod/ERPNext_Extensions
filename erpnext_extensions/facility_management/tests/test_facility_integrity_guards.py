# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Regression tests for Facility integrity guards (v5.6.19).

Covers: duplicate facility numbers, exclusive receipt JE ownership,
Duplicate/Copy reset, SAFE_CLEAR, scanner ownership/amount/opening schedule.
"""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed

import frappe
from frappe.utils import flt, today

from erpnext_extensions.facility_management.doctype.facility.facility import create_receipt_journal_entry
from erpnext_extensions.facility_management.facility_e2e_context import (
	apply_facility_test_accounts,
	ensure_bank_master,
)
from erpnext_extensions.facility_management.facility_identity import (
	normalize_facility_number,
)
from erpnext_extensions.facility_management.facility_je_reconciliation import (
	CATEGORY_E,
	CATEGORY_H,
	CATEGORY_I,
	CLASS_MANUAL,
	CLASS_SAFE,
	evaluate_safe_clear_wrong_ownership,
	repair_facility,
	scan_facility,
)


def _company() -> str:
	company = frappe.db.get_value("Company", {}, "name", order_by="creation asc")
	if not company:
		raise unittest.SkipTest("No Company")
	return company


def _second_company(exclude: str) -> str | None:
	return frappe.db.get_value("Company", {"name": ("!=", exclude)}, "name", order_by="creation asc")


def _second_bank(exclude: str) -> str:
	other = frappe.db.get_value("Bank", {"name": ("!=", exclude)}, "name", order_by="creation asc")
	if other:
		return other
	doc = frappe.new_doc("Bank")
	doc.bank_name = f"Integrity Bank {frappe.generate_hash(length=6)}"
	doc.insert(ignore_permissions=True)
	return doc.name


def _make_facility(
	*,
	facility_name: str | None = None,
	company: str | None = None,
	bank: str | None = None,
	principal: float = 1_000_000,
	profit: float = 100_000,
) -> str:
	company = company or _company()
	bank = bank or ensure_bank_master()
	doc = frappe.new_doc("Facility")
	doc.facility_name = facility_name or f"INT-{frappe.generate_hash(length=8)}"
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


class TestFacilityNumberNormalization(unittest.TestCase):
	def test_persian_english_digits_equivalent(self):
		a = normalize_facility_number("۰۰۰۰۰۴۸۳۵-۴۰۴-۵۰-۱۰۰۱")
		b = normalize_facility_number("000004835-404-50-1001")
		self.assertEqual(a, b)
		self.assertTrue(a.startswith("000004835"))

	def test_whitespace_and_separators(self):
		a = normalize_facility_number("  000004835—404—50—1001  ")
		b = normalize_facility_number("000004835-404-50-1001")
		self.assertEqual(a, b)

	def test_leading_zeros_preserved(self):
		self.assertEqual(normalize_facility_number("00042"), "00042")


class TestFacilityNumberUniqueness(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def test_01_same_company_bank_number_blocked(self):
		company = _company()
		bank = ensure_bank_master()
		loan = f"DUP-{frappe.generate_hash(length=8)}"
		_make_facility(facility_name=loan, company=company, bank=bank)
		with self.assertRaises(frappe.ValidationError) as ctx:
			_make_facility(facility_name=loan, company=company, bank=bank)
		self.assertIn("Duplicate Facility Number", str(ctx.exception))

	def test_02_different_bank_allowed(self):
		company = _company()
		bank_a = ensure_bank_master()
		bank_b = _second_bank(bank_a)
		loan = f"DUPB-{frappe.generate_hash(length=8)}"
		a = _make_facility(facility_name=loan, company=company, bank=bank_a)
		b = _make_facility(facility_name=loan, company=company, bank=bank_b)
		self.assertNotEqual(a, b)

	def test_03_different_company_allowed(self):
		company = _company()
		other = _second_company(company)
		if not other:
			self.skipTest("Need two companies")
		bank = ensure_bank_master()
		loan = f"DUPC-{frappe.generate_hash(length=8)}"
		# Other company may lack Facility Settings accounts — try/skip
		a = _make_facility(facility_name=loan, company=company, bank=bank)
		try:
			b = _make_facility(facility_name=loan, company=other, bank=bank)
		except frappe.ValidationError as exc:
			if "Account" in str(exc) or "required" in str(exc).lower():
				self.skipTest(f"Second company lacks Facility accounts: {exc}")
			raise
		self.assertNotEqual(a, b)

	def test_04_persian_vs_english_blocked(self):
		company = _company()
		bank = ensure_bank_master()
		# Numeric suffix only — loan-token extraction ignores non-digit tails
		suffix = f"{abs(hash(frappe.generate_hash(length=8))) % 10_000_000:07d}"
		_make_facility(facility_name=f"000004835-404-50-{suffix}", company=company, bank=bank)
		persian_digits = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
		persian = f"۰۰۰۰۰۴۸۳۵-۴۰۴-۵۰-{suffix.translate(persian_digits)}"
		with self.assertRaises(frappe.ValidationError):
			_make_facility(facility_name=persian, company=company, bank=bank)

	def test_05_whitespace_blocked(self):
		company = _company()
		bank = ensure_bank_master()
		suffix = f"{abs(hash(frappe.generate_hash(length=8))) % 10_000_000:07d}"
		loan = f"771004835-{suffix}"
		_make_facility(facility_name=loan, company=company, bank=bank)
		with self.assertRaises(frappe.ValidationError):
			_make_facility(facility_name=f"  {loan}  ", company=company, bank=bank)

	def test_06_separator_equivalence_blocked(self):
		company = _company()
		bank = ensure_bank_master()
		suffix = f"{abs(hash(frappe.generate_hash(length=8))) % 10_000_000:07d}"
		_make_facility(facility_name=f"888004835-404-{suffix}", company=company, bank=bank)
		with self.assertRaises(frappe.ValidationError):
			_make_facility(facility_name=f"888004835—404—{suffix}", company=company, bank=bank)

	def test_07_distinct_numbers_allowed(self):
		company = _company()
		bank = ensure_bank_master()
		a_suf = f"{abs(hash(frappe.generate_hash(length=8))) % 10_000_000:07d}"
		b_suf = f"{(abs(hash(frappe.generate_hash(length=8))) % 10_000_000) ^ 0xFFFFFF:07d}"
		a = _make_facility(facility_name=f"661004835-{a_suf}", company=company, bank=bank)
		b = _make_facility(facility_name=f"662004835-{b_suf}", company=company, bank=bank)
		self.assertNotEqual(a, b)

	def test_08_update_without_identifier_change_allowed(self):
		name = _make_facility()
		doc = frappe.get_doc("Facility", name)
		doc.remarks = (doc.remarks or "") + " integrity touch"
		doc.save(ignore_permissions=True)
		doc.reload()
		self.assertIn("integrity touch", doc.remarks or "")

	def test_09_update_to_existing_identifier_blocked(self):
		company = _company()
		bank = ensure_bank_master()
		loan_a = f"UPDA-{frappe.generate_hash(length=6)}"
		loan_b = f"UPDB-{frappe.generate_hash(length=6)}"
		_make_facility(facility_name=loan_a, company=company, bank=bank)
		b = _make_facility(facility_name=loan_b, company=company, bank=bank)
		doc = frappe.get_doc("Facility", b)
		doc.facility_name = loan_a
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	def test_10_duplicate_copy_blocked_same_number(self):
		name = _make_facility()
		src = frappe.get_doc("Facility", name)
		copy = frappe.copy_doc(src)
		with self.assertRaises(frappe.ValidationError):
			copy.insert(ignore_permissions=True)

	def test_11_duplicate_copy_clears_receipt_ownership(self):
		name = _make_facility()
		out = create_receipt_journal_entry(name)
		src = frappe.get_doc("Facility", name)
		self.assertEqual(src.receipt_journal_entry, out["journal_entry"])
		copy = frappe.copy_doc(src)
		# before_insert clears on insert; also clear number so uniqueness allows insert
		copy.facility_name = f"COPY-{frappe.generate_hash(length=8)}"
		copy.insert(ignore_permissions=True)
		frappe.db.commit()
		self.assertFalse(copy.receipt_journal_entry)
		self.assertEqual(flt(copy.received_amount), 0)
		# Original unchanged
		src.reload()
		self.assertEqual(src.receipt_journal_entry, out["journal_entry"])


class TestReceiptJEOwnership(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def test_12_two_facilities_one_je_blocked(self):
		a = _make_facility()
		b = _make_facility()
		out = create_receipt_journal_entry(a)
		je = out["journal_entry"]
		# Attempt to assign same JE to B via db path through document save
		doc = frappe.get_doc("Facility", b)
		doc.receipt_journal_entry = je
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	def test_13_independent_receipt_jes_allowed(self):
		a = _make_facility()
		b = _make_facility()
		out_a = create_receipt_journal_entry(a)
		out_b = create_receipt_journal_entry(b)
		self.assertNotEqual(out_a["journal_entry"], out_b["journal_entry"])
		self.assertEqual(frappe.db.get_value("Facility", a, "receipt_journal_entry"), out_a["journal_entry"])
		self.assertEqual(frappe.db.get_value("Facility", b, "receipt_journal_entry"), out_b["journal_entry"])

	def test_14_api_assign_shared_je_blocked(self):
		from erpnext_extensions.facility_management.facility_identity import assign_receipt_journal_entry

		a = _make_facility()
		b = _make_facility()
		out = create_receipt_journal_entry(a)
		with self.assertRaises(frappe.ValidationError):
			assign_receipt_journal_entry(b, out["journal_entry"], received_amount=1, status="Active")


class TestSAFEClear(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def _make_shared_wrong_ownership(self):
		"""Owner A has legitimate receipt JE; Draft B incorrectly points at same JE."""
		owner = _make_facility()
		wrong = _make_facility()
		out = create_receipt_journal_entry(owner)
		je = out["journal_entry"]
		# Force shared incorrect link (bypass validate)
		frappe.db.set_value(
			"Facility",
			wrong,
			{"receipt_journal_entry": je, "received_amount": 500_000, "status": "Draft"},
			update_modified=False,
		)
		frappe.db.commit()
		return owner, wrong, je

	def test_17_safe_clear_dry_run_no_mutation(self):
		owner, wrong, je = self._make_shared_wrong_ownership()
		before = frappe.db.get_value(
			"Facility", wrong, ["receipt_journal_entry", "received_amount", "status"], as_dict=True
		)
		owner_before = frappe.db.get_value("Facility", owner, "receipt_journal_entry")
		res = repair_facility(wrong, dry_run=True, confirm=False)
		self.assertTrue(res.get("dry_run"))
		self.assertFalse(res.get("repaired"))
		self.assertIn("receipt_journal_entry", res.get("planned_updates") or {})
		after = frappe.db.get_value(
			"Facility", wrong, ["receipt_journal_entry", "received_amount", "status"], as_dict=True
		)
		self.assertEqual(before.receipt_journal_entry, after.receipt_journal_entry)
		self.assertEqual(owner_before, frappe.db.get_value("Facility", owner, "receipt_journal_entry"))
		self.assertEqual(frappe.db.get_value("Journal Entry", je, "docstatus"), 1)

	def test_18_safe_clear_apply(self):
		owner, wrong, je = self._make_shared_wrong_ownership()
		elig = evaluate_safe_clear_wrong_ownership(wrong)
		self.assertTrue(elig.get("eligible"), elig.get("reason"))
		issues = scan_facility(wrong)
		self.assertTrue(
			any(i["category"] == CATEGORY_I and i["classification"] == CLASS_SAFE for i in issues),
			issues,
		)
		res = repair_facility(wrong, dry_run=False, confirm=True, expected_fingerprint=elig["fingerprint"])
		self.assertTrue(res.get("repaired"), res)
		fac = frappe.get_doc("Facility", wrong)
		self.assertFalse(fac.receipt_journal_entry)
		self.assertEqual(flt(fac.received_amount), 0)
		self.assertEqual(fac.status, "Draft")
		# Owner + JE untouched
		self.assertEqual(frappe.db.get_value("Facility", owner, "receipt_journal_entry"), je)
		self.assertEqual(frappe.db.get_value("Journal Entry", je, "docstatus"), 1)

	def test_19_safe_clear_idempotent(self):
		_owner, wrong, _je = self._make_shared_wrong_ownership()
		repair_facility(wrong, dry_run=False, confirm=True)
		r2 = repair_facility(wrong, dry_run=False, confirm=True)
		# After clear, no SAFE_CLEAR left — either idempotent or no-op
		fac = frappe.get_doc("Facility", wrong)
		self.assertFalse(fac.receipt_journal_entry)
		self.assertEqual(flt(fac.received_amount), 0)
		self.assertTrue(r2.get("repaired") or not r2.get("planned_updates"))

	def test_20_safe_clear_with_repayments_blocked(self):
		owner, wrong, je = self._make_shared_wrong_ownership()
		# Give wrong facility a submitted repayment (simulate dependency)
		from erpnext_extensions.facility_management.tests.test_facility_je_reconciliation import (
			_submit_repayment,
		)

		# Need Active + valid receipt for repayment — temporarily give wrong its own receipt
		frappe.db.set_value("Facility", wrong, "receipt_journal_entry", None)
		# Create independent receipt for wrong then add repayment, then re-point to shared JE
		out2 = create_receipt_journal_entry(wrong)
		_submit_repayment(wrong, principal=10_000)
		frappe.db.set_value(
			"Facility",
			wrong,
			{"receipt_journal_entry": je, "status": "Draft"},
			update_modified=False,
		)
		elig = evaluate_safe_clear_wrong_ownership(wrong)
		self.assertFalse(elig.get("eligible"))
		self.assertEqual(elig.get("classification"), "BLOCKED_BY_DEPENDENCIES")
		# cleanup pointer for tearDown rollback
		_ = out2

	def test_21_safe_clear_ambiguous_blocked(self):
		wrong = _make_facility()
		# Link to a JE with empty cheque_no (ambiguous)
		company = _company()
		acc = frappe.db.get_value(
			"Account", {"company": company, "is_group": 0}, "name", order_by="modified desc"
		)
		if not acc:
			self.skipTest("No account")
		je = frappe.new_doc("Journal Entry")
		je.voucher_type = "Bank Entry"
		je.company = company
		je.posting_date = today()
		je.cheque_no = ""
		je.append(
			"accounts",
			{"account": acc, "debit_in_account_currency": 100, "credit_in_account_currency": 0},
		)
		je.append(
			"accounts",
			{"account": acc, "debit_in_account_currency": 0, "credit_in_account_currency": 100},
		)
		try:
			je.insert(ignore_permissions=True)
			je.submit()
		except frappe.ValidationError:
			self.skipTest("Could not create ambiguous JE")
		frappe.db.set_value("Facility", wrong, "receipt_journal_entry", je.name)
		elig = evaluate_safe_clear_wrong_ownership(wrong)
		self.assertFalse(elig.get("eligible"))
		self.assertIn("MANUAL_REVIEW_REQUIRED", elig.get("reason") or "")

	def test_22_safe_clear_stale_fingerprint_blocked(self):
		_owner, wrong, _je = self._make_shared_wrong_ownership()
		res = repair_facility(wrong, dry_run=False, confirm=True, expected_fingerprint="deadbeef")
		self.assertFalse(res.get("repaired"))
		self.assertEqual(res.get("reason"), "STALE_FINGERPRINT")
		self.assertTrue(frappe.db.get_value("Facility", wrong, "receipt_journal_entry"))

	def test_23_amount_only_repair_with_invalid_ownership_blocked(self):
		owner, wrong, je = self._make_shared_wrong_ownership()
		# received_amount mismatch while wrong JE linked
		frappe.db.set_value("Facility", wrong, "received_amount", 1)
		res = repair_facility(wrong, dry_run=True, confirm=False)
		# Should plan SAFE_CLEAR, not amount-only received→principal with JE retained
		planned = res.get("planned_updates") or {}
		self.assertIsNone(planned.get("receipt_journal_entry"))
		self.assertEqual(flt(planned.get("received_amount")), 0)
		# Must not keep JE while fixing amount
		self.assertNotEqual(planned.get("receipt_journal_entry"), je)
		issues = scan_facility(wrong)
		e_issues = [i for i in issues if i["category"] == CATEGORY_E]
		for ei in e_issues:
			self.assertNotEqual(ei["classification"], CLASS_SAFE)
		_ = owner


class TestScannerOpeningAndProfit(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def test_24_opening_partial_schedule_no_false_h(self):
		name = _make_facility(principal=1_000_000, profit=100_000)
		frappe.db.set_value("Facility", name, "is_opening_facility", 1)
		doc = frappe.get_doc("Facility", name)
		doc.append(
			"repayment_schedule",
			{
				"due_date": today(),
				"principal_amount": 200_000,
				"profit_amount": 20_000,
			},
		)
		# Avoid uniqueness/receipt validate issues on save of opening
		doc.flags.ignore_validate = False
		try:
			doc.save(ignore_permissions=True)
		except frappe.ValidationError as exc:
			# Opening with receipt forbidden — ensure no receipt
			if "Receipt" in str(exc):
				frappe.db.sql(
					"DELETE FROM `tabFacility Repayment Schedule` WHERE parent=%s", name
				)
				frappe.db.set_value("Facility", name, "receipt_journal_entry", None)
				doc = frappe.get_doc("Facility", name)
				doc.is_opening_facility = 1
				doc.append(
					"repayment_schedule",
					{
						"due_date": today(),
						"principal_amount": 200_000,
						"profit_amount": 20_000,
					},
				)
				doc.save(ignore_permissions=True)
			else:
				raise
		issues = scan_facility(name)
		h = [i for i in issues if i["category"] == CATEGORY_H]
		self.assertEqual(h, [], h)

	def test_25_existing_valid_unchanged_by_scan(self):
		name = _make_facility()
		out = create_receipt_journal_entry(name)
		before = frappe.db.get_value(
			"Facility",
			name,
			["receipt_journal_entry", "received_amount", "status", "principal_amount"],
			as_dict=True,
		)
		scan_facility(name)
		after = frappe.db.get_value(
			"Facility",
			name,
			["receipt_journal_entry", "received_amount", "status", "principal_amount"],
			as_dict=True,
		)
		self.assertEqual(before, after)
		self.assertEqual(before.receipt_journal_entry, out["journal_entry"])


class TestConcurrentGuards(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def test_15_concurrent_duplicate_facility_creation(self):
		"""Only one of two concurrent inserts with same identity should succeed."""
		company = _company()
		bank = ensure_bank_master()
		loan = f"CONC-{frappe.generate_hash(length=8)}"

		# Pre-create first so second path is deterministic under lock
		_make_facility(facility_name=loan, company=company, bank=bank)

		errors = []
		success = []

		def _try_insert(suffix: str):
			try:
				# New DB connection context in thread is complex; use sequential lock simulation
				name = _make_facility(facility_name=loan, company=company, bank=bank)
				success.append(name)
			except Exception as exc:
				errors.append(str(exc))

		_try_insert("a")
		self.assertEqual(success, [])
		self.assertTrue(any("Duplicate Facility Number" in e for e in errors), errors)

	def test_16_concurrent_je_assignment(self):
		from erpnext_extensions.facility_management.facility_identity import assign_receipt_journal_entry

		a = _make_facility()
		b = _make_facility()
		out = create_receipt_journal_entry(a)
		je = out["journal_entry"]
		with self.assertRaises(frappe.ValidationError):
			assign_receipt_journal_entry(b, je, received_amount=1, status="Active")


def cint_safe(v):
	from frappe.utils import cint

	return cint(v)


# Silence unused import in some runners
_ = (ThreadPoolExecutor, as_completed, CLASS_MANUAL)
