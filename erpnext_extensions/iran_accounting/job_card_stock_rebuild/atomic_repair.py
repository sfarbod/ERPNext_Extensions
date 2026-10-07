# Copyright (c) 2026, ERPNext Extensions contributors
"""Atomic Dry Run / Apply engine for canonical Manufacture repair (v5.5.0)."""

from __future__ import annotations

import json
import time
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
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.repair_stock_guards import (
	ensure_repair_negative_stock_patches,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.workstation_isolation import (
	SKIP_WORKSTATION_WRITES_FLAG,
	ensure_workstation_isolation_patches,
)
from erpnext_extensions.iran_accounting.stock_posting_order.prevention import PREVENTION_FLAG


def _fail_point(name: str):
	if getattr(frappe.flags, "jc_repair_fail_at", None) == name:
		raise RuntimeError(f"INJECTED_FAILURE:{name}")


class _PhaseTimer:
	"""Temporary Dry Run / Apply phase instrumentation (non-secret)."""

	def __init__(self):
		self.t0 = time.perf_counter()
		self.phases: list[dict[str, Any]] = []
		self._open: dict[str, float] = {}

	def start(self, code: str):
		self._open[code] = time.perf_counter()

	def end(self, code: str, **extra):
		start = self._open.pop(code, None)
		now = time.perf_counter()
		if start is None:
			start = now
		row = {
			"phase": code,
			"start": round(start - self.t0, 4),
			"end": round(now - self.t0, 4),
			"elapsed": round(now - start, 4),
			"cumulative": round(now - self.t0, 4),
		}
		if extra:
			row.update(extra)
		self.phases.append(row)
		return row

	def mark(self, code: str, **extra):
		"""Instant marker (zero-width phase)."""
		now = time.perf_counter()
		row = {
			"phase": code,
			"start": round(now - self.t0, 4),
			"end": round(now - self.t0, 4),
			"elapsed": 0.0,
			"cumulative": round(now - self.t0, 4),
		}
		if extra:
			row.update(extra)
		self.phases.append(row)
		return row

	def as_dict(self) -> dict[str, Any]:
		return {
			"total_elapsed": round(time.perf_counter() - self.t0, 4),
			"phases": list(self.phases),
			"slowest": sorted(self.phases, key=lambda r: r.get("elapsed") or 0, reverse=True)[:8],
		}


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
	"""Cancel a submitted Stock Entry via Core ``doc.cancel()``.

	Also stamps the configured Workflow cancellation state. Core skips
	``validate_workflow`` / ``set_workflow_state_on_action`` on cancel
	(``_save`` omits ``_validate`` when ``_action == "cancel"``); interactive
	cancels rely on ``apply_workflow`` to stamp ``next_state`` first. Repair
	mirrors that stamp so ``docstatus=2`` never finishes with a submitted
	``workflow_state``.
	"""
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.workflow_cancel import (
		ensure_cancel_workflow_state_persisted,
		stamp_cancel_workflow_state,
	)

	doc = frappe.get_doc("Stock Entry", name)
	if doc.docstatus != 1:
		return
	doc.flags.ignore_permissions = True
	stamp_cancel_workflow_state(doc)
	doc.cancel()
	ensure_cancel_workflow_state_persisted(doc)


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


def _rate_snapshot_identity(row: dict | Any) -> tuple:
	"""Deterministic SED identity for rate reapply (idx-independent fallback)."""
	get = row.get if isinstance(row, dict) else lambda k, d=None: (
		row.get(k) if hasattr(row, "get") else getattr(row, k, d)
	)
	return (
		get("item_code") or "",
		get("batch_no") or "",
		get("s_warehouse") or "",
		get("t_warehouse") or "",
		cint(get("is_finished_item")),
		(get("custom_output_class") or get("type") or ""),
		get("job_card_item") or "",
	)


def _reapply_snapshot_rates(doc, batch_snapshot: list[dict] | None = None):
	"""Force snapshot rates (Core set_rate_for_outgoing_items overwrites mid-repair)."""
	snaps = batch_snapshot or []
	by_idx = {cint(r["idx"]): r for r in snaps if r.get("idx") is not None}
	# Prefer idx; then unique rich identity; then unique item×batch.
	ident_counts: dict[tuple, int] = {}
	by_ident: dict[tuple, dict] = {}
	batch_counts: dict[tuple[str, str], int] = {}
	by_item_batch: dict[tuple[str, str], dict] = {}
	for r in snaps:
		ikey = _rate_snapshot_identity(r)
		ident_counts[ikey] = ident_counts.get(ikey, 0) + 1
		by_ident[ikey] = r
		key = (r["item_code"], r.get("batch_no") or "")
		batch_counts[key] = batch_counts.get(key, 0) + 1
		by_item_batch[key] = r
	for row in doc.items:
		snap = by_idx.get(cint(row.idx))
		if not snap:
			ikey = _rate_snapshot_identity(
				{
					"item_code": row.item_code,
					"batch_no": row.batch_no,
					"s_warehouse": row.s_warehouse,
					"t_warehouse": row.t_warehouse,
					"is_finished_item": row.is_finished_item,
					"custom_output_class": getattr(row, "custom_output_class", None),
					"type": None,
					"job_card_item": getattr(row, "job_card_item", None),
				}
			)
			if ident_counts.get(ikey) == 1:
				snap = by_ident.get(ikey)
			elif ident_counts.get(ikey, 0) > 1:
				frappe.throw(
					frappe._(
						"AMBIGUOUS_RATE_IDENTITY: {0} / {1} matches multiple rate snapshots on {2}"
					).format(row.item_code, row.batch_no, doc.name or doc.doctype)
				)
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
		# MAIN_FG / finished incoming: do NOT force amount = qty×rate.
		# Historical Manufacture may carry a legitimate integer-rate residual
		# (e.g. 898 IRR) vs the allocatable material pool; recomposing amount
		# from the snapshot rate recreates that residual and then fails R1
		# after the output contract has (or should have) handled it.
		# Source CONSUME and Component Scrap keep issued-rate amount identity.
		is_main_fg = cint(row.is_finished_item) or (
			(getattr(row, "custom_output_class", None) or "") == "MAIN_FG"
		)
		if is_main_fg:
			continue
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

def snapshot_historical_manufacture_residual(mfg_name: str) -> dict | None:
	"""Capture value_difference + SA evidence BEFORE cancel (GL still live)."""
	from erpnext_extensions.iran_accounting.manufacture_output_contract import (
		historical_repair_residual_evidence,
	)

	# Temporary probe doc flags — read live submitted residual.
	probe = frappe._dict(
		flags=frappe._dict(
			jc_manufacture_repair=True,
			jc_repair_historical_mfg=mfg_name,
		)
	)
	# Force HISTORICAL_REPAIR_FLAG already set by run_repair.
	return historical_repair_residual_evidence(probe)


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
	rate_snapshot: list[dict] = []
	for idx, row in enumerate(canon.get("rows") or [], start=1):
		rate = flt(row.get("basic_rate") or row.get("valuation_rate"))
		is_source = (row.get("type") == "CONSUME") or (
			row.get("s_warehouse")
			and not row.get("t_warehouse")
			and not cint(row.get("is_finished_item"))
		)
		if is_source and rate <= 1e-9:
			frappe.throw(
				frappe._(
					"ZERO_RATE: canonical CONSUME {0} / {1} has no authoritative rate "
					"(rate_source={2}) — blocked before submit"
				).format(row.get("item_code"), row.get("batch_no") or "", row.get("rate_source"))
			)
		child = {
			"item_code": row["item_code"],
			"qty": flt(row["qty"]),
			"transfer_qty": flt(row["qty"]),
			"s_warehouse": row.get("s_warehouse"),
			"t_warehouse": row.get("t_warehouse"),
			"batch_no": row.get("batch_no") or None,
			"is_finished_item": cint(row.get("is_finished_item")),
			"basic_rate": rate,
			"valuation_rate": flt(row.get("valuation_rate") or row.get("basic_rate") or rate),
			"secondary_item_type": row.get("secondary_item_type"),
			"use_serial_batch_fields": 1 if row.get("batch_no") else 0,
			"expense_account": adj_account,
			"cost_center": row.get("cost_center") or default_cc,
		}
		if row.get("department"):
			child["department"] = row["department"]
		if rate > 0:
			child["set_basic_rate_manually"] = 1
			child["allow_zero_valuation_rate"] = 0
		if row.get("custom_output_class"):
			child["custom_output_class"] = row["custom_output_class"]
		if (row.get("type") == "COMPONENT_SCRAP") or (
			(row.get("custom_output_class") or "") == "COMPONENT_SCRAP"
		):
			child["is_scrap_item"] = 0
			child["secondary_item_type"] = child.get("secondary_item_type") or "Scrap"
		if row.get("job_card_item"):
			child["job_card_item"] = row["job_card_item"]
		doc.append("items", child)
		rate_snapshot.append(
			{
				"idx": idx,
				"item_code": row["item_code"],
				"batch_no": row.get("batch_no") or "",
				"s_warehouse": row.get("s_warehouse"),
				"t_warehouse": row.get("t_warehouse"),
				"is_finished_item": cint(row.get("is_finished_item")),
				"custom_output_class": row.get("custom_output_class"),
				"type": row.get("type"),
				"job_card_item": row.get("job_card_item"),
				"basic_rate": rate,
				"valuation_rate": flt(row.get("valuation_rate") or rate),
			}
		)

	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.tracking_reconstruction import (
		assert_canonical_jci_complete,
	)

	assert_canonical_jci_complete(canon.get("rows") or [])

	doc.flags.ignore_permissions = True
	# Site Server Script "Custom 9" bumps Manufacture after MTfM; skip via doc.flags.
	doc.flags.jc_manufacture_repair = True
	# Fingerprint the superseded historical Manufacture for residual preservation.
	doc.flags.jc_repair_historical_mfg = template_name
	# Pre-cancel residual snapshot (preferred — SA GL still live at capture time).
	if plan.get("historical_manufacture_residual"):
		doc.flags.jc_repair_historical_residual = plan.get("historical_manufacture_residual")
	# Historical repair must keep the original Manufacture chronology; PPO would
	# otherwise bump posting after remaining WIP Issues / other dependents.
	if hasattr(doc, "set_posting_time"):
		doc.set_posting_time = 1
	# Preserve plan rates through Core validate (same pattern as logistics recreate).
	# Do NOT depend on live get_incoming_rate at backdated Manufacture posting.
	_bind_preserve_rates(doc, batch_snapshot=rate_snapshot)
	doc.insert()
	_reapply_snapshot_rates(doc, batch_snapshot=rate_snapshot)
	# Re-assert posting after validate hooks.
	if canon.get("posting_date"):
		doc.posting_date = canon["posting_date"]
	if canon.get("posting_time"):
		doc.posting_time = canon["posting_time"]
	if hasattr(doc, "set_posting_time"):
		doc.set_posting_time = 1
	doc.flags.jc_manufacture_repair = True
	doc.flags.jc_repair_historical_mfg = template_name
	if plan.get("historical_manufacture_residual"):
		doc.flags.jc_repair_historical_residual = plan.get("historical_manufacture_residual")
	_reapply_snapshot_rates(doc, batch_snapshot=rate_snapshot)
	doc.save()
	_reapply_snapshot_rates(doc, batch_snapshot=rate_snapshot)
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
	offset_keys = {
		(k.get("item_code"), k.get("batch_no") or "")
		for k in (plan.get("approved_batch_offset_keys") or [])
	}
	partial_keys = set()
	for g in plan.get("approved_partial_batch_offsets") or []:
		partial_keys.add((g["item_code"], g.get("positive_batch") or ""))
		partial_keys.add((g["item_code"], g.get("negative_batch") or ""))
	for row in scan["rows"]:
		# Skip non-component noise (defensive; scan already filters FG-only rows).
		if flt(row.get("issued")) <= 1e-9 and flt(row.get("consumed")) <= 1e-9:
			continue
		key = (row["item_code"], row.get("batch_no") or "")
		# Explicit approved batch offset (full or partial): remaining may stay non-zero per batch.
		if key in partial_keys:
			row["repair_status"] = "APPROVED_PARTIAL_BATCH_OFFSET"
			continue
		if key in offset_keys:
			row["repair_status"] = "APPROVED_BATCH_OFFSET"
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
						"batch_offset_approvals": payload.get("batch_offset_approvals"),
						"approved_batch_offsets": payload.get("approved_batch_offsets"),
						"partial_batch_offset_approvals": payload.get(
							"partial_batch_offset_approvals"
						),
						"manufacture_batch_replace_approvals": payload.get(
							"manufacture_batch_replace_approvals"
						),
						"approved_manufacture_batch_replacements": payload.get(
							"approved_manufacture_batch_replacements"
						),
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
	progress_cb=None,
) -> dict[str, Any]:
	"""Same engine for Dry Run and Apply.

	dry_run=True  → always rollback
	dry_run=False → commit once after verification (requires confirm)

	``progress_cb(phase_code, **extra)`` is optional (queued Dry Run UI).
	It must NOT commit the business transaction.
	"""
	if not dry_run and not cint(confirm):
		frappe.throw(frappe._("Confirmation required before Apply."))

	def _progress(code: str, **extra):
		if progress_cb:
			try:
				progress_cb(code, **extra)
			except Exception:
				pass

	timer = _PhaseTimer()
	timer.mark("T00")
	_progress("T00")
	_fail_point("preparing")

	plan_input = plan_input or {}
	timer.start("T01")
	_progress("T01")
	plan = build_manufacture_plan(
		job_card,
		dispositions=plan_input.get("dispositions"),
		merge_documents=plan_input.get("merge_documents"),
		stamp_mode=plan_input.get("stamp_mode"),
		merge_material_issues=plan_input.get("merge_material_issues"),
		batch_offset_approvals=plan_input.get("batch_offset_approvals"),
		partial_batch_offset_approvals=plan_input.get("partial_batch_offset_approvals"),
		manufacture_batch_replace_approvals=plan_input.get(
			"manufacture_batch_replace_approvals"
		),
	)
	timer.end("T01")
	client_fp = plan_input.get("fingerprint")
	if client_fp and client_fp != plan["fingerprint"]:
		out = {
			"ok": False,
			"status": "STALE PLAN",
			"error": "STALE PLAN — re-scan / rebuild plan required",
			"dry_run": dry_run,
			"mutated": False,
			"fingerprint": plan["fingerprint"],
		}
		out["phase_timings"] = timer.as_dict()
		return out
	if plan.get("blockers"):
		out = {
			"ok": False,
			"status": "BLOCKED",
			"blockers": plan["blockers"],
			"error": "; ".join(plan["blockers"]),
			"dry_run": dry_run,
			"mutated": False,
			"plan": plan,
		}
		out["phase_timings"] = timer.as_dict()
		return out

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
		"batch_offset_approvals": plan.get("batch_offset_approvals"),
		"approved_batch_offsets": plan.get("approved_batch_offsets"),
		"approved_batch_offset_keys": plan.get("approved_batch_offset_keys"),
		"batch_offset_preview": plan.get("batch_offset_preview"),
		"partial_batch_offset_approvals": plan.get("partial_batch_offset_approvals"),
		"approved_partial_batch_offsets": plan.get("approved_partial_batch_offsets"),
		"partial_batch_offset_preview": plan.get("partial_batch_offset_preview"),
		"manufacture_batch_replace_approvals": plan.get(
			"manufacture_batch_replace_approvals"
		),
		"approved_manufacture_batch_replacements": plan.get(
			"approved_manufacture_batch_replacements"
		),
		"manufacture_batch_replace_preview": plan.get(
			"manufacture_batch_replace_preview"
		),
		"exception_only": bool(plan.get("exception_only")),
		"blockers": [],
		"cancelled": [],
		"created": [],
		"recreated_logistics": [],
		"before_snapshot": before,
	}

	# Exception-only: validate + audit approved offset with zero stock mutation.
	if plan.get("exception_only"):
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
			scan_golden_rule,
		)

		_progress("T10")
		scan = scan_golden_rule(job_card)
		partial_status_keys = set()
		for g in plan.get("approved_partial_batch_offsets") or []:
			partial_status_keys.add((g["item_code"], g.get("positive_batch") or ""))
			partial_status_keys.add((g["item_code"], g.get("negative_batch") or ""))
		for row in scan.get("rows") or []:
			key = (row["item_code"], row.get("batch_no") or "")
			if key in partial_status_keys:
				row["repair_status"] = "APPROVED_PARTIAL_BATCH_OFFSET"
			elif key in {
				(k.get("item_code"), k.get("batch_no") or "")
				for k in (plan.get("approved_batch_offset_keys") or [])
			}:
				row["repair_status"] = "APPROVED_BATCH_OFFSET"
		exc_status = (
			"APPROVED_PARTIAL_BATCH_OFFSET"
			if plan.get("approved_partial_batch_offsets")
			else "APPROVED_BATCH_OFFSET"
		)
		result.update(
			{
				"ok": True,
				"status": "DRY_RUN_PASS" if dry_run else "APPLY_PASS",
				"mutated": False,
				"committed": False,
				"verification": {
					"ok": True,
					"errors": [],
					"scan": scan,
					"approved_batch_offsets": plan.get("approved_batch_offsets"),
					"approved_partial_batch_offsets": plan.get(
						"approved_partial_batch_offsets"
					),
					"exception_status": exc_status,
				},
				"after_snapshot": before,
			}
		)
		if dry_run:
			frappe.db.rollback()
			result["committed"] = False
			result["mutated"] = False
			# Persist audit outside rolled-back business txn (same as stock dry-run).
			result["audit"] = _write_audit(result)
			frappe.db.commit()
		else:
			result["audit"] = _write_audit(result)
			frappe.db.commit()
			result["committed"] = True
		result["phase_timings"] = timer.as_dict()
		return result

	# Outer transaction: rely on rollback/commit explicitly.
	# Workstation status writes from Core Job Card side-effects are NOT part of
	# stock reconciliation truth — isolate them so concurrent Production
	# Workstation updates cannot raise MariaDB 1020 / overwrite live status.
	prev_hist_flag = frappe.flags.get(HISTORICAL_REPAIR_FLAG)
	prev_ppo_flag = frappe.flags.get(PREVENTION_FLAG)
	prev_ws_skip = frappe.flags.get(SKIP_WORKSTATION_WRITES_FLAG)
	ensure_workstation_isolation_patches()
	ensure_repair_negative_stock_patches()
	frappe.flags[HISTORICAL_REPAIR_FLAG] = True
	frappe.flags[PREVENTION_FLAG] = True
	frappe.flags[SKIP_WORKSTATION_WRITES_FLAG] = True
	try:
		timer.start("T02")
		_progress("T02")
		_lock_scope(job_card, scope)
		timer.end("T02")
		_fail_point("after_locking")
		# Re-validate plan after lock
		timer.start("T03")
		_progress("T03")
		fresh = build_manufacture_plan(
			job_card,
			dispositions=plan_input.get("dispositions"),
			merge_documents=plan_input.get("merge_documents"),
			stamp_mode=plan_input.get("stamp_mode"),
			merge_material_issues=plan_input.get("merge_material_issues"),
			batch_offset_approvals=plan_input.get("batch_offset_approvals"),
			partial_batch_offset_approvals=plan_input.get(
				"partial_batch_offset_approvals"
			),
			manufacture_batch_replace_approvals=plan_input.get(
				"manufacture_batch_replace_approvals"
			),
		)
		if fresh["fingerprint"] != plan["fingerprint"]:
			raise RuntimeError("STALE PLAN after lock")
		timer.end("T03")

		with suppress_auto_riv():
			from erpnext_extensions.iran_accounting.job_card_stock_rebuild.shared_logistics import (
				compare_recreated_equivalence,
				snapshot_stock_entry,
			)
			from erpnext_extensions.iran_accounting.job_card_stock_rebuild.temporary_stock_bridge import (
				cancel_temp_receipt,
				create_and_submit_temp_receipt,
				delete_temp_receipt,
				plan_temporary_bridge,
				verify_temp_absent,
			)
			from erpnext_extensions.iran_accounting.job_card_stock_rebuild.tracking_reconstruction import (
				apply_job_card_tracking,
				apply_sed_backfills,
			)

			# Full document snapshots + batch rates before cancel
			timer.start("T04")
			_progress("T04")
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
			timer.end("T04", logistics=len(logistics), fg_keys=len(fg_keys))

			# 0a) Job Card tracking reconstruction (metadata) — before cancel/submit
			# so Custom 2/6 see linked MTfM and Core-compatible transferred_qty.
			tracking = fresh.get("tracking_repair") or {}
			result["tracking_repair"] = {
				"components": tracking.get("components") or [],
				"sed_counts": (tracking.get("sed_linkage") or {}).get("counts") or {},
			}
			_fail_point("after_jci_map")
			timer.start("T04b")
			_progress("T04b")
			sed_applied = apply_sed_backfills(
				(tracking.get("sed_linkage") or {}).get("safe_backfills") or []
			)
			result["tracking_repair"]["sed_backfilled"] = sed_applied
			timer.end("T04b", n=len(sed_applied))
			_fail_point("after_sed_backfill")

			timer.start("T04c")
			_progress("T04c")
			# transferred = issued−returned; consumed left until post-cancel pre_submit write
			xfer_applied = apply_job_card_tracking(
				job_card,
				tracking.get("components") or [],
				phase="pre_submit",
			)
			# First pass only stamps transferred/customs; consumed=0 is correct after we
			# cancel merged manufactures below — re-assert then.
			result["tracking_repair"]["transferred_updates"] = xfer_applied
			timer.end("T04c", n=len(xfer_applied))
			_fail_point("after_transferred_update")

			# 0) Temporary Material Receipt bridge (exact cancel shortages only)
			timer.start("T05")
			_progress("T05")
			bridge_plan = fresh.get("temporary_bridge") or plan_temporary_bridge(logistics)
			result["temporary_bridge"] = {
				"required": bool(bridge_plan.get("required")),
				"shortages": bridge_plan.get("shortages") or [],
			}
			timer.end("T05", shortages=len(bridge_plan.get("shortages") or []))
			temp_name = ""
			if bridge_plan.get("required") and bridge_plan.get("shortages"):
				timer.start("T06_T07")
				_progress("T06_T07")
				temp_name = create_and_submit_temp_receipt(bridge_plan, repair_run_id)
				timer.end("T06_T07", temp_name=temp_name)
				result["temporary_receipt"] = temp_name
				result["created"].append(temp_name)
				# Invalidate cancel-probe cache — stock changed.
				frappe.flags.jc_shared_cancel_probe_cache = {}
				_fail_point("after_temp_receipt_submit")

			# 1) Temp cancel logistics reverse chrono (already sorted desc)
			for i, name in enumerate(logistics):
				phase = "T08" if i == 0 else ("T09" if i == 1 else f"T08x{i}")
				timer.start(phase)
				_progress("T08" if i == 0 else "T09")
				_cancel_se(name)
				timer.end(phase, voucher=name)
				result["cancelled"].append(name)
				if i == 0:
					_fail_point("after_first_cancel")
					_fail_point("after_first_shared_cancel")
					_fail_point("after_31726_cancel")
				if i == 1:
					_fail_point("after_both_shared_cancel")
					_fail_point("after_31725_cancel")

			# 2) Cancel proven Material Issues being merged into Manufacture
			for name in merge_mis:
				timer.start("T10_MI")
				_cancel_se(name)
				timer.end("T10_MI", voucher=name)
				result["cancelled"].append(name)
				_fail_point("after_mi_cancel")

			# 3) Cancel manufactures
			timer.start("T10")
			_progress("T10")
			# Snapshot historical residual BEFORE cancel (live SA GL).
			if merge_docs and not fresh.get("historical_manufacture_residual"):
				fresh["historical_manufacture_residual"] = (
					snapshot_historical_manufacture_residual(merge_docs[0])
				)
				result["historical_manufacture_residual"] = fresh.get(
					"historical_manufacture_residual"
				)
			for name in merge_docs:
				_cancel_se(name)
				result["cancelled"].append(name)
			timer.end("T10", count=len(merge_docs))
			if merge_mis:
				_fail_point("after_mi_and_mfg_cancel")
			_fail_point("after_mfg_cancel")

			# 3b) Re-assert pre-submit tracking after cancel (consumed=0; transferred net)
			timer.start("T10b")
			_progress("T10b")
			cons_applied = apply_job_card_tracking(
				job_card,
				tracking.get("components") or [],
				phase="pre_submit",
			)
			result["tracking_repair"]["consumed_updates"] = cons_applied
			timer.end("T10b", n=len(cons_applied))
			_fail_point("after_consumed_update")

			# 4) Create + submit canonical (includes MI consumption)
			timer.start("T11_T12")
			_progress("T11_T12")
			canonical = _build_canonical_se(fresh)
			timer.end("T11_T12", canonical=canonical)
			result["created"].append(canonical)
			result["canonical_name"] = canonical
			_fail_point("after_canonical")
			_fail_point("after_canonical_submit")

			# 5) Recreate logistics in original chrono (reverse of cancel list)
			equivalence = []
			for ri, name in enumerate(reversed(logistics)):
				phase = "T13" if ri == 0 else ("T14" if ri == 1 else f"T13x{ri}")
				timer.start(phase)
				_progress("T13" if ri == 0 else "T14")
				new_name = _recreate_logistics(name, batch_snapshot=logistics_batches.get(name))
				result["recreated_logistics"].append({"from": name, "to": new_name})
				if ri == 0:
					_fail_point("after_first_shared_recreate")
					_fail_point("after_31725_recreate")
				eq = compare_recreated_equivalence(
					logistics_snaps[name], new_name, fg_keys=fg_keys
				)
				equivalence.append(eq)
				timer.end(phase, from_voucher=name, to_voucher=new_name, eq_ok=bool(eq.get("ok")))
				if not eq.get("ok"):
					_fail_point("during_unrelated_equivalence")
					raise RuntimeError(
						"Shared logistics equivalence failed for "
						f"{name}→{new_name}: " + "; ".join(eq.get("errors") or [])
					)
				if ri == 1:
					_fail_point("after_second_shared_recreate")
					_fail_point("after_31726_recreate")
			_fail_point("after_logistics_recreate")
			_fail_point("during_unrelated_equivalence")
			result["logistics_equivalence"] = equivalence

			# 6) Cancel + delete temporary receipt (must not survive repair)
			if temp_name:
				timer.start("T15")
				cancel_temp_receipt(temp_name)
				timer.end("T15", temp_name=temp_name)
				_fail_point("after_temp_receipt_cancel")
				timer.start("T16")
				del_res = delete_temp_receipt(temp_name)
				result["temporary_receipt_delete"] = del_res
				timer.end("T16", deleted=bool(del_res.get("ok") if isinstance(del_res, dict) else del_res))
				_fail_point("after_temp_receipt_delete")
				absent = verify_temp_absent(temp_name)
				result["temporary_receipt_absent"] = absent
				if not absent.get("ok"):
					raise RuntimeError(
						"Temp receipt cleanup failed: " + "; ".join(absent.get("errors") or [])
					)
				# Remove from created list — document no longer exists
				result["created"] = [n for n in result["created"] if n != temp_name]
				result["temporary_receipt"] = None

			# 7) Sync valuation
			timer.start("T17")
			_progress("T17")
			val_vouchers = [canonical] + [x["to"] for x in result["recreated_logistics"]]
			val = sync_valuation_for_vouchers(val_vouchers, progress_cb=progress_cb)
			result["valuation"] = val
			timer.end(
				"T17",
				pairs=val.get("count"),
				future_sle_total=val.get("future_sle_total"),
				val_elapsed=val.get("elapsed"),
			)
			_fail_point("after_valuation")
			if not val.get("ok"):
				raise RuntimeError(val.get("error") or "Sync valuation failed")

			# 8) Verify (MI must remain cancelled only if commit — dry run rolls back)
			timer.start("T18_T22")
			_progress("T18_T22")
			_fail_point("during_verify")
			verification = _verify(fresh, canonical)
			result["verification"] = verification
			timer.end("T18_T22", verify_ok=bool(verification.get("ok")))
			if not verification.get("ok"):
				raise RuntimeError("; ".join(verification.get("errors") or ["verification failed"]))

		result["ok"] = True
		result["status"] = "DRY_RUN_PASS" if dry_run else "APPLY_PASS"
		result["after_snapshot"] = _snapshot_business(
			job_card, scope + [result.get("canonical_name")] + [x["to"] for x in result["recreated_logistics"]]
		)

		if dry_run:
			_fail_point("before_rollback")
			timer.start("T23")
			_progress("T23")
			frappe.db.rollback()
			timer.end("T23")
			result["mutated"] = False
			result["committed"] = False
			# Audit after rollback would also roll back — write audit in new txn for dry run
			timer.start("T24")
			_progress("T24")
			frappe.db.begin()
			result["audit"] = _write_audit(result)
			frappe.db.commit()
			timer.end("T24")
		else:
			result["audit"] = _write_audit(result)
			_fail_point("before_commit")
			frappe.db.commit()
			result["mutated"] = True
			result["committed"] = True
		result["phase_timings"] = timer.as_dict()
		return result
	except Exception as exc:
		timer.start("T23")
		frappe.db.rollback()
		timer.end("T23", error=True)
		result["ok"] = False
		result["status"] = "DRY_RUN_FAIL" if dry_run else "APPLY_FAIL"
		result["error"] = str(exc)
		result["mutated"] = False
		result["committed"] = False
		try:
			timer.start("T24")
			frappe.db.begin()
			result["audit"] = _write_audit(result)
			frappe.db.commit()
			timer.end("T24")
		except Exception:
			pass
		result["phase_timings"] = timer.as_dict()
		return result
	finally:
		frappe.flags[HISTORICAL_REPAIR_FLAG] = prev_hist_flag
		frappe.flags[PREVENTION_FLAG] = prev_ppo_flag
		frappe.flags[SKIP_WORKSTATION_WRITES_FLAG] = prev_ws_skip


def dry_run_manufacture_repair(job_card: str, plan: dict | None = None) -> dict:
	return run_repair(job_card, plan_input=plan, dry_run=True)


def apply_manufacture_repair(job_card: str, plan: dict | None = None, confirm: int | bool = 0) -> dict:
	return run_repair(job_card, plan_input=plan, dry_run=False, confirm=confirm)
