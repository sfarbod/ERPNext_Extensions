# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only serial / batch eligibility checks for Delivery Note Check Submission."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos


def validate_serial_and_batch_requirements(doc) -> list[dict]:
	"""Validate serial/batch presence and eligibility without creating bundles.

	Returns limitation dicts for cases that require submit-time auto-creation
	(e.g. auto-pick outward serials) which cannot be fully simulated read-only.
	"""
	limitations: list[dict] = []
	if cint(doc.get("is_return")):
		# Full return serial/batch bundle creation is not reproduced read-only.
		limitations.append(
			{
				"code": "return_serial_batch_bundle",
				"severity": "blocking",
				"message": _(
					"Return Delivery Notes require Serial/Batch Bundle creation at submit time; "
					"this check cannot fully validate return serial/batch handling read-only."
				),
			}
		)
		return limitations

	auto_pick = False
	try:
		auto_pick = cint(
			frappe.get_cached_value("Stock Settings", None, "auto_create_serial_and_batch_bundle_for_outward")
		)
	except Exception:
		auto_pick = False

	for table in ("items", "packed_items"):
		for row in doc.get(table) or []:
			item_code = row.get("item_code")
			if not item_code:
				continue
			has_serial = cint(frappe.get_cached_value("Item", item_code, "has_serial_no"))
			has_batch = cint(frappe.get_cached_value("Item", item_code, "has_batch_no"))
			if not has_serial and not has_batch:
				continue
			if not cint(frappe.get_cached_value("Item", item_code, "is_stock_item")):
				continue

			has_bundle = bool(row.get("serial_and_batch_bundle"))
			has_serial_field = bool((row.get("serial_no") or "").strip())
			has_batch_field = bool(row.get("batch_no"))

			if has_bundle:
				_validate_existing_bundle(doc, row)
				continue

			if has_serial and not has_serial_field:
				if auto_pick:
					limitations.append(
						{
							"code": "auto_pick_serial",
							"severity": "blocking",
							"message": _(
								"Row #{0}: Item {1} requires serial numbers. Auto-create Serial and "
								"Batch Bundle is enabled, but Check Submission cannot safely simulate "
								"auto-picking. Specify serial numbers (or a bundle) to validate, or submit carefully."
							).format(row.idx, frappe.bold(item_code)),
						}
					)
				else:
					frappe.throw(
						_("Row #{0}: Serial No is mandatory for Item {1}").format(
							row.idx, frappe.bold(item_code)
						),
						title=_("Serial Number Required"),
					)

			if has_batch and not has_batch_field and not has_serial_field:
				# Serial rows may imply batch via serial master; otherwise batch is required.
				if auto_pick:
					limitations.append(
						{
							"code": "auto_pick_batch",
							"severity": "blocking",
							"message": _(
								"Row #{0}: Item {1} requires a Batch No. Auto-create Serial and Batch "
								"Bundle is enabled; Check Submission cannot safely simulate auto-picking."
							).format(row.idx, frappe.bold(item_code)),
						}
					)
				else:
					frappe.throw(
						_("Row #{0}: Batch No is mandatory for Item {1}").format(
							row.idx, frappe.bold(item_code)
						),
						title=_("Batch Number Required"),
					)

			if has_serial_field:
				_validate_serial_nos_in_warehouse(doc, row)

			if has_batch_field:
				_validate_batch_usable(row)

	return limitations


def _validate_existing_bundle(doc, row) -> None:
	bundle = frappe.db.get_value(
		"Serial and Batch Bundle",
		row.serial_and_batch_bundle,
		["name", "company", "warehouse", "type_of_transaction", "item_code", "docstatus", "is_cancelled"],
		as_dict=True,
	)
	if not bundle:
		frappe.throw(
			_("Row #{0}: Serial and Batch Bundle {1} does not exist").format(
				row.idx, frappe.bold(row.serial_and_batch_bundle)
			)
		)
	if cint(bundle.is_cancelled) or cint(bundle.docstatus) == 2:
		frappe.throw(
			_("Row #{0}: Serial and Batch Bundle {1} is cancelled").format(
				row.idx, frappe.bold(bundle.name)
			)
		)
	if bundle.company and bundle.company != doc.company:
		frappe.throw(
			_("Row #{0}: Company on Serial and Batch Bundle {1} does not match Delivery Note").format(
				row.idx, frappe.bold(bundle.name)
			)
		)
	if bundle.warehouse and row.warehouse and bundle.warehouse != row.warehouse:
		frappe.throw(
			_("Row #{0}: Warehouse on Serial and Batch Bundle {1} must be {2}").format(
				row.idx, frappe.bold(bundle.name), frappe.bold(row.warehouse)
			)
		)


def _validate_serial_nos_in_warehouse(doc, row) -> None:
	serials = get_serial_nos(row.serial_no)
	if not serials:
		return

	needed = abs(flt(row.get("stock_qty") or row.get("qty") or 0))
	# For serialized items, qty should match serial count (stock UOM).
	if needed and abs(len(serials) - needed) > 1e-6 and cint(
		frappe.get_cached_value("Item", row.item_code, "has_serial_no")
	):
		# Allow mild float noise; serial items are discrete.
		if int(round(needed)) != len(serials):
			frappe.throw(
				_("Row #{0}: Number of Serial Nos ({1}) must match quantity ({2}) for Item {3}").format(
					row.idx, len(serials), int(round(needed)), frappe.bold(row.item_code)
				)
			)

	found = frappe.get_all(
		"Serial No",
		filters={"name": ("in", serials)},
		fields=["name", "item_code", "warehouse", "status"],
	)
	by_name = {d.name: d for d in found}
	for sn in serials:
		meta = by_name.get(sn)
		if not meta:
			frappe.throw(
				_("Row #{0}: Serial No {1} does not exist").format(row.idx, frappe.bold(sn)),
				title=_("Invalid Serial Number"),
			)
		if meta.item_code != row.item_code:
			frappe.throw(
				_("Row #{0}: Serial No {1} belongs to Item {2}, not {3}").format(
					row.idx, frappe.bold(sn), frappe.bold(meta.item_code), frappe.bold(row.item_code)
				)
			)
		if row.warehouse and meta.warehouse and meta.warehouse != row.warehouse:
			frappe.throw(
				_("Row #{0}: Serial No {1} is in Warehouse {2}, not {3}").format(
					row.idx,
					frappe.bold(sn),
					frappe.bold(meta.warehouse),
					frappe.bold(row.warehouse),
				),
				title=_("Serial Number Not Available"),
			)
		if meta.status and meta.status not in ("Active", "Inactive"):
			# Delivered / Expired etc. block outward
			if meta.status in ("Delivered", "Expired", "Consumed"):
				frappe.throw(
					_("Row #{0}: Serial No {1} has status {2} and cannot be delivered").format(
						row.idx, frappe.bold(sn), frappe.bold(meta.status)
					),
					title=_("Serial Number Not Available"),
				)


def _validate_batch_usable(row) -> None:
	batch = frappe.db.get_value(
		"Batch",
		row.batch_no,
		["name", "item", "disabled", "expiry_date"],
		as_dict=True,
	)
	if not batch:
		frappe.throw(
			_("Row #{0}: Batch {1} does not exist").format(row.idx, frappe.bold(row.batch_no))
		)
	if batch.item and batch.item != row.item_code:
		frappe.throw(
			_("Row #{0}: Batch {1} belongs to Item {2}, not {3}").format(
				row.idx, frappe.bold(row.batch_no), frappe.bold(batch.item), frappe.bold(row.item_code)
			)
		)
	if cint(batch.disabled):
		frappe.throw(
			_("Row #{0}: Batch {1} is disabled").format(row.idx, frappe.bold(row.batch_no))
		)
