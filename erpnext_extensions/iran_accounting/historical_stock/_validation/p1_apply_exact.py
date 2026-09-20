import json

import frappe


def run():
	from erpnext_extensions.iran_accounting.historical_stock.reconstruct import (
		repair_manufacture_selected,
	)

	out = []
	for v in ["MAT-STE-2026-25174", "MAT-STE-2026-25552"]:
		d = repair_manufacture_selected([{"voucher": v}], dry_run=True)
		a = repair_manufacture_selected([{"voucher": v}], dry_run=False)
		frappe.db.commit()
		sle = frappe.db.sql(
			"""
			SELECT item_code, ROUND(incoming_rate,2), ROUND(valuation_rate,2)
			FROM `tabStock Ledger Entry`
			WHERE is_cancelled=0 AND voucher_no=%s AND actual_qty>0
			""",
			v,
		)
		fg = frappe.db.sql(
			"""
			SELECT item_code, ROUND(basic_rate,2), ROUND(valuation_rate,2)
			FROM `tabStock Entry Detail`
			WHERE parent=%s AND IFNULL(is_finished_item,0)=1
			""",
			v,
		)
		out.append(
			{
				"voucher": v,
				"dry": {
					"applied": [
						{
							"status": x.get("status"),
							"confidence": x.get("confidence"),
							"sle_se_drift": x.get("sle_se_drift"),
						}
						for x in (d.get("applied") or [])
					],
					"blocked": d.get("blocked"),
				},
				"apply": {
					"applied": [
						{
							"status": x.get("status"),
							"sle_synced": x.get("sle_synced"),
							"written": x.get("written"),
							"replay_identities": x.get("replay_identities"),
						}
						for x in (a.get("applied") or [])
					],
					"blocked": a.get("blocked"),
				},
				"sle": sle,
				"fg_se": fg,
			}
		)
	print(json.dumps(out, default=str, ensure_ascii=False))
	return out
