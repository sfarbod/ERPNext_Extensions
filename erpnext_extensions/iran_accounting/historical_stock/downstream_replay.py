# Copyright (c) 2026, ERPNext Extensions contributors
"""Identity-scoped downstream valuation replay (item + warehouse hop + batch/SABB).

Walks forward from a repaired patient-zero chain. Writes only the repaired
item+batch. Manufacture FG residual / additional cost / scrap / by-product
rows are not overwritten. Unrelated lots are classified and skipped.
RIV is never invoked.
"""

from __future__ import annotations

import time
from decimal import Decimal

import frappe
from frappe.utils import flt, get_datetime

from erpnext_extensions.iran_accounting.historical_stock import VALUE_EPS
from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
	sync_sabb_from_sle,
	write_sle_transaction_rates,
)
from erpnext_extensions.iran_accounting.stock_posting_order import (
	STATUS_BLOCKED,
	STATUS_DOWNSTREAM_COMPLETE,
	STATUS_DOWNSTREAM_PENDING,
	STATUS_DOWNSTREAM_REPLAY_REQUIRED,
	STATUS_DOWNSTREAM_REPLAYING,
	STATUS_DOWNSTREAM_SKIPPED,
	STATUS_GL_REBUILD_REQUIRED,
	STATUS_INTEGRITY_COMPLETE,
)
from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
	VALUE_EPS as REPLAY_VALUE_EPS,
	_bin_from_last_sle,
	sync_transfer_incoming_rates,
)
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D

QTY_EPS = Decimal("0.0001")

CLASS_DIRECT = "DIRECT_DEPENDENT"
CLASS_INDIRECT = "INDIRECT_DEPENDENT"
CLASS_UNRELATED = "UNRELATED"
CLASS_STOP = "STOP_CHAIN"

SUPPORTED_PURPOSES = (
	"Material Transfer",
	"Material Transfer for Manufacture",
	"Manufacture",
	"Repack",
	"Material Issue",
	"Material Receipt",
	"Send to Subcontractor",
)

TRANSFER_PURPOSES = (
	"Material Transfer",
	"Material Transfer for Manufacture",
	"Send to Subcontractor",
)

MS_PER_VOUCHER = 50


def classify_dependency(movement: dict, ctx: dict) -> dict:
	"""Pure classifier. No database."""
	patient = set(ctx.get("patient_vouchers") or [])
	item = ctx.get("item")
	batch = ctx.get("batch")
	visited = set(ctx.get("visited_warehouses") or [])
	wo = ctx.get("work_order")
	job = ctx.get("job_card")
	vn = movement.get("voucher_no") or movement.get("voucher")
	reasons: list[str] = []
	mv_item = movement.get("item_code") or movement.get("item")
	mv_batch = movement.get("batch") or movement.get("batch_no")
	purpose = movement.get("purpose") or ""
	vtype = movement.get("voucher_type") or "Stock Entry"
	qty = D(movement.get("actual_qty"))
	wh = movement.get("warehouse")

	if vn in patient:
		return _cls(CLASS_UNRELATED, ["patient voucher"], STATUS_DOWNSTREAM_SKIPPED, True)
	if item and mv_item and mv_item != item:
		return _cls(CLASS_UNRELATED, ["cross-item"], STATUS_DOWNSTREAM_SKIPPED, True)
	if batch and mv_batch and mv_batch != batch:
		return _cls(CLASS_UNRELATED, ["different batch"], STATUS_DOWNSTREAM_SKIPPED, True)

	if mv_batch and mv_batch == batch:
		reasons.append("same batch")
	if movement.get("serial_and_batch_bundle"):
		reasons.append("same SABB")
	if wo and movement.get("work_order") == wo:
		reasons.append("same WO")
	if job and movement.get("job_card") == job:
		reasons.append("same Job Card")
	if wh and wh in visited:
		reasons.append("same transfer destination")
	if purpose == "Manufacture" and qty < 0:
		reasons.append("same manufacture consume")
	if purpose == "Repack":
		reasons.append("same repack")
	remaining = D(ctx.get("remaining_qty") or 0)
	if qty < 0 and remaining > 0 and abs(qty) + QTY_EPS < remaining:
		reasons.append("partial consume")
	if purpose in TRANSFER_PURPOSES:
		reasons.append("warehouse move")
		if qty > 0 and movement.get("batch_split"):
			reasons.append("batch split")

	if vtype != "Stock Entry":
		return _cls(CLASS_STOP, reasons + ["unknown voucher type"], STATUS_DOWNSTREAM_SKIPPED, True)
	if purpose and purpose not in SUPPORTED_PURPOSES:
		return _cls(CLASS_STOP, reasons + ["unsupported purpose"], STATUS_DOWNSTREAM_SKIPPED, True)
	if not reasons:
		return _cls(CLASS_UNRELATED, ["no shared identity"], STATUS_DOWNSTREAM_SKIPPED, True)

	klass = CLASS_INDIRECT if (qty > 0 and wh and wh not in visited) else CLASS_DIRECT
	return _cls(klass, reasons, STATUS_DOWNSTREAM_REPLAY_REQUIRED, False)


def expected_movements(opening_qty, opening_value, movements: list[dict]) -> list[dict]:
	"""Pure batch running-value replay. Last consume takes remaining value."""
	running_qty = D(opening_qty)
	running_value = D(opening_value)
	out = []
	n = len(movements)
	for i, mov in enumerate(movements):
		qty = D(mov.get("actual_qty"))
		current_svd = D(mov.get("stock_value_difference") or mov.get("current_svd") or 0)
		if running_qty != 0 and abs(running_qty + qty) <= QTY_EPS:
			expected_svd = -running_value
		elif running_qty != 0:
			expected_svd = qty * (running_value / running_qty)
		else:
			expected_svd = D(0)
		expected_rate = abs(expected_svd / qty) if qty != 0 else D(0)
		current_rate = abs(current_svd / qty) if qty != 0 else D(0)
		delta = expected_svd - current_svd
		replay_required = abs(delta) > REPLAY_VALUE_EPS
		running_qty += qty
		running_value += expected_svd
		if abs(running_qty) <= QTY_EPS:
			running_qty = D(0)
			running_value = D(0)
		row = {
			**mov,
			"expected_svd": flt(expected_svd),
			"expected_rate": flt(expected_rate),
			"current_valuation": flt(current_svd),
			"expected_valuation": flt(expected_svd),
			"difference": flt(delta),
			"replay_required": "YES" if replay_required else "NO",
			"replay_required_bool": replay_required,
			"estimated_impact": flt(delta),
			"remaining_qty_after": flt(running_qty),
			"remaining_value_after": flt(running_value),
			"is_last": i == n - 1,
		}
		out.append(row)
	return out


def discover_downstream_chain(
	item,
	batch,
	*,
	from_dt,
	patient_vouchers=None,
	warehouse=None,
	work_order=None,
	job_card=None,
) -> dict:
	t0 = time.perf_counter()
	patient = set(patient_vouchers or [])
	visited = set()
	if warehouse:
		visited.add(warehouse)
	source = _source_in_state(item, batch, from_dt, patient)
	if source:
		visited.add(source["warehouse"])
	rows = _fetch_batch_sles(item, batch, from_dt)
	classified = []
	remaining = D(source["qty"]) if source else D(0)
	ctx_wo = work_order or (source or {}).get("work_order")
	ctx_job = job_card or (source or {}).get("job_card")
	depth = 0
	for sle in rows:
		if sle.voucher_no in patient:
			if D(sle.actual_qty) > 0:
				visited.add(sle.warehouse)
			continue
		ctx = {
			"item": item,
			"batch": batch,
			"patient_vouchers": patient,
			"visited_warehouses": visited,
			"work_order": ctx_wo,
			"job_card": ctx_job,
			"remaining_qty": remaining,
		}
		info = classify_dependency(
			{
				"voucher_no": sle.voucher_no,
				"voucher_type": "Stock Entry",
				"purpose": sle.purpose,
				"item_code": item,
				"warehouse": sle.warehouse,
				"batch": batch,
				"actual_qty": sle.actual_qty,
				"work_order": sle.work_order,
				"job_card": sle.job_card,
				"serial_and_batch_bundle": sle.serial_and_batch_bundle,
			},
			ctx,
		)
		hop = 1
		if info["classification"] == CLASS_INDIRECT:
			hop = 2
			depth = max(depth, 2)
		elif info["classification"] == CLASS_DIRECT:
			depth = max(depth, 1)
		classified.append(
			{
				"voucher_no": sle.voucher_no,
				"purpose": sle.purpose,
				"warehouse": sle.warehouse,
				"actual_qty": sle.actual_qty,
				"stock_value_difference": sle.stock_value_difference,
				"incoming_rate": sle.incoming_rate,
				"outgoing_rate": sle.outgoing_rate,
				"sle": sle.name,
				"serial_and_batch_bundle": sle.serial_and_batch_bundle,
				"work_order": sle.work_order,
				"job_card": sle.job_card,
				"posting_datetime": str(sle.posting_datetime),
				"hop": hop,
				**info,
			}
		)
		if info["classification"] in (CLASS_DIRECT, CLASS_INDIRECT) and D(sle.actual_qty) > 0:
			visited.add(sle.warehouse)
		if info["classification"] in (CLASS_DIRECT, CLASS_INDIRECT):
			remaining += D(sle.actual_qty)
	return {
		"item": item,
		"batch": batch,
		"from_dt": str(from_dt),
		"patient_vouchers": sorted(patient),
		"source": source,
		"visited_warehouses": sorted(visited),
		"movements": classified,
		"replay_depth": depth,
		"discover_seconds": round(time.perf_counter() - t0, 4),
		"status": STATUS_DOWNSTREAM_PENDING,
	}


def plan_downstream_replay(
	item,
	batch,
	*,
	from_dt,
	patient_vouchers=None,
	warehouse=None,
	work_order=None,
	job_card=None,
) -> dict:
	t0 = time.perf_counter()
	disc = discover_downstream_chain(
		item,
		batch,
		from_dt=from_dt,
		patient_vouchers=patient_vouchers,
		warehouse=warehouse,
		work_order=work_order,
		job_card=job_card,
	)
	dependents = [
		m
		for m in disc["movements"]
		if m["classification"] in (CLASS_DIRECT, CLASS_INDIRECT) and not m.get("preview_only")
	]
	preview_only = [m for m in disc["movements"] if m.get("preview_only") and m["classification"] == CLASS_STOP]
	unrelated = [m for m in disc["movements"] if m["classification"] == CLASS_UNRELATED]
	src = disc.get("source") or {}
	priced = expected_movements(src.get("qty") or 0, src.get("value") or 0, dependents)
	required = [p for p in priced if p["replay_required_bool"]]
	gl_rebuild_count = sum(1 for p in required if p.get("purpose") in TRANSFER_PURPOSES)
	status = STATUS_DOWNSTREAM_COMPLETE if not required else STATUS_DOWNSTREAM_REPLAY_REQUIRED
	plan_seconds = round(time.perf_counter() - t0, 4)
	return {
		**disc,
		"status": status,
		"replay_scope": "item+batch/SABB (identity)",
		"dependent_count": len(dependents),
		"affected_vouchers": [p["voucher_no"] for p in priced],
		"replay_order": [p["voucher_no"] for p in required],
		"replay_required_count": len(required),
		"estimated_runtime_ms": len(required) * MS_PER_VOUCHER,
		"estimated_runtime": f"{len(required) * MS_PER_VOUCHER} ms",
		"gl_rebuild_count": gl_rebuild_count,
		"dependents": priced,
		"preview_only": preview_only,
		"unrelated_count": len(unrelated),
		"plan_seconds": plan_seconds,
		"patient_zero": (disc.get("patient_vouchers") or [None])[0],
		"graph": _graph(disc.get("patient_vouchers") or [], priced),
	}


def replay_downstream_chain(
	item,
	batch,
	*,
	from_dt,
	patient_vouchers=None,
	warehouse=None,
	work_order=None,
	job_card=None,
	dry_run=True,
) -> dict:
	"""Replay dependents. Savepoint per chain. Does not commit."""
	t0 = time.perf_counter()
	plan = plan_downstream_replay(
		item,
		batch,
		from_dt=from_dt,
		patient_vouchers=patient_vouchers,
		warehouse=warehouse,
		work_order=work_order,
		job_card=job_card,
	)
	plan["dry_run"] = dry_run
	plan["riv"] = "NOT_INVOKED"
	if dry_run:
		return plan
	if not plan["replay_order"]:
		plan["status"] = STATUS_DOWNSTREAM_COMPLETE
		plan["execute_seconds"] = round(time.perf_counter() - t0, 4)
		return plan

	savepoint = f"ds_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(savepoint)
	written = []
	gl = []
	fg_before = {}
	try:
		plan["status"] = STATUS_DOWNSTREAM_REPLAYING
		for step in plan["dependents"]:
			if not step["replay_required_bool"]:
				continue
			vn = step["voucher_no"]
			fg_before[vn] = _output_amounts(vn)
			_write_identity_sle(step)
			write_sle_transaction_rates(vn, item)
			sync_sabb_from_sle(vn, item)
			_align_consume_detail(vn, item, step["expected_rate"], step["expected_svd"])
			if step.get("purpose") in TRANSFER_PURPOSES:
				sync_transfer_incoming_rates(vn)
				sync_sabb_from_sle(vn, item)
				from erpnext_extensions.iran_accounting.stock_posting_order.repair import (
					_rebuild_gl_no_commit,
				)

				gl.append(_rebuild_gl_no_commit(vn))
			else:
				gl.append(
					{
						"voucher": vn,
						"rebuilt": False,
						"reason": "manufacture_fg_residual_preserved",
					}
				)
			fg_after = _output_amounts(vn)
			if fg_before[vn] != fg_after:
				raise frappe.ValidationError(f"Manufacture output residual changed on {vn}")
			written.append(vn)
		warehouses = {step["warehouse"] for step in plan["dependents"] if step["replay_required_bool"]}
		for wh in warehouses:
			_bin_from_last_sle(item, wh)
	except Exception:
		frappe.db.rollback(save_point=savepoint)
		raise

	after = plan_downstream_replay(
		item,
		batch,
		from_dt=from_dt,
		patient_vouchers=patient_vouchers,
		warehouse=warehouse,
		work_order=work_order,
		job_card=job_card,
	)
	sle_rows = max(1, len(written))
	elapsed = time.perf_counter() - t0
	status = STATUS_DOWNSTREAM_COMPLETE if not after["replay_required_count"] else STATUS_DOWNSTREAM_REPLAY_REQUIRED
	if any(g.get("rebuilt") and not (g.get("gl") or {}).get("balanced", True) for g in gl):
		status = STATUS_GL_REBUILD_REQUIRED
	return {
		**after,
		"dry_run": False,
		"status": status,
		"written": written,
		"gl_rebuilt": gl,
		"fg_preserved": fg_before,
		"rows_updated": len(written),
		"execute_seconds": round(elapsed, 4),
		"sle_per_sec": round(sle_rows / elapsed, 2) if elapsed else 0,
		"plan_seconds": plan.get("plan_seconds"),
		"riv": "NOT_INVOKED",
		"integrity": STATUS_INTEGRITY_COMPLETE if status == STATUS_DOWNSTREAM_COMPLETE else status,
	}


def replay_downstream_for_rows(rows: list[dict], *, dry_run=True) -> dict:
	applied = []
	blocked = []
	t0 = time.perf_counter()
	for row in rows or []:
		try:
			item = row.get("item") or row.get("item_code")
			batch = row.get("batch")
			if not item or not batch:
				raise frappe.ValidationError("item and batch are required")
			from_dt = row.get("from_dt")
			outbound = row.get("outbound_document")
			if not from_dt and outbound:
				dt = frappe.db.get_value(
					"Stock Entry", outbound, ["posting_date", "posting_time"], as_dict=True
				)
				if dt:
					from_dt = get_datetime(f"{dt.posting_date} {dt.posting_time}")
			if not from_dt:
				from_dt = row.get("proposed_outbound_time") or row.get("current_inbound_time")
			if from_dt:
				from_dt = get_datetime(from_dt)
			patient = [
				v
				for v in (row.get("inbound_document"), row.get("outbound_document"), row.get("patient_zero"))
				if v
			]
			applied.append(
				replay_downstream_chain(
					item,
					batch,
					from_dt=from_dt,
					patient_vouchers=patient,
					warehouse=row.get("warehouse"),
					work_order=row.get("work_order"),
					dry_run=dry_run,
				)
			)
		except Exception as exc:
			blocked.append({"row": row, "error": str(exc), "status": STATUS_BLOCKED})
	return {
		"dry_run": dry_run,
		"applied": applied,
		"blocked": blocked,
		"count": len(applied),
		"elapsed_seconds": round(time.perf_counter() - t0, 4),
	}


def _cls(classification, reasons, status, preview_only):
	return {
		"classification": classification,
		"reasons": reasons,
		"status": status,
		"preview_only": preview_only,
		"reason": "; ".join(reasons),
	}


def _graph(patient, dependents):
	nodes = list(patient)
	edges = []
	prev = patient[-1] if patient else None
	for d in dependents:
		nodes.append(d["voucher_no"])
		if prev:
			edges.append({"from": prev, "to": d["voucher_no"], "class": d.get("classification")})
		prev = d["voucher_no"]
	return {"nodes": nodes, "edges": edges}


def _source_in_state(item, batch, from_dt, patient) -> dict | None:
	rows = frappe.db.sql(
		"""
		SELECT sle.name, sle.warehouse, sle.actual_qty, sle.stock_value_difference,
		       sle.posting_datetime, sle.voucher_no, se.work_order, se.job_card
		FROM `tabStock Ledger Entry` sle
		JOIN `tabSerial and Batch Entry` sbe ON sbe.parent=sle.serial_and_batch_bundle
		JOIN `tabStock Entry` se ON se.name=sle.voucher_no
		WHERE sle.item_code=%s AND sle.is_cancelled=0 AND sbe.batch_no=%s
		  AND sle.actual_qty>0 AND sle.posting_datetime<=%s
		ORDER BY sle.posting_datetime DESC, sle.creation DESC
		LIMIT 5
		""",
		(item, batch, from_dt),
		as_dict=True,
	)
	for r in rows:
		if patient and r.voucher_no not in patient:
			continue
		return {
			"voucher_no": r.voucher_no,
			"warehouse": r.warehouse,
			"qty": r.actual_qty,
			"value": r.stock_value_difference,
			"work_order": r.work_order,
			"job_card": r.job_card,
		}
	if rows:
		r = rows[0]
		return {
			"voucher_no": r.voucher_no,
			"warehouse": r.warehouse,
			"qty": r.actual_qty,
			"value": r.stock_value_difference,
			"work_order": r.work_order,
			"job_card": r.job_card,
		}
	return None


def _fetch_batch_sles(item, batch, from_dt):
	return frappe.db.sql(
		"""
		SELECT sle.name, sle.voucher_no, sle.warehouse, sle.actual_qty, sle.incoming_rate,
		       sle.outgoing_rate, sle.stock_value_difference, sle.stock_value,
		       sle.qty_after_transaction, sle.posting_datetime, sle.serial_and_batch_bundle,
		       se.purpose, se.work_order, se.job_card
		FROM `tabStock Ledger Entry` sle
		JOIN `tabSerial and Batch Entry` sbe ON sbe.parent=sle.serial_and_batch_bundle
		JOIN `tabStock Entry` se ON se.name=sle.voucher_no
		WHERE sle.item_code=%s AND sle.is_cancelled=0 AND sbe.batch_no=%s
		  AND sle.posting_datetime>=%s
		ORDER BY sle.posting_datetime, sle.creation
		""",
		(item, batch, from_dt),
		as_dict=True,
	)


def _write_identity_sle(step: dict) -> None:
	sle_name = step.get("sle")
	if not sle_name:
		return
	old_svd = D(frappe.db.get_value("Stock Ledger Entry", sle_name, "stock_value_difference"))
	old_value = D(frappe.db.get_value("Stock Ledger Entry", sle_name, "stock_value"))
	qty = D(step.get("actual_qty"))
	new_svd = D(step["expected_svd"])
	rate = abs(new_svd / qty) if qty != 0 else D(0)
	payload = {
		"stock_value_difference": flt(new_svd),
		"stock_value": flt(old_value + (new_svd - old_svd)),
	}
	if qty < 0:
		payload["outgoing_rate"] = flt(rate)
		payload["incoming_rate"] = 0
	else:
		payload["incoming_rate"] = flt(rate)
		payload["outgoing_rate"] = 0
	frappe.db.set_value("Stock Ledger Entry", sle_name, payload, update_modified=False)


def _align_consume_detail(voucher_no, item_code, rate, svd) -> None:
	rows = frappe.db.sql(
		"""
		SELECT name, is_finished_item, is_scrap_item, secondary_item_type, s_warehouse, t_warehouse, qty
		FROM `tabStock Entry Detail`
		WHERE parent=%s AND item_code=%s
		""",
		(voucher_no, item_code),
		as_dict=True,
	)
	for d in rows:
		if _is_protected_output(d):
			continue
		qty = abs(flt(d.qty))
		amount = abs(flt(svd))
		frappe.db.set_value(
			"Stock Entry Detail",
			d.name,
			{
				"basic_rate": flt(rate),
				"valuation_rate": flt(rate),
				"amount": amount,
				"basic_amount": amount,
			},
			update_modified=False,
		)


def _is_protected_output(detail) -> bool:
	if flt(getattr(detail, "is_finished_item", 0)):
		return True
	if flt(getattr(detail, "is_scrap_item", 0)):
		return True
	if getattr(detail, "secondary_item_type", None):
		return True
	s_wh = getattr(detail, "s_warehouse", None)
	t_wh = getattr(detail, "t_warehouse", None)
	return bool(t_wh) and not s_wh


def _output_amounts(voucher_no) -> list[tuple]:
	rows = frappe.db.sql(
		"""
		SELECT name, amount FROM `tabStock Entry Detail`
		WHERE parent=%s AND (is_finished_item=1 OR is_scrap_item=1
		      OR IFNULL(secondary_item_type,'')!=''
		      OR (IFNULL(t_warehouse,'')!='' AND IFNULL(s_warehouse,'')=''))
		ORDER BY idx
		""",
		voucher_no,
	)
	return [(r[0], flt(r[1])) for r in rows]
