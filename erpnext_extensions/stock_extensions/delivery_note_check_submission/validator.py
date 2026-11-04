# Copyright (c) 2026, ERPNext Extensions contributors
"""Strict read-only Delivery Note Check Submission (v5.6.34).

Never mutates the database. Never uses savepoint mutation-and-rollback.
Category A: reuse genuine non-mutating ERPNext validators.
Category B: dedicated read-only checks that approximate submit-time conditions.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, strip_html

from erpnext_extensions.iran_accounting.buying_selling import validate_delivery_note as iran_validate_dn
from erpnext_extensions.stock_extensions.delivery_note_check_submission.accounting import (
	validate_accounting_readiness,
)
from erpnext_extensions.stock_extensions.delivery_note_check_submission.limitations import (
	collect_structural_limitations,
	has_blocking_limitations,
)
from erpnext_extensions.stock_extensions.delivery_note_check_submission.serial_batch import (
	validate_serial_and_batch_requirements,
)
from erpnext_extensions.stock_extensions.delivery_note_check_submission.stock_availability import (
	validate_over_delivery,
	validate_stock_availability,
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


def _normalize_error_message(exc: BaseException) -> str:
	msg = frappe.as_unicode(exc)
	if not msg or msg == type(exc).__name__:
		for entry in reversed(getattr(frappe.local, "message_log", None) or []):
			if isinstance(entry, dict) and entry.get("message"):
				msg = frappe.as_unicode(entry.get("message"))
				break
			if isinstance(entry, str) and entry:
				msg = entry
				break
	return (strip_html(msg) or type(exc).__name__).strip()


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
	"""Broad before/after fingerprint — must be identical after a read-only check."""
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


def _run_document_field_validations(doc) -> None:
	"""Category A — Document-level field validations that do not write."""
	# Document._validate_links reads self._action (must exist).
	doc._action = "submit"
	doc.flags.ignore_save_passwords = True
	doc.flags.ignore_permissions = False
	doc._validate_links()
	doc._validate_mandatory()
	doc._validate_data_fields()
	doc._validate_selects()
	doc._validate_non_negative()
	doc._validate_code_fields()
	doc._validate_length()
	for d in doc.get_all_children():
		d._validate_data_fields()
		d._validate_selects()
		d._validate_non_negative()
		d._validate_code_fields()
		d._validate_length()


def _run_category_a_erpnext(doc) -> None:
	"""Category A — call genuinely non-mutating ERPNext DN / controller validators."""
	doc._action = "submit"
	doc.flags.ignore_permissions = False

	# Delivery Note.validate subset (skip set_status, make_packing_list mutate-or-write paths,
	# set_serial_and_batch_bundle_from_pick_list which duplicates bundles).
	doc.validate_posting_time()
	doc.set_internal_return_target_warehouse()
	doc.validate_references()
	doc.so_required()
	doc.validate_proj_cust()
	doc.check_sales_order_on_hold_or_close("against_sales_order")
	doc.validate_warehouse()
	doc.validate_uom_is_integer("stock_uom", "stock_qty")
	doc.validate_uom_is_integer("uom", "qty")
	doc.validate_with_previous_doc()
	doc.validate_against_stock_reservation_entries()

	# SellingController / StockController / AccountsController pure validators
	doc.validate_items()
	if not (doc.get("is_debit_note") or doc.get("is_return")):
		doc.validate_max_discount()
	doc.validate_selling_price()
	doc.validate_price_list_country()
	doc.validate_for_duplicate_items()
	doc.validate_target_warehouse()
	doc.validate_sample_retention_warehouse()

	for table_name in ("items", "packed_items"):
		if doc.get(table_name):
			doc.validate_duplicate_serial_and_batch_bundle(table_name)

	if not doc.get("is_return"):
		doc.validate_inspection()
	doc.validate_warehouse_of_sabb()
	doc.validate_serialized_batch()
	doc.clean_serial_nos()  # in-memory only
	doc.validate_customer_provided_item()
	doc.validate_internal_transfer()
	doc.validate_putaway_capacity()
	doc.validate_inventory_dimension_mandatory()

	doc.validate_date_with_fiscal_year()
	doc.validate_party_accounts()
	doc.validate_party()
	doc.validate_party_specific_items()
	doc.validate_currency()
	doc.validate_party_account_currency()
	if doc.meta.get_field("taxes_and_charges"):
		doc.validate_enabled_taxes_and_charges()
		doc.validate_tax_account_company()
	doc.validate_qty_is_not_zero()

	# Submit-time pure validators
	doc.validate_packed_qty()
	frappe.get_cached_doc("Authorization Control").validate_approving_authority(
		doc.doctype, doc.company, doc.base_grand_total, doc
	)
	if not doc.is_return:
		doc.check_credit_limit()
	doc.validate_standalone_serial_nos_customer()

	# Controlled in-memory Iran IRR alignment (erpnext_extensions hook) — no DB write.
	iran_validate_dn(doc)


def _run_category_b_readonly(doc) -> list[dict]:
	"""Category B — dedicated read-only substitutes for mutating submit operations."""
	limitations: list[dict] = []
	limitations.extend(validate_serial_and_batch_requirements(doc))
	validate_stock_availability(doc)
	validate_over_delivery(doc)
	limitations.extend(validate_accounting_readiness(doc))
	return limitations


def validate_delivery_note_submission(name: str) -> dict[str, Any]:
	"""Strict read-only submission validation for a Draft Delivery Note."""
	doc = _assert_draft_delivery_note(name)
	before = _snapshot(name)

	prior_messages = list(getattr(frappe.local, "message_log", None) or [])
	errors: list[dict[str, str]] = []
	limitations: list[dict] = []
	checks_passed = False

	# Structural limitations are collected before the guarded window (read queries only).
	limitations.extend(collect_structural_limitations(doc))

	try:
		with enforce_read_only():
			work = frappe.get_doc("Delivery Note", name)
			_run_document_field_validations(work)
			_run_category_a_erpnext(work)
			limitations.extend(_run_category_b_readonly(work))
			checks_passed = True
	except frappe.PermissionError:
		raise
	except ReadOnlyViolation as exc:
		frappe.log_error(
			title="DN Check Submission read-only violation",
			message=frappe.as_json({"name": name, "error": _normalize_error_message(exc)}),
		)
		frappe.throw(
			_(
				"Check Submission aborted because a write or external side effect was attempted. "
				"No database changes were intended; please contact your system administrator. ({0})"
			).format(_normalize_error_message(exc)),
			ReadOnlyViolation,
		)
	except Exception as exc:
		errors.append({"exc_type": type(exc).__name__, "message": _normalize_error_message(exc)})
		checks_passed = False
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

	blocking = has_blocking_limitations(limitations)
	# Fully ready only when checks passed AND no blocking limitations.
	ok = bool(checks_passed and not errors and not blocking)

	result: dict[str, Any] = {
		"ok": ok,
		"ready_for_submission": ok,
		"name": name,
		"docstatus": cint(after["dn"]["docstatus"]) if after.get("dn") else 0,
		"errors": errors,
		"limitations": limitations,
		"skipped_checks": [l.get("code") for l in limitations if l.get("code")],
		"side_effects_clean": True,
		"architecture": "strict_read_only",
		"mutated": False,
	}

	if ok:
		result["message"] = SUCCESS_MESSAGE
		result["caveat"] = SUCCESS_CAVEAT
	elif checks_passed and not errors and blocking:
		result["message"] = INCOMPLETE_MESSAGE
		result["caveat"] = SUCCESS_CAVEAT
	else:
		result["message"] = _("Submission validation failed.")

	return result
