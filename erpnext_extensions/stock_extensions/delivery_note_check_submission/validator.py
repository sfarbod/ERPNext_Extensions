# Copyright (c) 2026, ERPNext Extensions contributors
"""Strict read-only Delivery Note Check Submission (v5.6.37).

Never mutates the database. Never uses savepoint mutation-and-rollback.
Category A: reuse genuine non-mutating ERPNext validators (run as independent groups).
Category B: dedicated read-only checks (historical stock / batch expiry / serials).

Validation errors from independent groups are aggregated; one failing item does not
stop validation of unrelated rows/groups.
"""

from __future__ import annotations

from typing import Any, Callable

import frappe
from frappe import _
from frappe.utils import cint

from erpnext_extensions.iran_accounting.buying_selling import validate_delivery_note as iran_validate_dn
from erpnext_extensions.stock_extensions.delivery_note_check_submission.accounting import (
	collect_accounting_errors,
)
from erpnext_extensions.stock_extensions.delivery_note_check_submission.errors import (
	catch_validation,
	dedupe_errors,
	make_error,
	normalize_message,
)
from erpnext_extensions.stock_extensions.delivery_note_check_submission.limitations import (
	collect_structural_limitations,
	has_blocking_limitations,
)
from erpnext_extensions.stock_extensions.delivery_note_check_submission.serial_batch import (
	collect_serial_and_batch_errors,
)
from erpnext_extensions.stock_extensions.delivery_note_check_submission.stock_availability import (
	collect_over_delivery_errors,
	collect_stock_availability_errors,
)
from erpnext_extensions.stock_extensions.delivery_note_check_submission.write_guard import (
	ReadOnlyViolation,
	enforce_read_only,
)

SUCCESS_MESSAGE = _(
	"All supported read-only submission validations passed. "
	"This Delivery Note has no detected blockers among the checks that were run."
)
SUCCESS_CAVEAT = _(
	"This result only confirms read-only validation at the time of checking and does not "
	"guarantee that stock or other conditions will remain unchanged until actual submission. "
	"See limitations for validations that were not (or cannot be) fully reproduced."
)
INCOMPLETE_MESSAGE = _(
	"Supported checks found no errors, but one or more important validations could not be "
	"performed safely in read-only mode. This Delivery Note is NOT certified ready for submission."
)


def _permission_ok(doc) -> None:
	if not frappe.has_permission("Delivery Note", "write", doc=doc):
		frappe.throw(
			_("You need Write permission on Delivery Note to run Check Submission."),
			frappe.PermissionError,
		)


def _assert_draft_delivery_note(name: str):
	if not name:
		frappe.throw(_("Delivery Note name is required."), frappe.ValidationError)

	meta = frappe.db.get_value("Delivery Note", name, ["name", "docstatus"], as_dict=True)
	if not meta:
		frappe.throw(_("Delivery Note {0} does not exist.").format(name), frappe.DoesNotExistError)

	if cint(meta.docstatus) != 0:
		frappe.throw(
			_("Check Submission is only available for Draft Delivery Notes."),
			frappe.ValidationError,
		)

	doc = frappe.get_doc("Delivery Note", name)
	_permission_ok(doc)
	return doc


def _snapshot(name: str) -> dict[str, Any]:
	dn = frappe.db.get_value(
		"Delivery Note", name, ["docstatus", "modified", "status", "per_billed"], as_dict=True
	)
	items = frappe.get_all(
		"Delivery Note Item",
		filters={"parent": name},
		fields=["name", "item_code", "warehouse", "qty", "stock_qty", "against_sales_order", "so_detail"],
	)
	so_names = sorted({r.against_sales_order for r in items if r.against_sales_order})
	so_items = []
	if so_names:
		so_items = frappe.get_all(
			"Sales Order Item",
			filters={"parent": ("in", so_names)},
			fields=["name", "delivered_qty", "qty", "parent"],
		)
	so_headers = []
	if so_names:
		so_headers = frappe.get_all(
			"Sales Order",
			filters={"name": ("in", so_names)},
			fields=["name", "status", "per_delivered", "modified"],
		)

	item_wh = {(r.item_code, r.warehouse) for r in items if r.item_code and r.warehouse}
	bins = []
	for item_code, warehouse in item_wh:
		bins.extend(
			frappe.get_all(
				"Bin",
				filters={"item_code": item_code, "warehouse": warehouse},
				fields=["name", "item_code", "warehouse", "actual_qty", "reserved_qty", "reserved_stock"],
			)
		)

	sre = []
	so_details = [r.so_detail for r in items if r.so_detail]
	if so_details:
		sre = frappe.get_all(
			"Stock Reservation Entry",
			filters={"voucher_detail_no": ("in", so_details), "docstatus": 1},
			fields=["name", "reserved_qty", "delivered_qty", "status", "modified"],
		)

	return {
		"dn": dn,
		"items": items,
		"so_headers": so_headers,
		"so_items": so_items,
		"bins": bins,
		"sre": sre,
		"sle": frappe.db.count(
			"Stock Ledger Entry", {"voucher_type": "Delivery Note", "voucher_no": name}
		),
		"gle": frappe.db.count("GL Entry", {"voucher_type": "Delivery Note", "voucher_no": name}),
		"sabb": frappe.db.count(
			"Serial and Batch Bundle", {"voucher_type": "Delivery Note", "voucher_no": name}
		),
	}


def _prepare_doc(doc) -> None:
	doc._action = "submit"
	doc.flags.ignore_save_passwords = True
	doc.flags.ignore_permissions = False


def _run_group(category: str, fn: Callable) -> list[dict]:
	return catch_validation(category, fn)


def _collect_document_field_errors(doc) -> list[dict]:
	errors: list[dict] = []
	# Run each field validation independently so one failure does not hide others.
	for category, fn in (
		("links", doc._validate_links),
		("mandatory_fields", doc._validate_mandatory),
		("data_fields", doc._validate_data_fields),
		("select_fields", doc._validate_selects),
		("non_negative", doc._validate_non_negative),
		("code_fields", doc._validate_code_fields),
		("field_length", doc._validate_length),
	):
		errors.extend(_run_group(category, fn))

	def _child_fields():
		for d in doc.get_all_children():
			d._validate_data_fields()
			d._validate_selects()
			d._validate_non_negative()
			d._validate_code_fields()
			d._validate_length()

	errors.extend(_run_group("child_fields", _child_fields))
	return errors


def _collect_warehouse_row_errors(doc) -> list[dict]:
	"""Per-row warehouse presence for stock items (complements doc.validate_warehouse)."""
	errors: list[dict] = []
	for table in ("items", "packed_items"):
		for row in doc.get(table) or []:
			if not row.get("item_code"):
				continue
			if not cint(frappe.get_cached_value("Item", row.item_code, "is_stock_item")):
				continue
			if not row.get("warehouse"):
				errors.append(
					make_error(
						category="warehouse",
						message=_("Row #{0}: Warehouse required for stock Item {1}").format(
							row.idx, frappe.bold(row.item_code)
						),
						item_code=row.item_code,
						row=row.idx,
						table=table,
					)
				)
	return errors


def _run_category_a_groups(doc) -> list[dict]:
	"""Independent Category A ERPNext validators — one failure does not stop the rest."""
	errors: list[dict] = []

	groups: list[tuple[str, Callable]] = [
		("posting_time", doc.validate_posting_time),
		("internal_return_warehouse", doc.set_internal_return_target_warehouse),
		("references", doc.validate_references),
		("so_required", doc.so_required),
		("project_customer", doc.validate_proj_cust),
		("sales_order_hold", lambda: doc.check_sales_order_on_hold_or_close("against_sales_order")),
		("warehouse", doc.validate_warehouse),
		("uom_stock", lambda: doc.validate_uom_is_integer("stock_uom", "stock_qty")),
		("uom_qty", lambda: doc.validate_uom_is_integer("uom", "qty")),
		("previous_doc", doc.validate_with_previous_doc),
		("stock_reservation_warehouse", doc.validate_against_stock_reservation_entries),
		("items", doc.validate_items),
		("max_discount", lambda: None if (doc.get("is_debit_note") or doc.get("is_return")) else doc.validate_max_discount()),
		("selling_price", doc.validate_selling_price),
		("price_list_country", doc.validate_price_list_country),
		("duplicate_items", doc.validate_for_duplicate_items),
		("target_warehouse", doc.validate_target_warehouse),
		("sample_retention", doc.validate_sample_retention_warehouse),
		(
			"duplicate_serial_batch_bundle",
			lambda: [
				doc.validate_duplicate_serial_and_batch_bundle(t)
				for t in ("items", "packed_items")
				if doc.get(t)
			],
		),
		("inspection", lambda: None if doc.get("is_return") else doc.validate_inspection()),
		("sabb_warehouse", doc.validate_warehouse_of_sabb),
		# Batch expiry / serial-batch eligibility are collected per-row in Category B
		# (serial_batch.collect_serial_and_batch_errors) so all rows are reported.
		("clean_serial_nos", doc.clean_serial_nos),
		("customer_provided_item", doc.validate_customer_provided_item),
		("internal_transfer", doc.validate_internal_transfer),
		("putaway_capacity", doc.validate_putaway_capacity),
		("inventory_dimension", doc.validate_inventory_dimension_mandatory),
		("fiscal_year", doc.validate_date_with_fiscal_year),
		("party_accounts", doc.validate_party_accounts),
		("party", doc.validate_party),
		("party_specific_items", doc.validate_party_specific_items),
		("currency", doc.validate_currency),
		("party_account_currency", doc.validate_party_account_currency),
		(
			"taxes_enabled",
			lambda: doc.validate_enabled_taxes_and_charges()
			if doc.meta.get_field("taxes_and_charges")
			else None,
		),
		(
			"tax_account_company",
			lambda: doc.validate_tax_account_company() if doc.meta.get_field("taxes_and_charges") else None,
		),
		("qty_not_zero", doc.validate_qty_is_not_zero),
		("packed_qty", doc.validate_packed_qty),
		(
			"approving_authority",
			lambda: frappe.get_cached_doc("Authorization Control").validate_approving_authority(
				doc.doctype, doc.company, doc.base_grand_total, doc
			),
		),
		("credit_limit", lambda: None if doc.is_return else doc.check_credit_limit()),
		("standalone_serial_customer", doc.validate_standalone_serial_nos_customer),
		("iran_align", lambda: iran_validate_dn(doc)),
	]

	for category, fn in groups:
		errors.extend(_run_group(category, fn))

	return errors


def _run_all_validations(doc) -> tuple[list[dict], list[dict], list[dict]]:
	"""Run all independent groups; return (errors, warnings, limitations)."""
	errors: list[dict] = []
	warnings: list[dict] = []
	limitations: list[dict] = []

	_prepare_doc(doc)

	errors.extend(_collect_document_field_errors(doc))
	errors.extend(_collect_warehouse_row_errors(doc))
	errors.extend(_run_category_a_groups(doc))

	# Category B collectors (already aggregate per row).
	sb_errors, sb_limitations = collect_serial_and_batch_errors(doc)
	errors.extend(sb_errors)
	limitations.extend(sb_limitations)

	errors.extend(collect_stock_availability_errors(doc))
	errors.extend(collect_over_delivery_errors(doc))

	acc_errors, acc_limitations = collect_accounting_errors(doc)
	errors.extend(acc_errors)
	limitations.extend(acc_limitations)

	errors = dedupe_errors(errors)
	return errors, warnings, limitations


def validate_delivery_note_submission(name: str) -> dict[str, Any]:
	"""Strict read-only submission validation for a Draft Delivery Note."""
	doc = _assert_draft_delivery_note(name)
	before = _snapshot(name)

	prior_messages = list(getattr(frappe.local, "message_log", None) or [])
	errors: list[dict] = []
	warnings: list[dict] = []
	limitations: list[dict] = []
	validation_completed = False

	limitations.extend(collect_structural_limitations(doc))

	try:
		with enforce_read_only():
			work = frappe.get_doc("Delivery Note", name)
			errs, warns, lims = _run_all_validations(work)
			errors.extend(errs)
			warnings.extend(warns)
			limitations.extend(lims)
			validation_completed = True
	except frappe.PermissionError:
		raise
	except ReadOnlyViolation as exc:
		frappe.log_error(
			title="DN Check Submission read-only violation",
			message=frappe.as_json({"name": name, "error": normalize_message(exc)}),
		)
		frappe.throw(
			_(
				"Check Submission aborted because a write or external side effect was attempted. "
				"No database changes were intended; please contact your system administrator. ({0})"
			).format(normalize_message(exc)),
			ReadOnlyViolation,
		)
	except Exception as exc:
		# Unexpected failure outside group collectors.
		errors.append(
			make_error(
				category="unexpected",
				message=exc,
				exc_type=type(exc).__name__,
			)
		)
		validation_completed = False
	finally:
		frappe.local.message_log = prior_messages

	after = _snapshot(name)
	if frappe.as_json(after) != frappe.as_json(before):
		frappe.log_error(
			title="DN Check Submission mutation detected",
			message=frappe.as_json({"name": name, "before": before, "after": after}),
		)
		frappe.throw(
			_(
				"Check Submission aborted because a database mutation was detected. "
				"This violates the read-only architecture; please contact your system administrator."
			),
			frappe.ValidationError,
		)

	errors = dedupe_errors(errors)
	blocking = has_blocking_limitations(limitations)
	ok = bool(validation_completed and not errors and not blocking)

	if ok:
		status = "pass"
	elif validation_completed and not errors and blocking:
		status = "incomplete"
	else:
		status = "fail"

	result: dict[str, Any] = {
		"status": status,
		"ok": ok,
		"ready_for_submission": ok,
		"name": name,
		"docstatus": cint(after["dn"]["docstatus"]) if after.get("dn") else 0,
		"errors": errors,
		"warnings": warnings,
		"limitations": limitations,
		"summary": {
			"errors": len(errors),
			"warnings": len(warnings),
			"limitations": len(limitations),
			"blocking_limitations": sum(1 for l in limitations if l.get("severity") == "blocking"),
		},
		"skipped_checks": [l.get("code") for l in limitations if l.get("code")],
		"side_effects_clean": True,
		"architecture": "strict_read_only",
		"mutated": False,
	}

	if ok:
		result["message"] = SUCCESS_MESSAGE
		result["caveat"] = SUCCESS_CAVEAT
	elif status == "incomplete":
		result["message"] = INCOMPLETE_MESSAGE
		result["caveat"] = SUCCESS_CAVEAT
	else:
		result["message"] = _("Submission validation failed — {0} error(s).").format(len(errors))

	return result
