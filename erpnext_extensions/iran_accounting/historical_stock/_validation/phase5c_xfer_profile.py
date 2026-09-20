def run():
	import frappe, json
	from collections import Counter
	from frappe.utils import flt
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.historical_stock.transfer_valuation import (
		EXACT, reconstruct_transfer_valuation, apply_transfer_reconstruction_to_row, TRANSFER_PURPOSES,
	)
	COMPANY = "اسپاد فارمد دارو"
	w = scan_wrong_rates(company=COMPANY, limit=6000)
	rows = [attach_plan(dict(r)) for r in (w.get("rows") or [])]
	buckets = Counter()
	mat_ste_exact_roots = set()
	other_exact_roots = set()
	diff_bands = Counter()
	samples = []
	for r in rows:
		if (r.get("purpose") or "") not in TRANSFER_PURPOSES:
			continue
		r = apply_transfer_reconstruction_to_row(dict(r))
		tr = r.get("transfer_reconstruction") or reconstruct_transfer_valuation(r)
		cls = (tr or {}).get("classification") or "?"
		buckets[cls] += 1
		if cls != EXACT:
			continue
		vn = str(r.get("voucher") or "")
		root = (tr or {}).get("root_voucher") or vn
		cur = flt(r.get("current_rate") or tr.get("current_rate"))
		exp = flt(tr.get("expected_rate"))
		diff = abs(cur - exp)
		band = "le1" if diff <= 1 else "le10" if diff <= 10 else "le100" if diff <= 100 else "le1000" if diff <= 1000 else "gt1000"
		diff_bands[band] += 1
		if vn.startswith("MAT-STE-") or vn.startswith("STE-"):
			mat_ste_exact_roots.add(root)
		else:
			other_exact_roots.add(root)
		if len(samples) < 8 and diff > 1 and vn.startswith("MAT-STE-"):
			samples.append({"v": vn, "item": r.get("item"), "cur": cur, "exp": exp, "diff": diff, "src": tr.get("authoritative_source")})
	out = {
		"findings_by_cls": dict(buckets),
		"mat_ste_exact_roots": len(mat_ste_exact_roots),
		"other_exact_roots": len(other_exact_roots),
		"other_sample": list(other_exact_roots)[:10],
		"diff_bands": dict(diff_bands),
		"samples": samples,
	}
	print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
	return out
