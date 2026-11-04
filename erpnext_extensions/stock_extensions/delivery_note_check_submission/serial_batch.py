# Copyright (c) 2026, ERPNext Extensions contributors
"""Read-only serial / batch eligibility checks for Delivery Note Check Submission."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint, flt, formatdate, getdate

from erpnext.stock.doctype.serial_no.serial_no import get_serial_nos

from erpnext_extensions.stock_extensions.delivery_note_check_submission.errors import make_error
from erpnext_extensions.stock_extensions.delivery_note_check_submission.stock_availability import (
	is_batch_expired_on,
)


def collect_serial_and_batch_errors(doc) -> tuple[list[dict], list[dict]]:
	"""Return (errors, limitations) for serial/batch requirements.

	Batch expiry is evaluated against Delivery Note ``posting_date`` (ERPNext:
	expired iff expiry_date < posting_date).
	"""
	errors: list[dict] = []
	limitations: list[dict] = []

	if cint(doc.get("is_return")):
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
		return errors, limitations

	posting_date = doc.get("posting_date")
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
				errors.extend(_collect_bundle_errors(doc, row, table))
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
					errors.append(
						make_error(
							category="serial_number",
							message=_("Row #{0}: Serial No is mandatory for Item {1}").format(
								row.idx, frappe.bold(item_code)
							),
							exc_type="ValidationError",
							item_code=item_code,
							row=row.idx,
							table=table,
						)
					)

			if has_batch and not has_batch_field and not has_serial_field:
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
					errors.append(
						make_error(
							category="batch_eligibility",
							message=_("Row #{0}: Batch No is mandatory for Item {1}").format(
								row.idx, frappe.bold(item_code)
							),
							exc_type="ValidationError",
							item_code=item_code,
							row=row.idx,
							table=table,
						)
					)

			if has_serial_field:
				errors.extend(_collect_serial_errors(doc, row, table))

			if has_batch_field:
				errors.extend(_collect_batch_errors(doc, row, table, posting_date))

	return errors, limitations


def validate_serial_and_batch_requirements(doc) -> list[dict]:
	"""Throw-compatible wrapper; returns limitations only (errors raise)."""
	errors, limitations = collect_serial_and_batch_errors(doc)
	if errors:
		frappe.throw(errors[0]["message"], title=_("Serial / Batch Validation"))
	return limitations


def _collect_bundle_errors(doc, row, table) -> list[dict]:
	errors: list[dict] = []
	bundle = frappe.db.get_value(
		"Serial and Batch Bundle",
		row.serial_and_batch_bundle,
		["name", "company", "warehouse", "type_of_transaction", "item_code", "docstatus", "is_cancelled"],
		as_dict=True,
	)
	if not bundle:
		errors.append(
			make_error(
				category="serial_batch_bundle",
				message=_("Row #{0}: Serial and Batch Bundle {1} does not exist").format(
					row.idx, frappe.bold(row.serial_and_batch_bundle)
				),
				item_code=row.item_code,
				row=row.idx,
				table=table,
			)
		)
		return errors
	if cint(bundle.is_cancelled) or cint(bundle.docstatus) == 2:
		errors.append(
			make_error(
				category="serial_batch_bundle",
				message=_("Row #{0}: Serial and Batch Bundle {1} is cancelled").format(
					row.idx, frappe.bold(bundle.name)
				),
				item_code=row.item_code,
				row=row.idx,
				table=table,
			)
		)
	if bundle.company and bundle.company != doc.company:
		errors.append(
			make_error(
				category="serial_batch_bundle",
				message=_(
					"Row #{0}: Company on Serial and Batch Bundle {1} does not match Delivery Note"
				).format(row.idx, frappe.bold(bundle.name)),
				item_code=row.item_code,
				row=row.idx,
				table=table,
			)
		)
	if bundle.warehouse and row.warehouse and bundle.warehouse != row.warehouse:
		errors.append(
			make_error(
				category="serial_batch_bundle",
				message=_("Row #{0}: Warehouse on Serial and Batch Bundle {1} must be {2}").format(
					row.idx, frappe.bold(bundle.name), frappe.bold(row.warehouse)
				),
				item_code=row.item_code,
				row=row.idx,
				warehouse=row.warehouse,
				table=table,
			)
		)
	return errors


def _collect_serial_errors(doc, row, table) -> list[dict]:
	errors: list[dict] = []
	serials = get_serial_nos(row.serial_no)
	if not serials:
		return errors

	needed = abs(flt(row.get("stock_qty") or row.get("qty") or 0))
	if needed and cint(frappe.get_cached_value("Item", row.item_code, "has_serial_no")):
		if int(round(needed)) != len(serials):
			errors.append(
				make_error(
					category="serial_number",
					message=_(
						"Row #{0}: Number of Serial Nos ({1}) must match quantity ({2}) for Item {3}"
					).format(row.idx, len(serials), int(round(needed)), frappe.bold(row.item_code)),
					item_code=row.item_code,
					row=row.idx,
					table=table,
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
			errors.append(
				make_error(
					category="serial_number",
					message=_("Row #{0}: Serial No {1} does not exist").format(row.idx, frappe.bold(sn)),
					exc_type="ValidationError",
					item_code=row.item_code,
					row=row.idx,
					serial_no=sn,
					table=table,
				)
			)
			continue
		if meta.item_code != row.item_code:
			errors.append(
				make_error(
					category="serial_number",
					message=_("Row #{0}: Serial No {1} belongs to Item {2}, not {3}").format(
						row.idx, frappe.bold(sn), frappe.bold(meta.item_code), frappe.bold(row.item_code)
					),
					item_code=row.item_code,
					row=row.idx,
					serial_no=sn,
					table=table,
				)
			)
		if row.warehouse and meta.warehouse and meta.warehouse != row.warehouse:
			errors.append(
				make_error(
					category="serial_number",
					message=_("Row #{0}: Serial No {1} is in Warehouse {2}, not {3}").format(
						row.idx,
						frappe.bold(sn),
						frappe.bold(meta.warehouse),
						frappe.bold(row.warehouse),
					),
					item_code=row.item_code,
					row=row.idx,
					warehouse=row.warehouse,
					serial_no=sn,
					table=table,
				)
			)
		if meta.status in ("Delivered", "Expired", "Consumed"):
			errors.append(
				make_error(
					category="serial_number",
					message=_("Row #{0}: Serial No {1} has status {2} and cannot be delivered").format(
						row.idx, frappe.bold(sn), frappe.bold(meta.status)
					),
					item_code=row.item_code,
					row=row.idx,
					serial_no=sn,
					table=table,
				)
			)
	return errors


def _collect_batch_errors(doc, row, table, posting_date) -> list[dict]:
	errors: list[dict] = []
	batch = frappe.db.get_value(
		"Batch",
		row.batch_no,
		["name", "item", "disabled", "expiry_date"],
		as_dict=True,
	)
	if not batch:
		errors.append(
			make_error(
				category="batch_eligibility",
				message=_("Row #{0}: Batch {1} does not exist").format(
					row.idx, frappe.bold(row.batch_no)
				),
				item_code=row.item_code,
				row=row.idx,
				batch_no=row.batch_no,
				table=table,
			)
		)
		return errors

	if batch.item and batch.item != row.item_code:
		errors.append(
			make_error(
				category="batch_eligibility",
				message=_("Row #{0}: Batch {1} belongs to Item {2}, not {3}").format(
					row.idx, frappe.bold(row.batch_no), frappe.bold(batch.item), frappe.bold(row.item_code)
				),
				item_code=row.item_code,
				row=row.idx,
				batch_no=row.batch_no,
				table=table,
			)
		)
	if cint(batch.disabled):
		errors.append(
			make_error(
				category="batch_eligibility",
				message=_("Row #{0}: Batch {1} is disabled").format(row.idx, frappe.bold(row.batch_no)),
				item_code=row.item_code,
				row=row.idx,
				batch_no=row.batch_no,
				table=table,
			)
		)

	# Expiry vs Delivery Note posting date (NOT today).
	if posting_date and is_batch_expired_on(batch.expiry_date, posting_date):
		errors.append(
			make_error(
				category="batch_expiration",
				message=_(
					"Row #{0}: The batch {1} has already expired. "
					"Expiration date: {2}. Delivery Note date: {3}."
				).format(
					row.idx,
					frappe.bold(row.batch_no),
					formatdate(batch.expiry_date),
					formatdate(posting_date),
				),
				exc_type="BatchExpiredError",
				item_code=row.item_code,
				row=row.idx,
				batch_no=row.batch_no,
				warehouse=row.get("warehouse"),
				table=table,
				extra={
					"expiry_date": str(getdate(batch.expiry_date)),
					"posting_date": str(getdate(posting_date)),
				},
			)
		)
	return errors
