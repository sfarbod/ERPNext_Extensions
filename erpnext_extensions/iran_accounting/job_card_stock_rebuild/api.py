# Copyright (c) 2026, ERPNext Extensions contributors
"""Whitelisted APIs for Job Card Stock Rebuild. System Manager only."""

from __future__ import annotations

import json

import frappe

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.service import (
	apply_rebuild,
	dry_run_rebuild,
	preview_rebuild,
	scan_job_card,
	scan_work_order_readonly,
)


def _guard():
	frappe.only_for("System Manager")


def _approvals(secondary_type_approvals):
	if secondary_type_approvals is None or secondary_type_approvals == "":
		return None
	if isinstance(secondary_type_approvals, str):
		return json.loads(secondary_type_approvals)
	return secondary_type_approvals


@frappe.whitelist()
def scan(job_card: str, item: str | None = None, batch: str | None = None):
	_guard()
	return scan_job_card(job_card, item_filter=item or None, batch_filter=batch or None)


@frappe.whitelist()
def preview(job_card: str, item: str | None = None, batch: str | None = None):
	_guard()
	return preview_rebuild(job_card, item_filter=item or None, batch_filter=batch or None)


@frappe.whitelist()
def dry_run(
	job_card: str,
	fingerprint: str | None = None,
	item: str | None = None,
	batch: str | None = None,
	secondary_type_approvals=None,
):
	_guard()
	return dry_run_rebuild(
		job_card,
		fingerprint=fingerprint or None,
		item_filter=item or None,
		batch_filter=batch or None,
		secondary_type_approvals=_approvals(secondary_type_approvals),
	)


@frappe.whitelist()
def apply(
	job_card: str,
	fingerprint: str,
	confirm: int | bool = 0,
	item: str | None = None,
	batch: str | None = None,
	secondary_type_approvals=None,
):
	_guard()
	return apply_rebuild(
		job_card,
		fingerprint=fingerprint,
		confirm=confirm,
		item_filter=item or None,
		batch_filter=batch or None,
		secondary_type_approvals=_approvals(secondary_type_approvals),
	)


@frappe.whitelist()
def scan_work_order(work_order: str):
	_guard()
	return scan_work_order_readonly(work_order)


def _parse_plan(plan):
	if plan is None or plan == "":
		return {}
	if isinstance(plan, str):
		return json.loads(plan)
	return dict(plan)


@frappe.whitelist()
def scan_manufacture_reconciliation(job_card: str):
	"""v5.5.0 — Golden Rule scan + document discovery."""
	_guard()
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import (
		scan_golden_rule,
	)
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
		build_manufacture_plan,
	)

	scan = scan_golden_rule(job_card)
	# Default plan preview using suggested dispositions
	dispositions = []
	for r in scan.get("rows") or []:
		dispositions.append(
			{
				"item_code": r["item_code"],
				"batch_no": r.get("batch_no") or "",
				"proposed_consumed": r.get("proposed_consumed") or 0,
				"proposed_scrap": r.get("proposed_scrap") or 0,
				"proposed_return": r.get("proposed_return") or 0,
				"proposed_still_in_wip": r.get("proposed_still_in_wip") or 0,
				"disposition": r.get("suggested_action"),
			}
		)
	plan = build_manufacture_plan(job_card, dispositions=dispositions)
	return {"scan": scan, "plan": plan}


@frappe.whitelist()
def dry_run_manufacture_repair(job_card: str, plan=None):
	"""Legacy synchronous Dry Run (kept for direct/engine tests).

	Desk UI must use ``start_manufacture_repair_dry_run`` (queued).
	"""
	_guard()
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import (
		dry_run_manufacture_repair as _dry,
	)

	return _dry(job_card, plan=_parse_plan(plan))


@frappe.whitelist()
def start_manufacture_repair_dry_run(job_card: str, plan=None):
	"""v5.5.3/5.5.4 — enqueue ONE background Dry Run; return immediately."""
	_guard()
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.queued_repair import (
		start_manufacture_repair_dry_run as _start,
	)

	return _start(job_card, plan=_parse_plan(plan))


@frappe.whitelist()
def get_manufacture_repair_dry_run_status(run_id: str):
	"""v5.5.3/5.5.4 — lightweight status poll for queued Dry Run."""
	_guard()
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.queued_repair import (
		get_manufacture_repair_dry_run_status as _status,
	)

	return _status(run_id)


@frappe.whitelist()
def get_active_manufacture_repair_dry_run(job_card: str):
	"""v5.5.3/5.5.4 — reconnect helper after page refresh (any active repair)."""
	_guard()
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.queued_repair import (
		get_active_repair,
	)

	active = get_active_repair(job_card)
	if not active:
		return {"ok": True, "active": False, "job_card": job_card}
	return {"ok": True, "active": True, **active}


@frappe.whitelist()
def start_manufacture_repair_apply(job_card: str, plan=None, confirm: int | bool = 0):
	"""v5.5.4 — enqueue ONE background Apply; return immediately."""
	_guard()
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.queued_repair import (
		start_manufacture_repair_apply as _start,
	)

	return _start(job_card, plan=_parse_plan(plan), confirm=confirm)


@frappe.whitelist()
def get_manufacture_repair_apply_status(run_id: str):
	"""v5.5.4 — lightweight status poll for queued Apply."""
	_guard()
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.queued_repair import (
		get_manufacture_repair_apply_status as _status,
	)

	return _status(run_id)


@frappe.whitelist()
def get_active_manufacture_repair_apply(job_card: str):
	"""v5.5.4 — reconnect helper for active Apply (or any active repair)."""
	_guard()
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.queued_repair import (
		get_active_repair,
	)

	active = get_active_repair(job_card)
	if not active:
		return {"ok": True, "active": False, "job_card": job_card}
	return {"ok": True, "active": True, **active}


@frappe.whitelist()
def apply_manufacture_repair(job_card: str, plan=None, confirm: int | bool = 0):
	"""Legacy synchronous Apply (engine/tests). Desk UI must use Start Apply.

	Blocked while any queued Dry Run/Apply is active for the Job Card.
	"""
	_guard()
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.queued_repair import (
		repair_in_progress,
	)

	if repair_in_progress(job_card):
		frappe.throw(
			frappe._("REPAIR_IN_PROGRESS — wait for the queued repair to finish"),
			title=frappe._("Repair in progress"),
		)
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import (
		apply_manufacture_repair as _apply,
	)

	return _apply(job_card, plan=_parse_plan(plan), confirm=confirm)
