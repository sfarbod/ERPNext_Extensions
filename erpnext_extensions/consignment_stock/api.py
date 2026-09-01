# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import frappe
from frappe import _

from erpnext_extensions.consignment_stock.constants import F_IS_RECEIPT, F_IS_RETURN
from erpnext_extensions.consignment_stock.recognition_service import create_recognition_journal_entry
from erpnext_extensions.consignment_stock.settlement_service import create_settlement_journal_entry


@frappe.whitelist()
def create_consignment_recognition_entry(stock_entry: str) -> dict:
	frappe.has_permission("Stock Entry", "write", throw=True)
	frappe.has_permission("Journal Entry", "create", throw=True)
	je_name = create_recognition_journal_entry(stock_entry)
	return {"journal_entry": je_name, "docstatus": 0}


@frappe.whitelist()
def create_consignment_return_settlement(stock_entry: str) -> dict:
	frappe.has_permission("Stock Entry", "write", throw=True)
	frappe.has_permission("Journal Entry", "create", throw=True)
	je_name = create_settlement_journal_entry(stock_entry)
	return {"journal_entry": je_name, "docstatus": 0}


@frappe.whitelist()
def make_consignment_return_from_receipt(source_name: str) -> dict:
	"""Map a submitted recognized receipt into a draft Consignment Return."""
	frappe.has_permission("Stock Entry", "create", throw=True)
	source = frappe.get_doc("Stock Entry", source_name)
	if source.docstatus != 1 or not source.get(F_IS_RECEIPT):
		frappe.throw(_("Source must be a submitted Consignment Receipt."))

	return_type = frappe.db.get_value(
		"Stock Entry Type",
		{F_IS_RETURN: 1, "purpose": "Material Issue"},
		"name",
	)
	if not return_type:
		frappe.throw(_("No Stock Entry Type configured for Consignment Return."))

	from erpnext_extensions.consignment_stock.constants import (
		F_HAS_RECEIPT_REF,
		F_PARTY,
		F_PARTY_TYPE,
		F_RECEIPT_DETAIL,
		F_RECEIPT_REF,
		F_RECEIPT_SE,
	)
	from erpnext_extensions.consignment_stock.returnable_qty import get_remaining_returnable_qty

	target = frappe.new_doc("Stock Entry")
	target.company = source.company
	target.stock_entry_type = return_type
	target.purpose = "Material Issue"
	target.set(F_PARTY_TYPE, source.get(F_PARTY_TYPE))
	target.set(F_PARTY, source.get(F_PARTY))
	target.set(F_HAS_RECEIPT_REF, 1)
	target.set(F_RECEIPT_REF, source.name)

	from erpnext_extensions.consignment_stock.accounting import (
		copy_accounting_dimensions_from_source_row,
	)

	for row in source.items:
		remaining = get_remaining_returnable_qty(row.name)
		if remaining <= 0:
			continue
		item_row = {
			"item_code": row.item_code,
			"qty": remaining,
			"transfer_qty": remaining,
			"uom": row.uom,
			"stock_uom": row.stock_uom,
			"conversion_factor": row.conversion_factor,
			"s_warehouse": row.t_warehouse,
			F_RECEIPT_SE: source.name,
			F_RECEIPT_DETAIL: row.name,
		}
		copy_accounting_dimensions_from_source_row(row, item_row)
		target.append("items", item_row)

	if not target.items:
		frappe.throw(_("No remaining returnable quantity on {0}.").format(source.name))

	target.insert()
	return {"doctype": "Stock Entry", "name": target.name}
