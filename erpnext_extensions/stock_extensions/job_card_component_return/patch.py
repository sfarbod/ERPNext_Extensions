# Copyright (c) 2026, ERPNext Extensions contributors
"""Fingerprint-guarded wrap of StockEntry.validate_work_order_status_for_return.

ERPNext 16.36+/16.37 introduced:

	Components can be returned only after Work Order … is Completed or Closed

This patch does NOT disable that check globally. It only skips the throw when a
Job Card–scoped, evidence-proven component return is fully validated.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from erpnext_extensions.iran_accounting.domain.riv_rate_guard import major_minor, source_sha256
from erpnext_extensions.stock_extensions.job_card_component_return.eligibility import (
	decide_work_order_status_bypass,
)

# Measured on ERPNext 16.37.0 (identical method body on 16.36.x).
_SUPPORTED_ERPNEXT_MINOR = frozenset({"16.36", "16.37"})
_SUPPORTED_METHOD_SHA256 = frozenset(
	{
		"9c50bb7b9d1972827bf8e168c5c7c8ee79aa5ff7bf27b80f9624a370604de911",
	}
)

_ATTR_FLAG = "_ee_jc_component_return_patched"
_ATTR_ORIGINAL = "_ee_jc_component_return_original"
_ATTR_BYPASS_ENABLED = "_ee_jc_component_return_bypass_enabled"


def apply_patch() -> None:
	"""Install idempotent wrap. Fail closed on unknown Core → no bypass."""
	from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry

	if getattr(StockEntry, _ATTR_FLAG, False):
		return

	import erpnext

	live = getattr(StockEntry, "validate_work_order_status_for_return", None)
	if live is None:
		frappe.log_error(
			title="JC component return patch skipped",
			message="StockEntry.validate_work_order_status_for_return missing",
		)
		StockEntry._ee_jc_component_return_patched = True
		StockEntry._ee_jc_component_return_bypass_enabled = False
		return

	# Already our wrap (re-entry)
	if getattr(live, "_ee_jc_return_wrap", False):
		StockEntry._ee_jc_component_return_patched = True
		return

	erp_mm = major_minor(getattr(erpnext, "__version__", ""))
	bypass_ok = True
	reason = ""
	try:
		digest = source_sha256(live)
	except Exception as exc:
		bypass_ok = False
		reason = f"fingerprint_failed:{exc}"
		digest = ""

	if erp_mm not in _SUPPORTED_ERPNEXT_MINOR:
		bypass_ok = False
		reason = f"unsupported_erpnext:{erp_mm}"
	elif digest not in _SUPPORTED_METHOD_SHA256:
		bypass_ok = False
		reason = f"unknown_core_fingerprint:{digest}"

	setattr(StockEntry, _ATTR_ORIGINAL, live)
	setattr(StockEntry, _ATTR_BYPASS_ENABLED, bypass_ok)

	def validate_work_order_status_for_return(self):
		original = getattr(StockEntry, _ATTR_ORIGINAL)
		# Preserve Core early-allow for Completed/Closed / non-returns.
		if not (cint(self.is_return) and getattr(self, "pro_doc", None)) or (
			self.pro_doc and self.pro_doc.status in ("Completed", "Closed")
		):
			return

		if not getattr(StockEntry, _ATTR_BYPASS_ENABLED, False):
			# Fail closed: unknown Core → never bypass.
			return original(self)

		decision = decide_work_order_status_bypass(self)
		if decision.allow:
			return

		if decision.is_attempted_jc_return:
			frappe.throw(
				decision.message or _("Job Card component return blocked"),
				title=_("Job Card Component Return Blocked"),
			)

		# Manual / WO-only returns keep the original Core throw.
		return original(self)

	validate_work_order_status_for_return._ee_jc_return_wrap = True  # type: ignore[attr-defined]
	validate_work_order_status_for_return._ee_jc_return_reason = reason  # type: ignore[attr-defined]
	StockEntry.validate_work_order_status_for_return = validate_work_order_status_for_return
	setattr(StockEntry, _ATTR_FLAG, True)

	if not bypass_ok:
		frappe.logger("erpnext_extensions").warning(
			"JC component return bypass DISABLED (fail closed): %s", reason
		)


def bypass_enabled() -> bool:
	from erpnext.stock.doctype.stock_entry.stock_entry import StockEntry

	return bool(getattr(StockEntry, _ATTR_BYPASS_ENABLED, False))
