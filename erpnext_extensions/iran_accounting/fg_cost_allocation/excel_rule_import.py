# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Rebuild Expense Account Dimension Rules from authoritative Excel sources.

Sources (Settings Attach / File / absolute path):
  - معین حساب - قیمت تمام شده.xlsx  → Account Code (+ optional Account Name)
  - دپارتمان - قیمت تمام شده.xlsx → Department list (column «مرکز»)

Authoritative mapping (corrected):
  - Account from accounts Excel
  - Department from Department Excel column «مرکز» (Department names, NOT Cost Centers)
  - Cost Center = blank on every generated rule → unrestricted Cost Center filter
    (optional dimension: blank means do not filter; NOT exact-empty GL matching)

Verified baseline (must match after parse):
  - 89 unique account codes (70×611, 17×621, 2×622)
  - 35 Department Excel rows (34 Departments + 1 excluded blank-dept row مشترک عمومی شرکت)
  - Expected combinations: 89 × 34 = 3026 Account × Department (blank Cost Center)
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

ACCOUNT_FILE_HINTS = ("معین حساب", "حساب", "account")
DEPT_FILE_HINTS = ("دپارتمان", "department", "مرکز")
BLANK_DEPT_LABEL = "مشترک عمومی شرکت"
EXPECTED_ACCOUNTS = 89
EXPECTED_MAPPINGS = 35
EXPECTED_NONEMPTY_DEPT = 34


def _open_workbook(path: str):
	import openpyxl

	return openpyxl.load_workbook(path, data_only=True, read_only=True)


def _resolve_file_path(value: str | None) -> str | None:
	"""Resolve Attach / File URL / absolute path to a local filesystem path."""
	if not value:
		return None
	value = value.strip()
	if os.path.isabs(value) and os.path.isfile(value):
		return value
	# File doctype name or URL
	if frappe.db.exists("File", value):
		return frappe.get_doc("File", value).get_full_path()
	# /private/files/... or /files/...
	site_path = frappe.get_site_path()
	basename = os.path.basename(value)
	candidates = [
		value,
		os.path.join(site_path, value.lstrip("/")),
		os.path.join(site_path, "private", "files", basename),
		os.path.join(site_path, "private", basename),
		os.path.join(site_path, "public", "files", basename),
	]
	# Attach fields store like /private/files/foo.xlsx
	if value.startswith("/private/files/") or value.startswith("/files/") or value.startswith("/private/"):
		candidates.insert(0, frappe.get_site_path(value.lstrip("/")))
	# File by file_url
	fname = frappe.db.get_value("File", {"file_url": value}, "name")
	if fname:
		return frappe.get_doc("File", fname).get_full_path()
	for c in candidates:
		if c and os.path.isfile(c):
			return c
	return None


def find_default_excel_paths() -> dict:
	"""Search common locations for the two authoritative Excel filenames."""
	site_private_files = frappe.get_site_path("private", "files")
	site_private_root = frappe.get_site_path("private")
	site_public = frappe.get_site_path("public", "files")
	fixture_dir = frappe.get_app_path(
		"erpnext_extensions", "iran_accounting", "fg_cost_allocation", "fixtures", "excel"
	)
	roots = [
		fixture_dir,
		site_private_root,
		site_private_files,
		site_public,
		"/workspace/development/frappe-bench/apps/erpnext_extensions/fixtures",
		"/tmp",
	]
	account_path = dept_path = None
	for root in roots:
		if not root or not os.path.isdir(root):
			continue
		try:
			names = os.listdir(root)
		except OSError:
			continue
		for name in names:
			low = name.lower()
			full = os.path.join(root, name)
			if not os.path.isfile(full):
				continue
			if not (low.endswith(".xlsx") or low.endswith(".xls")):
				continue
			if account_path is None and ("معین" in name or "حساب" in name) and "دپارتمان" not in name:
				account_path = full
			if dept_path is None and "دپارتمان" in name:
				dept_path = full
	return {"account_excel": account_path, "department_excel": dept_path, "searched_roots": roots}


def _sheet_rows(path: str) -> list[list]:
	wb = _open_workbook(path)
	ws = wb.active
	rows = []
	for row in ws.iter_rows(values_only=True):
		rows.append([("" if v is None else v) for v in row])
	wb.close()
	return rows


def _norm_header(v) -> str:
	return re.sub(r"\s+", " ", str(v or "").strip().lower())


def parse_accounts_excel(path: str) -> dict:
	rows = _sheet_rows(path)
	if not rows:
		frappe.throw(_("Accounts Excel is empty: {0}").format(path))
	header = [_norm_header(c) for c in rows[0]]
	code_idx = name_idx = None
	for i, h in enumerate(header):
		if code_idx is None and any(k in h for k in ("کد", "code", "account number", "شماره", "معین")):
			code_idx = i
		if name_idx is None and any(k in h for k in ("نام", "name", "عنوان")):
			name_idx = i
	# fallback: first column = code, second = name
	if code_idx is None:
		code_idx = 0
	if name_idx is None and len(header) > 1:
		name_idx = 1

	accounts = []
	seen = set()
	dupes = []
	for raw in rows[1:]:
		if code_idx >= len(raw):
			continue
		code = str(raw[code_idx]).strip()
		if not code or code.lower() in ("none", "nan"):
			continue
		# strip .0 from excel numbers
		if re.fullmatch(r"\d+\.0", code):
			code = code[:-2]
		# keep digits-only account codes when cell is numeric-like
		code_key = re.sub(r"[^\d].*", "", code) if re.match(r"^\d", code) else code
		name = str(raw[name_idx]).strip() if name_idx is not None and name_idx < len(raw) else ""
		if code_key in seen:
			dupes.append(code_key)
			continue
		seen.add(code_key)
		accounts.append({"account_code": code_key, "account_name_excel": name, "raw": code})

	pref = {"611": 0, "621": 0, "622": 0, "other": 0}
	for a in accounts:
		p = a["account_code"][:3]
		if p in pref:
			pref[p] += 1
		else:
			pref["other"] += 1

	return {
		"path": path,
		"accounts": accounts,
		"unique_count": len(accounts),
		"duplicates": dupes,
		"prefix_counts": pref,
		"baseline_ok": len(accounts) == EXPECTED_ACCOUNTS
		and pref.get("611") == 70
		and pref.get("621") == 17
		and pref.get("622") == 2,
	}


def parse_department_excel(path: str) -> dict:
	"""Parse Department Excel.

	Column «مرکز» holds Department names (not Cost Centers).
	Optional column «دپارتمان» is parent/group context only.
	Row «مشترک عمومی شرکت» is excluded pending approval.
	"""
	rows = _sheet_rows(path)
	if not rows:
		frappe.throw(_("Department Excel is empty: {0}").format(path))
	header = [_norm_header(c) for c in rows[0]]
	dept_idx = parent_idx = None
	for i, h in enumerate(header):
		if dept_idx is None and "مرکز" in h:
			dept_idx = i
		if parent_idx is None and ("دپارتمان" in h or h in ("department", "parent department")) and "مرکز" not in h:
			parent_idx = i
	# Authoritative layout: column0 = Department («مرکز»), column1 = parent (optional)
	if dept_idx is None:
		dept_idx = 0
	if parent_idx is None and len(header) > 1:
		parent_idx = 1 if dept_idx != 1 else (0 if dept_idx != 0 else None)
	if parent_idx == dept_idx:
		parent_idx = 1 if dept_idx == 0 and len(header) > 1 else None

	mappings = []
	blank_dept_rows = []
	for raw in rows[1:]:
		if dept_idx >= len(raw):
			continue
		dept = str(raw[dept_idx]).strip()
		parent = ""
		if parent_idx is not None and parent_idx < len(raw):
			parent = str(raw[parent_idx]).strip()
		if not dept or dept.lower() in ("none", "nan"):
			continue
		if parent.lower() in ("none", "nan"):
			parent = ""
		row = {
			"department_excel": dept,
			"parent_department_excel": parent,
			"cost_center_excel": "",
		}
		# Pending-approval exclusion: documented blank-Department / shared-company row
		if dept == BLANK_DEPT_LABEL or dept.startswith(f"{BLANK_DEPT_LABEL} -"):
			blank_dept_rows.append(row)
		mappings.append(row)

	blank_ids = {id(r) for r in blank_dept_rows}
	eligible = [m for m in mappings if id(m) not in blank_ids]
	return {
		"path": path,
		"mappings": mappings,
		"eligible_departments": eligible,
		"mapping_count": len(mappings),
		"nonblank_department_count": len(eligible),
		"blank_department_rows": blank_dept_rows,
		"baseline_ok": len(mappings) == EXPECTED_MAPPINGS and len(eligible) == EXPECTED_NONEMPTY_DEPT,
	}


def _match_account(company: str, account_code: str) -> dict:
	"""Resolve Excel account code to ERPNext Account for company."""
	# Prefer exact account_number match
	rows = frappe.db.sql(
		"""
		SELECT name, account_name, account_number, disabled, is_group, root_type, company
		FROM tabAccount
		WHERE company=%s AND account_number=%s
		""",
		(company, account_code),
		as_dict=True,
	)
	if not rows:
		# name starts with code
		rows = frappe.db.sql(
			"""
			SELECT name, account_name, account_number, disabled, is_group, root_type, company
			FROM tabAccount
			WHERE company=%s AND name LIKE %s
			ORDER BY name
			LIMIT 5
			""",
			(company, f"{account_code} - %"),
			as_dict=True,
		)
	if not rows:
		return {"ok": False, "reason": "Account not found in company", "account_code": account_code}
	if len(rows) > 1:
		# prefer leaf expense
		leaf = [r for r in rows if not cint(r.is_group)]
		rows = leaf or rows
	acc = rows[0]
	if cint(acc.is_group):
		return {"ok": False, "reason": "Account is a group", "account": acc.name, "account_code": account_code}
	if cint(acc.disabled):
		return {"ok": False, "reason": "Account disabled", "account": acc.name, "account_code": account_code}
	if (acc.root_type or "") != "Expense":
		return {
			"ok": False,
			"reason": f"Account root_type={acc.root_type} (expected Expense)",
			"account": acc.name,
			"account_code": account_code,
		}
	return {
		"ok": True,
		"account": acc.name,
		"account_name": acc.account_name,
		"account_number": acc.account_number,
		"account_code": account_code,
	}


def _match_link(doctype: str, excel_value: str, company: str | None = None) -> dict:
	"""Exact name match first, then startswith / contains. Never invent."""
	if not excel_value:
		return {"ok": False, "reason": "Blank value", "excel": excel_value}
	if frappe.db.exists(doctype, excel_value):
		return {"ok": True, "name": excel_value, "excel": excel_value}
	# Append company suffix variants
	candidates = [excel_value]
	if company and not excel_value.endswith(" - E"):
		# try with company abbr
		abbr = frappe.db.get_value("Company", company, "abbr") or "E"
		candidates.append(f"{excel_value} - {abbr}")
	filters = {}
	if company and frappe.get_meta(doctype).has_field("company"):
		filters["company"] = company
	for cand in candidates:
		if frappe.db.exists(doctype, cand):
			return {"ok": True, "name": cand, "excel": excel_value}
	# LIKE search
	like = f"%{excel_value}%"
	q_filters = dict(filters)
	names = frappe.get_all(doctype, filters=q_filters, or_filters={"name": ["like", like]}, pluck="name", limit=5)
	if len(names) == 1:
		return {"ok": True, "name": names[0], "excel": excel_value, "matched_by": "like"}
	if not names:
		return {"ok": False, "reason": f"{doctype} not found", "excel": excel_value}
	return {"ok": False, "reason": f"Ambiguous {doctype} match", "excel": excel_value, "candidates": names}


def build_rule_plan(settings_name: str, account_excel: str | None = None, department_excel: str | None = None) -> dict:
	doc = frappe.get_doc("Finished Goods Cost Allocation Settings", settings_name)
	doc.check_permission("read")
	company = doc.company

	found = find_default_excel_paths()
	# Prefer explicit paths; otherwise discover from site private/ (no Settings Attach fields).
	account_path = _resolve_file_path(account_excel or found.get("account_excel"))
	dept_path = _resolve_file_path(department_excel or found.get("department_excel"))
	if not account_path or not dept_path:
		return {
			"ok": False,
			"error": "Excel source files not found",
			"account_path": account_path,
			"department_path": dept_path,
			"auto_search": found,
			"hint": "Attach معین حساب / دپارتمان Excel on Cost Allocation Settings, or place under fixtures/excel.",
		}

	acc_src = parse_accounts_excel(account_path)
	dept_src = parse_department_excel(dept_path)

	resolved_accounts = []
	failed_accounts = []
	for a in acc_src["accounts"]:
		m = _match_account(company, a["account_code"])
		m["account_name_excel"] = a.get("account_name_excel")
		(resolved_accounts if m.get("ok") else failed_accounts).append(m)

	resolved_maps = []
	failed_maps = []
	blank_dept_excluded = []
	seen_departments: dict[str, str] = {}
	duplicate_departments = []

	# Pending-approval blank Department row (مشترک عمومی شرکت) — no Cost Center resolution
	for m in dept_src.get("blank_department_rows") or []:
		blank_dept_excluded.append(
			{
				**m,
				"cost_center": "",
				"department": "",
				"reason": "Blank Department row excluded until explicit approval",
				"label_hint": BLANK_DEPT_LABEL,
			}
		)

	for m in dept_src.get("eligible_departments") or []:
		dept = _match_link("Department", m["department_excel"], company)
		if not dept.get("ok"):
			failed_maps.append(
				{
					"mapping": m,
					"cost_center": {"ok": True, "name": "", "excel": ""},
					"department": dept,
				}
			)
			continue
		dept_name = dept["name"]
		if dept_name in seen_departments:
			duplicate_departments.append(
				{
					"department": dept_name,
					"first_excel": seen_departments[dept_name],
					"duplicate_excel": m["department_excel"],
				}
			)
			continue
		seen_departments[dept_name] = m["department_excel"]
		resolved_maps.append(
			{
				# Blank Cost Center → unrestricted CC filter at extraction time
				"cost_center": "",
				"department": dept_name,
				"department_excel": m["department_excel"],
				"parent_department_excel": m.get("parent_department_excel") or "",
				"cost_center_excel": "",
			}
		)

	expected_combos = len(resolved_accounts) * len(resolved_maps)
	baseline_combos = EXPECTED_ACCOUNTS * EXPECTED_NONEMPTY_DEPT

	existing = {
		((r.account or "").strip(), (r.cost_center or "").strip(), (r.department or "").strip()): r
		for r in (doc.expense_account_dimension_rules or [])
	}

	to_create = to_update = to_reuse = 0
	detail_rows = []
	planned_keys = set()
	duplicate_combinations = 0
	for acc in resolved_accounts:
		for mp in resolved_maps:
			key = (acc["account"], "", mp["department"])
			if key in planned_keys:
				duplicate_combinations += 1
				continue
			planned_keys.add(key)
			if key in existing:
				to_reuse += 1
				detail_rows.append(
					{
						"account": acc["account"],
						"account_name": acc.get("account_name"),
						"account_code": acc.get("account_number") or acc.get("account_code"),
						"cost_center": "",
						"department": mp["department"],
						"action": "Reused",
						"reason": "Exact Account × blank Cost Center × Department already present",
						"generation_status": "OK",
					}
				)
			else:
				to_create += 1
				detail_rows.append(
					{
						"account": acc["account"],
						"account_name": acc.get("account_name"),
						"account_code": acc.get("account_number") or acc.get("account_code"),
						"cost_center": "",
						"department": mp["department"],
						"action": "Created",
						"reason": "New Account × Department rule (Cost Center blank)",
						"generation_status": "OK",
					}
				)

	for mp in blank_dept_excluded:
		detail_rows.append(
			{
				"account": None,
				"account_name": None,
				"account_code": None,
				"cost_center": "",
				"department": mp.get("department_excel") or "",
				"action": "Excluded",
				"reason": mp["reason"],
				"generation_status": "Excluded",
			}
		)

	for fa in failed_accounts:
		detail_rows.append(
			{
				"account": fa.get("account"),
				"account_name": None,
				"account_code": fa.get("account_code"),
				"cost_center": "",
				"department": None,
				"action": "Failed",
				"reason": fa.get("reason"),
				"generation_status": "Failed",
			}
		)

	for fm in failed_maps:
		detail_rows.append(
			{
				"account": None,
				"account_name": None,
				"account_code": None,
				"cost_center": "",
				"department": (fm.get("mapping") or {}).get("department_excel"),
				"action": "Failed",
				"reason": (fm.get("department") or {}).get("reason") or "Department not resolved",
				"generation_status": "Failed",
			}
		)

	obsolete = [k for k in existing.keys() if k not in planned_keys]
	manual_keys = [
		k
		for k, r in existing.items()
		if (getattr(r, "source", None) or "") == "Manual" or "manual" in (r.description or "").lower()
	]
	existing_blank_cc_rules = sum(1 for k in existing if k[1] == "")

	return {
		"ok": True,
		"company": company,
		"account_path": account_path,
		"department_path": dept_path,
		"mapping_mode": "Account × Department (Cost Center unrestricted / blank filter)",
		"cost_center_policy": "optional_unrestricted",
		"blank_department_row_treatment": {
			"excel_label": BLANK_DEPT_LABEL,
			"status": "excluded_pending_approval",
			"intended_meaning_if_approved": (
				"Account-only rules (blank Department + blank Cost Center) for each of the 89 accounts. "
				"That would match any Department and any Cost Center for those accounts and would "
				"overlap every Account×Department rule for the same Account — not generated automatically."
			),
			"do_not_silently_generate_account_only": True,
		},
		"account_source": {
			"unique_count": acc_src["unique_count"],
			"prefix_counts": acc_src["prefix_counts"],
			"baseline_ok": acc_src["baseline_ok"],
			"duplicates": acc_src["duplicates"],
		},
		"dimension_source": {
			"mapping_count": dept_src["mapping_count"],
			"department_count": dept_src["nonblank_department_count"],
			"nonblank_department_count": dept_src["nonblank_department_count"],
			"blank_department_rows": dept_src["blank_department_rows"],
			"baseline_ok": dept_src["baseline_ok"],
		},
		"resolved_account_count": len(resolved_accounts),
		"resolved_department_count": len(resolved_maps),
		"resolved_mapping_count": len(resolved_maps),
		"unresolved_departments": [
			{
				"excel": (fm.get("mapping") or {}).get("department_excel"),
				"reason": (fm.get("department") or {}).get("reason"),
				"candidates": (fm.get("department") or {}).get("candidates"),
			}
			for fm in failed_maps
		],
		"failed_accounts": failed_accounts,
		"failed_mappings": failed_maps,
		# No administrative Cost Center exclusions — Cost Center is blank for all Excel rules
		"admin_excluded_mappings": [],
		"admin_cc_exclusions_applied": False,
		"blank_department_excluded": blank_dept_excluded,
		"unverified_admin_review": [],
		"duplicate_departments": duplicate_departments,
		"duplicate_combinations": duplicate_combinations,
		"expected_combinations_baseline": baseline_combos,
		"expected_combinations_after_resolve": expected_combos,
		"to_create": to_create,
		"to_reuse": to_reuse,
		"to_update": to_update,
		"excluded_admin_combo_estimate": 0,
		"excluded_blank_dept": len(blank_dept_excluded),
		"failed": len(failed_accounts) + len(failed_maps),
		"obsolete_generated_candidates": len(obsolete),
		"manual_rules_preserved": len(manual_keys),
		"existing_rule_count": len(existing),
		"existing_blank_cost_center_rules": existing_blank_cc_rules,
		"detail_rows": detail_rows,
		"resolved_accounts": resolved_accounts,
		"resolved_mappings": resolved_maps,
		"resolved_departments": [m["department"] for m in resolved_maps],
	}


@frappe.whitelist()
def preview_excel_rule_generation(
	settings_name: str, account_excel: str | None = None, department_excel: str | None = None
):
	plan = build_rule_plan(settings_name, account_excel, department_excel)
	# trim detail for preview payload
	out = dict(plan)
	details = out.get("detail_rows") or []
	out["detail_rows_sample"] = details[:100]
	out["detail_rows_count"] = len(details)
	out.pop("detail_rows", None)
	out.pop("resolved_accounts", None)
	out.pop("resolved_mappings", None)
	# Keep resolved_departments list for preview reporting (names only)
	return out


@frappe.whitelist()
def generate_excel_rules(
	settings_name: str,
	account_excel: str | None = None,
	department_excel: str | None = None,
	replace_generated: int = 1,
	disable_obsolete_generated: int = 1,
	replace_all: int = 0,
):
	"""Idempotent rebuild of rules from Excel sources.

	By default preserves Manual source rows. When replace_all=1 (authorized full rebuild),
	all existing rules including Manual are removed before inserting Excel Generated rules.
	"""
	doc = frappe.get_doc("Finished Goods Cost Allocation Settings", settings_name)
	doc.check_permission("write")
	plan = build_rule_plan(settings_name, account_excel, department_excel)
	if not plan.get("ok"):
		return plan
	# Development safety: never wipe existing rules when Excel resolution produced zero departments.
	if not plan.get("resolved_mapping_count"):
		return {
			"ok": False,
			"error": "Rule generation blocked: zero resolvable Departments from Excel",
			"preview": {
				"resolved_account_count": plan.get("resolved_account_count"),
				"resolved_department_count": plan.get("resolved_department_count"),
				"resolved_mapping_count": plan.get("resolved_mapping_count"),
				"unresolved_departments": plan.get("unresolved_departments"),
				"failed_mappings": plan.get("failed_mappings"),
				"blank_department_excluded": plan.get("blank_department_excluded"),
				"account_path": plan.get("account_path"),
				"department_path": plan.get("department_path"),
			},
			"existing_rules_preserved": True,
		}

	batch_id = datetime.now().strftime("EXCEL-%Y%m%d-%H%M%S")
	ts = str(now_datetime())
	created = updated = reused = 0
	deleted_before = len(doc.expense_account_dimension_rules or [])

	# Index existing
	existing_by_key = {}
	manual_rows = []
	for r in list(doc.expense_account_dimension_rules or []):
		key = ((r.account or "").strip(), (r.cost_center or "").strip(), (r.department or "").strip())
		src = (getattr(r, "source", None) or "").strip() or (
			"Manual" if "manual" in (r.description or "").lower() else "Generated"
		)
		if src == "Manual" and not cint(replace_all):
			manual_rows.append(r)
			existing_by_key[key] = r
		else:
			existing_by_key[key] = r

	if cint(replace_all) or cint(replace_generated):
		# Full rebuild: clear generated (and Manual when replace_all).
		doc.set("expense_account_dimension_rules", [])
		if not cint(replace_all):
			for r in manual_rows:
				doc.append("expense_account_dimension_rules", r.as_dict())
		existing_by_key = {
			((r.account or "").strip(), (r.cost_center or "").strip(), (r.department or "").strip()): r
			for r in doc.expense_account_dimension_rules
		}

	planned_keys = set()
	for acc in plan["resolved_accounts"]:
		for mp in plan["resolved_mappings"]:
			# Cost Center always exact-blank for Excel Generated rules
			key = (acc["account"], "", mp["department"])
			planned_keys.add(key)
			if key in existing_by_key:
				row = existing_by_key[key]
				row.cost_center = ""
				if hasattr(row, "account_name"):
					row.account_name = acc.get("account_name")
				if hasattr(row, "source"):
					if (row.source or "") != "Manual":
						row.source = "Excel Generated"
						row.generation_batch = batch_id
				if hasattr(row, "company"):
					row.company = doc.company
				row.enabled = 1
				reused += 1
			else:
				payload = {
					"account": acc["account"],
					"account_name": acc.get("account_name"),
					"cost_center": "",
					"department": mp["department"],
					"company": doc.company,
					"enabled": 1,
					"source": "Excel Generated",
					"generation_batch": batch_id,
					"description": f"Excel generated {batch_id} (Cost Center blank)",
				}
				doc.append("expense_account_dimension_rules", payload)
				created += 1

	# Disable obsolete generated rules still present when not full replace
	obsolete_disabled = 0
	if cint(disable_obsolete_generated) and not cint(replace_generated):
		for r in doc.expense_account_dimension_rules or []:
			key = ((r.account or "").strip(), (r.cost_center or "").strip(), (r.department or "").strip())
			src = getattr(r, "source", None) or ""
			if key not in planned_keys and src != "Manual" and cint(r.enabled):
				r.enabled = 0
				obsolete_disabled += 1

	summary = {
		"ok": True,
		"generation_batch": batch_id,
		"generated_on": ts,
		"replace_all": bool(cint(replace_all)),
		"deleted_before_rebuild": deleted_before if (cint(replace_all) or cint(replace_generated)) else 0,
		"source_accounts": plan["account_source"]["unique_count"],
		"source_dimension_mappings": plan["dimension_source"]["mapping_count"],
		"expected_combinations": plan["expected_combinations_after_resolve"],
		"baseline_combinations": plan["expected_combinations_baseline"],
		"created": created,
		"updated": updated,
		"reused": reused,
		"excluded_admin_mappings": len(plan["admin_excluded_mappings"]),
		"excluded_admin_combo_estimate": plan["excluded_admin_combo_estimate"],
		"excluded_blank_dept": plan["excluded_blank_dept"],
		"failed": plan["failed"],
		"failed_accounts": plan["failed_accounts"],
		"failed_mappings": [
			{"excel": x["mapping"], "cc": x["cost_center"], "dept": x["department"]}
			for x in plan["failed_mappings"]
		],
		"admin_excluded_mappings": plan["admin_excluded_mappings"],
		"blank_department_excluded": plan["blank_department_excluded"],
		"account_path": plan["account_path"],
		"department_path": plan["department_path"],
		"account_baseline_ok": plan["account_source"]["baseline_ok"],
		"dimension_baseline_ok": plan["dimension_source"]["baseline_ok"],
		"prefix_counts": plan["account_source"]["prefix_counts"],
		"obsolete_disabled": obsolete_disabled,
		"active_rule_count_expected": plan["expected_combinations_after_resolve"] + len(manual_rows),
	}

	# Persist detail lines: full set to searchable DocType; compact sample on Settings grid
	detail_rows = plan.get("detail_rows") or []
	for r in detail_rows:
		r.setdefault("generation_date", ts)
	# Settings child grid stays responsive — keep excluded/failed + first Created sample.
	summary_sample = []
	created_sample_cap = 100
	created_kept = 0
	for r in detail_rows:
		action = (r.get("action") or "").strip()
		if action in ("Excluded", "Failed"):
			summary_sample.append(r)
		elif action in ("Created", "Reused", "Updated") and created_kept < created_sample_cap:
			summary_sample.append(r)
			created_kept += 1
	if hasattr(doc, "replace_rule_generation_summary_rows"):
		doc.replace_rule_generation_summary_rows(summary_sample, generation_date=ts)
	_persist_generation_lines(doc.name, batch_id, ts, detail_rows)
	summary["rule_generation_summary_rows_on_settings"] = len(summary_sample)
	summary["rule_generation_lines_persisted"] = len(detail_rows)

	doc.last_rule_generation_summary = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
	if hasattr(doc, "last_rule_generation_batch"):
		doc.last_rule_generation_batch = batch_id
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	active = frappe.db.count(
		"Expense Account Dimension Rule",
		{"parent": doc.name, "enabled": 1},
	)
	summary["active_rule_count_actual"] = active
	summary["reconcile_ok"] = active == summary["active_rule_count_expected"] or active == plan[
		"expected_combinations_after_resolve"
	]
	return summary


def _persist_generation_lines(settings_name: str, batch_id: str, ts: str, rows: list[dict]):
	"""Materialize generation detail into searchable DocType when available."""
	doctype = "MFG Cost Rule Generation Line"
	if not frappe.db.exists("DocType", doctype):
		return
	frappe.db.delete(doctype, {"settings": settings_name, "generation_batch": batch_id})
	# Also clear prior batch lines for this settings to keep latest grid focused
	frappe.db.delete(doctype, {"settings": settings_name})
	for i, r in enumerate(rows, start=1):
		account = r.get("account") or None
		if account and not frappe.db.exists("Account", account):
			account = None
		cost_center = r.get("cost_center") or None
		if cost_center and not frappe.db.exists("Cost Center", cost_center):
			cost_center = None
		department = r.get("department") or None
		if department and not frappe.db.exists("Department", department):
			# Excluded blank-dept excel label is not a Department Link — keep in reason only.
			department = None
		doc = frappe.get_doc(
			{
				"doctype": doctype,
				"settings": settings_name,
				"generation_batch": batch_id,
				"generation_date": ts,
				"account": account,
				"account_code": r.get("account_code"),
				"account_name": r.get("account_name"),
				"cost_center": cost_center,
				"department": department,
				"generation_status": r.get("generation_status") or r.get("action"),
				"action": r.get("action"),
				"reason": r.get("reason"),
				"idx": i,
			}
		)
		doc.flags.ignore_links = True
		doc.insert(ignore_permissions=True)
