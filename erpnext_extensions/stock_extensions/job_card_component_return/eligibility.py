# Copyright (c) 2026, ERPNext Extensions contributors
"""Fail-closed eligibility for Job Card component-return Work Order status bypass."""

from __future__ import annotations

from dataclasses import dataclass, field

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_extensions.stock_extensions.job_card_component_return.returnable import (
	get_returnable_by_item_batch,
	jc_item_codes,
	jc_item_names,
)

PURPOSE_RETURN = "Material Transfer for Manufacture"

# Secondary / output types that must never ride the return bypass.
_BLOCKED_SECONDARY = frozenset(
	{
		"Scrap",
		"Co-Product",
		"By-Product",
		"Additional Finished Good",
	}
)

# Work Order statuses that may receive the JC return bypass when In Process.
# Completed/Closed never need the bypass (Core already allows).
_WO_ACTIVE_OK = frozenset({"In Process", "Not Started"})
_WO_BLOCK = frozenset({"Cancelled", "Stopped"})


@dataclass
class BypassDecision:
	allow: bool = False
	is_attempted_jc_return: bool = False
	code: str = ""
	message: str = ""
	details: dict = field(default_factory=dict)


def looks_like_job_card_component_return(stock_entry) -> bool:
	"""Structural signal only — never sufficient alone for bypass."""
	if not cint(stock_entry.get("is_return")):
		return False
	if (stock_entry.get("purpose") or "") != PURPOSE_RETURN:
		return False
	if not stock_entry.get("job_card"):
		return False
	if not stock_entry.get("work_order"):
		return False
	return True


def decide_work_order_status_bypass(stock_entry) -> BypassDecision:
	"""Decide whether the Core ``validate_work_order_status_for_return`` throw may be skipped.

	Call only when Core would otherwise throw (WO not Completed/Closed and is_return).
	"""
	if not looks_like_job_card_component_return(stock_entry):
		return BypassDecision(
			allow=False,
			is_attempted_jc_return=False,
			code="NOT_JC_RETURN",
			message="Not a Job Card component return",
		)

	jc_name = stock_entry.job_card
	wo_name = stock_entry.work_order

	jc = frappe.db.get_value(
		"Job Card",
		jc_name,
		["name", "docstatus", "status", "work_order", "company"],
		as_dict=True,
	)
	if not jc:
		return _block("JC_MISSING", _("Job Card {0} not found").format(jc_name))

	if cint(jc.docstatus) != 1:
		return _block(
			"JC_NOT_SUBMITTED",
			_("Job Card {0} must be submitted to return components (docstatus={1})").format(
				jc_name, jc.docstatus
			),
		)

	if (jc.status or "") == "Cancelled":
		return _block("JC_CANCELLED", _("Job Card {0} is Cancelled").format(jc_name))

	if (jc.work_order or "") != wo_name:
		return _block(
			"WO_MISMATCH",
			_("Stock Entry Work Order {0} does not match Job Card {1} Work Order {2}").format(
				wo_name, jc_name, jc.work_order
			),
		)

	wo_status = None
	pro_doc = getattr(stock_entry, "pro_doc", None)
	if pro_doc:
		wo_status = pro_doc.status
	else:
		wo_status = frappe.db.get_value("Work Order", wo_name, "status")

	if not wo_status:
		return _block("WO_MISSING", _("Work Order {0} not found").format(wo_name))

	if wo_status in _WO_BLOCK or cint(frappe.db.get_value("Work Order", wo_name, "docstatus")) == 2:
		return _block(
			"WO_CANCELLED",
			_("Work Order {0} status {1} does not allow component return").format(wo_name, wo_status),
		)

	# Completed/Closed should not reach here (Core early-return). Still allow if called.
	if wo_status in ("Completed", "Closed"):
		return BypassDecision(allow=True, is_attempted_jc_return=True, code="WO_FINISHED_CORE_OK")

	if wo_status not in _WO_ACTIVE_OK:
		return _block(
			"WO_STATUS_UNSUPPORTED",
			_("Work Order {0} status {1} is not eligible for Job Card return bypass").format(
				wo_name, wo_status
			),
		)

	items = list(stock_entry.get("items") or [])
	if not items:
		return _block("NO_ROWS", _("Return Stock Entry has no items"))

	allowed_items = jc_item_codes(jc_name)
	allowed_jci = jc_item_names(jc_name)
	returnable = get_returnable_by_item_batch(jc_name)

	# Pre-sum document qty per item/batch for over-return within same SE.
	doc_qty: dict[tuple[str, str], float] = {}
	for row in items:
		key = (row.item_code or "", row.batch_no or "")
		doc_qty[key] = doc_qty.get(key, 0.0) + flt(row.transfer_qty or row.qty)

	for row in items:
		decision = _validate_row(
			row,
			row_doc_qty=doc_qty.get((row.item_code or "", row.batch_no or ""), 0.0),
			returnable=returnable,
			allowed_items=allowed_items,
			allowed_jci=allowed_jci,
			jc_name=jc_name,
		)
		if not decision.allow:
			return decision

	return BypassDecision(
		allow=True,
		is_attempted_jc_return=True,
		code="JC_RETURN_OK",
		message="Job Card component return eligible",
		details={"job_card": jc_name, "work_order": wo_name, "wo_status": wo_status},
	)


def _validate_row(
	row,
	*,
	row_doc_qty: float,
	returnable: dict,
	allowed_items: set[str],
	allowed_jci: set[str],
	jc_name: str,
) -> BypassDecision:
	idx = row.idx
	item = row.item_code or ""
	batch = row.batch_no or ""
	qty = flt(row.transfer_qty or row.qty)
	precision = frappe.get_precision("Stock Entry Detail", "qty") or 3

	if cint(row.get("is_finished_item")):
		return _block(
			"FINISHED_GOOD_ROW",
			_("Row #{0}: finished good cannot use Job Card component return bypass").format(idx),
		)
	if cint(row.get("is_scrap_item")):
		return _block(
			"SCRAP_ROW",
			_("Row #{0}: scrap row cannot use Job Card component return bypass").format(idx),
		)
	sec = (row.get("secondary_item_type") or "").strip()
	if sec in _BLOCKED_SECONDARY:
		return _block(
			"SECONDARY_MASQUERADE",
			_("Row #{0}: secondary type {1} cannot masquerade as component return").format(idx, sec),
		)
	out_class = (row.get("custom_output_class") or "").strip()
	if out_class in (
		"COMPONENT_SCRAP",
		"MAIN_PRODUCT_REJECT",
		"CO_PRODUCT",
		"BY_PRODUCT",
		"ADDITIONAL_FINISHED_GOOD",
		"MAIN_FG",
	):
		return _block(
			"OUTPUT_CLASS_MASQUERADE",
			_("Row #{0}: output class {1} is not a component return").format(idx, out_class),
		)

	if not item:
		return _block("NO_ITEM", _("Row #{0}: item_code is required").format(idx))

	if item not in allowed_items:
		return _block(
			"UNRELATED_ITEM",
			_("Row #{0}: item {1} is not a component of Job Card {2}").format(idx, item, jc_name),
		)

	jci = row.get("job_card_item")
	if jci and jci not in allowed_jci:
		return _block(
			"BAD_JOB_CARD_ITEM",
			_("Row #{0}: job_card_item {1} does not belong to Job Card {2}").format(idx, jci, jc_name),
		)

	if qty <= 0:
		return _block(
			"NON_POSITIVE_QTY",
			_("Row #{0}: return qty must be > 0 (got {1})").format(idx, qty),
		)

	key = (item, batch)
	rec = returnable.get(key)
	if not rec:
		# batch-aware: item may exist under other batches
		item_keys = [k for k in returnable if k[0] == item]
		if item_keys and batch:
			return _block(
				"WRONG_BATCH",
				_(
					"Row #{0}: batch {1} has no returnable WIP for item {2} on Job Card {3}"
				).format(idx, batch, item, jc_name),
			)
		if not item_keys:
			return _block(
				"NO_TRANSFER_EVIDENCE",
				_("Row #{0}: no submitted transfer evidence for item {1} on Job Card {2}").format(
					idx, item, jc_name
				),
			)
		return _block(
			"NO_RETURNABLE",
			_("Row #{0}: item {1} / batch {2} has no returnable WIP on Job Card {3}").format(
				idx, item, batch or "—", jc_name
			),
		)

	available = flt(rec["returnable"])
	if available <= 0:
		return _block(
			"FULLY_RETURNED_OR_CONSUMED",
			_("Row #{0}: item {1} / batch {2} has zero returnable WIP on Job Card {3}").format(
				idx, item, batch or "—", jc_name
			),
		)

	# Document-level sum must not exceed returnable (covers multi-row same key).
	if flt(row_doc_qty, precision) > flt(available, precision):
		return _block(
			"OVER_RETURN",
			_(
				"Row #{0}: return qty {1} exceeds current returnable {2} for {3} / batch {4} on Job Card {5}"
			).format(idx, row_doc_qty, available, item, batch or "—", jc_name),
		)

	s_wh = row.get("s_warehouse")
	t_wh = row.get("t_warehouse")
	if not s_wh:
		return _block(
			"MISSING_SOURCE_WH",
			_("Row #{0}: source (WIP) warehouse is required for component return").format(idx),
		)
	if rec["wip_warehouses"] and s_wh not in rec["wip_warehouses"]:
		return _block(
			"WRONG_WIP_WAREHOUSE",
			_(
				"Row #{0}: source warehouse {1} is not the Job Card WIP ownership warehouse for {2}"
			).format(idx, s_wh, item),
		)

	# Destination may be blank while the operator fills the form; Core warehouse
	# validation still applies. When set, it must match original transfer source.
	if t_wh and rec["return_destinations"] and t_wh not in rec["return_destinations"]:
		return _block(
			"INVALID_DESTINATION",
			_(
				"Row #{0}: destination warehouse {1} is not a proven return destination for {2}"
			).format(idx, t_wh, item),
		)

	if jci and jci not in rec["job_card_items"] and rec["job_card_items"]:
		# Soft ambiguity: jci present on JC but not on transfer evidence for this batch.
		# Fail closed when evidence has explicit jci ownership for other rows of same key.
		return _block(
			"AMBIGUOUS_OWNERSHIP",
			_(
				"Row #{0}: job_card_item {1} is not proven for item {2} / batch {3} transfer evidence"
			).format(idx, jci, item, batch or "—"),
		)

	return BypassDecision(allow=True, is_attempted_jc_return=True, code="ROW_OK")


def _block(code: str, message: str) -> BypassDecision:
	return BypassDecision(
		allow=False,
		is_attempted_jc_return=True,
		code=code,
		message=message,
		details={"code": code},
	)

