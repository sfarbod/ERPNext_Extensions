# Copyright (c) 2026, ERPNext Extensions contributors
"""Synchronous valuation adapter for atomic Manufacture repair (v5.5.1).

Suppresses automatic async RIV creation during repair and runs scoped
``update_entries_after`` without RIV progress commits. Dependant SLE
expansion is limited to repair Item×Warehouse roots (performance hotfix).
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterable

import frappe
from frappe.utils import cint


@contextmanager
def _scope_dependant_repost(
	allowed_pairs: set[tuple[str, str]],
	already_covered: dict[tuple[str, str], object] | None = None,
):
	"""Limit Manufacture/Repack dependant expansion to Item×Warehouse roots in scope.

	Core ``include_dependant_sle_in_reposting`` walks every finished-good ledger that
	shared a raw material after the posting point. For a July historical repair that
	means months of unrelated plant production. Our caller already queues every
	Item×Warehouse touched by the repair vouchers, so dependants outside that set are
	redundant for the repair's authoritative valuation and dominate runtime.

	``already_covered`` is shared across sequential roots in one sync call so a later
	raw-material root does not re-walk finished-good ledgers already reposted.
	"""
	from erpnext.stock import stock_ledger as sl

	original = sl.update_entries_after.include_dependant_sle_in_reposting
	covered = already_covered if already_covered is not None else {}

	def _scoped(self, sle):
		from collections import deque

		before_keys = set(getattr(self, "distinct_dependant_item_wh", set()) or set())
		original(self, sle)
		extra_keys = (getattr(self, "distinct_dependant_item_wh", set()) or set()) - before_keys
		# Drop out-of-scope dependants and dependants already covered by earlier roots.
		drop_keys = set()
		for k in extra_keys:
			if k not in allowed_pairs:
				drop_keys.add(k)
				continue
			cov = covered.get(k)
			if cov is None:
				continue
			# If already covered from an earlier-or-equal datetime, do not walk again.
			dep_dt = None
			if getattr(self, "reposted_dependant_item_wh", None):
				dep_dt = self.reposted_dependant_item_wh.get(k)
			if dep_dt is not None and cov <= dep_dt:
				drop_keys.add(k)
		if not drop_keys:
			return
		for k in drop_keys:
			self.distinct_dependant_item_wh.discard(k)
			if getattr(self, "reposted_dependant_item_wh", None) is not None:
				self.reposted_dependant_item_wh.pop(k, None)
		allowed = (set(allowed_pairs) | before_keys) - drop_keys
		# Always keep the SLE's own item×warehouse queue rows.
		allowed.add((sle.item_code, sle.warehouse))
		kept = list(getattr(self, "_sles", []) or [])

		def _key(row):
			if isinstance(row, dict):
				return (row.get("item_code"), row.get("warehouse"))
			return (getattr(row, "item_code", None), getattr(row, "warehouse", None))

		filtered = [row for row in kept if _key(row) in allowed]
		self._sles = deque(self.sort_sles(filtered)) if hasattr(self, "sort_sles") else deque(filtered)
		self._sle_batch = {}

	sl.update_entries_after.include_dependant_sle_in_reposting = _scoped
	try:
		yield
	finally:
		sl.update_entries_after.include_dependant_sle_in_reposting = original


@contextmanager
def suppress_auto_riv():
	"""Repair context: no auto RIV, soft WO op-status, workflow state set without transition gate."""
	from erpnext.controllers.stock_controller import StockController
	from erpnext.manufacturing.doctype.work_order.work_order import WorkOrder
	from frappe.model.document import Document
	from frappe.model.workflow import set_workflow_state_on_action
	from frappe.utils import flt

	original_riv = StockController.repost_future_sle_and_gle
	original_op_status = WorkOrder.update_operation_status
	original_wf = Document.validate_workflow
	frappe.flags.jc_manufacture_repair = True

	def _noop(self, force=False, via_landed_cost_voucher=False):
		return None

	def _soft_update_operation_status(self):
		"""Same as Core, but clamp over-complete ops instead of throwing.

		Some historical WOs already have operation completed_qty > WO qty.
		Repair cancel/submit must not be blocked by that pre-existing state.
		"""
		allowance_percentage = flt(
			frappe.db.get_single_value("Manufacturing Settings", "overproduction_percentage_for_work_order")
		)
		max_allowed_qty_for_wo = flt(self.qty) + (allowance_percentage / 100 * flt(self.qty))
		for d in self.get("operations"):
			precision = d.precision("completed_qty")
			qty = flt(flt(d.completed_qty, precision) + flt(d.process_loss_qty, precision), precision)
			if not qty:
				d.status = "Pending"
			elif qty < flt(self.qty, precision):
				d.status = "Work in Progress"
			else:
				d.status = "Completed"
				if qty > flt(max_allowed_qty_for_wo, precision):
					pass

	def _repair_validate_workflow(self):
		"""Skip transition permission checks; still stamp Submitted/Cancelled states."""
		if frappe.flags.in_install == "frappe":
			return
		workflow = self.meta.get_workflow()
		if workflow and self._action != "save":
			set_workflow_state_on_action(self, workflow, self._action)

	StockController.repost_future_sle_and_gle = _noop
	WorkOrder.update_operation_status = _soft_update_operation_status
	Document.validate_workflow = _repair_validate_workflow
	try:
		yield
	finally:
		StockController.repost_future_sle_and_gle = original_riv
		WorkOrder.update_operation_status = original_op_status
		Document.validate_workflow = original_wf
		frappe.flags.jc_manufacture_repair = False


def _combine_posting(posting_date, posting_time):
	from erpnext.stock.utils import get_combine_datetime

	return get_combine_datetime(posting_date, posting_time)


def sync_valuation_for_vouchers(
	voucher_names: Iterable[str],
	limit_pairs: int = 80,
	progress_cb=None,
) -> dict:
	"""Synchronously repost affected item×warehouse ledgers for vouchers.

	Uses Core ``update_entries_after`` with no RIV document (no mid-flight commit).
	Blocks when affected scope exceeds ``limit_pairs``.

	Roots are deduplicated to one earliest posting point per Item×Warehouse.
	Only non-cancelled SLEs are considered so cancel stubs do not widen scope.

	Manufacture/Repack dependant expansion already walks finished-good ledgers
	while reposting raw materials. Track those covered roots and skip a later
	queued pair when Core has already reposted the same Item×Warehouse from an
	earlier-or-equal posting datetime (same intent as Core's dependant skip).

	``progress_cb`` is optional and must not commit the business transaction.
	"""
	from erpnext.stock.stock_ledger import update_entries_after

	names = [n for n in voucher_names if n]
	if not names:
		return {
			"ok": True,
			"pairs": [],
			"note": "no vouchers",
			"pair_timings": [],
			"skipped_covered": [],
		}

	t_sql0 = time.perf_counter()
	pairs = frappe.db.sql(
		"""
		select item_code, warehouse,
		       min(posting_date) as posting_date,
		       min(posting_time) as posting_time,
		       count(*) as sle_count
		from `tabStock Ledger Entry`
		where voucher_no in %s
		  and ifnull(is_cancelled, 0) = 0
		  and item_code is not null and item_code != ''
		  and warehouse is not null and warehouse != ''
		group by item_code, warehouse
		order by posting_date, posting_time, item_code, warehouse
		""",
		(names,),
		as_dict=1,
	)
	sql_elapsed = round(time.perf_counter() - t_sql0, 4)
	if len(pairs) > limit_pairs:
		return {
			"ok": False,
			"error": f"Sync valuation scope too large ({len(pairs)} pairs > {limit_pairs})",
			"pairs": pairs,
			"sql_elapsed": sql_elapsed,
		}

	done = []
	pair_timings = []
	skipped_covered = []
	future_total = 0
	# (item_code, warehouse) -> earliest posting_datetime already fully reposted
	covered: dict[tuple[str, str], object] = {}
	allowed_pairs = {(p.item_code, p.warehouse) for p in pairs}
	t_all0 = time.perf_counter()
	roots_total = len(pairs)
	roots_done = 0
	if progress_cb:
		try:
			progress_cb(
				"T17",
				valuation_roots_done=0,
				valuation_roots_total=roots_total,
			)
		except Exception:
			pass
	with _scope_dependant_repost(allowed_pairs, already_covered=covered):
		for p in pairs:
			if getattr(frappe.flags, "jc_repair_fail_at", None) == "during_valuation":
				raise RuntimeError("INJECTED_FAILURE:during_valuation")
			key = (p.item_code, p.warehouse)
			pair_dt = _combine_posting(p.posting_date, p.posting_time)
			covered_from = covered.get(key)
			if covered_from is not None and covered_from <= pair_dt:
				skipped_covered.append(
					{
						"item_code": p.item_code,
						"warehouse": p.warehouse,
						"posting_date": str(p.posting_date),
						"posting_time": str(p.posting_time),
						"covered_from": str(covered_from),
					}
				)
				pair_timings.append(
					{
						"item_code": p.item_code,
						"warehouse": p.warehouse,
						"posting_date": str(p.posting_date),
						"posting_time": str(p.posting_time),
						"voucher_sle_count": int(p.sle_count or 0),
						"future_sle_count": 0,
						"elapsed": 0.0,
						"skipped": True,
						"reason": "dependant_already_covered",
					}
				)
				roots_done += 1
				if progress_cb:
					try:
						progress_cb(
							"T17",
							valuation_roots_done=roots_done,
							valuation_roots_total=roots_total,
						)
					except Exception:
						pass
				continue

			future_n = frappe.db.sql(
				"""
				select count(*) from `tabStock Ledger Entry`
				where item_code=%s and warehouse=%s and ifnull(is_cancelled,0)=0
				  and (posting_date, posting_time) >= (%s, %s)
				""",
				(p.item_code, p.warehouse, p.posting_date, p.posting_time),
			)[0][0]
			future_total += int(future_n or 0)
			t1 = time.perf_counter()
			# Job Card Manufacture repair already sets HISTORICAL_REPAIR_FLAG.
			# Core ledger walk can encounter pre-existing intermediate negatives on
			# unrelated Item×Batch rows in the same warehouse; allowing that during
			# the walk matches historical_stock repair practice and does not change
			# rates — it only prevents aborting the synchronous repost mid-chain.
			from erpnext_extensions.iran_accounting.historical_stock import HISTORICAL_REPAIR_FLAG

			allow_neg = bool(frappe.flags.get(HISTORICAL_REPAIR_FLAG))
			obj = update_entries_after(
				{
					"item_code": p.item_code,
					"warehouse": p.warehouse,
					"posting_date": p.posting_date,
					"posting_time": p.posting_time,
				},
				allow_negative_stock=allow_neg,
			)
			elapsed = round(time.perf_counter() - t1, 4)
			prev = covered.get(key)
			if prev is None or pair_dt < prev:
				covered[key] = pair_dt
			for dep_key, dep_dt in (getattr(obj, "reposted_dependant_item_wh", None) or {}).items():
				if not dep_key or dep_dt is None:
					continue
				ik = (dep_key[0], dep_key[1]) if isinstance(dep_key, (tuple, list)) else dep_key
				if ik not in allowed_pairs:
					continue
				prev_dep = covered.get(ik)
				if prev_dep is None or dep_dt < prev_dep:
					covered[ik] = dep_dt
			pair_timings.append(
				{
					"item_code": p.item_code,
					"warehouse": p.warehouse,
					"posting_date": str(p.posting_date),
					"posting_time": str(p.posting_time),
					"voucher_sle_count": int(p.sle_count or 0),
					"future_sle_count": int(future_n or 0),
					"elapsed": elapsed,
					"skipped": False,
					"dependants_marked": len(getattr(obj, "reposted_dependant_item_wh", None) or {}),
				}
			)
			done.append((p.item_code, p.warehouse, str(p.posting_date)))
			roots_done += 1
			if progress_cb:
				try:
					progress_cb(
						"T17",
						valuation_roots_done=roots_done,
						valuation_roots_total=roots_total,
					)
				except Exception:
					pass
	return {
		"ok": True,
		"pairs": done,
		"count": len(done),
		"skipped_count": len(skipped_covered),
		"skipped_covered": skipped_covered,
		"sql_elapsed": sql_elapsed,
		"elapsed": round(time.perf_counter() - t_all0, 4),
		"future_sle_total": future_total,
		"pair_timings": pair_timings,
		"dependant_scope_pairs": len(allowed_pairs),
	}


def _riv_names_for_voucher_scope(voucher_name: str) -> set[str]:
	"""Active RIV names covering a voucher — transaction-based or item-based.

	With ``item_based_reposting=1``, Core creates Item×Warehouse RIVs whose
	``voucher_no`` is null. Discover them via the voucher's live SLE pairs.
	"""
	names: set[str] = set()
	for row in frappe.db.sql(
		"""
		select name from `tabRepost Item Valuation`
		where voucher_no=%s and docstatus=1
		  and status in ('Queued', 'In Progress', 'Completed')
		""",
		voucher_name,
		as_dict=1,
	):
		names.add(row.name)
	pairs = frappe.db.sql(
		"""
		select distinct item_code, warehouse, posting_date, posting_time
		from `tabStock Ledger Entry`
		where voucher_type='Stock Entry' and voucher_no=%s and is_cancelled=0
		""",
		voucher_name,
		as_dict=1,
	)
	for p in pairs:
		for row in frappe.db.sql(
			"""
			select name from `tabRepost Item Valuation`
			where docstatus=1
			  and status in ('Queued', 'In Progress', 'Completed')
			  and item_code=%s and warehouse=%s
			  and posting_date <= %s
			order by creation desc
			limit 5
			""",
			(p.item_code, p.warehouse, p.posting_date),
			as_dict=1,
		):
			names.add(row.name)
	return names


def enqueue_native_riv_for_vouchers(voucher_names: Iterable[str]) -> dict:
	"""After structural Apply Commit: create native RIV for submitted vouchers.

	Must run OUTSIDE the repair transaction (RIV submit is durable).
	Uses Core ``StockController.repost_future_sle_and_gle`` which respects
	site ``item_based_reposting`` and creates Item×Warehouse RIV docs.

	Idempotent enough for Core: duplicate overlapping Item×Warehouse RIVs are
	deduplicated by Core ``deduplicate_similar_repost`` where applicable.
	Does not re-run structural repair.

	Commits after each voucher so RIV rows survive process exit (RIV creation
	must not remain only in the post-Apply session buffer).
	"""
	from erpnext.controllers.stock_controller import future_sle_exists
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		ensure_iran_riv_recalculate_wrapper_active,
		is_iran_riv_recalculate_wrapper_active,
	)

	# Fail closed: do not create RIV that workers would execute on Core-only economics.
	try:
		ensure_iran_riv_recalculate_wrapper_active(bootstrap=True)
	except Exception as boot_exc:
		frappe.log_error("jc_repair_native_riv_bootstrap")
		return {
			"ok": False,
			"riv_names": [],
			"count": 0,
			"skipped": [],
			"errors": [f"IRAN_RIV_WRAPPER_INACTIVE: {boot_exc}"],
			"wrapper_active": False,
		}

	created: list[str] = []
	skipped: list[dict] = []
	errors: list[str] = []
	for name in [n for n in voucher_names if n]:
		if not frappe.db.exists("Stock Entry", name):
			skipped.append({"voucher": name, "reason": "missing"})
			continue
		doc = frappe.get_doc("Stock Entry", name)
		if cint(doc.docstatus) != 1:
			skipped.append({"voucher": name, "reason": f"docstatus={doc.docstatus}"})
			continue
		args = frappe._dict(
			{
				"posting_date": doc.posting_date,
				"posting_time": doc.posting_time,
				"voucher_type": "Stock Entry",
				"voucher_no": doc.name,
				"company": doc.company,
			}
		)
		try:
			# Force=True: cancel/recreate often needs repost even when future_sle
			# probe is ambiguous after repair chronology.
			before = _riv_names_for_voucher_scope(name)
			doc.flags.ignore_permissions = True
			doc.repost_future_sle_and_gle(force=True)
			# Durable evidence: item-based RIV has null voucher_no.
			frappe.db.commit()
			after = _riv_names_for_voucher_scope(name)
			new_names = sorted(after - before)
			created.extend(new_names if new_names else sorted(after))
			if not before and not after and not future_sle_exists(args):
				skipped.append({"voucher": name, "reason": "no_future_sle"})
		except Exception as exc:
			frappe.db.rollback()
			errors.append(f"{name}: {exc}")
			frappe.log_error(f"jc_repair_native_riv_enqueue:{name}")
	# Unique preserve order
	seen = set()
	unique = []
	for n in created:
		if n not in seen:
			seen.add(n)
			unique.append(n)
	return {
		"ok": not errors,
		"riv_names": unique,
		"count": len(unique),
		"skipped": skipped,
		"errors": errors,
		"wrapper_active": is_iran_riv_recalculate_wrapper_active(),
	}
