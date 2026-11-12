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

from erpnext_extensions.iran_accounting.fg_cost_allocation import FG_ITEM_GROUP, FG_PARENT_ITEM_GROUP
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
	persist_exceptions,
	persist_reconciliation_checks,
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
	doc.persian_period = f"{cint(doc.persian_year):04d}-{cint(doc.persian_month):02d}"
	doc.currency = currency

	try:
		# Persist NEW revision first; only clear previous on success
		rules = load_enabled_rules(settings.name)
		extract = extract_pools_for_rules(doc.company, period, rules)
		pools = extract.pools
		excluded = extract.excluded_net

		rows, excluded_rows, exclusion_summary = fetch_eligible_fg_rows(
			doc.company, period, settings_name=settings.name
		)
		products, prod_exceptions = consolidate_products(rows)
		norm_exceptions = apply_normalization(products, settings)
		# Item-scope exclusions are reported separately and must not create UOM blockers
		exceptions = prod_exceptions + norm_exceptions
		missing_uom_count = sum(1 for p in products.values() if p.missing_uom_factor) + len(
			[e for e in norm_exceptions if "UOM" in e or "conversion" in e.lower()]
		)
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
		if extract.has_overlap_conflicts:
			pair_n = len(extract.overlapping_rule_pairs)
			gl_n = len(extract.overlapping_gl_entries)
			blocking.append(
				f"Overlapping expense rules detected ({pair_n} rule pair(s), {gl_n} conflicting GL Entry(ies)). "
				"Ambiguous GL amounts were withheld from all Expense Pools to prevent double allocation. "
				"Resolve overlapping Account/Department/Cost Center rules before the calculation can be consumable."
			)
			for p in extract.overlapping_rule_pairs[:10]:
				blocking.append(
					f"Rule overlap: {p.get('rule_a')} vs {p.get('rule_b')} on Account {p.get('account')}"
				)
			for g in extract.overlapping_gl_entries[:10]:
				rules_hit = ", ".join(x.get("name", "") for x in (g.get("matching_rules") or []))
				blocking.append(
					f"GL conflict {g.get('gl_entry')}: matches [{rules_hit}] — amount withheld"
				)

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
		persist_reconciliation_checks(
			monthly_name=doc.name,
			revision=new_revision,
			persian_period=doc.persian_period
			or f"{cint(doc.persian_year):04d}-{cint(doc.persian_month):02d}",
			currency=currency,
			checks=recon.checks,
		)
		persist_exceptions(
			monthly_name=doc.name,
			revision=new_revision,
			persian_period=doc.persian_period
			or f"{cint(doc.persian_year):04d}-{cint(doc.persian_month):02d}",
			blocking=blocking,
			products=products,
		)

		total_identified = sum((p.monthly_net_expense for p in pools), Decimal("0"))
		total_allocated = sum((b.total_allocated_expense for b in batches), Decimal("0"))
		fingerprint = _fingerprint(pools, products, period)

		is_consistent = (
			1
			if recon.ok
			and share_ok
			and uom_complete
			and rules
			and not exceptions
			and not extract.has_overlap_conflicts
			else 0
		)
		if action == "Recalculate" or previous_revision > 0:
			status = "Recalculated" if is_consistent else "Incomplete"
		else:
			status = "Calculated" if is_consistent else "Incomplete"

		unallocated = total_identified - total_allocated
		over_allocated = total_allocated > total_identified
		if over_allocated:
			blocking.append(
				f"Total Allocated Expenses {total_allocated} exceed Total Identified Monthly Expenses {total_identified}"
			)
			is_consistent = 0
			status = "Incomplete"
		if extract.has_overlap_conflicts:
			is_consistent = 0
			status = "Incomplete"

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
		if hasattr(doc, "missing_uom_count"):
			doc.missing_uom_count = missing_uom_count
		if hasattr(doc, "over_allocated"):
			doc.over_allocated = 1 if over_allocated else 0
		# Keep JSON compact — full per-row check lists blow max_allowed_packet on large months.
		failed_checks = [c for c in recon.checks if not c.get("ok")]
		doc.reconciliation_summary = json.dumps(
			{
				"ok": recon.ok and not over_allocated,
				"uom_complete": uom_complete,
				"shares_are_provisional": not uom_complete,
				"over_allocated": over_allocated,
				"eligibility_source": "Item Primary UOMs",
				"default_seed_item_group": FG_ITEM_GROUP,
				"default_seed_parent_item_group": FG_PARENT_ITEM_GROUP,
				"eligible_source_row_count": len(rows),
				"configured_item_exclusions": {
					k: exclusion_summary.get(k)
					for k in (
						"excluded_source_row_count",
						"excluded_qty",
						"configured_item_count",
						"by_item_group",
					)
					if k in exclusion_summary
				},
				"check_counts": {
					"total": len(recon.checks),
					"failed": len(failed_checks),
				},
				"failed_checks_sample": failed_checks[:50],
				"messages": (
					recon.messages
					+ (
						[
							f"Allocated exceeds identified by {float(total_allocated - total_identified)}"
						]
						if over_allocated
						else []
					)
				)[:50],
			},
			ensure_ascii=False,
			default=str,
		)
		doc.exceptions_json = json.dumps(
			{
				"blocking": (blocking)[:100],
				"configured_item_exclusions": {
					"excluded_source_row_count": exclusion_summary.get("excluded_source_row_count"),
					"excluded_qty": exclusion_summary.get("excluded_qty"),
					"by_item_group": exclusion_summary.get("by_item_group"),
				},
				"rule_dimension_overlaps": {
					"has_conflicts": extract.has_overlap_conflicts,
					"overlapping_rule_pairs": extract.overlapping_rule_pairs[:50],
					"overlapping_gl_entries": extract.overlapping_gl_entries[:50],
					"policy": "reject_ambiguous_no_priority — conflicting GL amounts withheld from all pools",
				},
			},
			ensure_ascii=False,
			default=str,
		)
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
			"excluded_source_row_count": exclusion_summary.get("excluded_source_row_count", 0),
			"excluded_qty": exclusion_summary.get("excluded_qty", 0),
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
	return _apply_batch_share_override(
		monthly_name=row.monthly_calculation,
		revision=cint(row.revision),
		batch_no=row.batch_no,
		batch_allocation=row.name,
		applied_share=None if cint(clear) else flt(manual_share),
		clear=cint(clear),
	)


@frappe.whitelist()
def set_applied_batch_share(
	batch_expense_allocation: str | None = None,
	batch_allocation: str | None = None,
	applied_share: float | None = None,
	clear: int = 0,
	reason: str | None = None,
	commit: int = 1,
):
	"""Edit Applied Batch Share server-side; recalculate Allocated Amount and parent totals.

	Does not silently redistribute remaining share to other batches.
	"""
	if frappe.flags.get("costing_in_share_engine"):
		return {"skipped": 1, "reason": "already in share engine"}
	if batch_expense_allocation:
		bx = frappe.get_doc("MFG Cost Batch Expense Allocation", batch_expense_allocation)
		bx.check_permission("write")
		return _apply_batch_share_override(
			monthly_name=bx.monthly_calculation,
			revision=cint(bx.revision),
			batch_no=bx.batch_no,
			batch_allocation=bx.batch_allocation,
			applied_share=None if cint(clear) else flt(applied_share),
			clear=cint(clear),
			reason=reason,
			commit=bool(cint(commit)),
		)
	if batch_allocation:
		return set_batch_manual_share(batch_allocation, applied_share, clear=clear)
	frappe.throw(_("batch_expense_allocation or batch_allocation is required."))


def _apply_batch_share_override(
	*,
	monthly_name: str,
	revision: int,
	batch_no: str,
	batch_allocation: str | None,
	applied_share: float | None,
	clear: int = 0,
	reason: str | None = None,
	commit: bool = True,
) -> dict:
	from decimal import Decimal, ROUND_HALF_UP

	from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision
	from erpnext_extensions.iran_accounting.fg_cost_allocation import DETAIL_DOCTYPE_MAP

	frappe.flags.costing_in_share_engine = True
	try:
		return _apply_batch_share_override_inner(
			monthly_name=monthly_name,
			revision=revision,
			batch_no=batch_no,
			batch_allocation=batch_allocation,
			applied_share=applied_share,
			clear=clear,
			reason=reason,
			commit=commit,
		)
	finally:
		frappe.flags.costing_in_share_engine = False


def _apply_batch_share_override_inner(
	*,
	monthly_name: str,
	revision: int,
	batch_no: str,
	batch_allocation: str | None,
	applied_share: float | None,
	clear: int = 0,
	reason: str | None = None,
	commit: bool = True,
) -> dict:
	from decimal import Decimal, ROUND_HALF_UP

	from erpnext_extensions.iran_accounting.domain.currency import get_currency_precision
	from erpnext_extensions.iran_accounting.fg_cost_allocation import DETAIL_DOCTYPE_MAP

	monthly = frappe.get_doc("Monthly FG Cost Calculation", monthly_name)
	monthly.check_permission("write")
	if monthly.status == "Locked" or cint(monthly.is_consumed):
		frappe.throw(_("Cannot edit shares on Locked/consumed calculation."))
	if cint(monthly.revision) != cint(revision):
		frappe.throw(_("Share edits apply only to the active revision."))

	batch_row = None
	if batch_allocation:
		batch_row = frappe.get_doc("MFG Cost Batch Allocation", batch_allocation)
	else:
		name = frappe.db.get_value(
			DETAIL_DOCTYPE_MAP["batch"],
			{"monthly_calculation": monthly_name, "revision": revision, "batch_no": batch_no},
			"name",
		)
		if not name:
			frappe.throw(_("Production Batch allocation not found for {0}.").format(batch_no))
		batch_row = frappe.get_doc(DETAIL_DOCTYPE_MAP["batch"], name)

	previous_applied = flt(batch_row.applied_share)
	previous_manual = batch_row.manual_share
	calculated = flt(batch_row.calculated_share)

	if cint(clear):
		batch_row.is_manual_override = 0
		batch_row.manual_share = 0
		batch_row.applied_share = calculated
		new_applied = Decimal(str(calculated))
	else:
		if applied_share is None:
			frappe.throw(_("Applied Batch Share is required."))
		new_applied = Decimal(str(flt(applied_share)))
		if new_applied < 0:
			frappe.throw(_("Applied Batch Share cannot be negative."))
		batch_row.is_manual_override = 1
		batch_row.manual_share = float(new_applied)
		batch_row.applied_share = float(new_applied)
	# Never overwrite calculated_share
	batch_row.calculated_share = calculated
	batch_row.save(ignore_permissions=True)

	currency = monthly.currency or frappe.get_cached_value("Company", monthly.company, "default_currency")
	precision = get_currency_precision(currency)
	q = Decimal("1") if precision <= 0 else Decimal("1").scaleb(-precision)

	bx_rows = frappe.get_all(
		DETAIL_DOCTYPE_MAP["batch_expense"],
		filters={"monthly_calculation": monthly_name, "revision": revision, "batch_no": batch_no},
		fields=["name", "pool_monthly_net_expense", "calculated_batch_share", "expense_pool", "account"],
	)
	updated_bx = []
	for r in bx_rows:
		pool_net = Decimal(str(flt(r.pool_monthly_net_expense)))
		allocated = (pool_net * new_applied).quantize(q, rounding=ROUND_HALF_UP)
		frappe.db.set_value(
			DETAIL_DOCTYPE_MAP["batch_expense"],
			r.name,
			{
				"manual_batch_share": float(new_applied) if not cint(clear) else 0.0,
				"applied_batch_share": float(new_applied),
				"allocated_amount": float(allocated),
				"is_manual_override": 0 if cint(clear) else 1,
				"calculated_batch_share": flt(r.calculated_batch_share),
				"residual_applied": 0,
			},
			update_modified=True,
		)
		updated_bx.append({"name": r.name, "allocated_amount": float(allocated)})

	# Tiny currency residual only when Applied Batch Shares sum to ~100%.
	# Never redistribute large gaps (Incomplete / provisional shares) — that would break
	# Allocated Amount = Pool Monthly Net Expense × Applied Batch Share.
	share_rows_pre = frappe.get_all(
		DETAIL_DOCTYPE_MAP["batch"],
		filters={"monthly_calculation": monthly_name, "revision": revision},
		fields=["batch_no", "applied_share", "normalized_qty"],
	)
	applied_sum_pre = sum((Decimal(str(flt(r.applied_share))) for r in share_rows_pre), Decimal("0"))
	share_eps = Decimal("0.000001")
	shares_complete = bool(share_rows_pre) and abs(applied_sum_pre - Decimal("1")) <= share_eps
	if shares_complete:
		batch_norms = {r.batch_no: Decimal(str(flt(r.normalized_qty))) for r in share_rows_pre}
		pool_keys = frappe.db.sql(
			"""
			SELECT DISTINCT expense_pool, pool_monthly_net_expense
			FROM `tabMFG Cost Batch Expense Allocation`
			WHERE monthly_calculation=%s AND revision=%s
			""",
			(monthly_name, revision),
			as_dict=True,
		)
		for pk in pool_keys:
			rows = frappe.get_all(
				DETAIL_DOCTYPE_MAP["batch_expense"],
				filters={
					"monthly_calculation": monthly_name,
					"revision": revision,
					"expense_pool": pk.expense_pool,
				},
				fields=["name", "batch_no", "allocated_amount"],
			)
			if not rows:
				continue
			pool_net = Decimal(str(flt(pk.pool_monthly_net_expense)))
			alloc_sum = sum((Decimal(str(flt(x.allocated_amount))) for x in rows), Decimal("0"))
			diff = (pool_net - alloc_sum).quantize(q, rounding=ROUND_HALF_UP)
			if diff == 0:
				continue
			target = max(rows, key=lambda x: (batch_norms.get(x.batch_no, Decimal("0")), x.batch_no))
			new_amt = (Decimal(str(flt(target.allocated_amount))) + diff).quantize(q, rounding=ROUND_HALF_UP)
			frappe.db.set_value(
				DETAIL_DOCTYPE_MAP["batch_expense"],
				target.name,
				{"allocated_amount": float(new_amt), "residual_applied": 1},
				update_modified=True,
			)

	# Refresh batch totals from BxE amounts
	for bname, bno in frappe.get_all(
		DETAIL_DOCTYPE_MAP["batch"],
		filters={"monthly_calculation": monthly_name, "revision": revision},
		fields=["name", "batch_no"],
		as_list=True,
	):
		btotal = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(allocated_amount), 0)
			FROM `tabMFG Cost Batch Expense Allocation`
			WHERE monthly_calculation=%s AND revision=%s AND batch_no=%s
			""",
			(monthly_name, revision, bno),
		)[0][0]
		frappe.db.set_value(
			DETAIL_DOCTYPE_MAP["batch"],
			bname,
			"total_allocated_expense",
			flt(btotal),
			update_modified=True,
		)

	# Parent totals from active revision only
	total_allocated = frappe.db.sql(
		"""
		SELECT COALESCE(SUM(allocated_amount), 0)
		FROM `tabMFG Cost Batch Expense Allocation`
		WHERE monthly_calculation=%s AND revision=%s
		""",
		(monthly_name, revision),
	)[0][0]
	total_allocated = Decimal(str(flt(total_allocated)))
	identified = Decimal(str(flt(monthly.total_identified_expenses)))

	# Share variance across batches (do not redistribute)
	share_rows = frappe.get_all(
		DETAIL_DOCTYPE_MAP["batch"],
		filters={"monthly_calculation": monthly_name, "revision": revision},
		fields=["batch_no", "applied_share", "normalized_qty"],
	)
	applied_sum = sum((Decimal(str(flt(r.applied_share))) for r in share_rows), Decimal("0"))
	share_variance = applied_sum - (Decimal("1") if share_rows else Decimal("0"))
	eps = Decimal("0.000001")
	share_ok = abs(share_variance) <= eps
	over_allocated = total_allocated > identified

	monthly.total_allocated_expenses = float(total_allocated)
	monthly.unallocated_amount = float(identified - total_allocated)
	monthly.share_variance = float(share_variance)
	monthly.over_allocated = 1 if over_allocated else 0
	monthly.status = "Incomplete"
	monthly.is_consistent = 0
	monthly.save(ignore_permissions=True, ignore_version=True)

	write_revision_log(
		monthly_name=monthly_name,
		revision=revision,
		action="Manual Share Override",
		previous_status=monthly.status,
		new_status="Incomplete",
		source_fingerprint=monthly.source_fingerprint or "",
		totals={
			"total_allocated_expenses": str(total_allocated),
			"share_variance": str(share_variance),
			"batch_no": batch_no,
			"previous_applied": previous_applied,
			"new_applied": float(new_applied),
			"previous_manual": previous_manual,
			"reason": reason or "",
			"user": frappe.session.user,
			"timestamp": str(now_datetime()),
		},
		override_map={batch_no: str(new_applied)},
		reconciliation={
			"share_ok": share_ok,
			"over_allocated": over_allocated,
			"updated_batch_expense_rows": len(updated_bx),
		},
		retain_overrides=True,
	)
	if commit:
		frappe.db.commit()
	return {
		"batch_no": batch_no,
		"batch_allocation": batch_row.name,
		"applied_share": float(new_applied),
		"calculated_share": calculated,
		"total_allocated_expenses": float(total_allocated),
		"share_variance": float(share_variance),
		"over_allocated": 1 if over_allocated else 0,
		"updated_rows": updated_bx,
		"status": "Incomplete",
		"formula": "Allocated Amount = Pool Monthly Net Expense × Applied Batch Share",
	}


@frappe.whitelist()
def lock_monthly(name: str, lock: int = 1):
	doc = frappe.get_doc("Monthly FG Cost Calculation", name)
	doc.check_permission("write")
	if cint(lock):
		if not cint(doc.is_consistent) or doc.status == "Incomplete":
			frappe.throw(_("Only consistent Calculated/Recalculated documents can be Locked."))
		if cint(getattr(doc, "shares_are_provisional", 0)):
			frappe.throw(_("Cannot lock a calculation with provisional shares."))
		if cint(getattr(doc, "over_allocated", 0)):
			frappe.throw(_("Cannot lock a calculation where allocated expenses exceed identified expenses."))
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
