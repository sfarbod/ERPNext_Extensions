# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Dev-only: backup, cleanup derived Costing rows, regenerate FY1405.

Never touches GL Entry, SLE, Stock Entry, Work Order, Batch, Item, or UOM Conversion Detail.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime

import frappe
from frappe.utils import cint

from erpnext_extensions.iran_accounting.fg_cost_allocation import DETAIL_DOCTYPE_MAP

DERIVED_DOCTYPES = [
	DETAIL_DOCTYPE_MAP["product_expense"],
	DETAIL_DOCTYPE_MAP["account_dimension_summary"],
	DETAIL_DOCTYPE_MAP["reconciliation_check"],
	DETAIL_DOCTYPE_MAP["exception"],
	DETAIL_DOCTYPE_MAP["batch_expense"],
	DETAIL_DOCTYPE_MAP["gl_line"],
	DETAIL_DOCTYPE_MAP["product"],
	DETAIL_DOCTYPE_MAP["batch"],
	DETAIL_DOCTYPE_MAP["expense_pool"],
	DETAIL_DOCTYPE_MAP["revision"],
]

CONFIG_DOCTYPES = [
	"Finished Goods Cost Allocation Settings",
	"FG Item Primary UOM",
	"Expense Account Dimension Rule",
	"Monthly FG Cost Calculation",
]

SOURCE_FORBIDDEN = [
	"GL Entry",
	"Stock Ledger Entry",
	"Stock Entry",
	"Stock Entry Detail",
	"Work Order",
	"Batch",
	"Item",
	"UOM Conversion Detail",
]


def _backup_dir() -> str:
	stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
	path = f"/workspace/development/frappe-bench/sites/preserved_dev_backups/costing_{stamp}"
	os.makedirs(path, exist_ok=True)
	return path


def inventory() -> dict:
	out = {"config": {}, "derived": {}, "monthly": []}
	for dt in CONFIG_DOCTYPES:
		out["config"][dt] = frappe.db.count(dt)
	for dt in DERIVED_DOCTYPES:
		out["derived"][dt] = frappe.db.count(dt)
	out["monthly"] = frappe.get_all(
		"Monthly FG Cost Calculation",
		fields=["name", "persian_year", "persian_month", "persian_period", "revision", "status"],
		order_by="persian_year, persian_month",
	)
	out["forbidden_untouched_check"] = {dt: frappe.db.count(dt) for dt in SOURCE_FORBIDDEN}
	return out


def backup_costing_tables() -> dict:
	"""mysqldump only Costing-related tables (+ monthly headers/settings)."""
	site = frappe.local.site
	conf = frappe.get_site_config()
	db = conf.get("db_name")
	password = conf.get("db_password")
	host = conf.get("db_host") or "localhost"
	user = conf.get("db_user") or db
	path = _backup_dir()
	tables = []
	for dt in CONFIG_DOCTYPES + DERIVED_DOCTYPES + ["Desktop Icon", "Workspace", "Workspace Sidebar", "Sidebar"]:
		tables.append(f"tab{dt}")
	# unique preserve order
	seen = set()
	uniq = []
	for t in tables:
		if t not in seen:
			seen.add(t)
			uniq.append(t)
	dump_file = os.path.join(path, "costing_tables.sql")
	cmd = [
		"mysqldump",
		"-h",
		host,
		"-u",
		user,
		f"-p{password}",
		"--single-transaction",
		"--routines=0",
		"--triggers=0",
		db,
		*uniq,
	]
	with open(dump_file, "w") as fh:
		proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE, text=True)
	meta = {
		"site": site,
		"db": db,
		"path": path,
		"dump_file": dump_file,
		"tables": uniq,
		"mysqldump_returncode": proc.returncode,
		"stderr": (proc.stderr or "")[-2000:],
		"inventory_before": inventory(),
		"dump_bytes": os.path.getsize(dump_file) if os.path.exists(dump_file) else 0,
	}
	with open(os.path.join(path, "manifest.json"), "w") as fh:
		json.dump(meta, fh, ensure_ascii=False, indent=2, default=str)
	if proc.returncode != 0:
		frappe.throw(f"mysqldump failed: {proc.stderr}")
	# Verify dump non-empty and contains expected table markers
	with open(dump_file) as fh:
		head = fh.read(5000)
	meta["verified"] = "CREATE TABLE" in head or "INSERT INTO" in head
	with open(os.path.join(path, "manifest.json"), "w") as fh:
		json.dump(meta, fh, ensure_ascii=False, indent=2, default=str)
	return meta


def cleanup_derived(*, keep_monthly_headers: bool = True, keep_settings: bool = True) -> dict:
	"""Delete derived calculation rows. Preserve Settings + Monthly headers by default."""
	before = inventory()
	deleted = {}
	for dt in DERIVED_DOCTYPES:
		cnt = frappe.db.count(dt)
		frappe.db.sql(f"DELETE FROM `tab{dt}`")
		deleted[dt] = cnt
	# Reset monthly headers to Draft / revision 0 so regenerate starts clean
	if keep_monthly_headers:
		frappe.db.sql(
			"""
			UPDATE `tabMonthly FG Cost Calculation`
			SET revision=0, status='Draft', is_consistent=0,
			    total_identified_expenses=0, total_allocated_expenses=0,
			    unallocated_amount=0, excluded_gl_amount=0,
			    total_monthly_normalized_qty=0, gl_entry_count=0,
			    batch_count=0, product_count=0, pool_count=0,
			    exception_count=0, missing_uom_count=0, share_variance=0,
			    shares_are_provisional=0, eligible_source_row_count=0,
			    over_allocated=0, reconciliation_summary=NULL, exceptions_json=NULL,
			    source_fingerprint=NULL, calculated_on=NULL, calculated_by=NULL
			"""
		)
	assert keep_settings, "Settings must be preserved"
	# Sanity: source tables unchanged counts captured in before
	frappe.db.commit()
	after = inventory()
	return {"deleted": deleted, "before": before, "after": after, "settings_preserved": keep_settings}


def delete_all_calculations(*, confirm: int = 0) -> dict:
	"""Backup-required cleanup: delete ALL calculation headers and derived rows.

	Preserves Settings, Item Primary UOMs, Expense Account Dimension Rules.
	Does not touch GL Entry / SLE / SE / WO / Batch / Item / UOM Conversion Detail.
	"""
	if not cint(confirm):
		return {
			"deleted": 0,
			"preview_only": True,
			"inventory": inventory(),
			"message": "Pass confirm=1 after verified backup to delete all calculations.",
		}
	before = inventory()
	source_before = before["forbidden_untouched_check"]
	deleted = {}
	for dt in DERIVED_DOCTYPES:
		cnt = frappe.db.count(dt)
		frappe.db.sql(f"DELETE FROM `tab{dt}`")
		deleted[dt] = cnt
	# Also delete generation-line diagnostics tied to calculations? Keep rule gen lines.
	monthly_cnt = frappe.db.count("Monthly FG Cost Calculation")
	frappe.db.sql("DELETE FROM `tabMonthly FG Cost Calculation`")
	deleted["Monthly FG Cost Calculation"] = monthly_cnt
	frappe.db.commit()
	after = inventory()
	source_after = after["forbidden_untouched_check"]
	orphans = {dt: after["derived"].get(dt, 0) for dt in after["derived"]}
	return {
		"deleted": deleted,
		"before": before,
		"after": after,
		"settings_preserved": {
			"Finished Goods Cost Allocation Settings": after["config"]["Finished Goods Cost Allocation Settings"],
			"FG Item Primary UOM": after["config"]["FG Item Primary UOM"],
			"Expense Account Dimension Rule": after["config"]["Expense Account Dimension Rule"],
		},
		"monthly_headers_remaining": after["config"]["Monthly FG Cost Calculation"],
		"orphans": orphans,
		"source_unchanged": source_before == source_after,
		"source_before": source_before,
		"source_after": source_after,
	}


def regenerate_fy1405(*, retain_overrides: int = 0) -> dict:
	from erpnext_extensions.iran_accounting.fg_cost_allocation.engine import run_calculation

	results = []
	cals = frappe.get_all(
		"Monthly FG Cost Calculation",
		filters={"persian_year": 1405},
		fields=["name", "persian_month", "persian_period"],
		order_by="persian_month",
	)
	for cal in cals:
		# ensure persian_period
		period = f"1405-{cint(cal.persian_month):02d}"
		frappe.db.set_value(
			"Monthly FG Cost Calculation", cal.name, "persian_period", period, update_modified=False
		)
		try:
			msg = run_calculation(cal.name, retain_overrides=bool(retain_overrides), action="Calculate")
			doc = frappe.get_doc("Monthly FG Cost Calculation", cal.name)
			rev = cint(doc.revision)
			uom_missing = frappe.db.count(
				"MFG Cost Product Allocation",
				{"monthly_calculation": doc.name, "revision": rev, "missing_uom_factor": 1},
			)
			px = frappe.db.count(
				"MFG Cost Product Expense Allocation",
				{"monthly_calculation": doc.name, "revision": rev},
			)
			results.append(
				{
					"name": doc.name,
					"persian_period": doc.persian_period,
					"status": doc.status,
					"revision": rev,
					"is_consistent": cint(doc.is_consistent),
					"eligible_source_row_count": cint(doc.eligible_source_row_count),
					"batch_count": cint(doc.batch_count),
					"product_count": cint(doc.product_count),
					"total_monthly_normalized_qty": float(doc.total_monthly_normalized_qty or 0),
					"missing_uom_count": cint(doc.missing_uom_count),
					"uom_exception_rows": uom_missing,
					"pool_count": cint(doc.pool_count),
					"gl_entry_count": cint(doc.gl_entry_count),
					"total_identified_expenses": float(doc.total_identified_expenses or 0),
					"total_allocated_expenses": float(doc.total_allocated_expenses or 0),
					"batch_expense_count": frappe.db.count(
						"MFG Cost Batch Expense Allocation",
						{"monthly_calculation": doc.name, "revision": rev},
					),
					"product_expense_count_active_rev": px,
					"ads_count": frappe.db.count(
						"MFG Cost Account Dimension Summary",
						{"monthly_calculation": doc.name, "revision": rev},
					),
					"share_variance": float(doc.share_variance or 0),
					"over_allocated": cint(doc.over_allocated),
					"shares_are_provisional": cint(getattr(doc, "shares_are_provisional", 0)),
					"engine": msg,
					"error": None,
				}
			)
		except Exception as e:
			frappe.db.rollback()
			results.append(
				{
					"name": cal.name,
					"persian_period": period,
					"error": str(e),
				}
			)
	# actual qty sum helper
	for r in results:
		if r.get("error"):
			continue
		aq = frappe.db.sql(
			"""
			SELECT COALESCE(SUM(actual_qty),0), COALESCE(SUM(normalized_qty),0)
			FROM `tabMFG Cost Product Allocation`
			WHERE monthly_calculation=%s AND revision=%s
			""",
			(r["name"], r["revision"]),
		)[0]
		r["actual_qty_sum"] = float(aq[0] or 0)
		r["normalized_qty_sum"] = float(aq[1] or 0)
		# consumability probe
		from erpnext_extensions.iran_accounting.fg_cost_allocation.contract import (
			FGCostAllocationNotConsumable,
			assert_consumable,
		)

		try:
			assert_consumable(r["name"])
			r["consumable"] = True
			r["consumable_blocker"] = None
		except FGCostAllocationNotConsumable as ex:
			r["consumable"] = False
			r["consumable_blocker"] = str(ex)

	path = _backup_dir()
	with open(os.path.join(path, "fy1405_regen_results.json"), "w") as fh:
		json.dump(results, fh, ensure_ascii=False, indent=2, default=str)
	return {"results": results, "report_path": path}


def full_pipeline() -> dict:
	inv0 = inventory()
	bak = backup_costing_tables()
	clean = cleanup_derived()
	regen = regenerate_fy1405(retain_overrides=0)
	inv1 = inventory()
	# Confirm source tables unchanged
	src_before = bak["inventory_before"]["forbidden_untouched_check"]
	src_after = inv1["forbidden_untouched_check"]
	return {
		"inventory_start": inv0,
		"backup": {k: bak[k] for k in ("path", "dump_file", "dump_bytes", "verified", "mysqldump_returncode")},
		"cleanup": {"deleted": clean["deleted"]},
		"regen": regen,
		"source_counts_unchanged": src_before == src_after,
		"source_before": src_before,
		"source_after": src_after,
		"version": frappe.get_attr("erpnext_extensions.__version__"),
	}
