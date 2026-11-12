# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Dev-only verification helper. Not a patch."""

from __future__ import annotations

import frappe
import erpnext_extensions


def run():
	print("version", erpnext_extensions.__version__)

	for dt in ["Desktop Icon", "Workspace", "Workspace Sidebar", "Sidebar", "Module Def"]:
		meta = frappe.get_meta(dt)
		fields = ["name"]
		for f in ("module", "label", "hidden", "icon", "title", "link_to", "bg_color"):
			if meta.has_field(f):
				fields.append(f)
		rows = frappe.get_all(
			dt,
			filters={"name": ["in", ["Costing", "بهای تمام شده"]]},
			fields=fields,
		)
		print(dt, rows)

	print(
		"schema",
		{
			"persian_period": frappe.db.has_column("Monthly FG Cost Calculation", "persian_period"),
			"over_allocated": frappe.db.has_column("Monthly FG Cost Calculation", "over_allocated"),
			"calculated_batch_share": frappe.db.has_column(
				"MFG Cost Batch Expense Allocation", "calculated_batch_share"
			),
			"ads": bool(frappe.db.exists("DocType", "MFG Cost Account Dimension Summary")),
		},
	)

	cals = frappe.get_all(
		"Monthly FG Cost Calculation",
		filters={"persian_year": 1405},
		fields=[
			"name",
			"persian_year",
			"persian_month",
			"persian_period",
			"status",
			"revision",
			"total_identified_expenses",
			"total_allocated_expenses",
			"over_allocated",
			"is_consistent",
		],
		order_by="persian_month",
	)
	print("fy1405_count", len(cals))
	for c in cals:
		print(dict(c))
		print(
			"  counts",
			{
				"bx": frappe.db.count(
					"MFG Cost Batch Expense Allocation",
					{"monthly_calculation": c.name, "revision": c.revision},
				),
				"px": frappe.db.count(
					"MFG Cost Product Expense Allocation",
					{"monthly_calculation": c.name, "revision": c.revision},
				),
				"ads": frappe.db.count(
					"MFG Cost Account Dimension Summary",
					{"monthly_calculation": c.name, "revision": c.revision},
				),
			},
		)

	settings = frappe.get_all("Finished Goods Cost Allocation Settings", fields=["name", "company"])
	print("settings", settings)
	if settings:
		print("uom_rows", frappe.db.count("FG Item Primary UOM", {"parent": settings[0].name}))
		print(
			"by_group",
			frappe.db.sql(
				"select item_group, count(*) from `tabFG Item Primary UOM` where parent=%s group by item_group",
				settings[0].name,
			),
		)
	return {"ok": True}


def backfill_and_refresh():
	"""Safe backfills for display fields without recalculating locked history amounts."""
	import frappe
	from frappe.utils import cint, flt

	# Persian period display
	updated = 0
	for row in frappe.get_all(
		"Monthly FG Cost Calculation",
		fields=["name", "persian_year", "persian_month", "persian_period"],
	):
		period = f"{cint(row.persian_year):04d}-{cint(row.persian_month):02d}"
		if row.persian_period != period:
			frappe.db.set_value(
				"Monthly FG Cost Calculation",
				row.name,
				"persian_period",
				period,
				update_modified=False,
			)
			updated += 1
	print("persian_period_updated", updated)

	# Refresh Item Group / stock UOM on configured items
	from erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config import apply_row_factors

	settings = frappe.get_all("Finished Goods Cost Allocation Settings", pluck="name")
	for sname in settings:
		doc = frappe.get_doc("Finished Goods Cost Allocation Settings", sname)
		for row in doc.item_primary_uoms or []:
			if not row.item:
				continue
			meta = frappe.db.get_value(
				"Item", row.item, ["item_name", "item_group", "stock_uom"], as_dict=True
			)
			if meta:
				row.item_name = meta.item_name
				row.item_group = meta.item_group
				row.stock_uom = meta.stock_uom
			apply_row_factors(row)
		doc.save(ignore_permissions=True)
		print("settings_refreshed", sname, "rows", len(doc.item_primary_uoms or []))

	# Backfill calculated/manual share fields on Batch×Expense from parent batch
	for cal in frappe.get_all(
		"Monthly FG Cost Calculation", fields=["name", "revision"]
	):
		batches = {
			r.batch_no: r
			for r in frappe.get_all(
				"MFG Cost Batch Allocation",
				filters={"monthly_calculation": cal.name, "revision": cal.revision},
				fields=["batch_no", "calculated_share", "manual_share", "applied_share", "is_manual_override"],
			)
		}
		if not batches:
			continue
		# Only fill when calculated_batch_share is missing/zero but applied exists
		frappe.db.sql(
			"""
			UPDATE `tabMFG Cost Batch Expense Allocation` bx
			INNER JOIN `tabMFG Cost Batch Allocation` b
			  ON b.monthly_calculation = bx.monthly_calculation
			 AND b.revision = bx.revision
			 AND b.batch_no = bx.batch_no
			SET
			  bx.calculated_batch_share = IFNULL(NULLIF(bx.calculated_batch_share, 0), b.calculated_share),
			  bx.manual_batch_share = IFNULL(bx.manual_batch_share, b.manual_share),
			  bx.is_manual_override = IFNULL(NULLIF(bx.is_manual_override, 0), b.is_manual_override)
			WHERE bx.monthly_calculation=%s AND bx.revision=%s
			""",
			(cal.name, cal.revision),
		)

		# Rebuild Account Dimension Summary for active revision
		frappe.db.sql(
			"DELETE FROM `tabMFG Cost Account Dimension Summary` WHERE monthly_calculation=%s AND revision=%s",
			(cal.name, cal.revision),
		)
		rows = frappe.db.sql(
			"""
			SELECT account, IFNULL(cost_center,'') AS cost_center, IFNULL(department,'') AS department,
			       expense_pool, MAX(pool_monthly_net_expense) AS pool_net,
			       COUNT(DISTINCT batch_no) AS batch_count,
			       SUM(applied_batch_share) AS share_sum,
			       SUM(allocated_amount) AS amount_sum,
			       MAX(currency) AS currency
			FROM `tabMFG Cost Batch Expense Allocation`
			WHERE monthly_calculation=%s AND revision=%s
			GROUP BY account, IFNULL(cost_center,''), IFNULL(department,''), expense_pool
			""",
			(cal.name, cal.revision),
			as_dict=True,
		)
		from frappe.utils import now
		ts = now()
		user = frappe.session.user
		fields = [
			"name","creation","modified","owner","modified_by","docstatus","idx",
			"monthly_calculation","revision","account","account_name","cost_center","department",
			"expense_pool","pool_monthly_net_expense","allocated_batch_count",
			"total_applied_batch_share","total_allocated_amount","allocation_status","currency",
		]
		values = []
		for idx, r in enumerate(rows, start=1):
			acct_name = frappe.db.get_value("Account", r.account, "account_name") or ""
			status = "OK"
			if abs(flt(r.share_sum) - 1.0) > 0.0001 and flt(r.pool_net) != 0:
				status = "Share variance"
			values.append([
				frappe.generate_hash(length=10), ts, ts, user, user, 0, idx,
				cal.name, cal.revision, r.account, acct_name,
				r.cost_center or None, r.department or None, r.expense_pool,
				flt(r.pool_net), cint(r.batch_count), flt(r.share_sum), flt(r.amount_sum),
				status, r.currency,
			])
		if values:
			frappe.db.bulk_insert("MFG Cost Account Dimension Summary", fields=fields, values=values)
		print("ads_built", cal.name, len(values))

	frappe.db.commit()
	return {"ok": True}


def verify_share_override(monthly_name: str = "MFGCC-2026-00001"):
	from frappe.utils import flt

	from erpnext_extensions.iran_accounting.fg_cost_allocation.engine import set_applied_batch_share

	bx = frappe.get_all(
		"MFG Cost Batch Expense Allocation",
		filters={"monthly_calculation": monthly_name, "revision": 1},
		fields=[
			"name",
			"batch_no",
			"applied_batch_share",
			"allocated_amount",
			"pool_monthly_net_expense",
			"calculated_batch_share",
		],
		limit=1,
	)
	if not bx:
		return {"ok": False, "error": "no batch expense rows"}
	sample = bx[0]
	before_total = frappe.db.get_value(
		"Monthly FG Cost Calculation", monthly_name, "total_allocated_expenses"
	)
	new_share = flt(sample.applied_batch_share) + 0.001
	r = set_applied_batch_share(
		batch_expense_allocation=sample.name,
		applied_share=new_share,
		reason="dev verification override",
	)
	# Formula check on one updated row
	updated = frappe.db.get_value(
		"MFG Cost Batch Expense Allocation",
		sample.name,
		["applied_batch_share", "allocated_amount", "pool_monthly_net_expense", "calculated_batch_share"],
		as_dict=True,
	)
	expected = flt(updated.pool_monthly_net_expense) * flt(updated.applied_batch_share)
	r2 = set_applied_batch_share(
		batch_expense_allocation=sample.name, clear=1, reason="restore after verify"
	)
	return {
		"ok": True,
		"sample": sample.name,
		"batch_no": sample.batch_no,
		"override": r,
		"formula_allocated": updated.allocated_amount,
		"formula_expected": expected,
		"calculated_preserved": updated.calculated_batch_share,
		"restored": r2,
		"before_total": before_total,
	}


def verify_add_remove_item():
	"""Add a خوراکی item if present, then remove it; do not leave permanent scope change."""
	from erpnext_extensions.iran_accounting.fg_cost_allocation.uom_config import (
		add_items_to_primary_uoms,
	)

	settings = frappe.get_all("Finished Goods Cost Allocation Settings", pluck="name")
	if not settings:
		return {"ok": False, "error": "no settings"}
	sname = settings[0]
	before = frappe.db.count("FG Item Primary UOM", {"parent": sname})
	oral = frappe.db.sql(
		"""
		SELECT i.name FROM tabItem i
		INNER JOIN `tabItem Group` ig ON ig.name = i.item_group
		WHERE i.item_group = 'خوراکی' AND IFNULL(i.disabled,0)=0
		LIMIT 1
		"""
	)
	if not oral:
		return {"ok": False, "error": "no خوراکی item found", "before": before}
	item = oral[0][0]
	already = frappe.db.exists("FG Item Primary UOM", {"parent": sname, "item": item})
	if already:
		return {"ok": True, "skipped": True, "item": item, "before": before, "note": "already configured"}
	doc = frappe.get_doc("Finished Goods Cost Allocation Settings", sname)
	default_uom = doc.default_primary_uom
	res = add_items_to_primary_uoms(sname, [item], primary_uom=default_uom)
	# remove again
	doc = frappe.get_doc("Finished Goods Cost Allocation Settings", sname)
	doc.set("item_primary_uoms", [r for r in (doc.item_primary_uoms or []) if r.item != item])
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	after = frappe.db.count("FG Item Primary UOM", {"parent": sname})
	return {"ok": True, "item": item, "add_result": res, "before": before, "after": after}
