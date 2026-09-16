# Copyright (c) 2026, ERPNext Extensions contributors
"""Unified valuation rebuild: SE → SLE → SABB/SBE → Bin → GL.

Shared by posting-order repair and Zero / Lost Rate. Does not invoke RIV.
Manufacture inbound SVD is the 5.2.0 source of truth when that voucher is healthy.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, get_datetime

from erpnext_extensions.iran_accounting.historical_stock import (
	STATUS_BLOCKED,
	VALUE_EPS,
)
from erpnext_extensions.iran_accounting.historical_stock.manufacture import preview_manufacture_voucher
from erpnext_extensions.iran_accounting.stock_posting_order import (
	STATUS_GL_REBUILD_REQUIRED,
	STATUS_INTEGRITY_COMPLETE,
	STATUS_ORDER_FIXED_RATE_REBUILD_REQUIRED,
	STATUS_RATE_REBUILD_COMPLETE,
	STATUS_RATE_REBUILD_IN_PROGRESS,
	STATUS_DOWNSTREAM_VALUE_REPLAY_REQUIRED,
	STATUS_DOWNSTREAM_REPLAY_REQUIRED,
)
from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
	VALUE_EPS as REPLAY_VALUE_EPS,
	_bin_from_last_sle,
	replay_item_warehouse,
	sync_transfer_incoming_rates,
)
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D

TRANSFER_PURPOSES = (
	"Material Transfer",
	"Material Transfer for Manufacture",
	"Send to Subcontractor",
)


def _txn_rate(actual_qty, stock_value_difference):
	qty = D(actual_qty)
	if qty == 0:
		return D(0)
	return abs(D(stock_value_difference) / qty)


def diagnose_sle_rate_gaps(voucher_no: str, item_code=None) -> list[dict]:
	conds = ["voucher_type='Stock Entry'", "voucher_no=%s", "is_cancelled=0"]
	args = [voucher_no]
	if item_code:
		conds.append("item_code=%s")
		args.append(item_code)
	rows = frappe.db.sql(
		f"""
		SELECT name, item_code, warehouse, actual_qty, incoming_rate, outgoing_rate,
		       valuation_rate, stock_value, stock_value_difference, qty_after_transaction,
		       serial_and_batch_bundle, voucher_detail_no
		FROM `tabStock Ledger Entry`
		WHERE {" AND ".join(conds)}
		""",
		args,
		as_dict=True,
	)
	gaps = []
	for sle in rows:
		qty = D(sle.actual_qty)
		svd = D(sle.stock_value_difference)
		implied = _txn_rate(qty, svd)
		flags = []
		if qty > 0 and abs(svd) > VALUE_EPS and abs(D(sle.incoming_rate)) <= VALUE_EPS:
			flags.append("STALE_INCOMING_RATE")
		if qty < 0 and abs(svd) > VALUE_EPS and abs(D(sle.outgoing_rate)) <= VALUE_EPS:
			flags.append("STALE_OUTGOING_RATE")
		if qty < 0 and abs(svd) > VALUE_EPS and abs(implied) > VALUE_EPS and abs(D(sle.outgoing_rate)) <= VALUE_EPS:
			flags.append("ZERO_OUTGOING_WITH_VALUE")
		if sle.serial_and_batch_bundle:
			sbe = frappe.db.sql(
				"""
				SELECT incoming_rate, outgoing_rate, stock_value_difference, qty, batch_no
				FROM `tabSerial and Batch Entry` WHERE parent=%s
				""",
				sle.serial_and_batch_bundle,
				as_dict=True,
			)
			sabb = frappe.db.get_value(
				"Serial and Batch Bundle",
				sle.serial_and_batch_bundle,
				["avg_rate", "total_amount", "total_qty"],
				as_dict=True,
			)
			for e in sbe:
				if qty < 0 and abs(svd) > VALUE_EPS and abs(D(e.stock_value_difference)) <= VALUE_EPS:
					flags.append("STALE_SABB_SVD")
				if qty < 0 and abs(svd) > VALUE_EPS and abs(D(e.outgoing_rate)) <= VALUE_EPS:
					flags.append("STALE_SABB_OUTGOING")
				if qty > 0 and abs(svd) > VALUE_EPS and abs(D(e.incoming_rate)) <= VALUE_EPS:
					flags.append("STALE_SABB_INCOMING")
				if abs(abs(D(e.stock_value_difference)) - abs(svd)) > VALUE_EPS:
					flags.append("SABB_SVD_MISMATCH")
			if sabb and abs(abs(D(sabb.total_amount)) - abs(svd)) > VALUE_EPS:
				flags.append("SABB_AMOUNT_MISMATCH")
			if sabb and abs(svd) > VALUE_EPS and abs(D(sabb.avg_rate)) <= VALUE_EPS:
				flags.append("STALE_SABB_AVG_RATE")
		if flags:
			gaps.append(
				{
					"sle": sle.name,
					"item_code": sle.item_code,
					"warehouse": sle.warehouse,
					"actual_qty": sle.actual_qty,
					"incoming_rate": sle.incoming_rate,
					"outgoing_rate": sle.outgoing_rate,
					"implied_rate": flt(implied),
					"svd": sle.stock_value_difference,
					"flags": sorted(set(flags)),
				}
			)
	return gaps


def diagnose_chain(inbound_document, outbound_document, item=None, warehouse=None, batch=None) -> dict:
	in_gaps = diagnose_sle_rate_gaps(inbound_document, item)
	out_gaps = diagnose_sle_rate_gaps(outbound_document, item)
	mfg = None
	if inbound_document and frappe.db.get_value("Stock Entry", inbound_document, "purpose") == "Manufacture":
		mfg = preview_manufacture_voucher(inbound_document)
	neutral = True
	if outbound_document and item:
		rows = frappe.db.sql(
			"""
			SELECT actual_qty, stock_value_difference FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND item_code=%s AND is_cancelled=0
			""",
			(outbound_document, item),
			as_dict=True,
		)
		outs = [r for r in rows if D(r.actual_qty) < 0]
		ins = [r for r in rows if D(r.actual_qty) > 0]
		if outs and ins:
			neutral = abs(abs(D(outs[0].stock_value_difference)) - abs(D(ins[0].stock_value_difference))) <= VALUE_EPS
	stale = bool(in_gaps or out_gaps or not neutral)
	status = STATUS_ORDER_FIXED_RATE_REBUILD_REQUIRED if stale else STATUS_RATE_REBUILD_COMPLETE
	source_rate = None
	if inbound_document and item:
		src = frappe.db.sql(
			"""
			SELECT incoming_rate, stock_value_difference, actual_qty
			FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND item_code=%s AND actual_qty>0 AND is_cancelled=0
			LIMIT 1
			""",
			(inbound_document, item),
			as_dict=True,
		)
		if src:
			source_rate = flt(_txn_rate(src[0].actual_qty, src[0].stock_value_difference) or src[0].incoming_rate)
	return {
		"inbound_document": inbound_document,
		"outbound_document": outbound_document,
		"item": item,
		"warehouse": warehouse,
		"batch": batch,
		"stale": stale,
		"status": status,
		"quantity_impact": "FIXED_OR_HEALTHY",
		"valuation_impact": "STALE / REBUILD REQUIRED" if stale else "OK",
		"manufacture_source_rate": source_rate,
		"manufacture_needs_repair": bool(mfg and mfg.get("needs_repair")),
		"manufacture_status": (mfg or {}).get("status"),
		"transfer_value_neutral": neutral,
		"inbound_gaps": in_gaps,
		"outbound_gaps": out_gaps,
		"riv": "NOT_INVOKED",
	}


def write_sle_transaction_rates(voucher_no: str, item_code=None) -> list[dict]:
	conds = ["voucher_type='Stock Entry'", "voucher_no=%s", "is_cancelled=0"]
	args = [voucher_no]
	if item_code:
		conds.append("item_code=%s")
		args.append(item_code)
	rows = frappe.db.sql(
		f"""
		SELECT name, actual_qty, stock_value_difference, incoming_rate, outgoing_rate
		FROM `tabStock Ledger Entry` WHERE {" AND ".join(conds)}
		""",
		args,
		as_dict=True,
	)
	written = []
	for sle in rows:
		rate = _txn_rate(sle.actual_qty, sle.stock_value_difference)
		values = {}
		if D(sle.actual_qty) > 0:
			if abs(D(sle.incoming_rate) - rate) > REPLAY_VALUE_EPS and rate > 0:
				values["incoming_rate"] = flt(rate)
			values["outgoing_rate"] = 0
		elif D(sle.actual_qty) < 0:
			values["outgoing_rate"] = flt(rate)
			values["incoming_rate"] = 0
		if values:
			frappe.db.set_value("Stock Ledger Entry", sle.name, values, update_modified=False)
			written.append({"sle": sle.name, **values})
	return written


def sync_sabb_from_sle(voucher_no: str, item_code=None) -> list[dict]:
	"""Copy SLE SVD / txn rate onto Serial and Batch Bundle + Entry (report source)."""
	conds = ["voucher_type='Stock Entry'", "voucher_no=%s", "is_cancelled=0"]
	args = [voucher_no]
	if item_code:
		conds.append("item_code=%s")
		args.append(item_code)
	sles = frappe.db.sql(
		f"""
		SELECT name, actual_qty, stock_value_difference, serial_and_batch_bundle, item_code
		FROM `tabStock Ledger Entry`
		WHERE {" AND ".join(conds)} AND IFNULL(serial_and_batch_bundle,'')!=''
		""",
		args,
		as_dict=True,
	)
	changed = []
	for sle in sles:
		qty = D(sle.actual_qty)
		svd = D(sle.stock_value_difference)
		rate = _txn_rate(qty, svd)
		bundle = sle.serial_and_batch_bundle
		if not frappe.db.exists("Serial and Batch Bundle", bundle):
			continue
		frappe.db.set_value(
			"Serial and Batch Bundle",
			bundle,
			{"avg_rate": flt(rate), "total_amount": flt(svd), "total_qty": flt(qty)},
			update_modified=False,
		)
		if qty >= 0:
			frappe.db.sql(
				"""
				UPDATE `tabSerial and Batch Entry`
				SET incoming_rate=%s, outgoing_rate=0, stock_value_difference=%s
				WHERE parent=%s
				""",
				(flt(rate), flt(svd), bundle),
			)
		else:
			frappe.db.sql(
				"""
				UPDATE `tabSerial and Batch Entry`
				SET incoming_rate=0, outgoing_rate=%s, stock_value_difference=%s
				WHERE parent=%s
				""",
				(flt(rate), flt(svd), bundle),
			)
		changed.append(
			{
				"voucher": voucher_no,
				"item_code": sle.item_code,
				"sabb": bundle,
				"avg_rate": flt(rate),
				"total_amount": flt(svd),
			}
		)
	return changed


def _align_se_detail_from_sle(voucher_no: str, item_code: str) -> None:
	sles = frappe.db.sql(
		"""
		SELECT voucher_detail_no, actual_qty, stock_value_difference
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND item_code=%s AND is_cancelled=0 AND actual_qty<0
		LIMIT 1
		""",
		(voucher_no, item_code),
		as_dict=True,
	)
	if not sles or not sles[0].voucher_detail_no:
		return
	rate = _txn_rate(sles[0].actual_qty, sles[0].stock_value_difference)
	qty = abs(flt(sles[0].actual_qty))
	amount = abs(flt(sles[0].stock_value_difference))
	frappe.db.set_value(
		"Stock Entry Detail",
		sles[0].voucher_detail_no,
		{
			"basic_rate": flt(rate),
			"valuation_rate": flt(rate),
			"amount": amount,
			"basic_amount": amount,
		},
		update_modified=False,
	)


def _downstream_same_item(item_code, warehouse, from_dt, exclude: set) -> list[str]:
	rows = frappe.db.sql(
		"""
		SELECT DISTINCT voucher_no FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND posting_datetime>=%s
		ORDER BY posting_datetime, creation
		""",
		(item_code, warehouse, from_dt),
		pluck=True,
	)
	return [v for v in rows if v not in exclude]


def rebuild_chain_valuation(
	inbound_document,
	outbound_document,
	item=None,
	warehouse=None,
	batch=None,
	*,
	dry_run=True,
	allow_riv=False,
) -> dict:
	"""Rebuild pair valuation. RIV is never auto-invoked."""
	if allow_riv:
		raise frappe.ValidationError("RIV is not invoked from valuation rebuild")
	diag = diagnose_chain(inbound_document, outbound_document, item, warehouse, batch)
	plan = {
		**diag,
		"dry_run": dry_run,
		"status": STATUS_RATE_REBUILD_IN_PROGRESS if not dry_run else diag["status"],
		"sequence": [
			"prerequisite Manufacture",
			"source SLE",
			"source Batch/SABB",
			"dependent Transfer OUT",
			"dependent Transfer IN",
			"Bin",
			"selective GL",
		],
		"riv": "BLOCKED_UNTIL_CHAIN_HEALTHY",
	}
	if dry_run:
		if item and batch and inbound_document:
			from erpnext_extensions.iran_accounting.historical_stock.downstream_replay import (
				plan_downstream_replay,
			)

			dt = frappe.db.get_value(
				"Stock Entry", inbound_document, ["posting_date", "posting_time"], as_dict=True
			)
			if dt:
				from_dt = get_datetime(f"{dt.posting_date} {dt.posting_time}")
				ds = plan_downstream_replay(
					item,
					batch,
					from_dt=from_dt,
					patient_vouchers=[v for v in (inbound_document, outbound_document) if v],
					warehouse=warehouse,
				)
				plan.update(
					{
						"downstream": ds,
						"replay_depth": ds.get("replay_depth"),
						"dependent_count": ds.get("dependent_count"),
						"affected_vouchers": ds.get("affected_vouchers"),
						"estimated_runtime": ds.get("estimated_runtime"),
						"replay_scope": ds.get("replay_scope"),
					}
				)
				if ds.get("replay_required_count") and not diag.get("stale"):
					plan["status"] = STATUS_DOWNSTREAM_REPLAY_REQUIRED
		return plan

	mfg_applied = False
	if diag.get("manufacture_needs_repair") and diag.get("manufacture_status") == "RECONSTRUCTABLE":
		from erpnext_extensions.iran_accounting.historical_stock.reconstruct import repair_manufacture_selected

		repair_manufacture_selected([{"voucher": inbound_document}], dry_run=False)
		mfg_applied = True

	from_dt = None
	if inbound_document:
		from_dt = frappe.db.get_value(
			"Stock Entry", inbound_document, ["posting_date", "posting_time"], as_dict=True
		)
		if from_dt:
			from_dt = get_datetime(f"{from_dt.posting_date} {from_dt.posting_time}")
		else:
			sle_dt = frappe.db.get_value(
				"Stock Ledger Entry",
				{"voucher_no": inbound_document, "is_cancelled": 0},
				"posting_datetime",
				order_by="posting_datetime asc",
			)
			from_dt = get_datetime(sle_dt) if sle_dt else None
	write = {v for v in (inbound_document, outbound_document) if v}
	# Only rewrite Stock Entry valuation; external inbounds are quantity anchors.
	if inbound_document and not frappe.db.exists("Stock Entry", inbound_document):
		write.discard(inbound_document)
	replay = []
	if item and warehouse and from_dt:
		replay.append(
			replay_item_warehouse(item, warehouse, from_dt, ignore_inversion_artifacts=True, write_vouchers=write)
		)
	if outbound_document:
		sync_transfer_incoming_rates(outbound_document)
	dest_wh = None
	if outbound_document and item:
		dest = frappe.db.sql(
			"""
			SELECT warehouse FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND item_code=%s AND actual_qty>0 AND is_cancelled=0 LIMIT 1
			""",
			(outbound_document, item),
			pluck=True,
		)
		if dest:
			dest_wh = dest[0]
			if from_dt:
				replay.append(
					replay_item_warehouse(
						item, dest_wh, from_dt, ignore_inversion_artifacts=True, write_vouchers=write
					)
				)
			sync_transfer_incoming_rates(outbound_document)

	sabb = []
	sle_rates = []
	for vn in (inbound_document, outbound_document):
		if not vn:
			continue
		sle_rates.extend(write_sle_transaction_rates(vn, item))
		sabb.extend(sync_sabb_from_sle(vn, item))
	if outbound_document and item:
		_align_se_detail_from_sle(outbound_document, item)

	gl = []
	from erpnext_extensions.iran_accounting.stock_posting_order.repair import _rebuild_gl_no_commit

	for vn in (inbound_document, outbound_document):
		if vn:
			gl.append(_rebuild_gl_no_commit(vn))
	if item and warehouse:
		_bin_from_last_sle(item, warehouse)
	if item and dest_wh:
		_bin_from_last_sle(item, dest_wh)

	from erpnext_extensions.iran_accounting.historical_stock.downstream_replay import (
		plan_downstream_replay,
	)

	ds_plan = {}
	if item and batch and from_dt:
		ds_plan = plan_downstream_replay(
			item,
			batch,
			from_dt=from_dt,
			patient_vouchers=list(write),
			warehouse=dest_wh or warehouse,
		)

	after = diagnose_chain(inbound_document, outbound_document, item, warehouse, batch)
	pair_gl_ok = all((g.get("gl") or {}).get("balanced", g.get("rebuilt") is False) for g in gl)
	if after["stale"]:
		status = STATUS_ORDER_FIXED_RATE_REBUILD_REQUIRED
	elif not pair_gl_ok:
		status = STATUS_GL_REBUILD_REQUIRED
	elif ds_plan.get("replay_required_count"):
		status = STATUS_DOWNSTREAM_REPLAY_REQUIRED
		after["valuation_impact"] = "OK"
		after["downstream_status"] = STATUS_DOWNSTREAM_REPLAY_REQUIRED
	else:
		status = STATUS_INTEGRITY_COMPLETE
		after["valuation_impact"] = "OK"
		after["downstream_status"] = ds_plan.get("status") or STATUS_DOWNSTREAM_VALUE_REPLAY_REQUIRED

	return {
		**after,
		"dry_run": False,
		"status": status,
		"mfg_applied": mfg_applied,
		"replay": replay,
		"sabb": sabb,
		"sle_rates": sle_rates,
		"gl_rebuilt": gl,
		"downstream": ds_plan,
		"downstream_vouchers": ds_plan.get("replay_order") or ds_plan.get("affected_vouchers") or [],
		"downstream_count": ds_plan.get("dependent_count") or 0,
		"replay_depth": ds_plan.get("replay_depth"),
		"dependent_count": ds_plan.get("dependent_count"),
		"affected_vouchers": ds_plan.get("affected_vouchers"),
		"estimated_runtime": ds_plan.get("estimated_runtime"),
		"replay_scope": ds_plan.get("replay_scope"),
		"riv": "NOT_INVOKED",
		"quantity_impact": after.get("quantity_impact"),
		"valuation_impact": after.get("valuation_impact"),
	}


def rebuild_affected_documents(rows: list[dict], *, dry_run=True) -> dict:
	applied = []
	blocked = []
	for row in rows or []:
		inbound = row.get("inbound_document") or row.get("voucher") or row.get("prerequisite")
		outbound = row.get("outbound_document") or row.get("dependent")
		try:
			if not inbound or not outbound:
				raise frappe.ValidationError("inbound_document and outbound_document are required")
			applied.append(
				rebuild_chain_valuation(
					inbound,
					outbound,
					item=row.get("item") or row.get("item_code"),
					warehouse=row.get("warehouse"),
					batch=row.get("batch"),
					dry_run=dry_run,
					allow_riv=False,
				)
			)
		except Exception as exc:
			blocked.append({"row": row, "error": str(exc), "status": STATUS_BLOCKED})
	return {"dry_run": dry_run, "applied": applied, "blocked": blocked, "count": len(applied)}


def scan_stale_valuation_chains(company=None, from_date=None, to_date=None, limit=50) -> list[dict]:
	"""Find submitted transfers whose SABB SVD is zero while SLE still has value."""
	conds = [
		"sle.is_cancelled=0",
		"sle.actual_qty<0",
		"ABS(sle.stock_value_difference)>1",
		"se.docstatus=1",
		"se.purpose IN %s",
	]
	args: list = [TRANSFER_PURPOSES]
	if company:
		conds.append("se.company=%s")
		args.append(company)
	if from_date:
		conds.append("sle.posting_date>=%s")
		args.append(from_date)
	if to_date:
		conds.append("sle.posting_date<=%s")
		args.append(to_date)
	rows = frappe.db.sql(
		f"""
		SELECT sle.voucher_no outbound_document, sle.item_code item, sle.warehouse,
		       sle.incoming_rate, sle.outgoing_rate, sle.stock_value_difference,
		       sle.serial_and_batch_bundle, se.posting_date, se.posting_time, se.work_order
		FROM `tabStock Ledger Entry` sle
		JOIN `tabStock Entry` se ON se.name=sle.voucher_no
		WHERE {" AND ".join(conds)}
		ORDER BY sle.posting_datetime
		LIMIT {int(limit) * 4}
		""",
		args,
		as_dict=True,
	)
	out = []
	seen = set()
	for r in rows:
		sbe_svd = 0
		if r.serial_and_batch_bundle:
			sbe_svd = flt(
				frappe.db.get_value(
					"Serial and Batch Entry",
					{"parent": r.serial_and_batch_bundle},
					"stock_value_difference",
				)
			)
		stale_out = abs(flt(r.outgoing_rate)) <= VALUE_EPS
		stale_sbe = abs(sbe_svd) <= VALUE_EPS
		if not stale_out and not stale_sbe:
			continue
		key = (r.outbound_document, r.item, r.warehouse)
		if key in seen:
			continue
		seen.add(key)
		against = frappe.db.get_value(
			"Stock Entry Detail",
			{"parent": r.outbound_document, "item_code": r.item},
			"against_stock_entry",
		)
		diag = diagnose_chain(against, r.outbound_document, r.item, r.warehouse)
		out.append(
			{
				"topic": "POSTING_ORDER",
				"detection": "RATE_REBUILD",
				"inbound_document": against,
				"outbound_document": r.outbound_document,
				"item": r.item,
				"warehouse": r.warehouse,
				"work_order": r.work_order,
				"status": STATUS_ORDER_FIXED_RATE_REBUILD_REQUIRED,
				"optimizer_status": STATUS_ORDER_FIXED_RATE_REBUILD_REQUIRED,
				"eligible": True,
				"confidence": "EXACT",
				"valuation_impact": "STALE / REBUILD REQUIRED",
				"quantity_impact": "ORDER_ALREADY_FIXED",
				"manufacture_source_rate": diag.get("manufacture_source_rate"),
				"chain": f"{against or '?'} → {r.outbound_document}",
				"current_outbound_time": f"{r.posting_date} {r.posting_time}",
				**{k: diag.get(k) for k in ("inbound_gaps", "outbound_gaps", "transfer_value_neutral")},
			}
		)
		if len(out) >= limit:
			break
	return out
