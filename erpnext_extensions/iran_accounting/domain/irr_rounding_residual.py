# Copyright (c) 2026, ERPNext Extensions contributors
"""IRR integer-rate rounding residual → destination account by voucher domain.

Contract
--------
rate_derived_amount = ROUND_HALF_UP(qty × integer_valuation_rate, 0)
authoritative_amount = finalized row / SLE movement amount
rounding_residual = authoritative_amount − rate_derived_amount

Inventory GL and SLE keep authoritative_amount.
When residual ≠ 0, post an explicit residual GL leg and reclassify the same
magnitude from a non-Stock GL leg so the voucher stays balanced without
changing inventory value.

Destination (v5.5.45 accounting policy):
  Inventory valuation vouchers (Stock Entry / Purchase Receipt / Stock
  Reconciliation) → Company.stock_adjustment_account (never Financial Round Off).
  Non-inventory / financial Round Off paths remain on Company.round_off_account.

Debit/credit (signed debit on residual destination; positive = Debit):
  incoming inventory movement: round_off_debit = −residual
  outgoing inventory movement: round_off_debit = +residual

Example (incoming, amount=1371, qty=7, rate=196):
  residual = 1371 − 1372 = −1
  round_off_debit = −(−1) = +1  → Debit Stock Adjustment 1 (inventory)
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.domain.currency import (
	amount_rate_qty_residual,
	get_company_currency,
	is_irr_company,
	round_currency,
	round_row_amount,
)

IRR_RATE_ROUNDING_RESIDUAL_REMARK = "IRR rate rounding residual"
# Explicit in-memory GL-map marker (not a DocType field). Durable twin after persist is
# Company.round_off_account + IRR_RATE_ROUNDING_RESIDUAL_REMARK in remarks.
IRR_RATE_ROUNDING_RESIDUAL_MARKER = "_irr_rate_rounding_residual"

# v5.5.45 policy: inventory valuation Class A residuals → Stock Adjustment (not Financial Round Off).
IRR_INVENTORY_VALUATION_RESIDUAL_REMARK = "IRR inventory valuation residual"
IRR_INVENTORY_VALUATION_RESIDUAL_MARKER = "_irr_inventory_valuation_residual"

SUPPORTED_RESIDUAL_DOCTYPES = (
	"Stock Entry",
	"Purchase Receipt",
	"Stock Reconciliation",
)

# Inventory valuation vouchers: residual destination is Company.stock_adjustment_account.
INVENTORY_VALUATION_RESIDUAL_DOCTYPES = frozenset(SUPPORTED_RESIDUAL_DOCTYPES)

# Manufacture / Repack: incoming≠outgoing from integer-rate policy is a stock valuation
# difference → Company Stock Adjustment (vanilla ERPNext). Round Off must NOT compensate
# IRR rate residuals on these purposes.
IRR_RESIDUAL_ROUND_OFF_EXCLUDED_STOCK_ENTRY_PURPOSES = frozenset(
	{
		"Manufacture",
		"Repack",
	}
)


def stock_entry_excludes_irr_residual_round_off(doc) -> bool:
	"""True when IRR residual Round Off must not touch this Stock Entry."""
	if getattr(doc, "doctype", None) != "Stock Entry":
		return False
	return (doc.get("purpose") or "") in IRR_RESIDUAL_ROUND_OFF_EXCLUDED_STOCK_ENTRY_PURPOSES


def uses_stock_adjustment_for_irr_residual(doc) -> bool:
	"""True when Class A residual must post to Stock Adjustment (inventory policy v5.5.45)."""
	return getattr(doc, "doctype", None) in INVENTORY_VALUATION_RESIDUAL_DOCTYPES


def resolve_company_stock_adjustment_residual(company: str, *, require: bool = True) -> dict[str, str | None]:
	"""Stock Adjustment account + cost center for inventory valuation residuals.

	Cost center reuses Company.round_off_cost_center (sole dimension authority for residual
	legs) so inventory residual CC stays consistent with prior Round Off dimension policy.
	"""
	sa_account = frappe.get_cached_value("Company", company, "stock_adjustment_account")
	cost_center = frappe.get_cached_value("Company", company, "round_off_cost_center")
	if require:
		validate_stock_adjustment_residual_configuration(company, sa_account, cost_center)
	return {"account": sa_account, "cost_center": cost_center}


def validate_stock_adjustment_residual_configuration(
	company: str, account: str | None = None, cost_center: str | None = None
) -> None:
	"""Fail closed when inventory residual posting requires Stock Adjustment masters."""
	if account is None:
		account = frappe.get_cached_value("Company", company, "stock_adjustment_account")
	if cost_center is None:
		cost_center = frappe.get_cached_value("Company", company, "round_off_cost_center")

	if not account:
		frappe.throw(
			_(
				"Please set Stock Adjustment Account on Company {0} for inventory "
				"valuation residuals. Financial Round Off must not be used for stock valuation."
			).format(frappe.bold(company)),
			title=_("Missing Stock Adjustment Account"),
		)
	if not cost_center:
		frappe.throw(
			_(
				"Please set Round Off Cost Center on Company {0} (used as residual cost center "
				"for inventory Stock Adjustment legs)."
			).format(frappe.bold(company)),
			title=_("Missing Residual Cost Center"),
		)

	acc = frappe.db.get_value(
		"Account",
		account,
		["name", "company", "is_group", "disabled"],
		as_dict=True,
	)
	if not acc:
		frappe.throw(_("Stock Adjustment Account {0} does not exist.").format(frappe.bold(account)))
	if acc.company != company:
		frappe.throw(
			_("Stock Adjustment Account {0} does not belong to Company {1}.").format(
				frappe.bold(account), frappe.bold(company)
			)
		)
	if cint(acc.is_group):
		frappe.throw(_("Stock Adjustment Account {0} must not be a group.").format(frappe.bold(account)))
	if cint(acc.disabled):
		frappe.throw(_("Stock Adjustment Account {0} is disabled.").format(frappe.bold(account)))

	company_currency = frappe.get_cached_value("Company", company, "default_currency")
	account_currency = frappe.get_cached_value("Account", account, "account_currency")
	if company_currency and account_currency and account_currency != company_currency:
		frappe.throw(
			_(
				"Stock Adjustment Account {0} currency {1} is incompatible with Company {2} currency {3}."
			).format(
				frappe.bold(account),
				frappe.bold(account_currency),
				frappe.bold(company),
				frappe.bold(company_currency),
			),
			title=_("Stock Adjustment Currency"),
		)

	cc = frappe.db.get_value(
		"Cost Center",
		cost_center,
		["name", "company", "disabled", "is_group"],
		as_dict=True,
	)
	if not cc:
		frappe.throw(_("Residual Cost Center {0} does not exist.").format(frappe.bold(cost_center)))
	if cc.company != company:
		frappe.throw(
			_("Residual Cost Center {0} does not belong to Company {1}.").format(
				frappe.bold(cost_center), frappe.bold(company)
			)
		)
	if cint(cc.disabled):
		frappe.throw(_("Residual Cost Center {0} is disabled.").format(frappe.bold(cost_center)))
	if cint(cc.is_group):
		frappe.throw(_("Residual Cost Center {0} must not be a group.").format(frappe.bold(cost_center)))


def is_irr_inventory_valuation_residual_gl(
	entry,
	*,
	company: str | None = None,
	stock_adjustment_account: str | None = None,
) -> bool:
	"""Detect inventory valuation residual GL (Stock Adjustment destination)."""
	if not entry:
		return False
	if entry.get(IRR_INVENTORY_VALUATION_RESIDUAL_MARKER):
		return True
	account = entry.get("account")
	if not account:
		return False
	if stock_adjustment_account is None and company:
		try:
			stock_adjustment_account = frappe.get_cached_value(
				"Company", company, "stock_adjustment_account"
			)
		except Exception:
			stock_adjustment_account = None
	if not stock_adjustment_account or account != stock_adjustment_account:
		return False
	return IRR_INVENTORY_VALUATION_RESIDUAL_REMARK in (entry.get("remarks") or "")


def is_irr_rate_rounding_residual_gl(
	entry,
	*,
	company: str | None = None,
	round_off_account: str | None = None,
) -> bool:
	"""Structural detection of IRR residual GL rows (Round Off or inventory Stock Adjustment).

	Prefer explicit in-memory markers. After persist, require destination account + remark.
	Inventory vouchers use Stock Adjustment; financial Round Off remark is still recognized
	for legacy rows / non-inventory paths.
	"""
	if not entry:
		return False
	if entry.get(IRR_RATE_ROUNDING_RESIDUAL_MARKER) or entry.get(
		IRR_INVENTORY_VALUATION_RESIDUAL_MARKER
	):
		return True

	if is_irr_inventory_valuation_residual_gl(entry, company=company):
		return True

	account = entry.get("account")
	if not account:
		return False
	if round_off_account is None and company:
		try:
			round_off_account = frappe.get_cached_value("Company", company, "round_off_account")
		except Exception:
			round_off_account = None
	if not round_off_account or account != round_off_account:
		return False
	return IRR_RATE_ROUNDING_RESIDUAL_REMARK in (entry.get("remarks") or "")


def assert_irr_residual_round_off_masters(entry, company: str) -> None:
	"""Fail closed: residual Round Off must use Company Round Off Account + Cost Center only."""
	cfg = resolve_company_round_off(company, require=True)
	if entry.get("account") != cfg["account"]:
		frappe.throw(
			_("IRR Round Off GL account {0} must equal Company.round_off_account {1}.").format(
				frappe.bold(entry.get("account")), frappe.bold(cfg["account"])
			),
			title=_("IRR Round Off Account"),
		)
	if entry.get("cost_center") != cfg["cost_center"]:
		frappe.throw(
			_("IRR Round Off GL cost center {0} must equal Company.round_off_cost_center {1}.").format(
				frappe.bold(entry.get("cost_center")), frappe.bold(cfg["cost_center"])
			),
			title=_("IRR Round Off Cost Center"),
		)


def stamp_irr_residual_round_off_masters(entry, company: str) -> None:
	"""Set account/cost_center solely from Company Round Off masters + residual marker."""
	cfg = resolve_company_round_off(company, require=True)
	entry["account"] = cfg["account"]
	entry["cost_center"] = cfg["cost_center"]
	entry[IRR_RATE_ROUNDING_RESIDUAL_MARKER] = 1
	assert_irr_residual_round_off_masters(entry, company)


def stamp_irr_inventory_valuation_residual_masters(entry, company: str) -> None:
	"""Set account/cost_center from Stock Adjustment masters for inventory residuals."""
	cfg = resolve_company_stock_adjustment_residual(company, require=True)
	entry["account"] = cfg["account"]
	entry["cost_center"] = cfg["cost_center"]
	entry[IRR_INVENTORY_VALUATION_RESIDUAL_MARKER] = 1
	entry.pop(IRR_RATE_ROUNDING_RESIDUAL_MARKER, None)
	if entry.get("account") != cfg["account"]:
		frappe.throw(
			_("Inventory residual GL account {0} must equal Company.stock_adjustment_account {1}.").format(
				frappe.bold(entry.get("account")), frappe.bold(cfg["account"])
			),
			title=_("Stock Adjustment Account"),
		)
	if entry.get("cost_center") != cfg["cost_center"]:
		frappe.throw(
			_("Inventory residual GL cost center {0} must equal residual cost center {1}.").format(
				frappe.bold(entry.get("cost_center")), frappe.bold(cfg["cost_center"])
			),
			title=_("Stock Adjustment Cost Center"),
		)


def rate_derived_amount(qty, valuation_rate, currency: str | None):
	"""ROUND_HALF_UP(qty × ROUND_HALF_UP(valuation_rate))."""
	return round_row_amount(qty, valuation_rate, currency)


def compute_rounding_residual(authoritative_amount, qty, valuation_rate, currency: str | None):
	"""authoritative_amount − rate_derived_amount (IRR integer)."""
	return amount_rate_qty_residual(authoritative_amount, qty, valuation_rate, currency)


def round_off_signed_debit(residual, *, incoming: bool) -> float:
	"""Positive → Debit Round Off; negative → Credit Round Off."""
	return flt(-residual if incoming else residual)


def resolve_company_round_off(company: str, *, require: bool = True) -> dict[str, str | None]:
	"""Resolve Company.round_off_account / round_off_cost_center (no hard-coded names)."""
	account, cost_center = frappe.get_cached_value(
		"Company", company, ["round_off_account", "round_off_cost_center"]
	) or (None, None)
	if require:
		validate_round_off_configuration(company, account, cost_center)
	return {"account": account, "cost_center": cost_center}


def validate_round_off_configuration(company: str, account: str | None = None, cost_center: str | None = None) -> None:
	"""Fail closed when residual posting requires Round Off masters."""
	if account is None and cost_center is None:
		account, cost_center = frappe.get_cached_value(
			"Company", company, ["round_off_account", "round_off_cost_center"]
		) or (None, None)

	if not account:
		frappe.throw(
			_("Please set Round Off Account on Company {0} for IRR rate rounding residuals.").format(
				frappe.bold(company)
			),
			title=_("Missing Round Off Account"),
		)
	if not cost_center:
		frappe.throw(
			_("Please set Round Off Cost Center on Company {0} for IRR rate rounding residuals.").format(
				frappe.bold(company)
			),
			title=_("Missing Round Off Cost Center"),
		)

	acc = frappe.db.get_value(
		"Account",
		account,
		["name", "company", "is_group", "disabled", "account_type"],
		as_dict=True,
	)
	if not acc:
		frappe.throw(_("Round Off Account {0} does not exist.").format(frappe.bold(account)))
	if acc.company != company:
		frappe.throw(
			_("Round Off Account {0} does not belong to Company {1}.").format(
				frappe.bold(account), frappe.bold(company)
			)
		)
	if cint(acc.is_group):
		frappe.throw(_("Round Off Account {0} must not be a group.").format(frappe.bold(account)))
	if cint(acc.disabled):
		frappe.throw(_("Round Off Account {0} is disabled.").format(frappe.bold(account)))

	cc = frappe.db.get_value(
		"Cost Center",
		cost_center,
		["name", "company", "disabled", "is_group"],
		as_dict=True,
	)
	if not cc:
		frappe.throw(_("Round Off Cost Center {0} does not exist.").format(frappe.bold(cost_center)))
	if cc.company != company:
		frappe.throw(
			_("Round Off Cost Center {0} does not belong to Company {1}.").format(
				frappe.bold(cost_center), frappe.bold(company)
			)
		)
	if cint(cc.disabled):
		frappe.throw(_("Round Off Cost Center {0} is disabled.").format(frappe.bold(cost_center)))


def _transfer_qty(row) -> float:
	return flt(row.get("transfer_qty") if row.get("transfer_qty") not in (None, "") else row.get("qty"))


def collect_stock_entry_residuals(doc) -> list[dict[str, Any]]:
	"""Per-row residuals; skip dual-warehouse transfer rows (in/out cancel).

	Manufacture / Repack are excluded: valuation gaps use Stock Adjustment, not Round Off.
	"""
	if stock_entry_excludes_irr_residual_round_off(doc):
		return []
	ccy = get_company_currency(doc.company)
	out: list[dict[str, Any]] = []
	for row in doc.get("items") or []:
		if row.get("s_warehouse") and row.get("t_warehouse"):
			continue
		qty = _transfer_qty(row)
		auth = flt(row.get("amount"))
		rate = row.get("valuation_rate")
		if rate in (None, "") or not qty:
			continue
		residual = compute_rounding_residual(auth, qty, rate, ccy)
		if not residual:
			continue
		incoming = bool(row.get("t_warehouse")) and not row.get("s_warehouse")
		out.append(
			{
				"row_name": row.get("name"),
				"idx": row.get("idx"),
				"item_code": row.get("item_code"),
				"qty": qty,
				"valuation_rate": rate,
				"authoritative_amount": auth,
				"rate_derived_amount": rate_derived_amount(qty, rate, ccy),
				"residual": residual,
				"incoming": incoming,
				"round_off_debit": round_off_signed_debit(residual, incoming=incoming),
				"cost_center": row.get("cost_center"),
			}
		)
	return out


def collect_purchase_receipt_residuals(doc) -> list[dict[str, Any]]:
	"""Legacy collector (raw residual math). Prefer classify_document_residuals for 3.8.6+."""
	from erpnext_extensions.iran_accounting.domain.irr_residual_classification import (
		get_purchase_receipt_stock_valuation_eligible_item_codes,
		is_purchase_receipt_row_stock_valuation_eligible,
	)

	ccy = get_company_currency(doc.company)
	out: list[dict[str, Any]] = []
	eligible = get_purchase_receipt_stock_valuation_eligible_item_codes(doc)
	for row in doc.get("items") or []:
		if not is_purchase_receipt_row_stock_valuation_eligible(row, eligible):
			continue
		qty = flt(row.get("qty"))
		auth = flt(row.get("base_amount") if row.get("base_amount") not in (None, "") else row.get("amount"))
		rate = row.get("valuation_rate")
		if rate in (None, "") and row.get("base_rate") not in (None, ""):
			rate = row.get("base_rate")
		if not qty:
			continue
		# Include rate<=0 with non-zero amount so Class B classifier can see them.
		if rate in (None, ""):
			if not auth:
				continue
			residual = auth
			derived = 0
		elif flt(rate) <= 0 and auth:
			residual = auth
			derived = 0
		else:
			residual = compute_rounding_residual(auth, qty, rate, ccy)
			if not residual:
				continue
			derived = rate_derived_amount(qty, rate, ccy)
		out.append(
			{
				"row_name": row.get("name"),
				"idx": row.get("idx"),
				"item_code": row.get("item_code"),
				"qty": qty,
				"valuation_rate": rate if rate not in (None, "") else 0,
				"authoritative_amount": auth,
				"rate_derived_amount": derived,
				"residual": residual,
				"incoming": True,
				"round_off_debit": round_off_signed_debit(residual, incoming=True),
				"cost_center": row.get("cost_center"),
			}
		)
	return out


def collect_stock_reconciliation_residuals(doc) -> list[dict[str, Any]]:
	ccy = get_company_currency(doc.company)
	out: list[dict[str, Any]] = []
	for row in doc.get("items") or []:
		qty = flt(row.get("qty"))
		cur_qty = flt(row.get("current_qty"))
		auth_diff = flt(row.get("amount_difference"))
		new_res = compute_rounding_residual(row.get("amount"), qty, row.get("valuation_rate"), ccy) if qty else 0
		cur_res = (
			compute_rounding_residual(
				row.get("current_amount"), cur_qty, row.get("current_valuation_rate"), ccy
			)
			if cur_qty
			else 0
		)
		# Movement residual = new residual − current residual
		residual = round_currency(flt(new_res) - flt(cur_res), ccy)
		if not residual:
			continue
		incoming = auth_diff >= 0
		out.append(
			{
				"row_name": row.get("name"),
				"idx": row.get("idx"),
				"item_code": row.get("item_code"),
				"qty": qty,
				"valuation_rate": row.get("valuation_rate"),
				"authoritative_amount": auth_diff,
				"rate_derived_amount": round_currency(auth_diff - residual, ccy),
				"residual": residual,
				"incoming": incoming,
				"round_off_debit": round_off_signed_debit(residual, incoming=incoming),
				"cost_center": row.get("cost_center"),
			}
		)
	return out


def collect_document_residuals(doc) -> list[dict[str, Any]]:
	if doc.doctype == "Stock Entry":
		return collect_stock_entry_residuals(doc)
	if doc.doctype == "Purchase Receipt":
		return collect_purchase_receipt_residuals(doc)
	if doc.doctype == "Stock Reconciliation":
		return collect_stock_reconciliation_residuals(doc)
	return []


def document_has_rounding_residual(doc) -> bool:
	"""True when net Class A residual is non-zero (not Class B / not any-row)."""
	from erpnext_extensions.iran_accounting.domain.irr_residual_classification import (
		evaluate_irr_rate_rounding_residual,
	)

	decision = evaluate_irr_rate_rounding_residual(doc, gl_entries=None)
	return bool(flt(decision.net_signed_debit)) and not decision.class_b_rows


def assert_round_off_ready_if_needed(doc) -> None:
	"""Before submit: shared ResidualDecision (Class B / config). Partner checked at apply."""
	from erpnext_extensions.iran_accounting.domain.irr_residual_classification import (
		STATUS_BYPASS,
		STATUS_READY,
		evaluate_irr_rate_rounding_residual,
		raise_residual_decision,
	)

	if not is_irr_company(doc.company):
		return
	if doc.doctype not in SUPPORTED_RESIDUAL_DOCTYPES:
		return
	decision = evaluate_irr_rate_rounding_residual(doc, gl_entries=None)
	if decision.status == STATUS_BYPASS:
		return
	if decision.status == STATUS_READY:
		return
	raise_residual_decision(decision)


def strip_irr_rate_rounding_residual_gl(gl_map: list) -> list:
	"""Remove prior IRR residual Round Off rows (idempotent rebuild)."""
	if not gl_map:
		return gl_map
	company = None
	if gl_map:
		company = gl_map[0].get("company")
	kept = [
		entry
		for entry in gl_map
		if not is_irr_rate_rounding_residual_gl(entry, company=company)
	]
	gl_map[:] = kept
	return gl_map


def _is_stock_account(account: str | None) -> bool:
	if not account:
		return False
	return frappe.get_cached_value("Account", account, "account_type") == "Stock"


def _is_stock_adjustment_account(account: str | None, company: str | None = None) -> bool:
	"""Stock Adjustment holds inventory valuation differences — never reclassify into Round Off."""
	if not account:
		return False
	try:
		account_type = frappe.get_cached_value("Account", account, "account_type")
	except Exception:
		account_type = None
	if account_type == "Stock Adjustment":
		return True
	if company:
		try:
			company_adj = frappe.get_cached_value("Company", company, "stock_adjustment_account")
		except Exception:
			company_adj = None
		if company_adj and account == company_adj:
			return True
	return False


def _pick_adjustable_non_stock_leg(
	gl_map: list,
	company: str,
	round_off_account: str,
	*,
	protected_accounts: set[str] | None = None,
	forbidden_accounts: set[str] | None = None,
	reclass_magnitude: float = 0,
):
	"""Largest non-Stock, non-Round-Off, non-Stock-Adjustment monetary leg.

	Protected accounts (e.g. Stock Entry Additional Cost expense) may be used only
	when their magnitude strictly exceeds the reclass amount so the Add Cost GL
	remains visible.

	Forbidden accounts (e.g. Purchase Receipt GRNI / LCV clearing) are never used
	as residual partners — even when large — so purchase GRNI cannot absorb
	valuation rounding.
	"""
	protected = protected_accounts or set()
	forbidden = forbidden_accounts or set()
	need = abs(flt(reclass_magnitude))
	candidates = []
	for entry in gl_map:
		account = entry.get("account")
		if not account or account == round_off_account:
			continue
		if account in forbidden:
			continue
		if _is_stock_account(account):
			continue
		if _is_stock_adjustment_account(account, company):
			continue
		mag = max(flt(entry.get("debit")), flt(entry.get("credit")))
		if not mag:
			continue
		if account in protected and mag <= need:
			continue
		candidates.append((mag, entry))
	if not candidates:
		return None
	candidates.sort(key=lambda x: x[0], reverse=True)
	return candidates[0][1]


def _protected_reclass_accounts(doc) -> set[str]:
	"""Accounts that must remain visible on the voucher GL (not Round Off fodder).

	Purchase Receipt: never reclassify purchase GRNI / asset RBNB or LCV clearing
	legs into Stock Adjustment. Those are purchase/landed-cost clearing accounts —
	using them as residual partners creates unexplained GRNI residuals after PI.
	"""
	protected: set[str] = set()
	if getattr(doc, "doctype", None) == "Stock Entry":
		for row in doc.get("additional_costs") or []:
			if row.get("expense_account"):
				protected.add(row.get("expense_account"))
	if getattr(doc, "doctype", None) == "Purchase Receipt" and getattr(doc, "company", None):
		for field in (
			"stock_received_but_not_billed",
			"asset_received_but_not_billed",
		):
			try:
				acc = frappe.get_cached_value("Company", doc.company, field)
			except Exception:
				acc = None
			if acc:
				protected.add(acc)
		# LCV expense / clearing accounts on this PR rebuild
		try:
			if hasattr(doc, "get_item_account_wise_lcv_entries"):
				lcv_map = doc.get_item_account_wise_lcv_entries() or {}
				for entries in lcv_map.values():
					for entry in entries or []:
						exp = getattr(entry, "expense_account", None) or (
							entry.get("expense_account") if hasattr(entry, "get") else None
						)
						if exp:
							protected.add(exp)
		except Exception:
			pass
	return protected


def _apply_signed_debit_to_entry(entry, signed_debit: float, precision: int, currency: str) -> None:
	"""Add signed_debit to entry (positive increases debit / decreases credit)."""
	signed_debit = round_currency(signed_debit, currency)
	if not signed_debit:
		return
	if signed_debit > 0:
		if flt(entry.get("credit")):
			# reduce credit first
			credit = flt(entry.get("credit"))
			if credit >= signed_debit:
				entry["credit"] = round_currency(credit - signed_debit, currency)
				if entry.get("credit_in_account_currency") not in (None, ""):
					entry["credit_in_account_currency"] = entry["credit"]
			else:
				remainder = signed_debit - credit
				entry["credit"] = 0
				entry["credit_in_account_currency"] = 0
				entry["debit"] = round_currency(flt(entry.get("debit")) + remainder, currency)
				if entry.get("debit_in_account_currency") not in (None, ""):
					entry["debit_in_account_currency"] = entry["debit"]
		else:
			entry["debit"] = round_currency(flt(entry.get("debit")) + signed_debit, currency)
			if entry.get("debit_in_account_currency") not in (None, ""):
				entry["debit_in_account_currency"] = entry["debit"]
	else:
		credit_add = -signed_debit
		if flt(entry.get("debit")):
			debit = flt(entry.get("debit"))
			if debit >= credit_add:
				entry["debit"] = round_currency(debit - credit_add, currency)
				if entry.get("debit_in_account_currency") not in (None, ""):
					entry["debit_in_account_currency"] = entry["debit"]
			else:
				remainder = credit_add - debit
				entry["debit"] = 0
				entry["debit_in_account_currency"] = 0
				entry["credit"] = round_currency(flt(entry.get("credit")) + remainder, currency)
				if entry.get("credit_in_account_currency") not in (None, ""):
					entry["credit_in_account_currency"] = entry["credit"]
		else:
			entry["credit"] = round_currency(flt(entry.get("credit")) + credit_add, currency)
			if entry.get("credit_in_account_currency") not in (None, ""):
				entry["credit_in_account_currency"] = entry["credit"]


def _populate_round_off_dimensions(gle: dict, voucher_type: str, voucher_no: str, company: str) -> None:
	"""Deprecated path — use resolve_round_off_dimensions via ResidualDecision.

	Kept for older unit-test patches; applies per-field header inherit only (no AD defaults).
	"""
	from erpnext.accounts.doctype.accounting_dimension.accounting_dimension import (
		get_accounting_dimensions,
	)

	locked_cost_center = gle.get("cost_center")
	meta = frappe.get_meta(voucher_type)
	for dimension in get_accounting_dimensions():
		if dimension == "cost_center":
			continue
		if not meta.has_field(dimension):
			continue
		if voucher_no:
			value = frappe.db.get_value(voucher_type, voucher_no, dimension)
			if value:
				gle[dimension] = value
	if locked_cost_center:
		gle["cost_center"] = locked_cost_center


def apply_irr_rate_rounding_residual_gl(doc, gl_map: list | None) -> list | None:
	"""Post-process ERPNext GL map using shared ResidualDecision (3.8.6 / v5.5.45).

	Manufacture / Repack: no Class A residual layer — value_difference uses Stock Adjustment.
	Other inventory vouchers (Stock Entry / PR / SR): Class A residual → Stock Adjustment.
	Financial Round Off is never used for inventory valuation residuals.
	"""
	from erpnext_extensions.iran_accounting.domain.irr_residual_classification import (
		STATUS_BYPASS,
		STATUS_READY,
		evaluate_irr_rate_rounding_residual,
		raise_residual_decision,
	)

	if not gl_map:
		return gl_map

	decision = evaluate_irr_rate_rounding_residual(doc, gl_entries=gl_map)

	if decision.status == STATUS_BYPASS:
		strip_irr_rate_rounding_residual_gl(gl_map)
		return gl_map

	if decision.status != STATUS_READY:
		raise_residual_decision(decision)
		return gl_map

	net_ro_debit = flt(decision.net_signed_debit)
	use_sa = uses_stock_adjustment_for_irr_residual(doc)
	existing_ro = [
		e for e in gl_map if is_irr_rate_rounding_residual_gl(e, company=doc.company)
	]
	if existing_ro:
		for e in existing_ro:
			if use_sa:
				stamp_irr_inventory_valuation_residual_masters(e, doc.company)
			else:
				stamp_irr_residual_round_off_masters(e, doc.company)
			for fieldname, value in (decision.dimensions or {}).items():
				if fieldname != "cost_center" and value:
					e[fieldname] = value
		return gl_map

	cfg_account = decision.round_off_account
	cfg_cc = decision.round_off_cost_center
	ccy = get_company_currency(doc.company)
	precision = 0 if (ccy or "").upper() == "IRR" else 2

	adjustable = decision.partner
	if not adjustable:
		raise_residual_decision(decision)
		return gl_map

	_apply_signed_debit_to_entry(adjustable, -net_ro_debit, precision, ccy)

	template = gl_map[0]
	row_trace = ", ".join(
		f"row {r.get('idx')} {r.get('item_code')} residual={r.get('residual')}"
		for r in decision.class_a_rows[:8]
	)
	remark_prefix = (
		IRR_INVENTORY_VALUATION_RESIDUAL_REMARK if use_sa else IRR_RATE_ROUNDING_RESIDUAL_REMARK
	)
	ro = frappe._dict(
		{
			"account": cfg_account,
			"cost_center": cfg_cc,
			"company": doc.company,
			"posting_date": template.get("posting_date") or doc.get("posting_date"),
			"voucher_type": doc.doctype,
			"voucher_no": doc.name,
			"remarks": f"{remark_prefix}: {row_trace}",
			"is_opening": "No",
			"party_type": None,
			"party": None,
			"against_voucher_type": None,
			"against_voucher": None,
			"debit": abs(net_ro_debit) if net_ro_debit > 0 else 0,
			"credit": abs(net_ro_debit) if net_ro_debit < 0 else 0,
			"debit_in_account_currency": abs(net_ro_debit) if net_ro_debit > 0 else 0,
			"credit_in_account_currency": abs(net_ro_debit) if net_ro_debit < 0 else 0,
			"debit_in_transaction_currency": abs(net_ro_debit) if net_ro_debit > 0 else 0,
			"credit_in_transaction_currency": abs(net_ro_debit) if net_ro_debit < 0 else 0,
		}
	)
	for fieldname, value in (decision.dimensions or {}).items():
		if fieldname != "cost_center" and value:
			ro[fieldname] = value
	if template.get("finance_book"):
		ro["finance_book"] = template.get("finance_book")
	# Sole authority — never inherit row/leg/CCA/dimension CC.
	if use_sa:
		stamp_irr_inventory_valuation_residual_masters(ro, doc.company)
	else:
		ro[IRR_RATE_ROUNDING_RESIDUAL_MARKER] = 1
		stamp_irr_residual_round_off_masters(ro, doc.company)
	gl_map.append(ro)
	return gl_map


def expected_round_off_gl_totals(doc) -> dict[str, float]:
	"""Expected Round Off debit/credit from Class A residuals only (for validators/tests)."""
	from erpnext_extensions.iran_accounting.domain.irr_residual_classification import (
		evaluate_irr_rate_rounding_residual,
	)

	decision = evaluate_irr_rate_rounding_residual(doc, gl_entries=None)
	net = flt(decision.net_signed_debit)
	return {
		"net_signed_debit": net,
		"debit": abs(net) if net > 0 else 0.0,
		"credit": abs(net) if net < 0 else 0.0,
		"residuals": decision.class_a_rows,
		"class_b_rows": decision.class_b_rows,
		"status": decision.status,
	}


def fetch_irr_residual_gl_rows(voucher_type: str, voucher_no: str) -> list[dict]:
	rows = frappe.get_all(
		"GL Entry",
		filters={"voucher_type": voucher_type, "voucher_no": voucher_no, "is_cancelled": 0},
		fields=["name", "account", "debit", "credit", "cost_center", "remarks", "company"],
	)
	company = rows[0].get("company") if rows else None
	return [r for r in rows if is_irr_rate_rounding_residual_gl(r, company=company)]


def rebuild_irr_rate_rounding_residual_after_repost(doc) -> list[str]:
	"""After RIV/RAL amount/SLE reconcile: remake GL so residual Round Off is idempotent."""
	actions: list[str] = []
	if not is_irr_company(doc.company) or doc.docstatus != 1:
		return actions
	if doc.doctype not in SUPPORTED_RESIDUAL_DOCTYPES:
		return actions
	if not cint(frappe.get_cached_value("Company", doc.company, "enable_perpetual_inventory")):
		# ERPNext may use different flag; still attempt remake when make_gl_entries exists.
		pass

	if not hasattr(doc, "make_gl_entries"):
		return actions

	# Remake GL from document (cancels prior + posts fresh ERPNext + residual layer).
	from erpnext.accounts.general_ledger import make_reverse_gl_entries

	make_reverse_gl_entries(voucher_type=doc.doctype, voucher_no=doc.name)
	actions.append("reversed_gl_for_residual_rebuild")
	doc.make_gl_entries()
	actions.append("remade_gl_with_irr_rate_residual")
	return actions
