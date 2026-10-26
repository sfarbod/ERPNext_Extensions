# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Real DB integration / E2E / concurrency tests for Debt Purchase settlement.

Covers:
- Register → Assign → Facility Repayment submit → Settled
- Cancel restore
- Cancel failure atomicity (JE cancel fails)
- Journal Reference append/remove idempotency
- Real concurrent dual-submit on the same Assigned PDC
"""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from unittest.mock import patch

import frappe
from frappe.utils import flt, getdate, random_string, today

from erpnext_extensions.cheque_management.doctype.post_dated_cheque.post_dated_cheque import (
	_get_pdc_settings_for_company,
	get_default_party_accounts,
	resolve_pdc_accounts_for_journal,
)
from erpnext_extensions.cheque_management.pdc_journal_entry_service import (
	post_pdc_transition_journal_entry,
)
from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	WORKFLOW_ASSIGNED_DEBT_PURCHASE,
	WORKFLOW_DEBT_PURCHASE_SETTLED,
	WORKFLOW_DRAFT,
	WORKFLOW_REGISTERED,
	normalize_workflow_state_value,
)
from erpnext_extensions.cheque_management.pdc_workflow_to_cheque_status import (
	CHEQUE_STATUS_DEBT_PURCHASE_IN_COLLECTION,
	CHEQUE_STATUS_DEBT_PURCHASE_SETTLED,
)
from erpnext_extensions.facility_management.doctype.facility.facility import create_receipt_journal_entry
from erpnext_extensions.facility_management.facility_balances import get_facility_balance_row
from erpnext_extensions.facility_management.facility_debt_purchase import (
	PURPOSE_DEBT_PURCHASE_SETTLEMENT,
	REPAYMENT_METHOD_DEBT_PURCHASE,
	append_pdc_settlement_journal_reference,
	remove_pdc_settlement_journal_reference,
	_settlement_transition_key,
)
from erpnext_extensions.facility_management.facility_e2e_context import (
	apply_facility_test_accounts,
	ensure_bank_master,
)
from erpnext_extensions.facility_management.facility_settings_doc import (
	get_facility_settings_doc,
	resolve_repayment_cost_center,
)


PURPOSE_ASSIGNMENT = "Debt Purchase Assignment"


def _group_account(company: str, root_type: str) -> str:
	name = frappe.db.get_value(
		"Account",
		{"company": company, "is_group": 1, "root_type": root_type},
		"name",
		order_by="lft asc",
	)
	if not name:
		frappe.throw(f"No group Account root_type={root_type} for {company}")
	return name


def _ensure_leaf_account(company: str, parent: str, account_name: str) -> str:
	exists = frappe.db.get_value("Account", {"company": company, "account_name": account_name}, "name")
	if exists:
		return exists
	acc = frappe.new_doc("Account")
	acc.company = company
	acc.parent_account = parent
	acc.account_name = account_name
	acc.is_group = 0
	acc.insert(ignore_permissions=True)
	return acc.name


def _ensure_dpic_in_settings(company: str) -> str:
	asset = _group_account(company, "Asset")
	dpic = _ensure_leaf_account(company, asset, "E2E Debt Purchase In Collection")
	settings_name = frappe.db.get_value("PDC Settings", {"company": company}, "name") or company
	if not frappe.db.exists("PDC Settings", settings_name):
		doc = frappe.new_doc("PDC Settings")
		doc.company = company
		doc.name = settings_name
		doc.default_debt_purchase_in_collection_account = dpic
		doc.insert(ignore_permissions=True)
	else:
		doc = frappe.get_doc("PDC Settings", settings_name)
		if not (doc.default_debt_purchase_in_collection_account or "").strip():
			doc.default_debt_purchase_in_collection_account = dpic
			doc.save(ignore_permissions=True)
	return dpic


def _ensure_dp_facility_type() -> str:
	name = "E2E Debt Purchase Type"
	if frappe.db.exists("Facility Type", name):
		frappe.db.set_value("Facility Type", name, "is_debt_purchase", 1, update_modified=False)
		return name
	doc = frappe.new_doc("Facility Type")
	doc.facility_type_name = name
	doc.is_debt_purchase = 1
	doc.insert(ignore_permissions=True)
	return doc.name


def _pick_company() -> str:
	row = frappe.db.sql(
		"""
		SELECT c.name
		FROM `tabCompany` c
		WHERE c.name NOT LIKE '\\_Test%%'
		  AND EXISTS (SELECT 1 FROM `tabAccount` a WHERE a.company = c.name AND a.is_group = 0 LIMIT 1)
		ORDER BY c.creation ASC
		LIMIT 1
		""",
		as_list=True,
	)
	if row:
		return row[0][0]
	company = frappe.db.get_value("Company", {}, "name", order_by="creation asc")
	if not company:
		frappe.throw("No Company")
	return company


def _refs(pdc_name: str, *, purpose: str | None = None) -> list[dict]:
	filters: dict = {"parent": pdc_name, "parenttype": "Post Dated Cheque"}
	if purpose:
		filters["purpose"] = purpose
	return frappe.get_all(
		"PDC Journal Reference",
		filters=filters,
		fields=["name", "journal_entry", "purpose", "pdc_transition_key", "amount"],
		order_by="idx asc",
	)


def _je_rows(je_name: str) -> list[dict]:
	return frappe.get_all(
		"Journal Entry Account",
		filters={"parent": je_name},
		fields=[
			"idx",
			"account",
			"debit_in_account_currency",
			"credit_in_account_currency",
			"party_type",
			"party",
			"reference_type",
			"reference_name",
		],
		order_by="idx asc",
	)


class DebtPurchaseDbFixtureMixin:
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		cls.company = _pick_company()
		cls.dpic = _ensure_dpic_in_settings(cls.company)
		cls.ft = _ensure_dp_facility_type()
		cls.customer = frappe.db.get_value("Customer", {"disabled": 0}, "name", order_by="modified desc")
		if not cls.customer:
			raise unittest.SkipTest("No Customer")
		cls.bank_account = frappe.db.get_value(
			"Bank Account",
			{"company": cls.company, "disabled": 0},
			"name",
			order_by="modified desc",
		)
		if not cls.bank_account:
			raise unittest.SkipTest("No Bank Account")
		settings = _get_pdc_settings_for_company(cls.company)
		if not settings or not settings.default_cheques_in_hand_account:
			raise unittest.SkipTest("PDC Settings / Cheques in Hand missing")
		cls.settings = settings
		frappe.db.commit()

	def _new_assigned_pdc(self, *, amount: float = 1000.0) -> str:
		acc = get_default_party_accounts("Customer", self.customer, self.company, "Receivable") or {}
		doc = frappe.new_doc("Post Dated Cheque")
		doc.cheque_direction = "Receivable"
		doc.company = self.company
		doc.party_type = "Customer"
		doc.party = self.customer
		doc.cheque_no = f"DP-E2E-{random_string(8)}"
		doc.cheque_due_date = getdate(today()) + timedelta(days=30)
		doc.cheque_amount = amount
		doc.received_date = today()
		drawer_bank = frappe.db.get_value("Bank", {}, "name", order_by="modified desc")
		if not drawer_bank:
			drawer_bank = ensure_bank_master()
		doc.drawer_bank_name = drawer_bank
		doc.bank_account = self.bank_account
		doc.account_paid_to = acc.get("account_paid_to") or self.settings.default_cheques_in_hand_account
		doc.account_paid_from = acc.get("account_paid_from")
		doc.workflow_state = WORKFLOW_DRAFT
		doc.allocation_mode = "direct_settlement"
		doc.sayad_code = f"SAYAD-{doc.cheque_no}"[:32]
		doc.sayad_registered = 1
		doc.insert(ignore_permissions=True)
		frappe.db.commit()

		# Register
		doc.workflow_state = WORKFLOW_REGISTERED
		doc.flags.ignore_validate_update_after_submit = True
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc.reload()
		je = post_pdc_transition_journal_entry(doc, WORKFLOW_DRAFT, WORKFLOW_REGISTERED, posting_date=today())
		self.assertTrue(je)
		frappe.db.commit()

		# Assign Debt Purchase
		doc.reload()
		doc.workflow_state = WORKFLOW_ASSIGNED_DEBT_PURCHASE
		doc.flags.ignore_validate_update_after_submit = True
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		doc.reload()
		je_assign = post_pdc_transition_journal_entry(
			doc, WORKFLOW_REGISTERED, WORKFLOW_ASSIGNED_DEBT_PURCHASE, posting_date=today()
		)
		self.assertTrue(je_assign)
		frappe.db.commit()
		doc.reload()
		self.assertEqual(
			normalize_workflow_state_value(doc.workflow_state), WORKFLOW_ASSIGNED_DEBT_PURCHASE
		)
		self.assertEqual(doc.cheque_status, CHEQUE_STATUS_DEBT_PURCHASE_IN_COLLECTION)
		return doc.name

	def _new_active_facility(
		self,
		*,
		principal: float = 10000,
		profit: float = 2000,
		allocated_pdcs: list[str] | None = None,
	) -> str:
		settings = get_facility_settings_doc(self.company)
		bank = frappe.db.get_value("Bank", {}, "name", order_by="creation asc") or ensure_bank_master()
		cc = resolve_repayment_cost_center(facility=None, settings=settings)
		fac = frappe.new_doc("Facility")
		fac.facility_name = f"DP E2E {random_string(5)}"
		fac.company = self.company
		fac.facility_type = self.ft
		fac.bank = bank
		fac.contract_date = today()
		fac.receive_date = today()
		fac.principal_amount = principal
		fac.profit_amount = profit
		apply_facility_test_accounts(fac)
		if cc and not fac.cost_center:
			fac.cost_center = cc
		if not fac.department:
			dept = frappe.db.get_value(
				"Department", {"company": self.company, "disabled": 0}, "name", order_by="modified desc"
			) or frappe.db.get_value("Department", {"company": self.company}, "name")
			if dept:
				fac.department = dept
		# v5.6.20: Debt Purchase activation requires allocated PDC total == principal+profit.
		for pdc_name in allocated_pdcs or []:
			fac.append("debt_purchase_pdcs", {"post_dated_cheque": pdc_name})
		fac.insert(ignore_permissions=True)
		frappe.db.commit()
		create_receipt_journal_entry(fac.name)
		fac.reload()
		self.assertEqual(fac.status, "Active")
		return fac.name

	def _new_dp_repayment(self, facility: str, pdc_name: str, *, principal: float, profit: float) -> str:
		rep = frappe.new_doc("Facility Repayment")
		rep.facility = facility
		rep.company = self.company
		rep.posting_date = today()
		rep.repayment_method = REPAYMENT_METHOD_DEBT_PURCHASE
		rep.post_dated_cheque = pdc_name
		rep.principal_amount = principal
		rep.profit_amount = profit
		rep.penalty_amount = 0
		rep.insert(ignore_permissions=True)
		frappe.db.commit()
		return rep.name


class TestDebtPurchaseSettlementE2E(DebtPurchaseDbFixtureMixin, unittest.TestCase):
	def test_submit_settle_cancel_roundtrip(self):
		principal, profit = 900.0, 100.0
		amount = principal + profit
		pdc_name = self._new_assigned_pdc(amount=amount)
		fac_name = self._new_active_facility(
			principal=principal, profit=profit, allocated_pdcs=[pdc_name]
		)
		bal_before = get_facility_balance_row(fac_name)

		assign_refs = _refs(pdc_name, purpose=PURPOSE_ASSIGNMENT)
		self.assertEqual(len(assign_refs), 1)
		assign_je = assign_refs[0]["journal_entry"]
		assign_rows = _je_rows(assign_je)
		self.assertEqual(len(assign_rows), 2)
		pdc_party = frappe.db.get_value("Post Dated Cheque", pdc_name, ["party_type", "party"], as_dict=True)
		for r in assign_rows:
			self.assertEqual(r.get("party_type"), pdc_party.party_type)
			self.assertEqual(r.get("party"), pdc_party.party)
			self.assertFalse(r.get("reference_type"))
			self.assertFalse(r.get("reference_name"))

		rep_name = self._new_dp_repayment(fac_name, pdc_name, principal=principal, profit=profit)
		rep = frappe.get_doc("Facility Repayment", rep_name)
		rep.submit()
		frappe.db.commit()
		rep.reload()

		self.assertEqual(rep.docstatus, 1)
		self.assertTrue(rep.journal_entry)
		settle_je = frappe.get_doc("Journal Entry", rep.journal_entry)
		self.assertEqual(settle_je.docstatus, 1)

		pdc = frappe.get_doc("Post Dated Cheque", pdc_name)
		self.assertEqual(normalize_workflow_state_value(pdc.workflow_state), WORKFLOW_DEBT_PURCHASE_SETTLED)
		self.assertEqual(pdc.cheque_status, CHEQUE_STATUS_DEBT_PURCHASE_SETTLED)
		self.assertEqual(pdc.debt_purchase_facility, fac_name)
		self.assertEqual(pdc.debt_purchase_repayment, rep_name)

		settle_refs = _refs(pdc_name, purpose=PURPOSE_DEBT_PURCHASE_SETTLEMENT)
		self.assertEqual(len(settle_refs), 1, settle_refs)
		self.assertEqual(settle_refs[0]["journal_entry"], rep.journal_entry)
		self.assertEqual(settle_refs[0]["pdc_transition_key"], _settlement_transition_key(pdc))
		# Assignment ref still present
		self.assertEqual(len(_refs(pdc_name, purpose=PURPOSE_ASSIGNMENT)), 1)

		# Settlement JE: DPIC credit carries PDC Party; other Facility rows do not.
		dpic = resolve_pdc_accounts_for_journal(pdc)["debt_purchase_in_collection"]
		settle_rows = _je_rows(rep.journal_entry)
		credit_dpic = [
			r
			for r in settle_rows
			if r["account"] == dpic and flt(r["credit_in_account_currency"]) > 0
		]
		self.assertEqual(len(credit_dpic), 1)
		self.assertAlmostEqual(flt(credit_dpic[0]["credit_in_account_currency"]), amount)
		self.assertEqual(credit_dpic[0].get("party_type"), pdc.party_type)
		self.assertEqual(credit_dpic[0].get("party"), pdc.party)
		for r in settle_rows:
			self.assertFalse(r.get("reference_type"), r)
			self.assertFalse(r.get("reference_name"), r)
			if r["account"] == dpic and flt(r["credit_in_account_currency"]) > 0:
				continue
			self.assertFalse(r.get("party_type"), r)
			self.assertFalse(r.get("party"), r)

		bal_after = get_facility_balance_row(fac_name)
		self.assertAlmostEqual(
			flt(bal_after["paid_principal"]), flt(bal_before["paid_principal"]) + principal
		)
		self.assertAlmostEqual(flt(bal_after["paid_profit"]), flt(bal_before["paid_profit"]) + profit)

		# Cancel
		rep.cancel()
		frappe.db.commit()
		rep.reload()
		self.assertEqual(rep.docstatus, 2)
		settle_je.reload()
		self.assertEqual(settle_je.docstatus, 2)

		pdc.reload()
		self.assertEqual(
			normalize_workflow_state_value(pdc.workflow_state), WORKFLOW_ASSIGNED_DEBT_PURCHASE
		)
		self.assertEqual(pdc.cheque_status, CHEQUE_STATUS_DEBT_PURCHASE_IN_COLLECTION)
		self.assertFalse(pdc.debt_purchase_facility)
		self.assertFalse(pdc.debt_purchase_repayment)
		self.assertEqual(len(_refs(pdc_name, purpose=PURPOSE_DEBT_PURCHASE_SETTLEMENT)), 0)
		self.assertEqual(len(_refs(pdc_name, purpose=PURPOSE_ASSIGNMENT)), 1)

		bal_restored = get_facility_balance_row(fac_name)
		self.assertAlmostEqual(flt(bal_restored["paid_principal"]), flt(bal_before["paid_principal"]))
		self.assertAlmostEqual(flt(bal_restored["paid_profit"]), flt(bal_before["paid_profit"]))

	def test_cancel_failure_keeps_settled_state(self):
		principal, profit = 800.0, 200.0
		pdc_name = self._new_assigned_pdc(amount=principal + profit)
		fac_name = self._new_active_facility(
			principal=principal, profit=profit, allocated_pdcs=[pdc_name]
		)
		rep_name = self._new_dp_repayment(fac_name, pdc_name, principal=principal, profit=profit)
		rep = frappe.get_doc("Facility Repayment", rep_name)
		rep.submit()
		frappe.db.commit()
		rep.reload()
		je_name = rep.journal_entry
		self.assertEqual(len(_refs(pdc_name, purpose=PURPOSE_DEBT_PURCHASE_SETTLEMENT)), 1)

		with patch(
			"erpnext_extensions.facility_management.doctype.facility_repayment.facility_repayment.cancel_journal_entry",
			side_effect=frappe.ValidationError("forced JE cancel failure"),
		):
			with self.assertRaises(frappe.ValidationError):
				rep.cancel()
			frappe.db.rollback()

		rep.reload()
		pdc = frappe.get_doc("Post Dated Cheque", pdc_name)
		self.assertEqual(rep.docstatus, 1)
		self.assertEqual(rep.journal_entry, je_name)
		self.assertEqual(normalize_workflow_state_value(pdc.workflow_state), WORKFLOW_DEBT_PURCHASE_SETTLED)
		self.assertEqual(pdc.debt_purchase_repayment, rep_name)
		self.assertEqual(pdc.debt_purchase_facility, fac_name)
		self.assertEqual(len(_refs(pdc_name, purpose=PURPOSE_DEBT_PURCHASE_SETTLEMENT)), 1)
		self.assertEqual(frappe.db.get_value("Journal Entry", je_name, "docstatus"), 1)

		# Cleanup for site hygiene
		rep.cancel()
		frappe.db.commit()

	def test_settlement_reference_helpers_idempotent(self):
		principal, profit = 700.0, 300.0
		pdc_name = self._new_assigned_pdc(amount=principal + profit)
		fac_name = self._new_active_facility(
			principal=principal, profit=profit, allocated_pdcs=[pdc_name]
		)
		rep_name = self._new_dp_repayment(fac_name, pdc_name, principal=principal, profit=profit)
		rep = frappe.get_doc("Facility Repayment", rep_name)
		rep.submit()
		frappe.db.commit()
		rep.reload()
		pdc = frappe.get_doc("Post Dated Cheque", pdc_name)

		append_pdc_settlement_journal_reference(pdc, rep, rep.journal_entry)
		append_pdc_settlement_journal_reference(pdc, rep, rep.journal_entry)
		frappe.db.commit()
		self.assertEqual(len(_refs(pdc_name, purpose=PURPOSE_DEBT_PURCHASE_SETTLEMENT)), 1)

		remove_pdc_settlement_journal_reference(pdc, rep, settlement_je=rep.journal_entry)
		remove_pdc_settlement_journal_reference(pdc, rep, settlement_je=rep.journal_entry)
		frappe.db.commit()
		self.assertEqual(len(_refs(pdc_name, purpose=PURPOSE_DEBT_PURCHASE_SETTLEMENT)), 0)
		# Assignment untouched
		self.assertEqual(len(_refs(pdc_name, purpose=PURPOSE_ASSIGNMENT)), 1)

		# Re-append for cancel cleanup path (restore expects Settled + link; force restore via cancel)
		# Re-settle links were cleared only for refs; state still Settled — cancel normally.
		pdc.reload()
		append_pdc_settlement_journal_reference(pdc, rep, rep.journal_entry)
		frappe.db.commit()
		rep.reload()
		rep.cancel()
		frappe.db.commit()

	def test_repeated_settle_rejected(self):
		principal, profit = 600.0, 400.0
		pdc_name = self._new_assigned_pdc(amount=principal + profit)
		fac_name = self._new_active_facility(
			principal=principal, profit=profit, allocated_pdcs=[pdc_name]
		)
		rep1 = self._new_dp_repayment(fac_name, pdc_name, principal=principal, profit=profit)
		rep2 = self._new_dp_repayment(fac_name, pdc_name, principal=principal, profit=profit)
		frappe.get_doc("Facility Repayment", rep1).submit()
		frappe.db.commit()

		with self.assertRaises(frappe.ValidationError):
			frappe.get_doc("Facility Repayment", rep2).submit()
		frappe.db.rollback()

		frappe.get_doc("Facility Repayment", rep1).cancel()
		frappe.db.commit()


class TestDebtPurchaseConcurrency(DebtPurchaseDbFixtureMixin, unittest.TestCase):
	def test_dual_submit_same_cheque_one_wins(self):
		principal, profit = 900.0, 100.0
		amount = principal + profit
		pdc_name = self._new_assigned_pdc(amount=amount)
		# Facility total 25000; allocate settlement PDC + cover PDC for activation equality.
		cover = self._new_assigned_pdc(amount=24000.0)
		fac_name = self._new_active_facility(
			principal=20000, profit=5000, allocated_pdcs=[pdc_name, cover]
		)
		rep_a = self._new_dp_repayment(fac_name, pdc_name, principal=principal, profit=profit)
		rep_b = self._new_dp_repayment(fac_name, pdc_name, principal=principal, profit=profit)
		frappe.db.commit()

		site = frappe.local.site

		def worker(repayment_name: str) -> tuple[str, str]:
			frappe.init(site=site)
			frappe.connect()
			frappe.set_user("Administrator")
			try:
				doc = frappe.get_doc("Facility Repayment", repayment_name)
				doc.submit()
				frappe.db.commit()
				return ("ok", repayment_name)
			except Exception as exc:
				frappe.db.rollback()
				return ("err", f"{type(exc).__name__}: {exc}")
			finally:
				frappe.destroy()

		results: list[tuple[str, str]] = []
		with ThreadPoolExecutor(max_workers=2) as pool:
			futures = [pool.submit(worker, rep_a), pool.submit(worker, rep_b)]
			for fut in as_completed(futures):
				results.append(fut.result())

		oks = [r for r in results if r[0] == "ok"]
		errs = [r for r in results if r[0] == "err"]
		self.assertEqual(len(oks), 1, results)
		self.assertEqual(len(errs), 1, results)

		# Reconnect parent connection after destroy in workers
		frappe.connect()
		frappe.set_user("Administrator")

		winner = oks[0][1]
		loser = rep_b if winner == rep_a else rep_a
		win_doc = frappe.get_doc("Facility Repayment", winner)
		lose_doc = frappe.get_doc("Facility Repayment", loser)
		self.assertEqual(win_doc.docstatus, 1)
		self.assertEqual(lose_doc.docstatus, 0)
		self.assertTrue(win_doc.journal_entry)
		self.assertEqual(frappe.db.get_value("Journal Entry", win_doc.journal_entry, "docstatus"), 1)

		pdc = frappe.get_doc("Post Dated Cheque", pdc_name)
		self.assertEqual(normalize_workflow_state_value(pdc.workflow_state), WORKFLOW_DEBT_PURCHASE_SETTLED)
		self.assertEqual(pdc.debt_purchase_repayment, winner)
		self.assertEqual(len(_refs(pdc_name, purpose=PURPOSE_DEBT_PURCHASE_SETTLEMENT)), 1)

		# No second submitted repayment / settlement JE for this cheque
		submitted = frappe.get_all(
			"Facility Repayment",
			filters={"post_dated_cheque": pdc_name, "docstatus": 1},
			pluck="name",
		)
		self.assertEqual(submitted, [winner])

		# Cleanup
		win_doc.cancel()
		frappe.db.commit()


class TestDebtPurchaseRejectionE2E(DebtPurchaseDbFixtureMixin, unittest.TestCase):
	def test_non_dp_facility_submit_rejected_no_je_no_pdc_change(self):
		"""repayment_method=DP on non-DP Facility Type: reject with no accounting side effects."""
		non_dp = "E2E Non Debt Purchase Type"
		if frappe.db.exists("Facility Type", non_dp):
			frappe.db.set_value("Facility Type", non_dp, "is_debt_purchase", 0, update_modified=False)
		else:
			doc = frappe.new_doc("Facility Type")
			doc.facility_type_name = non_dp
			doc.is_debt_purchase = 0
			doc.insert(ignore_permissions=True)
		frappe.db.commit()

		pdc_name = self._new_assigned_pdc(amount=1000.0)
		pdc_before = frappe.get_doc("Post Dated Cheque", pdc_name)
		ws_before = pdc_before.workflow_state
		links_before = (pdc_before.debt_purchase_facility, pdc_before.debt_purchase_repayment)
		assign_refs_before = _refs(pdc_name, purpose=PURPOSE_ASSIGNMENT)

		cover = self._new_assigned_pdc(amount=12000.0)
		fac_name = self._new_active_facility(
			principal=10000, profit=2000, allocated_pdcs=[cover]
		)
		frappe.db.set_value("Facility", fac_name, "facility_type", non_dp, update_modified=False)
		frappe.db.commit()
		je_count_after_facility = frappe.db.count("Journal Entry", {"docstatus": 1})

		rep = frappe.new_doc("Facility Repayment")
		rep.facility = fac_name
		rep.company = self.company
		rep.posting_date = today()
		rep.repayment_method = REPAYMENT_METHOD_DEBT_PURCHASE
		rep.post_dated_cheque = pdc_name
		rep.principal_amount = 900
		rep.profit_amount = 100
		rep.penalty_amount = 0

		with self.assertRaises(frappe.ValidationError) as ctx:
			rep.insert(ignore_permissions=True)
		self.assertIn("Is Debt Purchase", str(ctx.exception))
		frappe.db.rollback()

		pdc_after = frappe.get_doc("Post Dated Cheque", pdc_name)
		self.assertEqual(pdc_after.workflow_state, ws_before)
		self.assertEqual(
			(pdc_after.debt_purchase_facility, pdc_after.debt_purchase_repayment), links_before
		)
		self.assertEqual(_refs(pdc_name, purpose=PURPOSE_ASSIGNMENT), assign_refs_before)
		self.assertEqual(frappe.db.count("Journal Entry", {"docstatus": 1}), je_count_after_facility)
		self.assertFalse(
			frappe.db.exists("Facility Repayment", {"post_dated_cheque": pdc_name, "docstatus": ["in", [0, 1]]})
		)


if __name__ == "__main__":
	unittest.main()
