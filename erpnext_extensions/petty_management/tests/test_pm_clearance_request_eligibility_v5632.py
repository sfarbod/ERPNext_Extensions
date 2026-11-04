# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.32 — PM Clearance PM Request eligibility (payment_status × status)."""

from __future__ import annotations

import re
import unittest

import frappe
from frappe.exceptions import ValidationError
from frappe.utils import cint, today

from erpnext_extensions.petty_management.services import allocation_service
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.pm_cancel_qa_fixtures import (
	prepare_pi_for_clearance_fixture,
)
from erpnext_extensions.petty_management.tests.test_pm_request_multi_pe import (
	_close_pm_request,
	_create_funding_pe,
	_new_submitted_request,
	_require_site_ready,
	_sync_funding_fields,
)


def _stamp_eligibility(req: str, *, payment_status: str, status: str) -> None:
	frappe.db.set_value(
		"PM Request",
		req,
		{"payment_status": payment_status, "status": status},
		update_modified=False,
	)
	frappe.db.commit()


def _query_names(emp: str, holder: str, petty: str, txt: str = "") -> list[str]:
	rows = allocation_service.pm_request_query_for_pm_clearance(
		"PM Request",
		txt,
		"name",
		0,
		50,
		{
			"employee": emp,
			"company": tpm.COMPANY,
			"holder": holder,
			"petty_cash_account": petty,
		},
	)
	return [r[0] for r in rows]


def _passes(req: str, emp: str, holder: str, petty: str) -> tuple[bool, str]:
	return allocation_service.pm_request_passes_clearance_filters(
		req,
		employee=emp,
		company=tpm.COMPANY,
		holder=holder,
		clearance_petty=petty,
	)


def _reuse_employee_with_holder() -> tuple[str, str, str]:
	"""Avoid creating Employees (site Raven User hook is broken on pinned_workspaces)."""
	rows = frappe.db.sql(
		"""
		select h.employee, h.name as holder, h.petty_cash_account
		from `tabPM Holder` h
		inner join `tabEmployee` e on e.name = h.employee
		where ifnull(h.is_blocked, 0) = 0
		  and ifnull(h.petty_cash_account, '') != ''
		  and h.company = %s
		order by h.modified desc
		limit 20
		""",
		(tpm.COMPANY,),
		as_dict=True,
	)
	if not rows:
		raise unittest.SkipTest("No PM Holder/Employee available for eligibility tests")
	row = rows[0]
	return row.employee, row.holder, row.petty_cash_account


class TestPmClearanceRequestEligibilityV5632(unittest.TestCase):
	def setUp(self):
		_require_site_ready(self)
		frappe.set_user("Administrator")
		self._cleanup: list[tuple[str, str]] = []
		self.emp, self.holder, self.petty = _reuse_employee_with_holder()

	def tearDown(self):
		frappe.set_user("Administrator")
		for doctype, name in reversed(self._cleanup):
			try:
				if not frappe.db.exists(doctype, name):
					continue
				doc = frappe.get_doc(doctype, name)
				if getattr(doc, "docstatus", 0) == 1:
					doc.cancel()
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
			except Exception:
				pass
		frappe.db.commit()

	def _track(self, doctype: str, name: str) -> None:
		self._cleanup.append((doctype, name))

	def _funded(self, requested: float, paid: float | None = None) -> str:
		paid = requested if paid is None else paid
		req = _new_submitted_request(self.emp, requested)
		self._track("PM Request", req)
		pe = _create_funding_pe(req, paid)
		self._track("Payment Entry", pe)
		_sync_funding_fields(req)
		return req

	def _submit_pi(self, amount: float):
		pi = tpm._make_pi_outstanding(amount)
		prepare_pi_for_clearance_fixture(pi)
		self._track("Purchase Invoice", pi.name)
		return pi

	def _assert_combo(self, payment_status: str, status: str, *, expect_ok: bool):
		req = self._funded(50_000, 50_000 if payment_status == "Paid" else 20_000)
		if payment_status == "Partially Paid" and status == "Closed":
			_close_pm_request(req, close_reason="Partial Approval")
			_sync_funding_fields(req)
		_stamp_eligibility(req, payment_status=payment_status, status=status)
		# Ensure PE pointer still valid for Paid/Partially Paid fixtures
		ok, reason = _passes(req, self.emp, self.holder, self.petty)
		in_q = req in _query_names(self.emp, self.holder, self.petty, req)
		if expect_ok:
			self.assertTrue(ok, f"{payment_status}/{status}: validate {reason}")
			self.assertIn(req, _query_names(self.emp, self.holder, self.petty, req), reason)
		else:
			self.assertFalse(ok, f"{payment_status}/{status} should fail validate")
			self.assertFalse(in_q, f"{payment_status}/{status} should be absent from link query")

	def test_01_paid_closed_pass(self):
		self._assert_combo("Paid", "Closed", expect_ok=True)

	def test_02_paid_paid_pass(self):
		self._assert_combo("Paid", "Paid", expect_ok=True)

	def test_03_partially_paid_closed_pass(self):
		self._assert_combo("Partially Paid", "Closed", expect_ok=True)

	def test_04_partially_paid_paid_status_pass(self):
		self._assert_combo("Partially Paid", "Paid", expect_ok=True)

	def test_05_partially_paid_open_block(self):
		# "Open" approximated as Waiting for Payment / non-Closed non-Paid
		self._assert_combo("Partially Paid", "Waiting for Payment", expect_ok=False)

	def test_06_partially_paid_draft_status_block(self):
		self._assert_combo("Partially Paid", "Draft", expect_ok=False)

	def test_07_unpaid_closed_block(self):
		self._assert_combo("Not Paid", "Closed", expect_ok=False)

	def test_08_unpaid_paid_status_block(self):
		self._assert_combo("Not Paid", "Paid", expect_ok=False)

	def test_09_unpaid_open_block(self):
		self._assert_combo("Not Paid", "Waiting for Payment", expect_ok=False)

	def test_10_partial_zero_available_excluded(self):
		req = self._funded(50_000, 20_000)
		_close_pm_request(req, close_reason="Partial Approval")
		_sync_funding_fields(req)
		_stamp_eligibility(req, payment_status="Partially Paid", status="Closed")
		# Consume all paid funding via clearance
		pi = self._submit_pi(20_000)
		cl = frappe.new_doc("PM Clearance")
		cl.company = tpm.COMPANY
		cl.employee = self.emp
		cl.transaction_date = today()
		tpm._append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi.name,
				"allocated_amount": 20_000,
				"outstanding_amount": 20_000,
			},
		)
		cl.append("request_allocations", {"pm_request": req, "allocated_amount": 20_000})
		cl.insert()
		self._track("PM Clearance", cl.name)
		cl.submit()
		tpm._approve_pm_clearance_for_reservation(cl.name)
		avail = allocation_service.get_pm_request_available_amount(req)
		self.assertAlmostEqual(avail, 0.0, places=2)
		ok, _ = _passes(req, self.emp, self.holder, self.petty)
		self.assertFalse(ok)
		self.assertNotIn(req, _query_names(self.emp, self.holder, self.petty, req))

	def test_11_partial_active_reservation_remaining_only(self):
		req = self._funded(100_000, 50_000)
		_close_pm_request(req, close_reason="Partial Approval")
		_sync_funding_fields(req)
		_stamp_eligibility(req, payment_status="Partially Paid", status="Closed")
		pi = self._submit_pi(30_000)
		cl = frappe.new_doc("PM Clearance")
		cl.company = tpm.COMPANY
		cl.employee = self.emp
		cl.transaction_date = today()
		tpm._append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi.name,
				"allocated_amount": 30_000,
				"outstanding_amount": 30_000,
			},
		)
		cl.append("request_allocations", {"pm_request": req, "allocated_amount": 30_000})
		cl.insert()
		self._track("PM Clearance", cl.name)
		avail = allocation_service.get_pm_request_available_amount(req)
		self.assertAlmostEqual(avail, 20_000.0, places=2)
		ok, _ = _passes(req, self.emp, self.holder, self.petty)
		self.assertTrue(ok)
		self.assertIn(req, _query_names(self.emp, self.holder, self.petty, req))

	def test_12_no_submitted_pe_excluded(self):
		req = _new_submitted_request(self.emp, 40_000)
		self._track("PM Request", req)
		_stamp_eligibility(req, payment_status="Partially Paid", status="Closed")
		# Clear payment_entry pointer
		frappe.db.set_value("PM Request", req, "payment_entry", None, update_modified=False)
		frappe.db.commit()
		self.assertNotIn(req, _query_names(self.emp, self.holder, self.petty, req))
		ok, reason = _passes(req, self.emp, self.holder, self.petty)
		self.assertFalse(ok)
		self.assertRegex(reason or "", r"Payment Entry|available|payment", re.I)

	def test_13_cancelled_pe_excluded(self):
		req = self._funded(30_000, 30_000)
		_stamp_eligibility(req, payment_status="Paid", status="Paid")
		pe = frappe.db.get_value("PM Request", req, "payment_entry")
		frappe.get_doc("Payment Entry", pe).cancel()
		_sync_funding_fields(req)
		self.assertNotIn(req, _query_names(self.emp, self.holder, self.petty, req))

	def test_14_draft_pe_excluded(self):
		req = _new_submitted_request(self.emp, 25_000)
		self._track("PM Request", req)
		from erpnext_extensions.petty_management.services.request_service import _build_payment_entry

		pe = _build_payment_entry(frappe.get_doc("PM Request", req), tpm.BANK_ACCOUNT, 25_000)
		pe.insert(ignore_permissions=True)
		self._track("Payment Entry", pe.name)
		frappe.db.set_value(
			"PM Request",
			req,
			{
				"payment_entry": pe.name,
				"payment_status": "Paid",
				"status": "Paid",
			},
			update_modified=False,
		)
		frappe.db.commit()
		self.assertEqual(cint(pe.docstatus), 0)
		self.assertNotIn(req, _query_names(self.emp, self.holder, self.petty, req))

	def test_15_wrong_employee_excluded(self):
		req = self._funded(15_000)
		rows = frappe.db.sql(
			"""
			select h.employee, h.name as holder, h.petty_cash_account
			from `tabPM Holder` h
			where h.company = %s and h.employee != %s and ifnull(h.is_blocked,0)=0
			limit 1
			""",
			(tpm.COMPANY, self.emp),
			as_dict=True,
		)
		if not rows:
			self.skipTest("No second holder for wrong-employee check")
		emp_b, holder_b, petty_b = rows[0].employee, rows[0].holder, rows[0].petty_cash_account
		self.assertNotIn(req, _query_names(emp_b, holder_b, petty_b, req))
		ok, _ = _passes(req, emp_b, holder_b, petty_b)
		self.assertFalse(ok)

	def test_16_blocked_holder_excluded(self):
		req = self._funded(12_000)
		frappe.db.set_value("PM Holder", self.holder, "is_blocked", 1, update_modified=False)
		frappe.db.commit()
		try:
			self.assertNotIn(req, _query_names(self.emp, self.holder, self.petty, req))
		finally:
			frappe.db.set_value("PM Holder", self.holder, "is_blocked", 0, update_modified=False)
			frappe.db.commit()

	def test_17_cancelled_request_excluded(self):
		req = self._funded(10_000)
		frappe.db.set_value("PM Request", req, "docstatus", 2, update_modified=False)
		frappe.db.commit()
		self.assertNotIn(req, _query_names(self.emp, self.holder, self.petty, req))
		ok, reason = _passes(req, self.emp, self.holder, self.petty)
		self.assertFalse(ok)
		self.assertIn("submitted", (reason or "").lower())

	def test_18_over_allocation_blocked(self):
		req = self._funded(63_000, 50_000)
		_close_pm_request(req, close_reason="Partial Approval")
		_sync_funding_fields(req)
		_stamp_eligibility(req, payment_status="Partially Paid", status="Closed")
		avail = allocation_service.get_pm_request_available_amount(req)
		self.assertAlmostEqual(avail, 50_000.0, places=2)
		pi = self._submit_pi(55_000)
		cl = frappe.new_doc("PM Clearance")
		cl.company = tpm.COMPANY
		cl.employee = self.emp
		cl.transaction_date = today()
		tpm._append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi.name,
				"allocated_amount": 55_000,
				"outstanding_amount": 55_000,
			},
		)
		cl.append("request_allocations", {"pm_request": req, "allocated_amount": 55_000})
		with self.assertRaises(ValidationError) as ctx:
			cl.insert()
		self.assertRegex(str(ctx.exception), r"available|exceed", re.I)

	def test_19_allocation_within_paid_ok(self):
		req = self._funded(63_000, 50_000)
		_close_pm_request(req, close_reason="Partial Approval")
		_sync_funding_fields(req)
		_stamp_eligibility(req, payment_status="Partially Paid", status="Closed")
		pi = self._submit_pi(50_000)
		cl = frappe.new_doc("PM Clearance")
		cl.company = tpm.COMPANY
		cl.employee = self.emp
		cl.transaction_date = today()
		tpm._append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi.name,
				"allocated_amount": 50_000,
				"outstanding_amount": 50_000,
			},
		)
		cl.append("request_allocations", {"pm_request": req, "allocated_amount": 50_000})
		cl.insert()
		self._track("PM Clearance", cl.name)
		self.assertAlmostEqual(
			allocation_service.get_pm_request_available_amount(req), 0.0, places=2
		)

	def test_20_fully_paid_behavior_unchanged(self):
		req, _pe = tpm._fund_pm_request(self.emp, 18_000)
		self._track("PM Request", req)
		self.assertIn(req, _query_names(self.emp, self.holder, self.petty, req))
		ok, _ = _passes(req, self.emp, self.holder, self.petty)
		self.assertTrue(ok)


if __name__ == "__main__":
	unittest.main()
