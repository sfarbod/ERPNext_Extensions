# Copyright (c) 2026, ERPNext Extensions contributors
"""Normalized Manufacture / Repack GL — residual-only Stock Adjustment.

Business rule
-------------
``621301`` (Stock Adjustment) must **never** act as a manufacturing clearing /
intermediary account. Ordinary Manufacture movements are:

* Inventory legs from SLE only (WIP / FG / SFG / scrap / by-product / co-product
  warehouse accounts).
* Additional Costs credited once to their OH / expense accounts (amounts already
  capitalized into incoming SLE via Iran allocation — do **not** debit a clearing
  account for the full OH again).
* Genuine valuation residual aggregated to **at most one** Stock Adjustment row:

	``SA_net(debit − credit) = −(Σ SLE.stock_value_difference − total_additional_costs)``

	* net > 0 → one debit row
	* net < 0 → one credit row
	* net = 0 → no SA row

This replaces the rejected SA→COGM gross-turnover remapping. Multi-output costing
(STAGE_CO, scrap, by/co-product, SFG, IRR, SABE) remains on SED/SLE; this module
only owns GL shape.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.currency import (
	get_company_currency,
	is_irr_company,
	round_currency,
)

MANUFACTURE_EXPENSE_PURPOSES: tuple[str, ...] = ("Manufacture", "Repack")

# Retained for campaign / Company field compatibility (no longer used as clearing).
COMPANY_MFG_EXPENSE_FIELD = "custom_iran_manufacturing_expense_account"

SA_RESIDUAL_REMARK_TOKEN = "IRR_MFG_SA_RESIDUAL"
NORMALIZED_GL_REMARK = "IRR_MFG_NORMALIZED_GL"


def is_manufacture_expense_purpose(purpose: str | None) -> bool:
	return str(purpose or "") in MANUFACTURE_EXPENSE_PURPOSES


def uses_normalized_manufacture_gl(doc) -> bool:
	"""True when Iran normalized Manufacture GL owns this document."""
	if not doc or getattr(doc, "doctype", None) != "Stock Entry":
		return False
	if not is_manufacture_expense_purpose(doc.get("purpose")):
		return False
	return bool(is_irr_company(doc.company))


def stock_adjustment_account(company: str) -> str | None:
	return frappe.get_cached_value("Company", company, "stock_adjustment_account") or None


def resolve_manufacturing_expense_account(company: str) -> str | None:
	"""Deprecated clearing-account helper — kept for callers; always None for GL.

	Normalized Manufacture GL does not use a COGM clearing bridge. Returns the
	configured Company field only for reporting/diagnostics.
	"""
	if not company:
		return None
	return frappe.get_cached_value("Company", company, COMPANY_MFG_EXPENSE_FIELD) or None


def manufacture_expected_sa_residual_net(doc, *, sle_net: float | None = None) -> float:
	"""Expected Stock Adjustment net (debit − credit).

	``SA_net = −(Σ SLE.stock_value_difference − total_additional_costs)``.
	"""
	ccy = get_company_currency(doc.company)
	if sle_net is None:
		sle_net = _sle_net_for_doc(doc)
	add = flt(doc.get("total_additional_costs"))
	return float(round_currency(-(flt(sle_net) - add), ccy))


def _sle_net_for_doc(doc) -> float:
	if doc.get("name") and frappe.db.exists(doc.doctype, doc.name):
		return float(
			frappe.db.sql(
				"""
				SELECT IFNULL(SUM(stock_value_difference), 0)
				FROM `tabStock Ledger Entry`
				WHERE voucher_type=%s AND voucher_no=%s AND IFNULL(is_cancelled, 0)=0
				""",
				(doc.doctype, doc.name),
			)[0][0]
			or 0.0
		)
	return flt(doc.get("total_incoming_value")) - flt(doc.get("total_outgoing_value"))


def _pick_cost_center(doc, item_row=None) -> str | None:
	if item_row and item_row.get("cost_center"):
		return item_row.cost_center
	for row in doc.get("items") or []:
		if row.get("is_finished_item") and row.get("cost_center"):
			return row.cost_center
	for row in doc.get("items") or []:
		if row.get("cost_center"):
			return row.cost_center
	return doc.get("cost_center")


def _pick_department(doc, item_row=None) -> str | None:
	"""Prefer FG / item-row department — required for P&L SA and OH accounts."""
	if item_row and item_row.get("department"):
		return item_row.department
	for row in doc.get("items") or []:
		if row.get("is_finished_item") and row.get("department"):
			return row.department
	for row in doc.get("items") or []:
		if row.get("department"):
			return row.department
	return doc.get("department")


def _dimension_item_row(doc, item_row=None):
	"""Synthetic / preferred item for get_gl_dict accounting dimensions."""
	if item_row is not None:
		return item_row
	for row in doc.get("items") or []:
		if row.get("is_finished_item"):
			return row
	items = doc.get("items") or []
	return items[0] if items else None


def _item_row_by_detail(doc, voucher_detail_no: str):
	for row in doc.get("items") or []:
		if row.name == voucher_detail_no:
			return row
	return None


def _append_inventory_leg(
	doc,
	gl_list: list,
	*,
	sle,
	inv_dict: dict,
	item_row,
	precision: int,
	against: str | None,
) -> float:
	"""Post the warehouse inventory leg only (no expense-account contra)."""
	amount = float(round_currency(flt(sle.stock_value_difference), get_company_currency(doc.company)))
	if abs(amount) < (1.0 / (10**precision) if precision else 0.5):
		# Still count raw for residual math via caller; skip zero GL.
		return float(flt(sle.stock_value_difference))

	project = sle.get("project") or (item_row.get("project") if item_row else None) or doc.get("project")
	base = {
		"account": inv_dict["account"],
		"against": against or inv_dict["account"],
		"cost_center": _pick_cost_center(doc, item_row),
		"project": project,
		"remarks": doc.get("remarks") or _(NORMALIZED_GL_REMARK),
		"is_opening": (item_row.get("is_opening") if item_row else None)
		or doc.get("is_opening")
		or "No",
	}
	if amount > 0:
		gl_list.append(
			doc.get_gl_dict(
				{**base, "debit": amount},
				inv_dict.get("account_currency"),
				item=item_row,
			)
		)
	else:
		gl_list.append(
			doc.get_gl_dict(
				{**base, "credit": abs(amount)},
				inv_dict.get("account_currency"),
				item=item_row,
			)
		)
	return amount


def _append_additional_cost_credits(doc, gl_list: list, *, against: str | None, precision: int) -> float:
	"""Credit Additional Cost expense accounts once (no clearing debit).

	Incoming SLE already carries allocated OH in stock_value_difference via
	Iran/Core amount capitalization. Pairing a full OH debit into SA/COGM would
	recreate the clearing bridge and double-count economically.
	"""
	del precision  # reserved for future amount-field precision; unused today
	ccy = get_company_currency(doc.company)
	total = 0.0
	dim_item = _dimension_item_row(doc)
	cost_center = _pick_cost_center(doc, dim_item)
	department = _pick_department(doc, dim_item)
	for row in doc.get("additional_costs") or []:
		account = row.get("expense_account")
		base_amount = float(
			round_currency(
				flt(row.get("base_amount") if row.get("base_amount") not in (None, "") else row.get("amount")),
				ccy,
			)
		)
		if not account or abs(base_amount) < 0.5:
			continue
		total += base_amount
		payload = {
			"account": account,
			"against": against or account,
			"cost_center": cost_center,
			"department": department,
			"remarks": doc.get("remarks") or _(NORMALIZED_GL_REMARK),
		}
		if base_amount > 0:
			payload["credit"] = base_amount
		else:
			# Negative Additional Costs (rare / repair lane): debit OH.
			payload["debit"] = abs(base_amount)
		# Pass FG/item so Accounting Dimensions (Department) fill like Core.
		gl_list.append(doc.get_gl_dict(payload, item=dim_item))
	# Prefer header total when child sum drifts by rounding.
	header = float(round_currency(flt(doc.get("total_additional_costs")), ccy))
	if abs(header) >= 0.5 and abs(header - total) >= 0.5 and not (doc.get("additional_costs") or []):
		# Header-only documents without child rows — nothing to post from children.
		pass
	return header if abs(header) >= 0.5 else total


def append_single_sa_residual(
	doc,
	gl_list: list,
	*,
	residual_net: float,
	against: str | None = None,
) -> None:
	"""Append at most one Stock Adjustment residual row (never Dr+Cr pair)."""
	sa = stock_adjustment_account(doc.company)
	if not sa:
		frappe.throw(
			_("Company {0} Stock Adjustment Account is required for Manufacture residual GL.").format(
				doc.company
			),
			title=_("Missing Stock Adjustment Account"),
		)
	ccy = get_company_currency(doc.company)
	residual_net = float(round_currency(residual_net, ccy))
	if abs(residual_net) < 0.5:
		return

	amount = abs(residual_net)
	remarks = (
		f"{SA_RESIDUAL_REMARK_TOKEN}|net={int(round(residual_net))}|"
		f"ROUND_HALF_UP|{NORMALIZED_GL_REMARK}"
	)
	dim_item = _dimension_item_row(doc)
	payload = {
		"account": sa,
		"against": against or sa,
		"cost_center": _pick_cost_center(doc, dim_item),
		"department": _pick_department(doc, dim_item),
		"remarks": remarks,
		"_irr_mfg_sa_residual": 1,
	}
	if residual_net > 0:
		payload["debit"] = amount
	else:
		payload["credit"] = amount
	gl_list.append(doc.get_gl_dict(payload, item=dim_item))


def collapse_sa_entries_to_single_residual(doc, gl_map: list | None) -> list:
	"""Force Stock Adjustment to one net residual row (or none).

	Merges every SA debit/credit on the map into a single one-sided residual.
	Safe to run after IRR/SABE post-processors.
	"""
	if not gl_map:
		return gl_map or []
	sa = stock_adjustment_account(doc.company)
	if not sa:
		return gl_map

	ccy = get_company_currency(doc.company)
	others: list = []
	sa_net = 0.0
	against = None
	for entry in gl_map:
		if (entry.get("account") or "") == sa:
			sa_net += flt(entry.get("debit")) - flt(entry.get("credit"))
			against = against or entry.get("against")
		else:
			others.append(entry)

	sa_net = float(round_currency(sa_net, ccy))
	append_single_sa_residual(doc, others, residual_net=sa_net, against=against)
	return others


def drop_net_zero_merged_gl_rows(doc, gl_map: list | None) -> list:
	"""Drop rows where debit equals credit after ``process_gl_map`` merge.

	Same-account Repack (batch↔batch or item↔item) posts inventory debit and
	credit that Core merges onto one GL dict. ``make_gl_entries`` then throws
	``Incorrect number of General Ledger Entries found`` when ``len(gl_map)==1``.

	A truly net-zero merged row (debit == credit on that already-merged key) is
	economically a no-op and must become an empty map — not a single-row map.
	One-sided residuals (Manufacture SA, unequal Repack nets) are preserved.
	Does not invent clearing / bridge accounts. Does not alter Department.
	"""
	if not gl_map:
		return gl_map or []

	ccy = get_company_currency(doc.company) if getattr(doc, "company", None) else None
	kept: list = []
	for entry in gl_map:
		debit = float(round_currency(flt(entry.get("debit")), ccy) if ccy else flt(entry.get("debit")))
		credit = float(
			round_currency(flt(entry.get("credit")), ccy) if ccy else flt(entry.get("credit"))
		)
		# Empty / cancelled merge key — drop.
		if abs(debit) < 0.5 and abs(credit) < 0.5:
			continue
		# Merged same-account cancel (debit == credit) — drop; do not leave len==1.
		if abs(debit - credit) < 0.5:
			continue
		kept.append(entry)
	return kept


def finalize_normalized_manufacture_gl_map(doc, gl_map: list | None) -> list:
	"""Collapse SA residual then drop net-zero merged rows (safe for make_gl_entries)."""
	collapsed = collapse_sa_entries_to_single_residual(doc, gl_map or [])
	return drop_net_zero_merged_gl_rows(doc, collapsed)


def assert_gl_map_safe_for_make_gl_entries(gl_map: list | None) -> None:
	"""Core ``make_gl_entries`` accepts empty or len>1 only — never a single row."""
	n = len(gl_map or [])
	if n == 1:
		frappe.throw(
			_(
				"Incorrect number of General Ledger Entries found. You might have selected a wrong Account in the transaction."
			)
		)


def build_normalized_manufacture_gl_entries(doc, inventory_account_map=None) -> list:
	"""Source-correct Manufacture GL: inventory + OH credits + one SA residual."""
	from erpnext.accounts.general_ledger import process_gl_map

	if not inventory_account_map:
		inventory_account_map = doc.get_inventory_account_map()

	precision = doc.get_debit_field_precision()
	sa = stock_adjustment_account(doc.company)
	sle_map = doc.get_stock_ledger_details()
	gl_list: list = []
	sle_net = 0.0
	warehouse_with_no_account: list[str] = []

	# Against pointer for inventory/OH: SA residual account (settlement narrative).
	against = sa

	for voucher_detail_no, sle_list in (sle_map or {}).items():
		item_row = _item_row_by_detail(doc, voucher_detail_no)
		for sle in sle_list or []:
			inv_dict = doc.get_inventory_account_dict(sle, inventory_account_map)
			if not inv_dict.get("account"):
				if sle.warehouse not in warehouse_with_no_account:
					warehouse_with_no_account.append(sle.warehouse)
				continue
			posted = _append_inventory_leg(
				doc,
				gl_list,
				sle=sle,
				inv_dict=inv_dict,
				item_row=item_row,
				precision=precision,
				against=against,
			)
			sle_net += posted

	if warehouse_with_no_account:
		frappe.throw(
			_("Primary inventory account not set for warehouses: {0}").format(
				", ".join(warehouse_with_no_account)
			)
		)

	add_posted = _append_additional_cost_credits(
		doc, gl_list, against=against, precision=precision
	)
	_ = add_posted

	# Prefer live SLE sum from legs; residual uses authoritative SLE query when named.
	residual = manufacture_expected_sa_residual_net(doc)
	append_single_sa_residual(doc, gl_list, residual_net=residual, against=against)

	# Landed cost voucher legs (if any) — Core helper; must not reintroduce SA bridge.
	if hasattr(doc, "set_gl_entries_for_landed_cost_voucher"):
		doc.set_gl_entries_for_landed_cost_voucher(gl_list, inventory_account_map)

	gl_map = process_gl_map(
		gl_list, from_repost=bool(frappe.flags.get("through_repost_item_valuation"))
	)
	return finalize_normalized_manufacture_gl_map(doc, gl_map)


def build_manufacture_gl_entries(doc, inventory_account_map=None, *, original_get_gl_entries=None) -> list:
	"""Public entry: normalized Manufacture GL (ignores Core expense-account bridge).

	``original_get_gl_entries`` is accepted for call-site compatibility but unused —
	Core bridge GL is intentionally not invoked.
	"""
	_ = original_get_gl_entries
	return build_normalized_manufacture_gl_entries(doc, inventory_account_map)


# --- Legacy no-ops kept so older imports/tests fail closed gracefully ---------


def apply_manufacturing_expense_accounts_on_items(doc) -> int:
	"""No-op: SED expense_account is not used as a Manufacture clearing bridge."""
	_ = doc
	return 0


def should_remap_expense_account(doc, expense_account: str | None) -> bool:
	_ = doc, expense_account
	return False


def peel_manufacture_cogm_residual_to_sa(doc, gl_map: list | None) -> list:
	"""Deprecated COGM peel — collapse any SA gross to a single residual instead."""
	return collapse_sa_entries_to_single_residual(doc, gl_map or [])


def summarize_gl_by_account(gl_rows: list[dict[str, Any]] | list) -> dict[str, dict[str, float]]:
	from collections import defaultdict

	by: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
	for entry in gl_rows or []:
		account = entry.get("account") or ""
		by[account][0] += flt(entry.get("debit"))
		by[account][1] += flt(entry.get("credit"))
	out: dict[str, dict[str, float]] = {}
	for account, (debit, credit) in sorted(by.items()):
		out[account] = {
			"debit": float(debit),
			"credit": float(credit),
			"net": float(debit - credit),
		}
	return out


def count_sa_gl_rows(gl_map: list | None, company: str) -> int:
	"""Count GL map rows on Company Stock Adjustment (pre-DB insert)."""
	sa = stock_adjustment_account(company)
	if not sa:
		return 0
	return sum(1 for e in (gl_map or []) if (e.get("account") or "") == sa)


def assert_sa_is_residual_only(gl_map: list | None, company: str) -> dict[str, Any]:
	"""Validation helper: SA has at most one side and at most one map row."""
	sa = stock_adjustment_account(company)
	summary = summarize_gl_by_account(gl_map or [])
	sa_row = summary.get(sa or "", {"debit": 0.0, "credit": 0.0, "net": 0.0})
	has_debit = abs(sa_row["debit"]) >= 0.5
	has_credit = abs(sa_row["credit"]) >= 0.5
	row_count = count_sa_gl_rows(gl_map, company)
	return {
		"stock_adjustment_account": sa,
		"debit": sa_row["debit"],
		"credit": sa_row["credit"],
		"net": sa_row["net"],
		"row_count": row_count,
		"residual_only": not (has_debit and has_credit),
		"single_sided_or_empty": (has_debit ^ has_credit) or (not has_debit and not has_credit),
		"at_most_one_row": row_count <= 1,
		"ok": (not (has_debit and has_credit)) and row_count <= 1,
	}
