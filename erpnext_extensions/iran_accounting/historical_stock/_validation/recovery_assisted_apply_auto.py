# Copyright (c) 2026 — Apply AUTO-promoted assisted recovery READY rows
from __future__ import annotations

import json


def run(*, max_n=20, dry_run=0):
	from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.engine import (
		run_assisted_campaign,
	)
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.apply import (
		apply_wrong_rate_root,
	)
	from erpnext_extensions.iran_accounting.stock_posting_order.repair import apply_repairs
	from frappe.utils import flt
	import frappe

	campaign = run_assisted_campaign(threshold=0.95)
	ready = list(campaign.get("ready_rows") or [])
	# Prefer roots (not dependents): sort by dependency depth proxy — no patient_zero wait
	def is_root(r):
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		return not pz_v or pz_v == r.get("voucher")

	ready.sort(key=lambda r: (0 if is_root(r) else 1, r.get("voucher") or ""))
	applied, failed, skipped = [], [], []
	for row in ready[: int(max_n)]:
		# Already-matching rates (transfer outs with incoming_rate=0 but valuation OK)
		# must not loop forever as AUTO — apply is idempotent and scanner still flags SVD residue.
		exp = flt(row.get("expected") or row.get("proposed_rate"))
		obs = flt(row.get("observed") or row.get("current_rate") or row.get("rate"))
		if exp and abs(exp - obs) <= max(1.0, abs(exp) * 1e-9):
			skipped.append(
				{
					"voucher": row.get("voucher"),
					"item": row.get("item"),
					"reason": "observed_matches_expected — SVD/transfer residue, not a rate write",
					"expected": exp,
					"observed": obs,
				}
			)
			continue
		if int(dry_run):
			applied.append({"voucher": row.get("voucher"), "item": row.get("item"), "dry_run": True, "expected": row.get("expected") or row.get("proposed_rate")})
			continue
		# Posting-order unique-inbound promotions carry moves — use PO apply.
		if str(row.get("topic") or "") == "POSTING_ORDER" and row.get("moves"):
			res = apply_repairs([row], dry_run=False)
			blocked = res.get("blocked") or []
			applied_rows = res.get("applied") or []
			entry = {
				"voucher": row.get("outbound_document") or row.get("voucher"),
				"item": row.get("item"),
				"ok": bool(applied_rows) and not blocked,
				"after_rate": None,
				"expected": None,
				"reason": (blocked[0].get("error") if blocked else None) or res.get("reason"),
				"kind": "POSTING_ORDER",
			}
		else:
			res = apply_wrong_rate_root(row, dry_run=False)
			entry = {
				"voucher": row.get("voucher"),
				"item": row.get("item"),
				"ok": bool(res.get("ok")),
				"after_rate": res.get("after_rate"),
				"expected": res.get("expected") or row.get("expected") or row.get("proposed_rate"),
				"reason": res.get("reason") or res.get("error") or ((res.get("out") or {}).get("reason")),
				"kind": "WRONG_RATE",
			}
		if entry["ok"]:
			frappe.db.commit()
			applied.append(entry)
		else:
			failed.append(entry)
	out = {
		"n_auto_ready": campaign.get("n_auto_ready"),
		"by_bucket": campaign.get("by_bucket"),
		"n_attempted": len(applied) + len(failed),
		"n_repaired": len(applied),
		"n_failed": len(failed),
		"n_skipped_matched": len(skipped),
		"applied": applied,
		"failed": failed[:15],
		"skipped": skipped[:15],
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:8000])
	return out
