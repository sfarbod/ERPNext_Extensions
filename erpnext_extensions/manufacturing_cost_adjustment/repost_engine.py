# Copyright (c) 2026, ERPNext Extensions contributors
"""Manufacturing Cost Repost Engine (MCRE) — no LCV dependency.

Sequence (TDS Rev.2):
  validate → lock → append SE.additional_costs → Core distribute → Iran
  → persist → update_entries_after (in-place SLE) → repost_gle_for_stock_vouchers
  → ledger contract → queue RIV (no via_landed_cost_voucher) → monitor → Complete
"""

from __future__ import annotations

import json
import traceback
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, flt, now_datetime

from erpnext_extensions.manufacturing_cost_adjustment import constants as C
from erpnext_extensions.manufacturing_cost_adjustment.locks import (
	acquire_se_lock,
	release_se_lock,
)


def _ensure_iran_runtime() -> None:
	"""Apply Iran monkey patches for script / console / non-request MCA runners.

	Desk requests and RQ jobs already run ``before_request`` / ``before_job``
	bootstrap. Bare ``frappe.connect()`` processors (Dev sequential MCA runs)
	do not — without patches, RIV GL rebuild raises Core ``Debit and Credit not
	equal … Difference is ±1`` instead of Iran's IRR residual absorption.
	Idempotent.
	"""
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran_runtime

	apply_iran_runtime()


def _raise_write_limit() -> None:
	"""MCA allocate/rebuild/RIV prep routinely exceeds Frappe's default write cap."""
	limit = cint(frappe.conf.get("max_writes_per_transaction") or 0) or 5_000_000
	try:
		frappe.db.MAX_WRITES_PER_TRANSACTION = limit
	except Exception:
		pass


def _is_quarantined(mca_name: str) -> bool:
	"""Operational exclusion — never auto-process; document left unchanged."""
	return mca_name in C.QUARANTINED_MCA_NAMES


@frappe.whitelist()
def process_mca(mca_name: str) -> dict[str, Any]:
	"""Entry point for enqueue / scheduler / Desk retry. Idempotent by checkpoint."""
	frappe.has_permission(C.MCA_DOCTYPE, "write", throw=True)
	if _is_quarantined(mca_name):
		frappe.logger("mca").warning(f"MCA quarantined — skip process_mca name={mca_name}")
		return {"ok": False, "reason": "quarantined"}
	_ensure_iran_runtime()
	_raise_write_limit()
	mca = frappe.get_doc(C.MCA_DOCTYPE, mca_name)
	mca.check_permission("write")
	if mca.docstatus != 1:
		return {"ok": False, "reason": "not_submitted"}
	if mca.processing_status in (C.PS_COMPLETED, C.PS_VOIDED):
		return {"ok": True, "reason": "already_terminal"}

	owner = f"mca:{mca_name}"
	if not acquire_se_lock(mca.stock_entry, owner):
		_schedule_retry(mca_name, "lock_busy")
		return {"ok": False, "reason": "lock_busy"}

	frappe.flags.in_mca_pipeline = True
	try:
		return _run_pipeline(mca_name)
	except Exception as exc:
		_fail(mca_name, exc)
		return {"ok": False, "reason": "failed", "error": str(exc)}
	finally:
		frappe.flags.in_mca_pipeline = False
		# Keep lock while Pending RIV so concurrent MCA cannot interleave;
		# release on Completed / Voided / Failed-after-compensate.
		try:
			status = frappe.db.get_value(C.MCA_DOCTYPE, mca_name, "processing_status")
		except Exception:
			try:
				frappe.db.rollback()
			except Exception:
				pass
			frappe.connect()
			status = frappe.db.get_value(C.MCA_DOCTYPE, mca_name, "processing_status")
		if status in (C.PS_COMPLETED, C.PS_VOIDED, C.PS_FAILED):
			release_se_lock(mca.stock_entry, owner)


def _run_pipeline(mca_name: str) -> dict[str, Any]:
	mca = frappe.get_doc(C.MCA_DOCTYPE, mca_name)
	se_name = mca.stock_entry
	_assert_se_still_submitted(se_name)

	cp = mca.checkpoint or C.CP_NONE

	if _cp_before(cp, C.CP_COSTS_APPENDED):
		_set_status(mca_name, C.PS_APPENDING)
		_append_costs(mca_name)
		_set_checkpoint(mca_name, C.CP_COSTS_APPENDED)
		frappe.db.commit()

	if _cp_before(cp if (mca.checkpoint or C.CP_NONE) else C.CP_COSTS_APPENDED, C.CP_ALLOCATED):
		# reload checkpoint
		cp = frappe.db.get_value(C.MCA_DOCTYPE, mca_name, "checkpoint") or C.CP_NONE
	if _cp_before(frappe.db.get_value(C.MCA_DOCTYPE, mca_name, "checkpoint") or "", C.CP_ALLOCATED):
		_set_status(mca_name, C.PS_ALLOCATING)
		_allocate_and_persist(mca_name)
		_set_checkpoint(mca_name, C.CP_ALLOCATED)
		frappe.db.commit()

	if _cp_before(frappe.db.get_value(C.MCA_DOCTYPE, mca_name, "checkpoint") or "", C.CP_SLE_REFRESHED):
		_set_status(mca_name, C.PS_REBUILDING_SLE)
		_refresh_sle(se_name)
		_set_checkpoint(mca_name, C.CP_SLE_REFRESHED)
		frappe.db.commit()

	if _cp_before(frappe.db.get_value(C.MCA_DOCTYPE, mca_name, "checkpoint") or "", C.CP_GL_REFRESHED):
		_set_status(mca_name, C.PS_REBUILDING_GL)
		_refresh_gl(se_name)
		_set_checkpoint(mca_name, C.CP_GL_REFRESHED)
		frappe.db.commit()

	if _cp_before(frappe.db.get_value(C.MCA_DOCTYPE, mca_name, "checkpoint") or "", C.CP_CONTRACT_OK):
		_set_status(mca_name, C.PS_VALIDATING)
		try:
			_enforce_contract(se_name)
		except Exception:
			_compensate_restore(mca_name)
			raise
		_set_checkpoint(mca_name, C.CP_CONTRACT_OK)
		frappe.db.commit()

	if _cp_before(frappe.db.get_value(C.MCA_DOCTYPE, mca_name, "checkpoint") or "", C.CP_RIV_QUEUED):
		_set_status(mca_name, C.PS_PENDING_RIV)
		_queue_or_reuse_riv(mca_name)
		_set_checkpoint(mca_name, C.CP_RIV_QUEUED)
		frappe.db.set_value(
			C.MCA_DOCTYPE,
			mca_name,
			{
				"repost_required": 1,
				"repost_status": C.RS_QUEUED,
				"last_repost_attempt_at": now_datetime(),
			},
			update_modified=False,
		)
		frappe.db.commit()

	# Monitor RIV
	return _monitor_riv_and_finalize(mca_name)


def _cp_before(current: str, target: str) -> bool:
	order = [
		C.CP_NONE,
		C.CP_COSTS_APPENDED,
		C.CP_ALLOCATED,
		C.CP_SLE_REFRESHED,
		C.CP_GL_REFRESHED,
		C.CP_CONTRACT_OK,
		C.CP_RIV_QUEUED,
		C.CP_COMPLETED,
	]
	try:
		return order.index(current or C.CP_NONE) < order.index(target)
	except ValueError:
		return True


def _assert_se_still_submitted(se_name: str) -> None:
	ds = frappe.db.get_value("Stock Entry", se_name, "docstatus")
	if cint(ds) != 1:
		frappe.throw(_("Stock Entry {0} is no longer submitted").format(se_name))


def _set_status(mca_name: str, status: str) -> None:
	frappe.db.set_value(C.MCA_DOCTYPE, mca_name, "processing_status", status, update_modified=False)


def _set_checkpoint(mca_name: str, checkpoint: str) -> None:
	frappe.db.set_value(C.MCA_DOCTYPE, mca_name, "checkpoint", checkpoint, update_modified=False)


def _snapshot_sed(se) -> str:
	rows = []
	for d in se.get("items") or []:
		rows.append(
			{
				"name": d.name,
				"item_code": d.item_code,
				"qty": d.qty,
				"basic_amount": d.basic_amount,
				"additional_cost": d.additional_cost,
				"landed_cost_voucher_amount": d.landed_cost_voucher_amount,
				"amount": d.amount,
				"valuation_rate": d.valuation_rate,
				"is_finished_item": d.is_finished_item,
			}
		)
	return json.dumps(
		{
			"total_additional_costs": se.total_additional_costs,
			"items": rows,
			"additional_costs": [
				{
					"name": r.name,
					"expense_account": r.expense_account,
					"base_amount": r.base_amount,
					C.CF_MCA: r.get(C.CF_MCA),
				}
				for r in se.get("additional_costs") or []
			],
		},
		default=str,
	)


def _append_costs(mca_name: str) -> None:
	"""Idempotently append MCA cost rows onto SE.additional_costs."""
	mca = frappe.get_doc(C.MCA_DOCTYPE, mca_name)
	se = frappe.get_doc("Stock Entry", mca.stock_entry)
	se.flags.mca_engine_update = True
	frappe.flags.in_mca_pipeline = True

	if not mca.valuation_before_json:
		frappe.db.set_value(
			C.MCA_DOCTYPE,
			mca_name,
			"valuation_before_json",
			_snapshot_sed(se),
			update_modified=False,
		)

	existing_by_mca_row = {}
	for r in se.get("additional_costs") or []:
		if r.get(C.CF_MCA) == mca_name and r.get(C.CF_MCA_COST_ROW):
			existing_by_mca_row[r.get(C.CF_MCA_COST_ROW)] = r

	from erpnext.controllers.taxes_and_totals import init_landed_taxes_and_totals

	changed = False
	for cost in mca.costs:
		if cost.se_additional_cost_row and cost.name in existing_by_mca_row:
			continue
		if cost.name in existing_by_mca_row:
			# already appended — ensure mapping
			row = existing_by_mca_row[cost.name]
			if not cost.se_additional_cost_row:
				frappe.db.set_value(
					C.MCA_COST_DOCTYPE,
					cost.name,
					"se_additional_cost_row",
					row.name,
					update_modified=False,
				)
			continue

		row = se.append(
			"additional_costs",
			{
				"expense_account": cost.expense_account,
				"account_currency": cost.account_currency,
				"exchange_rate": cost.exchange_rate or 1,
				"amount": cost.amount,
				"description": cost.description,
				"has_operating_cost": cint(cost.has_operating_cost),
				"has_corrective_cost": cint(cost.has_corrective_cost),
				"operation_id": cost.operation_id,
				"qty": cost.qty,
				"operating_component": cost.operating_component,
				"cost_center": cost.cost_center,
				"project": cost.project,
				C.CF_MCA: mca_name,
				C.CF_MCA_COST_ROW: cost.name,
			},
		)
		changed = True

	if changed or True:
		# Always recompute FX base_amount for new rows
		init_landed_taxes_and_totals(se)
		# Persist new children via db_insert (submitted parent)
		se.flags.ignore_validate_update_after_submit = True
		for r in se.get("additional_costs") or []:
			if r.get(C.CF_MCA) != mca_name:
				continue
			if r.get("__islocal") or not r.name or str(r.name).startswith("new-"):
				r.parent = se.name
				r.parenttype = "Stock Entry"
				r.parentfield = "additional_costs"
				r.db_insert()
			else:
				r.db_update()

		# Map names back to MCA children
		se.reload()
		for r in se.get("additional_costs") or []:
			if r.get(C.CF_MCA) != mca_name:
				continue
			mca_row = r.get(C.CF_MCA_COST_ROW)
			if mca_row:
				frappe.db.set_value(
					C.MCA_COST_DOCTYPE,
					mca_row,
					"se_additional_cost_row",
					r.name,
					update_modified=False,
				)


def _allocate_and_persist(mca_name: str) -> None:
	mca = frappe.get_doc(C.MCA_DOCTYPE, mca_name)
	se = frappe.get_doc("Stock Entry", mca.stock_entry)
	se.flags.ignore_validate_update_after_submit = True
	se.flags.mca_engine_update = True
	frappe.flags.in_mca_pipeline = True

	from erpnext.controllers.taxes_and_totals import init_landed_taxes_and_totals

	init_landed_taxes_and_totals(se)
	se.distribute_additional_costs()
	se.update_valuation_rate(reset_outgoing_rate=False)

	# Iran normalization
	try:
		from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
			align_stock_entry_item_amounts,
		)

		align_stock_entry_item_amounts(se)
	except Exception:
		frappe.log_error(title="MCA Iran align", message=frappe.get_traceback())
		raise

	try:
		from erpnext_extensions.iran_accounting.scrap_costing import (
			apply_iran_manufacture_output_contract,
		)

		apply_iran_manufacture_output_contract(se)
	except frappe.ValidationError:
		raise
	except Exception:
		frappe.log_error(title="MCA Iran contract", message=frappe.get_traceback())
		raise

	# Persist SED + header
	for d in se.get("items") or []:
		d.db_update()
	for d in se.get("additional_costs") or []:
		d.db_update()

	se.total_additional_costs = sum(flt(t.base_amount) for t in se.get("additional_costs") or [])
	se.set_total_incoming_outgoing_value()
	frappe.db.set_value(
		"Stock Entry",
		se.name,
		{
			"total_additional_costs": se.total_additional_costs,
			"total_incoming_value": se.total_incoming_value,
			"total_outgoing_value": se.total_outgoing_value,
			"value_difference": se.value_difference,
		},
		update_modified=False,
	)

	from erpnext_extensions.iran_accounting.stock_entry import (
		persist_irr_stock_entry_header_and_rows,
	)

	# Must persist integer basic_rate / amount composition before SLE refresh.
	# Soft-passing here left fractional SED rates that fail ledger contract.
	persist_irr_stock_entry_header_and_rows(se)

	se.reload()
	frappe.db.set_value(
		C.MCA_DOCTYPE,
		mca_name,
		"valuation_after_json",
		_snapshot_sed(se),
		update_modified=False,
	)
	frappe.db.set_value(
		C.MCA_DOCTYPE,
		mca_name,
		"total_additional_costs",
		sum(flt(r.base_amount) for r in mca.costs),
		update_modified=False,
	)


def _refresh_sle(se_name: str) -> None:
	"""In-place SLE valuation refresh via update_entries_after (no LCV flags).

	Core ``update_entries_after`` walks *future* item/warehouse SLEs, including
	dependent Manufacture outputs that share consumed materials. That walk is a
	repost, not a live submit: Iran two-slice / HRC-00015 must use RIV semantics
	(``through_repost_item_valuation``) so mid-chain negative or short SABE pools
	audit+fall-through instead of fail-closing the MCA source refresh.
	Final dependent rebuild remains the queued Transaction RIV.
	"""
	from erpnext.stock.stock_ledger import update_entries_after

	se = frappe.get_doc("Stock Entry", se_name)
	pairs = frappe.db.sql(
		"""
		SELECT DISTINCT item_code, warehouse
		FROM `tabStock Ledger Entry`
		WHERE voucher_type=%s AND voucher_no=%s AND IFNULL(is_cancelled,0)=0
		""",
		("Stock Entry", se_name),
		as_dict=True,
	)
	prev_riv_flag = bool(frappe.flags.get("through_repost_item_valuation"))
	frappe.flags.through_repost_item_valuation = True
	try:
		for p in pairs:
			update_entries_after(
				{
					"item_code": p.item_code,
					"warehouse": p.warehouse,
					"posting_date": se.posting_date,
					"posting_time": se.posting_time,
					"voucher_type": "Stock Entry",
					"voucher_no": se_name,
					"company": se.company,
				},
				allow_negative_stock=True,
			)
	finally:
		frappe.flags.through_repost_item_valuation = prev_riv_flag
	_iran_remirror_sle_from_sed(se_name)


def _integerize_sle_irr_transaction_rates(sle) -> bool:
	"""Integerize SLE rate fields when Core left a fractional warehouse average.

	``reassert_document_amount_authority_sle`` returns early when
	``|SVD − signed SED.amount| ≤ 0.5``, leaving Core's fractional
	``valuation_rate`` (often ``stock_value / qty_after``) untouched. The Iran
	ledger contract still requires integer IRR SLE rates.

	Transaction rate uses existing Iran ``integer_valuation_rate_from_amount``
	on ``|SVD| / |qty|`` — same policy as on_submit Manufacture finalize. Does
	not rewrite SVD from ``qty × rate`` (preserves Class A residual).
	"""
	from erpnext_extensions.iran_accounting.domain.currency import (
		amount_is_fractional,
		get_company_currency,
		integer_valuation_rate_from_amount,
		is_irr_company,
		round_currency,
	)

	company = sle.get("company") if hasattr(sle, "get") else getattr(sle, "company", None)
	if not company or not is_irr_company(company):
		return False
	ccy = get_company_currency(company)
	changed = False
	actual_qty = flt(sle.actual_qty)
	svd = flt(sle.stock_value_difference)
	qty = abs(actual_qty)
	if qty > 1e-9:
		tx_rate = float(integer_valuation_rate_from_amount(abs(svd), qty, ccy))
		if amount_is_fractional(sle.valuation_rate, ccy):
			sle.valuation_rate = tx_rate
			changed = True
		if actual_qty > 0 and amount_is_fractional(sle.incoming_rate, ccy):
			sle.incoming_rate = tx_rate
			changed = True
		elif actual_qty < 0 and amount_is_fractional(sle.outgoing_rate, ccy):
			sle.outgoing_rate = tx_rate
			changed = True
	for field in ("stock_value_difference", "stock_value"):
		val = flt(getattr(sle, field, 0))
		if amount_is_fractional(val, ccy):
			setattr(sle, field, float(round_currency(val, ccy)))
			changed = True
	return changed


def _iran_remirror_sle_from_sed(se_name: str) -> None:
	"""Restore Iran SED + SLE after Core ``update_entries_after`` / RIV rewrites.

	Order is load-bearing (mirrors on_submit Manufacture finalize):

	1. Re-run IRR Manufacture economic finalize + persist so ``basic_rate``,
	   ``basic_amount``, ``amount``, and ``valuation_rate`` are integer IRR and
	   composition-consistent (Core leaves fractional ``basic_rate`` intact).
	2. Integerize SABE rows from the refreshed SLE without abandoning document
	   amount authority (``align_sle=False`` then remirror).
	3. Reassert SLE ``stock_value_difference`` from signed ``SED.amount``.
	4. Integerize any remaining fractional SLE transaction rates (Core warehouse
	   average) without rewriting SVD from ``qty × rate``.

	Never set ``stock_value_difference = qty × valuation_rate`` after remirror —
	that fights Class A amount−rate×qty residual and fails ledger contract.
	"""
	from erpnext_extensions.iran_accounting.domain.currency import is_irr_company
	from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
		reassert_document_amount_authority_sle,
	)
	from erpnext_extensions.iran_accounting.stock_entry import (
		persist_irr_stock_entry_header_and_rows,
	)

	se = frappe.get_doc("Stock Entry", se_name)
	if not is_irr_company(se.company):
		return

	se.flags.ignore_validate_update_after_submit = True
	se.flags.mca_engine_update = True
	frappe.flags.in_mca_pipeline = True
	persist_irr_stock_entry_header_and_rows(se)

	try:
		from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
			apply_integer_sabe_policy_to_sle,
		)
	except ImportError:
		apply_integer_sabe_policy_to_sle = None  # type: ignore

	sles = frappe.get_all(
		"Stock Ledger Entry",
		filters={
			"voucher_type": "Stock Entry",
			"voucher_no": se_name,
			"is_cancelled": 0,
		},
		pluck="name",
	)
	for sle_name in sles:
		sle = frappe.get_doc("Stock Ledger Entry", sle_name)
		changed = False
		if apply_integer_sabe_policy_to_sle and sle.get("serial_and_batch_bundle"):
			try:
				# Localize SABE rates/SVD; do not let SABE qty×rate own SLE movement.
				apply_integer_sabe_policy_to_sle(sle, align_sle=False)
				changed = True
			except Exception:
				frappe.log_error(title=f"MCA SABE integerize {sle_name}")
		try:
			if reassert_document_amount_authority_sle(sle):
				changed = True
		except Exception:
			frappe.log_error(title=f"MCA SLE amount authority {sle_name}")
		# Always close the fractional-rate hole even when SVD already matches SED.
		if _integerize_sle_irr_transaction_rates(sle):
			changed = True
		if changed:
			sle.db_update()


def _refresh_gl(se_name: str) -> None:
	"""RIV-style GL delete+repost for THIS Stock Entry only."""
	from erpnext.accounts.utils import repost_gle_for_stock_vouchers

	se = frappe.db.get_value(
		"Stock Entry", se_name, ["posting_date", "company"], as_dict=True
	)
	repost_gle_for_stock_vouchers(
		[("Stock Entry", se_name)],
		posting_date=se.posting_date,
		company=se.company,
	)


def _enforce_contract(se_name: str) -> None:
	from erpnext_extensions.iran_accounting.domain.stock_entry_ledger_contract import (
		enforce_stock_entry_ledger_contract,
	)

	# API expects voucher_no str, not Document
	enforce_stock_entry_ledger_contract(se_name, raise_on_fail=True)


def _compensate_restore(mca_name: str) -> None:
	"""On contract failure after SLE write: remove MCA-tagged rows and re-allocate."""
	mca = frappe.get_doc(C.MCA_DOCTYPE, mca_name)
	se = frappe.get_doc("Stock Entry", mca.stock_entry)
	se.flags.ignore_validate_update_after_submit = True

	to_remove = [r for r in se.get("additional_costs") or [] if r.get(C.CF_MCA) == mca_name]
	for r in to_remove:
		frappe.db.delete("Landed Cost Taxes and Charges", {"name": r.name})

	se.reload()
	from erpnext.controllers.taxes_and_totals import init_landed_taxes_and_totals

	init_landed_taxes_and_totals(se)
	se.distribute_additional_costs()
	se.update_valuation_rate(reset_outgoing_rate=False)
	from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
		align_stock_entry_item_amounts,
	)
	from erpnext_extensions.iran_accounting.stock_entry import (
		persist_irr_stock_entry_header_and_rows,
	)

	align_stock_entry_item_amounts(se)
	for d in se.get("items") or []:
		d.db_update()
	for d in se.get("additional_costs") or []:
		d.db_update()
	se.total_additional_costs = sum(flt(t.base_amount) for t in se.get("additional_costs") or [])
	se.set_total_incoming_outgoing_value()
	frappe.db.set_value(
		"Stock Entry",
		se.name,
		{
			"total_additional_costs": se.total_additional_costs,
			"total_incoming_value": se.total_incoming_value,
			"total_outgoing_value": se.total_outgoing_value,
			"value_difference": se.value_difference,
		},
		update_modified=False,
	)
	persist_irr_stock_entry_header_and_rows(se)
	_refresh_sle(se.name)
	_refresh_gl(se.name)
	# Clear MCA mappings
	for cost in mca.costs:
		frappe.db.set_value(
			C.MCA_COST_DOCTYPE, cost.name, "se_additional_cost_row", None, update_modified=False
		)
	_set_checkpoint(mca_name, C.CP_NONE)


def _queue_or_reuse_riv(mca_name: str) -> None:
	"""Create Transaction RIV without via_landed_cost_voucher; reuse if present."""
	from erpnext.controllers.stock_controller import create_repost_item_valuation_entry

	mca = frappe.get_doc(C.MCA_DOCTYPE, mca_name)
	se = frappe.get_doc("Stock Entry", mca.stock_entry)

	existing = _parse_riv_list(mca.riv_voucher_nos)
	alive = []
	for riv in existing:
		st = frappe.db.get_value("Repost Item Valuation", riv, "status")
		if st in ("Queued", "In Progress"):
			alive.append(riv)
		elif st == "Failed":
			# allow recreate
			pass
		elif st in ("Completed", "Skipped"):
			alive.append(riv)

	if any(
		frappe.db.get_value("Repost Item Valuation", r, "status") in ("Queued", "In Progress")
		for r in alive
	):
		frappe.db.set_value(
			C.MCA_DOCTYPE,
			mca_name,
			"riv_voucher_nos",
			"\n".join(sorted(set(alive))),
			update_modified=False,
		)
		for riv in alive:
			_kick_riv_execution(riv)
		return

	# Also detect existing open RIV for this voucher
	open_riv = frappe.get_all(
		"Repost Item Valuation",
		filters={
			"voucher_type": "Stock Entry",
			"voucher_no": se.name,
			"docstatus": 1,
			"status": ("in", ("Queued", "In Progress")),
		},
		pluck="name",
	)
	if open_riv:
		frappe.db.set_value(
			C.MCA_DOCTYPE,
			mca_name,
			"riv_voucher_nos",
			"\n".join(open_riv),
			update_modified=False,
		)
		for riv in open_riv:
			_kick_riv_execution(riv)
		return

	before = set(
		frappe.get_all(
			"Repost Item Valuation",
			filters={"voucher_type": "Stock Entry", "voucher_no": se.name, "docstatus": 1},
			pluck="name",
		)
	)
	create_repost_item_valuation_entry(
		frappe._dict(
			{
				"posting_date": se.posting_date,
				"posting_time": se.posting_time,
				"voucher_type": "Stock Entry",
				"voucher_no": se.name,
				"company": se.company,
				# Explicitly NOT via_landed_cost_voucher
				"via_landed_cost_voucher": 0,
			}
		)
	)
	after = set(
		frappe.get_all(
			"Repost Item Valuation",
			filters={"voucher_type": "Stock Entry", "voucher_no": se.name, "docstatus": 1},
			pluck="name",
		)
	)
	new_ones = sorted(after - before) or sorted(after)
	frappe.db.set_value(
		C.MCA_DOCTYPE,
		mca_name,
		{
			"riv_voucher_nos": "\n".join(new_ones),
			"repost_attempt_count": cint(mca.repost_attempt_count) + 1,
		},
		update_modified=False,
	)
	# Do not wait solely on the hourly RIV timeslot — kick execution immediately.
	for riv in new_ones:
		_kick_riv_execution(riv)


def _kick_riv_execution(riv_name: str) -> None:
	"""Ensure a Queued/In-Progress RIV actually runs (worker-independent nudge).

	Outside tests, ERPNext leaves RIV Queued until scheduler timeslot. MCA must
	not abandon pending capitalization — actively execute or enqueue the RIV.
	"""
	if not riv_name or not frappe.db.exists("Repost Item Valuation", riv_name):
		return
	status = frappe.db.get_value("Repost Item Valuation", riv_name, "status")
	if status not in ("Queued", "In Progress"):
		return
	_ensure_iran_runtime()
	try:
		from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
			execute_reposting_entry,
		)

		# Sync path for unit tests and explicit inline runners
		if getattr(frappe, "in_test", False) or getattr(frappe.flags, "in_test", False):
			execute_reposting_entry(riv_name)
			return
		if getattr(frappe.flags, "mca_inline_riv", False):
			execute_reposting_entry(riv_name)
			return

		# When process_mca itself is an RQ job on the dedicated ``mca`` worker,
		# do NOT run multi-hour RIV inline — that leaves Queue Type=mca "busy"
		# for one voucher and starves other MCAs. Enqueue onto Core's ``long``
		# queue (same as repost_item_valuation.py) so long workers consume RIV.
		in_rq = False
		try:
			from rq import get_current_job

			in_rq = get_current_job() is not None
		except Exception:
			in_rq = False

		if in_rq or getattr(frappe.flags, "mca_riv_enqueue_only", False):
			frappe.enqueue(
				"erpnext.stock.doctype.repost_item_valuation.repost_item_valuation.execute_reposting_entry",
				name=riv_name,
				queue=C.MCA_RIV_RQ_QUEUE,
				timeout=C.MCA_JOB_TIMEOUT,
				job_id=f"mca-riv-exec:{riv_name}",
				deduplicate=True,
			)
			frappe.logger("mca").info(
				f"MCA RIV enqueued riv={riv_name} queue={C.MCA_RIV_RQ_QUEUE}"
			)
			return

		# Console / sequential Dev processors: execute sync, enqueue-long on failure
		try:
			execute_reposting_entry(riv_name)
		except Exception:
			frappe.enqueue(
				"erpnext.stock.doctype.repost_item_valuation.repost_item_valuation.execute_reposting_entry",
				name=riv_name,
				queue=C.MCA_RIV_RQ_QUEUE,
				timeout=C.MCA_JOB_TIMEOUT,
				job_id=f"mca-riv-exec:{riv_name}",
				deduplicate=True,
			)
	except Exception as exc:
		frappe.log_error(title=f"MCA RIV kick failed {riv_name}", message=str(exc))


def _parse_riv_list(text: str | None) -> list[str]:
	if not text:
		return []
	return [x.strip() for x in str(text).splitlines() if x.strip()]


def _monitor_riv_and_finalize(mca_name: str) -> dict[str, Any]:
	mca = frappe.get_doc(C.MCA_DOCTYPE, mca_name)
	rivs = _parse_riv_list(mca.riv_voucher_nos)
	if not rivs:
		_queue_or_reuse_riv(mca_name)
		mca.reload()
		rivs = _parse_riv_list(mca.riv_voucher_nos)

	# Kick any still-queued RIVs before deciding pending
	for riv in rivs:
		st = frappe.db.get_value("Repost Item Valuation", riv, "status")
		if st == "Queued":
			_kick_riv_execution(riv)

	statuses = {
		r: frappe.db.get_value("Repost Item Valuation", r, "status") for r in rivs if frappe.db.exists("Repost Item Valuation", r)
	}
	if not statuses:
		_schedule_retry(mca_name, "riv_missing")
		frappe.db.set_value(
			C.MCA_DOCTYPE,
			mca_name,
			{"repost_status": C.RS_FAILED, "last_repost_error": "RIV missing"},
			update_modified=False,
		)
		return {"ok": False, "reason": "riv_missing"}

	if any(s in ("Queued", "In Progress") for s in statuses.values()):
		_set_status(mca_name, C.PS_PENDING_RIV)
		frappe.db.set_value(
			C.MCA_DOCTYPE,
			mca_name,
			{"repost_status": C.RS_IN_PROGRESS, "repost_required": 1},
			update_modified=False,
		)
		_schedule_retry(mca_name, "riv_in_progress", delay=60)
		return {"ok": False, "reason": "riv_pending", "statuses": statuses}

	if any(s == "Failed" for s in statuses.values()):
		# Drop Failed RIV refs so `_queue_or_reuse_riv` can recreate. Restore
		# source SE SED/SLE integer IRR rates — a partial RIV walk can leave
		# Core fractional basic_rate on the Manufacture voucher.
		mca = frappe.get_doc(C.MCA_DOCTYPE, mca_name)
		alive = [
			r
			for r, st in statuses.items()
			if st not in ("Failed", None)
		]
		frappe.db.set_value(
			C.MCA_DOCTYPE,
			mca_name,
			{
				"riv_voucher_nos": "\n".join(alive),
				"repost_status": C.RS_FAILED,
				"repost_required": 1,
				"last_repost_error": f"RIV Failed: {statuses}",
				"processing_status": C.PS_PENDING_RIV,
				"checkpoint": C.CP_CONTRACT_OK,
			},
			update_modified=False,
		)
		try:
			_iran_remirror_sle_from_sed(mca.stock_entry)
			_refresh_gl(mca.stock_entry)
			_enforce_contract(mca.stock_entry)
		except Exception as exc:
			frappe.log_error(title=f"MCA remirror after RIV fail {mca_name}", message=str(exc))
		_queue_or_reuse_riv(mca_name)
		_schedule_retry(mca_name, "riv_failed", delay=120)
		return {"ok": False, "reason": "riv_failed", "statuses": statuses}

	# All Completed / Skipped
	_set_status(mca_name, C.PS_RECONCILING)
	_final_reconcile(mca.stock_entry)
	frappe.db.set_value(
		C.MCA_DOCTYPE,
		mca_name,
		{
			"processing_status": C.PS_COMPLETED,
			"checkpoint": C.CP_COMPLETED,
			"repost_required": 0,
			"repost_status": C.RS_COMPLETED,
			"repost_completed_at": now_datetime(),
			"next_retry_at": None,
			"last_repost_error": None,
		},
		update_modified=True,
	)
	release_se_lock(mca.stock_entry, f"mca:{mca_name}")
	frappe.db.commit()
	return {"ok": True, "reason": "completed"}


def _final_reconcile(se_name: str) -> None:
	"""Lightweight invariant checks after RIV."""
	# RIV may rewrite SLE via qty×rate; restore SED amount authority first.
	_iran_remirror_sle_from_sed(se_name)
	_refresh_gl(se_name)

	se = frappe.get_doc("Stock Entry", se_name)
	# Every additional_costs expense account should appear in GL credits
	accounts = {
		r.expense_account
		for r in se.get("additional_costs") or []
		if r.expense_account and flt(r.base_amount)
	}
	if not accounts:
		return
	gl_accounts = set(
		frappe.get_all(
			"GL Entry",
			filters={
				"voucher_type": "Stock Entry",
				"voucher_no": se_name,
				"is_cancelled": 0,
				"account": ("in", list(accounts)),
			},
			pluck="account",
		)
	)
	missing = accounts - gl_accounts
	if missing:
		frappe.throw(
			_("Final reconcile: Additional Cost GL missing for accounts {0}").format(
				", ".join(sorted(missing))
			)
		)
	_enforce_contract(se_name)


def _fail(mca_name: str, exc: BaseException) -> None:
	msg = f"{exc}\n{traceback.format_exc()}"
	transient = _is_transient(str(exc)) or "TooManyWrites" in type(exc).__name__
	# Pipeline may have already exhausted MAX_WRITES — clear the aborted txn first.
	try:
		frappe.db.rollback()
	except Exception:
		pass
	_raise_write_limit()
	try:
		frappe.db.set_value(
			C.MCA_DOCTYPE,
			mca_name,
			{
				"processing_status": C.PS_FAILED,
				"repost_required": 1,
				"repost_status": C.RS_FAILED,
				"last_repost_error": str(exc)[:2000],
				"error_log": msg[:50000],
			},
			update_modified=False,
		)
		frappe.db.commit()
	except Exception as persist_exc:
		frappe.log_error(
			title=f"MCA fail persist {mca_name}",
			message=f"{persist_exc}\noriginal={exc}",
		)
	if transient:
		_schedule_retry(mca_name, "transient", delay=120)
	frappe.log_error(title=f"MCA failed {mca_name}", message=msg)


def _is_transient(msg: str) -> bool:
	return any(m.lower() in msg.lower() for m in C.TRANSIENT_ERROR_MARKERS)


def _schedule_retry(mca_name: str, reason: str, delay: int = 60) -> None:
	from frappe.utils import add_to_date

	attempts = cint(frappe.db.get_value(C.MCA_DOCTYPE, mca_name, "repost_attempt_count"))
	if attempts >= C.MAX_RETRY_ATTEMPTS:
		frappe.db.set_value(
			C.MCA_DOCTYPE,
			mca_name,
			{
				"processing_status": C.PS_FAILED,
				"last_repost_error": f"Max retries exceeded ({reason})",
				"repost_required": 1,
			},
			update_modified=False,
		)
		return
	frappe.db.set_value(
		C.MCA_DOCTYPE,
		mca_name,
		{
			"repost_required": 1,
			"next_retry_at": add_to_date(now_datetime(), seconds=delay),
		},
		update_modified=False,
	)


def _is_permanent_business_failure(mca_name: str) -> bool:
	"""True when last_repost_error is a non-retryable business/integrity failure."""
	err = frappe.db.get_value(C.MCA_DOCTYPE, mca_name, "last_repost_error") or ""
	return any(m in err for m in C.PERMANENT_ERROR_MARKERS)


def process_pending_mca_reposts() -> int:
	"""Scheduler: resume MCAs with repost_required that are due.

	Enqueues onto the dedicated ``mca`` RQ queue (concurrency 1 worker). Skips
	permanent business failures (e.g. negative pool_value) so they stay Failed
	until data repair + manual retry.
	"""
	_ensure_iran_runtime()
	now = now_datetime()
	names = frappe.get_all(
		C.MCA_DOCTYPE,
		filters={
			"docstatus": 1,
			"repost_required": 1,
			"processing_status": (
				"in",
				[
					C.PS_QUEUED,
					C.PS_APPENDING,
					C.PS_ALLOCATING,
					C.PS_REBUILDING_SLE,
					C.PS_REBUILDING_GL,
					C.PS_VALIDATING,
					C.PS_PENDING_RIV,
					C.PS_RECONCILING,
					C.PS_FAILED,
				],
			),
		},
		pluck="name",
		order_by="name",
	)
	processed = 0
	# When running under tests / console, execute inline so RIV does not stall Queued
	inline = getattr(frappe, "in_test", False) or getattr(frappe.flags, "in_test", False) or getattr(
		frappe.flags, "mca_inline_scheduler", False
	)
	for name in names:
		next_at = frappe.db.get_value(C.MCA_DOCTYPE, name, "next_retry_at")
		status = frappe.db.get_value(C.MCA_DOCTYPE, name, "processing_status")
		if status == C.PS_VOIDED:
			continue
		if _is_quarantined(name):
			continue
		if _is_permanent_business_failure(name):
			continue
		# Permanent Failed (no next_retry_at) stays visible until manual retry;
		# only transient failures schedule next_retry_at.
		if status == C.PS_FAILED and (not next_at or next_at > now):
			continue
		# Skip if SE cancelled / missing
		se = frappe.db.get_value(C.MCA_DOCTYPE, name, "stock_entry")
		if not se or not frappe.db.exists("Stock Entry", se):
			continue
		if cint(frappe.db.get_value("Stock Entry", se, "docstatus")) == 2:
			void_mcas_for_stock_entry(se)
			continue
		# Kick linked RIVs first so monitoring can observe progress
		riv_text = frappe.db.get_value(C.MCA_DOCTYPE, name, "riv_voucher_nos")
		for riv in _parse_riv_list(riv_text):
			_kick_riv_execution(riv)
		frappe.logger("mca").info(
			f"MCA schedule enqueue name={name} se={se} status={status} riv={riv_text!r}"
		)
		if inline:
			try:
				process_mca(name)
			except Exception:
				frappe.log_error(title=f"MCA inline sched {name}")
		else:
			frappe.enqueue(
				"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.process_mca",
				mca_name=name,
				queue=C.MCA_RQ_QUEUE,
				timeout=C.MCA_JOB_TIMEOUT,
				job_id=f"mca-sched:{name}",
				deduplicate=True,
			)
		processed += 1
	return processed


def on_riv_change(doc, method=None) -> None:
	"""Hook: when RIV status changes, nudge linked MCA monitors."""
	if doc.voucher_type != "Stock Entry" or not doc.voucher_no:
		return
	mcas = frappe.get_all(
		C.MCA_DOCTYPE,
		filters={
			"docstatus": 1,
			"stock_entry": doc.voucher_no,
			"repost_required": 1,
			"processing_status": ("in", [C.PS_PENDING_RIV, C.PS_FAILED, C.PS_RECONCILING]),
		},
		pluck="name",
	)
	for name in mcas:
		if _is_quarantined(name):
			continue
		rivs = _parse_riv_list(frappe.db.get_value(C.MCA_DOCTYPE, name, "riv_voucher_nos"))
		if doc.name not in rivs and rivs:
			# still related by stock entry
			pass
		frappe.enqueue(
			"erpnext_extensions.manufacturing_cost_adjustment.repost_engine.process_mca",
			mca_name=name,
			queue=C.MCA_RQ_QUEUE,
			timeout=C.MCA_JOB_TIMEOUT,
			enqueue_after_commit=True,
			job_id=f"mca-riv:{name}:{doc.status}",
			deduplicate=True,
		)


def void_mcas_for_stock_entry(stock_entry: str) -> None:
	"""SE cancel: mark linked MCAs voided; clear pending reposts.

	Includes submitted MCAs and any cancelled MCA rows that still carry
	repost_required (orphan flags from partial cancel paths).
	"""
	names = frappe.get_all(
		C.MCA_DOCTYPE,
		filters={"stock_entry": stock_entry, "docstatus": ("in", (1, 2))},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value(
			C.MCA_DOCTYPE,
			name,
			{
				"processing_status": C.PS_VOIDED,
				"repost_required": 0,
				"repost_status": C.RS_NOT_REQUIRED,
				"next_retry_at": None,
			},
			update_modified=False,
		)
		release_se_lock(stock_entry, f"mca:{name}")


def on_stock_entry_cancel(doc, method=None) -> None:
	if doc.doctype != "Stock Entry":
		return
	void_mcas_for_stock_entry(doc.name)
