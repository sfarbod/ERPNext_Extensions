# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Dev-only baseline phase timing. Invoke via:
bench --site development.localhost execute \\
  erpnext_extensions.asset_usage_depreciation.scripts.baseline_timing.run
"""

from __future__ import annotations

import json
import time

import frappe
from frappe.utils import cint, flt

from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
	get_asset_depr_schedule_doc,
)
from erpnext_extensions.asset_usage_depreciation.services.accounting_amounts import to_depr_amount
from erpnext_extensions.asset_usage_depreciation.services.ads_amount_normalize import (
	authoritative_depreciable_total,
	normalize_full_schedule_amounts,
)
from erpnext_extensions.asset_usage_depreciation.services.ads_replace import replace_asset_depr_schedule
from erpnext_extensions.asset_usage_depreciation.services.depr_reset_rebuild import (
	_snapshot_asset,
	_validate_success_gates,
	_write_audit,
	analyze_asset,
)
from erpnext_extensions.asset_usage_depreciation.services.locks import lock_ads, lock_asset


def timed_repair(asset_name: str) -> dict:
	out: dict = {"asset": asset_name}
	t0 = time.perf_counter()
	t = t0
	plan = analyze_asset(asset_name)
	out["analyze_s"] = round(time.perf_counter() - t, 3)
	out["status_plan"] = plan.get("status")
	out["je_count"] = plan.get("je_count")
	out["ads_rows"] = plan.get("rows")
	out["frac"] = plan.get("fractional_rows")
	if plan.get("status") != "READY":
		out["skipped"] = True
		out["reason"] = plan.get("reason")
		out["total_s"] = round(time.perf_counter() - t0, 3)
		return out

	gl = frappe.db.sql(
		"""
		select count(*) from `tabGL Entry`
		where against_voucher_type='Asset' and against_voucher=%s
		  and ifnull(is_cancelled,0)=0
		""",
		(asset_name,),
	)[0][0]
	out["gl_open"] = int(gl)

	frappe.flags.asset_depr_reset_in_progress = True
	try:
		lock_asset(asset_name)
		asset = frappe.get_doc("Asset", asset_name)

		t = time.perf_counter()
		cancelled = []
		for je_name in plan["target_jes"]:
			je = frappe.get_doc("Journal Entry", je_name)
			if je.docstatus != 1:
				continue
			je.flags.ignore_permissions = True
			je.cancel()
			cancelled.append(je_name)
		out["cancel_s"] = round(time.perf_counter() - t, 3)
		out["jes_cancelled"] = len(cancelled)
		if cancelled:
			out["sec_per_je"] = round(out["cancel_s"] / len(cancelled), 3)

		t = time.perf_counter()
		expected_vad = to_depr_amount(
			flt(asset.net_purchase_amount) - flt(asset.opening_accumulated_depreciation)
		)
		asset.reload()
		for fb in asset.get("finance_books") or []:
			fb.db_set("value_after_depreciation", expected_vad)
			fb.db_set(
				"total_number_of_booked_depreciations",
				cint(asset.opening_number_of_booked_depreciations),
			)
		asset.db_set("value_after_depreciation", expected_vad)
		asset.reload()
		asset.set_status()
		asset.set_total_booked_depreciations()
		out["vad_s"] = round(time.perf_counter() - t, 3)

		t = time.perf_counter()
		ads = get_asset_depr_schedule_doc(asset_name, "Active", None)
		if not ads and asset.get("finance_books"):
			ads = get_asset_depr_schedule_doc(asset_name, "Active", asset.finance_books[0].finance_book)
		lock_ads(ads.name)
		ads = frappe.get_doc("Asset Depreciation Schedule", ads.name)
		for row in ads.get("depreciation_schedule") or []:
			if row.journal_entry:
				row.db_set("journal_entry", None)
		asset.reload()
		fb = asset.finance_books[0]
		fb.value_after_depreciation = expected_vad
		temp = frappe.copy_doc(ads)
		temp.flags.skip_iran_ads_normalize = True
		temp.create_depreciation_schedule(fb)
		rows = []
		for r in temp.get("depreciation_schedule") or []:
			rows.append(
				{
					"schedule_date": r.schedule_date,
					"depreciation_amount": r.depreciation_amount,
					"accumulated_depreciation_amount": r.accumulated_depreciation_amount,
					"journal_entry": None,
					"shift": getattr(r, "shift", None),
				}
			)
		total = authoritative_depreciable_total(asset, fb)
		normalize_full_schedule_amounts(
			rows,
			depreciable_total=total,
			opening_accumulated=flt(asset.opening_accumulated_depreciation),
		)
		out["ads_create_norm_s"] = round(time.perf_counter() - t, 3)

		t = time.perf_counter()
		notes = f"ADS rebuilt baseline timing. Cancelled {len(cancelled)} JEs."
		new_ads = replace_asset_depr_schedule(ads, rows, notes)
		out["ads_replace_s"] = round(time.perf_counter() - t, 3)

		t = time.perf_counter()
		asset.reload()
		gates = _validate_success_gates(asset, new_ads, cancelled, expected_vad, total)
		out["gates_s"] = round(time.perf_counter() - t, 3)
		out["gates_ok"] = gates.get("ok")
		out["gates_errors"] = gates.get("errors")

		t = time.perf_counter()
		_write_audit(
			{
				"asset": asset_name,
				"JEs_cancelled": cancelled,
				"old_ADS": ads.name,
				"new_ADS": new_ads.name,
				"rows": len(new_ads.depreciation_schedule),
				"due_rows": gates.get("due_rows"),
			}
		)
		frappe.db.commit()
		out["commit_s"] = round(time.perf_counter() - t, 3)
		out["status"] = "SUCCESS" if gates.get("ok") else "GATES_FAILED"
	except Exception as e:
		frappe.db.rollback()
		out["status"] = "FAILED"
		out["error"] = str(e)[:500]
		frappe.log_error(title=f"baseline failed {asset_name}")
	finally:
		frappe.flags.asset_depr_reset_in_progress = False
	out["total_s"] = round(time.perf_counter() - t0, 3)
	return out


def run(assets: str | None = None) -> list[dict]:
	"""assets: optional JSON list of [label, name] pairs."""
	pairs = [
		("LIGHT", "2067"),
		("MEDIUM", "2669"),
		("HEAVY", "3616"),
		("VERY_HEAVY", "2854"),
	]
	if assets:
		pairs = json.loads(assets) if isinstance(assets, str) else assets
	results = []
	for label, a in pairs:
		print(f"=== START {label} {a} ===", flush=True)
		r = timed_repair(a)
		r["class"] = label
		results.append(r)
		print(json.dumps(r, default=str), flush=True)
	frappe.cache.set_value("depr_baseline_timing", results)
	print("BASELINE_DONE", flush=True)
	return results
