# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

import frappe
from frappe.utils import cint


def get_pm_settings():
	if not frappe.db.exists("DocType", "PM Settings"):
		return None
	try:
		return frappe.get_single("PM Settings")
	except Exception:
		return None


def get_pm_holder_name(employee: str, company: str) -> str | None:
	if not employee or not company:
		return None
	return frappe.db.get_value(
		"PM Holder",
		{"employee": employee, "company": company, "is_blocked": 0},
		"name",
	)


def employee_has_draft_pm_clearance(employee: str, company: str) -> bool:
	"""True only for true Draft clearances (not Pending* approval at docstatus 0)."""
	if not frappe.db.has_table("PM Clearance"):
		return False
	r = frappe.db.sql(
		"""
		select name from `tabPM Clearance`
		where employee=%s and company=%s and docstatus=0
			and ifnull(status, '') in ('', 'Draft')
			and ifnull(status, '') != 'Cancelled'
			and (
				ifnull(workflow_state, '') = ''
				or workflow_state = 'Draft'
				or workflow_state in (
					select name from `tabWorkflow State`
					where workflow_state_name = 'Draft'
				)
			)
		limit 1
		""",
		(employee, company),
	)
	return bool(r)


def petty_clearance_requires_workflow_approval() -> bool:
	"""When True, PM Clearance submit requires non-empty workflow_state in Approved state."""
	s = get_pm_settings()
	if not s:
		return True
	return cint(getattr(s, "require_workflow_approval", 1)) == 1
