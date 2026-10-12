# Copyright (c) 2026, ERPNext Extensions contributors
"""Compatibility backport of ERPNext #59476 for Docker-stock ERPNext 16.37.x.

Upstream: fix(stock): compare the invoice rate against the receipt's net amount
Commit: 1fe6c2e92b0dcf3cdca6e945ea89c120fb42fa19

When ``set_landed_cost_based_on_purchase_invoice_rate`` is enabled, stock ERPNext
v16 compares PI billed net to PR gross ``item.rate``, double-applying Additional
Discount. Official fix compares to ``item.base_net_amount``.

This module wraps ``update_billing_percentage`` narrowly:
- If Core already contains #59476 → pass through (no override).
- Else → run Core billing % without rate adjust, apply upstream amt_diff formula
  using Core billed-qty helpers, then call Core ``adjust_incoming_rate_for_pr``.

Lifecycle: remove when the site ERPNext image includes #59476 / equivalent.
"""

from __future__ import annotations

import inspect
import logging

import frappe
from frappe.utils import flt

logger = logging.getLogger(__name__)

UPSTREAM_PR = "#59476"
UPSTREAM_COMMIT = "1fe6c2e92b0dcf3cdca6e945ea89c120fb42fa19"
# Exact upstream formula fragment used for source detection.
_UPSTREAM_FORMULA_MARKER = "flt(billed_amt / qty) * item.qty - flt(item.base_net_amount)"
_LEGACY_FORMULA_MARKER = "flt(item.rate) * flt(pr_doc.conversion_rate)"

_PATCH_ATTR = "_iran_pr_pi_landed_cost_59476"
_installed = False
_original_update_billing_percentage = None


def _core_update_billing_percentage_callable():
	"""Return the vanilla Core function even if our wrap is installed."""
	from erpnext.stock.doctype.purchase_receipt import purchase_receipt as pr_mod

	fn = pr_mod.update_billing_percentage
	orig = getattr(fn, "_iran_original", None)
	if orig is not None:
		return orig
	if _original_update_billing_percentage is not None:
		return _original_update_billing_percentage
	return fn


def core_has_upstream_59476() -> bool:
	"""True when installed ERPNext already uses the official net-amount comparison."""
	src = inspect.getsource(_core_update_billing_percentage_callable())
	compact = " ".join(src.split())
	if _UPSTREAM_FORMULA_MARKER in compact:
		return True
	# develop BillingStatusService path after extraction
	try:
		from erpnext.stock.doctype.purchase_receipt.services import billing_status

		svc_src = inspect.getsource(billing_status.set_amount_difference_with_purchase_invoice)
		if "flt(row.amount / row.qty) * flt(item.qty) - flt(item.base_net_amount)" in " ".join(
			svc_src.split()
		):
			return True
	except Exception:
		pass
	return False


def core_needs_59476_compat() -> bool:
	"""True when legacy gross-rate comparison is still present on Core."""
	if core_has_upstream_59476():
		return False
	src = inspect.getsource(_core_update_billing_percentage_callable())
	return _LEGACY_FORMULA_MARKER in src


def _compute_adjusted_amt_59476(*, billed_amt, qty, item_qty, base_net_amount, precision) -> float:
	"""Official #59476 company-currency net comparison."""
	return flt(flt(billed_amt / qty) * item_qty - flt(base_net_amount), precision)


def apply_59476_amount_differences(pr_doc) -> float:
	"""Set ``amount_difference_with_purchase_invoice`` using upstream #59476 semantics.

	Reuses Core billed-qty helpers; only the comparison baseline differs from legacy v16.
	Returns the sum of adjusted amounts (for per_billed negative-delta handling).
	"""
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import (
		get_billed_qty_amount_against_purchase_order,
		get_billed_qty_amount_against_purchase_receipt,
	)

	billed_qty_amt = get_billed_qty_amount_against_purchase_receipt(pr_doc)
	billed_qty_amt_based_on_po = get_billed_qty_amount_against_purchase_order(pr_doc)
	pi_landed_cost_amount = 0.0

	for item in pr_doc.items:
		adjusted_amt = 0.0
		if (
			item.billed_amt is not None
			and item.amount is not None
			and (
				billed_qty_amt.get(item.name)
				or billed_qty_amt_based_on_po.get(item.purchase_order_item)
			)
		):
			qty = None
			if billed_qty_amt.get(item.name):
				qty = billed_qty_amt.get(item.name).get("qty")

			if not qty and billed_qty_amt_based_on_po.get(item.purchase_order_item):
				if item.qty < billed_qty_amt_based_on_po.get(item.purchase_order_item)["qty"]:
					qty = item.qty
				else:
					qty = billed_qty_amt_based_on_po.get(item.purchase_order_item)["qty"]
				billed_qty_amt_based_on_po[item.purchase_order_item]["qty"] -= qty

			billed_amt = item.billed_amt
			if billed_qty_amt.get(item.name):
				billed_amt = flt(billed_qty_amt.get(item.name).get("amount"))
			elif billed_qty_amt_based_on_po.get(item.purchase_order_item):
				total_billed_qty = (
					billed_qty_amt_based_on_po.get(item.purchase_order_item).get("qty") + qty
				)
				if total_billed_qty:
					billed_amt = flt(
						flt(billed_qty_amt_based_on_po.get(item.purchase_order_item).get("amount"))
						* (qty / total_billed_qty)
					)
				else:
					billed_amt = 0.0
				billed_qty_amt_based_on_po[item.purchase_order_item]["amount"] -= billed_amt

			if qty:
				adjusted_amt = _compute_adjusted_amt_59476(
					billed_amt=billed_amt,
					qty=qty,
					item_qty=item.qty,
					base_net_amount=item.base_net_amount,
					precision=item.precision("amount"),
				)

		adjusted_amt = flt(adjusted_amt, item.precision("amount"))
		pi_landed_cost_amount += adjusted_amt
		item.db_set("amount_difference_with_purchase_invoice", adjusted_amt, update_modified=False)
		item.amount_difference_with_purchase_invoice = adjusted_amt

	return pi_landed_cost_amount


def _wrapped_update_billing_percentage(
	pr_doc, update_modified=True, adjust_incoming_rate=False
):
	from erpnext.stock.doctype.purchase_receipt.purchase_receipt import (
		adjust_incoming_rate_for_pr,
	)

	orig = _original_update_billing_percentage
	if orig is None:
		raise RuntimeError("PR/PI landed-cost compat patch missing original")

	# Upstream already fixed — never override native semantics.
	if not core_needs_59476_compat():
		return orig(
			pr_doc, update_modified=update_modified, adjust_incoming_rate=adjust_incoming_rate
		)

	if not adjust_incoming_rate:
		return orig(pr_doc, update_modified=update_modified, adjust_incoming_rate=False)

	# Billing % / status via Core without applying the defective rate adjust.
	# (adjust_incoming_rate=False leaves per_billed based on billed_amt only —
	# avoiding the legacy path that inflated % using the wrong negative amt_diff.)
	orig(pr_doc, update_modified=update_modified, adjust_incoming_rate=False)
	apply_59476_amount_differences(pr_doc)
	adjust_incoming_rate_for_pr(pr_doc)


def install_pr_pi_landed_cost_compat_patch() -> dict:
	"""Idempotent install. Safe for before_request / before_job / workers."""
	global _installed, _original_update_billing_percentage

	from erpnext.stock.doctype.purchase_receipt import purchase_receipt as pr_mod

	current = pr_mod.update_billing_percentage
	if getattr(current, _PATCH_ATTR, False):
		_installed = True
		return {
			"status": "already_installed",
			"upstream_pr": UPSTREAM_PR,
			"core_has_59476": core_has_upstream_59476(),
			"compat_active": core_needs_59476_compat(),
		}

	if not core_needs_59476_compat():
		# Core already fixed — do not wrap (avoids silent double semantics).
		_installed = True
		logger.info(
			"iran_accounting: PR/PI landed-cost compat skipped; Core has %s", UPSTREAM_PR
		)
		return {
			"status": "skipped_core_has_upstream",
			"upstream_pr": UPSTREAM_PR,
			"core_has_59476": True,
			"compat_active": False,
		}

	_original_update_billing_percentage = current
	_wrapped_update_billing_percentage._iran_original = current  # type: ignore[attr-defined]
	setattr(_wrapped_update_billing_percentage, _PATCH_ATTR, True)
	pr_mod.update_billing_percentage = _wrapped_update_billing_percentage
	_installed = True
	logger.info(
		"iran_accounting: installed PR/PI landed-cost compat for %s (%s)",
		UPSTREAM_PR,
		UPSTREAM_COMMIT,
	)
	return {
		"status": "installed",
		"upstream_pr": UPSTREAM_PR,
		"upstream_commit": UPSTREAM_COMMIT,
		"core_has_59476": False,
		"compat_active": True,
	}


def uninstall_pr_pi_landed_cost_compat_patch() -> None:
	"""Restore Core function (tests / removal procedure)."""
	global _installed, _original_update_billing_percentage
	from erpnext.stock.doctype.purchase_receipt import purchase_receipt as pr_mod

	if _original_update_billing_percentage is not None:
		pr_mod.update_billing_percentage = _original_update_billing_percentage
	_original_update_billing_percentage = None
	_installed = False
