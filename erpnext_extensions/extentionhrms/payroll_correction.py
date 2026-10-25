# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Narrowly scoped Payroll Entry correction API.

Whitelisted method::

    erpnext_extensions.extentionhrms.payroll_correction.payroll_remove_employee_accrual

Removes one verified employee from a submitted Payroll Entry and regenerates
the accrual Journal Entry via ``PayrollEntryWithAccountingDimensions``.

Security / safety invariants
----------------------------
* ``allow_guest=False``; role gate: System Manager / Accounts Manager / HR Manager.
* No credentials in code or logs.
* Mutations require exact document identifiers (Payroll Entry, Employee ID,
  Salary Slip, Journal Entry) plus accounting fingerprint gates.
* No direct SQL deletes of accounting documents.
* Employee master is never modified.
* Payroll Entry ``posting_date`` is never persisted as changed; JE date uses an
  in-memory override only.
* Redis lock on Payroll Entry prevents concurrent corrections.
* Idempotency key returns ``ALREADY_COMPLETED`` only after full corrected-state
  verification.

Transaction note
----------------
Frappe document cancel/submit/delete for Journal Entry and Salary Slip perform
their own lifecycle work (GL, linked fields). Intermediate commits are required
after irreversible lifecycle steps so subsequent steps see consistent state.
On failure after a checkpoint, the API rolls back the *current* uncommitted
work and returns a recovery hint; full recovery is restore-from-backup.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, getdate, now_datetime

REQUIRED_ROLES = ("System Manager", "Accounts Manager", "HR Manager")
LOCK_TTL_SECONDS = 900
IDEMPOTENCY_TTL_SECONDS = 60 * 60 * 24 * 14
EMPLOYEE_ID_RE = re.compile(r"^HR-EMP-\d+$")
MAX_IDEMPOTENCY_KEY_LEN = 128


def _assert_permission() -> None:
	if frappe.session.user == "Guest":
		frappe.throw(_("Guest access is not allowed"), frappe.PermissionError)
	frappe.only_for(REQUIRED_ROLES)


def _log(msg: str, data: dict | None = None) -> None:
	"""Structured log without secrets or large payroll payloads."""
	safe = {}
	for k, v in (data or {}).items():
		if k.lower() in {"api_key", "api_secret", "password", "authorization", "token"}:
			continue
		safe[k] = v
	frappe.logger("payroll_correction").info(f"{msg} {json.dumps(safe, default=str)[:1500]}")


def _idempotency_cache_key(key: str) -> str:
	return f"payroll_correction:idempotency:{key}"


def _lock_cache_key(payroll_entry: str) -> str:
	return f"payroll_correction:lock:{payroll_entry}"


@contextmanager
def _payroll_entry_lock(payroll_entry: str, idempotency_key: str):
	"""Cross-process lock via Redis SET NX (site-prefixed). Not in-memory-only."""
	cache = frappe.cache()
	logical_key = _lock_cache_key(payroll_entry)
	raw_key = cache.make_key(logical_key)
	token = f"{idempotency_key}:{frappe.session.user}:{now_datetime()}".encode()
	acquired = bool(cache.set(name=raw_key, value=token, nx=True, ex=LOCK_TTL_SECONDS))
	if not acquired:
		holder = cache.get(raw_key)
		frappe.throw(
			_("Payroll correction already in progress for {0} (lock={1})").format(
				payroll_entry, holder
			),
			frappe.ValidationError,
		)
	try:
		yield token.decode()
	finally:
		try:
			if cache.get(raw_key) == token:
				cache.delete(raw_key)
		except Exception:
			pass


def _ss_fingerprint(payroll_entry: str, exclude_employee: str | None = None) -> tuple:
	conds = ["payroll_entry=%s", "docstatus=1"]
	args: list[Any] = [payroll_entry]
	if exclude_employee:
		conds.append("employee!=%s")
		args.append(exclude_employee)
	row = frappe.db.sql(
		f"""
		SELECT COUNT(*), COALESCE(SUM(net_pay),0), COALESCE(SUM(gross_pay),0),
		       COALESCE(SUM(total_deduction),0)
		FROM `tabSalary Slip`
		WHERE {' AND '.join(conds)}
		""",
		tuple(args),
	)[0]
	return (int(row[0]), flt(row[1]), flt(row[2]), flt(row[3]))


def _active_jes(payroll_entry: str) -> list[str]:
	return frappe.db.sql_list(
		"""
		SELECT DISTINCT journal_entry
		FROM `tabSalary Slip`
		WHERE payroll_entry=%s AND docstatus=1 AND IFNULL(journal_entry,'')!=''
		""",
		(payroll_entry,),
	)


def _validate_params(
	*,
	action: str,
	payroll_entry: str,
	employee: str,
	salary_slip: str,
	expected_je: str,
	idempotency_key: str,
	required_je_posting_date: str,
) -> str:
	action = (action or "").strip().lower()
	if action not in ("dry_run", "execute"):
		frappe.throw(_("action must be dry_run or execute"))
	for label, value in (
		("payroll_entry", payroll_entry),
		("employee", employee),
		("salary_slip", salary_slip),
		("expected_je", expected_je),
		("idempotency_key", idempotency_key),
		("required_je_posting_date", required_je_posting_date),
	):
		if not value or not str(value).strip():
			frappe.throw(_("{0} is required").format(label))
	if not EMPLOYEE_ID_RE.match(str(employee).strip()):
		frappe.throw(_("employee must be an Employee ID matching HR-EMP-#####"))
	if not str(salary_slip).startswith("Sal Slip/"):
		frappe.throw(_("salary_slip must be a Salary Slip name"))
	key = str(idempotency_key).strip()
	if len(key) < 8 or len(key) > MAX_IDEMPOTENCY_KEY_LEN:
		frappe.throw(_("idempotency_key length must be between 8 and {0}").format(MAX_IDEMPOTENCY_KEY_LEN))
	getdate(required_je_posting_date)  # raises on invalid
	return action


def _verify_already_completed(
	*,
	payroll_entry: str,
	employee: str,
	salary_slip: str,
	expected_employees_after: int,
	required_je_posting_date: str,
	expected_corrected_je_debit: float | None,
	expected_corrected_je_rows: int | None,
) -> dict[str, Any] | None:
	"""Return completion payload only when *all* corrected-state gates pass."""
	pe = frappe.get_doc("Payroll Entry", payroll_entry)
	unique = {r.employee for r in pe.employees}
	if pe.docstatus != 1:
		return None
	if len(pe.employees) != expected_employees_after or len(unique) != expected_employees_after:
		return None
	if employee in unique:
		return None
	if frappe.db.exists("Salary Slip", salary_slip):
		return None
	fp = _ss_fingerprint(payroll_entry)
	if fp[0] != expected_employees_after:
		return None
	jes = _active_jes(payroll_entry)
	if len(jes) != 1:
		return None
	je = frappe.get_doc("Journal Entry", jes[0])
	if je.docstatus != 1:
		return None
	if getdate(je.posting_date) != getdate(required_je_posting_date):
		return None
	if abs(flt(je.total_debit) - flt(je.total_credit)) > 0.01:
		return None
	if expected_corrected_je_debit is not None and abs(flt(je.total_debit) - flt(expected_corrected_je_debit)) > 0.01:
		return None
	if expected_corrected_je_rows is not None and len(je.accounts) != int(expected_corrected_je_rows):
		return None
	party_left = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabJournal Entry Account` WHERE parent=%s AND party=%s",
		(je.name, employee),
	)[0][0]
	if party_left:
		return None
	return {
		"status": "ALREADY_COMPLETED",
		"payroll_entry": payroll_entry,
		"employees": len(pe.employees),
		"ss_fingerprint": fp,
		"journal_entry": {
			"name": je.name,
			"docstatus": je.docstatus,
			"posting_date": str(je.posting_date),
			"total_debit": je.total_debit,
			"total_credit": je.total_credit,
			"rows": len(je.accounts),
		},
	}


def _preflight(
	*,
	payroll_entry: str,
	employee: str,
	salary_slip: str,
	expected_je: str,
	expected_employees_before: int,
	expected_je_debit: float,
	required_je_posting_date: str,
	expected_employees_after: int,
	expected_je_rows: int | None = None,
	expected_ss_fingerprint_all: tuple | None = None,
	expected_corrected_je_debit: float | None = None,
	expected_corrected_je_rows: int | None = None,
) -> dict[str, Any]:
	out: dict[str, Any] = {"ok": True, "errors": [], "warnings": [], "state": {}}

	def fail(msg: str) -> None:
		out["ok"] = False
		out["errors"].append(msg)

	completed = _verify_already_completed(
		payroll_entry=payroll_entry,
		employee=employee,
		salary_slip=salary_slip,
		expected_employees_after=expected_employees_after,
		required_je_posting_date=required_je_posting_date,
		expected_corrected_je_debit=expected_corrected_je_debit,
		expected_corrected_je_rows=expected_corrected_je_rows,
	)
	if completed:
		out["state"]["already_corrected"] = True
		out["state"]["completed"] = completed
		out["warnings"].append("Correction already applied — ALREADY_COMPLETED.")
		return out
	out["state"]["already_corrected"] = False

	if not frappe.db.exists("Payroll Entry", payroll_entry):
		fail(f"Payroll Entry not found: {payroll_entry}")
		return out

	# Lock row for consistency during preflight (execute re-validates under Redis lock)
	frappe.db.get_value("Payroll Entry", payroll_entry, "name", for_update=True)
	pe = frappe.get_doc("Payroll Entry", payroll_entry)
	out["state"]["pe_docstatus"] = pe.docstatus
	out["state"]["pe_status"] = pe.status
	out["state"]["pe_posting_date"] = str(pe.posting_date)
	out["state"]["pe_employees"] = len(pe.employees)
	out["state"]["pe_number_of_employees"] = pe.number_of_employees
	out["state"]["pe_modified"] = str(pe.modified)

	if pe.docstatus != 1:
		fail(f"Payroll Entry must be submitted; docstatus={pe.docstatus}")

	unique = {r.employee for r in pe.employees}
	target_rows = [r for r in pe.employees if r.employee == employee]
	out["state"]["unique_employees"] = len(unique)
	out["state"]["target_rows"] = len(target_rows)

	if len(pe.employees) != expected_employees_before or len(unique) != expected_employees_before:
		fail(
			f"Employee count mismatch: rows={len(pe.employees)} unique={len(unique)} "
			f"expected_before={expected_employees_before}"
		)
	if len(target_rows) != 1:
		fail(f"Expected exactly one Employee Details row for {employee}, found {len(target_rows)}")

	if not frappe.db.exists("Employee", employee):
		fail(f"Employee master missing: {employee}")
	else:
		out["state"]["employee_name"] = frappe.db.get_value("Employee", employee, "employee_name")

	if not frappe.db.exists("Salary Slip", salary_slip):
		fail(f"Salary Slip not found: {salary_slip}")
	else:
		ss = frappe.get_doc("Salary Slip", salary_slip)
		out["state"]["ss"] = {
			"name": ss.name,
			"employee": ss.employee,
			"docstatus": ss.docstatus,
			"journal_entry": ss.journal_entry,
			"net_pay": ss.net_pay,
			"gross_pay": ss.gross_pay,
		}
		if ss.employee != employee:
			fail(f"Salary Slip employee mismatch: {ss.employee} != {employee}")
		if ss.payroll_entry != payroll_entry:
			fail(f"Salary Slip payroll_entry mismatch: {ss.payroll_entry}")
		if ss.docstatus != 1:
			fail(f"Salary Slip must be submitted; docstatus={ss.docstatus}")

	fp_all = _ss_fingerprint(payroll_entry)
	fp_wo = _ss_fingerprint(payroll_entry, exclude_employee=employee)
	out["state"]["ss_fingerprint_all"] = fp_all
	out["state"]["ss_fingerprint_without_target"] = fp_wo
	if fp_all[0] != expected_employees_before:
		fail(f"Submitted SS count {fp_all[0]} != expected {expected_employees_before}")
	if expected_ss_fingerprint_all is not None:
		exp = tuple(expected_ss_fingerprint_all)
		if (
			fp_all[0] != int(exp[0])
			or abs(flt(fp_all[1]) - flt(exp[1])) > 0.01
			or abs(flt(fp_all[2]) - flt(exp[2])) > 0.01
			or abs(flt(fp_all[3]) - flt(exp[3])) > 0.01
		):
			fail(f"SS fingerprint mismatch: got={fp_all} expected={exp}")

	jes = _active_jes(payroll_entry)
	out["state"]["active_jes"] = jes
	if len(jes) != 1:
		fail(f"Expected exactly one active accrual JE on slips, found {jes}")
	elif jes[0] != expected_je:
		fail(f"Active JE {jes[0]} != expected {expected_je}")
	else:
		je = frappe.get_doc("Journal Entry", expected_je)
		out["state"]["je"] = {
			"name": je.name,
			"docstatus": je.docstatus,
			"posting_date": str(je.posting_date),
			"total_debit": je.total_debit,
			"total_credit": je.total_credit,
			"rows": len(je.accounts),
		}
		if je.docstatus != 1:
			fail(f"JE docstatus must be 1; got {je.docstatus}")
		if abs(flt(je.total_debit) - flt(expected_je_debit)) > 0.01:
			fail(f"JE debit {je.total_debit} != expected {expected_je_debit}")
		if abs(flt(je.total_debit) - flt(je.total_credit)) > 0.01:
			fail("JE not balanced")
		if expected_je_rows is not None and len(je.accounts) != int(expected_je_rows):
			fail(f"JE rows {len(je.accounts)} != expected {expected_je_rows}")

	try:
		pay_ss = frappe.get_all(
			"Payment Entry Reference",
			filters={"reference_doctype": "Salary Slip", "reference_name": salary_slip},
			pluck="parent",
		)
		pay_je = frappe.get_all(
			"Payment Entry Reference",
			filters={"reference_doctype": "Journal Entry", "reference_name": expected_je},
			pluck="parent",
		)
		out["state"]["payment_refs_ss"] = pay_ss
		out["state"]["payment_refs_je"] = pay_je
		if pay_ss or pay_je:
			fail(f"Payment Entry references exist: ss={pay_ss} je={pay_je}")
	except frappe.PermissionError:
		out["warnings"].append("Payment Entry Reference not readable; skipped dependency check")

	out["state"]["required_je_posting_date"] = str(getdate(required_je_posting_date))
	out["state"]["plan"] = [
		"Cancel + delete accrual JE",
		f"Cancel + delete Salary Slip {salary_slip}",
		f"Remove {employee} from Payroll Entry.employees",
		"make_accrual_jv_entry with in-memory posting_date override",
		"Submit new JE; verify debit=credit and posting date",
	]
	return out


def _clear_ss_journal_links(payroll_entry: str) -> None:
	from frappe.query_builder import DocType

	SalarySlip = DocType("Salary Slip")
	frappe.qb.update(SalarySlip).set(SalarySlip.journal_entry, None).where(
		SalarySlip.payroll_entry == payroll_entry
	).run()


def _execute_mutation(
	*,
	payroll_entry: str,
	employee: str,
	salary_slip: str,
	expected_je: str,
	required_je_posting_date: str,
	expected_employees_after: int,
	expected_corrected_je_debit: float | None,
	expected_corrected_je_rows: int | None,
	other_ss_fingerprint: tuple,
) -> dict[str, Any]:
	"""Mutating path. Re-validates identities immediately before each destructive step."""
	result: dict[str, Any] = {"steps": [], "checkpoints": []}

	def checkpoint(name: str) -> None:
		frappe.db.commit()
		result["checkpoints"].append(name)

	# --- Cancel JE ---
	if not frappe.db.exists("Journal Entry", expected_je):
		frappe.throw(_("Journal Entry {0} missing at execute time").format(expected_je))
	je_ds = frappe.db.sql(
		"SELECT docstatus FROM `tabJournal Entry` WHERE name=%s FOR UPDATE", (expected_je,)
	)[0][0]
	resumed_cancel = False
	if je_ds == 1:
		je = frappe.get_doc("Journal Entry", expected_je)
		# ERPNext queues JE cancel/submit when accounts > 100 unless meta.queue_in_background.
		# Force the synchronous `_cancel` path so this API can verify completion in-process.
		je.meta.queue_in_background = 1
		je._cancel()
		checkpoint("je_cancelled")
	elif je_ds == 2:
		resumed_cancel = True
		checkpoint("je_already_cancelled")
	else:
		frappe.throw(_("Journal Entry {0} has unexpected docstatus {1}").format(expected_je, je_ds))

	je_ds = frappe.db.sql(
		"SELECT docstatus FROM `tabJournal Entry` WHERE name=%s", (expected_je,)
	)[0][0]
	if je_ds != 2:
		frappe.throw(_("JE cancel did not set docstatus=2 (got {0})").format(je_ds))
	active_gl = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabGL Entry`
		WHERE voucher_type='Journal Entry' AND voucher_no=%s AND is_cancelled=0
		""",
		(expected_je,),
	)[0][0]
	if active_gl:
		frappe.throw(_("Active GL rows remain after JE cancel: {0}").format(active_gl))
	result["steps"].append({"cancel_je": expected_je, "ok": True, "resumed": resumed_cancel})

	# --- Delete cancelled JE ---
	frappe.delete_doc("Journal Entry", expected_je, force=0)
	checkpoint("je_deleted")
	if frappe.db.exists("Journal Entry", expected_je):
		frappe.throw(_("JE still exists after delete"))
	result["steps"].append({"delete_je": expected_je, "ok": True})

	_clear_ss_journal_links(payroll_entry)
	checkpoint("ss_je_links_cleared")

	# --- Cancel + delete target SS ---
	ss = frappe.get_doc("Salary Slip", salary_slip)
	if ss.employee != employee or ss.payroll_entry != payroll_entry:
		frappe.throw(_("Refusing to delete Salary Slip — identity mismatch"))
	if ss.docstatus != 1:
		frappe.throw(_("Salary Slip is not submitted"))
	ss.cancel()
	checkpoint("ss_cancelled")
	ss_ds = frappe.db.sql("SELECT docstatus FROM `tabSalary Slip` WHERE name=%s", (salary_slip,))[0][0]
	if ss_ds != 2:
		frappe.throw(_("Salary Slip cancel failed (docstatus={0})").format(ss_ds))
	frappe.delete_doc("Salary Slip", salary_slip, force=0)
	checkpoint("ss_deleted")
	if frappe.db.exists("Salary Slip", salary_slip):
		frappe.throw(_("Salary Slip still exists after delete"))
	result["steps"].append({"delete_salary_slip": salary_slip, "ok": True})

	# Verify other slips unchanged
	fp_now = _ss_fingerprint(payroll_entry)
	if (
		fp_now[0] != int(other_ss_fingerprint[0])
		or abs(flt(fp_now[1]) - flt(other_ss_fingerprint[1])) > 0.01
		or abs(flt(fp_now[2]) - flt(other_ss_fingerprint[2])) > 0.01
	):
		frappe.throw(_("Remaining Salary Slip fingerprint changed unexpectedly: {0}").format(fp_now))

	# --- Remove PE employee row ---
	pe = frappe.get_doc("Payroll Entry", payroll_entry)
	pe_db_posting = pe.posting_date
	pe.employees = [r for r in pe.employees if r.employee != employee]
	pe.number_of_employees = len(pe.employees)
	if len(pe.employees) != expected_employees_after:
		frappe.throw(_("Employee Details count after removal is {0}").format(len(pe.employees)))
	# Submitted child table lacks allow_on_submit; mandatory custom attendance dates may be empty.
	pe.flags.ignore_mandatory = True
	pe.flags.ignore_validate_update_after_submit = True
	pe.save()
	checkpoint("pe_employee_removed")
	pe.reload()
	if pe.docstatus != 1 or employee in {r.employee for r in pe.employees}:
		frappe.throw(_("Payroll Entry employee removal failed"))
	result["steps"].append({"pe_employees": len(pe.employees), "ok": True})

	# --- Regenerate accrual JE ---
	pe = frappe.get_doc("Payroll Entry", payroll_entry)
	if pe.__class__.__name__ != "PayrollEntryWithAccountingDimensions":
		frappe.throw(_("Unexpected Payroll Entry controller: {0}").format(pe.__class__.__name__))
	pe.posting_date = getdate(required_je_posting_date)  # in-memory only
	submitted = frappe.get_all(
		"Salary Slip",
		filters={"payroll_entry": payroll_entry, "docstatus": 1},
		pluck="name",
		order_by="name",
	)
	if len(submitted) != expected_employees_after:
		frappe.throw(_("Expected {0} submitted slips, found {1}").format(expected_employees_after, len(submitted)))
	slip_docs = [frappe.get_doc("Salary Slip", n) for n in submitted]
	if any(s.journal_entry for s in slip_docs):
		frappe.throw(_("Some Salary Slips still reference a Journal Entry"))
	pe.make_accrual_jv_entry(slip_docs)
	checkpoint("je_regenerated")

	if str(getdate(frappe.db.get_value("Payroll Entry", payroll_entry, "posting_date"))) != str(
		getdate(pe_db_posting)
	):
		frappe.throw(_("Payroll Entry posting_date was unexpectedly changed"))

	new_jes = _active_jes(payroll_entry)
	if len(new_jes) != 1:
		frappe.throw(_("Expected one new JE on slips, found {0}").format(new_jes))
	new_je = frappe.get_doc("Journal Entry", new_jes[0])

	if getdate(new_je.posting_date) != getdate(required_je_posting_date):
		new_je.posting_date = getdate(required_je_posting_date)
		new_je.save()
		checkpoint("je_posting_date_corrected")
		new_je.reload()

	if getdate(new_je.posting_date) != getdate(required_je_posting_date):
		frappe.throw(_("JE posting_date is {0}, required {1}").format(new_je.posting_date, required_je_posting_date))
	if abs(flt(new_je.total_debit) - flt(new_je.total_credit)) > 0.5:
		frappe.throw(_("New JE unbalanced; refusing submit"))

	if new_je.docstatus == 0:
		# Force synchronous submit (same >100-row queue behaviour as cancel).
		new_je.meta.queue_in_background = 1
		new_je._submit()
		checkpoint("je_submitted")

	new_je_row = frappe.db.sql(
		"SELECT docstatus, posting_date, total_debit, total_credit FROM `tabJournal Entry` WHERE name=%s",
		(new_je.name,),
		as_dict=1,
	)[0]
	if int(new_je_row.docstatus) != 1:
		frappe.throw(_("New JE submit did not persist docstatus=1 (got {0})").format(new_je_row.docstatus))
	if getdate(new_je_row.posting_date) != getdate(required_je_posting_date):
		frappe.throw(_("Post-submit JE posting_date mismatch: {0}").format(new_je_row.posting_date))
	new_je = frappe.get_doc("Journal Entry", new_je.name)

	if expected_corrected_je_debit is not None and abs(flt(new_je.total_debit) - flt(expected_corrected_je_debit)) > 0.01:
		frappe.throw(
			_("Corrected JE debit {0} != expected {1}").format(new_je.total_debit, expected_corrected_je_debit)
		)
	if expected_corrected_je_rows is not None and len(new_je.accounts) != int(expected_corrected_je_rows):
		frappe.throw(
			_("Corrected JE rows {0} != expected {1}").format(len(new_je.accounts), expected_corrected_je_rows)
		)

	party_left = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabJournal Entry Account` WHERE parent=%s AND party=%s",
		(new_je.name, employee),
	)[0][0]
	if party_left:
		frappe.throw(_("Target employee party rows still present on new JE"))

	# Final fingerprint of remaining slips
	fp_final = _ss_fingerprint(payroll_entry)
	if fp_final[0] != expected_employees_after:
		frappe.throw(_("Final SS count {0} != {1}").format(fp_final[0], expected_employees_after))

	result["new_je"] = {
		"name": new_je.name,
		"docstatus": new_je.docstatus,
		"posting_date": str(new_je.posting_date),
		"total_debit": new_je.total_debit,
		"total_credit": new_je.total_credit,
		"rows": len(new_je.accounts),
	}
	result["ss_fingerprint"] = fp_final
	result["steps"].append({"regenerate_and_submit": new_je.name, "ok": True})
	return result


@frappe.whitelist(methods=["POST"], allow_guest=False)
def payroll_remove_employee_accrual(
	action: str,
	payroll_entry: str,
	employee: str,
	salary_slip: str,
	expected_je: str,
	expected_employees_before: int | str,
	expected_je_debit: float | str,
	required_je_posting_date: str,
	idempotency_key: str,
	expected_employees_after: int | str = 266,
	expected_je_rows: int | str | None = None,
	expected_corrected_je_debit: float | str | None = None,
	expected_corrected_je_rows: int | str | None = None,
	expected_ss_fingerprint_all: list | str | None = None,
) -> dict[str, Any]:
	"""Dry-run or execute removal of one employee from a submitted Payroll Entry accrual."""
	_assert_permission()
	action = _validate_params(
		action=action,
		payroll_entry=payroll_entry,
		employee=employee,
		salary_slip=salary_slip,
		expected_je=expected_je,
		idempotency_key=idempotency_key,
		required_je_posting_date=required_je_posting_date,
	)

	expected_employees_before = cint(expected_employees_before)
	expected_employees_after = cint(expected_employees_after)
	expected_je_debit = flt(expected_je_debit)
	expected_je_rows = cint(expected_je_rows) if expected_je_rows not in (None, "") else None
	expected_corrected_je_debit = (
		flt(expected_corrected_je_debit) if expected_corrected_je_debit not in (None, "") else None
	)
	expected_corrected_je_rows = (
		cint(expected_corrected_je_rows) if expected_corrected_je_rows not in (None, "") else None
	)
	if isinstance(expected_ss_fingerprint_all, str) and expected_ss_fingerprint_all:
		expected_ss_fingerprint_all = json.loads(expected_ss_fingerprint_all)

	cache_key = _idempotency_cache_key(str(idempotency_key).strip())
	prior = frappe.cache().get_value(cache_key)
	if prior and action == "execute" and prior.get("status") == "ALREADY_COMPLETED":
		return {
			"action": action,
			"status": "ALREADY_COMPLETED",
			"idempotent_replay": True,
			"prior_result": prior,
			"generated_at": str(now_datetime()),
			"mutated": False,
		}

	preflight_kwargs = dict(
		payroll_entry=payroll_entry,
		employee=employee,
		salary_slip=salary_slip,
		expected_je=expected_je,
		expected_employees_before=expected_employees_before,
		expected_je_debit=expected_je_debit,
		required_je_posting_date=required_je_posting_date,
		expected_employees_after=expected_employees_after,
		expected_je_rows=expected_je_rows,
		expected_ss_fingerprint_all=expected_ss_fingerprint_all,
		expected_corrected_je_debit=expected_corrected_je_debit,
		expected_corrected_je_rows=expected_corrected_je_rows,
	)

	if action == "dry_run":
		pre = _preflight(**preflight_kwargs)
		response = {
			"action": action,
			"generated_at": str(now_datetime()),
			"site": frappe.local.site,
			"preflight": pre,
			"mutated": False,
		}
		if pre["state"].get("already_corrected"):
			response["status"] = "ALREADY_COMPLETED"
			response["dry_run_result"] = "DRY_RUN_PASS"
			response["completed"] = pre["state"].get("completed")
		elif pre["ok"]:
			response["status"] = "READY"
			response["dry_run_result"] = "DRY_RUN_PASS"
		else:
			response["status"] = "BLOCKED"
			response["dry_run_result"] = "DRY_RUN_BLOCKED"
		_log("dry_run", {"status": response["status"], "payroll_entry": payroll_entry})
		return response

	# ----- execute -----
	with _payroll_entry_lock(payroll_entry, str(idempotency_key).strip()):
		# Re-validate after lock
		pre = _preflight(**preflight_kwargs)
		response: dict[str, Any] = {
			"action": action,
			"generated_at": str(now_datetime()),
			"site": frappe.local.site,
			"preflight": pre,
			"mutated": False,
		}

		if pre["state"].get("already_corrected"):
			completed = pre["state"]["completed"]
			response["status"] = "ALREADY_COMPLETED"
			response["completed"] = completed
			frappe.cache().set_value(cache_key, {**completed, "response_meta": {"site": frappe.local.site}}, expires_in_sec=IDEMPOTENCY_TTL_SECONDS)
			return response

		if not pre["ok"]:
			frappe.throw(_("Preflight failed: {0}").format("; ".join(pre["errors"])))

		other_fp = tuple(pre["state"]["ss_fingerprint_without_target"])
		try:
			result = _execute_mutation(
				payroll_entry=payroll_entry,
				employee=employee,
				salary_slip=salary_slip,
				expected_je=expected_je,
				required_je_posting_date=required_je_posting_date,
				expected_employees_after=expected_employees_after,
				expected_corrected_je_debit=expected_corrected_je_debit,
				expected_corrected_je_rows=expected_corrected_je_rows,
				other_ss_fingerprint=other_fp,
			)
		except Exception as exc:
			frappe.db.rollback()
			_log(
				"execute_failed",
				{
					"payroll_entry": payroll_entry,
					"error_type": type(exc).__name__,
					"recovery": "Restore from verified DB backup if checkpoints were committed",
				},
			)
			raise

		completed = {
			"status": "ALREADY_COMPLETED",
			"payroll_entry": payroll_entry,
			"employees": expected_employees_after,
			"ss_fingerprint": result["ss_fingerprint"],
			"journal_entry": result["new_je"],
			"executed_at": str(now_datetime()),
			"idempotency_key": str(idempotency_key).strip(),
		}
		response["mutated"] = True
		response["status"] = "EXECUTED"
		response["result"] = result
		response["completed"] = completed
		frappe.cache().set_value(cache_key, completed, expires_in_sec=IDEMPOTENCY_TTL_SECONDS)
		_log("execute_ok", {"new_je": result["new_je"]["name"], "payroll_entry": payroll_entry})
		return response
