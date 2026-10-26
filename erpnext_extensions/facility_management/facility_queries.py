# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import frappe
from frappe.desk.reportview import build_match_conditions
from frappe.utils import cint


def _normalize_link_filters(filters) -> frappe._dict:
	"""Accept dict or Frappe list-style filters from Link controls."""
	if not filters:
		return frappe._dict()
	if isinstance(filters, dict):
		return frappe._dict(filters)
	if isinstance(filters, str):
		import json

		filters = json.loads(filters)
		if isinstance(filters, dict):
			return frappe._dict(filters)
	if isinstance(filters, (list, tuple)):
		out = frappe._dict()
		for row in filters:
			if not row:
				continue
			# [doctype, fieldname, operator, value] or [fieldname, operator, value]
			if len(row) == 4:
				_dt, fieldname, operator, value = row
			elif len(row) == 3:
				fieldname, operator, value = row
			elif len(row) == 2:
				fieldname, value = row
				operator = "="
			else:
				continue
			if operator in ("=", "==", "Equals", "equals") or operator is None:
				out[fieldname] = value
			elif fieldname not in out:
				# Keep first equality-like mapping; complex ops unused by callers today.
				out[fieldname] = value
		return out
	return frappe._dict()


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def facility_type_link_query(doctype, txt, searchfield, start, page_len, filters):
	txt = f"%{txt}%"
	return frappe.db.sql(
		"""
		SELECT name, facility_type_name
		FROM `tabFacility Type`
		WHERE disabled = 0
			AND (facility_type_name LIKE %(txt)s OR name LIKE %(txt)s)
		ORDER BY facility_type_name ASC
		LIMIT %(page_len)s OFFSET %(start)s
		""",
		{"txt": txt, "start": cint(start), "page_len": cint(page_len)},
	)


@frappe.whitelist()
@frappe.validate_and_sanitize_search_inputs
def facility_link_query(doctype, txt, searchfield, start, page_len, filters):
	"""Search Facility by ID, facility name / loan number, and bank.

	Eligibility:
	- ``status`` filter is applied when provided.
	- ``repayment_select`` (legacy virtual filter from Facility Repayment) means
	  ``status = 'Active'`` — repayments are only allowed against Active Facilities.
	"""
	filters = _normalize_link_filters(filters)
	txt = f"%{(txt or '').strip()}%"
	conditions = [
		"(name LIKE %(txt)s OR facility_name LIKE %(txt)s OR bank LIKE %(txt)s OR IFNULL(remarks, '') LIKE %(txt)s)"
	]
	params = {"txt": txt, "start": cint(start), "page_len": cint(page_len)}

	if filters.get("company"):
		conditions.append("company = %(company)s")
		params["company"] = filters.company

	status = filters.get("status")
	if status:
		conditions.append("status = %(status)s")
		params["status"] = status
	elif cint(filters.get("repayment_select")):
		# Preserve historical Facility Repayment set_query contract.
		conditions.append("status = 'Active'")

	match_conditions = build_match_conditions("Facility")
	if match_conditions:
		conditions.append(match_conditions)

	# Columns shown in Link dropdown: ID, Facility Name, Bank, Status
	return frappe.db.sql(
		f"""
		SELECT name, facility_name, bank, status
		FROM `tabFacility`
		WHERE {" AND ".join(conditions)}
		ORDER BY
			CASE WHEN name LIKE %(txt)s THEN 0 ELSE 1 END,
			CASE WHEN facility_name LIKE %(txt)s THEN 0 ELSE 1 END,
			facility_name ASC,
			name ASC
		LIMIT %(page_len)s OFFSET %(start)s
		""",
		params,
	)


def facility_names_matching(
	company: str, facility_name: str, *, exact_facility: str | None = None
) -> list[str]:
	"""Partial match on Facility.facility_name within company."""
	if exact_facility:
		return [exact_facility]
	if not (facility_name or "").strip():
		return []
	return frappe.get_all(
		"Facility",
		filters={"company": company, "facility_name": ["like", f"%{facility_name.strip()}%"]},
		pluck="name",
		order_by="name asc",
	)
