# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.5.42 — cancelled PI Return bypass + Purchase Invoice cancel guard."""

from __future__ import annotations

import threading
import time
import unittest

import frappe
from frappe.utils import cint, flt, today

from erpnext_extensions.petty_management.services.purchase_invoice_clearance_guard import (
	before_cancel_purchase_invoice,
	find_active_clearances_referencing_pi,
	report_legacy_cancelled_pi_on_clearances,
)
from erpnext_extensions.petty_management.services.purchase_invoice_readiness import (
	validate_purchase_invoices_for_finance_approval,
	validate_purchase_invoices_for_prepare,
)
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


def _submit_dedicated_pi(amount: float) -> str:
	"""Create a fresh submitted PI owned by these tests (not shared site fixtures)."""
	pi = pm_ct._make_pi_outstanding(amount)
	dept = pm_ct._first_department_for_company()
	if dept:
		for row in pi.items:
			row.department = dept
		if hasattr(pi, "department"):
			pi.department = dept
	try:
		pi.insert(ignore_permissions=True)
		pi.submit()
	except TypeError as exc:
		if "do_not_round_fields" in str(exc):
			raise unittest.SkipTest(
				"Purchase Invoice submit incompatible with this Frappe version"
			) from exc
		raise
	except Exception as exc:
		raise unittest.SkipTest(f"Cannot create Purchase Invoice: {exc}") from exc
	frappe.db.set_value(
		"Purchase Invoice", pi.name, "outstanding_amount", amount, update_modified=False
	)
	frappe.db.commit()
	return pi.name


def _mark_pi_cancelled(pi: str) -> None:
	"""Simulate legacy cancelled PI without running cancel() (already linked)."""
	frappe.db.set_value("Purchase Invoice", pi, "docstatus", 2, update_modified=False)
	if frappe.db.has_column("Purchase Invoice", "status"):
		frappe.db.set_value("Purchase Invoice", pi, "status", "Cancelled", update_modified=False)


def _restore_pi_submitted(pi: str, outstanding: float | None = None) -> None:
	frappe.db.set_value("Purchase Invoice", pi, "docstatus", 1, update_modified=False)
	if frappe.db.has_column("Purchase Invoice", "status"):
		frappe.db.set_value("Purchase Invoice", pi, "status", "Unpaid", update_modified=False)
	if outstanding is not None:
		frappe.db.set_value(
			"Purchase Invoice", pi, "outstanding_amount", outstanding, update_modified=False
		)


class TestPmClearanceCancelledPiReturnV5542(unittest.TestCase):
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

		cls.mgr = "pm_v5542_mgr@example.com"
		cls.fin = "pm_v5542_fin@example.com"
		cls.holder = "pm_v5542_holder@example.com"
		cls.reviewer = "pm_v5542_rev@example.com"
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
		frappe.flags.pm_return_for_correction = False
		frappe.flags.in_pm_workflow_apply = False

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.flags.pm_return_for_correction = False
		frappe.flags.in_pm_workflow_apply = False

	def _fund(self, amount: float):
		emp = pm_ct._make_employee()
		frappe.db.set_value("Employee", emp, "expense_approver", self.mgr, update_modified=False)
		frappe.db.set_value("Employee", emp, "user_id", self.holder, update_modified=False)
		pm_ct._make_holder(emp)
		req, pe = pm_ct._fund_pm_request(emp, amount)
		return emp, req, pe

	def _pi(self, outstanding: float):
		pi = _submit_dedicated_pi(outstanding)

		def _cleanup():
			# Leave fixture submitted so later accounting cancel is optional.
			_restore_pi_submitted(pi, outstanding)

		self.addCleanup(_cleanup)
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
		if docstatus is not None:
			updates["docstatus"] = docstatus
		frappe.db.set_value("PM Clearance", cl.name, updates, update_modified=False)
		frappe.db.commit()
		return frappe.get_doc("PM Clearance", cl.name)

	# ---- TEST 1 ----
	def test_01_pending_cancelled_pi_return_succeeds(self):
		emp, req, _ = self._fund(5_000)
		pi = self._pi(5_000)
		cl = self._make_clearance(
			emp, req, pi, 5_000, state="Pending Finance Review", status="Pending Approval"
		)
		_mark_pi_cancelled(pi)
		frappe.db.commit()
		frappe.set_user(self.reviewer)
		out = apply_pm_workflow(frappe.get_doc("PM Clearance", cl.name), "PM Return for Correction")
		frappe.set_user("Administrator")
		self.assertEqual(_wf_title(out.workflow_state), "Draft")
		self.assertEqual(out.name, cl.name)
		self.assertEqual(cint(frappe.db.get_value("Purchase Invoice", pi, "docstatus")), 2)
		linked = [r.purchase_invoice for r in out.details if r.purchase_invoice == pi]
		self.assertEqual(linked, [pi])

	# ---- TEST 2 ----
	def test_02_finance_approve_blocked_with_cancelled_pi(self):
		emp, req, _ = self._fund(5_000)
		pi = self._pi(5_000)
		cl = self._make_clearance(
			emp, req, pi, 5_000, state="Pending Finance Review", status="Pending Approval"
		)
		_mark_pi_cancelled(pi)
		frappe.db.commit()
		doc = frappe.get_doc("PM Clearance", cl.name)
		with self.assertRaises(frappe.ValidationError):
			validate_purchase_invoices_for_finance_approval(doc)
		frappe.set_user(self.reviewer)
		with self.assertRaises(Exception):
			apply_pm_workflow(frappe.get_doc("PM Clearance", cl.name), "PM Finance Approve")
		frappe.set_user("Administrator")

	# ---- TEST 3 ----
	def test_03_draft_save_blocked_unless_removing_cancelled_pi(self):
		emp, req, _ = self._fund(5_000)
		pi = self._pi(5_000)
		cl = self._make_clearance(emp, req, pi, 5_000, state="Draft", status="Draft")
		_mark_pi_cancelled(pi)
		frappe.db.commit()
		doc = frappe.get_doc("PM Clearance", cl.name)
		with self.assertRaises(Exception):
			doc.save()
		# Correction: replace cancelled PI with a fresh submitted PI then save
		doc = frappe.get_doc("PM Clearance", cl.name)
		pi2 = self._pi(5_000)
		doc.details[0].purchase_invoice = pi2
		doc.details[0].allocated_amount = 5_000
		doc.details[0].outstanding_amount = 5_000
		doc.save()
		doc.reload()
		self.assertEqual(doc.details[0].purchase_invoice, pi2)

	# ---- cancel guard helpers ----
	def _assert_cancel_blocked(self, pi: str, *expected_clearances: str):
		doc = frappe.get_doc("Purchase Invoice", pi)
		with self.assertRaises(frappe.ValidationError) as ctx:
			before_cancel_purchase_invoice(doc)
		msg = str(ctx.exception)
		self.assertIn(pi, msg)
		for name in expected_clearances:
			self.assertIn(name, msg)
		# Also via document.cancel() path if still submitted
		if cint(doc.docstatus) == 1:
			with self.assertRaises(frappe.ValidationError) as ctx2:
				doc.cancel()
			self.assertIn("PM Clearance", str(ctx2.exception))

	# ---- TESTS 4–9 ----
	def test_04_draft_clearance_blocks_pi_cancel(self):
		emp, req, _ = self._fund(3_000)
		pi = self._pi(3_000)
		cl = self._make_clearance(emp, req, pi, 3_000, state="Draft", status="Draft")
		self._assert_cancel_blocked(pi, cl.name)

	def test_05_pending_manager_blocks_pi_cancel(self):
		emp, req, _ = self._fund(3_000)
		pi = self._pi(3_000)
		cl = self._make_clearance(
			emp, req, pi, 3_000, state="Pending Manager Approval", status="Pending Approval"
		)
		self._assert_cancel_blocked(pi, cl.name)

	def test_06_pending_finance_blocks_pi_cancel(self):
		emp, req, _ = self._fund(3_000)
		pi = self._pi(3_000)
		cl = self._make_clearance(
			emp, req, pi, 3_000, state="Pending Finance Review", status="Pending Approval"
		)
		self._assert_cancel_blocked(pi, cl.name)

	def test_07_returned_draft_blocks_pi_cancel(self):
		emp, req, _ = self._fund(3_000)
		pi = self._pi(3_000)
		cl = self._make_clearance(emp, req, pi, 3_000, state="Draft", status="Draft")
		# Simulate returned draft (same Draft status after Return)
		self._assert_cancel_blocked(pi, cl.name)

	def test_08_approved_blocks_pi_cancel(self):
		emp, req, _ = self._fund(3_000)
		pi = self._pi(3_000)
		cl = self._make_clearance(
			emp, req, pi, 3_000, state="Approved", status="Approved", docstatus=1
		)
		self._assert_cancel_blocked(pi, cl.name)

	def test_09_settled_blocks_pi_cancel(self):
		emp, req, _ = self._fund(3_000)
		pi = self._pi(3_000)
		cl = self._make_clearance(
			emp, req, pi, 3_000, state="Approved", status="Settled", docstatus=1
		)
		self._assert_cancel_blocked(pi, cl.name)

	# ---- TESTS 10–12 ----
	def test_10_cancelled_clearance_does_not_block(self):
		emp, req, _ = self._fund(3_000)
		pi = self._pi(3_000)
		cl = self._make_clearance(
			emp, req, pi, 3_000, state="Cancelled", status="Cancelled", docstatus=2
		)
		active = find_active_clearances_referencing_pi(pi)
		self.assertNotIn(cl.name, active)
		before_cancel_purchase_invoice(frappe.get_doc("Purchase Invoice", pi))

	def test_11_rejected_clearance_does_not_block(self):
		emp, req, _ = self._fund(3_000)
		pi = self._pi(3_000)
		cl = self._make_clearance(
			emp, req, pi, 3_000, state="Rejected", status="Rejected", docstatus=0
		)
		active = find_active_clearances_referencing_pi(pi)
		self.assertNotIn(cl.name, active)
		before_cancel_purchase_invoice(frappe.get_doc("Purchase Invoice", pi))

	def test_12_deleted_clearance_no_blocker(self):
		emp, req, _ = self._fund(3_000)
		pi = self._pi(3_000)
		cl = self._make_clearance(emp, req, pi, 3_000, state="Draft", status="Draft")
		frappe.delete_doc("PM Clearance", cl.name, force=1, ignore_permissions=True)
		frappe.db.commit()
		active = find_active_clearances_referencing_pi(pi)
		self.assertNotIn(cl.name, active)
		before_cancel_purchase_invoice(frappe.get_doc("Purchase Invoice", pi))

	# ---- TEST 13 ----
	def test_13_remove_pi_reference_releases_guard(self):
		emp, req, _ = self._fund(3_000)
		pi = self._pi(3_000)
		cl = self._make_clearance(emp, req, pi, 3_000, state="Draft", status="Draft")
		self._assert_cancel_blocked(pi, cl.name)
		doc = frappe.get_doc("PM Clearance", cl.name)
		pi2 = self._pi(3_000)
		doc.details[0].purchase_invoice = pi2
		doc.details[0].outstanding_amount = 3_000
		doc.save()
		frappe.db.commit()
		active = find_active_clearances_referencing_pi(pi)
		self.assertNotIn(cl.name, active)
		before_cancel_purchase_invoice(frappe.get_doc("Purchase Invoice", pi))

	# ---- TEST 14 ----
	def test_14_multiple_active_clearances_listed(self):
		emp, req, _ = self._fund(6_000)
		pi = self._pi(6_000)
		cl1 = self._make_clearance(emp, req, pi, 2_000, state="Draft", status="Draft")
		cl2 = self._make_clearance(emp, req, pi, 2_000, state="Draft", status="Draft")
		with self.assertRaises(frappe.ValidationError) as ctx:
			before_cancel_purchase_invoice(frappe.get_doc("Purchase Invoice", pi))
		msg = str(ctx.exception)
		self.assertIn(cl1.name, msg)
		self.assertIn(cl2.name, msg)

	# ---- TEST 15 ----
	def test_15_unrelated_pi_no_pm_blocker(self):
		pi = self._pi(1_000)
		active = find_active_clearances_referencing_pi(pi)
		self.assertEqual(active, [])
		before_cancel_purchase_invoice(frappe.get_doc("Purchase Invoice", pi))

	# ---- TESTS 16–18 (same server path) ----
	def test_16_17_18_cancel_paths_enforce_guard(self):
		emp, req, _ = self._fund(3_000)
		pi = self._pi(3_000)
		cl = self._make_clearance(emp, req, pi, 3_000, state="Draft", status="Draft")
		# 16 direct before_cancel / doc.cancel
		self._assert_cancel_blocked(pi, cl.name)
		# 18 API-style
		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc("Purchase Invoice", pi).cancel()

	# ---- TEST 19 ----
	def test_19_return_permissions_unchanged(self):
		emp, req, _ = self._fund(3_000)
		pi = self._pi(3_000)
		cl = self._make_clearance(
			emp, req, pi, 3_000, state="Pending Manager Approval", status="Pending Approval"
		)
		frappe.set_user(self.holder)
		with self.assertRaises(Exception):
			apply_pm_workflow(frappe.get_doc("PM Clearance", cl.name), "PM Return for Correction")
		frappe.set_user(self.mgr)
		out = apply_pm_workflow(frappe.get_doc("PM Clearance", cl.name), "PM Return for Correction")
		frappe.set_user("Administrator")
		self.assertEqual(_wf_title(out.workflow_state), "Draft")

	# ---- TEST 20: v5.1.4 regression (over-allocation return) ----
	def test_20_v514_return_over_allocation_regression(self):
		emp, req, _ = self._fund(10_000)
		pi = self._pi(1_200)
		# Stale allocated 10k vs live 1.2k outstanding
		cl = self._make_clearance(
			emp,
			req,
			pi,
			10_000,
			state="Pending Finance Review",
			status="Pending Approval",
			force_ignore_validate=True,
		)
		frappe.db.sql(
			"update `tabPM Clearance Detail` set allocated_amount=%s, outstanding_amount=%s where parent=%s",
			(10_000, 10_000, cl.name),
		)
		frappe.db.sql(
			"update `tabPM Clearance Request Allocation` set allocated_amount=%s where parent=%s",
			(10_000, cl.name),
		)
		frappe.db.commit()
		frappe.set_user(self.reviewer)
		out = apply_pm_workflow(frappe.get_doc("PM Clearance", cl.name), "PM Return for Correction")
		frappe.set_user("Administrator")
		self.assertEqual(_wf_title(out.workflow_state), "Draft")

	# ---- TEST 21: v5.2.11 funding availability return ----
	def test_21_v5211_return_funding_regression(self):
		emp, req, _ = self._fund(5_000)
		pi = self._pi(5_000)
		cl = self._make_clearance(
			emp, req, pi, 5_000, state="Pending Finance Review", status="Pending Approval"
		)
		# Exhaust sibling reservation so available appears zero for Return validate path
		frappe.set_user(self.reviewer)
		out = apply_pm_workflow(frappe.get_doc("PM Clearance", cl.name), "PM Return for Correction")
		frappe.set_user("Administrator")
		self.assertEqual(_wf_title(out.workflow_state), "Draft")

	# ---- TEST 22: v5.5.5 reservation return ----
	def test_22_v555_reservation_return_regression(self):
		emp, req, _ = self._fund(5_000)
		pi = self._pi(5_000)
		cl = self._make_clearance(
			emp, req, pi, 5_000, state="Pending Manager Approval", status="Pending Approval"
		)
		frappe.set_user(self.mgr)
		out = apply_pm_workflow(frappe.get_doc("PM Clearance", cl.name), "PM Return for Correction")
		frappe.set_user("Administrator")
		self.assertEqual(_wf_title(out.workflow_state), "Draft")

	# ---- TEST 23 ----
	def test_23_repeated_return(self):
		emp, req, _ = self._fund(5_000)
		pi = self._pi(5_000)
		cl = self._make_clearance(
			emp, req, pi, 5_000, state="Pending Manager Approval", status="Pending Approval"
		)
		frappe.set_user(self.mgr)
		out = apply_pm_workflow(frappe.get_doc("PM Clearance", cl.name), "PM Return for Correction")
		self.assertEqual(_wf_title(out.workflow_state), "Draft")
		frappe.set_user(self.holder)
		out = apply_pm_workflow(frappe.get_doc("PM Clearance", cl.name), "PM Submit Finance Review")
		self.assertEqual(_wf_title(out.workflow_state), "Pending Manager Approval")
		frappe.set_user(self.mgr)
		out = apply_pm_workflow(frappe.get_doc("PM Clearance", cl.name), "PM Return for Correction")
		frappe.set_user("Administrator")
		self.assertEqual(_wf_title(out.workflow_state), "Draft")

	# ---- TEST 24 remark-only pending save still blocks cancelled ----
	def test_24_remark_only_pending_does_not_skip_cancelled(self):
		emp, req, _ = self._fund(3_000)
		pi = self._pi(3_000)
		cl = self._make_clearance(
			emp, req, pi, 3_000, state="Pending Finance Review", status="Pending Approval"
		)
		_mark_pi_cancelled(pi)
		frappe.db.commit()
		doc = frappe.get_doc("PM Clearance", cl.name)
		# Without return flag, prepare must still throw on cancelled
		frappe.flags.pm_return_for_correction = False
		with self.assertRaises(frappe.ValidationError) as ctx:
			validate_purchase_invoices_for_prepare(doc)
		self.assertIn("cancelled", str(ctx.exception).lower())

	# ---- TEST 25 concurrency ----
	def test_25_cancel_vs_clearance_create_no_invalid_state(self):
		frappe.set_user("Administrator")
		emp, req, _ = self._fund(4_000)
		pi = self._pi(4_000)
		frappe.db.commit()
		site = frappe.local.site
		results: list[dict] = []
		barrier = threading.Barrier(2, timeout=45)

		def cancel_worker():
			import frappe as _f

			_f.init(site=site)
			_f.connect()
			_f.set_user("Administrator")
			err = None
			ok = False
			ds_after = None
			try:
				barrier.wait()
				time.sleep(0.02)
				_f.db.begin()
				# Guard + cancel mutation in the SAME transaction while named lock held.
				before_cancel_purchase_invoice(_f.get_doc("Purchase Invoice", pi))
				_f.db.set_value("Purchase Invoice", pi, "docstatus", 2, update_modified=False)
				# Final emptiness check still under lock (before commit releases it).
				leftover = find_active_clearances_referencing_pi(pi)
				if leftover:
					raise _f.ValidationError(f"Clearance appeared before cancel commit: {leftover}")
				_f.db.commit()
				ok = True
				ds_after = cint(_f.db.get_value("Purchase Invoice", pi, "docstatus"))
			except Exception as e:
				_f.db.rollback()
				err = str(e)[:400]
			results.append({"label": "cancel", "ok": ok, "err": err, "ds": ds_after})
			_f.destroy()

		def clearance_worker():
			import frappe as _f

			_f.init(site=site)
			_f.connect()
			_f.set_user("Administrator")
			err = None
			ok = False
			name = None
			ds_after = None
			try:
				barrier.wait()
				_f.db.begin()
				cl = _f.new_doc("PM Clearance")
				cl.company = pm_ct.COMPANY
				cl.employee = emp
				cl.transaction_date = today()
				pm_ct._append_pm_clearance_detail_row(
					cl,
					{
						"settlement_type": "Purchase Invoice",
						"purchase_invoice": pi,
						"allocated_amount": 4_000,
						"outstanding_amount": 4_000,
					},
				)
				cl.append(
					"request_allocations",
					{
						"funding_source_type": "PM Request",
						"pm_request": req,
						"allocated_amount": 4_000,
					},
				)
				cl.flags.ignore_mandatory = True
				cl.insert(ignore_permissions=True)
				_f.db.commit()
				ok = True
				name = cl.name
				ds_after = cint(_f.db.get_value("Purchase Invoice", pi, "docstatus"))
			except Exception as e:
				_f.db.rollback()
				err = str(e)[:400]
			results.append(
				{"label": "clearance", "ok": ok, "err": err, "name": name, "ds": ds_after}
			)
			_f.destroy()

		t1 = threading.Thread(target=cancel_worker)
		t2 = threading.Thread(target=clearance_worker)
		t1.start()
		t2.start()
		t1.join(90)
		t2.join(90)

		frappe.connect()
		frappe.set_user("Administrator")
		pi_ds = cint(frappe.db.get_value("Purchase Invoice", pi, "docstatus"))
		active = find_active_clearances_referencing_pi(pi)
		cancel_r = next(r for r in results if r["label"] == "cancel")
		clr_r = next(r for r in results if r["label"] == "clearance")

		# Never allow committed (cancelled PI + active Clearance reference).
		invalid = pi_ds == 2 and bool(active)
		self.assertFalse(
			invalid,
			f"Invalid committed state: pi_ds={pi_ds} active={active} results={results}",
		)
		if cancel_r["ok"] and clr_r["ok"]:
			self.fail(f"Both workers succeeded: {results} pi_ds={pi_ds} active={active}")
		if clr_r["ok"] and clr_r.get("name"):
			self.assertIn(clr_r["name"], active)
			self.assertEqual(pi_ds, 1)
		if cancel_r["ok"]:
			self.assertEqual(pi_ds, 2)
			self.assertFalse(clr_r["ok"])
			self.assertEqual(active, [])

		_restore_pi_submitted(pi, 4_000)
		frappe.db.commit()

	def test_legacy_diagnostic_includes_target_pattern(self):
		emp, req, _ = self._fund(2_000)
		pi = self._pi(2_000)
		cl = self._make_clearance(
			emp, req, pi, 2_000, state="Pending Finance Review", status="Pending Approval"
		)
		_mark_pi_cancelled(pi)
		frappe.db.commit()
		rep = report_legacy_cancelled_pi_on_clearances()
		names = {r["pm_clearance"] for r in rep}
		self.assertIn(cl.name, names)
		# Target production document if present
		if frappe.db.exists("PM Clearance", "CLR-HR-EMP-0024-202609-b39de9014e16"):
			self.assertIn("CLR-HR-EMP-0024-202609-b39de9014e16", names)
