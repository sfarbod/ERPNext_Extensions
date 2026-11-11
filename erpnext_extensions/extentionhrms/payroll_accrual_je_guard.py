# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Active Journal Entry guard for Payroll Entry accrual regeneration.

Root cause (HRMS):
- Accrual creation is driven by ``submit_salary_slips`` → ``make_accrual_jv_entry``.
- After Salary Slips are submitted there is no Desk action to recreate the accrual JE.
- ``get_sal_slip_list`` excludes any slip with a non-empty ``journal_entry``, including
  cancelled or deleted names, so a stale link permanently blocks the stock path.

Policy (v5.7.26):
- Block generation when any *active* linked JE exists (Draft=0 or Submitted=1).
- Allow generation when links are absent, cancelled (2), or point at missing docs.
- Clear inactive / missing Salary Slip ``journal_entry`` links before regenerating
  (relationship cleanup only; cancelled JE documents are preserved).
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _


ACTIVE_DOCSTATUS = (0, 1)


def collect_linked_journal_entry_names(payroll_entry: str) -> list[str]:
	"""All JE names associated with a Payroll Entry via slips or JE account refs."""
	from_slips = frappe.db.sql_list(
		"""
		SELECT DISTINCT journal_entry
		FROM `tabSalary Slip`
		WHERE payroll_entry=%s
		  AND docstatus < 2
		  AND IFNULL(journal_entry, '') != ''
		""",
		(payroll_entry,),
	)
	from_accounts = frappe.db.sql_list(
		"""
		SELECT DISTINCT parent
		FROM `tabJournal Entry Account`
		WHERE reference_type = 'Payroll Entry'
		  AND reference_name = %s
		""",
		(payroll_entry,),
	)
	# Preserve stable order for messages / tests.
	seen: set[str] = set()
	ordered: list[str] = []
	for name in list(from_slips or []) + list(from_accounts or []):
		if name and name not in seen:
			seen.add(name)
			ordered.append(name)
	return ordered


def classify_linked_journal_entries(payroll_entry: str) -> dict[str, list[str]]:
	"""Split linked JE names into active / cancelled / missing."""
	active: list[str] = []
	cancelled: list[str] = []
	missing: list[str] = []
	for name in collect_linked_journal_entry_names(payroll_entry):
		ds = frappe.db.get_value("Journal Entry", name, "docstatus")
		if ds is None:
			missing.append(name)
		elif int(ds) == 2:
			cancelled.append(name)
		elif int(ds) in ACTIVE_DOCSTATUS:
			active.append(name)
		else:
			# Unexpected docstatus — treat as active to fail closed.
			active.append(name)
	return {"active": active, "cancelled": cancelled, "missing": missing}


def get_active_accrual_journal_entries(payroll_entry: str) -> list[str]:
	return classify_linked_journal_entries(payroll_entry)["active"]


def clear_inactive_salary_slip_journal_links(payroll_entry: str) -> list[str]:
	"""Clear Salary Slip.journal_entry when the target JE is cancelled or missing.

	Does not touch slips linked to Draft/Submitted JEs. Returns cleared JE names.
	"""
	rows = frappe.db.sql(
		"""
		SELECT name, journal_entry
		FROM `tabSalary Slip`
		WHERE payroll_entry=%s
		  AND docstatus < 2
		  AND IFNULL(journal_entry, '') != ''
		""",
		(payroll_entry,),
		as_dict=True,
	)
	cleared: list[str] = []
	for row in rows:
		ss_name = row["name"] if isinstance(row, dict) else row.name
		je_name = row["journal_entry"] if isinstance(row, dict) else row.journal_entry
		ds = frappe.db.get_value("Journal Entry", je_name, "docstatus")
		if ds is None or int(ds) == 2:
			frappe.db.set_value(
				"Salary Slip",
				ss_name,
				"journal_entry",
				"",
				update_modified=False,
			)
			if je_name not in cleared:
				cleared.append(je_name)
	return cleared


def assert_no_active_accrual_journal_entry(payroll_entry: str) -> dict[str, list[str]]:
	classified = classify_linked_journal_entries(payroll_entry)
	active = classified["active"]
	if active:
		frappe.throw(
			_(
				"Cannot create a new Accrual Journal Entry for Payroll Entry {0} while "
				"active Journal Entry {1} exists (Draft or Submitted). "
				"Cancel the active Journal Entry first, or continue with that document."
			).format(payroll_entry, ", ".join(active)),
			title=_("Active Journal Entry Exists"),
		)
	return classified


def lock_payroll_entry(payroll_entry: str) -> None:
	"""Row-lock the Payroll Entry for concurrent generation attempts."""
	frappe.db.get_value("Payroll Entry", payroll_entry, "name", for_update=True)


def prepare_accrual_journal_regeneration(payroll_entry: str) -> dict[str, Any]:
	"""Lock, validate no active JE, clear inactive slip links. Safe to call before generate."""
	if not frappe.db.exists("Payroll Entry", payroll_entry):
		frappe.throw(_("Payroll Entry {0} not found").format(payroll_entry))

	lock_payroll_entry(payroll_entry)
	pe_ds = frappe.db.get_value("Payroll Entry", payroll_entry, "docstatus")
	if int(pe_ds or 0) != 1:
		frappe.throw(
			_("Payroll Entry {0} must be Submitted to generate an Accrual Journal Entry").format(
				payroll_entry
			)
		)

	classified = assert_no_active_accrual_journal_entry(payroll_entry)
	cleared = clear_inactive_salary_slip_journal_links(payroll_entry)
	# Re-check after cleanup in case another request created a Draft concurrently.
	classified_after = assert_no_active_accrual_journal_entry(payroll_entry)
	return {
		"classified_before": classified,
		"cleared_inactive_links": cleared,
		"classified_after": classified_after,
	}


# ---------------------------------------------------------------------------
# Desk / API whitelist wrappers
# ---------------------------------------------------------------------------
# ``frm.call("generate_accrual_journal_entry")`` resolves against the HRMS
# Payroll Entry *module* path, not the override_doctype_class controller.
# Desk must call these fully-qualified erpnext_extensions methods instead.


def _require_logged_in() -> None:
	if frappe.session.user == "Guest":
		frappe.throw(_("Login required"), frappe.PermissionError)


@frappe.whitelist(methods=["POST"])
def has_active_accrual_journal_entry(payroll_entry: str) -> dict[str, Any]:
	"""Whitelisted Desk helper — returns whether an active accrual JE exists."""
	_require_logged_in()
	if not payroll_entry:
		frappe.throw(_("payroll_entry is required"))
	if not frappe.db.exists("Payroll Entry", payroll_entry):
		frappe.throw(_("Payroll Entry {0} not found").format(payroll_entry))
	frappe.get_doc("Payroll Entry", payroll_entry).check_permission("read")
	active = get_active_accrual_journal_entries(payroll_entry)
	return {"has_active": bool(active), "active": active}


@frappe.whitelist(methods=["POST"])
def generate_accrual_journal_entry(payroll_entry: str) -> str:
	"""Whitelisted Desk entry-point; delegates to the Payroll Entry controller."""
	_require_logged_in()
	if not payroll_entry:
		frappe.throw(_("payroll_entry is required"))
	if not frappe.db.exists("Payroll Entry", payroll_entry):
		frappe.throw(_("Payroll Entry {0} not found").format(payroll_entry))

	pe = frappe.get_doc("Payroll Entry", payroll_entry)
	# Controller method enforces write permission + active-JE guard + Draft create.
	return pe.generate_accrual_journal_entry()
