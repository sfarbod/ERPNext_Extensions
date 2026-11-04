# Copyright (c) 2026, ERPNext Extensions contributors
"""Prep fixtures for v5.6.32 PM Clearance PM Request eligibility Playwright."""

from __future__ import annotations

import frappe
from frappe.utils import flt, today

from erpnext_extensions.petty_management.services.allocation_service import (
	get_pm_request_available_amount,
	pm_request_query_for_pm_clearance,
)
from erpnext_extensions.petty_management.tests import test_pm_clearance as tpm
from erpnext_extensions.petty_management.tests.test_pm_request_multi_pe import (
	_close_pm_request,
	_create_funding_pe,
	_new_submitted_request,
	_sync_funding_fields,
)

# Prefer Administrator — creating Users trips a broken Raven child DocType on this site.
USER = "Administrator"
TARGET_LIVE = "REQ-HR-EMP-0023-2026-09-00001"


def _admin_password() -> str:
	# Desk E2E default; override via site conf when present.
	return (frappe.conf.get("admin_password") or "admin").strip() or "admin"

def _ensure_user() -> str:
	"""Return a Desk login that already exists (no User insert)."""
	if not frappe.db.exists("User", USER):
		frappe.throw(f"Required login user missing: {USER}")
	return USER

def _existing_pi() -> str:
	rows = frappe.db.sql(
		"""
		select name from `tabPurchase Invoice`
		where docstatus = 1 and ifnull(outstanding_amount, 0) >= 1
		order by modified desc limit 1
		""",
		as_dict=True,
	)
	if not rows:
		frappe.throw("No submitted Purchase Invoice for v5.6.32 E2E")
	return rows[0].name


@frappe.whitelist()
def prepare_eligibility_fixtures() -> dict:
	"""Create Partially Paid Closed / Open / Unpaid Closed fixtures + inspect live target."""
	frappe.set_user("Administrator")
	tpm._ensure_company_context()
	_ensure_user()
	# Reuse an existing holder/employee — creating Employee trips a broken Raven hook on this site.
	row = frappe.db.sql(
		"""
		select h.employee, h.name as holder, h.petty_cash_account
		from `tabPM Holder` h
		inner join `tabEmployee` e on e.name = h.employee and ifnull(e.status,'') = 'Active'
		where h.company = %s and ifnull(h.is_blocked,0)=0
		  and ifnull(h.petty_cash_account,'')!=''
		order by h.modified desc limit 1
		""",
		(tpm.COMPANY,),
		as_dict=True,
	)
	if not row:
		frappe.throw("No PM Holder available for v5.6.32 E2E")
	emp = row[0].employee
	holder = row[0].holder
	petty = row[0].petty_cash_account
	# Workflow-safe manager stamp for fixture PM Requests.
	if frappe.db.exists("User", "Administrator"):
		frappe.db.set_value(
			"Employee", emp, "expense_approver", "Administrator", update_modified=False
		)
	pi = _existing_pi()
	orig_out = flt(frappe.db.get_value("Purchase Invoice", pi, "outstanding_amount"))
	frappe.db.set_value("Purchase Invoice", pi, "outstanding_amount", 100_000, update_modified=False)

	# A: Partially Paid + Closed with 50k available (requested 63k, paid 50k)
	req_ok = _new_submitted_request(emp, 63_000)
	_create_funding_pe(req_ok, 50_000)
	_sync_funding_fields(req_ok)
	_close_pm_request(req_ok, close_reason="Partial Approval")
	_sync_funding_fields(req_ok)
	frappe.db.set_value(
		"PM Request",
		req_ok,
		{"payment_status": "Partially Paid", "status": "Closed"},
		update_modified=False,
	)

	# D: Partially Paid + Open (Waiting for Payment) — must NOT appear
	req_open = _new_submitted_request(emp, 40_000)
	_create_funding_pe(req_open, 20_000)
	_sync_funding_fields(req_open)
	frappe.db.set_value(
		"PM Request",
		req_open,
		{"payment_status": "Partially Paid", "status": "Waiting for Payment", "is_closed": 0},
		update_modified=False,
	)

	# E: Unpaid + Closed — must NOT appear
	req_unpaid = _new_submitted_request(emp, 30_000)
	frappe.db.set_value(
		"PM Request",
		req_unpaid,
		{
			"payment_status": "Not Paid",
			"status": "Closed",
			"is_closed": 1,
			"payment_entry": None,
			"total_paid_amount": 0,
		},
		update_modified=False,
	)
	frappe.db.commit()

	live = None
	if frappe.db.exists("PM Request", TARGET_LIVE):
		row = frappe.db.get_value(
			"PM Request",
			TARGET_LIVE,
			[
				"name",
				"employee",
				"company",
				"holder",
				"payment_status",
				"status",
				"docstatus",
				"total_paid_amount",
				"petty_cash_account",
			],
			as_dict=True,
		)
		# Link query matches holder.petty_cash_account (not Request.petty_cash_account).
		live_petty = ""
		if row.get("holder"):
			live_petty = (
				frappe.db.get_value("PM Holder", row.holder, "petty_cash_account") or ""
			).strip()
		if not live_petty:
			live_petty = (row.get("petty_cash_account") or "").strip()
		live = {
			**row,
			"petty_cash_account": live_petty,
			"available": get_pm_request_available_amount(TARGET_LIVE),
		}

	filters = {
		"employee": emp,
		"company": tpm.COMPANY,
		"holder": holder,
		"petty_cash_account": petty,
	}
	q_ok = [r[0] for r in pm_request_query_for_pm_clearance("PM Request", req_ok, "name", 0, 20, filters)]
	q_open = [
		r[0] for r in pm_request_query_for_pm_clearance("PM Request", req_open, "name", 0, 20, filters)
	]
	q_unpaid = [
		r[0] for r in pm_request_query_for_pm_clearance("PM Request", req_unpaid, "name", 0, 20, filters)
	]

	pwd = _admin_password()
	return {
		"password": pwd,
		"user": {"email": USER, "password": pwd},
		"employee": emp,
		"holder": holder,
		"company": tpm.COMPANY,
		"petty_cash_account": petty,
		"purchase_invoice": pi,
		"original_pi_outstanding": orig_out,
		"req_partial_closed": req_ok,
		"req_partial_open": req_open,
		"req_unpaid_closed": req_unpaid,
		"available_partial_closed": get_pm_request_available_amount(req_ok),
		"live_target": live,
		"server_query": {
			"partial_closed_visible": req_ok in q_ok,
			"partial_open_visible": req_open in q_open,
			"unpaid_closed_visible": req_unpaid in q_unpaid,
		},
	}


@frappe.whitelist()
def restore_pi_outstanding(purchase_invoice: str, outstanding: float) -> None:
	frappe.set_user("Administrator")
	frappe.db.set_value(
		"Purchase Invoice",
		purchase_invoice,
		"outstanding_amount",
		flt(outstanding),
		update_modified=False,
	)
	frappe.db.commit()


@frappe.whitelist()
def link_query_contains(
	employee: str,
	company: str,
	holder: str,
	petty_cash_account: str,
	pm_request: str,
) -> dict:
	frappe.set_user("Administrator")
	rows = pm_request_query_for_pm_clearance(
		"PM Request",
		pm_request,
		"name",
		0,
		50,
		{
			"employee": employee,
			"company": company,
			"holder": holder,
			"petty_cash_account": petty_cash_account,
		},
	)
	names = [r[0] for r in rows]
	return {"contains": pm_request in names, "names": names}


@frappe.whitelist()
def try_alloc(
	employee: str,
	company: str,
	purchase_invoice: str,
	pm_request: str,
	amount: float,
) -> dict:
	"""Insert Draft Clearance allocation; always rollback."""
	frappe.set_user("Administrator")
	amt = flt(amount)
	cl = frappe.new_doc("PM Clearance")
	cl.company = company
	cl.employee = employee
	cl.transaction_date = today()
	tpm._append_pm_clearance_detail_row(
		cl,
		{
			"settlement_type": "Purchase Invoice",
			"purchase_invoice": purchase_invoice,
			"allocated_amount": amt,
			"outstanding_amount": 100_000,
		},
	)
	cl.append(
		"request_allocations",
		{"funding_source_type": "PM Request", "pm_request": pm_request, "allocated_amount": amt},
	)
	cl.flags.ignore_mandatory = True
	try:
		cl.insert(ignore_permissions=True)
		frappe.db.rollback()
		return {"ok": True, "name": cl.name}
	except Exception as e:
		frappe.db.rollback()
		return {"ok": False, "error": str(e)[:600]}
