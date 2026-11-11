# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Orchestration engine for Monthly FG Cost Calculation."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint, flt, get_datetime, now_datetime

from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision
from erpnext_extensions.iran_accounting.fg_cost_allocation.allocate import (
	allocate_pools_to_batches,
	allocate_products,
	build_batches,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.gl_extract import (
	extract_pools_for_rules,
	load_enabled_rules,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.normalize import apply_normalization
from erpnext_extensions.iran_accounting.fg_cost_allocation.period import resolve_persian_month
from erpnext_extensions.iran_accounting.fg_cost_allocation.persist import (
	clear_details,
	persist_all,
	write_revision_log,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.production_extract import (
	consolidate_products,
	fetch_eligible_fg_rows,
)
from erpnext_extensions.iran_accounting.fg_cost_allocation.reconcile import reconcile


def _settings_for_company(company: str):
	name = frappe.db.get_value("Finished Goods Cost Allocation Settings", {"company": company}, "name")
	if not name:
		frappe.throw(_("Finished Goods Cost Allocation Settings not found for {0}.").format(company))
	doc = frappe.get_doc("Finished Goods Cost Allocation Settings", name)
	if not cint(doc.enabled):
		frappe.throw(_("Finished Goods Cost Allocation Settings for {0} are disabled.").format(company))
	if not doc.normalization_uom:
		frappe.throw(_("Normalization UOM is required on Finished Goods Cost Allocation Settings."))
	return doc


def _load_override_map(monthly_name: str, revision: int) -> dict[str, Decimal]:
	rows = frappe.get_all(
		"MFG Cost Batch Allocation",
		filters={
			"monthly_calculation": monthly_name,
			"revision": cint(revision),
			"is_manual_override": 1,
		},
		fields=["batch_no", "manual_share", "applied_share"],
	)
	out: dict[str, Decimal] = {}
	for r in rows:
		if r.manual_share is not None:
			out[r.batch_no] = Decimal(str(flt(r.manual_share)))
		elif r.applied_share is not None:
			out[r.batch_no] = Decimal(str(flt(r.applied_share)))
	return out


def _fingerprint(pools, products, period) -> str:
	payload = {
		"period": [str(period.start_date), str(period.end_date)],
		"pools": [
			{
				"rule": p.settings_rule,
				"account": p.account,
				"cc": p.cost_center,
				"dept": p.department,
				"net": str(p.monthly_net_expense),
				"n": p.gl_entry_count,
				"lines": [l.gl_entry for l in p.lines],
			}
			for p in pools
		],
		"products": [
			{
				"batch": p.batch_no,
				"item": p.item_code,
				"qty": str(p.actual_qty),
				"norm": str(p.normalized_qty),
				"sed": sorted(p.stock_entry_details),
			}
			for p in sorted(products.values(), key=lambda x: (x.batch_no, x.item_code))
		],
	}
	raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def run_calculation(monthly_name: str, *, retain_overrides: bool | None = None, action: str = "Calculate"):
	"""Atomically (re)calculate a Monthly FG Cost Calculation document."""
	doc = frappe.get_doc("Monthly FG Cost Calculation", monthly_name)
	doc.check_permission("write")

	if cint(doc.is_consumed):
		frappe.throw(_("Cannot recalculate a consumed Monthly FG Cost Calculation."))
	if doc.status == "Locked" and action != "Unlock":
		frappe.throw(_("Calculation is Locked. Unlock before recalculating."))

	settings = _settings_for_company(doc.company)
	period = resolve_persian_month(doc.company, cint(doc.persian_year), cint(doc.persian_month))

	previous_status = doc.status
	previous_revision = cint(doc.revision)
	new_revision = previous_revision + 1

	if retain_overrides is None:
		retain_overrides = bool(cint(doc.retain_manual_overrides_default))

	overrides: dict[str, Decimal] = {}
	if retain_overrides and previous_revision > 0:
		overrides = _load_override_map(doc.name, previous_revision)

	currency = doc.currency or frappe.get_cached_value("Company", doc.company, "default_currency")
	share_precision = cint(settings.share_precision) or 8

	# Fill period fields on parent
	doc.start_date = period.start_date
	doc.end_date = period.end_date
	doc.persian_month_name = period.persian_month_name
	doc.period_key = period.period_key
	doc.currency = currency

	try:
		# Clear old details after capturing overrides
		if previous_revision > 0 and not cint(doc.archive_prior_revision_details):
			clear_details(doc.name)  # all revisions for this monthly
		elif previous_revision > 0:
			clear_details(doc.name, previous_revision)

		rules = load_enabled_rules(settings.name)
		pools, excluded = extract_pools_for_rules(doc.company, period, rules)

		rows = fetch_eligible_fg_rows(doc.company, period)
		products, prod_exceptions = consolidate_products(rows)
		norm_exceptions = apply_normalization(products, settings.normalization_uom)
		exceptions = prod_exceptions + norm_exceptions

		batches, month_norm, share_variance = build_batches(
			products, share_precision=share_precision, overrides=overrides
		)
		batch_expenses = allocate_pools_to_batches(pools, batches, currency=currency)
		product_rows, product_expenses = allocate_products(
			batches, batch_expenses, currency=currency, share_precision=share_precision
		)

		eps = Decimal("1").scaleb(-max(share_precision, 6))
		share_ok = abs(share_variance) <= eps * Decimal(max(len(batches), 1)) or month_norm == 0
		blocking = list(exceptions)
		if not share_ok and month_norm > 0:
			blocking.append(f"Applied share variance {share_variance} (must be 0 within tolerance)")

		recon = reconcile(
			pools=pools,
			batches=batches,
			batch_expenses=batch_expenses,
			product_rows=product_rows,
			product_expenses=product_expenses,
			month_norm=month_norm,
			share_variance=share_variance,
			share_precision=share_precision,
			currency=currency,
			blocking_exceptions=blocking,
		)

		persist_all(
			monthly_name=doc.name,
			revision=new_revision,
			period=period,
			currency=currency,
			pools=pools,
			batches=batches,
			batch_expenses=batch_expenses,
			product_rows=product_rows,
			product_expenses=product_expenses,
		)

		total_identified = sum((p.monthly_net_expense for p in pools), Decimal("0"))
		total_allocated = sum((b.total_allocated_expense for b in batches), Decimal("0"))
		fingerprint = _fingerprint(pools, products, period)

		is_consistent = 1 if recon.ok and share_ok and not exceptions else 0
		if action == "Recalculate" or previous_revision > 0:
			status = "Recalculated" if is_consistent else "Incomplete"
		else:
			status = "Calculated" if is_consistent else "Incomplete"

		# Incomplete if no production but expenses exist → unallocated
		unallocated = total_identified - total_allocated

		doc.revision = new_revision
		doc.status = status
		doc.is_consistent = is_consistent
		doc.calculated_on = now_datetime()
		doc.calculated_by = frappe.session.user
		doc.source_fingerprint = fingerprint
		doc.total_identified_expenses = float(total_identified)
		doc.total_allocated_expenses = float(total_allocated)
		doc.unallocated_amount = float(unallocated)
		doc.excluded_gl_amount = float(excluded)
		doc.total_monthly_normalized_qty = float(month_norm)
		doc.gl_entry_count = sum(p.gl_entry_count for p in pools)
		doc.batch_count = len(batches)
		doc.product_count = len(product_rows)
		doc.rule_count = len(rules)
		doc.pool_count = len(pools)
		doc.exception_count = len(exceptions)
		doc.share_variance = float(share_variance)
		doc.reconciliation_summary = json.dumps(
			{"ok": recon.ok, "checks": recon.checks, "messages": recon.messages},
			ensure_ascii=False,
			default=str,
		)
		doc.exceptions_json = json.dumps(exceptions, ensure_ascii=False)
		doc.save(ignore_permissions=True)

		totals = {
			"total_identified_expenses": str(total_identified),
			"total_allocated_expenses": str(total_allocated),
			"unallocated_amount": str(unallocated),
			"excluded_gl_amount": str(excluded),
			"total_monthly_normalized_qty": str(month_norm),
			"batch_count": len(batches),
			"product_count": len(product_rows),
			"gl_entry_count": doc.gl_entry_count,
		}
		write_revision_log(
			monthly_name=doc.name,
			revision=new_revision,
			action=action,
			previous_status=previous_status,
			new_status=status,
			source_fingerprint=fingerprint,
			totals=totals,
			override_map={k: str(v) for k, v in overrides.items()},
			reconciliation={"ok": recon.ok, "messages": recon.messages},
			retain_overrides=bool(retain_overrides),
		)

		frappe.db.commit()
		return {
			"name": doc.name,
			"revision": new_revision,
			"status": status,
			"is_consistent": is_consistent,
			"totals": totals,
			"exception_count": len(exceptions),
			"share_variance": float(share_variance),
		}
	except Exception:
		frappe.db.rollback()
		raise


@frappe.whitelist()
def calculate_monthly(name: str, retain_overrides: int | None = None):
	doc = frappe.get_doc("Monthly FG Cost Calculation", name)
	action = "Recalculate" if cint(doc.revision) > 0 else "Calculate"
	retain = None if retain_overrides is None else bool(cint(retain_overrides))
	return run_calculation(name, retain_overrides=retain, action=action)


@frappe.whitelist()
def set_batch_manual_share(batch_allocation: str, manual_share: float | None = None, clear: int = 0):
	"""Update manual share on a batch row and mark parent Incomplete until recalc."""
	row = frappe.get_doc("MFG Cost Batch Allocation", batch_allocation)
	row.check_permission("write")
	monthly = frappe.get_doc("Monthly FG Cost Calculation", row.monthly_calculation)
	if monthly.status == "Locked" or cint(monthly.is_consumed):
		frappe.throw(_("Cannot edit shares on Locked/consumed calculation."))

	if cint(clear):
		row.is_manual_override = 0
		row.manual_share = None
		row.applied_share = row.calculated_share
	else:
		row.is_manual_override = 1
		row.manual_share = flt(manual_share)
		row.applied_share = flt(manual_share)
	row.save(ignore_permissions=True)

	# Parent becomes Incomplete until recalculate reapplies pool allocations
	monthly.status = "Incomplete"
	monthly.is_consistent = 0
	monthly.save(ignore_permissions=True)
	frappe.db.commit()
	return {"batch_allocation": row.name, "applied_share": row.applied_share}


@frappe.whitelist()
def lock_monthly(name: str, lock: int = 1):
	doc = frappe.get_doc("Monthly FG Cost Calculation", name)
	doc.check_permission("write")
	if cint(lock):
		if not cint(doc.is_consistent) or doc.status == "Incomplete":
			frappe.throw(_("Only consistent Calculated/Recalculated documents can be Locked."))
		doc.status = "Locked"
	else:
		if cint(doc.is_consumed):
			frappe.throw(_("Cannot unlock a consumed calculation."))
		doc.status = "Recalculated" if cint(doc.revision) > 1 else "Calculated"
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def delete_monthly(name: str):
	doc = frappe.get_doc("Monthly FG Cost Calculation", name)
	doc.check_permission("delete")
	if cint(doc.is_consumed):
		frappe.throw(_("Cannot delete a consumed Monthly FG Cost Calculation."))
	clear_details(doc.name)
	# Keep revision logs for audit? Design: optional. Keep them with orphan monthly link cleared.
	frappe.delete_doc("Monthly FG Cost Calculation", name, force=1)
	frappe.db.commit()
	return {"deleted": name}
