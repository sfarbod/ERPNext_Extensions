# Copyright (c) 2026, ERPNext Extensions contributors
"""Future-prevention helper for automatic production Stock Entries.

HISTORICAL REPAIR (optimizer / repair.py):
    Minimum timestamp edits only if the current (posting_datetime, creation)
    sequence creates a temporary negative. If opening stock already keeps
    running qty >= 0, leave times unchanged.

FUTURE PREVENTION (this module):
    A proven prerequisite → dependent pair still gets explicit chronology
    (T < dependent time) even if current opening stock would cover the
    issue. Economic dependency must remain deterministic for automatic
    production documents. Do not skip the bump merely because timestamps
    match and stock happens to be sufficient.

Contract::

    ensure_dependent_stock_posting_after(prerequisite, dependent, minimum_seconds=1)

Dependent posting datetime must be strictly greater than the prerequisite.
Does not globally monkey-patch Stock Entry timestamps. Only proven production
dependencies (Job Card / Work Order / against Stock Entry) are adjusted.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint

from erpnext_extensions.iran_accounting.stock_posting_order import (
	DEPENDENT_PURPOSES,
	MINIMUM_DEPENDENT_SECONDS,
	PREREQUISITE_PURPOSES,
)
from erpnext_extensions.iran_accounting.stock_posting_order.ordering import (
	add_seconds,
	combine_posting,
	crosses_posting_date,
	format_date,
	format_time,
)

PREVENTION_FLAG = "skip_production_posting_order"


def _g(doc, key, default=None):
	if doc is None:
		return default
	if isinstance(doc, str):
		return default
	if isinstance(doc, dict):
		return doc.get(key, default)
	return doc.get(key, default) if hasattr(doc, "get") else getattr(doc, key, default)


def _as_doc(value, doctype="Stock Entry"):
	if value is None:
		return None
	if hasattr(value, "doctype"):
		return value
	if isinstance(value, str):
		if not frappe.db.exists(doctype, value):
			return None
		return frappe.get_doc(doctype, value)
	return value


def lock_production_chain(*, job_card=None, work_order=None, prerequisite_se=None) -> None:
	"""Row-level locks only. No table locks."""
	if prerequisite_se:
		name = prerequisite_se if isinstance(prerequisite_se, str) else prerequisite_se.name
		frappe.db.sql("select name from `tabStock Entry` where name=%s for update", name)
	if job_card:
		name = job_card if isinstance(job_card, str) else job_card.name
		frappe.db.sql("select name from `tabJob Card` where name=%s for update", name)
	if work_order:
		name = work_order if isinstance(work_order, str) else work_order.name
		frappe.db.sql("select name from `tabWork Order` where name=%s for update", name)


def ensure_dependent_stock_posting_after(
	prerequisite,
	dependent,
	minimum_seconds: int = MINIMUM_DEPENDENT_SECONDS,
	*,
	lock: bool = True,
) -> dict:
	"""Bump ``dependent`` posting datetime so it is after ``prerequisite``.

	Returns a result dict. No-op when already ordered or inputs are missing.
	Refuses to silently roll a posting date past midnight (caller must decide).
	"""
	prereq = _as_doc(prerequisite)
	dep = _as_doc(dependent)
	if not prereq or not dep:
		return {"changed": False, "reason": "missing_document"}

	if lock:
		lock_production_chain(
			job_card=_g(dep, "job_card") or _g(prereq, "job_card"),
			work_order=_g(dep, "work_order") or _g(prereq, "work_order"),
			prerequisite_se=prereq.name,
		)
		prereq.reload()

	pre_dt = combine_posting(prereq.posting_date, prereq.posting_time)
	dep_dt = combine_posting(dep.posting_date, dep.posting_time)
	need = add_seconds(pre_dt, minimum_seconds)
	if dep_dt > pre_dt:
		return {
			"changed": False,
			"reason": "already_after",
			"old": str(dep_dt),
			"new": str(dep_dt),
			"prerequisite": prereq.name,
			"dependent": dep.name,
		}

	occupied = _occupied_same_stock_seconds(prereq, dep, need)
	candidate = need
	while candidate in occupied:
		candidate = add_seconds(candidate, minimum_seconds)

	if crosses_posting_date(dep_dt, candidate):
		return {
			"changed": False,
			"reason": "midnight",
			"status": "MANUAL_REVIEW_MIDNIGHT",
			"old": str(dep_dt),
			"new": str(candidate),
			"prerequisite": prereq.name,
			"dependent": dep.name,
		}

	dep.posting_date = format_date(candidate)
	dep.posting_time = format_time(candidate)
	dep.set_posting_time = 1
	return {
		"changed": True,
		"reason": "ensure_after",
		"old": str(dep_dt),
		"new": str(candidate),
		"prerequisite": prereq.name,
		"dependent": dep.name,
	}


def _item_warehouse_keys(doc) -> set[tuple[str, str]]:
	keys = set()
	for row in doc.get("items") or []:
		item = row.get("item_code")
		for wh in (row.get("s_warehouse"), row.get("t_warehouse")):
			if item and wh:
				keys.add((item, wh))
	return keys


def _occupied_same_stock_seconds(prereq, dep, at_dt) -> set:
	"""Datetimes already used by other submitted SEs sharing item+warehouse."""
	keys = _item_warehouse_keys(prereq) & _item_warehouse_keys(dep)
	if not keys:
		keys = _item_warehouse_keys(dep)
	if not keys:
		return set()
	items = list({k[0] for k in keys})
	warehouses = list({k[1] for k in keys})
	exclude = [prereq.name]
	if getattr(dep, "name", None):
		exclude.append(dep.name)
	rows = frappe.db.sql(
		"""
		SELECT se.name, se.posting_date, se.posting_time
		FROM `tabStock Entry` se
		INNER JOIN `tabStock Entry Detail` d ON d.parent = se.name
		WHERE se.docstatus = 1
		  AND se.name NOT IN %s
		  AND d.item_code IN %s
		  AND (d.s_warehouse IN %s OR d.t_warehouse IN %s)
		""",
		(exclude, items, warehouses, warehouses),
		as_dict=True,
	)
	taken = set()
	for r in rows:
		taken.add(combine_posting(r.posting_date, r.posting_time))
	return taken


def find_prerequisite_stock_entry(doc) -> str | None:
	"""Proven prerequisite for an automatic production dependent."""
	if not doc or _g(doc, "doctype") != "Stock Entry":
		return None
	purpose = _g(doc, "purpose")
	if purpose not in DEPENDENT_PURPOSES:
		return None
	job_card = _g(doc, "job_card")
	work_order = _g(doc, "work_order")

	consumed = []
	against = []
	for row in doc.get("items") or []:
		if row.get("s_warehouse"):
			consumed.append((row.item_code, row.s_warehouse, (row.get("batch_no") or "").strip()))
		if row.get("against_stock_entry"):
			against.append(row.against_stock_entry)
	if against:
		src = against[0]
		if cint(frappe.db.get_value("Stock Entry", src, "docstatus")) == 1:
			return src
	if not job_card and not work_order:
		return None

	filters = {"docstatus": 1, "purpose": ["in", list(PREREQUISITE_PURPOSES)]}
	if job_card:
		filters["job_card"] = job_card
	elif work_order:
		filters["work_order"] = work_order
	candidates = frappe.get_all(
		"Stock Entry",
		filters=filters,
		fields=["name", "purpose", "posting_date", "posting_time", "creation", "job_card", "work_order"],
		order_by="posting_date desc, posting_time desc, creation desc",
		limit=30,
	)
	own = _g(doc, "name")
	for cand in candidates:
		if cand.name == own:
			continue
		if cand.purpose not in PREREQUISITE_PURPOSES:
			continue
		# structural: same JC, or MTfM of same WO feeding this Manufacture
		if job_card and cand.job_card == job_card:
			if _candidate_supplies(cand.name, consumed):
				return cand.name
			if cand.purpose == "Material Transfer for Manufacture":
				return cand.name
		if work_order and cand.work_order == work_order and cand.purpose == "Material Transfer for Manufacture":
			if purpose in ("Manufacture", "Material Consumption for Manufacture"):
				if _candidate_supplies(cand.name, consumed):
					return cand.name
		if work_order and cand.work_order == work_order and cand.purpose == "Manufacture":
			if _candidate_supplies(cand.name, consumed):
				return cand.name
	return None


def _candidate_supplies(se_name: str, consumed: list[tuple]) -> bool:
	if not consumed:
		return False
	rows = frappe.db.sql(
		"""
		SELECT item_code, t_warehouse, batch_no
		FROM `tabStock Entry Detail`
		WHERE parent=%s AND IFNULL(t_warehouse,'') != ''
		""",
		se_name,
		as_dict=True,
	)
	supplied = {(r.item_code, r.t_warehouse, (r.batch_no or "").strip()) for r in rows}
	supplied_no_batch = {(r.item_code, r.t_warehouse) for r in rows}
	for item, wh, batch in consumed:
		if batch and (item, wh, batch) in supplied:
			return True
		if (item, wh) in supplied_no_batch:
			return True
	return False


def apply_production_posting_order(doc, method=None) -> dict | None:
	"""Stock Entry hook. No-op for non-production / skipped / already ordered."""
	if getattr(frappe.flags, PREVENTION_FLAG, False):
		return None
	if not doc or doc.doctype != "Stock Entry":
		return None
	if cint(doc.docstatus) == 2:
		return None
	if doc.purpose not in DEPENDENT_PURPOSES:
		return None
	has_against = any((row.get("against_stock_entry") or "") for row in doc.get("items") or [])
	if not (doc.job_card or doc.work_order or has_against):
		return None
	prereq_name = find_prerequisite_stock_entry(doc)
	if not prereq_name:
		return None
	return ensure_dependent_stock_posting_after(prereq_name, doc)


def before_validate_stock_entry(doc, method=None):
	apply_production_posting_order(doc, method)


def before_submit_stock_entry(doc, method=None):
	# Re-validate immediately before write; another worker may have taken T+1.
	apply_production_posting_order(doc, method)
