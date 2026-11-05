# Copyright (c) 2026, ERPNext Extensions contributors
"""Explicit auditable repair of fractional IRR Serial and Batch Entry economics.

Historical / Core Moving Average paths may persist non-integer ``incoming_rate``
and float-dust ``stock_value_difference`` on SABE rows. Physical Two-Slice
allocation correctly refuses fractional pools; this module repairs those rows
with ROUND_HALF_UP to the integer IRR contract and writes an audit JSON.

Not a silent fix inside ``allocate_physical_two_slice`` — callers must invoke
repair deliberately (RIV / DN rewrite preflight).
"""

from __future__ import annotations

import json
import os
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import frappe
from frappe.utils import flt, now_datetime

from erpnext_extensions.iran_accounting.domain.currency import is_irr_company


def _d(value) -> Decimal:
	return Decimal(str(value if value is not None else 0))


def _rhu(value: Decimal) -> Decimal:
	return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def _audit_dir() -> str:
	path = os.path.join(
		frappe.utils.get_bench_path(),
		"sites",
		frappe.local.site,
		"private",
		"files",
		"iran_fractional_sabe_repair",
	)
	os.makedirs(path, exist_ok=True)
	return path


def _riv_context() -> dict[str, Any]:
	"""Best-effort RIV / session identifiers for audit payloads."""
	out: dict[str, Any] = {
		"through_repost_item_valuation": bool(
			frappe.flags.get("through_repost_item_valuation")
		),
	}
	for key in ("iran_active_riv", "current_riv", "riv_name"):
		val = frappe.flags.get(key)
		if val:
			out["riv"] = val
			break
	return out


def assert_bundle_integer_irr(bundle_name: str) -> None:
	"""Fail closed if any SABE child still carries a fractional IRR rate/amount."""
	if not bundle_name:
		return
	rows = frappe.get_all(
		"Serial and Batch Entry",
		filters={"parent": bundle_name},
		fields=["name", "incoming_rate", "outgoing_rate", "stock_value_difference", "qty"],
	)
	for r in rows or []:
		in_rate = _d(r.incoming_rate)
		out_rate = _d(getattr(r, "outgoing_rate", None) or 0)
		svd = _d(r.stock_value_difference)
		fractional = in_rate != in_rate.to_integral_value() or svd != svd.to_integral_value()
		# outgoing_rate is optional on older rows / unit fixtures — only enforce when set.
		if getattr(r, "outgoing_rate", None) not in (None, "", 0, 0.0):
			fractional = fractional or out_rate != out_rate.to_integral_value()
		if fractional:
			frappe.throw(
				f"IRR SABE still fractional after integerize: bundle={bundle_name} "
				f"entry={r.name} incoming={in_rate} outgoing={out_rate} svd={svd}",
				title="Historical SABE integerize",
			)


def integerize_sabe_row(
	entry_name: str, *, company: str | None = None, dry_run: bool = False
) -> dict[str, Any] | None:
	"""RHU-integerize one SABE row's rates and stock_value_difference. Returns diff or None.

	Uses the approved localization formula (integer qty → qty×RHU(rate);
	fractional qty → RHU(raw SVD)). Idempotent: already-integral matching rows
	are no-ops. ``dry_run=True`` reports proposed changes without writing.
	"""
	from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
		localize_sabe_row_values,
	)

	row = frappe.db.get_value(
		"Serial and Batch Entry",
		entry_name,
		[
			"name",
			"qty",
			"incoming_rate",
			"outgoing_rate",
			"stock_value_difference",
			"item_code",
			"batch_no",
			"warehouse",
			"voucher_type",
			"voucher_no",
		],
		as_dict=True,
	)
	if not row:
		return None
	comp = company
	if not comp and row.voucher_type and row.voucher_no:
		comp = frappe.db.get_value(row.voucher_type, row.voucher_no, "company")
	if comp and not is_irr_company(comp):
		return None

	qty = _d(row.qty)
	old_outgoing = flt(getattr(row, "outgoing_rate", None) or 0)
	raw_rate = flt(row.incoming_rate) or old_outgoing or 0.0
	loc = localize_sabe_row_values(
		qty=flt(row.qty),
		raw_rate=raw_rate,
		raw_svd=flt(row.stock_value_difference),
	)
	new_rate = int(loc["rate"])
	# Keep Decimal integral (no trailing .0) for audit/idempotency parity with RHU.
	new_svd = _rhu(_d(loc["svd"]))
	# Preserve outgoing_rate integerization for outward rows; leave 0 when unused.
	new_outgoing = float(new_rate) if flt(row.qty) < 0 or old_outgoing else 0.0

	in_ok = _d(row.incoming_rate) == _d(row.incoming_rate).to_integral_value() and int(
		_d(row.incoming_rate) or 0
	) == new_rate
	out_ok = _d(old_outgoing) == _d(old_outgoing).to_integral_value() and float(
		_d(old_outgoing)
	) == float(new_outgoing)
	svd_ok = _d(row.stock_value_difference) == _d(
		row.stock_value_difference
	).to_integral_value() and _d(row.stock_value_difference) == new_svd
	if in_ok and out_ok and svd_ok:
		return None

	payload = {
		"name": entry_name,
		"item_code": row.item_code,
		"warehouse": row.warehouse,
		"qty": str(qty),
		"old_rate": str(row.incoming_rate),
		"new_rate": new_rate,
		"old_outgoing_rate": str(old_outgoing),
		"new_outgoing_rate": new_outgoing,
		"old_svd": str(row.stock_value_difference),
		"new_svd": str(new_svd),
		"localization_difference": loc["localization_difference"],
		"voucher_type": row.voucher_type,
		"voucher_no": row.voucher_no,
		"batch_no": row.batch_no,
		"dry_run": dry_run,
	}
	if dry_run:
		return payload

	updates = {
		"incoming_rate": float(new_rate),
		"stock_value_difference": float(new_svd),
	}
	# Only stamp outgoing_rate when the row already carried one or is outward.
	if flt(row.qty) < 0 or old_outgoing:
		updates["outgoing_rate"] = float(new_outgoing)
	frappe.db.set_value(
		"Serial and Batch Entry",
		entry_name,
		updates,
		update_modified=False,
	)
	return payload


def integerize_bundle_sabe(
	bundle_name: str, *, company: str | None = None, dry_run: bool = False
) -> dict[str, Any]:
	"""Integerize all SABE children of a Serial and Batch Bundle.

	Does not commit — caller owns the transaction. After mutation, asserts no
	residual fractional IRR rates/amounts remain (skipped on dry_run).
	"""
	if not bundle_name:
		return {"repaired": 0, "rows": []}
	names = frappe.get_all(
		"Serial and Batch Entry",
		filters={"parent": bundle_name},
		pluck="name",
		order_by="idx",
	)
	changed = []
	for name in names:
		diff = integerize_sabe_row(name, company=company, dry_run=dry_run)
		if diff:
			changed.append(diff)
	if changed and not dry_run:
		# Keep bundle aggregates aligned with children.
		total_qty = flt(
			frappe.db.sql(
				"select coalesce(sum(qty),0) from `tabSerial and Batch Entry` where parent=%s",
				bundle_name,
			)[0][0]
		)
		total_amt = flt(
			frappe.db.sql(
				"select coalesce(sum(stock_value_difference),0) from `tabSerial and Batch Entry` where parent=%s",
				bundle_name,
			)[0][0]
		)
		avg = abs(total_amt / total_qty) if total_qty else 0.0
		frappe.db.set_value(
			"Serial and Batch Bundle",
			bundle_name,
			{"total_qty": total_qty, "total_amount": total_amt, "avg_rate": avg},
			update_modified=False,
		)
		assert_bundle_integer_irr(bundle_name)
	return {"bundle": bundle_name, "repaired": len(changed), "rows": changed, "dry_run": dry_run}


def find_fractional_sabe_rows(
	*,
	item_codes: list[str] | None = None,
	company: str | None = None,
	limit: int | None = None,
) -> list[dict[str, Any]]:
	"""Identify submitted SABE rows with fractional IRR monetary fields."""
	params: list[Any] = []
	item_clause = ""
	if item_codes:
		placeholders = ", ".join(["%s"] * len(item_codes))
		item_clause = f" AND e.item_code IN ({placeholders})"
		params.extend(item_codes)
	limit_clause = f" LIMIT {int(limit)}" if limit else ""
	rows = frappe.db.sql(
		f"""
		SELECT
			e.name, e.parent, e.item_code, e.batch_no, e.warehouse,
			e.qty, e.incoming_rate, e.outgoing_rate, e.stock_value_difference,
			e.voucher_type, e.voucher_no, e.voucher_detail_no, e.posting_datetime,
			b.company as bundle_company
		FROM `tabSerial and Batch Entry` e
		LEFT JOIN `tabSerial and Batch Bundle` b ON b.name = e.parent
		WHERE e.docstatus = 1 AND IFNULL(e.is_cancelled, 0) = 0
		  AND (
			ABS(e.incoming_rate - ROUND(e.incoming_rate)) > 0.000001
			OR ABS(IFNULL(e.outgoing_rate,0) - ROUND(IFNULL(e.outgoing_rate,0))) > 0.000001
			OR ABS(e.stock_value_difference - ROUND(e.stock_value_difference)) > 0.000001
		  )
		  {item_clause}
		ORDER BY e.posting_datetime, e.creation, e.idx
		{limit_clause}
		""",
		tuple(params),
		as_dict=True,
	)
	out = []
	for r in rows or []:
		comp = company or r.get("bundle_company")
		if not comp and r.get("voucher_type") and r.get("voucher_no"):
			comp = frappe.db.get_value(r.voucher_type, r.voucher_no, "company")
		if company and comp and comp != company:
			continue
		if comp and not is_irr_company(comp):
			continue
		out.append(r)
	return out


def dry_run_fractional_sabe_repair(
	*,
	item_codes: list[str] | None = None,
	company: str | None = None,
) -> dict[str, Any]:
	"""Read-only report of proposed SABE integerization (Phase 2 gate).

	Does not mutate. Idempotent preview: already-integer rows are omitted.
	"""
	candidates = find_fractional_sabe_rows(item_codes=item_codes, company=company)
	proposed = []
	localization_net = 0.0
	for r in candidates:
		diff = integerize_sabe_row(r.name, company=company, dry_run=True)
		if diff:
			proposed.append(diff)
			localization_net += flt(diff.get("localization_difference"))
	payload = {
		"at": str(now_datetime()),
		"site": frappe.local.site,
		"mode": "dry_run",
		"item_codes": item_codes,
		"company": company,
		"fractional_candidates": len(candidates),
		"proposed_repairs": len(proposed),
		"localization_difference_net": localization_net,
		"rows": proposed,
		"policy": (
			"ROUND_HALF_UP integer rates; integer qty → qty×rate SVD; "
			"localization_difference = integerized − ROUND(raw) → 621301 after approval; "
			"warehouse zero-qty SLE clear remains Core/ABSORB (Case B not Type A)."
		),
	}
	path = os.path.join(
		_audit_dir(),
		f"dry_run_{now_datetime().strftime('%Y%m%d_%H%M%S')}.json",
	)
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
	payload["audit_path"] = path
	return payload


def apply_fractional_sabe_repair(
	*,
	item_codes: list[str] | None = None,
	company: str | None = None,
	dry_run: bool = True,
	reason: str = "approved_historical_sabe_repair",
) -> dict[str, Any]:
	"""Controlled historical SABE repair. Defaults to dry_run (safe).

	When ``dry_run=False``, mutates SABE under a savepoint and writes audit JSON.
	Does not commit — caller owns the outer transaction. Second execution is a
	no-op when rows are already integer.
	"""
	if dry_run:
		return dry_run_fractional_sabe_repair(item_codes=item_codes, company=company)

	candidates = find_fractional_sabe_rows(item_codes=item_codes, company=company)
	savepoint = "iran_sabe_scope_" + frappe.generate_hash(length=8)
	frappe.db.savepoint(savepoint)
	changed: list[dict[str, Any]] = []
	bundles: set[str] = set()
	try:
		for r in candidates:
			diff = integerize_sabe_row(r.name, company=company, dry_run=False)
			if diff:
				changed.append(diff)
				if r.parent:
					bundles.add(r.parent)
		for b in bundles:
			integerize_bundle_sabe(b, company=company, dry_run=False)
		frappe.db.release_savepoint(savepoint)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise

	localization_net = sum(flt(d.get("localization_difference")) for d in changed)
	payload = {
		"at": str(now_datetime()),
		"site": frappe.local.site,
		"mode": "apply",
		"reason": reason,
		"item_codes": item_codes,
		"company": company,
		"repaired": len(changed),
		"localization_difference_net": localization_net,
		"rows": changed,
		**_riv_context(),
		"committed_by_caller": True,
		"note": (
			"SABE monetary fields integerized. Stock Adjustment GL for localization "
			"differences must be posted via the approved accounting mechanism / RIV "
			"after reconciliation. Case B warehouse-vs-batch gaps are not Type A."
		),
	}
	path = os.path.join(
		_audit_dir(),
		f"repair_scope_{now_datetime().strftime('%Y%m%d_%H%M%S')}.json",
	)
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
	payload["audit_path"] = path
	return payload


def log_riv_short_pool_event(
	*,
	item_code: str,
	batch_no: str,
	warehouse: str,
	pool_qty,
	out_qty,
	voucher_type: str | None,
	voucher_no: str | None,
	bundle: str | None = None,
) -> str:
	"""Persist an explicit audit record for RIV short-pool fallthrough.

	Fallthrough is RIV-only: live submit still raises AllocationError. The
	caller must subsequently integerize Core MA output so fractional rates
	cannot persist.
	"""
	payload = {
		"at": str(now_datetime()),
		"site": frappe.local.site,
		"event": "riv_short_pool_fallthrough",
		"item_code": item_code,
		"batch_no": batch_no,
		"warehouse": warehouse,
		"pool_qty": str(pool_qty),
		"out_qty": str(out_qty),
		"voucher_type": voucher_type,
		"voucher_no": voucher_no,
		"bundle": bundle,
		**_riv_context(),
		"policy": (
			"RIV-only: skip Physical Two-Slice when visible batch pool is short; "
			"Core MA + integerize_bundle_sabe must follow; live submit remains fail-closed."
		),
	}
	path = os.path.join(
		_audit_dir(),
		f"short_pool_{now_datetime().strftime('%Y%m%d_%H%M%S')}_{batch_no[:40]}.json",
	)
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
	return path


def repair_batch_pool_fractional_irr(
	item_code: str,
	batch_no: str,
	warehouse: str,
	*,
	company: str | None = None,
	exclude_voucher_detail_no: str | None = None,
	reason: str = "pre_two_slice_allocate",
) -> dict[str, Any]:
	"""Repair all submitted SABE rows for batch@warehouse to integer IRR economics.

	Uses a DB savepoint so a mid-repair failure rolls back only this repair.
	Does not commit — the caller (RIV / submit) owns the outer transaction.
	Audit JSON is written only after the savepoint release succeeds.
	"""
	savepoint = "iran_sabe_" + frappe.generate_hash(length=8)
	frappe.db.savepoint(savepoint)
	try:
		params: list[Any] = [item_code, batch_no, warehouse]
		excl = ""
		if exclude_voucher_detail_no:
			excl = " AND IFNULL(e.voucher_detail_no,'') != %s"
			params.append(exclude_voucher_detail_no)
		rows = frappe.db.sql(
			f"""
			SELECT e.name, e.parent
			FROM `tabSerial and Batch Entry` e
			WHERE e.item_code=%s AND e.batch_no=%s AND e.warehouse=%s
			  AND e.docstatus=1 AND e.is_cancelled=0
			  AND e.type_of_transaction IN ('Inward', 'Outward')
			  {excl}
			ORDER BY e.posting_datetime, e.creation, e.idx
			""",
			tuple(params),
			as_dict=True,
		)
		changed = []
		bundles = set()
		for r in rows or []:
			diff = integerize_sabe_row(r.name, company=company)
			if diff:
				changed.append(diff)
				if r.parent:
					bundles.add(r.parent)
		for b in bundles:
			integerize_bundle_sabe(b, company=company)
		frappe.db.release_savepoint(savepoint)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise

	payload = {
		"at": str(now_datetime()),
		"site": frappe.local.site,
		"reason": reason,
		"item_code": item_code,
		"batch_no": batch_no,
		"warehouse": warehouse,
		"exclude_voucher_detail_no": exclude_voucher_detail_no,
		"repaired": len(changed),
		"rows": changed,
		**_riv_context(),
		"committed_by_caller": True,
		"note": "Audit written after savepoint success; outer RIV/submit commits or rolls back.",
	}
	path = os.path.join(
		_audit_dir(),
		f"repair_{now_datetime().strftime('%Y%m%d_%H%M%S')}_{batch_no[:40]}.json",
	)
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
	payload["audit_path"] = path
	return payload
