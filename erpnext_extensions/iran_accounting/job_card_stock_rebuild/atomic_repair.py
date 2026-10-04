# Copyright (c) 2026, ERPNext Extensions contributors
"""Atomic Dry Run / Apply engine for canonical Manufacture repair (v5.5.0)."""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.iran_accounting.historical_stock import HISTORICAL_REPAIR_FLAG
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
	build_manufacture_plan,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.sync_valuation import (
	suppress_auto_riv,
	sync_valuation_for_vouchers,
)
from erpnext_extensions.iran_accounting.stock_posting_order.prevention import PREVENTION_FLAG


def _fail_point(name: str):
	if getattr(frappe.flags, "jc_repair_fail_at", None) == name:
		raise RuntimeError(f"INJECTED_FAILURE:{name}")


def _lock_scope(job_card: str, stock_entries: list[str]):
	frappe.db.sql("select name from `tabJob Card` where name=%s for update", job_card)
	wo = frappe.db.get_value("Job Card", job_card, "work_order")
	if wo:
		frappe.db.sql("select name from `tabWork Order` where name=%s for update", wo)
	for name in sorted(set(stock_entries)):
		frappe.db.sql("select name from `tabStock Entry` where name=%s for update", name)


def _snapshot_business(job_card: str, se_names: list[str]) -> dict:
	return {
		"jc": frappe.db.get_value(
			"Job Card",
			job_card,
			["modified", "status", "total_completed_qty", "for_quantity"],
			as_dict=1,
		),
		"wo": frappe.db.get_value(
			"Work Order",
			frappe.db.get_value("Job Card", job_card, "work_order"),
			["modified", "status", "produced_qty"],
			as_dict=1,
		)
		if frappe.db.get_value("Job Card", job_card, "work_order")
		else None,
		"ses": frappe.db.sql(
			"""
			select name, docstatus, modified, purpose, is_return, fg_completed_qty
			from `tabStock Entry` where name in %s order by name
			""",
			(se_names or ["__none__"],),
			as_dict=1,
		),
		"sle_count": frappe.db.sql(
			"""
			select count(*) from `tabStock Ledger Entry`
			where voucher_no in %s and is_cancelled=0
			""",
			(se_names or ["__none__"],),
		)[0][0],
		"gl_count": frappe.db.sql(
			"""
			select count(*) from `tabGL Entry`
			where voucher_type='Stock Entry' and voucher_no in %s and is_cancelled=0
			""",
			(se_names or ["__none__"],),
		)[0][0],
	}


def _cancel_se(name: str):
	doc = frappe.get_doc("Stock Entry", name)
	if doc.docstatus != 1:
		return
	doc.flags.ignore_permissions = True
	doc.cancel()


def _snapshot_se_batches(name: str) -> list[dict]:
	"""Capture Item×Batch×qty×rates before cancel (cancel may clear SED.batch_no)."""
	rows = frappe.db.sql(
		"""
		select name, idx, item_code, batch_no, serial_and_batch_bundle, qty,
		       s_warehouse, t_warehouse, basic_rate, valuation_rate,
		       expense_account, cost_center
		from `tabStock Entry Detail` where parent=%s order by idx
		""",
		name,
		as_dict=1,
	)
	out = []
	for r in rows:
		batch = r.batch_no
		if not batch and r.serial_and_batch_bundle:
			bb = frappe.db.sql(
				"""
				select batch_no from `tabSerial and Batch Entry`
				where parent=%s and ifnull(batch_no,'')!='' limit 1
				""",
				r.serial_and_batch_bundle,
			)
			batch = bb[0][0] if bb else None
		rate = flt(r.valuation_rate) or flt(r.basic_rate)
		# Prefer SLE economics for this detail row (authoritative for recreate).
		sle_rate = frappe.db.sql(
			"""
			select actual_qty, incoming_rate, outgoing_rate, valuation_rate, batch_no
			from `tabStock Ledger Entry`
			where voucher_no=%s and voucher_detail_no=%s and is_cancelled=0
			order by creation
			""",
			(name, r.name),
			as_dict=1,
		)
		if not sle_rate:
			sle_rate = frappe.db.sql(
				"""
				select actual_qty, incoming_rate, outgoing_rate, valuation_rate, batch_no
				from `tabStock Ledger Entry`
				where voucher_no=%s and item_code=%s and is_cancelled=0
				order by creation
				""",
				(name, r.item_code),
				as_dict=1,
			)
		if sle_rate:
			if not batch:
				for s in sle_rate:
					if s.batch_no:
						batch = s.batch_no
						break
			# Outward uses outgoing_rate; inward uses incoming_rate.
			for s in sle_rate:
				cand = flt(s.outgoing_rate) or flt(s.incoming_rate) or flt(s.valuation_rate)
				if cand > 0:
					rate = cand
					break
		out.append(
			{
				"idx": r.idx,
				"item_code": r.item_code,
				"batch_no": batch,
				"qty": flt(r.qty),
				"s_warehouse": r.s_warehouse,
				"t_warehouse": r.t_warehouse,
				"basic_rate": rate,
				"valuation_rate": rate,
				"expense_account": r.expense_account,
				"cost_center": r.cost_center,
			}
		)
	return out


def _reapply_snapshot_rates(doc, batch_snapshot: list[dict] | None = None):
	"""Force snapshot rates (Core set_rate_for_outgoing_items overwrites mid-repair)."""
	by_idx = {cint(r["idx"]): r for r in (batch_snapshot or [])}
	# Prefer idx; item×batch fallback only when unique in snapshot.
	batch_counts: dict[tuple[str, str], int] = {}
	by_item_batch: dict[tuple[str, str], dict] = {}
	for r in batch_snapshot or []:
		key = (r["item_code"], r.get("batch_no") or "")
		batch_counts[key] = batch_counts.get(key, 0) + 1
		by_item_batch[key] = r
	for row in doc.items:
		snap = by_idx.get(cint(row.idx))
		if not snap:
			key = (row.item_code, row.batch_no or "")
			if batch_counts.get(key) == 1:
				snap = by_item_batch.get(key)
		rate = flt((snap or {}).get("basic_rate") or (snap or {}).get("valuation_rate"))
		if rate <= 0:
			rate = flt(row.basic_rate) or flt(row.valuation_rate)
		if rate <= 0:
			frappe.throw(
				frappe._("Repair recreate missing rate for {0} / {1} on {2}").format(
					row.item_code, row.batch_no, doc.name or doc.doctype
				)
			)
		qty = flt(row.transfer_qty) or flt(row.qty)
		row.basic_rate = rate
		row.valuation_rate = rate
		row.set_basic_rate_manually = 1
		row.allow_zero_valuation_rate = 0
		row.basic_amount = qty * rate
		row.amount = qty * rate


def _bind_preserve_rates(doc, batch_snapshot: list[dict] | None = None):
	"""Core validate recalculates outgoing rates from live stock — preserve snapshot."""
	original = doc.calculate_rate_and_amount

	def _calc(reset_outgoing_rate=True, raise_error_if_no_rate=True):
		original(reset_outgoing_rate=False, raise_error_if_no_rate=False)
		_reapply_snapshot_rates(doc, batch_snapshot=batch_snapshot)
		if hasattr(doc, "set_total_incoming_outgoing_value"):
			doc.set_total_incoming_outgoing_value()
		if hasattr(doc, "set_total_amount"):
			doc.set_total_amount()

	doc.calculate_rate_and_amount = _calc


def _recreate_logistics(source_name: str, batch_snapshot: list[dict] | None = None) -> str:
	src = frappe.get_doc("Stock Entry", source_name)
	neu = frappe.copy_doc(src)
	neu.name = None
	_prepare_se_for_repair_submit(neu, batch_snapshot=batch_snapshot)
	if hasattr(neu, "set_posting_time"):
		neu.set_posting_time = 1
	neu.flags.ignore_permissions = True
	_bind_preserve_rates(neu, batch_snapshot=batch_snapshot)
	neu.insert()
	_reapply_snapshot_rates(neu, batch_snapshot=batch_snapshot)
	neu.save()
	_reapply_snapshot_rates(neu, batch_snapshot=batch_snapshot)
	neu.submit()
	return neu.name


def _prepare_se_for_repair_submit(doc, batch_snapshot: list[dict] | None = None):
	"""Reset workflow / amendment fields so repair submit is not blocked."""
	doc.docstatus = 0
	doc.amended_from = None
	if hasattr(doc, "workflow_state"):
		doc.workflow_state = "Draft"
	by_idx = {cint(r["idx"]): r for r in (batch_snapshot or [])}
	by_item_batch = {
		(r["item_code"], r.get("batch_no") or ""): r for r in (batch_snapshot or [])
	}
	for row in doc.items:
		snap = by_idx.get(cint(row.idx))
		if not snap:
			snap = by_item_batch.get((row.item_code, row.batch_no or ""))
		if snap:
			if snap.get("batch_no") and not row.batch_no:
				row.batch_no = snap["batch_no"]
			rate = flt(snap.get("basic_rate") or snap.get("valuation_rate"))
			if rate > 0:
				row.basic_rate = rate
				row.valuation_rate = rate
				row.allow_zero_valuation_rate = 0
			if snap.get("expense_account") and not row.expense_account:
				row.expense_account = snap["expense_account"]
			if snap.get("cost_center") and not row.cost_center:
				row.cost_center = snap["cost_center"]
		if not row.batch_no and row.serial_and_batch_bundle:
			bb = frappe.db.sql(
				"""
				select batch_no from `tabSerial and Batch Entry`
				where parent=%s and ifnull(batch_no,'')!='' limit 1
				""",
				row.serial_and_batch_bundle,
			)
			if bb:
				row.batch_no = bb[0][0]
		row.serial_and_batch_bundle = None
		row.rejected_serial_and_batch_bundle = None
		if row.batch_no:
			row.use_serial_batch_fields = 1
		# Transfers must carry a positive rate for Iran valuation integrity.
		if flt(row.basic_rate) <= 0 and flt(row.valuation_rate) > 0:
			row.basic_rate = flt(row.valuation_rate)
		if flt(row.valuation_rate) <= 0 and flt(row.basic_rate) > 0:
			row.valuation_rate = flt(row.basic_rate)
		# Core Stock Entry.validate zeros basic_rate unless this flag is set,
		# then recalculates — which can yield 0 mid-repair chronology.
		if flt(row.basic_rate) > 0:
			row.set_basic_rate_manually = 1
			row.allow_zero_valuation_rate = 0

def _build_canonical_se(plan: dict) -> str:
	canon = plan["canonical_manufacture"]
	supersedes = canon.get("supersedes") or []
	if not supersedes:
		frappe.throw("No Manufacture template to rebuild from")
	template_name = supersedes[0]
	template = frappe.get_doc("Stock Entry", template_name)

	doc = frappe.copy_doc(template)
	doc.name = None
	_prepare_se_for_repair_submit(doc)
	doc.job_card = plan["job_card"]
	doc.work_order = plan.get("work_order")
	doc.purpose = "Manufacture"
	doc.stock_entry_type = template.stock_entry_type or "Manufacture"
	doc.fg_completed_qty = flt(canon.get("fg_completed_qty"))
	if canon.get("posting_date"):
		doc.posting_date = canon["posting_date"]
	if canon.get("posting_time"):
		doc.posting_time = canon["posting_time"]
	if hasattr(doc, "set_posting_time"):
		doc.set_posting_time = 1
	if canon.get("historical_stamp") and hasattr(doc, "custom_manufacturing_costing_contract_version"):
		doc.custom_manufacturing_costing_contract_version = canon["historical_stamp"]

	# Site Server Script requires Manufacture expense_account = company stock adjustment.
	company = doc.company or template.company
	adj_account = (
		frappe.db.get_value("Company", company, "stock_adjustment_account")
		or "621301 - تعدیلات موجودی کالا - E"
	)
	default_cc = None
	for tr in template.items:
		if tr.cost_center:
			default_cc = tr.cost_center
			break

	doc.items = []
	for idx, row in enumerate(canon.get("rows") or [], start=1):
		child = {
			"item_code": row["item_code"],
			"qty": flt(row["qty"]),
			"transfer_qty": flt(row["qty"]),
			"s_warehouse": row.get("s_warehouse"),
			"t_warehouse": row.get("t_warehouse"),
			"batch_no": row.get("batch_no") or None,
			"is_finished_item": cint(row.get("is_finished_item")),
			"basic_rate": flt(row.get("basic_rate") or row.get("valuation_rate")),
			"valuation_rate": flt(row.get("valuation_rate") or row.get("basic_rate")),
			"secondary_item_type": row.get("secondary_item_type"),
			"use_serial_batch_fields": 1 if row.get("batch_no") else 0,
			"expense_account": adj_account,
			"cost_center": row.get("cost_center") or default_cc,
		}
		if row.get("custom_output_class"):
			child["custom_output_class"] = row["custom_output_class"]
		if (row.get("type") == "COMPONENT_SCRAP") or (
			(row.get("custom_output_class") or "") == "COMPONENT_SCRAP"
		):
			child["is_scrap_item"] = 0
			child["secondary_item_type"] = child.get("secondary_item_type") or "Scrap"
		doc.append("items", child)

	doc.flags.ignore_permissions = True
	# Site Server Script "Custom 9" bumps Manufacture after MTfM; skip via doc.flags.
	doc.flags.jc_manufacture_repair = True
	# Historical repair must keep the original Manufacture chronology; PPO would
	# otherwise bump posting after remaining WIP Issues / other dependents.
	if hasattr(doc, "set_posting_time"):
		doc.set_posting_time = 1
	doc.insert()
	# Re-assert posting after validate hooks.
	if canon.get("posting_date"):
		doc.posting_date = canon["posting_date"]
	if canon.get("posting_time"):
		doc.posting_time = canon["posting_time"]
	if hasattr(doc, "set_posting_time"):
		doc.set_posting_time = 1
	doc.flags.jc_manufacture_repair = True
	_fail_point("after_canonical_insert")
	doc.submit()
	# Current Manufacture contract may stamp 5.3.43 on submit; restore historical.
	if canon.get("historical_stamp") and hasattr(doc, "custom_manufacturing_costing_contract_version"):
		frappe.db.set_value(
			"Stock Entry",
			doc.name,
			"custom_manufacturing_costing_contract_version",
			canon["historical_stamp"],
			update_modified=False,
		)
	_fail_point("after_canonical_submit")
	return doc.name


def _verify(plan: dict, canonical_name: str) -> dict:
	job_card = plan["job_card"]
	active = frappe.db.sql(
		"""
		select name from `tabStock Entry`
		where job_card=%s and purpose='Manufacture' and docstatus=1
		""",
		job_card,
		as_dict=1,
	)
	active_names = [r.name for r in active]
	errors = []
	if len(active_names) != 1 or active_names[0] != canonical_name:
		errors.append(f"Expected one active Manufacture {canonical_name}, found {active_names}")

	# Golden Rule after repair
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import scan_golden_rule

	scan = scan_golden_rule(job_card)
	for row in scan["rows"]:
		# Skip non-component noise (defensive; scan already filters FG-only rows).
		if flt(row.get("issued")) <= 1e-9 and flt(row.get("consumed")) <= 1e-9:
			continue
		# Allow still-in-wip only if disposition said so
		disp = next(
			(
				d
				for d in plan.get("dispositions") or []
				if d["item_code"] == row["item_code"] and (d.get("batch_no") or "") == (row.get("batch_no") or "")
			),
			None,
		)
		allowed_rem = flt(disp.get("proposed_still_in_wip")) if disp else 0.0
		if abs(flt(row["remaining_wip"]) - allowed_rem) > 1e-6:
			errors.append(
				f"Golden Rule fail {row['item_code']}/{row['batch_no']}: remaining {row['remaining_wip']} allowed {allowed_rem}"
			)
		if row["status"] == "SCRAP MISMATCH":
			errors.append(f"Scrap mismatch remains for {row['item_code']}")

	_fail_point("during_sle_verify")
	# GL balance for canonical
	gl = frappe.db.sql(
		"""
		select sum(debit) d, sum(credit) c from `tabGL Entry`
		where voucher_type='Stock Entry' and voucher_no=%s and is_cancelled=0
		""",
		canonical_name,
		as_dict=1,
	)[0]
	_fail_point("during_gl_verify")
	if abs(flt(gl.d) - flt(gl.c)) > 1e-6:
		errors.append(f"GL imbalance on {canonical_name}: debit {gl.d} credit {gl.c}")

	sle_n = frappe.db.sql(
		"select count(*) from `tabStock Ledger Entry` where voucher_no=%s and is_cancelled=0",
		canonical_name,
	)[0][0]
	if cint(sle_n) <= 0:
		errors.append(f"No active SLE for {canonical_name}")

	_fail_point("during_sle_verify")
	_fail_point("during_gl_verify")
	return {"ok": not errors, "errors": errors, "active_manufactures": active_names, "scan": scan}


def _write_audit(payload: dict) -> str | None:
	try:
		doc = frappe.get_doc(
			{
				"doctype": "Job Card Stock Rebuild Log",
				"run_id": payload.get("repair_run_id") or str(uuid.uuid4()),
				"job_card": payload.get("job_card"),
				"work_order": payload.get("work_order"),
				"user": frappe.session.user,
				"timestamp": now_datetime(),
				"mode": payload.get("mode") or "DRY_RUN",
				"status": payload.get("status") or payload.get("overall_status"),
				"before_snapshot": json.dumps(payload.get("before_snapshot") or {}, default=str),
				"evidence_snapshot": json.dumps(
					{
						"fingerprint": payload.get("fingerprint"),
						"dispositions": payload.get("dispositions"),
						"merge_documents": payload.get("merge_documents"),
						"merge_material_issues": payload.get("merge_material_issues"),
					},
					default=str,
				),
				"proposed_snapshot": json.dumps(payload.get("canonical_manufacture") or {}, default=str),
				"after_snapshot": json.dumps(payload.get("after_snapshot") or {}, default=str),
				"verification_snapshot": json.dumps(payload.get("verification") or {}, default=str),
				"warnings": json.dumps(payload.get("blockers") or [], default=str),
				"affected_links": json.dumps(
					{
						"cancelled": payload.get("cancelled"),
						"created": payload.get("created"),
						"recreated_logistics": payload.get("recreated_logistics"),
					},
					default=str,
				),
				"error": (payload.get("error") or "")[:140],
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.log_error("jc_manufacture_repair_audit_failed")
		return None


def run_repair(
	job_card: str,
	plan_input: dict | None = None,
	dry_run: bool = True,
	confirm: int | bool = 0,
) -> dict[str, Any]:
	"""Same engine for Dry Run and Apply.

	dry_run=True  → always rollback
	dry_run=False → commit once after verification (requires confirm)
	"""
	if not dry_run and not cint(confirm):
		frappe.throw(frappe._("Confirmation required before Apply."))

	plan_input = plan_input or {}
	plan = build_manufacture_plan(
		job_card,
		dispositions=plan_input.get("dispositions"),
		merge_documents=plan_input.get("merge_documents"),
		stamp_mode=plan_input.get("stamp_mode"),
		merge_material_issues=plan_input.get("merge_material_issues"),
	)
	client_fp = plan_input.get("fingerprint")
	if client_fp and client_fp != plan["fingerprint"]:
		return {
			"ok": False,
			"status": "STALE PLAN",
			"error": "STALE PLAN — re-scan / rebuild plan required",
			"dry_run": dry_run,
			"mutated": False,
			"fingerprint": plan["fingerprint"],
		}
	if plan.get("blockers"):
		return {
			"ok": False,
			"status": "BLOCKED",
			"blockers": plan["blockers"],
			"error": "; ".join(plan["blockers"]),
			"dry_run": dry_run,
			"mutated": False,
			"plan": plan,
		}

	repair_run_id = str(uuid.uuid4())
	merge_docs = list(plan.get("merge_documents") or [])
	merge_mis = list(plan.get("merge_material_issues") or [])
	logistics = [x.name for x in (plan.get("downstream_logistics") or [])]
	scope = merge_docs + merge_mis + logistics
	before = _snapshot_business(job_card, scope)

	result = {
		"repair_run_id": repair_run_id,
		"job_card": job_card,
		"work_order": plan.get("work_order"),
		"mode": "DRY_RUN" if dry_run else "APPLY",
		"dry_run": dry_run,
		"fingerprint": plan["fingerprint"],
		"dispositions": plan.get("dispositions"),
		"merge_documents": merge_docs,
		"merge_material_issues": merge_mis,
		"canonical_manufacture": plan.get("canonical_manufacture"),
		"blockers": [],
		"cancelled": [],
		"created": [],
		"recreated_logistics": [],
		"before_snapshot": before,
	}

	# Outer transaction: rely on rollback/commit explicitly.
	prev_hist_flag = frappe.flags.get(HISTORICAL_REPAIR_FLAG)
	prev_ppo_flag = frappe.flags.get(PREVENTION_FLAG)
	frappe.flags[HISTORICAL_REPAIR_FLAG] = True
	frappe.flags[PREVENTION_FLAG] = True
	try:
		_lock_scope(job_card, scope)
		# Re-validate plan after lock
		fresh = build_manufacture_plan(
			job_card,
			dispositions=plan_input.get("dispositions"),
			merge_documents=plan_input.get("merge_documents"),
			stamp_mode=plan_input.get("stamp_mode"),
			merge_material_issues=plan_input.get("merge_material_issues"),
		)
		if fresh["fingerprint"] != plan["fingerprint"]:
			raise RuntimeError("STALE PLAN after lock")

		with suppress_auto_riv():
			from erpnext_extensions.iran_accounting.job_card_stock_rebuild.shared_logistics import (
				compare_recreated_equivalence,
				snapshot_stock_entry,
			)

			# Full document snapshots + batch rates before cancel
			logistics_batches = {name: _snapshot_se_batches(name) for name in logistics}
			logistics_snaps = {name: snapshot_stock_entry(name) for name in logistics}
			result["logistics_snapshots"] = {
				n: {"header": s["header"], "n_rows": len(s["rows"])} for n, s in logistics_snaps.items()
			}

			fg_keys = set()
			for fr in (fresh.get("scan") or {}).get("rows") or []:
				pass
			# FG keys from plan downstream discovery / canonical FG rows
			for row in (fresh.get("canonical_manufacture") or {}).get("rows") or []:
				if row.get("type") == "MAIN_FG" and row.get("item_code") and row.get("batch_no"):
					fg_keys.add((row["item_code"], row["batch_no"]))
			for d in fresh.get("downstream_logistics_audit") or fresh.get("downstream_logistics") or []:
				for rr in (getattr(d, "related_rows", None) or (d.get("related_rows") if isinstance(d, dict) else None) or []):
					if rr.get("item_code") and rr.get("batch_no"):
						fg_keys.add((rr["item_code"], rr["batch_no"]))

			# 1) Temp cancel logistics reverse chrono (already sorted desc)
			for i, name in enumerate(logistics):
				_cancel_se(name)
				result["cancelled"].append(name)
				if i == 0:
					_fail_point("after_first_cancel")
					_fail_point("after_first_shared_cancel")
				if i == 1:
					_fail_point("after_both_shared_cancel")

			# 2) Cancel proven Material Issues being merged into Manufacture
			for name in merge_mis:
				_cancel_se(name)
				result["cancelled"].append(name)
				_fail_point("after_mi_cancel")

			# 3) Cancel manufactures
			for name in merge_docs:
				_cancel_se(name)
				result["cancelled"].append(name)
			if merge_mis:
				_fail_point("after_mi_and_mfg_cancel")
			_fail_point("after_mfg_cancel")

			# 4) Create + submit canonical (includes MI consumption)
			canonical = _build_canonical_se(fresh)
			result["created"].append(canonical)
			result["canonical_name"] = canonical

			# 5) Recreate logistics in original chrono (reverse of cancel list)
			equivalence = []
			for ri, name in enumerate(reversed(logistics)):
				new_name = _recreate_logistics(name, batch_snapshot=logistics_batches.get(name))
				result["recreated_logistics"].append({"from": name, "to": new_name})
				if ri == 0:
					_fail_point("after_first_shared_recreate")
				eq = compare_recreated_equivalence(
					logistics_snaps[name], new_name, fg_keys=fg_keys
				)
				equivalence.append(eq)
				if not eq.get("ok"):
					_fail_point("during_unrelated_equivalence")
					raise RuntimeError(
						"Shared logistics equivalence failed for "
						f"{name}→{new_name}: " + "; ".join(eq.get("errors") or [])
					)
				if ri == 1:
					_fail_point("after_second_shared_recreate")
			_fail_point("after_logistics_recreate")
			_fail_point("during_unrelated_equivalence")
			result["logistics_equivalence"] = equivalence

			# 6) Sync valuation
			val_vouchers = [canonical] + [x["to"] for x in result["recreated_logistics"]]
			val = sync_valuation_for_vouchers(val_vouchers)
			result["valuation"] = val
			_fail_point("after_valuation")
			if not val.get("ok"):
				raise RuntimeError(val.get("error") or "Sync valuation failed")

			# 7) Verify (MI must remain cancelled only if commit — dry run rolls back)
			verification = _verify(fresh, canonical)
			result["verification"] = verification
			if not verification.get("ok"):
				raise RuntimeError("; ".join(verification.get("errors") or ["verification failed"]))

		result["ok"] = True
		result["status"] = "DRY_RUN_PASS" if dry_run else "APPLY_PASS"
		result["after_snapshot"] = _snapshot_business(
			job_card, scope + [result.get("canonical_name")] + [x["to"] for x in result["recreated_logistics"]]
		)

		if dry_run:
			frappe.db.rollback()
			result["mutated"] = False
			result["committed"] = False
			# Audit after rollback would also roll back — write audit in new txn for dry run
			frappe.db.begin()
			result["audit"] = _write_audit(result)
			frappe.db.commit()
		else:
			result["audit"] = _write_audit(result)
			frappe.db.commit()
			result["mutated"] = True
			result["committed"] = True
		return result
	except Exception as exc:
		frappe.db.rollback()
		result["ok"] = False
		result["status"] = "DRY_RUN_FAIL" if dry_run else "APPLY_FAIL"
		result["error"] = str(exc)
		result["mutated"] = False
		result["committed"] = False
		try:
			frappe.db.begin()
			result["audit"] = _write_audit(result)
			frappe.db.commit()
		except Exception:
			pass
		return result
	finally:
		frappe.flags[HISTORICAL_REPAIR_FLAG] = prev_hist_flag
		frappe.flags[PREVENTION_FLAG] = prev_ppo_flag


def dry_run_manufacture_repair(job_card: str, plan: dict | None = None) -> dict:
	return run_repair(job_card, plan_input=plan, dry_run=True)


def apply_manufacture_repair(job_card: str, plan: dict | None = None, confirm: int | bool = 0) -> dict:
	return run_repair(job_card, plan_input=plan, dry_run=False, confirm=confirm)
