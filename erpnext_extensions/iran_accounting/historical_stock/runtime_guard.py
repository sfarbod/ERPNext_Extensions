# Copyright (c) 2026, ERPNext Extensions contributors
"""Fail-closed runtime guard: do not silently persist unexpected outgoing rate 0.

Draft saves are allowed. Submit / before_submit is blocked when:

* allow_zero_valuation_rate is not set
* qty != 0
* basic_rate resolved to 0
* batch/SABB or Version or previous SLE proves a nonzero historical rate

Skip during historical repair and during RIV (SE rates are already preserved
by the 5.2.x IRR rate wrapper).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import ValuationIntegrityError
from erpnext_extensions.iran_accounting.historical_stock import (
	HISTORICAL_REPAIR_FLAG,
	QTY_EPS,
	RATE_EPS,
	SKIP_LOST_RATE_GUARD,
	VALUE_EPS,
)
from erpnext_extensions.iran_accounting.historical_stock.zero_rate import _batch_inward_rate, _previous_healthy_sle_rate


def assert_outgoing_rates_not_silently_zeroed(doc) -> None:
	if frappe.flags.get(SKIP_LOST_RATE_GUARD) or frappe.flags.get(HISTORICAL_REPAIR_FLAG):
		return
	if frappe.flags.get("through_repost_item_valuation"):
		return
	if getattr(doc, "doctype", None) != "Stock Entry":
		return
	# Draft save is not blocked; submit path sets docstatus 1 or flags.in_submit.
	submitting = cint(getattr(doc, "docstatus", 0) or 0) == 1 or bool(
		getattr(doc, "flags", None) and getattr(doc.flags, "get", lambda *_: None)("in_submit")
	)
	if not submitting:
		# Frappe before_submit: docstatus still 0. Detect via frappe.flags or caller.
		if not getattr(frappe.local, "historical_lost_rate_on_submit", False):
			# before_submit_stock_entry sets this flag.
			if not frappe.flags.get("stock_entry_before_submit"):
				return
	for row in doc.get("items") or []:
		_assert_row(doc, row)


def _assert_row(doc, row) -> None:
	if cint(row.get("allow_zero_valuation_rate")):
		return
	qty = flt(row.get("transfer_qty") if row.get("transfer_qty") not in (None, "") else row.get("qty"))
	if abs(qty) <= QTY_EPS:
		return
	if not row.get("s_warehouse"):
		return
	if abs(flt(row.get("basic_rate"))) > RATE_EPS:
		return
	item = row.get("item_code")
	warehouse = row.get("s_warehouse")
	batch = row.get("batch_no")
	evidence_rate = 0.0
	source = None
	if batch:
		evidence_rate = flt(_batch_inward_rate(item, batch, warehouse))
		if evidence_rate > VALUE_EPS:
			source = "batch_inward"
	if evidence_rate <= VALUE_EPS:
		evidence_rate = flt(
			_previous_healthy_sle_rate(item, warehouse, doc.posting_date, doc.posting_time)
		)
		if evidence_rate > VALUE_EPS:
			source = "previous_healthy_sle"
	if evidence_rate <= VALUE_EPS:
		return
	payload = {
		"invariant": "lost_outgoing_rate_zero",
		"voucher_type": "Stock Entry",
		"voucher_no": doc.name,
		"voucher_detail_no": row.name,
		"item_code": item,
		"warehouse": warehouse,
		"batch_no": batch,
		"purpose": doc.purpose,
		"basic_rate": row.get("basic_rate"),
		"historical_rate": evidence_rate,
		"source": source,
	}
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import format_valuation_integrity_message

	raise ValuationIntegrityError(
		format_valuation_integrity_message(payload)
		+ "\n"
		+ _("Stock valuation integrity — outgoing rate unexpectedly resolved to zero")
	)


def evidence_for_zero_outgoing(item, warehouse, batch, posting_date, posting_time) -> tuple[float, str | None]:
	rate = flt(_batch_inward_rate(item, batch, warehouse)) if batch else 0.0
	if rate > VALUE_EPS:
		return rate, "batch_inward"
	rate = flt(_previous_healthy_sle_rate(item, warehouse, posting_date, posting_time))
	if rate > VALUE_EPS:
		return rate, "previous_healthy_sle"
	return 0.0, None
