# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.2.15 Warehouse Dependency Engine — scope analyzer + replay simulator (no warehouse-wide apply)."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime

import frappe
from frappe.utils import flt


from erpnext_extensions.iran_accounting.historical_stock.util import dump_artifact, resolve_company




def analyze_warehouse_dependencies(warehouse: str, company=None, *, limit=5000) -> dict:
	"""Detect MA / cross-batch / cross-WO / cross-WH / identity / replay / batch deps."""
	if not warehouse:
		frappe.throw("warehouse is required")
	company = resolve_company(company)
	sles = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, batch_no, voucher_type, voucher_no,
		       posting_date, posting_time, actual_qty, qty_after_transaction,
		       stock_value, stock_value_difference, valuation_rate,
		       incoming_rate, outgoing_rate
		FROM `tabStock Ledger Entry`
		WHERE warehouse=%s AND is_cancelled=0
		ORDER BY posting_date, posting_time, creation, name
		LIMIT %s
		""",
		(warehouse, int(limit)),
		as_dict=True,
	)
	by_item = defaultdict(list)
	batches = set()
	vouchers = set()
	wo_links = 0
	cross_wh_items = set()
	for sle in sles:
		by_item[sle.item_code].append(sle)
		if sle.batch_no:
			batches.add(sle.batch_no)
		vouchers.add((sle.voucher_type, sle.voucher_no))

	# Cross-warehouse: items that also post elsewhere
	items = list(by_item.keys())
	if items:
		other = frappe.db.sql(
			"""
			SELECT DISTINCT item_code, warehouse FROM `tabStock Ledger Entry`
			WHERE item_code IN %s AND warehouse != %s AND is_cancelled=0
			LIMIT 5000
			""",
			(tuple(items[:500]), warehouse),
			as_dict=True,
		)
		for r in other:
			cross_wh_items.add(r.item_code)

	# Work order linkage via Stock Entry
	if vouchers:
		ste_names = [v for t, v in vouchers if t == "Stock Entry"][:500]
		if ste_names:
			wo_links = frappe.db.sql(
				"""
				SELECT COUNT(*) FROM `tabStock Entry`
				WHERE name IN %s AND IFNULL(work_order,'')!=''
				""",
				(tuple(ste_names),),
			)[0][0]

	identities = []
	for item, chain in by_item.items():
		ma_breaks = 0
		prev_val = None
		for sle in chain:
			vr = flt(sle.valuation_rate)
			if prev_val is not None and abs(vr - prev_val) > 0.0001 and abs(flt(sle.actual_qty)) > 0.0001:
				ma_breaks += 1
			prev_val = vr
		identities.append(
			{
				"item": item,
				"sle_count": len(chain),
				"ma_breaks": ma_breaks,
				"has_batch": any(s.batch_no for s in chain),
				"cross_warehouse": item in cross_wh_items,
			}
		)

	# Smallest safe replay scope: single identity with no cross-WH
	safe_scopes = [
		i for i in identities if not i["cross_warehouse"] and i["sle_count"] <= 50 and i["ma_breaks"] <= 2
	]
	out = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"company": company,
		"warehouse": warehouse,
		"sle_count": len(sles),
		"item_count": len(by_item),
		"batch_count": len(batches),
		"voucher_count": len(vouchers),
		"work_order_linked_stes": int(wo_links),
		"cross_warehouse_items": len(cross_wh_items),
		"identities_sample": identities[:40],
		"smallest_safe_replay_scopes": safe_scopes[:20],
		"estimated_sql": sum(i["sle_count"] for i in safe_scopes[:1]) if safe_scopes else 0,
		"estimated_replay": (safe_scopes[0]["sle_count"] if safe_scopes else 0),
		"expected_kpi_improvement": "identity-scoped only — never whole warehouse without proof",
		"promotion_status": "ENGINE_SCAFFOLD",
		"message": "Never replay whole warehouse unless proven necessary",
	}
	dump_artifact("warehouse", f"warehouse_{warehouse.replace('/', '_')}.json", out)
	return out


def simulate_warehouse_replay_scope(warehouse: str, item_code: str, company=None) -> dict:
	"""Replay Scope Simulator — estimate impact without writing."""
	company = resolve_company(company)
	n = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabStock Ledger Entry`
		WHERE warehouse=%s AND item_code=%s AND is_cancelled=0
		""",
		(warehouse, item_code),
	)[0][0]
	return {
		"warehouse": warehouse,
		"item": item_code,
		"company": company,
		"estimated_replay_rows": int(n),
		"estimated_sql": int(n) + 2,
		"scope": "IDENTITY",
		"warehouse_wide": False,
		"safe": True,
		"message": "Identity-scoped simulation only",
	}


def validate_warehouse_replay_scope(warehouse: str, item_code: str) -> dict:
	"""Warehouse Replay Validator — refuse warehouse-wide without proof."""
	analysis = analyze_warehouse_dependencies(warehouse)
	item_info = next((i for i in analysis.get("identities_sample") or [] if i["item"] == item_code), None)
	ok = bool(item_info) and not item_info.get("cross_warehouse")
	return {
		"ok": ok,
		"warehouse": warehouse,
		"item": item_code,
		"cross_warehouse": bool(item_info and item_info.get("cross_warehouse")),
		"forbid_warehouse_wide": True,
		"reason": None if ok else "cross-warehouse or identity missing — refuse warehouse-wide",
	}
