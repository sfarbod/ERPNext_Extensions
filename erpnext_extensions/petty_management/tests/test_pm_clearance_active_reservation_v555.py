# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.5 — active PM Clearance funding reservation + Return zero-available deadlock."""

from __future__ import annotations

import threading
import time
import unittest

import frappe
from frappe.utils import flt, today

from erpnext_extensions.petty_management.services.allocation_service import (
	get_pm_request_available_amount,
	sum_prior_pm_request_allocations,
	validate_request_allocations,
)
from erpnext_extensions.petty_management.services.clearance_reservation import (
	clearance_reserves_pm_request_balance,
	clearance_reserves_pm_request_balance_sql,
)
from erpnext_extensions.petty_management.services.clearance_service import validate_clearance
from erpnext_extensions.petty_management.services.workflow_utils import (
	apply_pm_workflow,
	resolve_workflow_state_link,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as pm_ct
from erpnext_extensions.petty_management.tests.test_pm_pending_remark_edit_v506 import (
	REVIEWER_ROLE,
	_ensure_user,
)


def _wf_title(link: str | None) -> str:
	if not link:
		return ""
	return (frappe.db.get_value("Workflow State", link, "workflow_state_name") or link or "").strip()


def _existing_submitted_pi(min_outstanding: float = 1.0) -> str | None:
	rows = frappe.db.sql(
		"""
		select name from `tabPurchase Invoice`
		where docstatus = 1 and ifnull(outstanding_amount, 0) >= %s
		order by modified desc limit 1
		""",
		(min_outstanding,),
		as_dict=True,
	)
	return rows[0].name if rows else None


class TestPmClearanceActiveReservationV555(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		pm_ct._ensure_company_context()
		if not pm_ct.COMPANY:
			raise unittest.SkipTest("No Company on site")
		from erpnext_extensions.patches.post_model_sync.migrate_pm_workflow_v402 import (
			_rebuild_pm_clearance_workflow,
			_seed_assignment_rules,
		)

		_rebuild_pm_clearance_workflow()
		_seed_assignment_rules()
		frappe.db.commit()

		cls.mgr = "pm_v555_mgr@example.com"
		cls.fin = "pm_v555_fin@example.com"
		cls.holder = "pm_v555_holder@example.com"
		cls.reviewer = "pm_v555_rev@example.com"
		desk = ["Accounts User", "Employee", "Desk User", "System Manager"]
		_ensure_user(cls.mgr, ["Petty Management User", "Expense Approver", *desk])
		_ensure_user(cls.fin, ["Petty Management Accountant", "Petty Management User", *desk])
		_ensure_user(cls.holder, ["Petty Management User", *desk])
		_ensure_user(cls.reviewer, [REVIEWER_ROLE, *desk])

		settings = frappe.get_single("PM Settings")
		settings.db_set("require_named_manager_approver", 1, update_modified=False)
		settings.db_set("finance_manager", cls.fin, update_modified=False)
		settings.db_set("clearance_finance_review_role", REVIEWER_ROLE, update_modified=False)
		settings.db_set("allow_negative_balance", 0, update_modified=False)

	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.flags.pm_return_for_correction = False
		frappe.flags.in_pm_workflow_apply = False

	def _fund(self, amount: float):
		emp = pm_ct._make_employee()
		frappe.db.set_value("Employee", emp, "expense_approver", self.mgr, update_modified=False)
		pm_ct._make_holder(emp)
		req, pe = pm_ct._fund_pm_request(emp, amount)
		return emp, req, pe

	def _pi(self, outstanding: float):
		pi = _existing_submitted_pi()
		if not pi:
			raise unittest.SkipTest("No submitted Purchase Invoice")
		orig = flt(frappe.db.get_value("Purchase Invoice", pi, "outstanding_amount"))
		frappe.db.set_value("Purchase Invoice", pi, "outstanding_amount", outstanding, update_modified=False)
		self.addCleanup(
			lambda: frappe.db.set_value(
				"Purchase Invoice", pi, "outstanding_amount", orig, update_modified=False
			)
		)
		return pi

	def _make_clearance(
		self,
		emp,
		req,
		pi,
		alloc: float,
		*,
		state: str | None = None,
		status: str | None = None,
		docstatus: int = 0,
		force_ignore_validate: bool = False,
	):
		cl = frappe.new_doc("PM Clearance")
		cl.company = pm_ct.COMPANY
		cl.employee = emp
		cl.transaction_date = today()
		pm_ct._append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi,
				"allocated_amount": alloc,
				"outstanding_amount": max(alloc, 1),
			},
		)
		cl.append(
			"request_allocations",
			{"funding_source_type": "PM Request", "pm_request": req, "allocated_amount": alloc},
		)
		cl.flags.ignore_mandatory = True
		if force_ignore_validate:
			cl.flags.ignore_validate = True
		cl.insert(ignore_permissions=True)
		updates = {"owner": self.holder, "manager_approver": self.mgr}
		if state:
			updates["workflow_state"] = resolve_workflow_state_link(state)
		if status:
			updates["status"] = status
		if docstatus:
			updates["docstatus"] = docstatus
		if updates:
			frappe.db.set_value("PM Clearance", cl.name, updates, update_modified=False)
		frappe.db.commit()
		return cl.name

	def test_01_return_when_siblings_consume_full_request(self):
		emp, req, _pe = self._fund(1000)
		pi = self._pi(1000)
		# Pending first (valid), then force-settle sibling that consumes full paid amount.
		pending = self._make_clearance(
			emp, req, pi, 400, state="Pending Finance Review", status="Pending Approval", docstatus=0
		)
		self._make_clearance(
			emp,
			req,
			pi,
			1000,
			state="Approved",
			status="Settled",
			docstatus=1,
			force_ignore_validate=True,
		)
		frappe.db.commit()
		self.assertEqual(flt(get_pm_request_available_amount(req, pending)), 0)
		out = apply_pm_workflow(frappe.get_doc("PM Clearance", pending), "PM Return for Correction")
		self.assertEqual(out.name, pending)
		self.assertEqual(_wf_title(out.workflow_state), "Draft")
		# Returned Draft still reserves (400) + settled (1000)
		self.assertEqual(flt(sum_prior_pm_request_allocations(req, None)), 1400)

	def test_02_finance_approve_blocked_when_zero_available(self):
		emp, req, _pe = self._fund(1000)
		pi = self._pi(1000)
		pending = self._make_clearance(
			emp, req, pi, 100, state="Pending Finance Review", status="Pending Approval", docstatus=0
		)
		self._make_clearance(
			emp,
			req,
			pi,
			1000,
			state="Approved",
			status="Settled",
			docstatus=1,
			force_ignore_validate=True,
		)
		frappe.db.commit()
		with self.assertRaises(frappe.ValidationError) as ctx:
			apply_pm_workflow(frappe.get_doc("PM Clearance", pending), "PM Finance Approve")
		self.assertTrue(
			"available" in str(ctx.exception).lower() or "exceed" in str(ctx.exception).lower()
		)

	def test_03_two_drafts_boundary(self):
		emp, req, _pe = self._fund(1000)
		pi = self._pi(1000)
		a = self._make_clearance(emp, req, pi, 700)
		b_ok = self._make_clearance(emp, req, pi, 300)
		self.assertTrue(a and b_ok)
		with self.assertRaises(frappe.ValidationError):
			self._make_clearance(emp, req, pi, 301)

	def test_04_pending_manager_and_finance_reserve(self):
		emp, req, _pe = self._fund(1000)
		pi = self._pi(1000)
		a = self._make_clearance(
			emp, req, pi, 700, state="Pending Manager Approval", status="Pending Approval"
		)
		self.assertEqual(flt(sum_prior_pm_request_allocations(req, None)), 700)
		with self.assertRaises(frappe.ValidationError):
			self._make_clearance(emp, req, pi, 301)
		frappe.db.set_value(
			"PM Clearance",
			a,
			{
				"workflow_state": resolve_workflow_state_link("Pending Finance Review"),
				"status": "Pending Approval",
			},
			update_modified=False,
		)
		frappe.db.commit()
		with self.assertRaises(frappe.ValidationError):
			self._make_clearance(emp, req, pi, 301)

	def test_05_returned_draft_retains_reservation(self):
		emp, req, _pe = self._fund(1000)
		pi = self._pi(1000)
		a = self._make_clearance(
			emp, req, pi, 700, state="Pending Finance Review", status="Pending Approval"
		)
		b = self._make_clearance(emp, req, pi, 300)
		apply_pm_workflow(frappe.get_doc("PM Clearance", a), "PM Return for Correction")
		self.assertEqual(_wf_title(frappe.db.get_value("PM Clearance", a, "workflow_state")), "Draft")
		self.assertEqual(flt(sum_prior_pm_request_allocations(req, None)), 1000)
		self.assertEqual(flt(get_pm_request_available_amount(req)), 0)
		with self.assertRaises(frappe.ValidationError):
			self._make_clearance(emp, req, pi, 1)

	def test_06_reduce_allocation_frees_headroom(self):
		emp, req, _pe = self._fund(1000)
		pi = self._pi(1000)
		a = self._make_clearance(emp, req, pi, 700)
		with self.assertRaises(frappe.ValidationError):
			self._make_clearance(emp, req, pi, 400)
		doc = frappe.get_doc("PM Clearance", a)
		doc.details[0].allocated_amount = 600
		doc.request_allocations[0].allocated_amount = 600
		doc.save()
		frappe.db.commit()
		b = self._make_clearance(emp, req, pi, 400)
		self.assertTrue(b)

	def test_07_remove_funding_row_releases(self):
		emp, req, _pe = self._fund(1000)
		pi = self._pi(1000)
		a = self._make_clearance(emp, req, pi, 700)
		self.assertEqual(flt(sum_prior_pm_request_allocations(req, None)), 700)
		frappe.db.sql("delete from `tabPM Clearance Request Allocation` where parent=%s", (a,))
		frappe.db.commit()
		self.assertEqual(flt(sum_prior_pm_request_allocations(req, None)), 0)

	def test_08_delete_draft_releases(self):
		emp, req, _pe = self._fund(1000)
		pi = self._pi(1000)
		a = self._make_clearance(emp, req, pi, 700)
		frappe.delete_doc("PM Clearance", a, force=1, ignore_permissions=True)
		frappe.db.commit()
		self.assertEqual(flt(sum_prior_pm_request_allocations(req, None)), 0)

	def test_09_reject_releases(self):
		emp, req, _pe = self._fund(1000)
		pi = self._pi(1000)
		a = self._make_clearance(
			emp, req, pi, 700, state="Pending Finance Review", status="Pending Approval"
		)
		frappe.db.set_value(
			"PM Clearance",
			a,
			{
				"workflow_state": resolve_workflow_state_link("Rejected"),
				"status": "Rejected",
				"docstatus": 1,
			},
			update_modified=False,
		)
		frappe.db.commit()
		self.assertFalse(
			clearance_reserves_pm_request_balance(frappe.db.get_value("PM Clearance", a, ["docstatus", "status"], as_dict=True))
		)
		self.assertEqual(flt(sum_prior_pm_request_allocations(req, None)), 0)

	def test_10_cancel_releases(self):
		emp, req, _pe = self._fund(1000)
		pi = self._pi(1000)
		a = self._make_clearance(emp, req, pi, 700, state="Approved", status="Approved", docstatus=1)
		self.assertEqual(flt(sum_prior_pm_request_allocations(req, None)), 700)
		frappe.db.set_value(
			"PM Clearance",
			a,
			{"docstatus": 2, "status": "Cancelled"},
			update_modified=False,
		)
		frappe.db.commit()
		self.assertEqual(flt(sum_prior_pm_request_allocations(req, None)), 0)

	def test_11_self_exclusion_no_double_count(self):
		emp, req, _pe = self._fund(1000)
		pi = self._pi(1000)
		a = self._make_clearance(emp, req, pi, 700)
		self.assertEqual(flt(sum_prior_pm_request_allocations(req, a)), 0)
		self.assertEqual(flt(get_pm_request_available_amount(req, a)), 1000)
		doc = frappe.get_doc("PM Clearance", a)
		doc.remark = (doc.remark or "") + " self-excl"
		doc.save()
		frappe.db.commit()
		self.assertEqual(flt(sum_prior_pm_request_allocations(req, None)), 700)

	def test_12_increase_existing_allocation_boundary(self):
		emp, req, _pe = self._fund(1000)
		pi = self._pi(1000)
		a = self._make_clearance(emp, req, pi, 600)
		b = self._make_clearance(emp, req, pi, 300)
		doc = frappe.get_doc("PM Clearance", a)
		doc.details[0].allocated_amount = 700
		doc.request_allocations[0].allocated_amount = 700
		doc.save()
		frappe.db.commit()
		doc = frappe.get_doc("PM Clearance", a)
		doc.details[0].allocated_amount = 701
		doc.request_allocations[0].allocated_amount = 701
		with self.assertRaises(frappe.ValidationError):
			doc.save()

	def test_13_multiple_requests_independent(self):
		emp, r1, _ = self._fund(1000)
		r2, _pe2 = pm_ct._fund_pm_request(emp, 500)
		pi = self._pi(1000)
		a = frappe.new_doc("PM Clearance")
		a.company = pm_ct.COMPANY
		a.employee = emp
		a.transaction_date = today()
		pm_ct._append_pm_clearance_detail_row(
			a,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi,
				"allocated_amount": 900,
				"outstanding_amount": 1000,
			},
		)
		a.append(
			"request_allocations",
			{"funding_source_type": "PM Request", "pm_request": r1, "allocated_amount": 700},
		)
		a.append(
			"request_allocations",
			{"funding_source_type": "PM Request", "pm_request": r2, "allocated_amount": 200},
		)
		a.flags.ignore_mandatory = True
		a.insert(ignore_permissions=True)
		frappe.db.commit()
		self.assertEqual(flt(sum_prior_pm_request_allocations(r1, None)), 700)
		self.assertEqual(flt(sum_prior_pm_request_allocations(r2, None)), 200)
		with self.assertRaises(frappe.ValidationError):
			self._make_clearance(emp, r1, pi, 301)
		ok = self._make_clearance(emp, r2, pi, 300)
		self.assertTrue(ok)

	def test_14_structural_still_blocked_on_return(self):
		emp, req, _pe = self._fund(1000)
		pi = self._pi(1000)
		pending = self._make_clearance(
			emp, req, pi, 100, state="Pending Finance Review", status="Pending Approval"
		)
		self._make_clearance(
			emp,
			req,
			pi,
			1000,
			state="Approved",
			status="Settled",
			docstatus=1,
			force_ignore_validate=True,
		)
		frappe.db.sql(
			"update `tabPM Clearance Request Allocation` set pm_request=null where parent=%s",
			(pending,),
		)
		frappe.db.commit()
		frappe.flags.pm_return_for_correction = True
		frappe.flags.in_pm_workflow_apply = True
		try:
			with self.assertRaises(frappe.ValidationError):
				validate_request_allocations(frappe.get_doc("PM Clearance", pending))
		finally:
			frappe.flags.pm_return_for_correction = False
			frappe.flags.in_pm_workflow_apply = False

	def test_17_repeated_return_reservation_stable(self):
		emp, req, _pe = self._fund(1000)
		pi = self._pi(1000)
		a = self._make_clearance(
			emp, req, pi, 700, state="Pending Finance Review", status="Pending Approval"
		)
		b = self._make_clearance(emp, req, pi, 300)
		for _ in range(2):
			apply_pm_workflow(frappe.get_doc("PM Clearance", a), "PM Return for Correction")
			self.assertEqual(flt(sum_prior_pm_request_allocations(req, None)), 1000)
			frappe.db.set_value(
				"PM Clearance",
				a,
				{
					"workflow_state": resolve_workflow_state_link("Pending Finance Review"),
					"status": "Pending Approval",
				},
				update_modified=False,
			)
			frappe.db.commit()
		self.assertEqual(flt(sum_prior_pm_request_allocations(req, None)), 1000)

	def test_predicate_sql_matches_python(self):
		self.assertIn("docstatus", clearance_reserves_pm_request_balance_sql("p"))
		self.assertIn("Cancelled", clearance_reserves_pm_request_balance_sql("p"))
		self.assertTrue(clearance_reserves_pm_request_balance({"docstatus": 0, "status": "Draft"}))
		self.assertTrue(
			clearance_reserves_pm_request_balance({"docstatus": 0, "status": "Pending Approval"})
		)
		self.assertTrue(clearance_reserves_pm_request_balance({"docstatus": 1, "status": "Settled"}))
		self.assertFalse(clearance_reserves_pm_request_balance({"docstatus": 1, "status": "Rejected"}))
		self.assertFalse(clearance_reserves_pm_request_balance({"docstatus": 2, "status": "Cancelled"}))


class TestPmClearanceReservationConcurrencyV555(unittest.TestCase):
	"""Real multi-connection concurrency — only one of 700+700 against 1000 may commit."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		pm_ct._ensure_company_context()
		if not pm_ct.COMPANY:
			raise unittest.SkipTest("No Company")

	def test_concurrent_700_700_only_one_succeeds(self):
		frappe.set_user("Administrator")
		emp = pm_ct._make_employee()
		pm_ct._make_holder(emp)
		req, _pe = pm_ct._fund_pm_request(emp, 1000)
		pi = _existing_submitted_pi()
		if not pi:
			raise unittest.SkipTest("No PI")
		orig = flt(frappe.db.get_value("Purchase Invoice", pi, "outstanding_amount"))
		frappe.db.set_value("Purchase Invoice", pi, "outstanding_amount", 1000, update_modified=False)
		frappe.db.commit()

		site = frappe.local.site
		results: list[dict] = []
		barrier = threading.Barrier(2, timeout=30)

		def worker(alloc: float, label: str):
			import frappe as _frappe

			_frappe.init(site=site)
			_frappe.connect()
			_frappe.set_user("Administrator")
			err = None
			name = None
			try:
				barrier.wait()
				# Named lock BEFORE begin/insert so peers fully commit before we read.
				from erpnext_extensions.petty_management.services.clearance_reservation import (
					lock_pm_requests_for_allocation,
				)

				lock_pm_requests_for_allocation([req])
				_frappe.db.begin()
				cl = _frappe.new_doc("PM Clearance")
				cl.company = pm_ct.COMPANY
				cl.employee = emp
				cl.transaction_date = today()
				pm_ct._append_pm_clearance_detail_row(
					cl,
					{
						"settlement_type": "Purchase Invoice",
						"purchase_invoice": pi,
						"allocated_amount": alloc,
						"outstanding_amount": 1000,
					},
				)
				cl.append(
					"request_allocations",
					{
						"funding_source_type": "PM Request",
						"pm_request": req,
						"allocated_amount": alloc,
					},
				)
				cl.flags.ignore_mandatory = True
				cl.insert(ignore_permissions=True)
				_frappe.db.commit()
				name = cl.name
				ok = True
			except Exception as e:
				_frappe.db.rollback()
				ok = False
				err = str(e)[:300]
			results.append({"label": label, "ok": ok, "name": name, "err": err})
			_frappe.destroy()

		t1 = threading.Thread(target=worker, args=(700, "A"))
		t2 = threading.Thread(target=worker, args=(700, "B"))
		t1.start()
		t2.start()
		t1.join(60)
		t2.join(60)

		frappe.connect()
		frappe.set_user("Administrator")
		ok_count = sum(1 for r in results if r["ok"])
		prior = flt(sum_prior_pm_request_allocations(req, None))
		frappe.db.set_value("Purchase Invoice", pi, "outstanding_amount", orig, update_modified=False)
		frappe.db.commit()

		self.assertEqual(len(results), 2, results)
		self.assertEqual(ok_count, 1, results)
		self.assertLessEqual(prior, 1000 + 1e-6, f"prior={prior} results={results}")
		self.assertGreaterEqual(prior, 700 - 1e-6)

	def test_concurrent_700_300_both_may_succeed(self):
		frappe.set_user("Administrator")
		emp = pm_ct._make_employee()
		pm_ct._make_holder(emp)
		req, _pe = pm_ct._fund_pm_request(emp, 1000)
		pi = _existing_submitted_pi()
		if not pi:
			raise unittest.SkipTest("No PI")
		orig = flt(frappe.db.get_value("Purchase Invoice", pi, "outstanding_amount"))
		frappe.db.set_value("Purchase Invoice", pi, "outstanding_amount", 1000, update_modified=False)
		frappe.db.commit()

		site = frappe.local.site
		results: list[dict] = []
		barrier = threading.Barrier(2, timeout=30)

		def worker(alloc: float, label: str):
			import frappe as _frappe

			_frappe.init(site=site)
			_frappe.connect()
			_frappe.set_user("Administrator")
			err = None
			name = None
			try:
				barrier.wait()
				from erpnext_extensions.petty_management.services.clearance_reservation import (
					lock_pm_requests_for_allocation,
				)

				lock_pm_requests_for_allocation([req])
				_frappe.db.begin()
				cl = _frappe.new_doc("PM Clearance")
				cl.company = pm_ct.COMPANY
				cl.employee = emp
				cl.transaction_date = today()
				pm_ct._append_pm_clearance_detail_row(
					cl,
					{
						"settlement_type": "Purchase Invoice",
						"purchase_invoice": pi,
						"allocated_amount": alloc,
						"outstanding_amount": 1000,
					},
				)
				cl.append(
					"request_allocations",
					{
						"funding_source_type": "PM Request",
						"pm_request": req,
						"allocated_amount": alloc,
					},
				)
				cl.flags.ignore_mandatory = True
				cl.insert(ignore_permissions=True)
				_frappe.db.commit()
				name = cl.name
				ok = True
			except Exception as e:
				_frappe.db.rollback()
				ok = False
				err = str(e)[:300]
			results.append({"label": label, "ok": ok, "name": name, "err": err, "alloc": alloc})
			_frappe.destroy()

		t1 = threading.Thread(target=worker, args=(700, "A"))
		t2 = threading.Thread(target=worker, args=(300, "B"))
		t1.start()
		t2.start()
		t1.join(60)
		t2.join(60)

		frappe.connect()
		frappe.set_user("Administrator")
		ok_count = sum(1 for r in results if r["ok"])
		prior = flt(sum_prior_pm_request_allocations(req, None))
		frappe.db.set_value("Purchase Invoice", pi, "outstanding_amount", orig, update_modified=False)
		frappe.db.commit()

		self.assertEqual(ok_count, 2, results)
		self.assertAlmostEqual(prior, 1000.0, places=2)
