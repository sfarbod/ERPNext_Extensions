from __future__ import annotations

import json
import time
import unittest
from datetime import date, timedelta

import frappe
from frappe.utils import now_datetime, today

from erpnext_extensions.cheque_management.pdc_workflow_rollback import (
	get_pdc_workflow_rollback_preview,
	rollback_workflow_state,
	sql_integrity_is_clean,
	sql_verify_no_orphan_gl_for_pdc,
	sql_verify_pdc_rollback_integrity,
)
from erpnext_extensions.cheque_management.pdc_workflow_to_cheque_status import (
	map_workflow_state_to_cheque_status,
)

# Uses unittest.TestCase (not FrappeTestCase) so the runner does not preload ERPNext
# Company test fixtures. That preload calls add_uom_data() against naming series
# MAT-UOM-CNV-, which collides with existing development-site UOM Conversion Factor
# rows when tabSeries.current lags max(name).


def _uniq(prefix: str) -> str:
	return f"{prefix}-{int(time.time() * 1000)}"


def _get_company() -> str:
	company = frappe.db.get_value("Company", {}, "name", order_by="creation asc")
	if not company:
		raise AssertionError("No Company on site for integration tests")
	return company


def _get_bank_account(company: str) -> str:
	ba = frappe.db.get_value("Bank Account", {"company": company}, "name", order_by="creation asc")
	if not ba:
		raise AssertionError(f"No Bank Account for company {company}")
	return ba


def _get_or_create_account(company: str, parent: str, account_name: str) -> str:
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


def _get_group_account(company: str, root_type: str) -> str:
	# Prefer non-ledger group accounts under the root type.
	name = frappe.db.get_value(
		"Account",
		{"company": company, "is_group": 1, "root_type": root_type},
		"name",
		order_by="lft asc",
	)
	if not name:
		raise AssertionError(f"No group Account with root_type={root_type} for company={company}")
	return name


def _ensure_pdc_settings(company: str, *, ci_hand: str, ci_clear: str, pool: str, protested: str) -> str:
	name = frappe.db.get_value("PDC Settings", {"company": company}, "name") or company
	if frappe.db.exists("PDC Settings", name):
		doc = frappe.get_doc("PDC Settings", name)
	else:
		doc = frappe.new_doc("PDC Settings")
		doc.company = company
		doc.name = name

	doc.default_cheques_in_hand_account = ci_hand
	doc.default_cheques_in_clearing_account = ci_clear
	doc.default_payable_cheque_account = pool
	doc.default_protested_account = protested
	doc.allow_endorsement = 1
	doc.require_sayad_registration = 0
	if doc.get("name") and frappe.db.exists("PDC Settings", doc.get("name")):
		doc.save(ignore_permissions=True)
	else:
		doc.insert(ignore_permissions=True)
	return doc.name


def _provision_payable_leaf(company: str, bank_account: str) -> str:
	start = (int(time.time() * 1000) % 900000) + 100000
	book = frappe.new_doc("Cheque Book")
	book.company = company
	book.bank_account = bank_account
	book.generation_mode = "prefix_plus_sequence"
	book.start_number = start
	book.end_number = start
	book.number_width = 6
	book.insert(ignore_permissions=True)
	book.generate_leaves()
	leaf = frappe.db.get_value("Cheque Leaf", {"cheque_book": book.name, "status": "Available"}, "name")
	if not leaf:
		raise AssertionError("Failed to generate payable Cheque Leaf")
	return leaf


def _apply_action(doc, action: str):
	from frappe.model.workflow import apply_workflow

	return apply_workflow(doc, action)


def _issue_payable(doc):
	if not doc.get("handover_date"):
		doc.handover_date = date.today()
		doc.save(ignore_permissions=True)
	return _apply_action(doc, "Issue Cheque")


def _clear_payable(doc):
	if not doc.get("cleared_date"):
		doc.cleared_date = date.today()
		doc.save(ignore_permissions=True)
	return _apply_action(doc, "Clear Cheque")


def _send_receivable_to_bank(doc):
	if not doc.get("sent_to_bank_date"):
		doc.sent_to_bank_date = date.today()
		doc.save(ignore_permissions=True)
	return _apply_action(doc, "Send to Bank")


def _clear_receivable(doc):
	if not doc.get("cleared_date"):
		doc.cleared_date = date.today()
		doc.save(ignore_permissions=True)
	return _apply_action(doc, "Clear Cheque")


def _refs_snapshot(pdc_name: str) -> dict[str, dict]:
	rows = frappe.get_all(
		"PDC Journal Reference",
		filters={"parent": pdc_name, "parenttype": "Post Dated Cheque"},
		fields=["name", "journal_entry", "purpose", "pdc_transition_key"],
		order_by="creation asc",
	)
	return {r.name: r for r in rows}


def _assert_new_refs(
	test: unittest.TestCase,
	pdc_name: str,
	before: dict[str, dict],
	*,
	expected_new: int,
	transition_key_contains: str | None = None,
):
	after = _refs_snapshot(pdc_name)
	new_names = set(after) - set(before)
	test.assertEqual(
		len(new_names), expected_new, msg=f"expected {expected_new} new PDC Journal Reference rows"
	)
	if expected_new and transition_key_contains:
		for name in new_names:
			key = after[name].pdc_transition_key or ""
			test.assertIn(transition_key_contains, key, msg=f"unexpected transition key {key!r}")
	return [after[n] for n in new_names]


def _je_names_for_pdc(pdc_name: str) -> list[str]:
	return [r.journal_entry for r in _refs_snapshot(pdc_name).values()]


def _assert_no_orphan_rows_for_je(test: unittest.TestCase, je: str):
	docstatus = frappe.db.get_value("Journal Entry", je, "docstatus")
	if docstatus == 2:
		test.assertEqual(frappe.db.count("GL Entry", {"voucher_no": je, "is_cancelled": 0}), 0)
		test.assertEqual(frappe.db.count("Payment Ledger Entry", {"voucher_no": je, "delinked": 0}), 0)


def _assert_after_rollback(
	test: unittest.TestCase,
	pdc_name: str,
	*,
	expected_workflow: str,
	cancelled_jes: list[str] | None = None,
	leaf: str | None = None,
	leaf_expect: dict | None = None,
):
	doc = frappe.get_doc("Post Dated Cheque", pdc_name)
	test.assertEqual(doc.workflow_state, expected_workflow)
	expected_status = map_workflow_state_to_cheque_status(doc.cheque_direction, expected_workflow)
	test.assertEqual(doc.cheque_status, expected_status)

	report = sql_verify_pdc_rollback_integrity(pdc_name, cancelled_journal_entries=cancelled_jes or [])
	test.assertTrue(sql_integrity_is_clean(report, cancelled_jes or []))
	if report.get("version", 0) == 0:
		test.assertGreaterEqual(report.get("comment", 0), 0)

	for je in cancelled_jes or []:
		test.assertEqual(frappe.db.get_value("Journal Entry", je, "docstatus"), 2)
		_assert_no_orphan_rows_for_je(test, je)
		test.assertEqual(
			frappe.db.count("PDC Journal Reference", {"parent": pdc_name, "journal_entry": je}), 0
		)

	if leaf and leaf_expect:
		leaf_row = frappe.db.get_value("Cheque Leaf", leaf, list(leaf_expect.keys()), as_dict=True)
		for key, val in leaf_expect.items():
			raw = leaf_row.get(key)
			got = (raw or "").strip() if isinstance(raw, str) or raw is None else raw
			exp = (val or "").strip() if isinstance(val, str) or val is None else val
			test.assertEqual(got, exp, msg=f"Cheque Leaf {key}")

	for row in doc.workflow_rollback_logs or []:
		test.assertTrue(row.rolled_back_on)
		test.assertTrue(row.rolled_back_by)
		test.assertTrue(row.from_state)
		test.assertTrue(row.to_state)
		test.assertTrue(row.reason)


class TestPDCWorkflowRollbackLifecycleIntegration(unittest.TestCase):
	"""Mandatory integration lifecycle tests (real docs + DB)."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")

	def _existing_party(self, doctype: str) -> str:
		"""Reuse site parties — avoids tax_id / duplicate-name fixture collisions."""
		name = frappe.db.get_value(doctype, {"disabled": 0}, "name", order_by="modified desc")
		if not name:
			name = frappe.db.get_value(doctype, {}, "name", order_by="creation asc")
		if not name:
			raise AssertionError(f"No {doctype} on site for integration tests")
		return name

	def _make_payable_pdc(self, company: str, bank_account: str, leaf: str, pool: str, ap: str) -> str:
		supplier = self._existing_party("Supplier")
		cheque_no = frappe.db.get_value("Cheque Leaf", leaf, "cheque_number")
		pdc = frappe.new_doc("Post Dated Cheque")
		pdc.naming_series = "PDC-.YYYY.-"
		pdc.company = company
		pdc.cheque_direction = "Payable"
		pdc.allocation_mode = "direct_settlement"
		pdc.advance_scope = "order_based"
		pdc.party_type = "Supplier"
		pdc.party = supplier
		pdc.cheque_no = cheque_no
		pdc.cheque_due_date = date.today() + timedelta(days=30)
		pdc.received_date = date.today()
		pdc.cheque_amount = 1000
		pdc.bank_account = bank_account
		pdc.cheque_leaf = leaf
		pdc.account_paid_from = pool
		pdc.account_paid_to = ap
		pdc.insert(ignore_permissions=True)
		return pdc.name

	def _make_receivable_pdc(self, company: str, bank_account: str, ar: str) -> str:
		customer = self._existing_party("Customer")
		pdc = frappe.new_doc("Post Dated Cheque")
		pdc.naming_series = "PDC-.YYYY.-"
		pdc.company = company
		pdc.cheque_direction = "Receivable"
		pdc.allocation_mode = "direct_settlement"
		pdc.advance_scope = "order_based"
		pdc.party_type = "Customer"
		pdc.party = customer
		pdc.cheque_no = str(int(time.time()) % 900000 + 100000)
		pdc.cheque_due_date = date.today() + timedelta(days=30)
		pdc.received_date = date.today()
		pdc.cheque_amount = 1200
		pdc.bank_account = bank_account
		bank = frappe.db.get_value("Bank", {}, "name", order_by="creation asc")
		if bank:
			pdc.drawer_bank_name = bank
		pdc.account_paid_from = ar
		# Receivable register typically requires Cheques in Hand on account_paid_to.
		settings = frappe.db.get_value(
			"PDC Settings",
			{"company": company},
			"default_cheques_in_hand_account",
		)
		if settings:
			pdc.account_paid_to = settings
		pdc.insert(ignore_permissions=True)
		return pdc.name

	def _assert_common_after_step(self, pdc_name: str):
		doc = frappe.get_doc("Post Dated Cheque", pdc_name)
		self.assertEqual(
			doc.cheque_status,
			map_workflow_state_to_cheque_status(doc.cheque_direction, doc.workflow_state),
		)

	def test_payable_full_lifecycle_and_rollback(self):
		company = _get_company()
		bank_account = _get_bank_account(company)

		assets = _get_group_account(company, "Asset")
		liab = _get_group_account(company, "Liability")
		ci_hand = _get_or_create_account(company, assets, _uniq("PDC-CIH"))
		ci_clear = _get_or_create_account(company, assets, _uniq("PDC-CLR"))
		protested = _get_or_create_account(company, assets, _uniq("PDC-PROT"))
		pool = _get_or_create_account(company, liab, _uniq("PDC-POOL"))
		ap = _get_or_create_account(company, liab, _uniq("PDC-AP"))
		_ensure_pdc_settings(company, ci_hand=ci_hand, ci_clear=ci_clear, pool=pool, protested=protested)

		leaf = _provision_payable_leaf(company, bank_account)
		pdc_name = self._make_payable_pdc(company, bank_account, leaf, pool, ap)

		# Draft → Registered (submits + posts register JE)
		refs0 = _refs_snapshot(pdc_name)
		doc = frappe.get_doc("Post Dated Cheque", pdc_name)
		doc = _apply_action(doc, "Register Cheque")
		doc.reload()
		self.assertEqual(doc.docstatus, 1)
		self.assertEqual(doc.workflow_state, "Registered")
		reg_rows = _assert_new_refs(
			self, pdc_name, refs0, expected_new=1, transition_key_contains="|Draft|Registered"
		)
		register_je = reg_rows[0].journal_entry
		self.assertTrue(frappe.db.get_value("Journal Entry", register_je, "docstatus") == 1)
		leaf_row = frappe.db.get_value(
			"Cheque Leaf", leaf, ["status", "linked_post_dated_cheque", "reserved_by_pdc"], as_dict=True
		)
		self.assertEqual(leaf_row.status, "Used")
		self.assertEqual((leaf_row.linked_post_dated_cheque or "").strip(), pdc_name)

		# Registered → Issued (no JE)
		refs_reg = _refs_snapshot(pdc_name)
		doc = _issue_payable(doc)
		doc.reload()
		self.assertEqual(doc.workflow_state, "Issued")
		_assert_new_refs(self, pdc_name, refs_reg, expected_new=0)

		# Issued → Cleared (posts clear JE)
		refs_issued = _refs_snapshot(pdc_name)
		doc = _clear_payable(doc)
		doc.reload()
		self.assertEqual(doc.workflow_state, "Cleared")
		clear_rows = _assert_new_refs(
			self, pdc_name, refs_issued, expected_new=1, transition_key_contains="|Issued|Cleared"
		)
		clear_je = clear_rows[0].journal_entry
		self.assertEqual(frappe.db.get_value("Journal Entry", clear_je, "docstatus"), 1)

		# Rollback Cleared → Issued
		prev = get_pdc_workflow_rollback_preview(pdc_name, "Issued")
		self.assertEqual(prev["current_state"], "Cleared")
		out = rollback_workflow_state(pdc_name, "Issued", "integration rollback 1")
		self.assertEqual(out["workflow_state"], "Issued")
		_assert_after_rollback(
			self,
			pdc_name,
			expected_workflow="Issued",
			cancelled_jes=[clear_je],
			leaf=leaf,
		)

		# Rollback Issued → Registered (no JE to cancel; just state)
		out = rollback_workflow_state(pdc_name, "Registered", "integration rollback 2")
		self.assertEqual(out["workflow_state"], "Registered")
		_assert_after_rollback(self, pdc_name, expected_workflow="Registered", leaf=leaf)

		# Rollback Registered → Draft (cancel register JE; docstatus becomes 0; leaf reserved)
		out = rollback_workflow_state(pdc_name, "Draft", "integration rollback 3")
		self.assertEqual(out["workflow_state"], "Draft")
		doc.reload()
		self.assertEqual(doc.docstatus, 0)
		_assert_after_rollback(
			self,
			pdc_name,
			expected_workflow="Draft",
			cancelled_jes=[clear_je, register_je],
			leaf=leaf,
			leaf_expect={
				"status": "Reserved",
				"reserved_by_pdc": pdc_name,
				"linked_post_dated_cheque": "",
			},
		)

		self._assert_common_after_step(pdc_name)
		self.assertGreaterEqual(
			len(frappe.get_doc("Post Dated Cheque", pdc_name).workflow_rollback_logs or []), 3
		)

	def test_receivable_full_lifecycle_and_rollback(self):
		company = _get_company()
		bank_account = _get_bank_account(company)

		assets = _get_group_account(company, "Asset")
		ci_hand = _get_or_create_account(company, assets, _uniq("PDC-CIH-R"))
		ci_clear = _get_or_create_account(company, assets, _uniq("PDC-CLR-R"))
		protested = _get_or_create_account(company, assets, _uniq("PDC-PROT-R"))
		ar = _get_or_create_account(company, assets, _uniq("PDC-AR"))
		_ensure_pdc_settings(company, ci_hand=ci_hand, ci_clear=ci_clear, pool=ci_hand, protested=protested)

		pdc_name = self._make_receivable_pdc(company, bank_account, ar)

		doc = frappe.get_doc("Post Dated Cheque", pdc_name)
		refs0 = _refs_snapshot(pdc_name)
		doc = _apply_action(doc, "Register Cheque")
		doc.reload()
		self.assertEqual(doc.workflow_state, "Registered")
		reg_rows = _assert_new_refs(
			self, pdc_name, refs0, expected_new=1, transition_key_contains="|Draft|Registered"
		)
		reg_je = [reg_rows[0].journal_entry]

		refs_reg = _refs_snapshot(pdc_name)
		doc = _send_receivable_to_bank(doc)
		doc.reload()
		self.assertEqual(doc.workflow_state, "Sent to Bank")
		stb_rows = _assert_new_refs(
			self,
			pdc_name,
			refs_reg,
			expected_new=1,
			transition_key_contains="|Registered|Sent to Bank",
		)
		stb_je_name = stb_rows[0].journal_entry

		refs_stb = _refs_snapshot(pdc_name)
		doc = _clear_receivable(doc)
		doc.reload()
		self.assertEqual(doc.workflow_state, "Cleared")
		clear_rows = _assert_new_refs(
			self,
			pdc_name,
			refs_stb,
			expected_new=1,
			transition_key_contains="|Sent to Bank|Cleared",
		)
		clear_je_name = clear_rows[0].journal_entry

		# Cleared → Sent to Bank
		out = rollback_workflow_state(pdc_name, "Sent to Bank", "recv rollback 1")
		self.assertEqual(out["workflow_state"], "Sent to Bank")
		_assert_after_rollback(
			self,
			pdc_name,
			expected_workflow="Sent to Bank",
			cancelled_jes=[clear_je_name],
		)

		# Sent to Bank → Registered
		out = rollback_workflow_state(pdc_name, "Registered", "recv rollback 2")
		self.assertEqual(out["workflow_state"], "Registered")
		_assert_after_rollback(
			self,
			pdc_name,
			expected_workflow="Registered",
			cancelled_jes=[clear_je_name, stb_je_name],
		)

		# Registered → Draft
		reg_je_name = reg_je[0]
		out = rollback_workflow_state(pdc_name, "Draft", "recv rollback 3")
		self.assertEqual(out["workflow_state"], "Draft")
		doc.reload()
		self.assertEqual(doc.docstatus, 0)
		_assert_after_rollback(
			self,
			pdc_name,
			expected_workflow="Draft",
			cancelled_jes=[clear_je_name, stb_je_name, reg_je_name],
		)

		self._assert_common_after_step(pdc_name)


def run_payable_lifecycle_integration() -> dict:
	"""Bench execute entrypoint for mandatory payable lifecycle test."""
	case = TestPDCWorkflowRollbackLifecycleIntegration()
	case.setUpClass()
	try:
		case.test_payable_full_lifecycle_and_rollback()
		return {"ok": True, "test": "payable_full_lifecycle_and_rollback"}
	except Exception as exc:
		import traceback

		frappe.log_error(title="PDC rollback payable integration")
		return {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}


def run_integration_sql_evidence() -> dict:
	"""Export before/after accounting snapshots for payable rollback steps."""
	from erpnext_extensions.cheque_management.pdc_rollback_sql_evidence import (
		accounting_snapshot_for_pdc,
	)

	frappe.set_user("Administrator")
	case = TestPDCWorkflowRollbackLifecycleIntegration()
	case.setUpClass()

	company = _get_company()
	bank_account = _get_bank_account(company)
	assets = _get_group_account(company, "Asset")
	liab = _get_group_account(company, "Liability")
	ci_hand = _get_or_create_account(company, assets, _uniq("SQL-EV-CIH"))
	ci_clear = _get_or_create_account(company, assets, _uniq("SQL-EV-CLR"))
	protested = _get_or_create_account(company, assets, _uniq("SQL-EV-PROT"))
	pool = _get_or_create_account(company, liab, _uniq("SQL-EV-POOL"))
	ap = _get_or_create_account(company, liab, _uniq("SQL-EV-AP"))
	_ensure_pdc_settings(company, ci_hand=ci_hand, ci_clear=ci_clear, pool=pool, protested=protested)
	leaf = _provision_payable_leaf(company, bank_account)
	pdc_name = case._make_payable_pdc(company, bank_account, leaf, pool, ap)
	doc = _apply_action(frappe.get_doc("Post Dated Cheque", pdc_name), "Register Cheque")
	doc = _issue_payable(doc)
	doc = _clear_payable(doc)

	steps: list[dict] = []

	def record(label: str, target: str, reason: str):
		before = accounting_snapshot_for_pdc(pdc_name)
		rollback_workflow_state(pdc_name, target, reason)
		after = accounting_snapshot_for_pdc(pdc_name)
		steps.append({"label": label, "target": target, "before": before, "after": after})

	record("Cleared_to_Issued", "Issued", "SQL evidence Cleared→Issued")
	record("Issued_to_Registered", "Registered", "SQL evidence Issued→Registered")
	record("Registered_to_Draft", "Draft", "SQL evidence Registered→Draft")
	frappe.db.commit()
	return {"ok": True, "pdc_name": pdc_name, "steps": steps}


def run_receivable_lifecycle_integration() -> dict:
	case = TestPDCWorkflowRollbackLifecycleIntegration()
	case.setUpClass()
	try:
		case.test_receivable_full_lifecycle_and_rollback()
		return {"ok": True, "test": "receivable_full_lifecycle_and_rollback"}
	except Exception as exc:
		frappe.log_error(title="PDC rollback receivable integration")
		return {"ok": False, "error": str(exc)}
