from __future__ import annotations
import json
from collections import Counter

def run(*, sample=8):
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import (
		find_negative_intervals,
		classify_interval,
		propose_outbound_after_inbound,
	)
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import fetch_ledger_rows, _annotate, _identity_key
	from collections import defaultdict
	import frappe

	scan = run_full_history_scan(company="اسپاد فارمد دارو")
	later = [r for r in (scan.get("rows") or []) if (r.get("optimizer_status") or r.get("status")) == "LATER_INBOUND_UNRELATED"]
	reasons = Counter()
	samples = []
	for r in later[: int(sample)]:
		# re-fetch identity and classify
		item, wh, batch = r.get("item"), r.get("warehouse"), r.get("batch")
		rows = frappe.db.sql(
			"""
			SELECT name, item_code, warehouse, batch_no, voucher_type, voucher_no, actual_qty,
			       posting_datetime, posting_date, creation, modified, serial_and_batch_bundle,
			       company
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			ORDER BY posting_datetime, creation
			""",
			(item, wh),
			as_dict=True,
		)
		# cheap: just report inbound vt and times
		in_v, out_v = r.get("inbound_document"), r.get("outbound_document")
		in_vt = frappe.db.get_value("Stock Ledger Entry", {"voucher_no": in_v, "is_cancelled": 0}, "voucher_type")
		samples.append({
			"out": out_v,
			"in": in_v,
			"in_vt": in_vt,
			"item": item,
			"cur_out": r.get("current_outbound_time"),
			"cur_in": r.get("current_inbound_time"),
			"conf": r.get("confidence"),
			"reason": r.get("dependency_reason"),
			"moves": r.get("moves"),
			"min_before": r.get("min_qty_before"),
			"min_after": r.get("min_qty_after"),
		})
		reasons[f"vt={in_vt}|conf={r.get('confidence')}|moves={bool(r.get('moves'))}"] += 1
	# all later inbound types
	all_vt = Counter()
	for r in later:
		in_v = r.get("inbound_document")
		vt = frappe.db.get_value("Stock Ledger Entry", {"voucher_no": in_v, "is_cancelled": 0}, "voucher_type") if in_v else None
		all_vt[str(vt)] += 1
	out = {"n": len(later), "by_inbound_vt": dict(all_vt), "reason_buckets": dict(reasons), "samples": samples}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:8000])
	return out
