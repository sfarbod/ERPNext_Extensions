# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Thin hooks that re-apply usage factors after ERPNext rebuilds ADS."""

from __future__ import annotations

import frappe

from erpnext_extensions.asset_usage_depreciation.services.replan_service import (
	asset_has_submitted_usage_periods,
	replan_asset_usage_depreciation,
)


def _reapply_for_asset(asset_name: str, trigger_doc=None):
	if not asset_name:
		return
	if frappe.flags.get("usage_replan_in_progress"):
		return
	if not asset_has_submitted_usage_periods(asset_name):
		return
	replan_asset_usage_depreciation(
		asset_name,
		trigger_doc=trigger_doc,
		context={"skip_if_no_usage": True, "source": "erpnext_reschedule"},
	)


def on_asset_value_adjustment_submit(doc, method=None):
	_reapply_for_asset(doc.asset, trigger_doc=doc)


def on_asset_value_adjustment_cancel(doc, method=None):
	_reapply_for_asset(doc.asset, trigger_doc=doc)


def on_asset_repair_submit(doc, method=None):
	if not doc.asset:
		return
	if not (getattr(doc, "capitalize_repair_cost", 0) or getattr(doc, "increase_in_asset_life", 0)):
		return
	_reapply_for_asset(doc.asset, trigger_doc=doc)


def on_asset_repair_cancel(doc, method=None):
	if not doc.asset:
		return
	if not (getattr(doc, "capitalize_repair_cost", 0) or getattr(doc, "increase_in_asset_life", 0)):
		return
	_reapply_for_asset(doc.asset, trigger_doc=doc)


def on_sales_invoice_submit(doc, method=None):
	# Narrow: only invoices that dispose fixed assets
	for asset_name in _disposed_assets_on_sales_invoice(doc):
		_reapply_for_asset(asset_name, trigger_doc=doc)


def on_sales_invoice_cancel(doc, method=None):
	for asset_name in _disposed_assets_on_sales_invoice(doc):
		_reapply_for_asset(asset_name, trigger_doc=doc)


def _disposed_assets_on_sales_invoice(doc) -> list[str]:
	"""Return asset names only when SI items reference Asset (disposal path)."""
	names: list[str] = []
	for item in doc.get("items") or []:
		asset_name = getattr(item, "asset", None)
		if not asset_name:
			continue
		# is_fixed_asset on item is the ERPNext disposal signal when present
		if getattr(item, "is_fixed_asset", None) in (None, 1):
			names.append(asset_name)
	return names


@frappe.whitelist()
def scrap_asset(asset_name: str, scrap_date=None):
	"""Whitelist wrapper: run core scrap, then re-apply usage factors.

	Uses ``override_whitelisted_methods`` (same pattern as other modules in this
	app). Imports the original function from the ERPNext module object so this
	is not a monkey patch of the module attribute.

	Signature mirrors ERPNext ``depreciation.scrap_asset`` on purpose. Frappe
	RPC (``handler.execute_cmd`` → ``frappe.call(..., **form_dict)``) includes
	dispatch metadata such as ``cmd`` in ``form_dict``. ``frappe.get_newargs``
	only strips unsupported keys when the callee does **not** accept
	``**kwargs``. Matching the native fixed signature keeps RPC metadata out of
	this wrapper and out of the core call, while still forwarding the business
	arguments ``asset_name`` / ``scrap_date``.
	"""
	from erpnext.assets.doctype.asset import depreciation as depr_mod

	result = depr_mod.scrap_asset(asset_name, scrap_date)
	_reapply_for_asset(asset_name)
	return result


@frappe.whitelist()
def restore_asset(asset_name: str):
	"""Whitelist wrapper: run core restore, then re-apply usage factors.

	Same RPC-boundary contract as ``scrap_asset``: fixed signature so Frappe
	strips ``cmd`` / other form_dict metadata before the wrapper runs, and only
	``asset_name`` is forwarded to native ``depreciation.restore_asset``.
	"""
	from erpnext.assets.doctype.asset import depreciation as depr_mod

	result = depr_mod.restore_asset(asset_name)
	_reapply_for_asset(asset_name)
	return result


def on_asset_submit(doc, method=None):
	"""Link a newly capitalized asset to an open purchase allocation and issue it."""
	from erpnext_extensions.asset_usage_depreciation.services.fulfillment_service import (
		link_purchased_asset,
	)

	link_purchased_asset(doc)


def on_asset_movement_cancel(doc, method=None):
	from erpnext_extensions.asset_usage_depreciation.services.fulfillment_service import (
		on_asset_movement_cancel as _on_cancel,
	)

	_on_cancel(doc)


def on_material_request_cancel(doc, method=None):
	from erpnext_extensions.asset_usage_depreciation.services.fulfillment_service import (
		on_material_request_cancel as _on_cancel,
	)

	_on_cancel(doc)


def on_purchase_receipt_submit(doc, method=None):
	from erpnext_extensions.asset_usage_depreciation.services.fulfillment_service import (
		on_purchase_receipt_submit as _on_submit,
	)

	_on_submit(doc)
