from __future__ import annotations
import json
from collections import defaultdict

def run():
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import fetch_ledger_rows
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import _annotate, _identity_key
	from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import (
		find_negative_intervals,
		classify_interval,
		propose_outbound_after_inbound,
	)

	raw = fetch_ledger_rows(company="اسپاد فارمد دارو")
	annotated = [_annotate(r) for r in raw]
	by_identity = defaultdict(list)
	by_voucher_all = defaultdict(list)
	for r in annotated:
		if r.get("canonical_batch") == "*MULTI*":
			continue
		by_identity[_identity_key(r)].append(r)
		if r.get("voucher_type") == "Stock Entry":
			by_voucher_all[r["voucher_no"]].append(r)

	targets = [
		("MAT-STE-2026-23937", "15010109"),
		("MAT-STE-2026-03423", "15010222"),
		("MAT-STE-2026-03191", "15010224"),
	]
	out = []
	for out_v, item in targets:
		# find identity series containing outbound
		series = None
		for key, rows in by_identity.items():
			if key[0] != item:
				continue
			if any(r.get("voucher_no") == out_v for r in rows):
				series = rows
				break
		if not series:
			out.append({"out": out_v, "item": item, "error": "series_not_found"})
			continue
		intervals = find_negative_intervals(series)
		hit = None
		for iv in intervals:
			if (iv.get("outbound") or {}).get("voucher_no") == out_v or getattr(iv.get("outbound"), "get", lambda k: None)("voucher_no") == out_v:
				hit = iv
				break
			ob = iv.get("outbound")
			vn = ob.get("voucher_no") if isinstance(ob, dict) else getattr(ob, "voucher_no", None)
			if vn == out_v:
				hit = iv
				break
		if not hit:
			# match by any interval with this outbound
			for iv in intervals:
				ob = iv["outbound"]
				vn = ob["voucher_no"] if isinstance(ob, dict) else ob.voucher_no
				if vn == out_v:
					hit = iv
					break
		if not hit:
			out.append({"out": out_v, "item": item, "error": "interval_not_found", "n_intervals": len(intervals)})
			continue
		classified = classify_interval(hit, series=series, by_voucher_all=by_voucher_all, by_identity=by_identity)
		prop = propose_outbound_after_inbound(dict(hit, inbound=hit.get("inbound")), series)
		# also try with later inbound from interval inbounds picking PRE
		inbounds = list(hit.get("inbounds") or [])
		pre = None
		for ib in inbounds + ([hit.get("inbound")] if hit.get("inbound") is not None else []):
			if ib is None:
				continue
			vt = ib.get("voucher_type") if isinstance(ib, dict) else getattr(ib, "voucher_type", None)
			if vt == "Purchase Receipt":
				pre = ib
				break
		prop_pre = None
		if pre is not None:
			iv2 = dict(hit)
			iv2["inbound"] = pre
			prop_pre = propose_outbound_after_inbound(iv2, series)
		out.append({
			"out": out_v,
			"item": item,
			"recovered": hit.get("recovered"),
			"classified": {k: classified.get(k) for k in ("status","optimizer_status","confidence","dependency_reason","min_qty_before","min_qty_after","moves")},
			"proposal": {k: prop.get(k) for k in ("ok","status","reason","proposed_outbound","seconds_shifted")},
			"proposal_pre": {k: (prop_pre or {}).get(k) for k in ("ok","status","reason","proposed_outbound","seconds_shifted")} if prop_pre else None,
			"inbound_pick": (hit.get("inbound") or {}).get("voucher_no") if isinstance(hit.get("inbound"), dict) else getattr(hit.get("inbound"), "voucher_no", None),
			"inbound_vt": (hit.get("inbound") or {}).get("voucher_type") if isinstance(hit.get("inbound"), dict) else getattr(hit.get("inbound"), "voucher_type", None),
			"n_inbounds": len(hit.get("inbounds") or []),
		})
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:12000])
	return out
