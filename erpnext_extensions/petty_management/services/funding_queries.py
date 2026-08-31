"""SQL aggregates for PM Request funding Payment Entries (architecture v2.1)."""

from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import cint, flt

_EPS = 1e-6


def pe_line_amount_sql(alias: str = "pe") -> str:
	return f"""
		case
			when ifnull({alias}.paid_amount, 0) > 0 then {alias}.paid_amount
			when ifnull({alias}.received_amount, 0) > 0 then {alias}.received_amount
			else 0
		end
	"""


def pm_request_pe_link_sql(pe_alias: str = "pe", pm_request_param: str = "%s") -> tuple[str, list]:
	"""WHERE fragment linking PE rows to a PM Request name."""
	meta = frappe.get_meta("Payment Entry")
	clauses = [f"{pe_alias}.reference_no = {pm_request_param}"]
	params: list = []
	if meta.has_field("custom_pm_request"):
		clauses.append(f"{pe_alias}.custom_pm_request = {pm_request_param}")
		params.append(pm_request_param)
	return f"({' OR '.join(clauses)})", params


def _pm_request_filter_values(pm_request: str) -> dict:
	return {"pm_request": pm_request}


def count_payment_entries_for_pm_request(pm_request: str) -> dict[str, int]:
	"""Counts aligned with Desk payment entry table (reference_no / custom_pm_request link)."""
	rows = list_payment_entries_for_pm_request(pm_request)
	submitted = sum(1 for r in rows if (r.get("status") or "") == "Submitted")
	draft = sum(1 for r in rows if (r.get("status") or "") == "Draft")
	cancelled = sum(1 for r in rows if (r.get("status") or "") == "Cancelled")
	payment_entry_count = len(rows)
	return {
		"submitted_payment_entry_count": submitted,
		"draft_payment_entry_count": draft,
		"cancelled_payment_entry_count": cancelled,
		"payment_entry_count": payment_entry_count,
	}


def payment_entry_list_filters_for_pm_request(pm_request: str) -> dict:
	"""Desk List/Payment Entry route filters for a PM Request."""
	meta = frappe.get_meta("Payment Entry")
	filters: dict = {"reference_no": pm_request}
	if meta.has_field("custom_pm_request"):
		filters["custom_pm_request"] = pm_request
	return filters


def sum_submitted_pe_amount(pm_request: str) -> float:
	req = frappe.db.get_value(
		"PM Request",
		pm_request,
		["company", "employee"],
		as_dict=True,
	)
	if not req:
		return 0.0
	meta = frappe.get_meta("Payment Entry")
	link_parts = ["pe.reference_no = %(pm_request)s"]
	if meta.has_field("custom_pm_request"):
		link_parts.append("pe.custom_pm_request = %(pm_request)s")
	link_sql = " OR ".join(link_parts)
	amt = pe_line_amount_sql("pe")
	return flt(
		frappe.db.sql(
			f"""
			select coalesce(sum({amt}), 0)
			from `tabPayment Entry` pe
			where pe.docstatus = 1
				and pe.payment_type = 'Pay'
				and pe.company = %(company)s
				and pe.party_type = 'Employee'
				and pe.party = %(employee)s
				and ({link_sql})
			""",
			{
				"pm_request": pm_request,
				"company": req.company,
				"employee": req.employee,
			},
		)[0][0]
	)


def sum_draft_pe_amount(pm_request: str, exclude_pe: str | None = None) -> float:
	req = frappe.db.get_value(
		"PM Request",
		pm_request,
		["company", "employee"],
		as_dict=True,
	)
	if not req:
		return 0.0
	meta = frappe.get_meta("Payment Entry")
	link_parts = ["pe.reference_no = %(pm_request)s"]
	if meta.has_field("custom_pm_request"):
		link_parts.append("pe.custom_pm_request = %(pm_request)s")
	link_sql = " OR ".join(link_parts)
	amt = pe_line_amount_sql("pe")
	exclude_sql = " and pe.name != %(exclude_pe)s " if exclude_pe else ""
	params = {
		"pm_request": pm_request,
		"company": req.company,
		"employee": req.employee,
	}
	if exclude_pe:
		params["exclude_pe"] = exclude_pe
	return flt(
		frappe.db.sql(
			f"""
			select coalesce(sum({amt}), 0)
			from `tabPayment Entry` pe
			where pe.docstatus = 0
				and pe.payment_type = 'Pay'
				and pe.company = %(company)s
				and pe.party_type = 'Employee'
				and pe.party = %(employee)s
				and ({link_sql})
				{exclude_sql}
			""",
			params,
		)[0][0]
	)


def count_linked_payment_entries(pm_request: str, *, docstatus: tuple[int, ...] | None = None) -> int:
	meta = frappe.get_meta("Payment Entry")
	link_parts = ["pe.reference_no = %(pm_request)s"]
	if meta.has_field("custom_pm_request"):
		link_parts.append("pe.custom_pm_request = %(pm_request)s")
	link_sql = " OR ".join(link_parts)
	ds_sql = ""
	params: dict = {"pm_request": pm_request}
	if docstatus is not None:
		ds_sql = " and pe.docstatus in %(docstatus)s "
		params["docstatus"] = docstatus
	return cint(
		frappe.db.sql(
			f"""
			select count(*)
			from `tabPayment Entry` pe
			where ({link_sql})
				{ds_sql}
			""",
			params,
		)[0][0]
	)


def has_draft_payment_entry(pm_request: str) -> bool:
	return count_linked_payment_entries(pm_request, docstatus=(0,)) > 0


def find_pm_requests_for_payment_entry(pe_name: str) -> list[str]:
	fields = ["reference_no"]
	meta = frappe.get_meta("Payment Entry")
	if meta.has_field("custom_pm_request"):
		fields.append("custom_pm_request")
	pe = frappe.db.get_value("Payment Entry", pe_name, fields, as_dict=True)
	if not pe:
		return []
	names: set[str] = set()
	ref = (pe.reference_no or "").strip()
	if ref and frappe.db.exists("PM Request", ref):
		names.add(ref)
	custom = (pe.get("custom_pm_request") or "").strip()
	if custom and frappe.db.exists("PM Request", custom):
		names.add(custom)
	linked = frappe.get_all("PM Request", filters={"payment_entry": pe_name}, pluck="name")
	for n in linked:
		names.add(n)
	return sorted(names)


def find_pm_requests_for_clearance(clearance: str | Document) -> list[str]:
	"""PM Requests linked via non-legacy Clearance allocation rows."""
	if isinstance(clearance, Document):
		names = {
			(row.pm_request or "").strip()
			for row in clearance.get("request_allocations") or []
			if not cint(row.is_legacy_row) and (row.pm_request or "").strip()
		}
		return sorted(n for n in names if frappe.db.exists("PM Request", n))

	return frappe.db.sql(
		"""
		SELECT DISTINCT a.pm_request
		FROM `tabPM Clearance Request Allocation` a
		WHERE a.parent = %s
			AND a.parenttype = 'PM Clearance'
			AND a.parentfield = 'request_allocations'
			AND IFNULL(a.is_legacy_row, 0) = 0
			AND IFNULL(a.pm_request, '') != ''
		ORDER BY a.pm_request
		""",
		(clearance,),
		pluck=True,
	) or []


def find_pm_requests_for_journal_entry(je_name: str) -> list[str]:
	"""PM Requests tied to a Journal Entry directly or via linked Clearance."""
	names: set[str] = set()
	for req in frappe.get_all("PM Request", filters={"journal_entry": je_name}, pluck="name"):
		names.add(req)
	for clearance in frappe.get_all("PM Clearance", filters={"journal_entry": je_name}, pluck="name"):
		for req in find_pm_requests_for_clearance(clearance):
			names.add(req)
	return sorted(names)


def list_payment_entries_for_pm_request(pm_request: str) -> list[dict]:
	"""All funding PE rows linked to this PM Request (read-only list for Desk)."""
	meta = frappe.get_meta("Payment Entry")
	link_parts = ["pe.reference_no = %(pm_request)s"]
	if meta.has_field("custom_pm_request"):
		link_parts.append("pe.custom_pm_request = %(pm_request)s")
	link_sql = " OR ".join(link_parts)
	amt = pe_line_amount_sql("pe")
	rows = frappe.db.sql(
		f"""
		select
			pe.name as payment_entry,
			pe.posting_date,
			pe.docstatus,
			{amt} as amount
		from `tabPayment Entry` pe
		where ({link_sql})
		order by pe.modified desc, pe.creation desc
		""",
		{"pm_request": pm_request},
		as_dict=True,
	)
	out: list[dict] = []
	for row in rows:
		ds = cint(row.docstatus)
		status = {0: "Draft", 1: "Submitted", 2: "Cancelled"}.get(ds, "")
		out.append(
			{
				"payment_entry": row.payment_entry,
				"amount": flt(row.amount),
				"status": status,
				"posting_date": row.posting_date,
			}
		)
	return out


def resolve_latest_payment_entry(pm_request: str, exclude_pe: str | None = None) -> str | None:
	meta = frappe.get_meta("Payment Entry")
	link_parts = ["pe.reference_no = %(pm_request)s"]
	if meta.has_field("custom_pm_request"):
		link_parts.append("pe.custom_pm_request = %(pm_request)s")
	link_sql = " OR ".join(link_parts)
	exclude_sql = " and pe.name != %(exclude_pe)s " if exclude_pe else ""
	params: dict = {"pm_request": pm_request}
	if exclude_pe:
		params["exclude_pe"] = exclude_pe
	row = frappe.db.sql(
		f"""
		select pe.name
		from `tabPayment Entry` pe
		where ({link_sql})
			and pe.docstatus in (0, 1)
			{exclude_sql}
		order by pe.docstatus desc, pe.modified desc, pe.creation desc
		limit 1
		""",
		params,
	)
	if row:
		return row[0][0]
	scalar = frappe.db.get_value("PM Request", pm_request, "payment_entry")
	if exclude_pe and scalar == exclude_pe:
		return None
	return scalar or None
