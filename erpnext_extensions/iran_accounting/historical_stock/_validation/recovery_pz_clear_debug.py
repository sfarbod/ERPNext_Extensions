# Copyright (c) 2026 — Debug why WAITING_PZ not clearing for known healthy PZ
from __future__ import annotations

import json

COMPANY = "اسپاد فارمد دارو"
PZ = "MAT-STE-2026-31064"


def run():
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import (
		_rate_patient_cleared,
		_patient_rate_healthy,
		_voucher_type_of,
		_sle_rates_for_voucher_item,
		attach_plan,
	)

	scan = scan_wrong_rates(company=COMPANY, limit=5000)
	rows = scan.get("rows") or []
	deps = []
	for r in rows:
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		if pz_v != PZ:
			continue
		if str(r.get("planner_status") or "") != "WAITING_PATIENT_ZERO":
			continue
		cache = {"rows_by_voucher": {x.get("voucher"): x for x in rows if x.get("voucher")}}
		item = r.get("item")
		wh = r.get("warehouse")
		sles = _sle_rates_for_voucher_item(PZ, item, wh)
		sles_any = _sle_rates_for_voucher_item(PZ, item, None)
		deps.append(
			{
				"voucher": r.get("voucher"),
				"item": item,
				"wh": wh,
				"conf": r.get("confidence"),
				"expected": r.get("expected") or r.get("proposed_rate"),
				"vt": _voucher_type_of(PZ),
				"healthy": _patient_rate_healthy(PZ, cache, row=r),
				"cleared": _rate_patient_cleared(PZ, cache, row=r),
				"pz_in_scan": PZ in cache["rows_by_voucher"],
				"pz_row_ps": (cache["rows_by_voucher"].get(PZ) or {}).get("planner_status"),
				"n_sle_wh": len(sles or []),
				"n_sle_any": len(sles_any or []),
				"sle_any_sample": [
					{"ir": s.get("incoming_rate"), "vr": s.get("valuation_rate"), "qty": s.get("actual_qty")}
					for s in (sles_any or [])[:3]
				],
			}
		)
		if len(deps) >= 8:
			break
	out = {"n_waiting_for_pz": len(deps), "sample": deps}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
