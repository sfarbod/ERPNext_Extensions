# Copyright (c) 2026, ERPNext Extensions contributors
"""Development-only: backup fingerprint, recalculate, validate, submit MAT-STE-2026-43536.

Usage (after verified DB backup):
  bench --site development.localhost execute \\
    erpnext_extensions.iran_accounting.scripts._phase2_43536_submit.run \\
    --kwargs '{"mode":"fingerprint"}'
  ... mode=recalculate | validate_preview | submit
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import frappe
from frappe.utils import flt, now_datetime

TARGET = "MAT-STE-2026-43536"
REPORT_DIR = Path(
	frappe.get_site_path("phase3_baselines")
) if False else None  # set in run()


def _round_int(v) -> int:
	return int(Decimal(str(flt(v))).quantize(Decimal("1")))


def fingerprint(name: str = TARGET) -> dict:
	se = frappe.get_doc("Stock Entry", name)
	sed = []
	for i in se.items:
		sed.append(
			{
				"name": i.name,
				"idx": i.idx,
				"item_code": i.item_code,
				"qty": flt(i.qty),
				"basic_rate": flt(i.basic_rate),
				"basic_amount": flt(i.basic_amount),
				"valuation_rate": flt(i.valuation_rate),
				"amount": flt(i.amount),
				"s_warehouse": i.s_warehouse,
				"t_warehouse": i.t_warehouse,
				"batch_no": i.batch_no,
				"serial_and_batch_bundle": i.serial_and_batch_bundle,
				"is_finished_item": cint_safe(i.is_finished_item),
				"is_scrap_item": cint_safe(i.is_scrap_item),
			}
		)
	sle = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, actual_qty, qty_after_transaction,
		       valuation_rate, stock_value, stock_value_difference, is_cancelled
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s
		ORDER BY posting_date, posting_time, creation
		""",
		name,
		as_dict=True,
	)
	gl = frappe.db.sql(
		"""
		SELECT name, account, debit, credit, is_cancelled, remarks
		FROM `tabGL Entry`
		WHERE voucher_no=%s
		ORDER BY account, name
		""",
		name,
		as_dict=True,
	)
	bins = []
	seen = set()
	for i in se.items:
		for wh in (i.s_warehouse, i.t_warehouse):
			if not wh:
				continue
			key = (i.item_code, wh)
			if key in seen:
				continue
			seen.add(key)
			b = frappe.db.get_value(
				"Bin",
				{"item_code": i.item_code, "warehouse": wh},
				["actual_qty", "valuation_rate", "stock_value"],
				as_dict=True,
			)
			if b:
				bins.append(
					{
						"item_code": i.item_code,
						"warehouse": wh,
						"actual_qty": flt(b.actual_qty),
						"valuation_rate": flt(b.valuation_rate),
						"stock_value": flt(b.stock_value),
					}
				)
	batches = []
	for i in se.items:
		if not i.batch_no:
			continue
		bt = frappe.db.get_value(
			"Batch", i.batch_no, ["batch_qty"], as_dict=True
		)
		batches.append(
			{
				"batch_no": i.batch_no,
				"item_code": i.item_code,
				"batch_qty": flt(bt.batch_qty) if bt else None,
			}
		)
	# Serial and Batch Entry amounts (SABE)
	sabe = frappe.db.sql(
		"""
		SELECT sbe.name, sbe.batch_no, sbe.qty, sbe.incoming_rate, sbe.stock_value_difference,
		       sbb.voucher_no, sbb.item_code, sbb.warehouse
		FROM `tabSerial and Batch Entry` sbe
		INNER JOIN `tabSerial and Batch Bundle` sbb ON sbb.name = sbe.parent
		WHERE sbb.voucher_no=%s OR sbb.name IN (
			SELECT serial_and_batch_bundle FROM `tabStock Entry Detail`
			WHERE parent=%s AND IFNULL(serial_and_batch_bundle,'')!=''
		)
		""",
		(name, name),
		as_dict=True,
	)
	payload = {
		"at": str(now_datetime()),
		"target": name,
		"docstatus": se.docstatus,
		"purpose": se.purpose,
		"company": se.company,
		"posting_date": str(se.posting_date),
		"modified": str(se.modified),
		"value_difference": flt(se.value_difference),
		"total_outgoing_value": flt(se.total_outgoing_value),
		"total_incoming_value": flt(se.total_incoming_value),
		"sed": sed,
		"sle": [dict(r) for r in sle],
		"gl": [dict(r) for r in gl],
		"bins": bins,
		"batches": batches,
		"sabe": [dict(r) for r in sabe],
		"sa_account": frappe.db.get_value(
			"Company", se.company, "stock_adjustment_account"
		),
	}
	blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
	payload["fingerprint_sha256"] = hashlib.sha256(blob.encode()).hexdigest()
	return payload


def cint_safe(v) -> int:
	try:
		return int(v or 0)
	except Exception:
		return 0


def validate_preconditions(fp: dict) -> list[str]:
	errs = []
	if fp["docstatus"] != 0:
		errs.append(f"docstatus={fp['docstatus']} expected Draft(0)")
	if fp["purpose"] != "Manufacture":
		errs.append(f"purpose={fp['purpose']} expected Manufacture")
	if fp["sle"]:
		errs.append(f"unexpected SLE count={len(fp['sle'])} on Draft")
	if fp["gl"]:
		errs.append(f"unexpected GL count={len(fp['gl'])} on Draft")
	# Case B: full-clear rows must have SED amount == Bin stock_value
	bin_map = {(b["item_code"], b["warehouse"]): b for b in fp["bins"]}
	for row in fp["sed"]:
		wh = row.get("s_warehouse")
		if not wh or not flt(row.get("qty")):
			continue
		b = bin_map.get((row["item_code"], wh))
		if not b:
			continue
		if abs(flt(b["actual_qty"]) - flt(row["qty"])) < 1e-9:
			# consume-to-zero: amounts must match bin stock_value
			if abs(flt(b["stock_value"]) - flt(row["basic_amount"])) >= 0.5:
				errs.append(
					f"CaseB gap {row['item_code']}@{wh}: "
					f"bin_sv={b['stock_value']} sed_amt={row['basic_amount']}"
				)
	# SABE integerization: stock_value_difference / incoming_rate*qty should be int where qty int
	for s in fp["sabe"]:
		qty = flt(s.get("qty"))
		svd = flt(s.get("stock_value_difference"))
		if abs(qty - round(qty)) < 1e-9 and abs(svd - round(svd)) >= 0.5:
			errs.append(
				f"SABE fractional svd {s.get('name')} qty={qty} svd={svd}"
			)
	return errs


def recalculate(name: str = TARGET) -> dict:
	from erpnext_extensions.iran_accounting.integration.bootstrap import (
		apply as bootstrap_apply,
	)
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		ensure_iran_riv_gl_handlers_active,
		ensure_iran_riv_recalculate_wrapper_active,
	)

	bootstrap_apply()
	ensure_iran_riv_recalculate_wrapper_active(bootstrap=False)
	ensure_iran_riv_gl_handlers_active(bootstrap=False)

	se = frappe.get_doc("Stock Entry", name)
	if se.docstatus != 0:
		frappe.throw(f"{name} is not Draft")
	before = {
		"value_difference": flt(se.value_difference),
		"total_outgoing_value": flt(se.total_outgoing_value),
		"total_incoming_value": flt(se.total_incoming_value),
		"rows": [
			(i.name, flt(i.basic_rate), flt(i.basic_amount)) for i in se.items
		],
	}
	se.set_missing_values()
	# Prefer get_stock_and_rate which mirrors UI "Update Rate and Availability"
	if hasattr(se, "get_stock_and_rate"):
		se.get_stock_and_rate()
	elif hasattr(se, "set_rate_and_amount"):
		se.set_rate_and_amount()
	se.flags.ignore_validate_update_after_submit = True
	se.save()
	frappe.db.commit()
	se.reload()
	after = {
		"value_difference": flt(se.value_difference),
		"total_outgoing_value": flt(se.total_outgoing_value),
		"total_incoming_value": flt(se.total_incoming_value),
		"rows": [
			(i.name, flt(i.basic_rate), flt(i.basic_amount)) for i in se.items
		],
	}
	return {"before": before, "after": after, "changed": before != after}


def gl_preview(name: str = TARGET) -> dict:
	"""Simulate submit inside a DB savepoint, capture GL, then roll back.

	Draft Stock Entries have no SLE yet, so ``get_gl_entries()`` alone is empty.
	"""
	from erpnext_extensions.iran_accounting.integration.bootstrap import (
		apply as bootstrap_apply,
	)
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		ensure_iran_riv_gl_handlers_active,
		ensure_iran_riv_recalculate_wrapper_active,
	)

	bootstrap_apply()
	ensure_iran_riv_recalculate_wrapper_active(bootstrap=False)
	ensure_iran_riv_gl_handlers_active(bootstrap=False)

	se = frappe.get_doc("Stock Entry", name)
	if se.docstatus != 0:
		frappe.throw(f"{name} is not Draft")

	sa = frappe.db.get_value("Company", se.company, "stock_adjustment_account")
	sp = f"phase2_43536_preview_{frappe.generate_hash(length=8)}"
	frappe.db.savepoint(sp)
	err = None
	gl_map = []
	try:
		# Mutate inside savepoint only — never commit.
		se.flags.ignore_permissions = True
		se.submit()
		gl_map = frappe.db.sql(
			"""
			SELECT account, debit, credit, remarks
			FROM `tabGL Entry`
			WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
			""",
			name,
			as_dict=True,
		)
	except Exception as e:
		err = frappe.get_traceback()
		frappe.log_error(title=f"43536 GL preview failed: {e}", message=err)
	finally:
		frappe.db.rollback(save_point=sp)
		# Reload clean draft state for subsequent steps
		frappe.clear_document_cache("Stock Entry", name)

	debit = sum(flt(e.get("debit")) for e in gl_map)
	credit = sum(flt(e.get("credit")) for e in gl_map)
	sa_legs = [
		{
			"account": e.get("account"),
			"debit": flt(e.get("debit")),
			"credit": flt(e.get("credit")),
			"remarks": (e.get("remarks") or "")[:200],
			"irr_a2": "IRR_SABE_A2|" in str(e.get("remarks") or ""),
		}
		for e in gl_map
		if e.get("account") == sa
	]
	# Confirm still draft after rollback
	docstatus = frappe.db.get_value("Stock Entry", name, "docstatus")
	return {
		"gl_count": len(gl_map),
		"debit": debit,
		"credit": credit,
		"diff": debit - credit,
		"balanced": abs(debit - credit) < 0.5 and err is None,
		"sa_account": sa,
		"sa_legs": sa_legs,
		"sa_net": sum(flt(x["credit"]) - flt(x["debit"]) for x in sa_legs),
		"a2_leg_count": sum(1 for x in sa_legs if x["irr_a2"]),
		"preview_error": err,
		"docstatus_after_rollback": docstatus,
	}


def submit_target(name: str = TARGET) -> dict:
	from erpnext_extensions.iran_accounting.integration.bootstrap import (
		apply as bootstrap_apply,
	)
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		ensure_iran_riv_gl_handlers_active,
		ensure_iran_riv_recalculate_wrapper_active,
	)

	bootstrap_apply()
	ensure_iran_riv_recalculate_wrapper_active(bootstrap=False)
	ensure_iran_riv_gl_handlers_active(bootstrap=False)

	se = frappe.get_doc("Stock Entry", name)
	if se.docstatus != 0:
		frappe.throw(f"{name} is not Draft (docstatus={se.docstatus})")
	se.submit()
	frappe.db.commit()
	se.reload()
	fp = fingerprint(name)
	gl_sum = frappe.db.sql(
		"""
		SELECT SUM(debit) d, SUM(credit) c
		FROM `tabGL Entry`
		WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
		""",
		name,
	)[0]
	sa = frappe.db.get_value("Company", se.company, "stock_adjustment_account")
	sa_rows = frappe.db.sql(
		"""
		SELECT name, debit, credit, remarks
		FROM `tabGL Entry`
		WHERE voucher_no=%s AND account=%s AND IFNULL(is_cancelled,0)=0
		""",
		(name, sa),
		as_dict=True,
	)
	return {
		"docstatus": se.docstatus,
		"gl_debit": flt(gl_sum[0]),
		"gl_credit": flt(gl_sum[1]),
		"gl_diff": flt(gl_sum[0]) - flt(gl_sum[1]),
		"sle_count": len(fp["sle"]),
		"sa_rows": [dict(r) for r in sa_rows],
		"fingerprint_sha256": fp["fingerprint_sha256"],
		"value_difference": flt(se.value_difference),
	}


def run(mode: str = "fingerprint", backup_verified: int = 0):
	site = frappe.local.site
	if site != "development.localhost":
		frappe.throw(f"Refusing to run on site={site}; Development only")
	out_dir = Path(frappe.get_site_path("phase3_baselines"))
	out_dir.mkdir(parents=True, exist_ok=True)

	if mode == "fingerprint":
		fp = fingerprint()
		errs = validate_preconditions(fp)
		path = out_dir / "phase2_43536_pre_fingerprint.json"
		path.write_text(
			json.dumps({"fingerprint": fp, "precondition_errors": errs}, ensure_ascii=False, indent=2, default=str),
			encoding="utf-8",
		)
		return {
			"ok": not errs,
			"path": str(path),
			"docstatus": fp["docstatus"],
			"fingerprint_sha256": fp["fingerprint_sha256"],
			"precondition_errors": errs,
			"sabe_count": len(fp["sabe"]),
			"sed_count": len(fp["sed"]),
		}

	if mode == "recalculate":
		if not cint_safe(backup_verified):
			frappe.throw("Set backup_verified=1 after taking a verified DB backup")
		result = recalculate()
		fp = fingerprint()
		errs = validate_preconditions(fp)
		path = out_dir / "phase2_43536_post_recalculate.json"
		path.write_text(
			json.dumps(
				{"recalculate": result, "fingerprint": fp, "precondition_errors": errs},
				ensure_ascii=False,
				indent=2,
				default=str,
			),
			encoding="utf-8",
		)
		return {"ok": not errs, "path": str(path), "recalculate": result, "precondition_errors": errs}

	if mode == "validate_preview":
		prev = gl_preview()
		fp = fingerprint()
		errs = validate_preconditions(fp)
		if not prev["balanced"]:
			errs.append(f"GL preview unbalanced diff={prev['diff']}")
		path = out_dir / "phase2_43536_gl_preview.json"
		path.write_text(
			json.dumps(
				{"preview": prev, "fingerprint_sha256": fp["fingerprint_sha256"], "errors": errs},
				ensure_ascii=False,
				indent=2,
				default=str,
			),
			encoding="utf-8",
		)
		return {"ok": not errs and prev["balanced"], "path": str(path), "preview": prev, "errors": errs}

	if mode == "submit":
		if not cint_safe(backup_verified):
			frappe.throw("Set backup_verified=1 after taking a verified DB backup")
		fp = fingerprint()
		errs = validate_preconditions(fp)
		prev = gl_preview()
		if not prev["balanced"]:
			errs.append(f"GL preview unbalanced diff={prev['diff']}")
		if errs:
			return {"ok": False, "blocked": True, "errors": errs, "preview": prev}
		result = submit_target()
		post = fingerprint()
		path = out_dir / "phase2_43536_submit_result.json"
		path.write_text(
			json.dumps(
				{"submit": result, "post_fingerprint": post, "preview_before": prev},
				ensure_ascii=False,
				indent=2,
				default=str,
			),
			encoding="utf-8",
		)
		ok = (
			result["docstatus"] == 1
			and abs(result["gl_diff"]) < 0.5
			and result["sle_count"] > 0
		)
		return {"ok": ok, "path": str(path), "submit": result}

	frappe.throw(f"Unknown mode={mode}")


def debug_submit_mismatches(name: str = TARGET):
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as bootstrap_apply
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		ensure_iran_riv_gl_handlers_active,
		ensure_iran_riv_recalculate_wrapper_active,
	)
	bootstrap_apply()
	ensure_iran_riv_recalculate_wrapper_active(bootstrap=False)
	ensure_iran_riv_gl_handlers_active(bootstrap=False)
	se = frappe.get_doc("Stock Entry", name)
	sp = f"dbg_{frappe.generate_hash(length=6)}"
	frappe.db.savepoint(sp)
	out = {"error": None, "mismatches": [], "sle_sum": 0, "fg": None}
	try:
		se.flags.ignore_permissions = True
		se.submit()
		out["error"] = "submit_unexpectedly_succeeded"
	except Exception as e:
		out["error"] = str(e)
		rows = frappe.db.sql(
			"""
			SELECT name, item_code, actual_qty, stock_value_difference, voucher_detail_no
			FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
			ORDER BY creation
			""",
			name,
			as_dict=True,
		)
		out["sle_count"] = len(rows)
		out["sle_sum"] = sum(flt(r.stock_value_difference) for r in rows)
		for r in rows:
			sed = frappe.db.get_value(
				"Stock Entry Detail",
				r.voucher_detail_no,
				["item_code", "basic_amount", "amount", "is_finished_item", "s_warehouse", "t_warehouse"],
				as_dict=True,
			)
			if not sed:
				continue
			mag = flt(sed.amount) if flt(sed.amount) else flt(sed.basic_amount)
			expected = -mag if flt(r.actual_qty) < 0 else mag
			delta = flt(r.stock_value_difference) - expected
			if abs(delta) >= 0.5:
				out["mismatches"].append(
					{
						"item": r.item_code,
						"sle_svd": flt(r.stock_value_difference),
						"sed_mirror": expected,
						"delta": delta,
						"detail": r.voucher_detail_no,
						"fg": cint_safe(sed.is_finished_item),
					}
				)
		out["fg"] = frappe.db.get_value(
			"Stock Entry Detail",
			{"parent": name, "is_finished_item": 1},
			["name", "basic_amount", "amount", "basic_rate"],
			as_dict=True,
		)
		# consume deltas summary
		out["mismatch_net_delta"] = sum(m["delta"] for m in out["mismatches"])
	finally:
		frappe.db.rollback(save_point=sp)
	out["docstatus"] = frappe.db.get_value("Stock Entry", name, "docstatus")
	path = Path(frappe.get_site_path("phase3_baselines")) / "phase2_43536_debug_mismatches.json"
	path.write_text(frappe.as_json(out), encoding="utf-8")
	return {"path": str(path), "error": out["error"], "mismatches": out["mismatches"], "sle_sum": out["sle_sum"], "mismatch_net_delta": out.get("mismatch_net_delta"), "fg": out.get("fg")}
