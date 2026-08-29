# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""Tests for PM Clearance settlement + PM Request allocation (real ``build_clearance_je_accounts`` / JE insert).

Cheque-management style: lives under ``petty_management/tests/``. Uses ``_Test Company`` when present;
otherwise the first Company on the site and its bank/supplier/item links.

**Import note:** PM Clearance helpers are imported **inside** test methods or via ``_pm()`` so this module
does not pull ERPNext accounting code during Frappe test discovery (avoids fiscal-year bootstrap errors).

Run from bench root (recommended: **module only**; do not combine ``--lightmode`` with ``--app`` — lightmode
prioritizes ``--app`` and loads every ``test_*.py`` in the app)::

    bench --site development.localhost run-tests \\
        --module erpnext_extensions.petty_management.tests.test_pm_clearance \\
        --skip-before-tests

Shim path (same tests via star-import)::

    bench --site development.localhost run-tests \\
        --module erpnext_extensions.petty_management.doctype.pm_clearance.test_pm_clearance \\
        --skip-before-tests

**Site requirements:** valid fiscal years for the chosen company; Payment Entry / Purchase Invoice helpers
may raise ``NameError`` (fiscal overlap) if the site has overlapping ``Fiscal Year`` rows (e.g. Gregorian vs Jalali).
Lightmode (single-module import, no full type-validator walk)::

    bench --site development.localhost run-tests \\
        --module erpnext_extensions.petty_management.tests.test_pm_clearance \\
        --lightmode

If discovery fails with overlapping fiscal year / ``before_tests`` errors, add ``--skip-before-tests``.
"""

from __future__ import annotations

import unittest

import frappe
from frappe.exceptions import ValidationError
from frappe.utils import cint, flt, today

from erpnext_extensions.petty_management.utils import get_pm_settings

# Resolved in ``_ensure_company_context`` (setUpClass).
COMPANY = ""
PETTY_ACCOUNT = ""
BANK_ACCOUNT = ""


def _pm():
	"""Lazy import of ``pm_clearance`` module (avoid import-time side effects)."""
	from erpnext_extensions.petty_management.doctype.pm_clearance import pm_clearance as mod

	return mod


def _ensure_approval_settings() -> None:
	"""v4.0.2: stamp service requires CEO/Finance Users on PM Settings for submit."""
	settings = get_pm_settings()
	if not settings:
		return
	admin = "Administrator"
	if not getattr(settings, "ceo_approver", None):
		settings.db_set("ceo_approver", admin, update_modified=False)
	if not getattr(settings, "finance_manager", None):
		settings.db_set("finance_manager", admin, update_modified=False)
	if not getattr(settings, "finance_supervisor", None):
		settings.db_set("finance_supervisor", admin, update_modified=False)
	if getattr(settings, "require_named_manager_approver", None) is None:
		settings.db_set("require_named_manager_approver", 1, update_modified=False)


def _ensure_company_context() -> None:
	"""Set module-level COMPANY / PETTY_ACCOUNT / BANK_ACCOUNT (idempotent)."""
	global COMPANY, PETTY_ACCOUNT, BANK_ACCOUNT
	_ensure_approval_settings()
	if COMPANY:
		return
	if frappe.db.exists("Company", "_Test Company"):
		COMPANY = "_Test Company"
	else:
		names = frappe.get_all("Company", pluck="name", limit=1)
		if not names:
			return
		COMPANY = names[0]
	abbr = frappe.db.get_value("Company", COMPANY, "abbr")
	PETTY_ACCOUNT = f"Petty Cash PM Test Payable - {abbr}"
	BANK_ACCOUNT = frappe.db.get_value("Company", COMPANY, "default_bank_account")
	if not BANK_ACCOUNT:
		row = frappe.db.sql(
			"""
			select name from `tabAccount`
			where company=%s and ifnull(is_group,0)=0 and account_type in ('Bank', 'Cash')
			limit 1
			""",
			COMPANY,
		)
		BANK_ACCOUNT = row[0][0] if row else ""


def _petty_parent_account() -> str:
	abbr = frappe.db.get_value("Company", COMPANY, "abbr")
	for name in (
		f"Current Assets - {abbr}",
		f"Application of Funds (Assets) - {abbr}",
	):
		if frappe.db.exists("Account", name):
			return name
	parent_from_bank = frappe.db.sql(
		"""
		select parent_account from `tabAccount`
		where company=%s and account_type in ('Bank', 'Cash') and ifnull(is_group,0)=0 and disabled=0
		limit 1
		""",
		COMPANY,
	)
	if parent_from_bank and parent_from_bank[0][0] and frappe.db.exists("Account", parent_from_bank[0][0]):
		return parent_from_bank[0][0]
	group = frappe.db.sql(
		"""
		select name from `tabAccount`
		where company=%s and ifnull(is_group,0)=1 and disabled=0
		order by lft asc
		limit 1
		""",
		COMPANY,
	)
	if group:
		return group[0][0]
	return f"Current Assets - {abbr}"


def _insert_leaf_account(account_name: str, parent_account: str, account_type: str) -> str:
	"""Create a leaf Account without importing ``test_account`` (avoids BootStrapTestData / fiscal-year bootstrap)."""
	abbr = frappe.db.get_value("Company", COMPANY, "abbr")
	full_name = f"{account_name} - {abbr}"
	if frappe.db.exists("Account", full_name):
		return full_name
	doc = frappe.new_doc("Account")
	doc.account_name = account_name
	doc.parent_account = parent_account
	doc.company = COMPANY
	doc.account_type = account_type
	doc.is_group = 0
	doc.insert(ignore_permissions=True)
	return doc.name


def _ensure_petty_account() -> str:
	if frappe.db.exists("Account", PETTY_ACCOUNT):
		return PETTY_ACCOUNT
	parent = _petty_parent_account()
	return _insert_leaf_account("Petty Cash PM Test Payable", parent, "Payable")


def _workflow_state_for(document_type: str, state_title: str) -> str | None:
	wf_name = frappe.db.get_value(
		"Workflow",
		{"document_type": document_type, "is_active": 1},
		"name",
	)
	if not wf_name:
		return None
	wf = frappe.get_doc("Workflow", wf_name)
	for s in wf.states:
		title = frappe.db.get_value("Workflow State", s.state, "workflow_state_name")
		if title == state_title:
			return s.state
	return None


def _finance_clear_pm_request(req_name: str, *, status: str = "Waiting for Payment") -> None:
	"""Mark PM Request as finance-cleared (workflow Finance Approved; business status as given)."""
	from erpnext_extensions.petty_management.services.workflow_utils import resolve_workflow_state_link

	ws = _workflow_state_for("PM Request", "Finance Approved") or resolve_workflow_state_link(
		"Finance Approved"
	)
	if not ws:
		# Legacy sites mid-migration
		ws = _workflow_state_for("PM Request", "Waiting for Payment") or resolve_workflow_state_link(
			"Waiting for Payment"
		)
	if not ws:
		raise RuntimeError("PM Request Finance Approved workflow state missing")
	frappe.db.set_value(
		"PM Request",
		req_name,
		{"workflow_state": ws, "status": status},
		update_modified=False,
	)


def _stamp_and_apply_request_approvals(req_name: str, user: str | None = None) -> None:
	"""Apply Manager→CEO→Finance as a single user (tests / smoke).

	When Manager=CEO=Finance, v4.1.4 auto-skip may advance through all Approves after the
	first click — only apply remaining actions while still pending.
	"""
	from erpnext_extensions.petty_management.services.workflow_utils import apply_pm_workflow

	user = user or frappe.session.user
	frappe.db.set_value(
		"PM Request",
		req_name,
		{
			"manager_approver": user,
			"ceo_approver": user,
			"finance_approver": user,
		},
		update_modified=False,
	)
	# Administrator is not stamped; apply as the stamped user so transitions + auto-skip work.
	prev = frappe.session.user
	try:
		if user != "Administrator":
			frappe.set_user(user)
		doc = frappe.get_doc("PM Request", req_name)
		title = (
			frappe.db.get_value("Workflow State", doc.workflow_state, "workflow_state_name")
			or doc.workflow_state
			or ""
		)
		if title == "Pending Manager Approval":
			apply_pm_workflow(doc, "PM Manager Approve")
			doc.reload()
			title = (
				frappe.db.get_value("Workflow State", doc.workflow_state, "workflow_state_name")
				or doc.workflow_state
				or ""
			)
		if title == "Pending CEO Approval":
			apply_pm_workflow(doc, "PM CEO Approve")
			doc.reload()
			title = (
				frappe.db.get_value("Workflow State", doc.workflow_state, "workflow_state_name")
				or doc.workflow_state
				or ""
			)
		if title == "Pending Finance Approval":
			apply_pm_workflow(doc, "PM Finance Approve")
	finally:
		frappe.set_user(prev)


def _stamp_and_apply_clearance_approvals(cl_name: str, user: str | None = None) -> None:
	"""Apply Manager→Finance as a single user (tests / smoke).

	Auto-skip may finish the chain after Manager Approve when stamps match.
	"""
	from erpnext_extensions.petty_management.services.workflow_utils import apply_pm_workflow

	user = user or frappe.session.user
	frappe.db.set_value(
		"PM Clearance",
		cl_name,
		{"manager_approver": user, "finance_approver": user},
		update_modified=False,
	)
	prev = frappe.session.user
	try:
		if user != "Administrator":
			frappe.set_user(user)
		doc = frappe.get_doc("PM Clearance", cl_name)
		title = (
			frappe.db.get_value("Workflow State", doc.workflow_state, "workflow_state_name")
			or doc.workflow_state
			or ""
		)
		if title == "Pending Manager Approval":
			apply_pm_workflow(doc, "PM Manager Approve")
			doc.reload()
			title = (
				frappe.db.get_value("Workflow State", doc.workflow_state, "workflow_state_name")
				or doc.workflow_state
				or ""
			)
		if title == "Pending Finance Review":
			try:
				apply_pm_workflow(doc, "PM Finance Approve")
			except Exception:
				doc.reload()
				apply_pm_workflow(doc, "PM Approve")
	finally:
		frappe.set_user(prev)


def _approve_pm_clearance_for_reservation(cl_name: str) -> None:
	"""Test/smoke fixture: reach Approved via legitimate workflow only (no raw docstatus).

	Walks Draft → Pending Manager → Pending Finance Review → Finance Approve submit.
	"""
	from erpnext_extensions.petty_management.services.workflow_utils import apply_pm_workflow

	doc = frappe.get_doc("PM Clearance", cl_name)
	title = (
		frappe.db.get_value("Workflow State", doc.workflow_state, "workflow_state_name")
		or doc.workflow_state
		or ""
	)
	if title == "Approved" and cint(doc.docstatus) == 1:
		return

	user = frappe.session.user
	if title in ("", "Draft"):
		# Ensure manager stamp exists for Submit → Pending Manager
		if not (doc.manager_approver or "").strip():
			frappe.db.set_value(
				"PM Clearance", cl_name, "manager_approver", user, update_modified=False
			)
		apply_pm_workflow(frappe.get_doc("PM Clearance", cl_name), "PM Submit Finance Review")
		doc.reload()

	_stamp_and_apply_clearance_approvals(cl_name)
	doc = frappe.get_doc("PM Clearance", cl_name)
	if cint(doc.docstatus) != 1:
		frappe.throw(f"Test fixture failed to submit clearance {cl_name} via Finance Approve")


def _default_cost_center() -> str | None:
	cc = frappe.db.get_value("Company", COMPANY, "cost_center")
	if cc and frappe.db.exists("Cost Center", cc):
		return cc
	r = frappe.get_all(
		"Cost Center",
		filters={"company": COMPANY, "disabled": 0, "is_group": 0},
		pluck="name",
		limit=1,
	)
	return r[0] if r else None


def _default_expense_account_for_item(item_code: str) -> str | None:
	row = frappe.db.sql(
		"select expense_account from `tabItem Default` where parent=%s and company=%s limit 1",
		(item_code, COMPANY),
	)
	if row and row[0][0]:
		return row[0][0]
	row2 = frappe.db.sql("select default_expense_account from `tabCompany` where name=%s", (COMPANY,))
	return row2[0][0] if row2 and row2[0][0] else None


def _company_fallback_expense_account() -> str | None:
	"""Leaf expense account when Item Default / Company.default_expense_account are empty."""
	row = frappe.db.sql(
		"""
		select name from `tabAccount`
		where company=%s and ifnull(disabled,0)=0 and ifnull(is_group,0)=0
			and account_type in ('Expense Account', 'Cost of Goods Sold')
		limit 1
		""",
		(COMPANY,),
	)
	return row[0][0] if row else None


def _first_department_for_company() -> str | None:
	r = frappe.get_all("Department", filters={"company": COMPANY}, pluck="name", limit=1)
	if r:
		return r[0]
	row = frappe.db.sql("select name from `tabDepartment` limit 1")
	return row[0][0] if row else None


def _make_employee() -> str:
	"""Create Employee without ERPNext ``test_employee`` (that import pulls in BootStrapTestData / fiscal-year seeding)."""
	dept = _first_department_for_company()
	if not dept:
		raise unittest.SkipTest("No Department found; cannot create test Employee.")
	key = (frappe.generate_hash(length=8) + "_pm")[:20]
	doc = frappe.new_doc("Employee")
	doc.first_name = key
	doc.company = COMPANY
	doc.department = dept
	doc.date_of_joining = today()
	doc.date_of_birth = "1990-01-01"
	doc.status = "Active"
	doc.gender = "Male"
	# v4.0.2 manager stamp: Employee.expense_approver (User)
	if frappe.db.exists("User", "Administrator"):
		doc.expense_approver = "Administrator"
	doc.insert(ignore_permissions=True)
	return doc.name


def _pm_holder_primary_key(employee: str, company: str) -> str:
	"""Match ``PMHolder.autoname`` (employee-company, max 120)."""
	base = f"{employee}-{company}"
	return base[:120] if len(base) > 120 else base


def _sync_holder_petty_and_balance(name: str) -> None:
	mb = frappe.db.get_value("PM Holder", name, "max_balance")
	if mb is None or flt(mb) <= 0:
		frappe.db.set_value("PM Holder", name, "max_balance", 100_000_000, update_modified=False)
	petty_cur = (frappe.db.get_value("PM Holder", name, "petty_cash_account") or "").strip()
	if petty_cur != PETTY_ACCOUNT:
		frappe.db.set_value("PM Holder", name, "petty_cash_account", PETTY_ACCOUNT, update_modified=False)


def _make_holder(employee: str) -> str:
	petty = _ensure_petty_account()
	pk = _pm_holder_primary_key(employee, COMPANY)
	if frappe.db.exists("PM Holder", pk):
		_sync_holder_petty_and_balance(pk)
		return pk
	if frappe.db.exists("PM Holder", {"employee": employee, "company": COMPANY}):
		name = frappe.db.get_value("PM Holder", {"employee": employee, "company": COMPANY}, "name")
		_sync_holder_petty_and_balance(name)
		return name
	h = frappe.new_doc("PM Holder")
	h.employee = employee
	h.company = COMPANY
	h.petty_cash_account = petty
	h.max_balance = 100_000_000
	try:
		h.insert()
	except Exception as exc:
		if "duplicate entry" not in str(exc).lower():
			raise
		row = frappe.db.sql("select name from `tabPM Holder` where name=%s", (pk,))
		name = (
			row[0][0]
			if row
			else frappe.db.get_value("PM Holder", {"employee": employee, "company": COMPANY}, "name")
		)
		if not name:
			raise
		_sync_holder_petty_and_balance(name)
		return name
	return h.name


def _fund_pm_request(employee: str, amount: float) -> tuple[str, str]:
	from erpnext_extensions.petty_management.services.request_service import _build_payment_entry

	petty = _ensure_petty_account()
	req = frappe.new_doc("PM Request")
	req.company = COMPANY
	req.employee = employee
	req.transaction_date = today()
	req.append("details", {"advance_amount": amount})
	req.insert()
	req.submit()
	req.reload()

	pe = _build_payment_entry(req, BANK_ACCOUNT, amount)
	pe.insert(ignore_permissions=True)
	pe.submit()

	req.db_set("payment_entry", pe.name, update_modified=False)
	req.db_set("payment_status", "Paid", update_modified=False)
	req.db_set("status", "Paid", update_modified=False)
	# Commit + cache flush: holder paid SQL joins PM Request + submitted PE; rare
	# cross-request timing in heavy suites could read ``holder``/``paid`` as zero before flush.
	frappe.db.commit()
	frappe.clear_cache(doctype="PM Request")
	frappe.clear_cache(doctype="Payment Entry")
	return req.name, pe.name


def _make_pi_outstanding(amount: float):
	"""Build a draft Purchase Invoice without importing ERPNext ``test_purchase_invoice``."""

	supplier = "_Test Supplier" if frappe.db.exists("Supplier", "_Test Supplier") else None
	if not supplier:
		suppliers = frappe.get_all("Supplier", pluck="name", limit=1)
		if not suppliers:
			raise unittest.SkipTest("No Supplier on site; cannot create Purchase Invoice.")
		supplier = suppliers[0]
	item_code = _purchase_item_code()

	wh = _default_warehouse_for_company()
	if not wh:
		raise unittest.SkipTest("No warehouse for Purchase Invoice in this company.")

	expense_account = _default_expense_account_for_item(item_code) or _company_fallback_expense_account()
	if not expense_account:
		raise unittest.SkipTest(
			"No expense account resolved for item/company; cannot create Purchase Invoice."
		)

	cc = _default_cost_center()
	item_doc = frappe.get_cached_doc("Item", item_code)
	uom = item_doc.stock_uom or "Nos"

	pi = frappe.new_doc("Purchase Invoice")
	pi.company = COMPANY
	pi.supplier = supplier
	pi.posting_date = today()
	pi.currency = frappe.db.get_value("Company", COMPANY, "default_currency") or "IRR"
	pi.conversion_rate = 1
	pi.bill_no = "PM-PI-" + frappe.generate_hash(length=8)
	if cc:
		pi.cost_center = cc
	pi.append(
		"items",
		{
			"item_code": item_code,
			"qty": 1,
			"rate": amount,
			"warehouse": wh,
			"expense_account": expense_account,
			"cost_center": cc,
			"uom": uom,
			"stock_uom": uom,
			"conversion_factor": 1,
		},
	)
	return pi


def _insert_legacy_allocation_row(parent: str, total: float) -> None:
	"""Same shape as migration patch (submitted-parent path)."""
	name = frappe.generate_hash(length=10)
	user = "Administrator"
	frappe.db.sql(
		"""
		INSERT INTO `tabPM Clearance Request Allocation`
		(`name`, `creation`, `modified`, `modified_by`, `owner`, `docstatus`,
		 `parent`, `parenttype`, `parentfield`, `idx`,
		 `is_legacy_row`, `allocated_amount`, `request_amount`, `paid_amount`,
		 `previously_allocated_amount`, `available_amount`, `pm_request`)
		VALUES
		(%s, NOW(), NOW(), %s, %s, 0,
		 %s, 'PM Clearance', 'request_allocations', 1,
		 1, %s, 0, 0, 0, 0, NULL)
		""",
		(name, user, user, parent, total),
	)


def _purchase_invoice_pm_scalar_values(pi_name: str) -> dict[str, str | None]:
	meta = frappe.get_meta("Purchase Invoice")
	fields = ("custom_pm_request", "custom_pm_clearance", "custom_pm_holder")
	return {
		fieldname: frappe.db.get_value("Purchase Invoice", pi_name, fieldname)
		for fieldname in fields
		if meta.has_field(fieldname)
	}


def _default_warehouse_for_company() -> str | None:
	if frappe.db.has_column("Company", "default_warehouse"):
		w = frappe.db.get_value("Company", COMPANY, "default_warehouse")
		if w and frappe.db.exists("Warehouse", w):
			return w
	row = frappe.db.sql(
		"""
		select name from `tabWarehouse`
		where company=%s and ifnull(disabled,0)=0
		limit 1
		""",
		COMPANY,
	)
	return row[0][0] if row else None


def _purchase_item_code() -> str:
	"""Pick an Item that can appear on Purchase Order / Purchase Invoice."""
	if frappe.db.exists("Item", "_Test Item") and cint(
		frappe.db.get_value("Item", "_Test Item", "is_purchase_item")
	):
		return "_Test Item"
	items = frappe.get_all(
		"Item",
		filters={"disabled": 0, "is_purchase_item": 1},
		pluck="name",
		limit=1,
	)
	if not items:
		raise unittest.SkipTest("No purchase-enabled Item on site.")
	return items[0]


def _supplier_advance_test_account() -> str:
	"""Return a proper Payable (party-capable) supplier-advance ledger account.

	The settlement JE debits this account with a Supplier party and links it to the
	Purchase Order as an advance; ERPNext only reconciles that correctly for a
	``Payable`` account, so we must never return an arbitrary leaf account.
	"""
	acc = frappe.db.get_value("Company", COMPANY, "default_advance_paid_account")
	if acc and frappe.db.get_value("Account", acc, "account_type") == "Payable":
		return acc

	# Reuse a deterministic leaf we created earlier if present.
	abbr = frappe.db.get_value("Company", COMPANY, "abbr")
	existing = f"PM Test Supplier Advance - {abbr}"
	if frappe.db.exists("Account", existing):
		return existing

	# Create under a real parent group (Payable/Asset-advance parents preferred).
	candidate_parents = [
		"1600 - Loans and Advances (Assets)",
		"Loans and Advances (Assets)",
		"Creditors",
		"Accounts Payable",
		"Current Liabilities",
	]
	for label in candidate_parents:
		parent = f"{label} - {abbr}"
		if frappe.db.exists("Account", parent) and cint(frappe.db.get_value("Account", parent, "is_group")):
			return _insert_leaf_account("PM Test Supplier Advance", parent, "Payable")

	# Fallback: reuse any existing Payable ledger account in the company.
	payable = frappe.get_all(
		"Account",
		filters={"company": COMPANY, "account_type": "Payable", "is_group": 0},
		pluck="name",
		limit=1,
	)
	if payable:
		return payable[0]
	raise unittest.SkipTest("No Payable account available for supplier advance test account.")


def _make_purchase_order_for_company(qty: float = 5, rate: float = 1000):
	wh = _default_warehouse_for_company()
	if not wh:
		raise unittest.SkipTest("No warehouse for Purchase Order in this company.")

	supplier = "_Test Supplier" if frappe.db.exists("Supplier", "_Test Supplier") else None
	if not supplier:
		suppliers = frappe.get_all("Supplier", pluck="name", limit=1)
		if not suppliers:
			raise unittest.SkipTest("No Supplier on site.")
		supplier = suppliers[0]
	item_code = _purchase_item_code()

	item_doc = frappe.get_cached_doc("Item", item_code)
	uom = item_doc.stock_uom or "Nos"
	company_currency = frappe.get_cached_value("Company", COMPANY, "default_currency") or "INR"
	po = frappe.new_doc("Purchase Order")
	po.company = COMPANY
	po.supplier = supplier
	# Keep PO in company currency so advance settlement validations use
	# grand_total / base_grand_total consistently (avoids supplier-currency
	# IRR POs with tiny INR base totals on multi-currency sites).
	po.currency = company_currency
	po.conversion_rate = 1
	po.transaction_date = today()
	po.schedule_date = today()
	po.append(
		"items",
		{
			"item_code": item_code,
			"warehouse": wh,
			"qty": qty,
			"rate": rate,
			"schedule_date": today(),
			"uom": uom,
			"stock_uom": uom,
		},
	)
	po.insert(ignore_permissions=True)
	po.submit()
	return po


def _pm_clearance_detail_policy_fields() -> dict:
	"""Bill no / proof when PM Settings require them (site-specific)."""
	extras: dict = {}
	s = get_pm_settings()
	if not s:
		return extras
	if cint(getattr(s, "require_bill_no", 0)):
		extras["bill_no"] = f"PM-TEST-{frappe.generate_hash(length=8)}"
	if cint(getattr(s, "require_attachment", 0)):
		from frappe.utils.file_manager import save_file

		att = save_file(
			f"pm-proof-{frappe.generate_hash(length=6)}.txt",
			b"pm",
			"",
			"",
			is_private=0,
		)
		extras["proof"] = att.file_url
	return extras


def _append_pm_clearance_detail_row(doc, row: dict) -> None:
	doc.append("details", {**row, **_pm_clearance_detail_policy_fields()})


def _reconciliation_errors_touching_refs(company: str, refs: set[str]):
	"""Return error-level reconciliation issues whose ``references`` intersect ``refs``."""
	from erpnext_extensions.petty_management.services.reconciliation_service import reconcile

	res = reconcile(apply_safe_fixes=False, company=company)
	bad = []
	for issue in res.issues:
		if issue.severity != "error":
			continue
		for v in (issue.references or {}).values():
			if isinstance(v, str) and v in refs:
				bad.append(issue)
				break
	return bad


class TestPMClearanceAllocation(unittest.TestCase):
	"""Allocation validation, reservation, preview, settlement, legacy guards."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_ensure_company_context()
		if not COMPANY:
			raise unittest.SkipTest("No Company on site.")
		if not BANK_ACCOUNT:
			raise unittest.SkipTest(f"No bank/cash account resolved for company {COMPANY!r}.")
		_ensure_petty_account()

	def setUp(self):
		frappe.set_user("Administrator")
		self._cleanup_names: list[tuple[str, str]] = []

	def tearDown(self):
		frappe.set_user("Administrator")
		for doctype, name in reversed(self._cleanup_names):
			try:
				if doctype == "PM Request" and frappe.db.exists("PM Request", name):
					pe = frappe.db.get_value("PM Request", name, "payment_entry")
					if pe and frappe.db.exists("Payment Entry", pe):
						pe_doc = frappe.get_doc("Payment Entry", pe)
						if pe_doc.docstatus == 1:
							pe_doc.cancel()
						frappe.delete_doc("Payment Entry", pe, force=True, ignore_permissions=True)
				if doctype == "Purchase Order" and frappe.db.exists("Purchase Order", name):
					po_doc = frappe.get_doc("Purchase Order", name)
					if po_doc.docstatus == 1:
						po_doc.reload()
						po_doc.cancel()
					frappe.delete_doc("Purchase Order", name, force=True, ignore_permissions=True)
					continue
				doc = frappe.get_doc(doctype, name)
				if getattr(doc, "docstatus", 0) == 1:
					doc.reload()
					doc.cancel()
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
			except Exception:
				pass
		frappe.db.commit()

	def _track(self, doctype: str, name: str) -> None:
		self._cleanup_names.append((doctype, name))

	def _base_clearance(self, employee: str, pi, pi_amount: float):
		cl = frappe.new_doc("PM Clearance")
		cl.company = COMPANY
		cl.employee = employee
		cl.transaction_date = today()
		_append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi.name,
				"allocated_amount": pi_amount,
			},
		)
		return cl

	def test_funding_makes_pm_request_available_for_allocation(self):
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 50_000.0)
		self._track("PM Request", req_name)

		mod = _pm()
		prev = mod.sum_prior_pm_request_allocations(req_name, "__nonexistent_clearance__")
		self.assertEqual(prev, 0.0)
		prev_all = mod.sum_prior_pm_request_allocations(req_name, None)
		self.assertEqual(prev_all, 0.0)

	def test_pm_request_allocation_context_stamps_paid_request_snapshot(self):
		mod = _pm()
		from erpnext_extensions.petty_management.services import allocation_service

		frappe.is_whitelisted(mod.get_pm_request_allocation_context)
		frappe.is_whitelisted(allocation_service.get_pm_request_allocation_context)

		emp = _make_employee()
		self._track("Employee", emp)
		holder = _make_holder(emp)
		petty = frappe.db.get_value("PM Holder", holder, "petty_cash_account")
		req_name, _pe = _fund_pm_request(emp, 25_000.0)
		self._track("PM Request", req_name)

		ctx = mod.get_pm_request_allocation_context(
			req_name,
			company=COMPANY,
			employee=emp,
			holder=holder,
			petty_cash_account=petty,
		)
		self.assertEqual(ctx["pm_request"], req_name)
		self.assertEqual(flt(ctx["request_amount"]), 25_000)
		self.assertGreater(flt(ctx["paid_amount"]), 0)
		self.assertGreater(flt(ctx["available_amount"]), 0)
		self.assertEqual(ctx["employee"], emp)
		self.assertEqual(ctx["holder"], holder)
		self.assertEqual(ctx["petty_cash_account"], petty)
		self.assertEqual(ctx["company"], COMPANY)

		pi = _make_pi_outstanding(10_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 10_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 10_000})
		cl.insert()
		self._track("PM Clearance", cl.name)
		row = cl.request_allocations[0]
		self.assertEqual(flt(row.request_amount), 25_000)
		self.assertGreater(flt(row.paid_amount), 0)
		self.assertGreater(flt(row.available_amount), 0)

	def test_shared_petty_cash_account_allowed_for_two_holders(self):
		emp_a = _make_employee()
		emp_b = _make_employee()
		self._track("Employee", emp_a)
		self._track("Employee", emp_b)

		holder_a = _make_holder(emp_a)
		holder_b = _make_holder(emp_b)
		petty_a = frappe.db.get_value("PM Holder", holder_a, "petty_cash_account")
		petty_b = frappe.db.get_value("PM Holder", holder_b, "petty_cash_account")

		self.assertNotEqual(holder_a, holder_b)
		self.assertEqual(petty_a, petty_b)

	def test_pm_request_availability_is_request_based_with_shared_account(self):
		from erpnext_extensions.petty_management.services.allocation_service import (
			get_pm_request_available_amount,
		)

		emp_a = _make_employee()
		emp_b = _make_employee()
		self._track("Employee", emp_a)
		self._track("Employee", emp_b)
		holder_a = _make_holder(emp_a)
		holder_b = _make_holder(emp_b)
		self.assertEqual(
			frappe.db.get_value("PM Holder", holder_a, "petty_cash_account"),
			frappe.db.get_value("PM Holder", holder_b, "petty_cash_account"),
		)

		req_a, _pe_a = _fund_pm_request(emp_a, 10_000.0)
		req_b, _pe_b = _fund_pm_request(emp_b, 20_000.0)
		self._track("PM Request", req_a)
		self._track("PM Request", req_b)

		pi = _make_pi_outstanding(4_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp_a, pi, 4_000)
		cl.append("request_allocations", {"pm_request": req_a, "allocated_amount": 4_000})
		cl.insert()
		cl.submit()
		self._track("PM Clearance", cl.name)
		_approve_pm_clearance_for_reservation(cl.name)

		self.assertEqual(flt(get_pm_request_available_amount(req_a)), 6_000)
		self.assertEqual(flt(get_pm_request_available_amount(req_b)), 20_000)

	def test_pm_request_query_exact_docname_uses_alias_safe_search(self):
		mod = _pm()
		emp = _make_employee()
		self._track("Employee", emp)
		holder = _make_holder(emp)
		petty = frappe.db.get_value("PM Holder", holder, "petty_cash_account")
		req_name, _pe = _fund_pm_request(emp, 12_000.0)
		self._track("PM Request", req_name)

		rows = mod.pm_request_query_for_pm_clearance(
			"PM Request",
			req_name,
			"name",
			0,
			20,
			{
				"company": COMPANY,
				"employee": emp,
				"holder": holder,
				"petty_cash_account": petty,
			},
		)
		self.assertIn(req_name, [r[0] for r in rows])

	def test_clearance_without_request_allocations_fails(self):
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		pi = _make_pi_outstanding(10_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 10_000)
		with self.assertRaises(ValidationError):
			cl.insert()

	def test_sum_mismatch_pi_vs_pm_request_fails(self):
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 50_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(10_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 10_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 9_999})
		with self.assertRaises(ValidationError) as ctx:
			cl.insert()
		self.assertIn("Total funding allocation", str(ctx.exception))

	def test_allocation_over_available_fails(self):
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 10_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(12_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 12_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 12_000})
		with self.assertRaises(ValidationError):
			cl.insert()

	def test_duplicate_pm_request_rows_fail(self):
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 100_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(20_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 20_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 10_000})
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 10_000})
		with self.assertRaises(ValidationError):
			cl.insert()

	def test_submitted_clearance_without_je_reserves_pm_request(self):
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 50_000.0)
		self._track("PM Request", req_name)

		pi1 = _make_pi_outstanding(30_000)
		pi1.insert()
		pi1.submit()
		self._track("Purchase Invoice", pi1.name)

		cl1 = self._base_clearance(emp, pi1, 30_000)
		cl1.append("request_allocations", {"pm_request": req_name, "allocated_amount": 30_000})
		cl1.insert()
		cl1.submit()
		self._track("PM Clearance", cl1.name)
		_approve_pm_clearance_for_reservation(cl1.name)

		pi2 = _make_pi_outstanding(25_000)
		pi2.insert()
		pi2.submit()
		self._track("Purchase Invoice", pi2.name)

		cl2 = self._base_clearance(emp, pi2, 25_000)
		cl2.append("request_allocations", {"pm_request": req_name, "allocated_amount": 25_000})
		with self.assertRaises(ValidationError):
			cl2.insert()

	def _submit_pi(self, pi):
		try:
			pi.insert(ignore_permissions=True)
			pi.submit()
		except TypeError as exc:
			if "do_not_round_fields" in str(exc):
				raise unittest.SkipTest(
					"Purchase Invoice submit incompatible with this Frappe version"
				) from exc
			raise

	def test_opening_advance_funding_clearance_saves(self):
		from erpnext_extensions.petty_management.services.opening_advance_service import (
			get_opening_advance_available_amount,
		)

		emp = _make_employee()
		self._track("Employee", emp)
		holder = _make_holder(emp)
		oa = frappe.new_doc("PM Opening Advance")
		oa.holder = holder
		oa.opening_date = today()
		oa.opening_source_type = "Opening Balance"
		oa.opening_advance_amount = 12_000
		oa.previously_settled_before_migration = 4_000
		oa.insert(ignore_permissions=True)
		oa.submit()
		self._track("PM Opening Advance", oa.name)
		self.assertEqual(get_opening_advance_available_amount(oa.name), 8_000)

		pi = _make_pi_outstanding(4_000)
		self._submit_pi(pi)
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 4_000)
		cl.append(
			"request_allocations",
			{
				"funding_source_type": "PM Opening Advance",
				"pm_opening_advance": oa.name,
				"allocated_amount": 4_000,
			},
		)
		cl.insert(ignore_permissions=True)
		self._track("PM Clearance", cl.name)
		row = cl.request_allocations[0]
		self.assertEqual(row.funding_source_type, "PM Opening Advance")
		self.assertFalse(row.pm_request)
		self.assertGreater(flt(row.available_amount), 0)

	def test_opening_allocation_does_not_require_pm_request_field(self):
		emp = _make_employee()
		self._track("Employee", emp)
		holder = _make_holder(emp)
		oa = frappe.new_doc("PM Opening Advance")
		oa.holder = holder
		oa.opening_date = today()
		oa.opening_source_type = "Opening Balance"
		oa.opening_advance_amount = 10_000
		oa.insert(ignore_permissions=True)
		oa.submit()
		self._track("PM Opening Advance", oa.name)
		pi = _make_pi_outstanding(2_000)
		self._submit_pi(pi)
		self._track("Purchase Invoice", pi.name)
		cl = self._base_clearance(emp, pi, 2_000)
		cl.append(
			"request_allocations",
			{
				"funding_source_type": "PM Opening Advance",
				"pm_request": "SHOULD-BE-CLEARED",
				"pm_opening_advance": oa.name,
				"allocated_amount": 2_000,
			},
		)
		cl.insert(ignore_permissions=True)
		self._track("PM Clearance", cl.name)
		self.assertFalse(cl.request_allocations[0].pm_request)

	def test_mixed_pm_request_and_opening_allocation_saves(self):
		emp = _make_employee()
		self._track("Employee", emp)
		holder = _make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 10_000.0)
		self._track("PM Request", req_name)
		oa = frappe.new_doc("PM Opening Advance")
		oa.holder = holder
		oa.opening_date = today()
		oa.opening_source_type = "Opening Balance"
		oa.opening_advance_amount = 10_000
		oa.insert(ignore_permissions=True)
		oa.submit()
		self._track("PM Opening Advance", oa.name)
		pi = _make_pi_outstanding(6_000)
		self._submit_pi(pi)
		self._track("Purchase Invoice", pi.name)
		cl = self._base_clearance(emp, pi, 6_000)
		cl.append(
			"request_allocations",
			{"funding_source_type": "PM Request", "pm_request": req_name, "allocated_amount": 3_000},
		)
		cl.append(
			"request_allocations",
			{
				"funding_source_type": "PM Opening Advance",
				"pm_opening_advance": oa.name,
				"allocated_amount": 3_000,
			},
		)
		cl.insert(ignore_permissions=True)
		self._track("PM Clearance", cl.name)

	def test_purchase_invoice_query_includes_draft(self):
		"""v4.1.5: Draft PI is selectable during Clearance prepare."""
		from erpnext_extensions.petty_management.doctype.pm_clearance.pm_clearance import (
			purchase_invoice_query_for_pm_clearance,
		)

		pi = _make_pi_outstanding(1_000)
		pi.insert(ignore_permissions=True)
		self._track("Purchase Invoice", pi.name)
		rows = purchase_invoice_query_for_pm_clearance(
			"Purchase Invoice",
			pi.name,
			"name",
			0,
			20,
			{"company": COMPANY},
		)
		names = {r[0] for r in rows}
		self.assertIn(pi.name, names)
		desc = next(r[1] for r in rows if r[0] == pi.name)
		self.assertIn("Draft", desc)

	def test_draft_purchase_invoice_allowed_on_clearance_save(self):
		"""v4.1.5: Draft PI may be saved on Clearance; Finance/Settle still gated."""
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 5_000.0)
		self._track("PM Request", req_name)
		pi = _make_pi_outstanding(1_000)
		pi.insert(ignore_permissions=True)
		self._track("Purchase Invoice", pi.name)
		alloc = flt(pi.grand_total or 1_000)
		cl = self._base_clearance(emp, pi, alloc)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": alloc})
		cl.insert(ignore_permissions=True)
		self._track("PM Clearance", cl.name)
		self.assertEqual(cint(frappe.db.get_value("Purchase Invoice", pi.name, "docstatus")), 0)

	def test_preview_returns_pi_debit_and_petty_credit_without_creating_je(self):
		mod = _pm()
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 100_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(5_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 5_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 5_000})
		cl.insert()
		self._track("PM Clearance", cl.name)

		n_before = frappe.db.count("Journal Entry")

		out = mod.preview_pm_clearance_settlement(pm_clearance=cl.name)
		n_after = frappe.db.count("Journal Entry")
		self.assertEqual(n_before, n_after)

		accounts = out.get("accounts") or []
		self.assertGreaterEqual(len(accounts), 2)
		debit_lines = [a for a in accounts if flt(a.get("debit_in_account_currency")) > 0]
		credit_lines = [a for a in accounts if flt(a.get("credit_in_account_currency")) > 0]
		self.assertEqual(len(debit_lines), 1)
		self.assertEqual(len(credit_lines), 1)
		self.assertEqual(debit_lines[0].get("account"), pi.credit_to)
		self.assertEqual(debit_lines[0].get("reference_type"), "Purchase Invoice")
		self.assertEqual(debit_lines[0].get("reference_name"), pi.name)
		petty = _ensure_petty_account()
		self.assertEqual(credit_lines[0].get("account"), petty)
		self.assertEqual(flt(credit_lines[0].get("credit_in_account_currency")), 5_000)

	def test_preview_unsaved_doc_validates_allocations_without_mutating_source(self):
		mod = _pm()
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 50_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(6_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 6_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 6_000})
		self.assertEqual(flt(cl.request_allocations[0].paid_amount), 0)

		out = mod.preview_pm_clearance_settlement(doc=frappe.as_json(cl.as_dict()))
		accounts = out.get("accounts") or []
		self.assertEqual(len([a for a in accounts if flt(a.get("debit_in_account_currency")) > 0]), 1)
		self.assertEqual(len([a for a in accounts if flt(a.get("credit_in_account_currency")) > 0]), 1)
		self.assertEqual(flt(cl.request_allocations[0].paid_amount), 0)

	def test_preview_saved_doc_does_not_create_je_or_extra_allocation_rows(self):
		mod = _pm()
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 50_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(4_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 4_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 4_000})
		cl.insert()
		self._track("PM Clearance", cl.name)
		before_rows = len(cl.request_allocations)
		before_snapshot = (
			flt(cl.request_allocations[0].request_amount),
			flt(cl.request_allocations[0].paid_amount),
			flt(cl.request_allocations[0].previously_allocated_amount),
			flt(cl.request_allocations[0].available_amount),
		)
		n_before = frappe.db.count("Journal Entry")

		mod.preview_pm_clearance_settlement(pm_clearance=cl.name)

		cl.reload()
		self.assertEqual(frappe.db.count("Journal Entry"), n_before)
		self.assertEqual(len(cl.request_allocations), before_rows)
		after_snapshot = (
			flt(cl.request_allocations[0].request_amount),
			flt(cl.request_allocations[0].paid_amount),
			flt(cl.request_allocations[0].previously_allocated_amount),
			flt(cl.request_allocations[0].available_amount),
		)
		self.assertEqual(after_snapshot, before_snapshot)

	def test_allocation_snapshot_validation_is_idempotent(self):
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 50_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(3_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 3_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 3_000})
		cl.insert()
		self._track("PM Clearance", cl.name)
		cl.reload()
		before_rows = len(cl.request_allocations)
		before_snapshot = (
			flt(cl.request_allocations[0].request_amount),
			flt(cl.request_allocations[0].paid_amount),
			flt(cl.request_allocations[0].previously_allocated_amount),
			flt(cl.request_allocations[0].available_amount),
		)

		cl.validate()
		cl.validate()

		self.assertEqual(len(cl.request_allocations), before_rows)
		after_snapshot = (
			flt(cl.request_allocations[0].request_amount),
			flt(cl.request_allocations[0].paid_amount),
			flt(cl.request_allocations[0].previously_allocated_amount),
			flt(cl.request_allocations[0].available_amount),
		)
		self.assertEqual(after_snapshot, before_snapshot)

	def test_preview_uses_same_builder_as_insert_path(self):
		mod = _pm()
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 20_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(7_500)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 7_500)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 7_500})
		cl.insert()
		self._track("PM Clearance", cl.name)

		cl.reload()
		direct = mod.build_clearance_je_accounts(cl)
		prev = mod.preview_pm_clearance_settlement(pm_clearance=cl.name)["accounts"]
		self.assertEqual(len(direct), len(prev))
		for a, b in zip(direct, prev, strict=True):
			self.assertEqual(a.get("account"), b.get("account"))
			self.assertEqual(flt(a.get("debit_in_account_currency")), flt(b.get("debit_in_account_currency")))
			self.assertEqual(
				flt(a.get("credit_in_account_currency")), flt(b.get("credit_in_account_currency"))
			)

	def test_settle_creates_je_and_sets_settled(self):
		mod = _pm()
		approved = _workflow_state_for("PM Clearance", "Approved")
		if not approved:
			self.skipTest("Active PM Clearance workflow with Approved state not found.")

		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_a, _pe_a = _fund_pm_request(emp, 40_000.0)
		self._track("PM Request", req_a)
		req_b, _pe_b = _fund_pm_request(emp, 60_000.0)
		self._track("PM Request", req_b)

		pi = _make_pi_outstanding(45_440)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)
		pi.reload()
		outstanding_before = flt(pi.outstanding_amount)
		alloc_a = 40_000.0
		alloc_b = outstanding_before - alloc_a
		self.assertGreater(alloc_b, 0)

		cl = self._base_clearance(emp, pi, outstanding_before)
		cl.append("request_allocations", {"pm_request": req_a, "allocated_amount": alloc_a})
		cl.append("request_allocations", {"pm_request": req_b, "allocated_amount": alloc_b})
		cl.insert()
		cl.submit()
		self._track("PM Clearance", cl.name)

		frappe.db.set_value("PM Clearance", cl.name, "workflow_state", approved, update_modified=False)

		out = mod.settle_petty_cash(cl.name)
		je_name = out["journal_entry"]
		self._track("Journal Entry", je_name)

		from erpnext_extensions.petty_management.utils import get_pm_settings

		settings = get_pm_settings()
		auto_submit = bool(settings and settings.auto_submit_journal_entry)

		cl.reload()
		je = frappe.get_doc("Journal Entry", je_name)
		self.assertEqual(je.docstatus, 1 if auto_submit else 0)
		if auto_submit:
			self.assertEqual(cl.status, "Settled")
		else:
			self.assertEqual(cl.status, "Pending Journal Entry Submission")
			if je.docstatus == 0:
				je.submit()
			cl.reload()
			self.assertEqual(cl.status, "Settled")

		self.assertEqual(cl.journal_entry, je_name)

		je = frappe.get_doc("Journal Entry", je_name)
		rows = je.get("accounts") or []
		dr = [r for r in rows if flt(r.debit_in_account_currency) > 0]
		cr = [r for r in rows if flt(r.credit_in_account_currency) > 0]
		self.assertEqual(len(dr), 1)
		self.assertEqual(len(cr), 1)
		self.assertEqual(dr[0].account, pi.credit_to)
		self.assertEqual(dr[0].reference_type, "Purchase Invoice")
		self.assertEqual(dr[0].reference_name, pi.name)
		self.assertEqual(cr[0].account, _ensure_petty_account())
		self.assertEqual(flt(cr[0].credit_in_account_currency), outstanding_before)

		pi.reload()
		self.assertLess(flt(pi.outstanding_amount), outstanding_before)

		self.assertTrue(all(not value for value in _purchase_invoice_pm_scalar_values(pi.name).values()))

	def test_settle_je_cancel_then_clearance_cancel_roll_back_reservation(self):
		"""Settlement JE cancel clears clearance link; reservation stays until clearance is cancelled."""
		mod = _pm()
		approved = _workflow_state_for("PM Clearance", "Approved")
		if not approved:
			self.skipTest("Active PM Clearance workflow with Approved state not found.")

		from erpnext_extensions.petty_management.services.allocation_service import (
			sum_prior_pm_request_allocations,
		)
		from erpnext_extensions.petty_management.services.holder_service import get_holder_settled_amount

		emp = _make_employee()
		self._track("Employee", emp)
		holder_name = _make_holder(emp)
		req_name, pe_name = _fund_pm_request(emp, 10_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(3_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 3_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 3_000})
		cl.insert()
		self._track("PM Clearance", cl.name)

		# v4.7.2: draft/Pending* clearances stay docstatus=0 until Finance Approve.
		self.assertEqual(cint(cl.docstatus), 0)
		self.assertLess(flt(sum_prior_pm_request_allocations(req_name, None)), 1e-3)

		_approve_pm_clearance_for_reservation(cl.name)
		self.assertGreaterEqual(flt(sum_prior_pm_request_allocations(req_name, None)), 3_000.0 - 1e-3)

		frappe.db.set_value("PM Clearance", cl.name, "workflow_state", approved, update_modified=False)
		out = mod.settle_petty_cash(cl.name)
		je_name = out["journal_entry"]
		self._track("Journal Entry", je_name)

		je = frappe.get_doc("Journal Entry", je_name)
		if je.docstatus == 0:
			je.submit()
			je.reload()

		cl.reload()
		self.assertEqual(cl.status, "Settled")
		self.assertTrue((cl.journal_entry or "").strip())
		self.assertGreaterEqual(get_holder_settled_amount(holder_name), 3_000.0 - 1e-3)

		je.reload()
		je.cancel()

		cl.reload()
		self.assertNotEqual(cl.status, "Settled")
		self.assertFalse((cl.journal_entry or "").strip())
		self.assertGreaterEqual(flt(sum_prior_pm_request_allocations(req_name, None)), 3_000.0 - 1e-3)
		self.assertLess(get_holder_settled_amount(holder_name), 1e-3)

		cl = frappe.get_doc("PM Clearance", cl.name)
		cl.cancel()

		self.assertLess(flt(sum_prior_pm_request_allocations(req_name, None)), 1e-3)

		refs = {req_name, cl.name, je_name, pi.name, holder_name, pe_name}
		bad = _reconciliation_errors_touching_refs(COMPANY, refs)
		self.assertFalse(
			bad,
			msg="; ".join(f"{i.code}: {i.detail}" for i in bad) or "reconciliation errors",
		)

	def test_duplicate_settle_returns_existing_journal_entry(self):
		mod = _pm()
		approved = _workflow_state_for("PM Clearance", "Approved")
		if not approved:
			self.skipTest("Active PM Clearance workflow with Approved state not found.")

		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 10_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(10_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 10_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 10_000})
		cl.insert()
		cl.submit()
		self._track("PM Clearance", cl.name)
		_approve_pm_clearance_for_reservation(cl.name)
		frappe.db.set_value("PM Clearance", cl.name, "workflow_state", approved, update_modified=False)

		first = mod.settle_petty_cash(cl.name)
		self._track("Journal Entry", first["journal_entry"])
		before_count = frappe.db.count("Journal Entry")
		second = mod.settle_petty_cash(cl.name)
		after_count = frappe.db.count("Journal Entry")

		self.assertEqual(first["journal_entry"], second["journal_entry"])
		self.assertEqual(before_count, after_count)

	def test_pm_request_query_excludes_other_employee_requests(self):
		mod = _pm()
		emp_a = _make_employee()
		emp_b = _make_employee()
		self._track("Employee", emp_a)
		self._track("Employee", emp_b)
		_make_holder(emp_a)
		_make_holder(emp_b)
		req_b, _pe_b = _fund_pm_request(emp_b, 50_000.0)
		self._track("PM Request", req_b)

		holder_a = frappe.db.get_value("PM Holder", {"employee": emp_a, "company": COMPANY}, "name")
		petty_a = frappe.db.get_value("PM Holder", holder_a, "petty_cash_account")
		rows = mod.pm_request_query_for_pm_clearance(
			"PM Request",
			"",
			"name",
			0,
			20,
			{
				"employee": emp_a,
				"company": COMPANY,
				"holder": holder_a,
				"petty_cash_account": petty_a,
			},
		)
		names = [r[0] for r in rows]
		self.assertNotIn(req_b, names)

	def test_clearance_rejects_pm_request_from_other_employee(self):
		emp_a = _make_employee()
		emp_b = _make_employee()
		self._track("Employee", emp_a)
		self._track("Employee", emp_b)
		_make_holder(emp_a)
		_make_holder(emp_b)
		req_b, _pe_b = _fund_pm_request(emp_b, 20_000.0)
		self._track("PM Request", req_b)

		pi = _make_pi_outstanding(5_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp_a, pi, 5_000)
		cl.append("request_allocations", {"pm_request": req_b, "allocated_amount": 5_000})
		with self.assertRaises(ValidationError) as ctx:
			cl.insert()
		self.assertIn(req_b, str(ctx.exception))

	def test_petty_cash_account_matches_request_after_insert(self):
		mod = _pm()
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 30_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(8_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 8_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 8_000})
		cl.insert()
		self._track("PM Clearance", cl.name)

		cl.reload()
		req_doc = frappe.get_doc("PM Request", req_name)
		req_petty = mod.pm_request_petty_cash_from_holder(req_doc)
		clr_petty = mod.clearance_petty_cash_account(cl)
		self.assertTrue(clr_petty)
		self.assertEqual(req_petty, clr_petty)

	def test_settlement_totals_mismatch_with_pi_and_supplier_advance(self):
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 200_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(10_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)
		po = _make_purchase_order_for_company(qty=1, rate=5_000)
		self._track("Purchase Order", po.name)
		sa_acc = _supplier_advance_test_account()

		cl = frappe.new_doc("PM Clearance")
		cl.company = COMPANY
		cl.employee = emp
		cl.transaction_date = today()
		_append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi.name,
				"allocated_amount": 10_000,
			},
		)
		_append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Supplier Advance",
				"purchase_order": po.name,
				"supplier_advance_account": sa_acc,
				"allocated_amount": 5_000,
			},
		)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 10_000})
		with self.assertRaises(ValidationError) as ctx:
			cl.insert()
		self.assertIn("Total funding allocation", str(ctx.exception))

	def test_preview_supplier_advance_debit_and_single_petty_credit(self):
		mod = _pm()
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 50_000.0)
		self._track("PM Request", req_name)

		po = _make_purchase_order_for_company(qty=2, rate=3_000)
		self._track("Purchase Order", po.name)
		sa_acc = _supplier_advance_test_account()
		alloc = 6_000.0

		cl = frappe.new_doc("PM Clearance")
		cl.company = COMPANY
		cl.employee = emp
		cl.transaction_date = today()
		_append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Supplier Advance",
				"purchase_order": po.name,
				"supplier_advance_account": sa_acc,
				"allocated_amount": alloc,
			},
		)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": alloc})
		cl.insert()
		self._track("PM Clearance", cl.name)

		out = mod.preview_pm_clearance_settlement(pm_clearance=cl.name)
		accounts = out.get("accounts") or []
		debit_lines = [a for a in accounts if flt(a.get("debit_in_account_currency")) > 0]
		credit_lines = [a for a in accounts if flt(a.get("credit_in_account_currency")) > 0]
		self.assertEqual(len(debit_lines), 1)
		self.assertEqual(len(credit_lines), 1)
		self.assertEqual(debit_lines[0].get("account"), sa_acc)
		self.assertEqual(debit_lines[0].get("party_type"), "Supplier")
		self.assertEqual(debit_lines[0].get("party"), po.supplier)
		self.assertEqual(debit_lines[0].get("reference_type"), "Purchase Order")
		self.assertEqual(debit_lines[0].get("reference_name"), po.name)
		self.assertEqual(flt(credit_lines[0].get("credit_in_account_currency")), alloc)
		self.assertEqual(credit_lines[0].get("account"), _ensure_petty_account())

	def test_preview_mixed_pi_and_supplier_advance_matches_builder(self):
		mod = _pm()
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 200_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(12_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)
		po = _make_purchase_order_for_company(qty=1, rate=8_000)
		self._track("Purchase Order", po.name)
		sa_acc = _supplier_advance_test_account()
		pi_alloc = 12_000.0
		sa_alloc = 8_000.0
		total = pi_alloc + sa_alloc

		cl = frappe.new_doc("PM Clearance")
		cl.company = COMPANY
		cl.employee = emp
		cl.transaction_date = today()
		_append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi.name,
				"allocated_amount": pi_alloc,
			},
		)
		_append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Supplier Advance",
				"purchase_order": po.name,
				"supplier_advance_account": sa_acc,
				"allocated_amount": sa_alloc,
			},
		)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": total})
		cl.insert()
		self._track("PM Clearance", cl.name)

		cl.reload()
		direct = mod.build_clearance_je_accounts(cl)
		prev = mod.preview_pm_clearance_settlement(pm_clearance=cl.name)["accounts"]
		self.assertEqual(len(direct), len(prev))
		for a, b in zip(direct, prev, strict=True):
			self.assertEqual(a.get("account"), b.get("account"))
			self.assertEqual(flt(a.get("debit_in_account_currency")), flt(b.get("debit_in_account_currency")))
			self.assertEqual(
				flt(a.get("credit_in_account_currency")), flt(b.get("credit_in_account_currency"))
			)
			self.assertEqual(a.get("reference_type"), b.get("reference_type"))
			self.assertEqual(a.get("reference_name"), b.get("reference_name"))

		debit_lines = [a for a in prev if flt(a.get("debit_in_account_currency")) > 0]
		credit_lines = [a for a in prev if flt(a.get("credit_in_account_currency")) > 0]
		self.assertEqual(len(debit_lines), 2)
		self.assertEqual(len(credit_lines), 1)
		self.assertEqual(flt(credit_lines[0].get("credit_in_account_currency")), total)
		accts = {d.get("account") for d in debit_lines}
		self.assertIn(pi.credit_to, accts)
		self.assertIn(sa_acc, accts)

	def test_settle_supplier_advance_creates_je(self):
		mod = _pm()
		approved = _workflow_state_for("PM Clearance", "Approved")
		if not approved:
			self.skipTest("Active PM Clearance workflow with Approved state not found.")

		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 80_000.0)
		self._track("PM Request", req_name)

		po = _make_purchase_order_for_company(qty=1, rate=15_000)
		self._track("Purchase Order", po.name)
		sa_acc = _supplier_advance_test_account()
		alloc = 15_000.0

		cl = frappe.new_doc("PM Clearance")
		cl.company = COMPANY
		cl.employee = emp
		cl.transaction_date = today()
		_append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Supplier Advance",
				"purchase_order": po.name,
				"supplier_advance_account": sa_acc,
				"allocated_amount": alloc,
			},
		)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": alloc})
		cl.insert()
		cl.submit()
		self._track("PM Clearance", cl.name)
		frappe.db.set_value("PM Clearance", cl.name, "workflow_state", approved, update_modified=False)

		out = mod.settle_petty_cash(cl.name)
		je_name = out["journal_entry"]
		self._track("Journal Entry", je_name)

		je = frappe.get_doc("Journal Entry", je_name)
		if je.docstatus == 0:
			je.submit()

		rows = je.get("accounts") or []
		dr = [r for r in rows if flt(r.debit_in_account_currency) > 0]
		cr = [r for r in rows if flt(r.credit_in_account_currency) > 0]
		self.assertEqual(len(dr), 1)
		self.assertEqual(len(cr), 1)
		self.assertEqual(dr[0].account, sa_acc)
		self.assertEqual(dr[0].party_type, "Supplier")
		self.assertEqual(dr[0].reference_type, "Purchase Order")
		self.assertEqual(dr[0].reference_name, po.name)
		self.assertEqual(flt(cr[0].credit_in_account_currency), alloc)

	def test_settle_mixed_pi_and_supplier_advance_one_credit(self):
		mod = _pm()
		approved = _workflow_state_for("PM Clearance", "Approved")
		if not approved:
			self.skipTest("Active PM Clearance workflow with Approved state not found.")

		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 500_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(20_440)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)
		po = _make_purchase_order_for_company(qty=1, rate=20_000)
		self._track("Purchase Order", po.name)
		sa_acc = _supplier_advance_test_account()
		pi_alloc = 20_440.0
		sa_alloc = 20_000.0
		total = pi_alloc + sa_alloc
		pi.reload()
		outstanding_before = flt(pi.outstanding_amount)

		cl = frappe.new_doc("PM Clearance")
		cl.company = COMPANY
		cl.employee = emp
		cl.transaction_date = today()
		_append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi.name,
				"allocated_amount": pi_alloc,
			},
		)
		_append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Supplier Advance",
				"purchase_order": po.name,
				"supplier_advance_account": sa_acc,
				"allocated_amount": sa_alloc,
			},
		)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": total})
		cl.insert()
		cl.submit()
		self._track("PM Clearance", cl.name)
		frappe.db.set_value("PM Clearance", cl.name, "workflow_state", approved, update_modified=False)

		out = mod.settle_petty_cash(cl.name)
		je_name = out["journal_entry"]
		self._track("Journal Entry", je_name)

		je = frappe.get_doc("Journal Entry", je_name)
		if je.docstatus == 0:
			je.submit()

		rows = je.get("accounts") or []
		dr = [r for r in rows if flt(r.debit_in_account_currency) > 0]
		cr = [r for r in rows if flt(r.credit_in_account_currency) > 0]
		self.assertEqual(len(dr), 2)
		self.assertEqual(len(cr), 1)
		self.assertEqual(flt(cr[0].credit_in_account_currency), total)

		pi.reload()
		self.assertLess(flt(pi.outstanding_amount), outstanding_before)

	def test_new_clearance_cannot_use_legacy_row_without_db_legacy(self):
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 50_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(1_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 1_000)
		cl.append(
			"request_allocations",
			{"is_legacy_row": 1, "allocated_amount": 1_000},
		)
		with self.assertRaises(ValidationError):
			cl.insert()

	def test_legacy_row_validate_passes_when_present_in_db_like_migration(self):
		"""Simulate migration: DB already has legacy child → clearance may keep legacy-only allocation."""
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 50_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(5_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 5_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 5_000})
		cl.insert()
		self._track("PM Clearance", cl.name)
		total = flt(cl.total_expense_amount)

		frappe.db.sql(
			"delete from `tabPM Clearance Request Allocation` where parent=%s and parenttype='PM Clearance'",
			(cl.name,),
		)
		_insert_legacy_allocation_row(cl.name, total)
		frappe.db.commit()

		doc = frappe.get_doc("PM Clearance", cl.name)
		doc.validate()

	def test_pm_settlement_ledger_shows_separate_settlement_and_funding_rows(self):
		from erpnext_extensions.petty_management.report.pm_settlement_ledger import (
			pm_settlement_ledger as report,
		)

		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, pe_name = _fund_pm_request(emp, 5_000)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(5_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = self._base_clearance(emp, pi, 5_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 5_000})
		cl.insert()
		self._track("PM Clearance", cl.name)
		cl.submit()
		_approve_pm_clearance_for_reservation(cl.name)

		_columns, data = report.execute({"pm_clearance": cl.name})
		settlement_rows = [row for row in data if row.row_type == "Settlement Line"]
		funding_rows = [row for row in data if row.row_type == "Funding Allocation Line"]

		self.assertEqual(len(settlement_rows), 1)
		self.assertEqual(settlement_rows[0].purchase_invoice, pi.name)
		self.assertEqual(flt(settlement_rows[0].settlement_amount), 5_000)
		self.assertFalse(settlement_rows[0].pm_request)

		self.assertEqual(len(funding_rows), 1)
		self.assertEqual(funding_rows[0].pm_request, req_name)
		self.assertEqual(funding_rows[0].payment_entry, pe_name)
		self.assertEqual(flt(funding_rows[0].pm_request_allocated_amount), 5_000)
		self.assertFalse(funding_rows[0].purchase_invoice)

	def test_pm_settlement_ledger_does_not_multiply_multiple_lines_and_allocations(self):
		from erpnext_extensions.petty_management.report.pm_settlement_ledger import (
			pm_settlement_ledger as report,
		)

		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_a, _pe_a = _fund_pm_request(emp, 1_500)
		self._track("PM Request", req_a)
		req_b, _pe_b = _fund_pm_request(emp, 2_500)
		self._track("PM Request", req_b)

		pi_a = _make_pi_outstanding(1_500)
		pi_a.insert()
		pi_a.submit()
		self._track("Purchase Invoice", pi_a.name)
		pi_b = _make_pi_outstanding(2_500)
		pi_b.insert()
		pi_b.submit()
		self._track("Purchase Invoice", pi_b.name)

		cl = self._base_clearance(emp, pi_a, 1_500)
		_append_pm_clearance_detail_row(
			cl,
			{
				"settlement_type": "Purchase Invoice",
				"purchase_invoice": pi_b.name,
				"allocated_amount": 2_500,
			},
		)
		cl.append("request_allocations", {"pm_request": req_a, "allocated_amount": 1_500})
		cl.append("request_allocations", {"pm_request": req_b, "allocated_amount": 2_500})
		cl.insert()
		self._track("PM Clearance", cl.name)
		cl.submit()
		_approve_pm_clearance_for_reservation(cl.name)

		_columns, data = report.execute({"pm_clearance": cl.name})
		settlement_rows = [row for row in data if row.row_type == "Settlement Line"]
		funding_rows = [row for row in data if row.row_type == "Funding Allocation Line"]

		self.assertEqual(len(settlement_rows), 2)
		self.assertEqual(len(funding_rows), 2)
		self.assertEqual(sum(flt(row.settlement_amount) for row in settlement_rows), 4_000)
		self.assertEqual(sum(flt(row.pm_request_allocated_amount) for row in funding_rows), 4_000)

	def test_multiple_clearances_can_reference_same_pi_without_pi_scalar_overwrite(self):
		mod = _pm()
		approved = _workflow_state_for("PM Clearance", "Approved")
		if not approved:
			self.skipTest("Active PM Clearance workflow with Approved state not found.")

		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_a, _pe_a = _fund_pm_request(emp, 2_000)
		self._track("PM Request", req_a)
		req_b, _pe_b = _fund_pm_request(emp, 3_000)
		self._track("PM Request", req_b)

		pi = _make_pi_outstanding(10_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl_a = self._base_clearance(emp, pi, 2_000)
		cl_a.append("request_allocations", {"pm_request": req_a, "allocated_amount": 2_000})
		cl_a.insert()
		cl_a.submit()
		self._track("PM Clearance", cl_a.name)
		frappe.db.set_value("PM Clearance", cl_a.name, "workflow_state", approved, update_modified=False)
		out_a = mod.settle_petty_cash(cl_a.name)
		self._track("Journal Entry", out_a["journal_entry"])

		cl_b = self._base_clearance(emp, pi, 3_000)
		cl_b.append("request_allocations", {"pm_request": req_b, "allocated_amount": 3_000})
		cl_b.insert()
		cl_b.submit()
		self._track("PM Clearance", cl_b.name)
		frappe.db.set_value("PM Clearance", cl_b.name, "workflow_state", approved, update_modified=False)
		out_b = mod.settle_petty_cash(cl_b.name)
		self._track("Journal Entry", out_b["journal_entry"])

		self.assertTrue(all(not value for value in _purchase_invoice_pm_scalar_values(pi.name).values()))
		self.assertEqual(
			frappe.db.sql(
				"""
				select count(*)
				from `tabPM Clearance Detail`
				where purchase_invoice=%s and parent in (%s, %s)
				""",
				(pi.name, cl_a.name, cl_b.name),
			)[0][0],
			2,
		)

	def test_preview_includes_line_type_and_auto_submit_flag(self):
		mod = _pm()
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 20_000.0)
		pi = _make_pi_outstanding(5_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)
		cl = self._base_clearance(emp, pi, 5_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 5_000})
		cl.insert()
		self._track("PM Clearance", cl.name)
		out = mod.preview_pm_clearance_settlement(pm_clearance=cl.name)
		self.assertIn("auto_submit_journal_entry", out)
		self.assertEqual(out.get("pm_clearance"), cl.name)
		self.assertIn("is_balanced", out)
		self.assertTrue(out.get("is_balanced"))
		for row in out.get("accounts") or []:
			self.assertIn(row.get("line_type"), ("Debit", "Credit", ""))


def _lifecycle_base_clearance(employee: str, pi, pi_amount: float):
	cl = frappe.new_doc("PM Clearance")
	cl.company = COMPANY
	cl.employee = employee
	cl.transaction_date = today()
	_append_pm_clearance_detail_row(
		cl,
		{
			"settlement_type": "Purchase Invoice",
			"purchase_invoice": pi.name,
			"allocated_amount": pi_amount,
		},
	)
	return cl


class TestPMClearanceLifecyclePolicy(unittest.TestCase):
	"""Status/workflow vs JE consistency, action matrix, accounting lock."""

	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		_ensure_company_context()
		if not COMPANY:
			raise unittest.SkipTest("No Company on site.")

	def setUp(self):
		frappe.set_user("Administrator")
		self._cleanup_names: list[tuple[str, str]] = []

	def tearDown(self):
		frappe.set_user("Administrator")
		for doctype, name in reversed(self._cleanup_names):
			try:
				if doctype == "PM Request" and frappe.db.exists("PM Request", name):
					pe = frappe.db.get_value("PM Request", name, "payment_entry")
					if pe and frappe.db.exists("Payment Entry", pe):
						pe_doc = frappe.get_doc("Payment Entry", pe)
						if pe_doc.docstatus == 1:
							pe_doc.cancel()
						frappe.delete_doc("Payment Entry", pe, force=True, ignore_permissions=True)
				doc = frappe.get_doc(doctype, name)
				if getattr(doc, "docstatus", 0) == 1:
					doc.reload()
					doc.cancel()
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
			except Exception:
				pass
		frappe.db.commit()

	def _track(self, doctype: str, name: str) -> None:
		self._cleanup_names.append((doctype, name))

	def _settled_clearance(self):
		mod = _pm()
		approved = _workflow_state_for("PM Clearance", "Approved")
		if not approved:
			self.skipTest("Active PM Clearance workflow with Approved state not found.")
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 15_000.0)
		self._track("PM Request", req_name)
		pi = _make_pi_outstanding(4_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)
		cl = _lifecycle_base_clearance(emp, pi, 4_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 4_000})
		cl.insert()
		cl.submit()
		self._track("PM Clearance", cl.name)
		_approve_pm_clearance_for_reservation(cl.name)
		frappe.db.set_value("PM Clearance", cl.name, "workflow_state", approved, update_modified=False)
		out = mod.settle_petty_cash(cl.name)
		je_name = out["journal_entry"]
		self._track("Journal Entry", je_name)
		je = frappe.get_doc("Journal Entry", je_name)
		if je.docstatus == 0:
			je.submit()
		cl.reload()
		return cl, je_name

	def test_settled_clearance_cannot_be_rejected_via_workflow_guard(self):
		cl, _je = self._settled_clearance()
		from erpnext_extensions.petty_management.services.clearance_action_policy import (
			validate_apply_workflow_action,
		)

		with self.assertRaises(ValidationError):
			validate_apply_workflow_action(cl, "PM Reject")

	def test_submitted_je_blocks_workflow_state_change(self):
		cl, _je = self._settled_clearance()
		rejected = _workflow_state_for("PM Clearance", "Rejected")
		if not rejected:
			self.skipTest("Rejected workflow state not configured.")
		cl.reload()
		cl.workflow_state = rejected
		with self.assertRaises(ValidationError):
			cl.save(ignore_permissions=True)

	def test_list_status_settled_after_stale_db_repair(self):
		cl, _je = self._settled_clearance()
		approved = _workflow_state_for("PM Clearance", "Approved")
		if approved:
			frappe.db.set_value(
				"PM Clearance",
				cl.name,
				{"status": "Approved", "workflow_state": approved},
				update_modified=False,
			)
		from erpnext_extensions.petty_management.services.clearance_action_policy import (
			sync_clearance_lifecycle_if_stale,
		)

		doc = frappe.get_doc("PM Clearance", cl.name)
		sync_clearance_lifecycle_if_stale(doc)
		row = frappe.db.get_value("PM Clearance", cl.name, ["status", "workflow_state"], as_dict=True)
		self.assertEqual(row.status, "Settled")
		# v4.0.2: JE updates business status only; approval workflow stays Approved
		ws_title = frappe.db.get_value("Workflow State", row.workflow_state, "workflow_state_name")
		self.assertEqual(ws_title, "Approved")
		self.assertEqual(row.workflow_state, approved or row.workflow_state)

	def test_je_cancel_restores_non_settled_lifecycle(self):
		cl, je_name = self._settled_clearance()
		je = frappe.get_doc("Journal Entry", je_name)
		je.cancel()
		cl.reload()
		self.assertNotEqual(cl.status, "Settled")
		from erpnext_extensions.petty_management.doctype.pm_clearance.pm_clearance import (
			get_pm_clearance_action_flags,
		)

		flags = get_pm_clearance_action_flags(cl.name)
		self.assertFalse(flags.get("accounting_locked"))
		self.assertTrue(flags.get("can_reject") or cl.status == "Approved")

	def test_action_flags_draft_and_approved(self):
		from erpnext_extensions.petty_management.services.clearance_action_policy import (
			get_pm_clearance_action_flags,
		)

		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 5_000.0)
		self._track("PM Request", req_name)
		pi = _make_pi_outstanding(1_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)
		cl = _lifecycle_base_clearance(emp, pi, 1_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 1_000})
		cl.insert()
		self._track("PM Clearance", cl.name)
		draft_flags = get_pm_clearance_action_flags(cl.name)
		self.assertTrue(draft_flags["can_preview"])
		self.assertFalse(draft_flags["can_settle"])
		self.assertFalse(draft_flags["can_reject"])

		cl.submit()
		_approve_pm_clearance_for_reservation(cl.name)
		cl.reload()
		appr_flags = get_pm_clearance_action_flags(cl.name)
		self.assertTrue(appr_flags["can_settle"])
		self.assertTrue(appr_flags["can_reject"])
		self.assertEqual(appr_flags["lifecycle_state"], "Approved")

	def test_action_flags_settled_locked(self):
		from erpnext_extensions.petty_management.services.clearance_action_policy import (
			get_pm_clearance_action_flags,
		)

		cl, _je = self._settled_clearance()
		settled_flags = get_pm_clearance_action_flags(cl.name)
		self.assertFalse(settled_flags["can_settle"])
		self.assertFalse(settled_flags["can_reject"])
		self.assertTrue(settled_flags["accounting_locked"])
		self.assertEqual(settled_flags["lifecycle_state"], "Settled")

	def test_cancelled_clearance_releases_pm_request_reservation(self):
		"""After JE cancel + clearance cancel, prior allocations must not reserve funding."""
		mod = _pm()
		from erpnext_extensions.petty_management.services.allocation_service import (
			get_pm_request_available_amount,
			sum_prior_pm_request_allocations,
		)

		approved = _workflow_state_for("PM Clearance", "Approved")
		if not approved:
			self.skipTest("Active PM Clearance workflow with Approved state not found.")

		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 10_000.0)
		self._track("PM Request", req_name)

		pi = _make_pi_outstanding(3_000)
		pi.insert()
		pi.submit()
		self._track("Purchase Invoice", pi.name)

		cl = _lifecycle_base_clearance(emp, pi, 3_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 3_000})
		cl.insert()
		cl.submit()
		self._track("PM Clearance", cl.name)
		_approve_pm_clearance_for_reservation(cl.name)
		frappe.db.set_value("PM Clearance", cl.name, "workflow_state", approved, update_modified=False)

		self.assertGreaterEqual(flt(sum_prior_pm_request_allocations(req_name, None)), 3_000.0 - 1e-3)

		out = mod.settle_petty_cash(cl.name)
		je_name = out["journal_entry"]
		self._track("Journal Entry", je_name)
		je = frappe.get_doc("Journal Entry", je_name)
		if je.docstatus == 0:
			je.submit()

		je.cancel()
		cl = frappe.get_doc("PM Clearance", cl.name)
		cl.cancel()

		self.assertEqual(cint(frappe.db.get_value("PM Clearance", cl.name, "docstatus")), 2)
		st = (frappe.db.get_value("PM Clearance", cl.name, "status") or "").strip()
		self.assertEqual(st, "Cancelled")
		self.assertLess(flt(sum_prior_pm_request_allocations(req_name, None)), 1e-3)
		self.assertGreaterEqual(flt(get_pm_request_available_amount(req_name)), 10_000.0 - 1e-3)

	def test_preview_remains_balanced(self):
		mod = _pm()
		emp = _make_employee()
		self._track("Employee", emp)
		_make_holder(emp)
		req_name, _pe = _fund_pm_request(emp, 8_000.0)
		pi = _make_pi_outstanding(2_000)
		pi.insert()
		pi.submit()
		cl = _lifecycle_base_clearance(emp, pi, 2_000)
		cl.append("request_allocations", {"pm_request": req_name, "allocated_amount": 2_000})
		cl.insert()
		self._track("PM Clearance", cl.name)
		out = mod.preview_pm_clearance_settlement(pm_clearance=cl.name)
		self.assertTrue(out.get("is_balanced"))
		self.assertLess(abs(flt(out.get("total_debit")) - flt(out.get("total_credit"))), 0.01)


if __name__ == "__main__":
	unittest.main()
