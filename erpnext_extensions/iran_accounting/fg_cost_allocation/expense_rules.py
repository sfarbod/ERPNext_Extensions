# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Department-level expense rule discovery and generation for accounts starting with 6."""

from __future__ import annotations

import json
import re

import frappe
from frappe import _
from frappe.utils import cint, getdate

ADMIN_STAFF_TOKENS = ("اداری", "ستادی", "ستاد")
# Previously approved / verified administrative Cost Center leaf for اسپاد فارمد دارو.
VERIFIED_ADMIN_COST_CENTERS = ("3000 - اداری و ستاد - E",)


def is_admin_staff_cost_center(cost_center: str | None) -> bool:
	"""Return True only for verified administrative/headquarters Cost Centers.

	Uses the previously approved leaf Cost Center identity first. Token matching is
	retained only as a secondary check against that same known classification family
	and must not invent new administrative centers from arbitrary names.
	"""
	if not cost_center:
		return False
	if cost_center in VERIFIED_ADMIN_COST_CENTERS:
		return True
	# Secondary: exact previously-known naming pattern for the verified leaf only.
	cc_name = frappe.db.get_value("Cost Center", cost_center, "cost_center_name") or ""
	if cost_center.startswith("3000 -") and "اداری و ستاد" in f"{cost_center} {cc_name}":
		return True
	return False


def admin_classification(cost_center: str | None) -> dict:
	"""Explain inclusion/exclusion for reporting (never silent)."""
	if not cost_center:
		return {"cost_center": cost_center, "exclude": False, "reason": "Blank Cost Center"}
	if is_admin_staff_cost_center(cost_center):
		return {
			"cost_center": cost_center,
			"exclude": True,
			"reason": "Verified administrative/headquarters Cost Center (اداری و ستاد)",
		}
	cc_name = frappe.db.get_value("Cost Center", cost_center, "cost_center_name") or ""
	blob = f"{cost_center} {cc_name}"
	if any(tok in blob for tok in ADMIN_STAFF_TOKENS):
		return {
			"cost_center": cost_center,
			"exclude": False,
			"review": True,
			"reason": "Name contains اداری/ستاد tokens but is not the verified headquarters leaf — listed for review, not auto-excluded",
		}
	return {"cost_center": cost_center, "exclude": False, "reason": "Not administrative"}


def list_expense_accounts_starting_with_6(company: str) -> list[str]:
	rows = frappe.db.sql(
		"""
		SELECT name FROM tabAccount
		WHERE company = %s
		  AND is_group = 0
		  AND IFNULL(account_number, '') LIKE '6%%'
		  AND root_type = 'Expense'
		ORDER BY account_number, name
		""",
		(company,),
	)
	return [r[0] for r in rows]


def discover_gl_dimension_combos(
	company: str,
	*,
	from_date=None,
	to_date=None,
	exclude_admin_staff: bool = True,
):
	"""Distinct Account×CC×Dept from live GL for accounts starting with 6.

	Returns (eligible_combos, excluded_admin_staff_combos).
	"""
	accounts = list_expense_accounts_starting_with_6(company)
	if not accounts:
		return [], []
	conditions = [
		"g.company = %(company)s",
		"g.account IN %(accounts)s",
		"IFNULL(g.is_cancelled, 0) = 0",
		"IFNULL(g.is_opening, 'No') != 'Yes'",
		"IFNULL(g.cost_center, '') != ''",
		"IFNULL(g.department, '') != ''",
	]
	params = {"company": company, "accounts": tuple(accounts)}
	if from_date:
		conditions.append("g.posting_date >= %(from_date)s")
		params["from_date"] = str(getdate(from_date))
	if to_date:
		conditions.append("g.posting_date <= %(to_date)s")
		params["to_date"] = str(getdate(to_date))

	rows = frappe.db.sql(
		f"""
		SELECT g.account, g.cost_center, g.department, COUNT(*) AS gle_count
		FROM `tabGL Entry` g
		WHERE {' AND '.join(conditions)}
		GROUP BY g.account, g.cost_center, g.department
		ORDER BY g.account, g.cost_center, g.department
		""",
		params,
		as_dict=True,
	)
	out = []
	excluded = []
	for r in rows:
		if exclude_admin_staff and is_admin_staff_cost_center(r.cost_center):
			excluded.append(r)
			continue
		out.append(r)
	return out, excluded


def _rule_key(account, cost_center, department) -> tuple[str, str, str]:
	return ((account or "").strip(), (cost_center or "").strip(), (department or "").strip())


@frappe.whitelist()
def preview_generate_department_rules(
	settings_name: str, from_date: str | None = None, to_date: str | None = None
):
	doc = frappe.get_doc("Finished Goods Cost Allocation Settings", settings_name)
	doc.check_permission("read")
	# Admin/Staff Cost Center exclusion setting removed — do not auto-exclude by CC class.
	combos, excluded = discover_gl_dimension_combos(
		doc.company, from_date=from_date, to_date=to_date, exclude_admin_staff=False
	)
	existing = {
		_rule_key(r.account, r.cost_center, r.department)
		for r in (doc.expense_account_dimension_rules or [])
	}
	to_create = []
	duplicates = 0
	for c in combos:
		key = _rule_key(c.account, c.cost_center, c.department)
		if key in existing:
			duplicates += 1
			continue
		to_create.append(
			{
				"account": c.account,
				"cost_center": c.cost_center,
				"department": c.department,
				"gle_count": c.gle_count,
				"enabled": 1,
			}
		)
	return {
		"accounts_with_prefix_6": len(list_expense_accounts_starting_with_6(doc.company)),
		"discovered": len(combos) + len(excluded),
		"to_create": len(to_create),
		"duplicates_skipped": duplicates,
		"excluded_admin_staff": len(excluded),
		"excluded_samples": excluded[:20],
		"preview_rows": to_create[:200],
		"preview_truncated": max(0, len(to_create) - 200),
	}


@frappe.whitelist()
def generate_department_rules(
	settings_name: str,
	from_date: str | None = None,
	to_date: str | None = None,
	replace_all: int = 0,
):
	"""Idempotent generation of Account×CC×Dept rules from GL discovery."""
	doc = frappe.get_doc("Finished Goods Cost Allocation Settings", settings_name)
	doc.check_permission("write")
	# Admin/Staff Cost Center exclusion setting removed — do not auto-exclude by CC class.
	combos, excluded = discover_gl_dimension_combos(
		doc.company, from_date=from_date, to_date=to_date, exclude_admin_staff=False
	)

	created = skipped = 0
	if cint(replace_all):
		doc.set("expense_account_dimension_rules", [])

	existing = {
		_rule_key(r.account, r.cost_center, r.department)
		for r in (doc.expense_account_dimension_rules or [])
	}
	for c in combos:
		key = _rule_key(c.account, c.cost_center, c.department)
		if key in existing:
			skipped += 1
			continue
		doc.append(
			"expense_account_dimension_rules",
			{
				"account": c.account,
				"cost_center": c.cost_center,
				"department": c.department,
				"enabled": 1,
				"description": f"Auto-generated from GL (n={c.gle_count})",
			},
		)
		existing.add(key)
		created += 1

	summary = {
		"created": created,
		"duplicates_skipped": skipped,
		"excluded_admin_staff": len(excluded),
		"replace_all": bool(cint(replace_all)),
		"from_date": from_date,
		"to_date": to_date,
		"accounts_with_prefix_6": len(list_expense_accounts_starting_with_6(doc.company)),
	}
	doc.last_rule_generation_summary = json.dumps(summary, ensure_ascii=False, indent=2)
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return summary


@frappe.whitelist()
def remove_blank_cc_seed_rules(settings_name: str, confirm: int = 0):
	"""Remove the three known incorrect blank-CC seed rules if still present."""
	doc = frappe.get_doc("Finished Goods Cost Allocation Settings", settings_name)
	doc.check_permission("write")
	targets = [
		("611028 - حقوق پایه - E", "", "واحد فرمولاسیون و ساخت بیولوژیک - E"),
		("611028 - حقوق پایه - E", "", "واحد فرمولاسیون و ساخت غیربیولوژیک - E"),
		("611106 - هزینه آموزش کارکنان - E", "", "واحد بسته بندی - E"),
	]
	found = []
	for r in list(doc.expense_account_dimension_rules or []):
		key = _rule_key(r.account, r.cost_center, r.department)
		if key in targets:
			found.append(
				{
					"idx": r.idx,
					"name": r.name,
					"account": r.account,
					"cost_center": r.cost_center,
					"department": r.department,
				}
			)
	if len(found) != 3 and not found:
		return {"removed": 0, "found": found, "message": "No matching seed rows present"}
	if len(found) != 3:
		return {
			"removed": 0,
			"found": found,
			"message": "Ambiguous: expected exactly 3 matching blank-CC seed rows",
			"requires_confirmation": True,
		}
	if not cint(confirm):
		return {"removed": 0, "found": found, "preview_only": True}
	# delete matching rows
	keep = []
	removed = []
	target_set = set(targets)
	for r in doc.expense_account_dimension_rules or []:
		key = _rule_key(r.account, r.cost_center, r.department)
		if key in target_set:
			removed.append(key)
		else:
			keep.append(r)
	doc.set("expense_account_dimension_rules", [])
	for r in keep:
		doc.append("expense_account_dimension_rules", r.as_dict())
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"removed": len(removed), "removed_rows": found}
