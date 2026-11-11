# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Orchestration engine for Monthly FG Cost Calculation."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

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
	# Compact fingerprint: avoid embedding every GL name (packet/version bloat).
	pool_digest = hashlib.sha256(
		"|".join(
			f"{p.settings_rule}:{p.account}:{p.cost_center}:{p.department}:{p.monthly_net_expense}:{p.gl_entry_count}"
			for p in pools
		).encode("utf-8")
	).hexdigest()
	prod_digest = hashlib.sha256(
		"|".join(
			f"{p.batch_no}:{p.item_code}:{p.actual_qty}:{p.normalized_qty}:{','.join(sorted(p.stock_entry_details))}"
			for p in sorted(products.values(), key=lambda x: (x.batch_no, x.item_code))
		).encode("utf-8")
	).hexdigest()
	payload = {
		"period": [str(period.start_date), str(period.end_date)],
		"pools": pool_digest,
		"products": prod_digest,
	}
	raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def run_calculation(monthly_name: str, *, retain_overrides: bool | None = None, action: str = "Calculate"):
	"""Atomically (re)calculate. Failed runs leave the previous revision intact."""
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

	doc.start_date = period.start_date
	doc.end_date = period.end_date
	doc.persian_month_name = period.persian_month_name
	doc.period_key = period.period_key
	doc.currency = currency

	try:
		# Persist NEW revision first; only clear previous on success
		rules = load_enabled_rules(settings.name)
		pools, excluded = extract_pools_for_rules(doc.company, period, rules)

		rows = fetch_eligible_fg_rows(doc.company, period)
		products, prod_exceptions = consolidate_products(rows)
		norm_exceptions = apply_normalization(products, settings)
		exceptions = prod_exceptions + norm_exceptions
		uom_complete = not any(p.missing_uom_factor for p in products.values()) and not norm_exceptions

		batches, month_norm, share_variance = build_batches(
			products,
			share_precision=share_precision,
			overrides=overrides,
			finalize_share_sum=uom_complete,
		)
		batch_expenses = allocate_pools_to_batches(pools, batches, currency=currency)
		product_rows, product_expenses = allocate_products(
			batches, batch_expenses, currency=currency, share_precision=share_precision
		)

		eps = Decimal("1").scaleb(-max(share_precision, 6))
		share_ok = (
			uom_complete
			and (abs(share_variance) <= eps * Decimal(max(len(batches), 1)) or month_norm == 0)
		)
		blocking = list(exceptions)
		if not uom_complete:
			blocking.append(
				"UOM incomplete: batch shares are provisional; month-wide denominator is incomplete"
			)
		if uom_complete and not share_ok and month_norm > 0:
			blocking.append(f"Applied share variance {share_variance} (must be 0 within tolerance)")
		if not rules:
			blocking.append("No enabled expense dimension rules configured")

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
			uom_complete=uom_complete,
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

		is_consistent = 1 if recon.ok and share_ok and uom_complete and rules and not exceptions else 0
		if action == "Recalculate" or previous_revision > 0:
			status = "Recalculated" if is_consistent else "Incomplete"
		else:
			status = "Calculated" if is_consistent else "Incomplete"

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
		if hasattr(doc, "shares_are_provisional"):
			doc.shares_are_provisional = 0 if uom_complete else 1
		if hasattr(doc, "eligible_source_row_count"):
			doc.eligible_source_row_count = len(rows)
		doc.reconciliation_summary = json.dumps(
			{
				"ok": recon.ok,
				"uom_complete": uom_complete,
				"shares_are_provisional": not uom_complete,
				"checks": recon.checks,
				"messages": recon.messages,
			},
			ensure_ascii=False,
			default=str,
		)
		doc.exceptions_json = json.dumps(exceptions, ensure_ascii=False)
		# ignore_version must be passed to save(); _save overwrites flags.ignore_version
		doc.save(ignore_permissions=True, ignore_version=True)

		totals = {
			"total_identified_expenses": str(total_identified),
			"total_allocated_expenses": str(total_allocated),
			"unallocated_amount": str(unallocated),
			"excluded_gl_amount": str(excluded),
			"total_monthly_normalized_qty": str(month_norm),
			"batch_count": len(batches),
			"product_count": len(product_rows),
			"gl_entry_count": doc.gl_entry_count,
			"eligible_source_row_count": len(rows),
			"uom_complete": uom_complete,
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
			reconciliation={"ok": recon.ok, "messages": recon.messages, "uom_complete": uom_complete},
			retain_overrides=bool(retain_overrides),
		)

		# Commit new revision first so a failed prior-revision cleanup cannot roll it back.
		frappe.db.commit()

		if previous_revision > 0 and not cint(doc.archive_prior_revision_details):
			for attempt in range(3):
				try:
					clear_details(doc.name, previous_revision)
					frappe.db.commit()
					break
				except Exception:
					frappe.db.rollback()
					if attempt == 2:
						frappe.log_error(
							title=f"FG Cost prior revision cleanup failed: {doc.name} r{previous_revision}"
						)

		return {
			"name": doc.name,
			"revision": new_revision,
			"status": status,
			"is_consistent": is_consistent,
			"totals": totals,
			"exception_count": len(exceptions),
			"share_variance": float(share_variance),
			"shares_are_provisional": not uom_complete,
		}
	except Exception:
		# Drop any partially written new-revision rows; keep previous revision
		frappe.db.rollback()
		try:
			clear_details(doc.name, new_revision)
			frappe.db.commit()
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

	monthly.status = "Incomplete"
	monthly.is_consistent = 0
	if hasattr(monthly, "shares_are_provisional"):
		monthly.shares_are_provisional = 1
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
		if cint(getattr(doc, "shares_are_provisional", 0)):
			frappe.throw(_("Cannot lock a calculation with provisional shares."))
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
	frappe.delete_doc("Monthly FG Cost Calculation", name, force=1)
	frappe.db.commit()
	return {"deleted": name}
