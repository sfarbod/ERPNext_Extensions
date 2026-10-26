# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Unit / integration tests for Facility Debt Purchase PDC allocation (v5.6.20)."""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

import frappe
from frappe.utils import random_string, today

from erpnext_extensions.cheque_management.pdc_workflow_state_machine import WORKFLOW_REGISTERED
from erpnext_extensions.facility_management.facility_e2e_context import (
	apply_facility_test_accounts,
	ensure_bank_master,
)
from erpnext_extensions.facility_management.facility_monetary import parse_facility_amount
from erpnext_extensions.facility_management.facility_pdc_allocation import (
	assert_activation_allocation_equals_facility_total,
	facility_contract_total,
	sum_allocated_cheque_amounts,
)
from erpnext_extensions.facility_management.facility_settings_doc import (
	get_facility_settings_doc,
	resolve_repayment_cost_center,
)
from erpnext_extensions.facility_management.tests.test_facility_debt_purchase_e2e import (
	DebtPurchaseDbFixtureMixin,
)


class TestFacilityPdcAllocation(DebtPurchaseDbFixtureMixin, unittest.TestCase):
	def _draft_facility(self, *, principal: float, profit: float):
		settings = get_facility_settings_doc(self.company)
		bank = frappe.db.get_value("Bank", {}, "name", order_by="creation asc") or ensure_bank_master()
		cc = resolve_repayment_cost_center(facility=None, settings=settings)
		fac = frappe.new_doc("Facility")
		fac.facility_name = f"PDC-ALLOC-{random_string(8)}"
		fac.company = self.company
		fac.facility_type = self.ft
		fac.bank = bank
		fac.contract_date = today()
		fac.principal_amount = principal
		fac.profit_amount = profit
		apply_facility_test_accounts(fac)
		if cc and not fac.cost_center:
			fac.cost_center = cc
		fac.insert(ignore_permissions=True)
		frappe.db.commit()
		return fac

	def test_draft_incomplete_allocation_can_save(self):
		fac = self._draft_facility(principal=1_000_000, profit=100_000)
		pdc = self._new_assigned_pdc(amount=500_000)
		fac.append("debt_purchase_pdcs", {"post_dated_cheque": pdc})
		fac.save(ignore_permissions=True)
		frappe.db.commit()
		self.assertEqual(len(fac.debt_purchase_pdcs), 1)
		self.assertNotEqual(sum_allocated_cheque_amounts(fac), facility_contract_total(fac))

	def test_draft_activation_requires_exact_total(self):
		fac = self._draft_facility(principal=1_000_000, profit=100_000)
		pdc = self._new_assigned_pdc(amount=500_000)
		fac.append("debt_purchase_pdcs", {"post_dated_cheque": pdc})
		fac.save(ignore_permissions=True)
		fac.status = "Active"
		fac.is_opening_facility = 1
		with self.assertRaises(frappe.ValidationError) as ctx:
			fac.save(ignore_permissions=True)
		self.assertIn("فعال‌سازی", str(ctx.exception))
		frappe.db.rollback()

	def test_draft_exact_allocation_can_activate_opening(self):
		fac = self._draft_facility(principal=1_000_000, profit=100_000)
		pdc = self._new_assigned_pdc(amount=1_100_000)
		fac.append("debt_purchase_pdcs", {"post_dated_cheque": pdc})
		fac.save(ignore_permissions=True)
		fac.status = "Active"
		fac.is_opening_facility = 1
		fac.save(ignore_permissions=True)
		frappe.db.commit()
		self.assertEqual(fac.status, "Active")

	def test_duplicate_pdc_same_facility_rejected(self):
		fac = self._draft_facility(principal=1_000_000, profit=0)
		pdc = self._new_assigned_pdc(amount=1_000_000)
		fac.append("debt_purchase_pdcs", {"post_dated_cheque": pdc})
		fac.append("debt_purchase_pdcs", {"post_dated_cheque": pdc})
		with self.assertRaises(frappe.ValidationError) as ctx:
			fac.save(ignore_permissions=True)
		self.assertIn("بیش از یک‌بار", str(ctx.exception))
		frappe.db.rollback()

	def test_same_pdc_two_facilities_rejected(self):
		fac1 = self._draft_facility(principal=1_000_000, profit=0)
		fac2 = self._draft_facility(principal=1_000_000, profit=0)
		pdc = self._new_assigned_pdc(amount=1_000_000)
		fac1.append("debt_purchase_pdcs", {"post_dated_cheque": pdc})
		fac1.save(ignore_permissions=True)
		frappe.db.commit()
		fac2.append("debt_purchase_pdcs", {"post_dated_cheque": pdc})
		with self.assertRaises(frappe.ValidationError) as ctx:
			fac2.save(ignore_permissions=True)
		self.assertIn(fac1.name, str(ctx.exception))
		frappe.db.rollback()

	def test_unique_index_rejects_second_row(self):
		fac1 = self._draft_facility(principal=1_000_000, profit=0)
		pdc = self._new_assigned_pdc(amount=1_000_000)
		fac1.append("debt_purchase_pdcs", {"post_dated_cheque": pdc})
		fac1.save(ignore_permissions=True)
		frappe.db.commit()
		with self.assertRaises(Exception):
			frappe.db.sql(
				"""
				INSERT INTO `tabFacility PDC Allocation`
					(name, creation, modified, modified_by, owner, docstatus,
					 parent, parentfield, parenttype, idx, post_dated_cheque)
				VALUES (%s, NOW(), NOW(), 'Administrator', 'Administrator', 0,
					%s, 'debt_purchase_pdcs', 'Facility', 99, %s)
				""",
				(f"dup-{random_string(8)}", fac1.name, pdc),
			)
			frappe.db.commit()
		frappe.db.rollback()

	def test_irr_exact_comparison(self):
		fac = self._draft_facility(principal=100_000_000_000, profit=5_000_000_000)
		pdc = self._new_assigned_pdc(amount=105_000_000_000)
		fac.append("debt_purchase_pdcs", {"post_dated_cheque": pdc})
		fac.save(ignore_permissions=True)
		frappe.db.commit()
		self.assertEqual(sum_allocated_cheque_amounts(fac), facility_contract_total(fac))
		self.assertEqual(facility_contract_total(fac), Decimal("105000000000"))

	def test_historical_active_empty_allocation_can_edit(self):
		fac = self._draft_facility(principal=1_000_000, profit=0)
		fac.is_opening_facility = 1
		fac.save(ignore_permissions=True)
		frappe.db.set_value("Facility", fac.name, "status", "Active", update_modified=False)
		frappe.db.commit()
		fac = frappe.get_doc("Facility", fac.name)
		self.assertEqual(fac.status, "Active")
		self.assertEqual(len(fac.debt_purchase_pdcs or []), 0)
		fac.remarks = f"hist-edit-{random_string(4)}"
		fac.save(ignore_permissions=True)
		pdc = self._new_assigned_pdc(amount=1_000_000)
		fac.append("debt_purchase_pdcs", {"post_dated_cheque": pdc})
		fac.save(ignore_permissions=True)
		frappe.db.commit()
		self.assertEqual(len(fac.debt_purchase_pdcs), 1)

	def test_non_debt_purchase_rejects_allocation_rows(self):
		non_dp = "E2E Non Debt Purchase Type"
		if not frappe.db.exists("Facility Type", non_dp):
			doc = frappe.new_doc("Facility Type")
			doc.facility_type_name = non_dp
			doc.is_debt_purchase = 0
			doc.insert(ignore_permissions=True)
		else:
			frappe.db.set_value("Facility Type", non_dp, "is_debt_purchase", 0, update_modified=False)
		frappe.db.commit()

		settings = get_facility_settings_doc(self.company)
		bank = frappe.db.get_value("Bank", {}, "name", order_by="creation asc") or ensure_bank_master()
		fac = frappe.new_doc("Facility")
		fac.facility_name = f"NON-DP-{random_string(6)}"
		fac.company = self.company
		fac.facility_type = non_dp
		fac.bank = bank
		fac.contract_date = today()
		fac.principal_amount = 1_000_000
		fac.profit_amount = 0
		apply_facility_test_accounts(fac)
		fac.insert(ignore_permissions=True)
		pdc = self._new_assigned_pdc(amount=1_000_000)
		fac.append("debt_purchase_pdcs", {"post_dated_cheque": pdc})
		with self.assertRaises(frappe.ValidationError) as ctx:
			fac.save(ignore_permissions=True)
		self.assertIn("خرید دین", str(ctx.exception))
		frappe.db.rollback()

	def test_invalid_workflow_rejected(self):
		fac = self._draft_facility(principal=1_000_000, profit=0)
		pdc_name = self._new_assigned_pdc(amount=1_000_000)
		frappe.db.set_value(
			"Post Dated Cheque", pdc_name, {"workflow_state": WORKFLOW_REGISTERED}, update_modified=False
		)
		frappe.db.commit()
		fac.append("debt_purchase_pdcs", {"post_dated_cheque": pdc_name})
		with self.assertRaises(frappe.ValidationError):
			fac.save(ignore_permissions=True)
		frappe.db.rollback()

	def test_cheque_details_refreshed_from_pdc(self):
		fac = self._draft_facility(principal=1_000_000, profit=0)
		pdc_name = self._new_assigned_pdc(amount=1_000_000)
		pdc = frappe.get_doc("Post Dated Cheque", pdc_name)
		fac.append(
			"debt_purchase_pdcs",
			{"post_dated_cheque": pdc_name, "cheque_amount": 1, "cheque_no": "TAMPERED"},
		)
		fac.save(ignore_permissions=True)
		frappe.db.commit()
		row = fac.debt_purchase_pdcs[0]
		self.assertEqual(parse_facility_amount(row.cheque_amount), parse_facility_amount(pdc.cheque_amount))
		self.assertEqual(row.cheque_no, pdc.cheque_no)

	def test_activation_api_cannot_bypass(self):
		fac = self._draft_facility(principal=1_000_000, profit=100_000)
		with self.assertRaises(frappe.ValidationError) as ctx:
			assert_activation_allocation_equals_facility_total(fac)
		self.assertIn("فعال‌سازی", str(ctx.exception))

	def test_repayment_rejects_unallocated_pdc(self):
		from erpnext_extensions.facility_management.facility_debt_purchase import (
			REPAYMENT_METHOD_DEBT_PURCHASE,
			validate_debt_purchase_cheque_repayment,
		)

		pdc_alloc = self._new_assigned_pdc(amount=1_000_000)
		fac_name = self._new_active_facility(
			principal=1_000_000, profit=0, allocated_pdcs=[pdc_alloc]
		)
		fac = frappe.get_doc("Facility", fac_name)
		other = self._new_assigned_pdc(amount=1_000_000)
		repayment = frappe._dict(
			name="FREP-TEST",
			facility=fac.name,
			company=fac.company,
			posting_date=today(),
			principal_amount=1_000_000,
			profit_amount=0,
			penalty_amount=0,
			repayment_method=REPAYMENT_METHOD_DEBT_PURCHASE,
			post_dated_cheque=other,
			currency="IRR",
			docstatus=0,
		)
		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_debt_purchase_cheque_repayment(repayment, facility=fac)
		self.assertIn("تخصیص", str(ctx.exception))

	def test_concurrent_allocation_one_wins(self):
		"""Race two claims for the same PDC via named lock + unique index path."""
		from erpnext_extensions.facility_management.facility_pdc_allocation import (
			_acquire_pdc_allocation_lock,
			_db_owner_of_pdc,
		)

		fac1 = self._draft_facility(principal=1_000_000, profit=0)
		fac2 = self._draft_facility(principal=1_000_000, profit=0)
		pdc = self._new_assigned_pdc(amount=1_000_000)
		frappe.db.sql(
			"DELETE FROM `tabFacility PDC Allocation` WHERE post_dated_cheque = %s",
			(pdc,),
		)
		frappe.db.commit()
		site = frappe.local.site

		def _try_claim(fac_name: str):
			frappe.init(site=site)
			frappe.connect()
			frappe.set_user("Administrator")
			try:
				_acquire_pdc_allocation_lock(pdc)
				other = _db_owner_of_pdc(pdc, exclude_facility=fac_name)
				if other:
					frappe.throw(f"owned by {other}")
				row_name = f"race-{random_string(10)}"
				frappe.db.sql(
					"""
					INSERT INTO `tabFacility PDC Allocation`
						(name, creation, modified, modified_by, owner, docstatus,
						 parent, parentfield, parenttype, idx, post_dated_cheque, cheque_amount)
					VALUES (%s, NOW(), NOW(), 'Administrator', 'Administrator', 0,
						%s, 'debt_purchase_pdcs', 'Facility', 1, %s, 1000000)
					""",
					(row_name, fac_name, pdc),
				)
				frappe.db.commit()
				return ("ok", fac_name)
			except Exception as e:
				frappe.db.rollback()
				return ("err", f"{type(e).__name__}:{e}")
			finally:
				frappe.destroy()

		with ThreadPoolExecutor(max_workers=2) as pool:
			futures = [pool.submit(_try_claim, fac1.name), pool.submit(_try_claim, fac2.name)]
			results = [f.result() for f in as_completed(futures)]
		frappe.connect()
		frappe.set_user("Administrator")
		oks = [r for r in results if r[0] == "ok"]
		errs = [r for r in results if r[0] == "err"]
		self.assertEqual(len(oks), 1, results)
		self.assertEqual(len(errs), 1, results)
		owners = frappe.get_all(
			"Facility PDC Allocation",
			filters={"post_dated_cheque": pdc},
			pluck="parent",
		)
		self.assertEqual(len(owners), 1)

	def test_remove_pdc_used_in_submitted_repayment_blocked(self):
		principal, profit = 900.0, 100.0
		pdc = self._new_assigned_pdc(amount=principal + profit)
		fac_name = self._new_active_facility(
			principal=principal, profit=profit, allocated_pdcs=[pdc]
		)
		# Ensure Facility has dimension required for DP repayment JE (Department).
		fac = frappe.get_doc("Facility", fac_name)
		if not fac.department:
			dept = frappe.db.get_value("Department", {"company": self.company}, "name")
			if dept:
				frappe.db.set_value("Facility", fac_name, "department", dept, update_modified=False)
				frappe.db.commit()
		rep_name = self._new_dp_repayment(fac_name, pdc, principal=principal, profit=profit)
		rep = frappe.get_doc("Facility Repayment", rep_name)
		rep.submit()
		frappe.db.commit()

		fac = frappe.get_doc("Facility", fac_name)
		fac.set("debt_purchase_pdcs", [])
		with self.assertRaises(frappe.ValidationError) as ctx:
			fac.save(ignore_permissions=True)
		self.assertIn("بازپرداخت", str(ctx.exception))
		frappe.db.rollback()
		frappe.get_doc("Facility Repayment", rep_name).cancel()
		frappe.db.commit()


class TestFacilityScheduleFooterHelpers(unittest.TestCase):
	def test_empty_schedule_totals(self):
		rows: list[dict] = []
		principal = sum((parse_facility_amount(r.get("principal_amount")) for r in rows), Decimal("0"))
		profit = sum((parse_facility_amount(r.get("profit_amount")) for r in rows), Decimal("0"))
		self.assertEqual(len(rows), 0)
		self.assertEqual(principal + profit, 0)

	def test_multi_installment_totals(self):
		rows = [
			{"principal_amount": "100", "profit_amount": "10"},
			{"principal_amount": "200", "profit_amount": "20"},
		]
		principal = sum((parse_facility_amount(r["principal_amount"]) for r in rows), Decimal("0"))
		profit = sum((parse_facility_amount(r["profit_amount"]) for r in rows), Decimal("0"))
		self.assertEqual(principal, Decimal("300"))
		self.assertEqual(profit, Decimal("30"))
		self.assertEqual(principal + profit, Decimal("330"))
