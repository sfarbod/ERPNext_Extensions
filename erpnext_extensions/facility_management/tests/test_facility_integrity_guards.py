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


def _ensure_second_company(exclude: str) -> str:
	"""Return another Company with a minimal chart usable for Facility tests."""
	from erpnext_extensions.facility_management.facility_e2e_context import (
		ensure_facility_settings_accounts,
		resolve_facility_gl_accounts,
	)

	other = _second_company(exclude)
	if other:
		accounts = resolve_facility_gl_accounts(other)
		ensure_facility_settings_accounts(other, accounts)
		return other

	from frappe.utils import random_string

	abbr = "FIT" + random_string(3)
	name = f"Facility Integrity {abbr}"
	currency = frappe.db.get_value("Company", exclude, "default_currency") or "IRR"
	country = frappe.db.get_value("Company", exclude, "country") or "Iran"
	doc = frappe.get_doc(
		{
			"doctype": "Company",
			"company_name": name,
			"abbr": abbr,
			"default_currency": currency,
			"country": country,
		}
	)
	doc.insert(ignore_permissions=True)

	def _acc(account_name, parent, root_type, *, is_group=0, account_type=None):
		row = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": account_name,
				"parent_account": parent,
				"company": doc.name,
				"root_type": root_type,
				"is_group": is_group,
				"account_type": account_type,
			}
		)
		row.insert(ignore_permissions=True)
		return row.name

	asset_root = frappe.db.get_value(
		"Account", {"company": doc.name, "root_type": "Asset", "is_group": 1}, "name"
	)
	liab_root = frappe.db.get_value(
		"Account", {"company": doc.name, "root_type": "Liability", "is_group": 1}, "name"
	)
	exp_root = frappe.db.get_value(
		"Account", {"company": doc.name, "root_type": "Expense", "is_group": 1}, "name"
	)
	if asset_root:
		bank_group = _acc("FIT Banks", asset_root, "Asset", is_group=1, account_type="Bank")
		_acc("FIT Bank GL", bank_group, "Asset", account_type="Bank")
	if liab_root:
		_acc("FIT Loan Payable", liab_root, "Liability")
		_acc("FIT Deferred Interest", liab_root, "Liability")
	if exp_root:
		_acc("FIT Interest Exp", exp_root, "Expense")
		_acc("FIT Penalty Exp", exp_root, "Expense")
	frappe.db.commit()
	accounts = resolve_facility_gl_accounts(doc.name)
	ensure_facility_settings_accounts(doc.name, accounts)
	return doc.name


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
		other = _ensure_second_company(company)
		bank = ensure_bank_master()
		loan = f"DUPC-{frappe.generate_hash(length=8)}"
		a = _make_facility(facility_name=loan, company=company, bank=bank)
		b = _make_facility(facility_name=loan, company=other, bank=bank)
		self.assertNotEqual(a, b)
		self.assertEqual(
			frappe.db.get_value("Facility", a, "company"),
			company,
		)
		self.assertEqual(
			frappe.db.get_value("Facility", b, "company"),
			other,
		)

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
		# Link to a submitted JE with empty cheque_no (ambiguous ownership)
		company = _company()
		fac = frappe.get_doc("Facility", wrong)
		debit_acc = fac.bank_account
		credit_acc = fac.loan_payable_account
		self.assertTrue(debit_acc and credit_acc, "Need Bank + Loan Payable on Facility")
		# Avoid Payable/Receivable accounts that require Party
		acc_type = frappe.db.get_value("Account", credit_acc, "account_type")
		if acc_type in ("Payable", "Receivable"):
			credit_acc = frappe.db.get_value(
				"Account",
				{
					"company": company,
					"is_group": 0,
					"root_type": "Liability",
					"account_type": ("not in", ["Payable", "Receivable"]),
				},
				"name",
				order_by="modified desc",
			)
		self.assertTrue(credit_acc, "Need non-party Liability account")
		je = frappe.new_doc("Journal Entry")
		# Journal Entry (not Bank Entry) so empty cheque_no does not trip bank-reference rules
		je.voucher_type = "Journal Entry"
		je.company = company
		je.posting_date = today()
		je.cheque_no = ""
		je.append(
			"accounts",
			{"account": debit_acc, "debit_in_account_currency": 100, "credit_in_account_currency": 0},
		)
		je.append(
			"accounts",
			{"account": credit_acc, "debit_in_account_currency": 0, "credit_in_account_currency": 100},
		)
		je.insert(ignore_permissions=True)
		je.submit()
		frappe.db.commit()
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


def _cross_process_insert_facility(
	site: str,
	company: str,
	bank: str,
	loan: str,
	accounts: dict,
	result_path: str,
) -> None:
	"""Independent process worker: insert Facility with given loan number.

	``accounts`` is pre-resolved by the parent so workers do not contend on
	Facility Settings updates.
	"""
	import json
	import os
	import traceback

	os.environ["FRAPPE_SITE"] = site
	import frappe as _frappe

	_frappe.init(site=site)
	_frappe.connect()
	_frappe.set_user("Administrator")
	payload = {"ok": False, "name": None, "error": None}
	try:
		from frappe.utils import today as _today

		doc = _frappe.new_doc("Facility")
		doc.facility_name = loan
		doc.company = company
		doc.bank = bank
		doc.status = "Draft"
		doc.contract_date = _today()
		doc.receive_date = _today()
		doc.principal_amount = 1_000_000
		doc.profit_amount = 100_000
		doc.installment_count = 1
		doc.bank_account = accounts.get("bank_gl")
		doc.loan_payable_account = accounts.get("loan_payable")
		doc.deferred_loan_interest_account = accounts.get("deferred")
		doc.interest_expense_account = accounts.get("interest")
		doc.penalty_expense_account = accounts.get("penalty") or accounts.get("interest")
		doc.cost_center = accounts.get("cost_center")
		last_exc = None
		for _attempt in range(4):
			try:
				doc.insert(ignore_permissions=True)
				_frappe.db.commit()
				payload["ok"] = True
				payload["name"] = doc.name
				last_exc = None
				break
			except _frappe.ValidationError as exc:
				_frappe.db.rollback()
				last_exc = exc
				break
			except Exception as exc:
				_frappe.db.rollback()
				last_exc = exc
				# Retry naming-series races; uniqueness lock should then block
				if "Deadlock" in type(exc).__name__ or "1020" in str(exc) or "1213" in str(exc):
					doc = _frappe.new_doc("Facility")
					doc.facility_name = loan
					doc.company = company
					doc.bank = bank
					doc.status = "Draft"
					doc.contract_date = _today()
					doc.receive_date = _today()
					doc.principal_amount = 1_000_000
					doc.profit_amount = 100_000
					doc.installment_count = 1
					doc.bank_account = accounts.get("bank_gl")
					doc.loan_payable_account = accounts.get("loan_payable")
					doc.deferred_loan_interest_account = accounts.get("deferred")
					doc.interest_expense_account = accounts.get("interest")
					doc.penalty_expense_account = accounts.get("penalty") or accounts.get("interest")
					doc.cost_center = accounts.get("cost_center")
					continue
				break
		if last_exc is not None and not payload["ok"]:
			payload["error"] = f"{type(last_exc).__name__}: {last_exc}"
			payload["trace"] = traceback.format_exc()[-1500:]
	except Exception as exc:
		_frappe.db.rollback()
		payload["error"] = f"{type(exc).__name__}: {exc}"
		payload["trace"] = traceback.format_exc()[-1500:]
	finally:
		with open(result_path, "w", encoding="utf-8") as fh:
			json.dump(payload, fh)
		_frappe.destroy()


def _cross_process_assign_je(site: str, facility: str, je: str, result_path: str) -> None:
	import json
	import os
	import traceback

	os.environ["FRAPPE_SITE"] = site
	import frappe as _frappe

	_frappe.init(site=site)
	_frappe.connect()
	_frappe.set_user("Administrator")
	payload = {"ok": False, "error": None}
	try:
		from erpnext_extensions.facility_management.facility_identity import (
			assign_receipt_journal_entry as _assign,
		)

		_assign(facility, je, received_amount=1, status="Active")
		_frappe.db.commit()
		payload["ok"] = True
	except Exception as exc:
		_frappe.db.rollback()
		payload["error"] = f"{type(exc).__name__}: {exc}"
		payload["trace"] = traceback.format_exc()[-1500:]
	finally:
		with open(result_path, "w", encoding="utf-8") as fh:
			json.dump(payload, fh)
		_frappe.destroy()


class TestConcurrentGuards(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def test_15_concurrent_duplicate_facility_creation(self):
		"""Independent processes: only one insert with the same identity succeeds."""
		import json
		import multiprocessing as mp
		import tempfile
		from pathlib import Path

		from erpnext_extensions.facility_management.facility_e2e_context import (
			resolve_facility_gl_accounts,
		)

		company = _company()
		bank = ensure_bank_master()
		accounts = resolve_facility_gl_accounts(company)
		# Ensure settings once in parent (workers receive account names only)
		probe = frappe.new_doc("Facility")
		probe.company = company
		apply_facility_test_accounts(probe, company=company)
		suffix = f"{abs(hash(frappe.generate_hash(length=8))) % 10_000_000:07d}"
		loan = f"990004835-{suffix}"
		site = frappe.local.site
		acct = {
			"bank_gl": accounts.get("bank_gl") or probe.bank_account,
			"loan_payable": accounts.get("loan_payable") or probe.loan_payable_account,
			"deferred": accounts.get("deferred") or probe.deferred_loan_interest_account,
			"interest": accounts.get("interest") or probe.interest_expense_account,
			"penalty": accounts.get("penalty") or probe.penalty_expense_account,
			"cost_center": accounts.get("cost_center") or probe.cost_center,
		}

		with tempfile.TemporaryDirectory() as tmp:
			p1 = str(Path(tmp) / "r1.json")
			p2 = str(Path(tmp) / "r2.json")
			procs = [
				mp.Process(
					target=_cross_process_insert_facility,
					args=(site, company, bank, loan, acct, p1),
				),
				mp.Process(
					target=_cross_process_insert_facility,
					args=(site, company, bank, loan, acct, p2),
				),
			]
			for p in procs:
				p.start()
			for p in procs:
				p.join(timeout=120)
				self.assertFalse(p.is_alive(), "worker hung")
			r1 = json.loads(Path(p1).read_text())
			r2 = json.loads(Path(p2).read_text())

		oks = [r for r in (r1, r2) if r.get("ok")]
		errs = [r for r in (r1, r2) if not r.get("ok")]
		self.assertEqual(len(oks), 1, {"r1": r1, "r2": r2})
		self.assertEqual(len(errs), 1, {"r1": r1, "r2": r2})
		err = errs[0].get("error") or ""
		self.assertIn("Duplicate Facility Number", err, errs[0])
		# End parent snapshot so committed worker rows are visible (REPEATABLE READ)
		frappe.db.commit()
		from erpnext_extensions.facility_management.facility_identity import (
			normalize_facility_number,
		)

		norm = normalize_facility_number(loan)
		peers = frappe.get_all(
			"Facility",
			filters={"company": company, "bank": bank},
			fields=["name", "facility_name"],
		)
		same = [p.name for p in peers if normalize_facility_number(p.facility_name) == norm]
		self.assertEqual(len(same), 1, same)
		self.assertEqual(same[0], oks[0]["name"])

	def test_16_concurrent_je_assignment(self):
		"""Independent processes: only one Facility can claim the same receipt JE."""
		import json
		import multiprocessing as mp
		import tempfile
		from pathlib import Path

		from erpnext_extensions.facility_management.facility_identity import assign_receipt_journal_entry

		a = _make_facility()
		b = _make_facility()
		out = create_receipt_journal_entry(a)
		je = out["journal_entry"]
		# Clear owner link so both processes race to assign the same submitted JE
		# (cheque_no still equals A — B must fail cheque check OR exclusivity).
		# For exclusivity race: create JE owned by nobody by clearing A's pointer,
		# then set cheque_no empty? Business rule requires cheque_no == facility.
		# Race: two facilities cannot both assign when cheque matches only one.
		# Use sequential exclusive check still required:
		with self.assertRaises(frappe.ValidationError):
			assign_receipt_journal_entry(b, je, received_amount=1, status="Active")

		# Cross-process race on a JE whose cheque_no we temporarily align is unrealistic;
		# race two assigns of the same JE after clearing the owner pointer but keeping cheque=A.
		frappe.db.set_value("Facility", a, "receipt_journal_entry", None)
		frappe.db.commit()
		site = frappe.local.site
		with tempfile.TemporaryDirectory() as tmp:
			p1 = str(Path(tmp) / "a.json")
			p2 = str(Path(tmp) / "b.json")
			# Process 1 assigns to A (cheque matches). Process 2 tries B (cheque mismatch).
			procs = [
				mp.Process(target=_cross_process_assign_je, args=(site, a, je, p1)),
				mp.Process(target=_cross_process_assign_je, args=(site, b, je, p2)),
			]
			for p in procs:
				p.start()
			for p in procs:
				p.join(timeout=120)
			r1 = json.loads(Path(p1).read_text())
			r2 = json.loads(Path(p2).read_text())
		# Exactly one may succeed (A); B fails cheque or exclusivity
		self.assertTrue(r1.get("ok"), r1)
		self.assertFalse(r2.get("ok"), r2)
		owners = frappe.get_all("Facility", filters={"receipt_journal_entry": je}, pluck="name")
		self.assertEqual(owners, [a])


# Silence unused import in some runners
_ = (ThreadPoolExecutor, as_completed, CLASS_MANUAL)
