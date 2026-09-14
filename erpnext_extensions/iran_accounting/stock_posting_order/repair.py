# Copyright (c) 2026, ERPNext Extensions contributors
"""Detect, preview, and apply production posting-order repairs."""

from __future__ import annotations

from datetime import datetime

import frappe
from frappe.utils import cint, flt, get_datetime, now_datetime

from erpnext_extensions.iran_accounting.stock_posting_order import (
	STATUS_BLOCKED,
	STATUS_DRAFT,
	STATUS_DRY_RUN,
	STATUS_REPAIRED,
	STATUS_STALE,
	STATUS_VALUATION_POISON,
	STATUS_INTEGRITY_COMPLETE,
	STATUS_ORDER_FIXED_RATE_REBUILD_REQUIRED,
	STATUS_DOWNSTREAM_REPLAY_REQUIRED,
	STATUS_DOWNSTREAM_COMPLETE,
)
from erpnext_extensions.iran_accounting.stock_posting_order.optimizer import simulate_running
from erpnext_extensions.iran_accounting.stock_posting_order.ordering import (
	format_date,
	format_time,
)
from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
	replay_item_warehouse,
	sync_transfer_incoming_rates,
)
from erpnext_extensions.iran_accounting.stock_posting_order.scanner import (
	run_full_history_scan,
	scan_same_time_groups,
)
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D

SCAN_LIMIT = 2000


def _row_rank(row: dict) -> tuple:
	elig = 0 if row.get("eligible") else 1
	cross = 0 if row.get("detection") == "CROSS_TIME" else 1
	try:
		amt = abs(float(row.get("negative_amount") or row.get("min_qty_before") or 0))
	except (TypeError, ValueError):
		amt = 0
	return (elig, cross, -amt, str(row.get("current_outbound_time") or ""))


def _gl_balanced(voucher_no: str) -> dict:
	rows = frappe.db.sql(
		"""
		SELECT SUM(debit) debit, SUM(credit) credit
		FROM `tabGL Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND IFNULL(is_cancelled,0)=0
		""",
		voucher_no,
		as_dict=True,
	)
	debit = flt(rows[0].debit) if rows else 0
	credit = flt(rows[0].credit) if rows else 0
	return {"debit": debit, "credit": credit, "balanced": abs(debit - credit) < 0.5}


def scan_production_posting_order_anomalies(
	*,
	company: str | None = None,
	from_date: str | None = None,
	to_date: str | None = None,
	include_likely: bool = True,
	include_no_repair: bool = False,
) -> list[dict]:
	"""Find same-time and cross-time posting-order anomalies (SABB-aware, true opening)."""
	result = scan_same_time_groups(
		company=company,
		from_date=from_date,
		to_date=to_date,
		include_likely=include_likely,
		include_no_repair=include_no_repair,
	)
	rows = list(result.get("rows") or [])
	from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import scan_stale_valuation_chains

	stale = scan_stale_valuation_chains(company=company, from_date=from_date, to_date=to_date)
	have = {(r.get("outbound_document"), r.get("item")) for r in rows}
	for extra in stale:
		key = (extra.get("outbound_document"), extra.get("item"))
		if key not in have:
			rows.append(extra)
			have.add(key)
	rows.sort(key=_row_rank)
	trimmed = rows[:SCAN_LIMIT] if len(rows) > SCAN_LIMIT else rows
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan_many

	return attach_plan_many(trimmed)


def dry_run(candidates: list[dict] | None = None, **scan_kwargs) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan_many

	if candidates is not None:
		rows = attach_plan_many(list(candidates))
		return {
			"dry_run": True,
			"status": STATUS_DRY_RUN,
			"count": len(rows),
			"eligible": [r for r in rows if r.get("eligible")],
			"rows": rows,
		}
	scan_kwargs.setdefault("include_no_repair", False)
	result = run_full_history_scan(**scan_kwargs)
	rows = attach_plan_many(list(result.get("rows") or []))
	rows.sort(key=_row_rank)
	return {
		"dry_run": True,
		"status": STATUS_DRY_RUN,
		"count": len(rows),
		"eligible": [r for r in rows if r.get("eligible")],
		"rows": rows,
		"summary": result.get("summary") or {},
		"timing": result.get("timing") or {},
		"sle_scanned": result.get("sle_scanned"),
	}


def _assert_preview_fresh(row: dict) -> None:
	in_mod = str(frappe.db.get_value("Stock Entry", row["inbound_document"], "modified"))
	out_mod = str(frappe.db.get_value("Stock Entry", row["outbound_document"], "modified"))
	if in_mod != str(row.get("inbound_modified")) or out_mod != str(row.get("outbound_modified")):
		frappe.throw(
			"Document changed since preview; aborting posting-order repair.",
			title="Stale preview",
		)
	in_ds = cint(frappe.db.get_value("Stock Entry", row["inbound_document"], "docstatus"))
	out_ds = cint(frappe.db.get_value("Stock Entry", row["outbound_document"], "docstatus"))
	if in_ds != 1 or out_ds != 1:
		status = "CANCELLED" if 2 in (in_ds, out_ds) else STATUS_DRAFT
		frappe.throw(f"Repair blocked ({status}): documents must be submitted.", title=status)


def _row_moves(row: dict) -> list[dict]:
	moves = list(row.get("moves") or [])
	if moves:
		return moves
	old = row.get("current_outbound_time")
	new = row.get("proposed_outbound_time")
	if new and old and str(new) != str(old):
		return [
			{
				"document": row["outbound_document"],
				"old": old,
				"new": new,
				"seconds": row.get("minimum_seconds_required") or 1,
			}
		]
	return []


def _update_posting_datetime(se_name: str, new_dt: datetime) -> None:
	frappe.db.set_value(
		"Stock Entry",
		se_name,
		{
			"posting_date": format_date(new_dt),
			"posting_time": format_time(new_dt),
			"set_posting_time": 1,
		},
		update_modified=True,
	)
	frappe.db.sql(
		"""
		UPDATE `tabStock Ledger Entry`
		SET posting_date=%s, posting_time=%s, posting_datetime=%s
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND is_cancelled=0
		""",
		(format_date(new_dt), format_time(new_dt), new_dt, se_name),
	)
	if frappe.db.exists("DocType", "Serial and Batch Bundle"):
		frappe.db.sql(
			"""
			UPDATE `tabSerial and Batch Bundle`
			SET posting_datetime=%s
			WHERE voucher_type='Stock Entry' AND voucher_no=%s
			""",
			(new_dt, se_name),
		)


def _voucher_item_warehouses(se_name: str) -> list[tuple[str, str]]:
	return frappe.db.sql(
		"""
		SELECT DISTINCT item_code, warehouse
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND is_cancelled=0
		""",
		se_name,
	)


def _ordered_identities(identities: set, row: dict) -> list[tuple[str, str]]:
	"""Replay the inverted warehouse first so transfer-out SVD exists before dest IN."""
	primary = (row.get("item"), row.get("warehouse"))
	seen = set()
	out = []
	if primary[0] and primary[1]:
		out.append(primary)
		seen.add(primary)
	for iw in sorted(identities, key=lambda x: (str(x[0]), str(x[1]))):
		if not iw[0] or not iw[1] or iw in seen:
			continue
		out.append(iw)
		seen.add(iw)
	return out


def _assert_transfer_value_neutral(voucher_no: str, item_code: str | None) -> None:
	if not voucher_no or not item_code:
		return
	rows = frappe.db.sql(
		"""
		SELECT warehouse, actual_qty, stock_value_difference
		FROM `tabStock Ledger Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s AND item_code=%s
		  AND is_cancelled=0
		""",
		(voucher_no, item_code),
		as_dict=True,
	)
	outs = [r for r in rows if D(r.actual_qty) < 0]
	ins = [r for r in rows if D(r.actual_qty) > 0]
	if not outs or not ins:
		return
	if abs(abs(D(outs[0].stock_value_difference)) - abs(D(ins[0].stock_value_difference))) > 0.5:
		raise frappe.ValidationError(
			f"Replay failed: {voucher_no} transfer SVD not value-neutral "
			f"(out {outs[0].stock_value_difference} vs in {ins[0].stock_value_difference})"
		)


def _maybe_riv(row: dict, vouchers: list[str]) -> str | None:
	if row.get("valuation_impact") != "VALUATION-IMPACTING" and row.get("gl_impact") != "RIV_REQUIRED":
		return None
	if not frappe.db.exists("DocType", "Repost Item Valuation"):
		return None
	from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import repost

	target = vouchers[-1] if vouchers else row.get("outbound_document")
	riv = frappe.new_doc("Repost Item Valuation")
	riv.company = row.get("company") or frappe.db.get_value("Stock Entry", target, "company")
	riv.based_on = "Transaction"
	riv.voucher_type = "Stock Entry"
	riv.voucher_no = target
	riv.allow_negative_stock = 1
	riv.flags.ignore_permissions = True
	riv.insert(ignore_permissions=True)
	repost(riv)
	return riv.name


def apply_repairs(rows: list[dict], *, dry_run: bool = True, user: str | None = None) -> dict:
	if dry_run:
		return dry_run_selected(rows)
	applied = []
	blocked = []
	run_id = f"PPO-{now_datetime().strftime('%Y%m%d%H%M%S')}-{frappe.generate_hash(length=6)}"
	log = _new_audit_log(run_id, user or frappe.session.user)
	for row in rows:
		try:
			_assert_preview_fresh(row)
			from erpnext_extensions.iran_accounting.historical_stock.planner import assert_ready

			assert_ready(row)
			moves = _row_moves(row)
			from_dt = get_datetime(row["current_inbound_time"])
			out_dt = get_datetime(row["current_outbound_time"])
			from_dt = min(from_dt, out_dt)
			identities = set()
			for doc_move in moves:
				identities |= set(_voucher_item_warehouses(doc_move["document"]))
			if row.get("item") and row.get("warehouse"):
				identities.add((row["item"], row["warehouse"]))

			sles = _identity_window(
				row["item"],
				row["warehouse"],
				row.get("batch"),
				from_dt,
			)
			opening = D(row.get("opening_qty") or 0)
			if sles:
				times = {}
				for m in moves:
					times[m["document"]] = get_datetime(m["new"])
				times.setdefault(row["inbound_document"], get_datetime(row["proposed_inbound_time"] or row["current_inbound_time"]))
				times.setdefault(row["outbound_document"], get_datetime(row["proposed_outbound_time"]))
				current = simulate_running(sles, opening)
				proposed = simulate_running(sles, opening, times)
			else:
				current = proposed = {"min_qty": row.get("min_qty_before"), "final_qty": row.get("final_qty_before")}

			gl_before = {
				vn: _gl_balanced(vn)
				for vn in {row["inbound_document"], row["outbound_document"], *[m["document"] for m in moves]}
			}

			savepoint = f"ppo_{frappe.generate_hash(length=8)}"
			frappe.db.savepoint(savepoint)
			try:
				for m in moves:
					_update_posting_datetime(m["document"], get_datetime(m["new"]))

				replay_results = []
				valuation_changed = False
				touched = set()
				ordered = _ordered_identities(identities, row)
				moved_vouchers = [m["document"] for m in moves]
				sync_targets = set(moved_vouchers) | {
					row.get("inbound_document"),
					row.get("outbound_document"),
				}
				write_vouchers = {vn for vn in sync_targets if vn}
				allow_unrelated = str(row.get("planner_status") or "") == "READY_BATCH_SCOPED_REPAIR"
				# Source warehouse first, then copy transfer-in incoming_rate,
				# then destination warehouse. A second pass picks up dest SVD.
				# Only the inverted pair is written; later lots keep their SLE.
				for _pass in (1, 2):
					pass_results = []
					for item_code, warehouse in ordered:
						rep = replay_item_warehouse(
							item_code,
							warehouse,
							from_dt,
							ignore_inversion_artifacts=True,
							write_vouchers=write_vouchers,
							allow_unrelated_poison=allow_unrelated,
						)
						if not rep.get("ok"):
							raise frappe.ValidationError(
								f"{rep.get('status')}: {item_code} {warehouse} ({rep.get('reason')})"
							)
						pass_results.append(rep)
						if rep.get("valuation_changed"):
							valuation_changed = True
						touched |= set(rep.get("touched_vouchers") or [])
						for vn in list(sync_targets | touched):
							if vn:
								sync_transfer_incoming_rates(vn)
					replay_results = pass_results

				# Timestamp change is not the repair. qty_after on the inverted
				# voucher must be non-negative after replay in ERPNext order.
				_assert_ledger_repaired(row, from_dt)
				for vn in moved_vouchers:
					_assert_transfer_value_neutral(vn, row.get("item"))

				gl_touched = []
				if valuation_changed:
					for vn in sorted(touched | {row["outbound_document"]}):
						gl_touched.append(_rebuild_gl_no_commit(vn))

				from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
					rebuild_chain_valuation,
				)

				valuation = rebuild_chain_valuation(
					row.get("inbound_document"),
					row.get("outbound_document"),
					item=row.get("item"),
					warehouse=row.get("warehouse"),
					batch=row.get("batch"),
					dry_run=False,
					allow_riv=False,
				)

				gl_after = {
					vn: _gl_balanced(vn)
					for vn in gl_before
				}
				if not valuation_changed:
					for vn, after in gl_after.items():
						if not after["balanced"]:
							raise frappe.ValidationError(f"GL unbalanced after quantity-only repair: {vn}")
			except Exception:
				frappe.db.rollback(save_point=savepoint)
				raise

			riv_name = None
			riv_revalidated = _revalidate_failed_riv(row.get("item"), row.get("warehouse"))

			primary = moves[0]
			qty_ok = True
			rate_status = (valuation or {}).get("status") or STATUS_ORDER_FIXED_RATE_REBUILD_REQUIRED
			if rate_status in (
				STATUS_INTEGRITY_COMPLETE,
				STATUS_DOWNSTREAM_REPLAY_REQUIRED,
				STATUS_DOWNSTREAM_COMPLETE,
			):
				final_status = rate_status
			else:
				final_status = STATUS_ORDER_FIXED_RATE_REBUILD_REQUIRED
			entry = {
				**row,
				"status": final_status,
				"old_outbound_time": primary.get("old") or row.get("current_outbound_time"),
				"new_outbound_time": primary.get("new") or row.get("proposed_outbound_time"),
				"min_qty_before": str(current.get("min_qty")),
				"min_qty_after": str(proposed.get("min_qty")),
				"riv": riv_name,
				"repair_run_id": run_id,
				"replay": replay_results,
				"valuation_changed": valuation_changed,
				"valuation": valuation,
				"valuation_impact": (valuation or {}).get("valuation_impact"),
				"replay_depth": (valuation or {}).get("replay_depth"),
				"dependent_count": (valuation or {}).get("dependent_count"),
				"affected_vouchers": (valuation or {}).get("affected_vouchers"),
				"estimated_runtime": (valuation or {}).get("estimated_runtime"),
				"replay_scope": (valuation or {}).get("replay_scope"),
				"downstream": (valuation or {}).get("downstream"),
				"gl_before": gl_before,
				"gl_after": gl_after,
				"gl_rebuilt": gl_touched or (valuation or {}).get("gl_rebuilt"),
				"failed_riv_revalidated": riv_revalidated,
				"seconds_shifted": max((m.get("seconds") or 0) for m in moves),
			}
			_append_audit(log, entry)
			applied.append(entry)
		except Exception as exc:
			msg = str(exc)
			status = STATUS_STALE if "preview" in msg.lower() else STATUS_BLOCKED
			if STATUS_VALUATION_POISON in msg:
				status = STATUS_VALUATION_POISON
			blocked.append({"row": row, "error": msg, "status": status})
			_append_audit(
				log,
				{
					**row,
					"status": status,
					"dependency_reason": msg,
					"old_outbound_time": row.get("current_outbound_time"),
					"new_outbound_time": row.get("proposed_outbound_time"),
				},
			)
	if log:
		log.save(ignore_permissions=True)
	sql_executed = 0
	for entry in applied:
		sql_executed += len(entry.get("replay") or []) + 1
	skip_reason = None
	if not applied:
		skip_reason = (blocked[0].get("error") if blocked else None) or "No SQL updates. Nothing was written."
	return {
		"dry_run": False,
		"repair_run_id": run_id,
		"applied": applied,
		"blocked": blocked,
		"aborted": bool(blocked) and not applied,
		"reason": skip_reason,
		"skip_reason": skip_reason,
		"sql_updates_planned": None,
		"sql_updates_executed": 0 if not applied else sql_executed,
		"savepoint_created": bool(applied),
		"transaction_committed": bool(applied),
	}


def _assert_ledger_repaired(row: dict, from_dt) -> None:
	"""Fail closed if timestamps moved but qty_after still inverted."""
	item = row.get("item")
	warehouse = row.get("warehouse")
	if not item or not warehouse:
		return
	sles = _identity_window(item, warehouse, row.get("batch"), from_dt)
	if not sles:
		raise frappe.ValidationError("Replay produced an empty SLE window")
	out_name = row.get("outbound_document")
	in_name = row.get("inbound_document")
	out_dt = in_dt = None
	for s in sles:
		if s.voucher_no == out_name and D(s.actual_qty) < 0:
			out_dt = get_datetime(s.posting_datetime)
			if D(s.qty_after_transaction) < -0.0001:
				raise frappe.ValidationError(
					f"Replay failed: {s.name} qty_after_transaction still {s.qty_after_transaction}"
				)
		if s.voucher_no == in_name and D(s.actual_qty) > 0:
			in_dt = get_datetime(s.posting_datetime)
	if out_dt and in_dt and out_dt < in_dt:
		raise frappe.ValidationError(
			f"Replay failed: outbound {out_name} still before inbound {in_name}"
		)
	run = D(row.get("opening_qty") or 0)
	for s in sles:
		run += D(s.actual_qty)
		if run < -0.0001:
			raise frappe.ValidationError(
				f"Replay failed: batch running qty still {run} at {s.voucher_no}"
			)


def _rebuild_gl_no_commit(voucher_no: str) -> dict:
	if not voucher_no or not frappe.db.exists("Stock Entry", voucher_no):
		return {"voucher": voucher_no, "rebuilt": False, "reason": "missing"}
	se = frappe.get_doc("Stock Entry", voucher_no)
	if cint(se.docstatus) != 1:
		return {"voucher": voucher_no, "rebuilt": False, "reason": "not_submitted"}
	from erpnext.accounts.general_ledger import toggle_debit_credit_if_negative
	from erpnext.accounts.utils import _delete_accounting_ledger_entries

	inventory_account_map = se.get_inventory_account_map()
	expected = toggle_debit_credit_if_negative(se.get_gl_entries(inventory_account_map))
	_delete_accounting_ledger_entries("Stock Entry", voucher_no)
	if expected:
		se.make_gl_entries(gl_entries=expected, from_repost=True)
	after = _gl_balanced(voucher_no)
	if not after["balanced"]:
		raise frappe.ValidationError(f"GL rebuild failed: {voucher_no} unbalanced")
	return {"voucher": voucher_no, "rebuilt": True, "gl": after}


def _revalidate_failed_riv(item_code, warehouse) -> list:
	if not item_code or not warehouse or not frappe.db.exists("DocType", "Repost Item Valuation"):
		return []
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import classify_failed_riv

	docs = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, voucher_no, voucher_type, posting_date,
		       status, error_log, based_on, modified, company
		FROM `tabRepost Item Valuation`
		WHERE status='Failed' AND docstatus=1
		  AND item_code=%s AND warehouse=%s
		ORDER BY modified DESC
		LIMIT 20
		""",
		(item_code, warehouse),
		as_dict=True,
	)
	return [
		{"name": d.name, "riv_status": classify_failed_riv(d).get("riv_status"), "error": (d.error_log or "")[:180]}
		for d in docs
	]


def _identity_window(item_code, warehouse, batch_no, posting_datetime) -> list:
	conds = [
		"item_code=%s",
		"warehouse=%s",
		"is_cancelled=0",
		"posting_datetime>=%s",
	]
	args = [item_code, warehouse, posting_datetime]
	rows = frappe.db.sql(
		f"""
		SELECT name, voucher_no, actual_qty, qty_after_transaction, incoming_rate,
		       valuation_rate, stock_value, stock_value_difference, posting_datetime,
		       creation, batch_no, serial_and_batch_bundle, warehouse, item_code
		FROM `tabStock Ledger Entry`
		WHERE {" AND ".join(conds)}
		ORDER BY posting_datetime, creation
		""",
		args,
		as_dict=True,
	)
	if not batch_no:
		return rows
	from erpnext_extensions.iran_accounting.stock_posting_order.batch_identity import canonical_batch_no

	wanted = str(batch_no)
	out = []
	for r in rows:
		entries = []
		if r.serial_and_batch_bundle:
			entries = frappe.db.sql(
				"SELECT batch_no FROM `tabSerial and Batch Entry` WHERE parent=%s",
				r.serial_and_batch_bundle,
				as_dict=True,
			)
		canon = canonical_batch_no(r, entries) or ""
		if canon == wanted or (not canon and not wanted):
			out.append(r)
	return out


def dry_run_selected(rows: list[dict]) -> dict:
	return {"dry_run": True, "status": STATUS_DRY_RUN, "rows": rows, "count": len(rows)}


def _new_audit_log(run_id: str, user: str):
	if not frappe.db.exists("DocType", "Production Posting Order Repair Log"):
		return None
	doc = frappe.new_doc("Production Posting Order Repair Log")
	doc.repair_run_id = run_id
	doc.repaired_by = user
	doc.repaired_on = now_datetime()
	return doc


def _append_audit(log, entry: dict) -> None:
	if not log:
		return
	payload = {
		"inbound_document": entry.get("inbound_document"),
		"outbound_document": entry.get("outbound_document"),
		"item": entry.get("item"),
		"warehouse": entry.get("warehouse"),
		"batch_no": entry.get("batch"),
		"sabb": entry.get("sabb_outbound") or entry.get("sabb_inbound"),
		"old_datetime": entry.get("old_outbound_time") or entry.get("current_outbound_time"),
		"new_datetime": entry.get("new_outbound_time") or entry.get("proposed_outbound_time"),
		"dependency_reason": entry.get("dependency_reason"),
		"min_qty_before": flt(entry.get("min_qty_before")),
		"min_qty_after": flt(entry.get("min_qty_after")),
		"valuation_impact": entry.get("valuation_impact"),
		"gl_impact": entry.get("gl_impact"),
		"confidence": entry.get("confidence"),
		"status": entry.get("status"),
	}
	meta = getattr(log, "meta", None)
	if meta and meta.get_field("entries"):
		child = frappe.get_meta("Production Posting Order Repair Entry")
		if child and child.get_field("seconds_shifted"):
			payload["seconds_shifted"] = flt(entry.get("seconds_shifted"))
	log.append("entries", payload)
