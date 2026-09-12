# Copyright (c) 2026, ERPNext Extensions contributors
"""Safe SLE DB persistence after in-memory IRR adjustments in process_sle."""

from __future__ import annotations

import logging

import frappe
from frappe.model.document import Document

from erpnext_extensions.iran_accounting.domain.ledger_rounding import (
	SLE_MONETARY_FIELDS,
	_get_entry_value,
)

logger = logging.getLogger(__name__)

_SLE_PERSIST_FIELDS = SLE_MONETARY_FIELDS + ("qty_after_transaction",)


def persist_processed_sle_if_possible(sle) -> bool:
	"""Write IRR-rounded fields to DB only when ERPNext has already persisted the SLE row.

	``update_entries_after.process_sle`` usually sets ``sle.doctype`` and calls ``db_update`` before
	returning. On early return (e.g. negative-stock validation) ``sle`` may be a ``frappe._dict`` without
	``doctype`` — never call ``frappe.get_doc(sle)`` in that case.
	"""
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		assert_sle_valuation_integrity_after_sync,
	)

	assert_sle_valuation_integrity_after_sync(sle)

	name = _get_entry_value(sle, "name")
	doctype = _get_entry_value(sle, "doctype")

	if isinstance(sle, Document):
		if sle.doctype != "Stock Ledger Entry":
			return False
		if not sle.name or sle.is_new():
			return False
		sle.db_update()
		return True

	if doctype == "Stock Ledger Entry" and name:
		try:
			frappe.get_doc(sle).db_update()
			return True
		except (ValueError, frappe.ValidationError) as exc:
			logger.debug("persist_processed_sle_if_possible: get_doc(mapping) failed: %s", exc)
			# fall through to load-by-name

	if not name:
		return False
	if doctype and doctype != "Stock Ledger Entry":
		return False

	if not frappe.db.exists("Stock Ledger Entry", name):
		return False

	doc = frappe.get_doc("Stock Ledger Entry", name)
	for field in _SLE_PERSIST_FIELDS:
		val = _get_entry_value(sle, field)
		if val is not None and val != "":
			doc.set(field, val)
	doc.db_update()
	return True
