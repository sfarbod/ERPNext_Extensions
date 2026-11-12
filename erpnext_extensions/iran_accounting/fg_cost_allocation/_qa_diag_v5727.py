"""One-shot diagnostics for QA findings. Not a production module."""

from __future__ import annotations

import json

import frappe
from frappe.utils import flt


def main():
	out = {}
	for name in ("MFGCC-2026-00007", "MFGCC-2026-00008"):
		if not frappe.db.exists("Monthly FG Cost Calculation", name):
			out[name] = {"missing": True}
			continue
		doc = frappe.get_doc("Monthly FG Cost Calculation", name)
		ex = frappe.get_all(
			"MFG Cost Exception",
			filters={"monthly_calculation": name},
			fields=[
				"name",
				"exception_type",
				"severity",
				"message",
				"item_code",
				"batch_no",
				"account",
			],
			limit_page_length=30,
		)
		fails = frappe.get_all(
			"MFG Cost Reconciliation Check",
			filters={"monthly_calculation": name, "passed": 0},
			fields=[
				"name",
				"check_code",
				"check_name",
				"expected_value",
				"actual_value",
				"variance",
				"message",
			],
			limit_page_length=30,
		)
		# product cost recon sample
		products = frappe.get_all(
			"MFG Cost Product Allocation",
			filters={"monthly_calculation": name},
			fields=[
				"name",
				"item_code",
				"material_consumption_value",
				"product_allocated_expense",
				"total_product_cost",
				"missing_uom_factor",
			],
			limit_page_length=50,
		)
		recon_ok = []
		for p in products:
			expected = flt(p.material_consumption_value) + flt(p.product_allocated_expense)
			recon_ok.append(
				{
					"item": p.item_code,
					"ok": abs(flt(p.total_product_cost) - expected) < 0.05,
					"total": flt(p.total_product_cost),
					"expected": expected,
					"missing_uom": p.missing_uom_factor,
				}
			)
		# double-count GL lines
		dup = frappe.db.sql(
			"""
			SELECT gl_entry, COUNT(*) c
			FROM `tabMFG Cost GL Line`
			WHERE monthly_calculation=%s
			GROUP BY gl_entry
			HAVING COUNT(*) > 1
			LIMIT 10
			""",
			name,
			as_dict=True,
		)
		out[name] = {
			"status": doc.status,
			"revision": doc.revision,
			"is_consistent": doc.is_consistent,
			"missing_uom_count": doc.missing_uom_count,
			"exception_count": doc.exception_count,
			"exceptions": ex,
			"recon_fails": fails,
			"product_cost_recon": {
				"all_ok": all(x["ok"] for x in recon_ok),
				"sample": recon_ok[:5],
			},
			"duplicate_gl_lines": dup,
			"totals": {
				"identified": flt(doc.total_identified_expenses),
				"allocated": flt(doc.total_allocated_expenses),
				"unallocated": flt(doc.unallocated_amount),
				"pools": doc.pool_count,
				"batches": doc.batch_count,
				"products": doc.product_count,
				"gl": doc.gl_entry_count,
			},
		}

	# orphaned revisions pointing to missing monthly
	orph_rev = frappe.db.sql(
		"""
		SELECT r.name, r.monthly_calculation, r.revision
		FROM `tabMFG Cost Calculation Revision` r
		LEFT JOIN `tabMonthly FG Cost Calculation` m ON m.name = r.monthly_calculation
		WHERE m.name IS NULL
		LIMIT 20
		""",
		as_dict=True,
	)
	settings = frappe.get_doc(
		"Finished Goods Cost Allocation Settings", "Finished Goods Cost Allocation Settings"
	)
	out["orphaned_revisions"] = orph_rev
	out["selling_price_list"] = settings.selling_price_list
	out["rules"] = frappe.db.count("Expense Account Dimension Rule")
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
