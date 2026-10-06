# Copyright (c) 2026, ERPNext Extensions contributors
"""Single Stock Entry Dimension Repair — Scan / Preview / Dry Run / Apply.

GL rebuild path (proven against ERPNext 16.37.0 + Iran Accounting patches):

  expected = toggle_debit_credit_if_negative(se.get_gl_entries(inventory_account_map))
  _delete_accounting_ledger_entries("Stock Entry", se.name)
  se.make_gl_entries(gl_entries=expected, from_repost=True)

``make_gl_entries`` is the Iran-patched StockController method which runs
``apply_irr_rate_rounding_residual_gl`` + ``align_irr_gl_map_to_currency_precision``
before persisting. SLE is never touched.
"""

from __future__ import annotations

import hashlib
import json

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.iran_accounting.manufacturing_dimension.guard import (
	_norm,
	is_guard_applicable,
	validate_manufacturing_dimension_uniformity,
)

# SED fields that Apply may mutate. Everything else on SED is forbidden.
_ALLOWED_SED_MUTATIONS = frozenset({"department", "cost_center"})

# Forbidden SED fields — verified unchanged after Apply.
_FORBIDDEN_SED_FIELDS = (
	"qty",
	"transfer_qty",
	"basic_rate",
	"valuation_rate",
	"basic_amount",
	"amount",
	"stock_value_difference",
	"additional_cost",
	"item_code",
	"s_warehouse",
	"t_warehouse",
	"batch_no",
	"serial_no",
)

_SLE_INVARIANT_FIELDS = (
	"name",
	"item_code",
	"warehouse",
	"actual_qty",
	"stock_value_difference",
	"valuation_rate",
)

_GL_COMPARE_KEYS = (
	"account",
	"cost_center",
	"department",
	"debit",
	"credit",
	"against",
	"party_type",
	"party",
	"project",
)

_USED_FINGERPRINT_CACHE_KEY = "se_dimension_repair:used_fingerprints"


def assert_repair_permission() -> None:
	if frappe.session.user == "Administrator":
		return
	roles = set(frappe.get_roles())
	if roles & {"System Manager", "Accounts Manager"}:
		return
	frappe.throw(
		_("Stock Entry Dimension Repair requires Accounts Manager or System Manager."),
		frappe.PermissionError,
	)


def _require_stock_entry(name: str):
	if not name or not frappe.db.exists("Stock Entry", name):
		frappe.throw(_("Stock Entry {0} not found").format(name))
	return frappe.get_doc("Stock Entry", name)


def _row_payload(row) -> dict:
	return {
		"idx": row.idx,
		"row_name": row.name,
		"item_code": row.item_code,
		"department": _norm(getattr(row, "department", None)),
		"cost_center": _norm(getattr(row, "cost_center", None)),
	}


def _group_key(department: str, cost_center: str) -> str:
	return f"{department or '(blank)'} / {cost_center or '(blank)'}"


def scan_stock_entry_dimensions(stock_entry: str) -> dict:
	"""Read-only scan of SED dimensions and distinct groups."""
	se = _require_stock_entry(stock_entry)
	rows = [_row_payload(r) for r in (se.items or []) if r.item_code]
	groups_map: dict[str, list[dict]] = {}
	for r in rows:
		key = _group_key(r["department"], r["cost_center"])
		groups_map.setdefault(key, []).append(r)

	groups = []
	for key, group_rows in sorted(groups_map.items(), key=lambda x: x[1][0]["idx"]):
		groups.append(
			{
				"label": key,
				"department": group_rows[0]["department"],
				"cost_center": group_rows[0]["cost_center"],
				"row_count": len(group_rows),
				"rows": group_rows,
			}
		)

	status = "NO_REPAIR_NEEDED" if len(groups) <= 1 else "MIXED_DIMENSIONS"
	return {
		"name": se.name,
		"purpose": se.purpose,
		"docstatus": cint(se.docstatus),
		"posting_date": str(se.posting_date),
		"posting_time": str(se.posting_time),
		"modified": str(se.modified),
		"company": se.company,
		"rows": rows,
		"groups": groups,
		"status": status,
	}


def _validate_targets(company: str, department: str, cost_center: str) -> tuple[str, str]:
	department = _norm(department)
	cost_center = _norm(cost_center)
	if not department:
		frappe.throw(_("Target Department is required"))
	if not cost_center:
		frappe.throw(_("Target Cost Center is required"))
	if not frappe.db.exists("Department", department):
		frappe.throw(_("Department {0} does not exist").format(department))
	if not frappe.db.exists("Cost Center", cost_center):
		frappe.throw(_("Cost Center {0} does not exist").format(cost_center))
	cc_company = frappe.db.get_value("Cost Center", cost_center, "company")
	if cc_company and cc_company != company:
		frappe.throw(_("Cost Center {0} does not belong to company {1}").format(cost_center, company))
	return department, cost_center


def _validate_selected_rows(se, selected_row_names: list[str]) -> list[str]:
	selected = sorted({_norm(n) for n in (selected_row_names or []) if _norm(n)})
	if not selected:
		frappe.throw(_("At least one Stock Entry Detail row must be selected"))
	by_name = {r.name: r for r in (se.items or []) if r.item_code}
	missing = [n for n in selected if n not in by_name]
	if missing:
		frappe.throw(_("Selected rows not on Stock Entry {0}: {1}").format(se.name, ", ".join(missing)))
	return selected


def preview_dimension_repair(
	stock_entry: str,
	selected_row_names: list[str],
	target_department: str,
	target_cost_center: str,
) -> dict:
	"""Return exact before/after proposed SED dimension changes (no mutation)."""
	se = _require_stock_entry(stock_entry)
	target_department, target_cost_center = _validate_targets(
		se.company, target_department, target_cost_center
	)
	selected = _validate_selected_rows(se, selected_row_names)

	changes = []
	for row in se.items:
		if not row.item_code:
			continue
		before = _row_payload(row)
		after = dict(before)
		if row.name in selected:
			after["department"] = target_department
			after["cost_center"] = target_cost_center
		if before != after:
			changes.append({"before": before, "after": after})

	return {
		"name": se.name,
		"purpose": se.purpose,
		"docstatus": cint(se.docstatus),
		"selected_row_names": selected,
		"target_department": target_department,
		"target_cost_center": target_cost_center,
		"changes": changes,
		"unchanged_row_count": len([r for r in se.items if r.item_code and r.name not in selected]),
	}


def _fetch_active_gl(voucher_no: str) -> list[dict]:
	rows = frappe.db.sql(
		"""
		SELECT name, account, cost_center, department, debit, credit,
		       against, party_type, party, project, is_cancelled
		FROM `tabGL Entry`
		WHERE voucher_type=%s AND voucher_no=%s AND IFNULL(is_cancelled,0)=0
		ORDER BY account, cost_center, department, debit, credit, name
		""",
		("Stock Entry", voucher_no),
		as_dict=True,
	)
	return [dict(r) for r in rows]


def _fetch_sle_invariants(voucher_no: str) -> list[dict]:
	rows = frappe.db.sql(
		f"""
		SELECT {", ".join(_SLE_INVARIANT_FIELDS)}
		FROM `tabStock Ledger Entry`
		WHERE voucher_type=%s AND voucher_no=%s
		ORDER BY name
		""",
		("Stock Entry", voucher_no),
		as_dict=True,
	)
	return [dict(r) for r in rows]


def _fetch_sed_snapshot(voucher_no: str) -> list[dict]:
	fields = ["name", "idx", "item_code", "department", "cost_center", *_FORBIDDEN_SED_FIELDS]
	# Some sites may lack batch_no / serial_no depending on version; filter to existing.
	meta = frappe.get_meta("Stock Entry Detail")
	fields = [f for f in fields if f == "name" or meta.has_field(f) or f in ("name", "idx")]
	# Always keep name/idx/item_code/dept/cc
	base = ["name", "idx", "item_code", "department", "cost_center"]
	extra = [f for f in _FORBIDDEN_SED_FIELDS if meta.has_field(f)]
	cols = base + extra
	rows = frappe.db.sql(
		f"""
		SELECT {", ".join(f"`{c}`" for c in cols)}
		FROM `tabStock Entry Detail`
		WHERE parent=%s AND parenttype='Stock Entry'
		ORDER BY idx
		""",
		(voucher_no,),
		as_dict=True,
	)
	return [dict(r) for r in rows]


def _gl_compare_rows(rows: list[dict]) -> list[dict]:
	out = []
	for r in rows:
		entry = {k: r.get(k) for k in _GL_COMPARE_KEYS}
		entry["debit"] = flt(entry.get("debit"))
		entry["credit"] = flt(entry.get("credit"))
		entry["department"] = _norm(entry.get("department"))
		entry["cost_center"] = _norm(entry.get("cost_center"))
		entry["account"] = _norm(entry.get("account"))
		out.append(entry)
	out.sort(
		key=lambda e: (
			e.get("account") or "",
			e.get("cost_center") or "",
			e.get("department") or "",
			flt(e.get("debit")),
			flt(e.get("credit")),
			e.get("against") or "",
		)
	)
	return out


def _gl_summary(rows: list[dict]) -> dict:
	debit = sum(flt(r.get("debit")) for r in rows)
	credit = sum(flt(r.get("credit")) for r in rows)
	by_key: dict[str, dict] = {}
	for r in _gl_compare_rows(rows):
		key = f"{r['account']}|{r['cost_center']}|{r['department']}"
		slot = by_key.setdefault(key, {"account": r["account"], "cost_center": r["cost_center"], "department": r["department"], "debit": 0.0, "credit": 0.0})
		slot["debit"] = flt(slot["debit"] + flt(r["debit"]))
		slot["credit"] = flt(slot["credit"] + flt(r["credit"]))
	return {
		"debit": flt(debit),
		"credit": flt(credit),
		"balanced": abs(debit - credit) < 0.5,
		"row_count": len(rows),
		"by_dimension": sorted(by_key.values(), key=lambda x: (x["account"], x["cost_center"], x["department"])),
	}


def _gl_diff(current: list[dict], proposed: list[dict]) -> dict:
	cur = _gl_compare_rows(current)
	prop = _gl_compare_rows(proposed)
	# Multiset diff by compare-key tuple
	from collections import Counter

	def key(e):
		return tuple(e.get(k) for k in _GL_COMPARE_KEYS)

	c_cur = Counter(key(e) for e in cur)
	c_prop = Counter(key(e) for e in prop)
	removed = []
	added = []
	for k, n in (c_cur - c_prop).items():
		payload = dict(zip(_GL_COMPARE_KEYS, k, strict=False))
		payload["count"] = n
		removed.append(payload)
	for k, n in (c_prop - c_cur).items():
		payload = dict(zip(_GL_COMPARE_KEYS, k, strict=False))
		payload["count"] = n
		added.append(payload)
	return {
		"identical": not removed and not added,
		"removed": removed,
		"added": added,
		"current_summary": _gl_summary(current),
		"proposed_summary": _gl_summary(proposed),
	}


def _build_fingerprint(
	se,
	selected: list[str],
	target_department: str,
	target_cost_center: str,
	current_gl: list[dict],
	current_sed: list[dict],
) -> str:
	payload = {
		"stock_entry": se.name,
		"modified": str(se.modified),
		"selected": sorted(selected),
		"target_department": target_department,
		"target_cost_center": target_cost_center,
		"sed": [
			{
				"name": r.get("name"),
				"department": _norm(r.get("department")),
				"cost_center": _norm(r.get("cost_center")),
			}
			for r in current_sed
		],
		"gl": _gl_compare_rows(current_gl),
	}
	raw = json.dumps(payload, sort_keys=True, default=str)
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fingerprint_used(fp: str) -> bool:
	used = frappe.cache().get_value(_USED_FINGERPRINT_CACHE_KEY) or []
	return fp in used


def _mark_fingerprint_used(fp: str) -> None:
	used = list(frappe.cache().get_value(_USED_FINGERPRINT_CACHE_KEY) or [])
	if fp not in used:
		used.append(fp)
		# Keep bounded
		frappe.cache().set_value(_USED_FINGERPRINT_CACHE_KEY, used[-200:])


def compute_proposed_gl_map(se) -> list[dict]:
	"""In-memory GL map matching the Apply rebuild engine (no DB write).

	Mirrors Iran StockController.make_gl_entries post-processing + process_gl_map
	as used when persisting with from_repost=True.
	"""
	from erpnext.accounts.general_ledger import process_gl_map, toggle_debit_credit_if_negative
	from erpnext_extensions.iran_accounting.domain.currency import is_irr_company
	from erpnext_extensions.iran_accounting.domain.irr_gl_precision_align import (
		align_irr_gl_map_to_currency_precision,
	)
	from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
		apply_irr_rate_rounding_residual_gl,
	)

	inventory_account_map = se.get_inventory_account_map()
	gl_entries = toggle_debit_credit_if_negative(se.get_gl_entries(inventory_account_map)) or []
	if is_irr_company(se.company) and gl_entries is not None:
		apply_irr_rate_rounding_residual_gl(se, gl_entries)
		align_irr_gl_map_to_currency_precision(se, gl_entries)
	if gl_entries:
		gl_entries = process_gl_map(gl_entries, merge_entries=True, from_repost=True) or []

	out = []
	for e in gl_entries or []:
		if isinstance(e, dict):
			out.append(dict(e))
		elif callable(getattr(e, "as_dict", None)):
			out.append(dict(e.as_dict()))
		else:
			out.append(dict(e))
	return out


def _apply_dimensions_in_memory(se, selected: list[str], department: str, cost_center: str):
	for row in se.items or []:
		if row.name in selected:
			row.department = department
			row.cost_center = cost_center


def dry_run_dimension_repair(
	stock_entry: str,
	selected_row_names: list[str],
	target_department: str,
	target_cost_center: str,
) -> dict:
	"""Guaranteed non-persistent Dry Run. Zero SED/GL/SLE writes."""
	se = _require_stock_entry(stock_entry)
	if cint(se.docstatus) != 1:
		frappe.throw(_("Dry Run requires a submitted Stock Entry"))

	target_department, target_cost_center = _validate_targets(
		se.company, target_department, target_cost_center
	)
	selected = _validate_selected_rows(se, selected_row_names)

	# Snapshots BEFORE any in-memory mutation (and prove no DB change after)
	modified_before = str(se.modified)
	sed_before = _fetch_sed_snapshot(se.name)
	gl_before = _fetch_active_gl(se.name)
	sle_before = _fetch_sle_invariants(se.name)

	preview = preview_dimension_repair(
		stock_entry, selected, target_department, target_cost_center
	)

	# In-memory only
	_apply_dimensions_in_memory(se, selected, target_department, target_cost_center)
	proposed_gl = compute_proposed_gl_map(se)

	# Prove zero persistence
	modified_after = str(frappe.db.get_value("Stock Entry", se.name, "modified"))
	sed_after = _fetch_sed_snapshot(se.name)
	gl_after = _fetch_active_gl(se.name)
	sle_after = _fetch_sle_invariants(se.name)

	db_clean = (
		modified_before == modified_after
		and sed_before == sed_after
		and _gl_compare_rows(gl_before) == _gl_compare_rows(gl_after)
		and sle_before == sle_after
	)

	fingerprint = _build_fingerprint(
		frappe.get_doc("Stock Entry", se.name),  # fresh modified
		selected,
		target_department,
		target_cost_center,
		gl_before,
		sed_before,
	)

	proposed_dimensions = []
	for row in sed_before:
		payload = {
			"row_name": row["name"],
			"idx": row.get("idx"),
			"item_code": row.get("item_code"),
			"department": _norm(row.get("department")),
			"cost_center": _norm(row.get("cost_center")),
		}
		if row["name"] in selected:
			payload["department"] = target_department
			payload["cost_center"] = target_cost_center
		proposed_dimensions.append(payload)

	diff = _gl_diff(gl_before, proposed_gl)
	validation = {
		"db_mutation": not db_clean,
		"proposed_gl_balanced": _gl_summary(proposed_gl)["balanced"],
		"sle_impact": "NONE",
		"ok": db_clean and _gl_summary(proposed_gl)["balanced"],
	}

	return {
		"dry_run": True,
		"name": se.name,
		"purpose": se.purpose,
		"selected_row_names": selected,
		"target_department": target_department,
		"target_cost_center": target_cost_center,
		"current_dimensions": [
			{
				"row_name": r["name"],
				"idx": r.get("idx"),
				"item_code": r.get("item_code"),
				"department": _norm(r.get("department")),
				"cost_center": _norm(r.get("cost_center")),
			}
			for r in sed_before
		],
		"proposed_dimensions": proposed_dimensions,
		"preview_changes": preview.get("changes"),
		"current_gl": _gl_compare_rows(gl_before),
		"proposed_gl": _gl_compare_rows(proposed_gl),
		"gl_diff": diff,
		"sle_impact": {"changed": False, "count_before": len(sle_before), "count_after": len(sle_after)},
		"validation": validation,
		"fingerprint": fingerprint,
		"db_clean": db_clean,
		"modified_unchanged": modified_before == modified_after,
	}


def rebuild_gl_only_for_stock_entry(se) -> dict:
	"""Proven GL-only rebuild. Does not touch SLE.

	Path:
	  toggle_debit_credit_if_negative(get_gl_entries(...))
	  _delete_accounting_ledger_entries(...)
	  make_gl_entries(..., from_repost=True)  # Iran-patched
	"""
	from erpnext.accounts.general_ledger import toggle_debit_credit_if_negative
	from erpnext.accounts.utils import _delete_accounting_ledger_entries

	if getattr(frappe.flags, "force_dimension_repair_gl_fail", False):
		raise frappe.ValidationError("Forced GL rebuild failure (test)")

	inventory_account_map = se.get_inventory_account_map()
	expected = toggle_debit_credit_if_negative(se.get_gl_entries(inventory_account_map))
	_delete_accounting_ledger_entries("Stock Entry", se.name)
	if expected:
		se.make_gl_entries(gl_entries=expected, from_repost=True)
	after = _fetch_active_gl(se.name)
	summary = _gl_summary(after)
	if not summary["balanced"]:
		raise frappe.ValidationError(
			_("GL rebuild failed for {0}: debit={1} credit={2}").format(
				se.name, summary["debit"], summary["credit"]
			)
		)
	return {"rebuilt": True, "gl": summary}


def _verify_apply(
	se_name: str,
	*,
	selected: list[str],
	target_department: str,
	target_cost_center: str,
	sed_before: list[dict],
	sle_before: list[dict],
	proposed_gl: list[dict],
) -> list[str]:
	errors = []
	gl_after = _fetch_active_gl(se_name)
	summary = _gl_summary(gl_after)
	# V1
	if not summary["balanced"]:
		errors.append("V1: active GL debit != credit")
	# V2 / V3
	sle_after = _fetch_sle_invariants(se_name)
	if len(sle_after) != len(sle_before):
		errors.append("V2: SLE count changed")
	if sle_after != sle_before:
		errors.append("V3: SLE invariants changed")
	# V4 / V5
	sed_after = _fetch_sed_snapshot(se_name)
	before_by = {r["name"]: r for r in sed_before}
	after_by = {r["name"]: r for r in sed_after}
	if set(before_by) != set(after_by):
		errors.append("V4: SED row set changed")
	meta = frappe.get_meta("Stock Entry Detail")
	forbidden = [f for f in _FORBIDDEN_SED_FIELDS if meta.has_field(f)]
	for name, before in before_by.items():
		after = after_by.get(name) or {}
		for f in forbidden:
			if before.get(f) != after.get(f):
				errors.append(f"V4: forbidden SED field {f} changed on {name}")
		if name in selected:
			if _norm(after.get("department")) != target_department:
				errors.append(f"V5: selected row {name} department not updated")
			if _norm(after.get("cost_center")) != target_cost_center:
				errors.append(f"V5: selected row {name} cost_center not updated")
		else:
			if _norm(after.get("department")) != _norm(before.get("department")):
				errors.append(f"V5: unselected row {name} department changed")
			if _norm(after.get("cost_center")) != _norm(before.get("cost_center")):
				errors.append(f"V5: unselected row {name} cost_center changed")
	# V6
	if _gl_compare_rows(gl_after) != _gl_compare_rows(proposed_gl):
		errors.append("V6: active GL does not match Dry Run proposed GL")
	# V7 — no duplicate active rows by compare key with inflated counts vs proposed
	from collections import Counter

	def key(e):
		return tuple(e.get(k) for k in _GL_COMPARE_KEYS)

	if Counter(key(e) for e in _gl_compare_rows(gl_after)) != Counter(
		key(e) for e in _gl_compare_rows(proposed_gl)
	):
		errors.append("V7: duplicate or mismatched active GL after rebuild")
	# V8
	se = frappe.get_doc("Stock Entry", se_name)
	if is_guard_applicable(se):
		try:
			validate_manufacturing_dimension_uniformity(se)
		except frappe.ValidationError:
			errors.append("V8: Guard still fails after repair")
	if getattr(frappe.flags, "force_dimension_repair_verify_fail", False):
		errors.append("Forced verify failure (test)")
	return errors


def _write_audit_comment(
	se_name: str,
	*,
	selected: list[str],
	before_dims: list[dict],
	after_dims: list[dict],
	fingerprint: str,
	gl_before_summary: dict,
	gl_after_summary: dict,
) -> None:
	content = (
		"<b>Stock Entry Dimension Repair</b><br>"
		f"User: {frappe.session.user}<br>"
		f"Timestamp: {now_datetime()}<br>"
		f"Selected rows: {', '.join(selected)}<br>"
		f"Fingerprint: {fingerprint}<br>"
		f"Before dimensions: {frappe.as_json(before_dims)}<br>"
		f"After dimensions: {frappe.as_json(after_dims)}<br>"
		f"GL before: debit={gl_before_summary.get('debit')} credit={gl_before_summary.get('credit')} rows={gl_before_summary.get('row_count')}<br>"
		f"GL after: debit={gl_after_summary.get('debit')} credit={gl_after_summary.get('credit')} rows={gl_after_summary.get('row_count')}"
	)
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"reference_doctype": "Stock Entry",
			"reference_name": se_name,
			"content": content,
		}
	).insert(ignore_permissions=True)


def apply_dimension_repair(
	stock_entry: str,
	selected_row_names: list[str],
	target_department: str,
	target_cost_center: str,
	fingerprint: str,
	confirm: bool = False,
) -> dict:
	"""Apply SED department/cost_center changes + GL-only rebuild for one voucher.

	Requires a matching Dry Run fingerprint and explicit confirm=True.
	"""
	assert_repair_permission()
	if not confirm:
		frappe.throw(_("Explicit confirmation is required to Apply Dimension Repair"))
	if not fingerprint:
		frappe.throw(_("Dry Run fingerprint is required"))
	if _fingerprint_used(fingerprint):
		frappe.throw(_("This Dry Run fingerprint was already used. Run a fresh Dry Run."))

	se = _require_stock_entry(stock_entry)
	if cint(se.docstatus) != 1:
		frappe.throw(_("Apply requires a submitted Stock Entry"))

	# Immutable ledger hard-delete path required (site must allow GL delete/repost)
	if cint(frappe.db.get_single_value("Accounts Settings", "enable_immutable_ledger")):
		frappe.throw(
			_(
				"Cannot Apply Dimension Repair while Accounts Settings.enable_immutable_ledger is enabled "
				"(GL-only rebuild deletes and recreates GL for this voucher)."
			)
		)

	target_department, target_cost_center = _validate_targets(
		se.company, target_department, target_cost_center
	)
	selected = _validate_selected_rows(se, selected_row_names)

	sed_before = _fetch_sed_snapshot(se.name)
	gl_before = _fetch_active_gl(se.name)
	sle_before = _fetch_sle_invariants(se.name)

	expected_fp = _build_fingerprint(
		se, selected, target_department, target_cost_center, gl_before, sed_before
	)
	if fingerprint != expected_fp:
		frappe.throw(
			_(
				"Stale Dry Run fingerprint — Stock Entry state changed since Dry Run. "
				"Run Dry Run again."
			)
		)

	# Compute proposed GL from in-memory mutated copy (same as Dry Run) for V6.
	se_mem = frappe.get_doc("Stock Entry", se.name)
	_apply_dimensions_in_memory(se_mem, selected, target_department, target_cost_center)
	proposed_gl = compute_proposed_gl_map(se_mem)

	before_dims = [
		{
			"row_name": r["name"],
			"department": _norm(r.get("department")),
			"cost_center": _norm(r.get("cost_center")),
		}
		for r in sed_before
		if r["name"] in selected
	]

	try:
		# Mutate ONLY approved SED fields
		for row_name in selected:
			frappe.db.set_value(
				"Stock Entry Detail",
				row_name,
				{"department": target_department, "cost_center": target_cost_center},
				update_modified=False,
			)

		se_fresh = frappe.get_doc("Stock Entry", se.name)
		rebuild_gl_only_for_stock_entry(se_fresh)

		errors = _verify_apply(
			se.name,
			selected=selected,
			target_department=target_department,
			target_cost_center=target_cost_center,
			sed_before=sed_before,
			sle_before=sle_before,
			proposed_gl=proposed_gl,
		)
		if errors:
			raise frappe.ValidationError(
				_("Dimension Repair verification failed:\n{0}").format("\n".join(errors))
			)

		gl_after = _fetch_active_gl(se.name)
		after_dims = [
			{
				"row_name": n,
				"department": target_department,
				"cost_center": target_cost_center,
			}
			for n in selected
		]
		_write_audit_comment(
			se.name,
			selected=selected,
			before_dims=before_dims,
			after_dims=after_dims,
			fingerprint=fingerprint,
			gl_before_summary=_gl_summary(gl_before),
			gl_after_summary=_gl_summary(gl_after),
		)
		_mark_fingerprint_used(fingerprint)
		# Explicit commit boundary for callers that manage transactions in tests
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		raise

	return {
		"applied": True,
		"name": se.name,
		"selected_row_names": selected,
		"target_department": target_department,
		"target_cost_center": target_cost_center,
		"fingerprint": fingerprint,
		"gl_before": _gl_summary(gl_before),
		"gl_after": _gl_summary(_fetch_active_gl(se.name)),
		"sle_unchanged": True,
	}
