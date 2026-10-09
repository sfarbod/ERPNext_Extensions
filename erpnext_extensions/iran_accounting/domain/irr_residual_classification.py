# Copyright (c) 2026, ERPNext Extensions contributors
"""Provenance-based IRR residual classification and shared ResidualDecision evaluator.

Contract (3.8.7)
----------------
Class A: residual reproduced exactly by approved iran_accounting rounding helpers
         for the voucher flow (provenance-first; path-derived bound only after).
Class B: valuation inconsistency / invalid rate / unexplained gap — fail closed.
Net Class A == 0 → full Round Off subsystem bypass (no Account/CC/dim/partner).
Net Class A != 0 → Company Round Off Account/CC + Company Round Off Dimension
Defaults + safe non-stock partner (never Stock Adjustment fallback).

Purchase Receipt (3.8.7): authoritative stock amount follows ERPNext's
BuyingController.update_valuation_rate numerator (base_net_amount + item_tax_amount
+ landed_cost_voucher_amount + …) and the same stock-UOM qty divisor. Never
amount/base_amount ÷ qty.

Purchase Receipt (5.5.28): historical UVR float ``valuation_rate = auth/stock_qty``
(pre-Iran integerization, DECIMAL(30,9) or IEEE float) is amount-authoritative
Class A after coercion to ``integer_valuation_rate_from_amount`` — not Class B.
True non-integer rates that are not exact auth÷qty reconstructions remain Class B.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.domain.currency import (
	amount_rate_qty_residual,
	get_company_currency,
	integer_valuation_rate_from_amount,
	is_irr_company,
	round_currency,
	round_monetary_rate,
	round_row_amount,
)

# Purchase Receipt Item.valuation_rate is DECIMAL(30,9) in MariaDB / Frappe.
_PR_VALUATION_RATE_QUANTUM = Decimal("0.000000001")

STATUS_BYPASS = "bypass"
STATUS_READY = "ready"
STATUS_CLASS_B = "class_b_error"
STATUS_CONFIG = "config_error"
STATUS_PARTNER = "partner_error"


@dataclass
class ResidualDecision:
	status: str = STATUS_BYPASS
	class_a_rows: list[dict[str, Any]] = field(default_factory=list)
	class_b_rows: list[dict[str, Any]] = field(default_factory=list)
	net_signed_debit: float = 0.0
	round_off_account: str | None = None
	round_off_cost_center: str | None = None
	dimensions: dict[str, Any] = field(default_factory=dict)
	partner: Any | None = None
	partner_checked: bool = False
	messages: list[str] = field(default_factory=list)
	diagnostics: list[dict[str, Any]] = field(default_factory=list)

	@property
	def is_bypass(self) -> bool:
		return self.status == STATUS_BYPASS

	@property
	def is_ready(self) -> bool:
		return self.status == STATUS_READY

	@property
	def is_error(self) -> bool:
		return self.status in (STATUS_CLASS_B, STATUS_CONFIG, STATUS_PARTNER)


def _dimension_fieldnames() -> list[str]:
	from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
		get_accounting_dimensions,
	)

	return [d for d in get_accounting_dimensions() if d != "cost_center"]


def _row_dimension_values(row) -> dict[str, Any]:
	out = {}
	for fieldname in _dimension_fieldnames():
		val = row.get(fieldname) if hasattr(row, "get") else getattr(row, fieldname, None)
		if val not in (None, ""):
			out[fieldname] = val
	return out


def _path_derived_bound(qty) -> float:
	"""Secondary bound after provenance: integer amount÷qty remainder is < |qty|."""
	q = abs(flt(qty))
	return max(1.0, q) if q else 1.0


def is_legacy_uvr_float_valuation_rate(authoritative_amount, qty, valuation_rate, currency: str) -> bool:
	"""True when VR is the historical UVR float ``amount / stock_qty`` (pre-Iran integerization).

	Exact reconstruction only (no abs-diff tolerance):

	1. Rate is non-integer under the IRR monetary-rate contract.
	2. ``ROUND_HALF_UP(rate)`` equals ``integer_valuation_rate_from_amount(auth, qty)``.
	3. Rate equals one of:
	   - ``flt(auth / qty)`` (ERPNext UVR float division), or
	   - ``(auth / qty)`` quantized to DECIMAL(30,9) via ROUND_HALF_UP / ROUND_DOWN /
	     ROUND_HALF_EVEN (MariaDB persistence of the same rational).

	Economically amount-authoritative: the float is storage shape, not a second price.
	"""
	if valuation_rate in (None, "") or authoritative_amount in (None, ""):
		return False
	qty_f = flt(qty)
	auth_f = flt(authoritative_amount)
	rate_f = flt(valuation_rate)
	if not qty_f or not auth_f:
		return False

	rounded_rate = flt(round_monetary_rate(rate_f, currency))
	if rounded_rate == rate_f:
		return False

	int_from_amount = flt(integer_valuation_rate_from_amount(auth_f, qty_f, currency))
	if rounded_rate != int_from_amount:
		return False

	# Path 1: IEEE float reconstruction used by BuyingController.update_valuation_rate
	if rate_f == flt(auth_f / qty_f):
		return True

	# Path 2: DECIMAL(30,9) persistence of the exact rational amount/qty
	try:
		d_auth = Decimal(str(auth_f))
		d_qty = Decimal(str(qty_f))
		d_rate = Decimal(str(rate_f)).quantize(_PR_VALUATION_RATE_QUANTUM, rounding=ROUND_HALF_UP)
		quot = d_auth / d_qty
	except Exception:
		return False

	for mode in (ROUND_HALF_UP, ROUND_DOWN, ROUND_HALF_EVEN):
		if quot.quantize(_PR_VALUATION_RATE_QUANTUM, rounding=mode) == d_rate:
			return True
	return False


def classify_amount_rate_residual(
	*,
	qty,
	authoritative_amount,
	valuation_rate,
	currency: str,
	item_code: str | None = None,
	idx: int | None = None,
	row_name: str | None = None,
	extra: dict | None = None,
) -> dict[str, Any]:
	"""Classify one amount vs rate-first product residual using approved helpers only."""
	qty = flt(qty)
	auth = flt(authoritative_amount)
	rate = valuation_rate
	diag = {
		"item_code": item_code,
		"idx": idx,
		"row_name": row_name,
		"qty": qty,
		"authoritative_amount": auth,
		"valuation_rate": rate,
		**(extra or {}),
	}

	if not qty:
		return {
			**diag,
			"class": "skip",
			"reason": "zero_qty",
			"residual": 0,
			"round_off_debit": 0,
		}

	if rate in (None, ""):
		if not auth:
			return {**diag, "class": "skip", "reason": "missing_rate_zero_amount", "residual": 0}
		return {
			**diag,
			"class": "B",
			"reason": "missing_valuation_rate",
			"residual": auth,
			"expected_valuation_rate": integer_valuation_rate_from_amount(auth, qty, currency)
			if auth
			else None,
			"expected_amount": None,
		}

	rate_f = flt(rate)
	if rate_f <= 0 and auth:
		return {
			**diag,
			"class": "B",
			"reason": "valuation_rate_le_zero_with_nonzero_amount",
			"residual": auth,
			"expected_valuation_rate": integer_valuation_rate_from_amount(auth, qty, currency),
			"expected_amount": round_row_amount(qty, integer_valuation_rate_from_amount(auth, qty, currency), currency),
		}

	rounded_rate = flt(round_monetary_rate(rate_f, currency))
	legacy_uvr_float = False
	if rounded_rate != rate_f:
		# Historical Purchase Receipt UVR stores flt(amount/stock_qty) before Iran
		# integerization. Coerce to amount-authoritative integer VR — do not mutate
		# the document; classification only.
		if auth and is_legacy_uvr_float_valuation_rate(auth, qty, rate_f, currency):
			legacy_uvr_float = True
			diag["legacy_uvr_float_valuation_rate"] = rate_f
			rate_f = flt(integer_valuation_rate_from_amount(auth, qty, currency))
		else:
			return {
				**diag,
				"class": "B",
				"reason": "non_integer_rate_under_irr_contract",
				"residual": amount_rate_qty_residual(auth, qty, rate_f, currency),
				"expected_valuation_rate": rounded_rate,
				"expected_amount": round_row_amount(qty, rounded_rate, currency),
			}

	derived = flt(round_row_amount(qty, rate_f, currency))
	residual = flt(amount_rate_qty_residual(auth, qty, rate_f, currency))
	rate_from_amount = flt(integer_valuation_rate_from_amount(auth, qty, currency)) if auth else None

	diag.update(
		{
			"rate_derived_amount": derived,
			"residual": residual,
			"expected_valuation_rate": rate_from_amount,
			"expected_amount": derived,
		}
	)

	if not residual:
		return {**diag, "class": "skip", "reason": "zero_residual"}

	# Provenance: amount-authoritative integer VR pipeline (SE compose / SR amount auth)
	# Also covers legacy UVR float rates coerced above to integer_from_amount.
	if rate_from_amount is not None and rate_f == rate_from_amount:
		bound = _path_derived_bound(qty)
		if abs(residual) >= bound:
			return {
				**diag,
				"class": "B",
				"reason": "provenance_matched_but_exceeds_path_derived_bound",
				"path_derived_bound": bound,
			}
		reason = (
			"amount_authoritative_legacy_uvr_float_rate"
			if legacy_uvr_float
			else "amount_authoritative_integer_valuation_rate"
		)
		return {
			**diag,
			"class": "A",
			"reason": reason,
			"path_derived_bound": bound,
		}

	# Provenance: rate-first product should equal amount → residual must be 0; else mismatch
	if auth == derived:
		return {**diag, "class": "skip", "reason": "auth_equals_rate_first_product"}

	return {
		**diag,
		"class": "B",
		"reason": "amount_rate_mismatch_not_reproducible_by_approved_pipeline",
	}


def enrich_candidate_dimensions(candidate: dict, row) -> dict:
	candidate["dimensions"] = _row_dimension_values(row)
	return candidate


def purchase_receipt_valuation_stock_qty(row) -> float:
	"""Stock-UOM qty used as the divisor in BuyingController.update_valuation_rate.

	Mirrors erpnext.controllers.buying_controller.BuyingController.update_valuation_rate:
	``qty_in_stock_uom = qty * conversion_factor`` (rejected_qty fallback when qty is 0).
	ERPNext does not expose a reusable helper for this divisor.
	"""
	conversion_factor = flt(row.get("conversion_factor"))
	if not conversion_factor:
		conversion_factor = 1.0
	qty_in_stock_uom = flt(flt(row.get("qty")) * conversion_factor)
	if not qty_in_stock_uom and row.get("rejected_qty"):
		qty_in_stock_uom = flt(flt(row.get("rejected_qty")) * conversion_factor)
	return qty_in_stock_uom


def get_purchase_receipt_stock_valuation_eligible_item_codes(doc) -> frozenset[str]:
	"""Item codes ERPNext UVR stock-values on a Purchase Receipt.

	Mirrors ``BuyingController.update_valuation_rate``::

	    stock_and_asset_items = self.get_stock_items() + self.get_asset_items()

	Prefer ERPNext document methods when present (AccountsController.get_stock_items
	+ BuyingController.get_asset_items). Fallback mirrors the same Item / row flags
	when the caller is a lightweight stub without those methods.

	Non-stock, non-asset service rows (e.g. subcontracting auto-PR service lines)
	are intentionally excluded — ERPNext forces ``valuation_rate = 0`` for them.
	"""
	has_stock_api = hasattr(doc, "get_stock_items") and callable(doc.get_stock_items)
	has_asset_api = hasattr(doc, "get_asset_items") and callable(doc.get_asset_items)
	if has_stock_api and has_asset_api:
		try:
			stock = list(doc.get_stock_items() or [])
			assets = list(doc.get_asset_items() or [])
			return frozenset(code for code in (stock + assets) if code)
		except Exception:
			pass

	# Fallback mirror: Item.is_stock_item / Item.is_fixed_asset / row.is_fixed_asset
	codes = {
		row.get("item_code")
		for row in (doc.get("items") or [])
		if row.get("item_code")
	}
	if not codes:
		return frozenset()

	eligible: set[str] = set()
	try:
		eligible.update(
			frappe.db.get_values(
				"Item",
				{"name": ["in", list(codes)], "is_stock_item": 1},
				pluck="name",
				cache=True,
			)
			or []
		)
		eligible.update(
			frappe.db.get_values(
				"Item",
				{"name": ["in", list(codes)], "is_fixed_asset": 1},
				pluck="name",
				cache=True,
			)
			or []
		)
	except Exception:
		pass

	for row in doc.get("items") or []:
		code = row.get("item_code")
		if code and cint(row.get("is_fixed_asset")):
			eligible.add(code)

	return frozenset(eligible)


def is_purchase_receipt_row_stock_valuation_eligible(row, eligible_item_codes: frozenset[str]) -> bool:
	"""True when the PR row is in ERPNext's UVR stock/asset eligibility set."""
	code = row.get("item_code") if hasattr(row, "get") else getattr(row, "item_code", None)
	return bool(code) and code in eligible_item_codes


def purchase_receipt_stock_valuation_amount(row, doc=None) -> float:
	"""Authoritative PR stock valuation amount (numerator of valuation_rate).

	ERPNext has no reusable public API for this sum. The formula is inlined in
	``BuyingController.update_valuation_rate``
	(``erpnext/controllers/buying_controller.py``). This helper mirrors that
	numerator using fields ERPNext already computed on the item row
	(``base_net_amount`` / internal transfer net, ``item_tax_amount``,
	``landed_cost_voucher_amount``, …). It does **not** re-derive purchase taxes
	or landed-cost allocation.
	"""
	# Net leg — same branching as BuyingController.update_valuation_rate
	# Use flt(...) for truthiness so incomplete mocks / empty strings do not
	# accidentally select the internal-transfer or rejected branches.
	sir = row.get("sales_incoming_rate")
	if sir not in (None, "") and flt(sir):
		net_rate = flt(row.get("qty")) * flt(sir)
	else:
		net_rate = flt(row.get("base_net_amount"))
		rejected_qty = flt(row.get("rejected_qty"))
		if not net_rate and rejected_qty:
			# Match ERPNext: only when Buying Settings allow rejected valuation.
			try:
				allow_rejected = frappe.get_single_value(
					"Buying Settings", "set_valuation_rate_for_rejected_materials"
				)
			except Exception:
				allow_rejected = 0
			if allow_rejected:
				net_rate = rejected_qty * flt(row.get("net_rate"))

	item_tax_amount = flt(row.get("item_tax_amount"))
	lcv = flt(row.get("landed_cost_voucher_amount"))

	# Old subcontract branch includes rm_supp_cost and omits PI rate difference.
	if doc is not None and cint(doc.get("is_old_subcontracting_flow")):
		return net_rate + item_tax_amount + flt(row.get("rm_supp_cost")) + lcv

	return (
		net_rate
		+ item_tax_amount
		+ lcv
		+ flt(row.get("amount_difference_with_purchase_invoice"))
	)


def classify_document_residuals(doc) -> tuple[list[dict], list[dict]]:
	"""Collect and classify residual candidates for supported doctypes."""
	from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
		_transfer_qty,
		collect_document_residuals,
		round_off_signed_debit,
		stock_entry_excludes_irr_residual_round_off,
	)

	if stock_entry_excludes_irr_residual_round_off(doc):
		return [], []

	ccy = get_company_currency(doc.company)
	class_a: list[dict] = []
	class_b: list[dict] = []

	if doc.doctype == "Purchase Receipt":
		eligible = get_purchase_receipt_stock_valuation_eligible_item_codes(doc)
		for row in doc.get("items") or []:
			if not is_purchase_receipt_row_stock_valuation_eligible(row, eligible):
				continue
			qty = purchase_receipt_valuation_stock_qty(row)
			auth = purchase_receipt_stock_valuation_amount(row, doc)
			rate = row.get("valuation_rate")
			classified = classify_amount_rate_residual(
				qty=qty,
				authoritative_amount=auth,
				valuation_rate=rate,
				currency=ccy,
				item_code=row.get("item_code"),
				idx=row.get("idx"),
				row_name=row.get("name"),
				extra={
					"base_rate": row.get("base_rate"),
					"base_amount": row.get("base_amount"),
					"base_net_amount": row.get("base_net_amount"),
					"amount": row.get("amount"),
					"item_tax_amount": row.get("item_tax_amount"),
					"landed_cost_voucher_amount": row.get("landed_cost_voucher_amount"),
					"conversion_factor": row.get("conversion_factor"),
					"stock_qty": qty,
				},
			)
			enrich_candidate_dimensions(classified, row)
			_bucket(classified, class_a, class_b, incoming=True, currency=ccy)

	elif doc.doctype == "Stock Entry":
		for row in doc.get("items") or []:
			if row.get("s_warehouse") and row.get("t_warehouse"):
				continue
			qty = _transfer_qty(row)
			auth = flt(row.get("amount"))
			rate = row.get("valuation_rate")
			classified = classify_amount_rate_residual(
				qty=qty,
				authoritative_amount=auth,
				valuation_rate=rate,
				currency=ccy,
				item_code=row.get("item_code"),
				idx=row.get("idx"),
				row_name=row.get("name"),
				extra={
					"basic_rate": row.get("basic_rate"),
					"basic_amount": row.get("basic_amount"),
					"additional_cost": row.get("additional_cost"),
					"landed_cost_voucher_amount": row.get("landed_cost_voucher_amount"),
				},
			)
			enrich_candidate_dimensions(classified, row)
			incoming = bool(row.get("t_warehouse")) and not row.get("s_warehouse")
			_bucket(classified, class_a, class_b, incoming=incoming, currency=ccy)

	elif doc.doctype == "Stock Reconciliation":
		# Keep SR movement residual collection, then classify each resulting residual.
		for r in collect_document_residuals(doc):
			row = _find_row(doc, r.get("row_name"))
			classified = classify_amount_rate_residual(
				qty=r.get("qty"),
				authoritative_amount=flt(r.get("rate_derived_amount")) + flt(r.get("residual")),
				valuation_rate=r.get("valuation_rate"),
				currency=ccy,
				item_code=r.get("item_code"),
				idx=r.get("idx"),
				row_name=r.get("row_name"),
			)
			# Prefer explicit residual from SR collector when provenance matches amount path
			if classified.get("class") == "A":
				classified["residual"] = r.get("residual")
			if row:
				enrich_candidate_dimensions(classified, row)
			else:
				classified["dimensions"] = {}
			_bucket(
				classified,
				class_a,
				class_b,
				incoming=bool(r.get("incoming")),
				currency=ccy,
				preset_debit=r.get("round_off_debit"),
			)

	return class_a, class_b


def _find_row(doc, row_name):
	if not row_name:
		return None
	for row in doc.get("items") or []:
		if row.get("name") == row_name:
			return row
	return None


def _bucket(
	classified: dict,
	class_a: list,
	class_b: list,
	*,
	incoming: bool,
	currency: str,
	preset_debit=None,
):
	from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import round_off_signed_debit

	cls = classified.get("class")
	if cls == "skip":
		return
	residual = flt(classified.get("residual"))
	classified["incoming"] = incoming
	classified["round_off_debit"] = (
		flt(preset_debit)
		if preset_debit is not None and cls == "A"
		else round_off_signed_debit(residual, incoming=incoming)
	)
	classified["round_off_debit"] = round_currency(classified["round_off_debit"], currency)
	if cls == "A":
		class_a.append(classified)
	elif cls == "B":
		class_b.append(classified)


def _format_class_b_message(doc, rows: list[dict]) -> str:
	parts = [
		_(
			"IRR rate-rounding residual rejected: valuation inconsistency (Class B). "
			"Round Off Account and Stock Adjustment Account must not absorb this gap."
		)
	]
	parts.append(_("Voucher: {0} {1}").format(doc.doctype, doc.name or _("new")))
	for r in rows[:8]:
		parts.append(
			_(
				"Row {idx} item {item}: qty={qty}, valuation_rate={rate}, "
				"amount={auth}, expected_rate={exp_rate}, expected_amount={exp_amt}, "
				"residual={residual}, reason={reason}"
			).format(
				idx=r.get("idx"),
				item=r.get("item_code"),
				qty=r.get("qty"),
				rate=r.get("valuation_rate"),
				auth=r.get("authoritative_amount"),
				exp_rate=r.get("expected_valuation_rate"),
				exp_amt=r.get("expected_amount"),
				residual=r.get("residual"),
				reason=r.get("reason"),
			)
		)
	return "\n".join(parts)


def get_company_round_off_dimension_defaults(company: str) -> dict[str, str]:
	"""Map fieldname → default_value from Company child table (no AD defaults)."""
	if not frappe.db.exists("DocType", "Round Off Dimension Default"):
		return {}
	rows = frappe.get_all(
		"Round Off Dimension Default",
		filters={"parent": company, "parenttype": "Company"},
		fields=["accounting_dimension", "default_value"],
		order_by="idx asc",
	)
	out: dict[str, str] = {}
	for row in rows:
		fn = (row.accounting_dimension or "").strip()
		if not fn or fn in ("cost_center", "account"):
			continue
		if row.default_value:
			out[fn] = row.default_value
	return out


def resolve_round_off_dimensions(
	*,
	doc,
	company: str,
	round_off_account: str,
	class_a_rows: list[dict],
) -> dict[str, Any]:
	"""Header → unique Class A row value → Company Round Off Dimension Defaults → fail."""
	from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
		get_checks_for_pl_and_bs_accounts,
	)

	report_type = frappe.get_cached_value("Account", round_off_account, "report_type")
	meta = frappe.get_meta(doc.doctype)
	company_defaults = get_company_round_off_dimension_defaults(company)
	resolved: dict[str, Any] = {}

	# Optional: copy project from header when present (non-mandatory convenience)
	if meta.has_field("project") and doc.get("project"):
		resolved["project"] = doc.get("project")

	for dimension in get_checks_for_pl_and_bs_accounts():
		if dimension.company != company:
			continue
		fieldname = dimension.fieldname
		if fieldname in ("cost_center", "account"):
			continue
		mandatory = (report_type == "Profit and Loss" and dimension.mandatory_for_pl) or (
			report_type == "Balance Sheet" and dimension.mandatory_for_bs
		)
		if not mandatory:
			continue

		# 1) Header per field (do not require all dimensions on meta)
		if meta.has_field(fieldname) and doc.get(fieldname):
			resolved[fieldname] = doc.get(fieldname)
			continue

		# 2) Unique non-empty among Class A residual source rows
		values = {
			r.get("dimensions", {}).get(fieldname)
			for r in class_a_rows
			if r.get("dimensions", {}).get(fieldname) not in (None, "")
		}
		if len(values) == 1:
			resolved[fieldname] = next(iter(values))
			continue

		# 3) Company Round Off Dimension Defaults (never AD default_dimension)
		if company_defaults.get(fieldname):
			resolved[fieldname] = company_defaults[fieldname]
			continue

		# 4) Fail
		frappe.throw(
			_(
				"Mandatory accounting dimension {0} is missing for IRR Round Off residual on "
				"Company {1}. Set it on the voucher, ensure a single value on residual rows, "
				"or configure Company → Round Off Dimension Defaults."
			).format(frappe.bold(dimension.label or fieldname), frappe.bold(company)),
			title=_("Missing Round Off Dimension"),
		)

	return resolved


def evaluate_irr_rate_rounding_residual(
	doc,
	gl_entries=None,
	context=None,
) -> ResidualDecision:
	"""Shared validate/apply/RIV decision for IRR rate-rounding residual only."""
	from erpnext_extensions.iran_accounting.domain.irr_rounding_residual import (
		SUPPORTED_RESIDUAL_DOCTYPES,
		_pick_adjustable_non_stock_leg,
		_protected_reclass_accounts,
		resolve_company_round_off,
		stock_entry_excludes_irr_residual_round_off,
		validate_round_off_configuration,
	)

	_ = context
	decision = ResidualDecision()

	if not getattr(doc, "company", None) or not is_irr_company(doc.company):
		decision.status = STATUS_BYPASS
		decision.messages.append("non_irr_or_missing_company")
		return decision

	if doc.doctype not in SUPPORTED_RESIDUAL_DOCTYPES:
		decision.status = STATUS_BYPASS
		decision.messages.append("unsupported_doctype")
		return decision

	if stock_entry_excludes_irr_residual_round_off(doc):
		decision.status = STATUS_BYPASS
		decision.messages.append("manufacture_repack_excluded_use_stock_adjustment")
		return decision

	if doc.doctype == "Stock Entry":
		from erpnext_extensions.iran_accounting.zero_value_transfer import (
			_should_force_balanced_transfer_gl,
		)

		precision = 0
		if hasattr(doc, "get_debit_field_precision"):
			precision = doc.get_debit_field_precision()
		if _should_force_balanced_transfer_gl(doc, precision):
			decision.status = STATUS_BYPASS
			decision.messages.append("zero_value_transfer_gl_path")
			return decision

	class_a, class_b = classify_document_residuals(doc)
	decision.class_a_rows = class_a
	decision.class_b_rows = class_b
	decision.diagnostics = list(class_b) + list(class_a)

	if class_b:
		decision.status = STATUS_CLASS_B
		decision.messages.append(_format_class_b_message(doc, class_b))
		return decision

	ccy = get_company_currency(doc.company)
	net = round_currency(sum(flt(r.get("round_off_debit")) for r in class_a), ccy)
	decision.net_signed_debit = flt(net)

	if not decision.net_signed_debit:
		decision.status = STATUS_BYPASS
		decision.messages.append("net_class_a_zero")
		return decision

	# Net Class A != 0 → resolve Round Off masters (not before)
	try:
		cfg = resolve_company_round_off(doc.company, require=True)
		validate_round_off_configuration(doc.company, cfg["account"], cfg["cost_center"])
	except Exception as e:
		decision.status = STATUS_CONFIG
		decision.messages.append(str(e))
		return decision

	decision.round_off_account = cfg["account"]
	decision.round_off_cost_center = cfg["cost_center"]

	try:
		decision.dimensions = resolve_round_off_dimensions(
			doc=doc,
			company=doc.company,
			round_off_account=cfg["account"],
			class_a_rows=class_a,
		)
	except Exception as e:
		decision.status = STATUS_CONFIG
		decision.messages.append(str(e))
		return decision

	if gl_entries is None:
		# Preflight without GL: config+dims OK; partner checked at apply.
		decision.status = STATUS_READY
		decision.partner_checked = False
		decision.messages.append("ready_pending_partner_at_apply")
		return decision

	protected = _protected_reclass_accounts(doc)
	partner = _pick_adjustable_non_stock_leg(
		gl_entries,
		doc.company,
		cfg["account"],
		protected_accounts=protected,
		reclass_magnitude=decision.net_signed_debit,
	)
	decision.partner_checked = True
	if not partner:
		examined = []
		for entry in gl_entries:
			acc = entry.get("account")
			examined.append(
				{
					"account": acc,
					"debit": entry.get("debit"),
					"credit": entry.get("credit"),
				}
			)
		decision.status = STATUS_PARTNER
		examined_txt = ", ".join(
			f"{e['account']}(D={e['debit']}/C={e['credit']})" for e in examined[:20]
		)
		protected_txt = ", ".join(sorted(protected)) if protected else "none"
		decision.messages.append(
			"No safe non-stock GL partner is available to reclassify the IRR Round Off residual.\n"
			f"Voucher: {doc.doctype} {doc.name or 'new'}\n"
			f"Net residual (signed debit): {decision.net_signed_debit}\n"
			f"Round Off Account: {cfg['account']}\n"
			"Stock Adjustment Account must not be used as a fallback.\n"
			f"Protected Additional Cost accounts: {protected_txt}\n"
			f"GL accounts examined: {examined_txt}"
		)
		return decision

	decision.partner = partner
	decision.status = STATUS_READY
	decision.messages.append("ready")
	return decision


def raise_residual_decision(decision: ResidualDecision) -> None:
	"""Throw for non-ready error statuses."""
	if not decision.is_error:
		return
	title = {
		STATUS_CLASS_B: _("IRR Residual Classification"),
		STATUS_CONFIG: _("IRR Round Off Configuration"),
		STATUS_PARTNER: _("IRR Round Off Partner"),
	}.get(decision.status, _("IRR Round Off"))
	msg = "\n".join(decision.messages) or decision.status
	frappe.throw(msg, title=title)
