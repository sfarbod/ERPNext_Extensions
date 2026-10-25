"""E2E runner for payroll_remove_employee_accrual on payroll-e2e.localhost.

Expects the site to be restored from the pre-correction 267-employee backup.
Does NOT touch development.localhost.
"""

from __future__ import annotations

import json
import traceback

import frappe
from frappe.utils import flt, getdate

from erpnext_extensions.extentionhrms.payroll_correction import payroll_remove_employee_accrual

PE = "شهریور 1405"
EMP = "HR-EMP-0035"
SS = "Sal Slip/HR-EMP-0035/00030"
JE = "ACC-JV-1405-172040"
REQ_DATE = "2026-09-22"
ORIG_DEBIT = 284839471791.0
CORR_DEBIT = 283672596839.0
ORIG_ROWS = 1365
CORR_ROWS = 1363
FP_ALL = (267, 182792231842.0, 215166716678.0, 32374484836.0)

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
		employee=EMP,
		salary_slip=SS,
		expected_je=JE,
		expected_employees_before=267,
		expected_je_debit=ORIG_DEBIT,
		required_je_posting_date=REQ_DATE,
		expected_employees_after=266,
		expected_je_rows=ORIG_ROWS,
		expected_corrected_je_debit=CORR_DEBIT,
		expected_corrected_je_rows=CORR_ROWS,
		expected_ss_fingerprint_all=list(FP_ALL),
		idempotency_key="e2e-shahrivar1405-001",
	)
	base.update(extra)
	return base


def execute():
	assert frappe.local.site == "payroll-e2e.localhost", frappe.local.site
	frappe.set_user("Administrator")

	# Baseline state
	emp_n = frappe.db.count("Payroll Employee Detail", {"parent": PE})
	ss_n = frappe.db.count("Salary Slip", {"payroll_entry": PE, "docstatus": 1})
	_ok("baseline_267", emp_n == 267 and ss_n == 267, f"emp={emp_n} ss={ss_n}")
	_ok("baseline_ss_exists", bool(frappe.db.exists("Salary Slip", SS)))
	_ok("baseline_je", frappe.db.get_value("Journal Entry", JE, "docstatus") == 1)

	# Capture other SS fingerprints before execute
	others = frappe.get_all(
		"Salary Slip",
		filters={"payroll_entry": PE, "docstatus": 1, "employee": ["!=", EMP]},
		fields=["name", "net_pay", "gross_pay", "total_deduction"],
	)
	other_map = {r.name: (flt(r.net_pay), flt(r.gross_pay), flt(r.total_deduction)) for r in others}
	_ok("other_map_266", len(other_map) == 266)

	# 1) Valid dry-run
	dry = payroll_remove_employee_accrual(action="dry_run", **_kwargs())
	_ok("dry_run_pass", dry.get("dry_run_result") == "DRY_RUN_PASS" and dry.get("status") == "READY", dry.get("status"))
	_ok("dry_run_no_mutate", dry.get("mutated") is False)

	# 13-16 negative dry-runs (no mutation)
	bad_emp = payroll_remove_employee_accrual(
		action="dry_run", **_kwargs(employee="HR-EMP-9999", idempotency_key="e2e-bad-emp-0001")
	)
	_ok("invalid_employee_blocked", bad_emp.get("dry_run_result") == "DRY_RUN_BLOCKED")

	bad_ss = payroll_remove_employee_accrual(
		action="dry_run",
		**_kwargs(salary_slip="Sal Slip/HR-EMP-0001/00030", idempotency_key="e2e-bad-ss-0001"),
	)
	_ok("incorrect_salary_slip_blocked", bad_ss.get("dry_run_result") == "DRY_RUN_BLOCKED")

	bad_je = payroll_remove_employee_accrual(
		action="dry_run", **_kwargs(expected_je="ACC-JV-DOES-NOT-EXIST", idempotency_key="e2e-bad-je-0001")
	)
	_ok("incorrect_je_blocked", bad_je.get("dry_run_result") == "DRY_RUN_BLOCKED")

	bad_fp = payroll_remove_employee_accrual(
		action="dry_run", **_kwargs(expected_je_debit=1, idempotency_key="e2e-bad-fp-0001")
	)
	_ok("incorrect_fingerprint_blocked", bad_fp.get("dry_run_result") == "DRY_RUN_BLOCKED")

	# 17 Unauthorized user
	frappe.set_user("Guest")
	try:
		payroll_remove_employee_accrual(action="dry_run", **_kwargs(idempotency_key="e2e-guest-0001"))
		_ok("guest_rejected", False, "Guest was allowed")
	except Exception:
		_ok("guest_rejected", True)
	finally:
		frappe.set_user("Administrator")

	# 18 Missing permissions — create a user without required roles if possible
	try:
		if not frappe.db.exists("User", "payroll_corr_noperm@example.com"):
			u = frappe.get_doc(
				{
					"doctype": "User",
					"email": "payroll_corr_noperm@example.com",
					"first_name": "NoPerm",
					"send_welcome_email": 0,
					"user_type": "System User",
				}
			)
			u.insert(ignore_permissions=True)
			frappe.db.commit()
		frappe.set_user("payroll_corr_noperm@example.com")
		try:
			payroll_remove_employee_accrual(action="dry_run", **_kwargs(idempotency_key="e2e-noperm-0001"))
			_ok("noperm_rejected", False, "unprivileged user allowed")
		except Exception:
			_ok("noperm_rejected", True)
		finally:
			frappe.set_user("Administrator")
	except Exception as e:
		_skip("noperm_rejected", f"could not create user: {e}")
		frappe.set_user("Administrator")

	# Ensure still 267 before execute
	_ok(
		"still_267_before_execute",
		frappe.db.count("Payroll Employee Detail", {"parent": PE}) == 267,
	)

	# 2) Full execute
	try:
		exe = payroll_remove_employee_accrual(action="execute", **_kwargs())
		_ok("execute_status", exe.get("status") == "EXECUTED", exe.get("status"))
		_ok("execute_mutated", exe.get("mutated") is True)
		new_je = exe["result"]["new_je"]
		_ok("posting_date", str(getdate(new_je["posting_date"])) == REQ_DATE, new_je["posting_date"])
		_ok("debit_match", abs(flt(new_je["total_debit"]) - CORR_DEBIT) < 0.01, new_je["total_debit"])
		_ok("credit_match", abs(flt(new_je["total_credit"]) - CORR_DEBIT) < 0.01, new_je["total_credit"])
		_ok("rows_match", int(new_je["rows"]) == CORR_ROWS, new_je["rows"])
		_ok("je_submitted", int(new_je["docstatus"]) == 1)
	except Exception as e:
		_ok("execute_status", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-800:]}")
		return _finish()

	# 3-10 post checks
	pe = frappe.get_doc("Payroll Entry", PE)
	_ok("pe_still_submitted", pe.docstatus == 1 and pe.status == "Submitted")
	_ok("employees_266", len(pe.employees) == 266 and pe.number_of_employees == 266)
	_ok("target_absent_pe", EMP not in {r.employee for r in pe.employees})
	_ok("ss_266", frappe.db.count("Salary Slip", {"payroll_entry": PE, "docstatus": 1}) == 266)
	_ok("target_ss_deleted", not frappe.db.exists("Salary Slip", SS))
	_ok("pe_posting_unchanged", str(getdate(pe.posting_date)) == "2026-10-21")

	unchanged = True
	for name, vals in other_map.items():
		row = frappe.db.get_value(
			"Salary Slip", name, ["docstatus", "net_pay", "gross_pay", "total_deduction"], as_dict=1
		)
		if not row or row.docstatus != 1:
			unchanged = False
			break
		if abs(flt(row.net_pay) - vals[0]) > 0.01 or abs(flt(row.gross_pay) - vals[1]) > 0.01:
			unchanged = False
			break
	_ok("other_ss_unchanged", unchanged)

	jes = frappe.db.sql_list(
		"""
		SELECT DISTINCT journal_entry FROM `tabSalary Slip`
		WHERE payroll_entry=%s AND docstatus=1 AND IFNULL(journal_entry,'')!=''
		""",
		(PE,),
	)
	_ok("single_active_je", len(jes) == 1, jes)
	je = frappe.get_doc("Journal Entry", jes[0])
	_ok("gl_balanced", abs(flt(je.total_debit) - flt(je.total_credit)) < 0.01)
	party = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabJournal Entry Account` WHERE parent=%s AND party=%s",
		(je.name, EMP),
	)[0][0]
	_ok("no_target_party", party == 0)
	_ok("emp_master_intact", frappe.db.exists("Employee", EMP))

	# dimensions: cost centers present
	ccs = frappe.db.sql_list(
		"SELECT DISTINCT cost_center FROM `tabJournal Entry Account` WHERE parent=%s AND IFNULL(cost_center,'')!=''",
		(je.name,),
	)
	_ok("cost_centers_present", len(ccs) >= 1, ccs)

	# 11-12 duplicate / idempotent
	dup = payroll_remove_employee_accrual(action="execute", **_kwargs())
	_ok("duplicate_already_completed", dup.get("status") == "ALREADY_COMPLETED", dup.get("status"))
	_ok("duplicate_not_mutated", dup.get("mutated") is False)

	dry2 = payroll_remove_employee_accrual(action="dry_run", **_kwargs(idempotency_key="e2e-after-0001"))
	_ok("dry_run_after_already", dry2.get("status") == "ALREADY_COMPLETED")

	# 19 Concurrent — hold lock and expect second acquire to fail
	from erpnext_extensions.extentionhrms import payroll_correction as pc

	raised = False
	try:
		with pc._payroll_entry_lock(PE, "holder-lock-key-001"):
			try:
				with pc._payroll_entry_lock(PE, "contender-lock-key-001"):
					pass
			except Exception:
				raised = True
		_ok("concurrent_lock_blocks", raised)
	except Exception as e:
		_ok("concurrent_lock_blocks", False, e)

	# Failure-path tests 20-24: skipped on post-correction site (would need another restore)
	for name in (
		"failure_during_je_cancel",
		"failure_during_ss_delete",
		"failure_during_je_create",
		"failure_during_je_submit",
		"transaction_rollback_midflight",
	):
		_skip(name, "requires separate pre-correction restore fixture after successful E2E")

	_ok("no_second_active_je", len(jes) == 1)
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
	return out
