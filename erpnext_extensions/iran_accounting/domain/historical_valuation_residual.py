# Copyright (c) 2026, ERPNext Extensions contributors
"""Historical valuation residual classification and absorb/SA contracts (v5.5.51).

When a consume-to-zero SLE would otherwise ghost ``previous + SED − 0`` inventory
value (5.5.50 clears ``stock_value`` while keeping SED SVD), classify the gap and
either:

* **ABSORB_INTO_MOVEMENT** — transfer / issue: include the residual in SVD so
  inventory value stays capitalized or expenses correctly (same outcome as Core
  MA / mid-anchor RIV). No Stock Adjustment GL.
* **STOCK_ADJUSTMENT** — Manufacture/Repack document authority: SED SVD stays
  document-authoritative; proven historical ghost posts via Company Stock
  Adjustment with Temporary Valuation Adjustment traceability.
* **UNDETERMINED** — fail closed; do not fabricate an adjustment.
* **LEGITIMATE** / **CURRENT_BUG** — no historical bridge.

I3/I4 remain enabled. Non-blocking means accounting for proven historical
residuals — not disabling integrity guards.

Policy boundary
---------------
``ACTIVE_HISTORICAL_VALUATION_GUARD`` selects transitional vs future-strict
behavior without rewriting the valuation engine. Flip only after Historical
Rebuild Campaign verification.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.domain.currency import (
	get_company_currency,
	is_irr_company,
	round_currency,
)
from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
	_quantum,
	is_final_zero_qty_consume_state,
	throw_valuation_integrity,
)

# ---------------------------------------------------------------------------
# Historical valuation guard policy (named boundary — no user-facing toggle)
# ---------------------------------------------------------------------------
TRANSITIONAL_NON_BLOCKING = "TRANSITIONAL_NON_BLOCKING"
STRICT_AFTER_HISTORICAL_REBUILD = "STRICT_AFTER_HISTORICAL_REBUILD"

# Temporary business policy while historical SLE/GL cleanup is incomplete.
# Operations must continue; proven historical residuals are accounted for
# (absorb / configured Stock Adjustment) instead of blocking valid submits.
ACTIVE_HISTORICAL_VALUATION_GUARD = TRANSITIONAL_NON_BLOCKING

# TODO(HISTORICAL-REBUILD-STRICT-GUARD):
# The current non-blocking historical valuation adjustment behavior is
# temporary.
#
# After Historical Rebuild Campaign has completed and all historical
# SLE/GL valuation inconsistencies have been verified and reconciled,
# restore strict valuation enforcement.
#
# At that point:
# 1. Historical residuals must no longer be automatically tolerated.
# 2. Unexpected zero-quantity residuals must fail before persistence.
# 3. Invalid SVD direction must fail.  (I3 — already active; keep enabled.)
# 4. Unexpected valuation differences must not be automatically posted
#    to Stock Adjustment.
# 5. Transitional adjustment behavior must be disabled or removed.
#
# Transition: set ACTIVE_HISTORICAL_VALUATION_GUARD =
# STRICT_AFTER_HISTORICAL_REBUILD (do not rewrite the valuation engine).
# Do not activate strict mode now.


def is_transitional_non_blocking() -> bool:
	"""True while temporary non-blocking historical residual policy is active."""
	return ACTIVE_HISTORICAL_VALUATION_GUARD == TRANSITIONAL_NON_BLOCKING


def is_strict_historical_valuation_guard() -> bool:
	"""True after HRC cleanup — historical residuals are no longer auto-tolerated."""
	return ACTIVE_HISTORICAL_VALUATION_GUARD == STRICT_AFTER_HISTORICAL_REBUILD


# Mirrored purpose sets (avoid import cycle with stock_entry_sync).
_NATIVE_OUTGOING = frozenset(
	{
		"Material Issue",
		"Material Transfer",
		"Material Transfer for Manufacture",
		"Send to Subcontractor",
	}
)
_DOCUMENT_AUTHORITY = frozenset(
	{
		"Manufacture",
		"Repack",
		"Material Consumption for Manufacture",
	}
)

# Classification categories (task contract)
HISTORICAL_RESIDUAL = "HISTORICAL_RESIDUAL"
CURRENT_TRANSACTION_BUG = "CURRENT_TRANSACTION_BUG"
LEGITIMATE_VALUATION_ADJUSTMENT = "LEGITIMATE_VALUATION_ADJUSTMENT"
UNDETERMINED = "UNDETERMINED"

# Disposition
ABSORB_INTO_MOVEMENT = "ABSORB_INTO_MOVEMENT"
POST_STOCK_ADJUSTMENT = "POST_STOCK_ADJUSTMENT"
TRACE_ONLY = "TRACE_ONLY"
FAIL_CLOSED = "FAIL_CLOSED"
NOOP = "NOOP"

# Soft ceiling: Manufacture absorb/SA must not auto-expense 40436-class (~3.3e9).
# Modest/mid residuals (incl. 40942-scale ~5.8e7) may absorb into consume SVD so
# Core value_difference posts Stock Adjustment; billions require manual review.
MANUFACTURE_AUTO_SA_MAX = 500_000_000.0

REASON_HISTORICAL_ZERO_QTY_RESIDUAL = "HISTORICAL_ZERO_QTY_RESIDUAL"

# Pending queue on Stock Entry / frappe.local during process_sle
PENDING_ZERO_QTY_ATTR = "_iran_historical_zero_qty_residuals"


def _entry_get(obj, field, default=None):
	if obj is None:
		return default
	if hasattr(obj, "get"):
		value = obj.get(field)
		return default if value is None else value
	return getattr(obj, field, default)


def ghost_residual_amount(value_before: float, movement: float, company: str | None) -> float:
	"""Inventory value that would vanish if stock_value is forced to 0 while SVD=movement."""
	ccy = get_company_currency(company) if company else "IRR"
	return float(round_currency(flt(value_before) + flt(movement), ccy))


def prior_type_a_on_identity(item_code: str, warehouse: str, before_sle) -> dict | None:
	"""Nearest prior Type-A SLE (qa=0, |sv|>1) on the same identity, if any."""
	if not item_code or not warehouse:
		return None
	posting_datetime = _entry_get(before_sle, "posting_datetime")
	creation = _entry_get(before_sle, "creation")
	name = _entry_get(before_sle, "name")
	rows = frappe.db.sql(
		"""
		select name, voucher_type, voucher_no, stock_value, stock_value_difference,
		       qty_after_transaction, posting_datetime, creation
		from `tabStock Ledger Entry`
		where item_code=%s and warehouse=%s and is_cancelled=0
		  and qty_after_transaction=0 and abs(stock_value)>1
		  and name != %s
		order by posting_datetime desc, creation desc
		limit 5
		""",
		(item_code, warehouse, name or ""),
		as_dict=True,
	)
	if not rows:
		return None
	# Prefer rows strictly before this SLE when timestamps exist.
	for r in rows:
		if posting_datetime and r.posting_datetime and r.posting_datetime > posting_datetime:
			continue
		if (
			posting_datetime
			and r.posting_datetime == posting_datetime
			and creation
			and r.creation
			and r.creation >= creation
		):
			continue
		return r
	return rows[0]


def classify_zero_qty_ghost(
	*,
	sle,
	purpose: str | None,
	value_before: float,
	movement: float,
	company: str | None,
) -> dict[str, Any]:
	"""Classify a would-be ghost residual at final consume-to-zero."""
	company = company or _entry_get(sle, "company")
	quantum = _quantum(company)
	ghost = ghost_residual_amount(value_before, movement, company)
	base = {
		"category": LEGITIMATE_VALUATION_ADJUSTMENT,
		"disposition": NOOP,
		"ghost_amount": ghost,
		"value_before": flt(value_before),
		"movement": flt(movement),
		"purpose": purpose,
	}
	if abs(ghost) <= quantum:
		return base
	if not is_final_zero_qty_consume_state(sle):
		return {
			**base,
			"category": LEGITIMATE_VALUATION_ADJUSTMENT,
			"disposition": NOOP,
			"detail": "not_final_zero_qty_consume",
		}

	purpose = str(purpose or "")
	item_code = _entry_get(sle, "item_code")
	warehouse = _entry_get(sle, "warehouse")
	ancestor = prior_type_a_on_identity(item_code, warehouse, sle)
	has_historical_proof = ancestor is not None or abs(ghost) > quantum

	# Future-strict: after HRC verification, do not auto-tolerate historical ghosts.
	# I3/I4 remain active in both modes; this only gates transitional absorb/SA.
	if is_strict_historical_valuation_guard():
		return {
			**base,
			"category": HISTORICAL_RESIDUAL,
			"disposition": FAIL_CLOSED,
			"detail": "strict_guard_rejects_historical_residual",
			"guard": STRICT_AFTER_HISTORICAL_REBUILD,
			"historical_source_voucher": (ancestor or {}).get("voucher_no"),
			"historical_source_sle": (ancestor or {}).get("name"),
		}

	# Transfer / issue: residual is inventory movement, not P&L plug.
	if purpose in _NATIVE_OUTGOING:
		return {
			**base,
			"category": HISTORICAL_RESIDUAL,
			"disposition": ABSORB_INTO_MOVEMENT,
			"guard": TRANSITIONAL_NON_BLOCKING,
			"historical_source_voucher": (ancestor or {}).get("voucher_no"),
			"historical_source_sle": (ancestor or {}).get("name"),
			"detail": "absorb_residual_into_outgoing_svd",
		}

	# Manufacture / Repack: do not silently ghost. Modest historical residuals are
	# absorbed into consume SVD (SED realigned); FG amounts stay → header
	# value_difference posts Company Stock Adjustment (Core). Huge amounts
	# (40436-class) fail closed — never auto-expense billions without approval.
	if purpose in _DOCUMENT_AUTHORITY:
		if not has_historical_proof and abs(ghost) > quantum:
			has_historical_proof = abs(flt(value_before)) > abs(flt(movement)) + quantum

		if abs(ghost) > MANUFACTURE_AUTO_SA_MAX:
			# SED may claim tens of trillions while the true previous layer is
			# empty/modest (MAT-STE-2026-36178-1 / item 30100298). Absorbing
			# only clears ``value_before`` — it never auto-expenses the
			# overclaim. FAIL_CLOSED after Core already wrote rate×qty SVD
			# leaves VALUATION_CONTINUITY (sv=0, svd=SED) because
			# ``value_before = sv − svd`` invents a fictional previous.
			# Cap when the available layer itself is within the ceiling.
			if abs(flt(value_before)) <= MANUFACTURE_AUTO_SA_MAX:
				return {
					**base,
					"category": HISTORICAL_RESIDUAL,
					"disposition": ABSORB_INTO_MOVEMENT,
					"detail": "manufacture_cap_sed_overclaim_to_available_layer",
					"ceiling": MANUFACTURE_AUTO_SA_MAX,
					"rejected_overclaim": ghost,
					"guard": TRANSITIONAL_NON_BLOCKING,
					"historical_source_voucher": (ancestor or {}).get("voucher_no"),
					"historical_source_sle": (ancestor or {}).get("name"),
				}
			return {
				**base,
				"category": UNDETERMINED,
				"disposition": FAIL_CLOSED,
				"detail": "manufacture_ghost_exceeds_auto_sa_ceiling",
				"ceiling": MANUFACTURE_AUTO_SA_MAX,
				"guard": TRANSITIONAL_NON_BLOCKING,
				"historical_source_voucher": (ancestor or {}).get("voucher_no"),
				"historical_source_sle": (ancestor or {}).get("name"),
			}

		if not has_historical_proof:
			return {
				**base,
				"category": UNDETERMINED,
				"disposition": FAIL_CLOSED,
				"detail": "manufacture_ghost_without_historical_proof",
				"guard": TRANSITIONAL_NON_BLOCKING,
			}

		return {
			**base,
			"category": HISTORICAL_RESIDUAL,
			"disposition": ABSORB_INTO_MOVEMENT,
			"guard": TRANSITIONAL_NON_BLOCKING,
			"historical_source_voucher": (ancestor or {}).get("voucher_no"),
			"historical_source_sle": (ancestor or {}).get("name"),
			"detail": "manufacture_absorb_residual_value_difference_sa",
		}

	return {
		**base,
		"category": UNDETERMINED,
		"disposition": FAIL_CLOSED,
		"detail": "unsupported_purpose_for_ghost_residual",
		"guard": TRANSITIONAL_NON_BLOCKING,
	}


def queue_pending_zero_qty_residual(sle, classification: dict[str, Any]) -> None:
	"""Attach a pending residual record for TVA / campaign discovery."""
	if classification.get("disposition") not in (
		ABSORB_INTO_MOVEMENT,
		POST_STOCK_ADJUSTMENT,
		TRACE_ONLY,
	):
		return
	rec = {
		"item_code": _entry_get(sle, "item_code"),
		"warehouse": _entry_get(sle, "warehouse"),
		"voucher_type": _entry_get(sle, "voucher_type"),
		"voucher_no": _entry_get(sle, "voucher_no"),
		"voucher_detail_no": _entry_get(sle, "voucher_detail_no"),
		"company": _entry_get(sle, "company"),
		"sle_name": _entry_get(sle, "name"),
		"classification": classification,
		"ghost_amount": classification.get("ghost_amount"),
		"disposition": classification.get("disposition"),
		"category": classification.get("category"),
	}
	bucket = getattr(frappe.local, PENDING_ZERO_QTY_ATTR, None)
	if bucket is None:
		bucket = []
		setattr(frappe.local, PENDING_ZERO_QTY_ATTR, bucket)
	bucket.append(rec)
	# Also stamp active Stock Entry when available for on_submit bridge.
	doc = getattr(frappe.local, "iran_tva_active_stock_entry", None)
	if doc is not None:
		pending = getattr(doc, PENDING_ZERO_QTY_ATTR, None)
		if pending is None:
			pending = []
			setattr(doc, PENDING_ZERO_QTY_ATTR, pending)
		pending.append(rec)


def consume_pending_zero_qty_residuals(doc=None) -> list[dict[str, Any]]:
	out: list[dict[str, Any]] = []
	if doc is not None:
		out.extend(list(getattr(doc, PENDING_ZERO_QTY_ATTR, None) or []))
		setattr(doc, PENDING_ZERO_QTY_ATTR, [])
	local_bucket = getattr(frappe.local, PENDING_ZERO_QTY_ATTR, None) or []
	out.extend(local_bucket)
	setattr(frappe.local, PENDING_ZERO_QTY_ATTR, [])
	return out


def apply_zero_qty_movement_contract(
	sle,
	*,
	movement: float,
	rate: float,
	ccy: str,
	purpose: str | None,
	row=None,
) -> float:
	"""Apply SED/native movement at consume-to-zero with historical residual policy.

	Returns the effective ``stock_value_difference`` written onto the SLE.
	"""
	value_after = flt(sle.stock_value)
	value_before = value_after - flt(sle.stock_value_difference)
	company = _entry_get(sle, "company")
	effective_movement = float(round_currency(movement, ccy))

	if not is_final_zero_qty_consume_state(sle):
		sle.stock_value_difference = effective_movement
		sle.stock_value = round_currency(value_before + effective_movement, ccy)
		return effective_movement

	classification = classify_zero_qty_ghost(
		sle=sle,
		purpose=purpose,
		value_before=value_before,
		movement=effective_movement,
		company=company,
	)
	disposition = classification.get("disposition")
	ghost = flt(classification.get("ghost_amount"))

	if disposition == FAIL_CLOSED:
		throw_valuation_integrity(
			"I4",
			detail=(
				f"historical residual at qty=0 is {classification.get('category')}: "
				f"{classification.get('detail')} ghost={ghost}"
			),
			sle=sle,
		)

	if disposition == ABSORB_INTO_MOVEMENT and abs(ghost) > _quantum(company):
		# Core empty-warehouse identity: SVD clears the entire previous layer.
		candidate = float(round_currency(-value_before, ccy))
		# CASE A / I3 guard: absorbing a negative previous layer on an outbound
		# consume yields positive SVD (MAT-STE-2026-40942 / item 13200244).
		# Keep the caller-supplied movement and clear terminal stock_value.
		if flt(_entry_get(sle, "actual_qty")) < 0 and candidate > _quantum(company):
			sle.stock_value_difference = effective_movement
			sle.stock_value = float(round_currency(0, ccy))
			queue_pending_zero_qty_residual(
				sle,
				{
					**classification,
					"detail": "absorb_skipped_negative_prev_would_i3",
					"skipped_absorb_candidate": candidate,
				},
			)
		else:
			effective_movement = candidate
			sle.stock_value_difference = effective_movement
			sle.stock_value = float(round_currency(0, ccy))
			if row is not None and _entry_get(sle, "voucher_detail_no"):
				from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
					align_sed_from_native_outgoing_sle,
				)

				qty = abs(flt(_entry_get(sle, "actual_qty")))
				absorbed_rate = (
					float(round_currency(abs(effective_movement) / qty, ccy)) if qty else rate
				)
				align_sed_from_native_outgoing_sle(
					sle, row, absorbed_rate, effective_movement, ccy
				)
				rate = absorbed_rate
			queue_pending_zero_qty_residual(sle, classification)
	elif disposition == POST_STOCK_ADJUSTMENT and abs(ghost) > _quantum(company):
		# Keep document SED movement; clear terminal stock_value; SA later.
		sle.stock_value_difference = effective_movement
		sle.stock_value = float(round_currency(0, ccy))
		queue_pending_zero_qty_residual(sle, classification)
	else:
		sle.stock_value_difference = effective_movement
		sle.stock_value = float(round_currency(0, ccy))

	# Rates from movement
	qty = abs(flt(_entry_get(sle, "actual_qty")))
	if qty > 1e-9:
		rate = float(round_currency(abs(effective_movement) / qty, ccy))
	sle.valuation_rate = rate
	if flt(sle.actual_qty) > 0:
		sle.incoming_rate = rate
	elif flt(sle.actual_qty) < 0:
		sle.outgoing_rate = rate
	return effective_movement


def build_tva_from_zero_qty_residual(doc, rec: dict[str, Any]) -> dict[str, Any] | None:
	"""Translate a queued zero-qty residual into a Temporary Valuation Adjustment dict."""
	from erpnext_extensions.iran_accounting.domain.temporary_valuation_adjustment import (
		CATEGORY_HISTORICAL_VALUATION,
		CATEGORY_TRACE_ONLY,
		HOTFIX_VERSION,
		KIND_TEMPORARY,
		make_idempotency_key,
		resolve_stock_adjustment_account,
	)

	classification = rec.get("classification") or {}
	disposition = rec.get("disposition") or classification.get("disposition")
	ghost = flt(rec.get("ghost_amount") or classification.get("ghost_amount"))
	if abs(ghost) <= _quantum(doc.company):
		return None

	sa_account = resolve_stock_adjustment_account(doc.company)
	reason = REASON_HISTORICAL_ZERO_QTY_RESIDUAL
	key = make_idempotency_key(doc.name, rec.get("voucher_detail_no"), reason, ghost)

	if disposition == ABSORB_INTO_MOVEMENT:
		return {
			"kind": KIND_TEMPORARY,
			"reason_code": reason,
			"stock_entry": doc.name,
			"stock_entry_detail": rec.get("voucher_detail_no"),
			"item_code": rec.get("item_code"),
			"warehouse": rec.get("warehouse"),
			"company": doc.company,
			"purpose": doc.get("purpose"),
			"posting_date": doc.get("posting_date"),
			"posting_time": doc.get("posting_time"),
			"expected_value": 0.0,
			"actual_value": ghost,
			"adjustment_delta": ghost,
			"debit_credit": "None",
			"stock_adjustment_account": sa_account,
			"posting_mode": "TRACE",
			"accounting_category": CATEGORY_TRACE_ONLY,
			"repair_status": "OPEN",
			"hotfix_version": HOTFIX_VERSION,
			"idempotency_key": key,
			"remarks": (
				f"[HISTORICAL_RESIDUAL] Zero-qty residual {ghost} absorbed into outgoing "
				f"SVD (inventory transfer/issue). No Stock Adjustment GL. "
				f"source={classification.get('historical_source_voucher')}"
			),
			"payload": {
				"classification": classification,
				"disposition": disposition,
				"historical_source_voucher": classification.get("historical_source_voucher"),
				"historical_source_sle": classification.get("historical_source_sle"),
				"campaign_discoverable": True,
				"lifecycle": "OPEN",
			},
		}

	if disposition == POST_STOCK_ADJUSTMENT:
		if not sa_account:
			frappe.throw(
				_(
					"Company {0} Stock Adjustment Account is required to account for "
					"historical manufacture residuals at empty warehouse."
				).format(frappe.bold(doc.company)),
				title=_("Missing Stock Adjustment Account"),
			)
		# Ghost > 0 means inventory value removed without SVD → credit inventory / debit SA.
		return {
			"kind": KIND_TEMPORARY,
			"reason_code": reason,
			"stock_entry": doc.name,
			"stock_entry_detail": rec.get("voucher_detail_no"),
			"item_code": rec.get("item_code"),
			"warehouse": rec.get("warehouse"),
			"company": doc.company,
			"purpose": doc.get("purpose"),
			"posting_date": doc.get("posting_date"),
			"posting_time": doc.get("posting_time"),
			"expected_value": 0.0,
			"actual_value": ghost,
			"adjustment_delta": abs(ghost),
			"debit_credit": "Debit" if ghost > 0 else "Credit",
			"stock_adjustment_account": sa_account,
			"posting_mode": "STOCK_ADJUSTMENT_GL",
			"accounting_category": CATEGORY_HISTORICAL_VALUATION,
			"repair_status": "OPEN",
			"hotfix_version": HOTFIX_VERSION,
			"idempotency_key": key,
			"remarks": (
				f"[HISTORICAL_RESIDUAL] Manufacture/Repack zero-qty ghost {ghost} "
				f"posted via Stock Adjustment {sa_account}. "
				f"source={classification.get('historical_source_voucher')}"
			),
			"payload": {
				"classification": classification,
				"disposition": disposition,
				"historical_source_voucher": classification.get("historical_source_voucher"),
				"historical_source_sle": classification.get("historical_source_sle"),
				"campaign_discoverable": True,
				"lifecycle": "OPEN",
				"gl_destination": "STOCK_ADJUSTMENT",
			},
		}

	return None


def list_campaign_discoverable_residuals(filters: dict | None = None) -> list[dict]:
	"""Integration contract for Historical Campaign (PHASE 2 consumer).

	Does not mutate HRC status or create RIVs. Returns TVA rows that remain
	open for historical repair / reconciliation.
	"""
	from erpnext_extensions.iran_accounting.domain.temporary_valuation_adjustment import (
		DOCTYPE,
	)

	if not frappe.db.exists("DocType", DOCTYPE):
		return []
	filters = dict(filters or {})
	status = filters.pop("repair_status", None)
	query_filters: dict[str, Any] = {}
	if status:
		query_filters["repair_status"] = status
	else:
		query_filters["repair_status"] = (
			"in",
			["OPEN", "SUBMITTED", "REPOST_PENDING", "MANUAL_REVIEW"],
		)
	if filters.get("item_code"):
		query_filters["item_code"] = filters["item_code"]
	if filters.get("warehouse"):
		query_filters["warehouse"] = filters["warehouse"]
	if filters.get("company"):
		query_filters["company"] = filters["company"]
	if filters.get("reason_code"):
		query_filters["reason_code"] = filters["reason_code"]
	else:
		query_filters["reason_code"] = (
			"in",
			[
				REASON_HISTORICAL_ZERO_QTY_RESIDUAL,
				"HISTORICAL_VALUATION_MISMATCH",
				"TRANSFER_VALUATION_RESIDUAL",
				"MANUFACTURING_VALUATION_RESIDUAL",
				"HISTORICAL_ZERO_VALUE_STOCK",
			],
		)

	rows = frappe.get_all(
		DOCTYPE,
		filters=query_filters,
		fields=[
			"name",
			"idempotency_key",
			"reason_code",
			"accounting_category",
			"repair_status",
			"stock_entry",
			"stock_entry_detail",
			"item_code",
			"warehouse",
			"company",
			"purpose",
			"adjustment_delta",
			"posting_mode",
			"stock_adjustment_account",
			"gl_entry",
			"payload",
			"remarks",
			"posting_date",
		],
		order_by="posting_date asc, creation asc",
		limit_page_length=cint(filters.get("limit") or 500),
	)
	# Never claim clean merely because an adjustment exists.
	for r in rows:
		r["historical_valuation_clean"] = False
		r["campaign_action"] = "REVIEW_AND_REPOST_SOURCE"
	return rows


def mark_tva_repost_pending_for_identity(item_code: str, warehouse: str) -> int:
	"""When a RIV starts on an identity, mark related OPEN/SUBMITTED TVA as REPOST_PENDING."""
	from erpnext_extensions.iran_accounting.domain.temporary_valuation_adjustment import (
		DOCTYPE,
	)

	if not frappe.db.exists("DocType", DOCTYPE):
		return 0
	names = frappe.get_all(
		DOCTYPE,
		filters={
			"item_code": item_code,
			"warehouse": warehouse,
			"repair_status": ("in", ["OPEN", "SUBMITTED"]),
			"reason_code": REASON_HISTORICAL_ZERO_QTY_RESIDUAL,
		},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value(DOCTYPE, name, "repair_status", "REPOST_PENDING", update_modified=False)
	return len(names)


def reconcile_tva_after_riv(item_code: str, warehouse: str, riv_name: str | None = None) -> dict:
	"""After RIV: if identity has no Type-A leftover, mark pending TVA RECONCILED.

	Does not delete GL. SA reversals for Manufacture residuals are left as
	MANUAL_REVIEW when a STOCK_ADJUSTMENT_GL row exists — auto-reverse only when
	delta is TRACE absorb (no GL).
	"""
	from erpnext_extensions.iran_accounting.domain.temporary_valuation_adjustment import (
		DOCTYPE,
	)

	type_a = frappe.db.sql(
		"""
		select count(*) from `tabStock Ledger Entry`
		where item_code=%s and warehouse=%s and is_cancelled=0
		  and qty_after_transaction=0 and abs(stock_value)>1
		""",
		(item_code, warehouse),
	)[0][0]
	names = frappe.get_all(
		DOCTYPE,
		filters={
			"item_code": item_code,
			"warehouse": warehouse,
			"repair_status": ("in", ["OPEN", "SUBMITTED", "REPOST_PENDING"]),
			"reason_code": REASON_HISTORICAL_ZERO_QTY_RESIDUAL,
		},
		fields=["name", "posting_mode", "adjustment_delta", "gl_entry"],
	)
	reconciled = reversed_n = manual = 0
	for row in names:
		if type_a:
			frappe.db.set_value(
				DOCTYPE, row.name, "repair_status", "MANUAL_REVIEW", update_modified=False
			)
			manual += 1
			continue
		if row.posting_mode == "STOCK_ADJUSTMENT_GL" and row.gl_entry:
			# Keep auditable; finance must reverse SA explicitly after verifying GL.
			frappe.db.set_value(
				DOCTYPE,
				row.name,
				{
					"repair_status": "MANUAL_REVIEW",
					"remarks": (
						frappe.db.get_value(DOCTYPE, row.name, "remarks") or ""
					)
					+ f" | RIV {riv_name or ''} cleared Type-A; SA GL requires explicit reverse.",
				},
				update_modified=False,
			)
			manual += 1
		else:
			frappe.db.set_value(
				DOCTYPE,
				row.name,
				{
					"repair_status": "RECONCILED",
					"remarks": (
						frappe.db.get_value(DOCTYPE, row.name, "remarks") or ""
					)
					+ f" | Reconciled after RIV {riv_name or ''} (residual absorbed/cleared).",
				},
				update_modified=False,
			)
			reconciled += 1
	return {
		"type_a_remaining": type_a,
		"reconciled": reconciled,
		"reversed": reversed_n,
		"manual_review": manual,
	}
