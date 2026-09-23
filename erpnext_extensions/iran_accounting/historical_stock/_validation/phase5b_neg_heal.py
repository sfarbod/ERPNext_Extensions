# Copyright (c) 2026, ERPNext Extensions contributors
"""Attempt controlled heal of 30300042 Quarantine negatives from mfg canary."""

from __future__ import annotations

import json

ITEM = "30300042"
WH = "انبار Quarantine محصول نیمه ساخته اسپاد"
# Earliest damaged manufacture from canary wave.
ROOT = "MAT-STE-2026-27825"


def run():
	import frappe
	from frappe.utils import get_datetime
	from erpnext_extensions.iran_accounting.stock_posting_order.replay import replay_item_warehouse

	# Snapshot damage
	before = frappe.db.sql(
		"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
	)[0][0]

	# Find posting_datetime of root
	root_dt = frappe.db.get_value("Stock Entry", ROOT, "posting_date")
	root_tm = frappe.db.get_value("Stock Entry", ROOT, "posting_time")
	from_dt = get_datetime(f"{root_dt} {root_tm or '00:00:00'}")
	# Start slightly before root
	from datetime import timedelta

	from_dt = from_dt - timedelta(seconds=1)

	# Check Version for SE detail rates on ROOT
	versions = frappe.db.sql(
		"""
		SELECT creation, data FROM `tabVersion`
		WHERE ref_doctype='Stock Entry' AND docname=%s
		ORDER BY creation DESC LIMIT 5
		""",
		ROOT,
		as_dict=True,
	)
	ver_info = []
	for v in versions:
		ver_info.append({"creation": str(v.creation), "data_len": len(v.data or "")})

	# Current FG SE rates on ROOT
	fg = frappe.db.sql(
		"""
		SELECT name, item_code, qty, basic_rate, amount, valuation_rate, is_finished_item
		FROM `tabStock Entry Detail` WHERE parent=%s
		""",
		ROOT,
		as_dict=True,
	)

	# Controlled identity replay — no global RIV
	frappe.db.begin()
	try:
		rep = replay_item_warehouse(
			ITEM,
			WH,
			from_dt,
			ignore_inversion_artifacts=True,
			write_vouchers=None,  # rewrite all in window? check signature
			allow_unrelated_poison=False,
		)
		neg_mid = frappe.db.sql(
			"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE is_cancelled=0 AND valuation_rate < -0.0001"
		)[0][0]
		out = {
			"before_neg": before,
			"after_replay_neg": neg_mid,
			"replay": {
				"ok": rep.get("ok"),
				"status": rep.get("status"),
				"reason": rep.get("reason"),
				"valuation_changed": rep.get("valuation_changed"),
				"touched": (rep.get("touched_vouchers") or [])[:20],
			},
			"root_fg_rows": fg,
			"versions": ver_info,
		}
		if neg_mid == 0 and rep.get("ok"):
			frappe.db.commit()
			out["committed"] = True
		else:
			frappe.db.rollback()
			out["committed"] = False
			out["rollback_reason"] = "replay did not clear negatives or not ok"
	except Exception as exc:
		frappe.db.rollback()
		out = {"error": str(exc), "before_neg": before, "root_fg_rows": fg, "versions": ver_info}

	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
