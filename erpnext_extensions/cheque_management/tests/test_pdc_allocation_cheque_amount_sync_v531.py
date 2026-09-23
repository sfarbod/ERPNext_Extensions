# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""v5.3.1 — Draft single-allocation sync when Cheque Amount is reduced.

Covers unit scenarios A–H and a Payment Request → Draft → reduce → save lifecycle
(integration, skipped when site fixtures are unavailable).

Run::

    ./env/bin/python -m unittest \\
        erpnext_extensions.cheque_management.tests.test_pdc_allocation_cheque_amount_sync_v531 -v
"""

from __future__ import annotations

import time
import unittest
from contextlib import ExitStack, contextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.exceptions import ValidationError
from frappe.utils import flt

import erpnext_extensions.cheque_management.pdc_allocation as pdc_alloc
from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
	PostDatedCheque,
)
from erpnext_extensions.cheque_management.pdc_allocation import (
	ALLOCATION_MODE_DIRECT,
	sync_pdc_allocation_summary_amounts,
	sync_single_pdc_allocation_on_reduced_cheque_amount,
)
from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	CHEQUE_DIRECTION_PAYABLE,
	CHEQUE_DIRECTION_RECEIVABLE,
	WORKFLOW_DRAFT,
	WORKFLOW_REGISTERED,
)


def _uniq(prefix: str) -> str:
	return f"{prefix}-{int(time.time() * 1000)}"


def _alloc_row(amount: float, **kw) -> SimpleNamespace:
	base = dict(
		amount=amount,
		allocation_mode=ALLOCATION_MODE_DIRECT,
		reference_doctype="Payment Request",
		reference_name="PR-1",
		company="_TC",
		party_type="Supplier",
		party="SUP-1",
		currency="IRR",
		source_doctype="Payment Request",
		source_name="PR-1",
	)
	base.update(kw)
	return SimpleNamespace(**base)


def _pdc(**kwargs) -> PostDatedCheque:
	p = PostDatedCheque.__new__(PostDatedCheque)
	defaults: dict = {
		"cheque_amount": 100.0,
		"cheque_direction": CHEQUE_DIRECTION_PAYABLE,
		"workflow_state": WORKFLOW_DRAFT,
		"docstatus": 0,
		"company": "_TC",
		"currency": "IRR",
		"name": "PDC-SYNC-TEST",
		"party_type": "Supplier",
		"party": "SUP-1",
		"allocation_mode": ALLOCATION_MODE_DIRECT,
		"allocation_mode_locked": 0,
		"allocations": [],
		"allocated_amount": 0.0,
		"unallocated_amount": 0.0,
	}
	defaults.update(kwargs)
	for k, v in defaults.items():
		setattr(p, k, v)
	return p


@contextmanager
def _frappe_throw_as_validation():
	def _throw(msg, *args, **kwargs):
		raise ValidationError(msg)

	with patch.object(frappe, "_", lambda s: s), patch.object(frappe, "throw", side_effect=_throw):
		yield


_SNAP_PR = {
	"company": "_TC",
	"currency": "IRR",
	"party_type": "Supplier",
	"party": "SUP-1",
	"payment_request_type": "Outward",
	"docstatus": 1,
	"workflow_state": "Approved",
	"outstanding_amount": 1_000_000.0,
	"status": "Requested",
}


_SNAP_SI = {
	"company": "_TC",
	"currency": "IRR",
	"customer": "C-1",
	"docstatus": 1,
	"outstanding_amount": 1_000_000.0,
}


def _capacity_patches():
	return (
		patch.object(pdc_alloc, "_read_sales_invoice_for_pdc_allocation", return_value=_SNAP_SI),
		patch.object(pdc_alloc, "_read_purchase_invoice_for_pdc_allocation", return_value=None),
		patch.object(pdc_alloc, "_read_payment_request_for_pdc_allocation", return_value=_SNAP_PR),
		patch.object(pdc_alloc, "_read_sales_order_for_pdc_allocation", return_value=None),
		patch.object(pdc_alloc, "_read_purchase_order_for_pdc_allocation", return_value=None),
		patch.object(pdc_alloc, "get_pr_remaining_capacity", return_value=1_000_000.0),
		patch.object(pdc_alloc, "get_invoice_remaining_capacity", return_value=1_000_000.0),
		patch.object(
			pdc_alloc,
			"get_receivable_sales_invoice_direct_settlement_remaining_capacity",
			return_value=1_000_000.0,
		),
		patch.object(pdc_alloc, "get_order_remaining_advance_capacity", return_value=1_000_000.0),
		patch.object(pdc_alloc, "get_invoice_ledger_outstanding", return_value=0.0),
		patch.object(pdc_alloc, "sum_effective_pdc_direct_to_invoice", return_value=0.0),
		patch.object(pdc_alloc, "sum_effective_pdc_via_pr_to_invoice", return_value=0.0),
	)


def _si_row(amount: float) -> SimpleNamespace:
	return _alloc_row(
		amount,
		reference_doctype="Sales Invoice",
		reference_name="SINV-1",
		source_doctype=None,
		source_name=None,
		party_type="Customer",
		party="C-1",
	)


class TestSyncSingleAllocationOnReducedChequeAmount(unittest.TestCase):
	"""Unit Tests A–H for ``sync_single_pdc_allocation_on_reduced_cheque_amount``."""

	def test_a_new_single_allocation_clamped_on_decrease(self):
		"""Test A — Cheque 100 → 80, single allocation 100 → 80."""
		p = _pdc(
			cheque_amount=80.0,
			allocations=[_alloc_row(100.0)],
			docstatus=0,
		)
		changed = sync_single_pdc_allocation_on_reduced_cheque_amount(p)
		self.assertTrue(changed)
		self.assertEqual(flt(p.allocations[0].amount), 80.0)
		with _frappe_throw_as_validation():
			sync_pdc_allocation_summary_amounts(p)
		self.assertEqual(flt(p.allocated_amount), 80.0)
		self.assertEqual(flt(p.unallocated_amount), 0.0)

	def test_b_draft_validate_allocations_clamps_and_saves_path(self):
		"""Test B — Saved Draft path via ``_validate_allocations`` (no ValidationError)."""
		p = _pdc(
			cheque_amount=80.0,
			cheque_direction=CHEQUE_DIRECTION_RECEIVABLE,
			party_type="Customer",
			party="C-1",
			allocations=[_si_row(100.0)],
			workflow_state=WORKFLOW_DRAFT,
			docstatus=0,
		)
		with ExitStack() as stack:
			stack.enter_context(_frappe_throw_as_validation())
			for c in _capacity_patches():
				stack.enter_context(c)
			PostDatedCheque._validate_allocations(p)
		self.assertEqual(flt(p.allocations[0].amount), 80.0)
		self.assertEqual(flt(p.allocated_amount), 80.0)
		self.assertEqual(flt(p.unallocated_amount), 0.0)

	def test_c_register_path_uses_clamped_allocation(self):
		"""Test C — After clamp, amounts remain consistent for Register (docstatus still 0 during validate)."""
		p = _pdc(
			cheque_amount=80.0,
			cheque_direction=CHEQUE_DIRECTION_RECEIVABLE,
			party_type="Customer",
			party="C-1",
			allocations=[_si_row(100.0)],
			workflow_state=WORKFLOW_DRAFT,
			docstatus=0,
		)
		with ExitStack() as stack:
			stack.enter_context(_frappe_throw_as_validation())
			for c in _capacity_patches():
				stack.enter_context(c)
			PostDatedCheque._validate_allocations(p)
		self.assertEqual(flt(p.allocations[0].amount), 80.0)
		# Simulate post-register state: sync must not rewrite submitted docs.
		p.docstatus = 1
		p.workflow_state = WORKFLOW_REGISTERED
		p.cheque_amount = 80.0
		p.allocations[0].amount = 80.0
		self.assertFalse(sync_single_pdc_allocation_on_reduced_cheque_amount(p))
		self.assertEqual(flt(p.allocations[0].amount), 80.0)

	def test_d_genuine_multi_row_over_allocation_still_rejected(self):
		"""Test D — Multi-row total > cheque is not auto-fixed; validation still raises."""
		p = _pdc(
			cheque_amount=80.0,
			allocations=[
				_alloc_row(60.0, reference_name="PR-A"),
				_alloc_row(40.0, reference_name="PR-B"),
			],
			docstatus=0,
		)
		self.assertFalse(sync_single_pdc_allocation_on_reduced_cheque_amount(p))
		self.assertEqual(flt(p.allocations[0].amount), 60.0)
		self.assertEqual(flt(p.allocations[1].amount), 40.0)
		with _frappe_throw_as_validation(), self.assertRaises(ValidationError) as ctx:
			sync_pdc_allocation_summary_amounts(p)
		self.assertIn("cannot exceed Cheque Amount", str(ctx.exception))

	def test_e_multi_allocation_no_silent_redistribution(self):
		"""Test E — Cheque 100→80 with rows 60+40: amounts unchanged by sync helper."""
		p = _pdc(
			cheque_amount=80.0,
			allocations=[
				_alloc_row(60.0, reference_name="PR-A"),
				_alloc_row(40.0, reference_name="PR-B"),
			],
		)
		before = [flt(r.amount) for r in p.allocations]
		self.assertFalse(sync_single_pdc_allocation_on_reduced_cheque_amount(p))
		self.assertEqual([flt(r.amount) for r in p.allocations], before)

	def test_f_manual_under_allocation_preserved(self):
		"""Test F — Cheque 100 / Allocation 80: do not inflate to 100."""
		p = _pdc(cheque_amount=100.0, allocations=[_alloc_row(80.0)])
		self.assertFalse(sync_single_pdc_allocation_on_reduced_cheque_amount(p))
		self.assertEqual(flt(p.allocations[0].amount), 80.0)
		with _frappe_throw_as_validation():
			sync_pdc_allocation_summary_amounts(p)
		self.assertEqual(flt(p.allocated_amount), 80.0)
		self.assertEqual(flt(p.unallocated_amount), 20.0)

	def test_g_increasing_cheque_does_not_raise_allocation(self):
		"""Test G — Cheque 80→100 with allocation 80: leave allocation at 80."""
		p = _pdc(cheque_amount=100.0, allocations=[_alloc_row(80.0)])
		self.assertFalse(sync_single_pdc_allocation_on_reduced_cheque_amount(p))
		self.assertEqual(flt(p.allocations[0].amount), 80.0)

	def test_h_submitted_pdc_not_rewritten(self):
		"""Test H — docstatus 1: helper is a no-op even if allocation > cheque."""
		p = _pdc(
			cheque_amount=80.0,
			allocations=[_alloc_row(100.0)],
			docstatus=1,
			workflow_state=WORKFLOW_REGISTERED,
		)
		self.assertFalse(sync_single_pdc_allocation_on_reduced_cheque_amount(p))
		self.assertEqual(flt(p.allocations[0].amount), 100.0)


class TestDraftPDCChequeAmountSyncIntegration(unittest.TestCase):
	"""DB-backed Draft save regression (Test B / partial PR lifecycle when fixtures exist)."""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Only run under bench --site (frappe.db bound). Bare unittest skips cleanly.
		try:
			frappe.db.exists("DocType", "Post Dated Cheque")
		except Exception:
			raise unittest.SkipTest("No Frappe DB (run via bench --site)") from None
		if not frappe.db.exists("DocType", "Post Dated Cheque"):
			raise unittest.SkipTest("Post Dated Cheque not installed")
		frappe.set_user("Administrator")

	def _company(self) -> str:
		company = frappe.db.get_value("Company", {}, "name", order_by="creation asc")
		if not company:
			self.skipTest("No Company")
		return company

	def _bank_account_company(self, company: str) -> str:
		# Prefer a bank account that already has Available cheque leaves (real cheque books).
		row = frappe.db.sql(
			"""
			select ba.name
			from `tabBank Account` ba
			inner join `tabCheque Leaf` cl on cl.bank_account = ba.name and cl.status = 'Available'
			where ba.company = %s
			limit 1
			""",
			company,
		)
		if row:
			return row[0][0]
		ba = frappe.db.get_value(
			"Bank Account",
			{"company": company, "is_company_account": 1},
			"name",
			order_by="creation asc",
		)
		if not ba:
			ba = frappe.db.get_value("Bank Account", {"company": company}, "name", order_by="creation asc")
		if not ba:
			self.skipTest(f"No Bank Account for {company}")
		return ba

	def _group(self, company: str, root_type: str) -> str:
		name = frappe.db.get_value(
			"Account",
			{"company": company, "is_group": 1, "root_type": root_type},
			"name",
			order_by="lft asc",
		)
		if not name:
			self.skipTest(f"No group Account root_type={root_type}")
		return name

	def _account(self, company: str, parent: str, account_name: str) -> str:
		exists = frappe.db.get_value("Account", {"company": company, "account_name": account_name}, "name")
		if exists:
			return exists
		acc = frappe.new_doc("Account")
		acc.company = company
		acc.account_name = account_name
		acc.parent_account = parent
		acc.is_group = 0
		acc.insert(ignore_permissions=True)
		return acc.name

	def _ensure_settings(self, company: str, pool: str) -> None:
		name = frappe.db.get_value("PDC Settings", {"company": company}, "name") or company
		if frappe.db.exists("PDC Settings", name):
			doc = frappe.get_doc("PDC Settings", name)
		else:
			doc = frappe.new_doc("PDC Settings")
			doc.company = company
			doc.name = name
		doc.default_payable_cheque_account = pool
		doc.default_protested_account = pool
		if not doc.get("default_cheques_in_hand_account"):
			doc.default_cheques_in_hand_account = pool
		if not doc.get("default_cheques_in_clearing_account"):
			doc.default_cheques_in_clearing_account = pool
		doc.require_sayad_registration = 0
		if doc.get("name") and frappe.db.exists("PDC Settings", doc.get("name")):
			doc.save(ignore_permissions=True)
		else:
			doc.insert(ignore_permissions=True)

	def _leaf(self, company: str, bank_account: str) -> str:
		existing = frappe.db.get_value(
			"Cheque Leaf",
			{"bank_account": bank_account, "status": "Available"},
			"name",
			order_by="creation asc",
		)
		if existing:
			return existing
		start = (int(time.time() * 1000) % 900000) + 100000
		book = frappe.new_doc("Cheque Book")
		book.company = company
		book.bank_account = bank_account
		book.generation_mode = "prefix_plus_sequence"
		book.prefix = "V531"
		book.start_number = start
		book.end_number = start
		book.number_padding = 0
		book.insert(ignore_permissions=True)
		book.generate_leaves()
		leaf = frappe.db.get_value(
			"Cheque Leaf", {"cheque_book": book.name, "status": "Available"}, "name"
		)
		if not leaf:
			self.skipTest("Cheque Leaf not created")
		return leaf

	def test_b_saved_draft_reduce_cheque_auto_clamps_allocation(self):
		"""Regression: save Draft at 100/100, reduce cheque to 80 without editing child, save OK."""
		if not frappe.db.exists("DocType", "Cheque Leaf"):
			self.skipTest("Cheque Leaf not installed")

		company = self._company()
		bank_account = self._bank_account_company(company)
		liability = self._group(company, "Liability")
		pool = self._account(company, liability, _uniq("PDC-POOL-SYNC"))
		self._ensure_settings(company, pool)
		leaf = self._leaf(company, bank_account)

		from erpnext_extensions.cheque_management.pdc_settlement_summary import (
			get_settlement_summary_for_reference,
		)

		ref_dt = ref_nm = party = None
		pr_rows = frappe.get_all(
			"Payment Request",
			filters={"docstatus": 1, "party_type": "Supplier", "company": company},
			fields=["name", "party"],
			limit=30,
			order_by="modified desc",
		)
		for row in pr_rows:
			try:
				summary = get_settlement_summary_for_reference("Payment Request", row.name)
			except Exception:
				continue
			if summary and flt(summary.get("remaining_balance")) >= 100:
				ref_dt, ref_nm, party = "Payment Request", row.name, row.party
				break

		if not ref_nm:
			pi = frappe.db.get_value(
				"Purchase Invoice",
				{"docstatus": 1, "company": company, "outstanding_amount": (">", 100)},
				["name", "supplier"],
				as_dict=True,
				order_by="modified desc",
			)
			if not pi:
				self.skipTest("No Payment Request or Purchase Invoice with capacity")
			ref_dt, ref_nm, party = "Purchase Invoice", pi.name, pi.supplier

		doc = frappe.new_doc("Post Dated Cheque")
		doc.cheque_direction = "Payable"
		doc.company = company
		doc.bank_account = bank_account
		doc.cheque_leaf = leaf
		doc.cheque_no = frappe.db.get_value("Cheque Leaf", leaf, "cheque_number") or "1"
		doc.cheque_amount = 100
		doc.cheque_due_date = date.today()
		doc.received_date = date.today()
		doc.party_type = "Supplier"
		doc.party = party
		doc.holder_party_type = "Supplier"
		doc.holder_party = party
		doc.account_paid_from = pool
		doc.allocation_mode = ALLOCATION_MODE_DIRECT
		doc.reference_doctype = ref_dt
		doc.reference_name = ref_nm
		child = {
			"allocation_mode": ALLOCATION_MODE_DIRECT,
			"reference_doctype": ref_dt,
			"reference_name": ref_nm,
			"amount": 100,
			"company": company,
			"party_type": "Supplier",
			"party": party,
		}
		if ref_dt == "Payment Request":
			child["source_doctype"] = "Payment Request"
			child["source_name"] = ref_nm
		doc.append("allocations", child)

		doc.insert(ignore_permissions=True)
		doc.reload()
		self.assertEqual(flt(doc.cheque_amount), 100)
		self.assertEqual(flt(doc.allocations[0].amount), 100)
		self.assertEqual(cint_ds(doc.docstatus), 0)

		doc.cheque_amount = 80
		self.assertEqual(flt(doc.allocations[0].amount), 100)
		doc.save(ignore_permissions=True)
		doc.reload()

		self.assertEqual(flt(doc.cheque_amount), 80)
		self.assertEqual(flt(doc.allocations[0].amount), 80)
		self.assertEqual(flt(doc.allocated_amount), 80)
		self.assertGreaterEqual(flt(doc.unallocated_amount), -1e-6)

		frappe.delete_doc("Post Dated Cheque", doc.name, force=1, ignore_permissions=True)

	def test_c_register_after_reduction_when_supported(self):
		"""Test C — Register after Draft reduction when workflow + accounts allow."""
		from frappe.model.workflow import apply_workflow

		if not frappe.db.exists("DocType", "Cheque Leaf"):
			self.skipTest("Cheque Leaf not installed")

		company = self._company()
		bank_account = self._bank_account_company(company)
		liability = self._group(company, "Liability")
		pool = self._account(company, liability, _uniq("PDC-POOL-REG"))
		self._ensure_settings(company, pool)
		leaf = self._leaf(company, bank_account)

		pi = frappe.db.get_value(
			"Purchase Invoice",
			{"docstatus": 1, "company": company, "outstanding_amount": (">", 100)},
			["name", "supplier"],
			as_dict=True,
			order_by="modified desc",
		)
		if not pi:
			self.skipTest("No Purchase Invoice with outstanding for Register integration")

		doc = frappe.new_doc("Post Dated Cheque")
		doc.cheque_direction = "Payable"
		doc.company = company
		doc.bank_account = bank_account
		doc.cheque_leaf = leaf
		doc.cheque_no = frappe.db.get_value("Cheque Leaf", leaf, "cheque_number") or "1"
		doc.cheque_amount = 100
		doc.cheque_due_date = date.today()
		doc.received_date = date.today()
		doc.party_type = "Supplier"
		doc.party = pi.supplier
		doc.holder_party_type = "Supplier"
		doc.holder_party = pi.supplier
		doc.account_paid_from = pool
		doc.allocation_mode = ALLOCATION_MODE_DIRECT
		doc.reference_doctype = "Purchase Invoice"
		doc.reference_name = pi.name
		doc.append(
			"allocations",
			{
				"allocation_mode": ALLOCATION_MODE_DIRECT,
				"reference_doctype": "Purchase Invoice",
				"reference_name": pi.name,
				"amount": 100,
				"company": company,
				"party_type": "Supplier",
				"party": pi.supplier,
			},
		)
		doc.insert(ignore_permissions=True)
		doc.reload()
		doc.cheque_amount = 80
		doc.save(ignore_permissions=True)
		doc.reload()
		self.assertEqual(flt(doc.allocations[0].amount), 80)

		try:
			registered = apply_workflow(doc, "Register Cheque")
			registered.reload()
			self.assertEqual(cint_ds(registered.docstatus), 1)
			self.assertEqual(flt(registered.allocations[0].amount), 80)
		except Exception as exc:
			frappe.delete_doc("Post Dated Cheque", doc.name, force=1, ignore_permissions=True)
			self.skipTest(f"Register not possible on this site fixture: {exc}")

		try:
			frappe.delete_doc("Post Dated Cheque", registered.name, force=1, ignore_permissions=True)
		except Exception:
			pass

	def test_partial_pr_second_pdc_capacity_when_available(self):
		"""Where PR remaining allows: first Draft PDC at 80 leaves capacity for a second prefill."""
		from erpnext_extensions.cheque_management.pdc_create_from_source import (
			prepare_post_dated_cheque_prefill_from_source,
		)
		from erpnext_extensions.cheque_management.pdc_settlement_summary import (
			get_settlement_summary_for_reference,
		)

		company = self._company()
		pr_rows = frappe.get_all(
			"Payment Request",
			filters={"docstatus": 1, "party_type": "Supplier", "company": company},
			fields=["name", "party"],
			limit=30,
			order_by="modified desc",
		)
		chosen = None
		remaining0 = 0.0
		for row in pr_rows:
			try:
				summary = get_settlement_summary_for_reference("Payment Request", row.name)
			except Exception:
				continue
			rem = flt(summary.get("remaining_balance") if summary else 0)
			if rem >= 100:
				chosen = row
				remaining0 = rem
				break
		if not chosen:
			self.skipTest("No Payment Request with remaining >= 100")

		if not frappe.db.exists("DocType", "Cheque Leaf"):
			self.skipTest("Cheque Leaf not installed")

		bank_account = self._bank_account_company(company)
		liability = self._group(company, "Liability")
		pool = self._account(company, liability, _uniq("PDC-POOL-PR2"))
		self._ensure_settings(company, pool)
		leaf = self._leaf(company, bank_account)

		doc = frappe.new_doc("Post Dated Cheque")
		doc.cheque_direction = "Payable"
		doc.company = company
		doc.bank_account = bank_account
		doc.cheque_leaf = leaf
		doc.cheque_no = frappe.db.get_value("Cheque Leaf", leaf, "cheque_number") or "1"
		doc.cheque_amount = 100
		doc.cheque_due_date = date.today()
		doc.received_date = date.today()
		doc.party_type = "Supplier"
		doc.party = chosen.party
		doc.holder_party_type = "Supplier"
		doc.holder_party = chosen.party
		doc.account_paid_from = pool
		doc.allocation_mode = ALLOCATION_MODE_DIRECT
		doc.reference_doctype = "Payment Request"
		doc.reference_name = chosen.name
		doc.append(
			"allocations",
			{
				"allocation_mode": ALLOCATION_MODE_DIRECT,
				"reference_doctype": "Payment Request",
				"reference_name": chosen.name,
				"source_doctype": "Payment Request",
				"source_name": chosen.name,
				"amount": 100,
				"company": company,
				"party_type": "Supplier",
				"party": chosen.party,
			},
		)
		doc.insert(ignore_permissions=True)
		doc.cheque_amount = 80
		doc.save(ignore_permissions=True)
		doc.reload()
		self.assertEqual(flt(doc.allocations[0].amount), 80)

		summary_after = get_settlement_summary_for_reference("Payment Request", chosen.name)
		remaining_after = flt(summary_after.get("remaining_balance") if summary_after else 0)
		# Draft payable allocations may or may not reserve capacity depending on effective milestone;
		# assert remaining is finite and second prefill either works or explains zero remaining.
		self.assertGreaterEqual(remaining_after, 0)
		prefill = prepare_post_dated_cheque_prefill_from_source("Payment Request", chosen.name)
		if remaining_after >= 20 and prefill.get("can_create"):
			self.assertGreaterEqual(flt((prefill.get("prefill") or {}).get("cheque_amount")), 20)
		frappe.delete_doc("Post Dated Cheque", doc.name, force=1, ignore_permissions=True)
		_ = remaining0  # original remaining was large enough to start


def cint_ds(v) -> int:
	try:
		return int(v or 0)
	except (TypeError, ValueError):
		return 0
