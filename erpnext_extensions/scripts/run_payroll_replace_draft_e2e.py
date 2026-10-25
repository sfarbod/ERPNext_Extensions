"""E2E runner for payroll_replace_accrual_je_draft on payroll-e2e.localhost.

Expects the site in the post-correction 266-employee state with a submitted
accrual JE (ACC-JV-1405-172040). Does NOT touch development.localhost.
Does NOT submit the regenerated Journal Entry.
"""

from __future__ import annotations

import json
import traceback

import frappe
from frappe.utils import flt, getdate

from erpnext_extensions.extentionhrms.payroll_correction import payroll_replace_accrual_je_draft

PE = "شهریور 1405"
JE = "ACC-JV-1405-172040"
REQ_DATE = "2026-09-22"
DEBIT = 283672596839.0
ROWS = 1363
EMPLOYEES = 266
FP = (266, 182054351325.0, 214325700529.0, 32271349204.0)

RESULTS = {"passed": [], "failed": [], "skipped": []}


def _ok(name, cond, detail=""):
	if cond:
		RESULTS["passed"].append(name)
		print("PASS", name, detail)
	else:
		RESULTS["failed"].append({"test": name, "detail": str(detail)})
		print("FAIL", name, detail)


def _skip(name, reason):
	RESULTS["skipped"].append({"test": name, "reason": reason})
	print("SKIP", name, reason)


def _kwargs(**extra):
	base = dict(
		payroll_entry=PE,
		expected_je=JE,
		expected_employees=EMPLOYEES,
		expected_je_debit=DEBIT,
		expected_je_rows=ROWS,
		required_je_posting_date=REQ_DATE,
		expected_ss_fingerprint=list(FP),
		idempotency_key="e2e-draft-replace-001",
	)
	base.update(extra)
	return base


def execute():
	assert frappe.local.site == "payroll-e2e.localhost", frappe.local.site
	frappe.set_user("Administrator")

	# Clear prior idempotency / locks for this key family
	from erpnext_extensions.extentionhrms import payroll_correction as pc

	frappe.cache().delete_value(pc._idempotency_cache_key("draft_replace:e2e-draft-replace-001"))
	frappe.cache().delete_value(pc._lock_cache_key(PE))

	# --- Baseline ---
	emp_n = frappe.db.count("Payroll Employee Detail", {"parent": PE})
	ss_n = frappe.db.count("Salary Slip", {"payroll_entry": PE, "docstatus": 1})
	_ok("baseline_266", emp_n == 266 and ss_n == 266, f"emp={emp_n} ss={ss_n}")
	_ok("baseline_je_submitted", frappe.db.get_value("Journal Entry", JE, "docstatus") == 1)
	je0 = frappe.get_doc("Journal Entry", JE)
	_ok("baseline_rows", len(je0.accounts) == ROWS, len(je0.accounts))
	_ok("baseline_debit", abs(flt(je0.total_debit) - DEBIT) < 0.01, je0.total_debit)
	_ok("baseline_posting", str(getdate(je0.posting_date)) == REQ_DATE, je0.posting_date)
	active_gl0 = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabGL Entry` WHERE voucher_no=%s AND is_cancelled=0", (JE,)
	)[0][0]
	_ok("baseline_active_gl", active_gl0 == ROWS, active_gl0)

	ss_before = frappe.get_all(
		"Salary Slip",
		filters={"payroll_entry": PE, "docstatus": 1},
		fields=["name", "net_pay", "gross_pay", "total_deduction", "journal_entry"],
	)
	ss_map = {
		r.name: (flt(r.net_pay), flt(r.gross_pay), flt(r.total_deduction), r.journal_entry)
		for r in ss_before
	}
	_ok("ss_map_266", len(ss_map) == 266)
	_ok("all_ss_linked_old_je", all(v[3] == JE for v in ss_map.values()))

	pe_posting_before = frappe.db.get_value("Payroll Entry", PE, "posting_date")

	# --- Negative dry-runs (no mutation) ---
	dry = payroll_replace_accrual_je_draft(action="dry_run", **_kwargs())
	_ok(
		"dry_run_pass",
		dry.get("dry_run_result") == "DRY_RUN_PASS" and dry.get("status") == "READY",
		dry.get("status"),
	)
	_ok("dry_run_no_mutate", dry.get("mutated") is False)
	_ok(
		"still_submitted_after_dry",
		frappe.db.get_value("Journal Entry", JE, "docstatus") == 1,
	)

	bad_je = payroll_replace_accrual_je_draft(
		action="dry_run", **_kwargs(expected_je="ACC-JV-DOES-NOT-EXIST", idempotency_key="e2e-draft-bad-je")
	)
	_ok("invalid_je_blocked", bad_je.get("dry_run_result") == "DRY_RUN_BLOCKED", bad_je.get("status"))

	bad_fp = payroll_replace_accrual_je_draft(
		action="dry_run", **_kwargs(expected_je_debit=1, idempotency_key="e2e-draft-bad-fp")
	)
	_ok("incorrect_totals_blocked", bad_fp.get("dry_run_result") == "DRY_RUN_BLOCKED")

	bad_rows = payroll_replace_accrual_je_draft(
		action="dry_run", **_kwargs(expected_je_rows=9999, idempotency_key="e2e-draft-bad-rows")
	)
	_ok("incorrect_rows_blocked", bad_rows.get("dry_run_result") == "DRY_RUN_BLOCKED")

	frappe.set_user("Guest")
	try:
		payroll_replace_accrual_je_draft(action="dry_run", **_kwargs(idempotency_key="e2e-draft-guest"))
		_ok("guest_rejected", False, "Guest was allowed")
	except Exception:
		_ok("guest_rejected", True)
	finally:
		frappe.set_user("Administrator")

	try:
		if not frappe.db.exists("User", "payroll_draft_noperm@example.com"):
			u = frappe.get_doc(
				{
					"doctype": "User",
					"email": "payroll_draft_noperm@example.com",
					"first_name": "NoPermDraft",
					"send_welcome_email": 0,
					"user_type": "System User",
				}
			)
			u.insert(ignore_permissions=True)
			frappe.db.commit()
		frappe.set_user("payroll_draft_noperm@example.com")
		try:
			payroll_replace_accrual_je_draft(
				action="dry_run", **_kwargs(idempotency_key="e2e-draft-noperm")
			)
			_ok("noperm_rejected", False, "unprivileged user allowed")
		except Exception:
			_ok("noperm_rejected", True)
		finally:
			frappe.set_user("Administrator")
	except Exception as e:
		_skip("noperm_rejected", f"could not create user: {e}")
		frappe.set_user("Administrator")

	# --- Execute ---
	try:
		exe = payroll_replace_accrual_je_draft(action="execute", **_kwargs())
		_ok("execute_status", exe.get("status") == "EXECUTED", exe.get("status"))
		_ok("execute_mutated", exe.get("mutated") is True)
		new_je_info = exe["result"]["new_je"]
		_ok("new_je_draft", int(new_je_info["docstatus"]) == 0, new_je_info)
		_ok(
			"new_je_posting_date",
			str(getdate(new_je_info["posting_date"])) == REQ_DATE,
			new_je_info["posting_date"],
		)
		_ok(
			"new_je_debit",
			abs(flt(new_je_info["total_debit"]) - DEBIT) < 0.01,
			new_je_info["total_debit"],
		)
		_ok(
			"new_je_credit",
			abs(flt(new_je_info["total_credit"]) - DEBIT) < 0.01,
			new_je_info["total_credit"],
		)
		_ok("new_je_rows", int(new_je_info["rows"]) == ROWS, new_je_info["rows"])
		new_je_name = new_je_info["name"]
	except Exception as e:
		_ok(
			"execute_status",
			False,
			f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1200:]}",
		)
		return _finish()

	# --- Post checks ---
	# Naming series may reuse the cancelled name; if reused, the existing doc must be Draft.
	if new_je_name == JE:
		_ok(
			"old_je_replaced_same_name",
			frappe.db.exists("Journal Entry", JE)
			and frappe.db.get_value("Journal Entry", JE, "docstatus") == 0,
			"naming series reused cancelled name as Draft",
		)
	else:
		_ok("old_je_deleted", not frappe.db.exists("Journal Entry", JE))
	# Any GL under the original voucher number must be inactive (cancel marks is_cancelled=1;
	# a reused Draft name must have zero active GL).
	old_gl = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabGL Entry` WHERE voucher_no=%s AND is_cancelled=0", (JE,)
	)[0][0]
	_ok("old_je_zero_active_gl", old_gl == 0, old_gl)

	new_je = frappe.get_doc("Journal Entry", new_je_name)
	_ok("new_je_exists_draft", new_je.docstatus == 0)
	_ok("new_je_posting_persist", str(getdate(new_je.posting_date)) == REQ_DATE)
	_ok("new_je_balanced", abs(flt(new_je.total_debit) - flt(new_je.total_credit)) < 0.01)
	_ok("new_je_rows_persist", len(new_je.accounts) == ROWS)
	new_gl = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabGL Entry` WHERE voucher_no=%s AND is_cancelled=0",
		(new_je_name,),
	)[0][0]
	_ok("new_je_zero_active_gl", new_gl == 0, new_gl)

	pe = frappe.get_doc("Payroll Entry", PE)
	_ok("pe_still_submitted", pe.docstatus == 1 and pe.status == "Submitted")
	_ok("employees_still_266", len(pe.employees) == 266 and pe.number_of_employees == 266)
	_ok(
		"pe_posting_unchanged",
		str(getdate(pe.posting_date)) == str(getdate(pe_posting_before)),
		pe.posting_date,
	)

	ss_after = frappe.get_all(
		"Salary Slip",
		filters={"payroll_entry": PE, "docstatus": 1},
		fields=["name", "net_pay", "gross_pay", "total_deduction", "journal_entry", "docstatus"],
	)
	_ok("ss_still_266", len(ss_after) == 266)
	amounts_ok = True
	links_ok = True
	for r in ss_after:
		prev = ss_map.get(r.name)
		if not prev:
			amounts_ok = False
			continue
		if abs(flt(r.net_pay) - prev[0]) > 0.01 or abs(flt(r.gross_pay) - prev[1]) > 0.01:
			amounts_ok = False
		if r.journal_entry != new_je_name:
			links_ok = False
	_ok("ss_amounts_unchanged", amounts_ok)
	_ok("ss_linked_to_draft_je", links_ok, "HRMS set_journal_entry_in_salary_slips on Draft")
	# No slip may point at a missing JE or a submitted/cancelled non-target voucher.
	orphan = False
	for r in ss_after:
		if not r.journal_entry:
			orphan = True
			continue
		ds = frappe.db.get_value("Journal Entry", r.journal_entry, "docstatus")
		if ds is None or int(ds) != 0 or r.journal_entry != new_je_name:
			orphan = True
	_ok("no_dangling_or_orphan_je_refs", not orphan)
	jes = frappe.db.sql_list(
		"""
		SELECT DISTINCT journal_entry FROM `tabSalary Slip`
		WHERE payroll_entry=%s AND docstatus=1 AND IFNULL(journal_entry,'')!=''
		""",
		(PE,),
	)
	_ok("single_linked_je", jes == [new_je_name], jes)

	# Duplicate / idempotent
	dup = payroll_replace_accrual_je_draft(action="execute", **_kwargs())
	_ok("duplicate_already_completed", dup.get("status") == "ALREADY_COMPLETED", dup.get("status"))
	_ok("duplicate_not_mutated", dup.get("mutated") is False)

	dry2 = payroll_replace_accrual_je_draft(
		action="dry_run", **_kwargs(idempotency_key="e2e-draft-after-0001")
	)
	_ok("dry_run_after_already", dry2.get("status") == "ALREADY_COMPLETED")

	# Concurrent lock
	raised = False
	try:
		with pc._payroll_entry_lock(PE, "draft-holder-lock-001"):
			try:
				with pc._payroll_entry_lock(PE, "draft-contender-lock-001"):
					pass
			except Exception:
				raised = True
		_ok("concurrent_lock_blocks", raised)
	except Exception as e:
		_ok("concurrent_lock_blocks", False, e)

	# Draft must remain draft — do not submit during verification
	_ok(
		"still_draft_end",
		frappe.db.get_value("Journal Entry", new_je_name, "docstatus") == 0,
	)

	for name in (
		"failure_during_je_cancel",
		"failure_during_je_create",
		"transaction_rollback_midflight",
	):
		_skip(name, "requires separate restore fixture after successful E2E")

	return _finish()


def _finish():
	out = {
		"passed": RESULTS["passed"],
		"failed": RESULTS["failed"],
		"skipped": RESULTS["skipped"],
		"pass_count": len(RESULTS["passed"]),
		"fail_count": len(RESULTS["failed"]),
		"skip_count": len(RESULTS["skipped"]),
	}
	print("SUMMARY", json.dumps({k: out[k] for k in ("pass_count", "fail_count", "skip_count")}))
	print("FAILED_DETAIL", json.dumps(RESULTS["failed"], ensure_ascii=False, indent=2))
	return out
