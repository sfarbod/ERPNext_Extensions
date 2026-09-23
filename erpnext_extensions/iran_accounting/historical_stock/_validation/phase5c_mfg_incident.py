# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 5C — reconstruct Manufacture n=10 incident on 30300042 Quarantine."""

from __future__ import annotations

import json

COMPANY = "اسپاد فارمد دارو"
ITEM = "30300042"
WH = "انبار Quarantine محصول نیمه ساخته اسپاد"
# From Phase 5B mfg canary n=10 selected_roots — first damage voucher in audit.
ROOT = "MAT-STE-2026-27825"
WAVE = [
	"MAT-STE-2026-27531",
	"MAT-STE-2026-27634",
	"MAT-STE-2026-27732",
	"MAT-STE-2026-27764",
	"MAT-STE-2026-27794",
	"MAT-STE-2026-27803",
	"MAT-STE-2026-27825",
	"MAT-STE-2026-27835",
	"MAT-STE-2026-27853",
	"MAT-STE-2026-27857",
]


def run():
	import frappe
	from frappe.utils import flt
	from erpnext_extensions.iran_accounting.historical_stock.manufacture import preview_manufacture_voucher
	from erpnext_extensions.iran_accounting.stock_posting_order.replay import sle_poison_reason, replay_series
	from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D

	out = {"item": ITEM, "warehouse": WH, "wave": WAVE, "root": ROOT}

	# Which wave vouchers touch this identity?
	touching = []
	for vn in WAVE:
		n = frappe.db.sql(
			"""
			SELECT COUNT(*) FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND item_code=%s AND warehouse=%s AND is_cancelled=0
			""",
			(vn, ITEM, WH),
		)[0][0]
		if n:
			touching.append(vn)
	out["wave_touching_identity"] = touching

	# Pre/current SE detail for ROOT
	se_rows = frappe.db.sql(
		"""
		SELECT name, idx, item_code, qty, basic_rate, amount, valuation_rate,
		       is_finished_item, secondary_item_type, s_warehouse, t_warehouse, batch_no
		FROM `tabStock Entry Detail` WHERE parent=%s ORDER BY idx
		""",
		ROOT,
		as_dict=True,
	)
	out["root_se_detail"] = se_rows
	preview = preview_manufacture_voucher(ROOT)
	out["root_preview"] = {
		"needs_repair": preview.get("needs_repair"),
		"confidence": preview.get("confidence"),
		"status": preview.get("status"),
		"expected_target_rate": preview.get("expected_target_rate"),
		"current_target_rate": preview.get("current_target_rate"),
		"input_health": preview.get("input_health"),
		"matched_but_corrupt": preview.get("matched_but_corrupt"),
		"zero_rm": preview.get("zero_rm_present"),
		"changed_n": len(preview.get("changed_rows") or []),
	}

	# SLE chain around ROOT for this identity
	chain = frappe.db.sql(
		"""
		SELECT name, voucher_no, actual_qty, incoming_rate, outgoing_rate, valuation_rate,
		       stock_value_difference, stock_value, qty_after_transaction, posting_datetime, creation
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND posting_datetime BETWEEN
		      DATE_SUB((SELECT posting_datetime FROM `tabStock Ledger Entry`
		                WHERE voucher_no=%s AND item_code=%s AND warehouse=%s AND is_cancelled=0
		                ORDER BY posting_datetime LIMIT 1), INTERVAL 2 DAY)
		  AND DATE_ADD((SELECT posting_datetime FROM `tabStock Ledger Entry`
		                WHERE voucher_no=%s AND item_code=%s AND warehouse=%s AND is_cancelled=0
		                ORDER BY posting_datetime DESC LIMIT 1), INTERVAL 5 DAY)
		ORDER BY posting_datetime, creation
		LIMIT 40
		""",
		(ITEM, WH, ROOT, ITEM, WH, ROOT, ITEM, WH),
		as_dict=True,
	)
	# Annotate poison + sign issues
	annotated = []
	for r in chain:
		poison = sle_poison_reason(r)
		qty = flt(r.actual_qty)
		svd = flt(r.stock_value_difference)
		sign_issue = (qty < 0 and svd > 0.0001) or (qty > 0 and svd < -0.0001)
		annotated.append(
			{
				"voucher": r.voucher_no,
				"sle": r.name,
				"qty": qty,
				"in_rate": flt(r.incoming_rate),
				"out_rate": flt(r.outgoing_rate),
				"val_rate": flt(r.valuation_rate),
				"svd": svd,
				"stock_value": flt(r.stock_value),
				"qty_after": flt(r.qty_after_transaction),
				"dt": str(r.posting_datetime),
				"poison": poison,
				"sign_inverted_svd": sign_issue,
			}
		)
	out["chain_annotated"] = annotated

	# Simulate what replay_from_patient_zero does from ROOT posting date with CURRENT sles
	root_dt = frappe.db.sql(
		"""
		SELECT posting_datetime FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime LIMIT 1
		""",
		(ROOT, ITEM, WH),
	)[0][0]
	prev = frappe.db.sql(
		"""
		SELECT qty_after_transaction, stock_value, valuation_rate, voucher_no, stock_value_difference, actual_qty
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0 AND posting_datetime < %s
		ORDER BY posting_datetime DESC, creation DESC LIMIT 1
		""",
		(ITEM, WH, root_dt),
		as_dict=True,
	)
	out["replay_boundary"] = {
		"from_dt": str(root_dt),
		"prev": prev[0] if prev else None,
	}

	# Full identity series: detect if replay_series from opening=prev can produce neg val_rate
	all_rows = frappe.db.sql(
		"""
		SELECT name, voucher_no, actual_qty, incoming_rate, outgoing_rate, valuation_rate,
		       stock_value, stock_value_difference, qty_after_transaction, posting_datetime, creation
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation
		""",
		(ITEM, WH),
		as_dict=True,
	)
	# Find index of ROOT
	root_idx = next((i for i, r in enumerate(all_rows) if r.voucher_no == ROOT), None)
	out["root_idx_in_identity"] = root_idx

	# Simulate OLD buggy path: take stored SVD for manufacture inbound, but if outbound
	# had inverted SVD already in DB before heal, replay_series outgoing uses running MA
	# which goes negative when opening qty is negative.
	if root_idx is not None and root_idx > 0:
		opening_qty = flt(all_rows[root_idx - 1].qty_after_transaction)
		opening_value = flt(all_rows[root_idx - 1].stock_value)
	else:
		opening_qty = opening_value = 0

	# Window from root onward as replay_from_patient_zero would see
	window = all_rows[root_idx:] if root_idx is not None else all_rows
	# Also simulate with PURPOSE attach for manufacture SVD preservation
	for r in window:
		pur = frappe.db.get_value("Stock Entry", r.voucher_no, "purpose") if r.voucher_no else None
		r["purpose"] = pur

	series = replay_series(window, opening_qty=opening_qty, opening_value=opening_value)
	first_neg = None
	neg_n = 0
	for i, step in enumerate(series):
		vr = flt(step["valuation_rate"])
		if vr < -0.0001:
			neg_n += 1
			if first_neg is None:
				first_neg = {
					"i": i,
					"voucher": step.get("voucher_no"),
					"val_rate": vr,
					"qty_after": flt(step["qty_after_transaction"]),
					"stock_value": flt(step["stock_value"]),
					"svd": flt(step["stock_value_difference"]),
					"opening_qty_at_root": opening_qty,
					"opening_value_at_root": opening_value,
				}
	out["replay_sim"] = {
		"opening_qty": opening_qty,
		"opening_value": opening_value,
		"rows": len(series),
		"neg_valuation_steps": neg_n,
		"first_neg": first_neg,
		"final": series[-1] if series else None,
	}

	# Hypothesis checks
	out["answers"] = {
		"opening_qty_before_root_negative": opening_qty < 0,
		"opening_value_before_root": opening_value,
		"root_is_first_wave_touch": touching[0] == ROOT if touching else None,
		"earlier_wave_vouchers_same_identity": [v for v in WAVE if v in touching and v < ROOT],
	}

	# Version history for ROOT SE rates around Phase 5B apply time
	versions = frappe.db.sql(
		"""
		SELECT creation, LEFT(data, 500) AS data_head
		FROM `tabVersion`
		WHERE ref_doctype='Stock Entry' AND docname=%s
		ORDER BY creation DESC LIMIT 5
		""",
		ROOT,
		as_dict=True,
	)
	out["versions"] = [{"creation": str(v.creation), "head": v.data_head} for v in versions]

	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
