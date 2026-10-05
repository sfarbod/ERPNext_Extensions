"""Controlled Apply runner for PO-JOB08760 — Development only."""

from __future__ import annotations

import json

import frappe
from frappe.utils import cint, flt


JC = "PO-JOB08760"
FOREIGN = [
	"MAT-STE-2026-32617",
	"MAT-STE-2026-40364",
	"MAT-STE-2026-33377",
	"MAT-STE-2026-33928",
	"MAT-STE-2026-40369",
	"MAT-STE-2026-37643",
	"MAT-STE-2026-37777",
]
DISP = [
	{
		"item_code": "13200544",
		"batch_no": "5648-13200544-PR-10741",
		"proposed_consumed": 1148,
		"proposed_scrap": 0,
		"proposed_return": 0,
		"proposed_still_in_wip": 0,
	}
]


def _plan_input(fingerprint=None):
	p = {
		"dispositions": DISP,
		"merge_documents": ["MAT-STE-2026-31724-1"],
		"stamp_mode": "HISTORICAL",
	}
	if fingerprint:
		p["fingerprint"] = fingerprint
	return p


def rescan():
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import scan_golden_rule
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
		build_manufacture_plan,
	)

	scan = scan_golden_rule(JC)
	row = next(
		r
		for r in scan["rows"]
		if r["item_code"] == "13200544" and r.get("batch_no") == "5648-13200544-PR-10741"
	)
	plan = build_manufacture_plan(
		JC,
		dispositions=DISP,
		merge_documents=["MAT-STE-2026-31724-1"],
		stamp_mode="HISTORICAL",
	)
	return {
		"row": {
			k: row.get(k)
			for k in (
				"issued",
				"returned",
				"consumed",
				"remaining_wip",
				"status",
				"suggested_action",
			)
		},
		"fingerprint": plan["fingerprint"],
		"scan_fingerprint": scan["fingerprint"],
		"apply_allowed": plan["apply_allowed"],
		"blockers": plan["blockers"],
		"bridge": {
			"required": bool((plan.get("temporary_bridge") or {}).get("required")),
			"shortages": (plan.get("temporary_bridge") or {}).get("shortages") or [],
		},
		"cancel_set": plan.get("minimal_cancel_set"),
		"stamp": (plan.get("canonical_manufacture") or {}).get("historical_stamp"),
	}


def dry_run():
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import run_repair

	pre = rescan()
	if not pre["apply_allowed"]:
		return {"ok": False, "phase": "precheck", "pre": pre}
	row = pre["row"]
	if abs(flt(row["issued"]) - 1160) > 1e-6 or abs(flt(row["returned"]) - 12) > 1e-6:
		return {"ok": False, "phase": "baseline", "pre": pre, "error": "Issued/Returned drift"}
	if abs(flt(row["consumed"])) > 1e-6 or abs(flt(row["remaining_wip"]) - 1148) > 1e-6:
		return {"ok": False, "phase": "baseline", "pre": pre, "error": "Consume/Remaining drift"}
	res = run_repair(JC, plan_input=_plan_input(pre["fingerprint"]), dry_run=True)
	return {
		"ok": bool(res.get("ok")) and res.get("status") == "DRY_RUN_PASS",
		"status": res.get("status"),
		"error": res.get("error"),
		"pre": pre,
		"cancelled": res.get("cancelled"),
		"created": res.get("created"),
		"recreated": res.get("recreated_logistics"),
		"temp_absent": res.get("temporary_receipt_absent"),
		"bridge": res.get("temporary_bridge"),
	}


def apply_now():
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import run_repair
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import scan_golden_rule
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.shared_logistics import batch_qty
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.temporary_stock_bridge import (
		TEMP_REMARK_MARKER,
	)

	pre = rescan()
	if not pre["apply_allowed"]:
		return {"ok": False, "phase": "precheck", "pre": pre}
	# Fresh dry run gate
	dry = dry_run()
	if not dry.get("ok"):
		return {"ok": False, "phase": "dry_run", "dry": dry}

	# Re-fingerprint after dry run (rollback may update audit but not business)
	pre2 = rescan()
	res = run_repair(
		JC, plan_input=_plan_input(pre2["fingerprint"]), dry_run=False, confirm=1
	)
	out = {
		"ok": bool(res.get("ok")) and res.get("status") == "APPLY_PASS",
		"status": res.get("status"),
		"error": res.get("error"),
		"committed": res.get("committed"),
		"mutated": res.get("mutated"),
		"canonical_name": res.get("canonical_name"),
		"cancelled": res.get("cancelled"),
		"recreated": res.get("recreated_logistics"),
		"temporary_receipt": res.get("temporary_receipt"),
		"temporary_receipt_delete": res.get("temporary_receipt_delete"),
		"temporary_receipt_absent": res.get("temporary_receipt_absent"),
		"bridge_shortages": (res.get("temporary_bridge") or {}).get("shortages"),
		"equivalence": res.get("logistics_equivalence"),
		"valuation": res.get("valuation"),
		"verification": res.get("verification"),
		"pre": pre2,
		"dry_status": dry.get("status"),
	}
	if not out["ok"]:
		return out

	# Post-apply assertions
	scan = scan_golden_rule(JC)
	row = next(
		r
		for r in scan["rows"]
		if r["item_code"] == "13200544" and r.get("batch_no") == "5648-13200544-PR-10741"
	)
	active_mfg = frappe.db.sql(
		"""
		select name, custom_manufacturing_costing_contract_version as stamp, fg_completed_qty, docstatus
		from `tabStock Entry`
		where job_card=%s and purpose='Manufacture' and docstatus=1
		""",
		JC,
		as_dict=1,
	)
	temps = frappe.db.sql(
		"select name from `tabStock Entry` where remarks like %s",
		(f"%{TEMP_REMARK_MARKER}%",),
		pluck=True,
	)
	item, batch = "20100067", "505188-20100067-MY262821A11"
	milan = "انبار آماده فروش -میلان پارس"
	retain = "انبار Retain Sample محصول نهایی اسپاد"
	foreign = {n: cint(frappe.db.get_value("Stock Entry", n, "docstatus")) for n in FOREIGN}
	old = {
		"31724-1": cint(frappe.db.get_value("Stock Entry", "MAT-STE-2026-31724-1", "docstatus")),
		"31725": cint(frappe.db.get_value("Stock Entry", "MAT-STE-2026-31725", "docstatus")),
		"31726": cint(frappe.db.get_value("Stock Entry", "MAT-STE-2026-31726", "docstatus")),
	}
	canon = active_mfg[0].name if len(active_mfg) == 1 else None
	gl = (
		frappe.db.sql(
			"""
			select sum(debit) d, sum(credit) c from `tabGL Entry`
			where voucher_type='Stock Entry' and voucher_no=%s and is_cancelled=0
			""",
			canon,
			as_dict=1,
		)[0]
		if canon
		else None
	)
	fg_rows = []
	if canon:
		fg_rows = frappe.db.sql(
			"""
			select item_code, batch_no, qty, t_warehouse, is_finished_item
			from `tabStock Entry Detail` where parent=%s and item_code=%s
			order by idx
			""",
			(canon, "20100067"),
			as_dict=1,
		)
	out["post"] = {
		"golden_13200544": {
			k: row.get(k)
			for k in ("issued", "returned", "consumed", "remaining_wip", "status")
		},
		"active_manufactures": active_mfg,
		"temps": temps,
		"old_docstatus": old,
		"foreign": foreign,
		"fg_physical": {
			"milan": batch_qty(item, batch, milan),
			"retain": batch_qty(item, batch, retain),
		},
		"fg_rows": fg_rows,
		"gl": {"debit": flt(gl.d) if gl else None, "credit": flt(gl.c) if gl else None},
		"stamp": active_mfg[0].stamp if active_mfg else None,
	}
	# Hard gates
	g = out["post"]["golden_13200544"]
	errs = []
	if abs(flt(g["issued"]) - 1160) > 1e-6 or abs(flt(g["returned"]) - 12) > 1e-6:
		errs.append("issued/returned mismatch")
	if abs(flt(g["consumed"]) - 1148) > 1e-6 or abs(flt(g["remaining_wip"])) > 1e-6:
		errs.append(f"consume/remaining fail: {g}")
	if g["status"] not in ("OK",):
		# MULTIPLE MANUFACTURE shouldn't apply; OK expected
		if g["status"] != "OK":
			errs.append(f"golden status {g['status']}")
	if len(active_mfg) != 1:
		errs.append(f"active mfg count {len(active_mfg)}")
	if temps:
		errs.append(f"temp survives: {temps}")
	if old["31724-1"] != 2 or old["31725"] != 2 or old["31726"] != 2:
		errs.append(f"old docs not cancelled: {old}")
	if any(v != 1 for v in foreign.values()):
		errs.append(f"foreign drift: {foreign}")
	if abs(out["post"]["fg_physical"]["milan"] - 545) > 1e-6:
		errs.append(f"milan qty {out['post']['fg_physical']['milan']}")
	if abs(out["post"]["fg_physical"]["retain"] - 29) > 1e-6:
		errs.append(f"retain qty {out['post']['fg_physical']['retain']}")
	if gl and abs(flt(gl.d) - flt(gl.c)) > 1e-6:
		errs.append(f"GL imbalance {gl}")
	if (active_mfg[0].stamp if active_mfg else None) != "5.3.34":
		errs.append(f"stamp {active_mfg[0].stamp if active_mfg else None}")
	out["post_errors"] = errs
	out["post_ok"] = not errs
	out["ok"] = out["ok"] and out["post_ok"]
	return out


def run():
	return apply_now()
