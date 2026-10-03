# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase-4: Iran Accounting compatibility audit + LMA/WR/scrap/transfer evidence."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import frappe
from frappe.utils import flt

OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/"
	"private/files/hr_correction_20260930"
)
COMPANY = "اسپاد فارمد دارو"


def _dump(name: str, payload: dict) -> None:
	OUT.mkdir(parents=True, exist_ok=True)
	(OUT / name).write_text(
		json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
	)


def _sle_around(item: str, warehouse: str, voucher: str, window: int = 4) -> dict:
	all_rows = frappe.db.sql(
		"""
		SELECT name, posting_datetime, voucher_no, voucher_type,
		       ROUND(actual_qty,6) aq, ROUND(qty_after_transaction,6) q,
		       ROUND(stock_value,2) sv, ROUND(valuation_rate,2) vr,
		       ROUND(incoming_rate,2) ir, ROUND(outgoing_rate,2) ogr,
		       ROUND(stock_value_difference,2) svd
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime ASC, creation ASC
		""",
		(item, warehouse),
		as_dict=True,
	)
	idx = next((i for i, r in enumerate(all_rows) if r.voucher_no == voucher), None)
	tip = all_rows[-1] if all_rows else None
	around = []
	if idx is not None:
		around = all_rows[max(0, idx - window) : idx + window + 1]
	# tip rate0 / leftover pattern at patient voucher
	pz = [r for r in all_rows if r.voucher_no == voucher]
	return {
		"n_sles": len(all_rows),
		"patient_idx": idx,
		"patient_sles": pz,
		"around": around,
		"tip": tip,
		"tip_healthy_valued": bool(
			tip and flt(tip.q) > 0 and flt(tip.sv) > 1 and flt(tip.vr) > 1
		),
		"tip_zero_layer": bool(tip and flt(tip.q) > 0 and abs(flt(tip.sv)) <= 1 and abs(flt(tip.vr)) < 1),
		"tip_rate0_valued": bool(
			tip and flt(tip.q) > 0 and flt(tip.sv) > 1 and abs(flt(tip.vr)) < 1
		),
		"midchain_rate0_valued_still": any(
			flt(r.q) > 0 and flt(r.sv) > 1 and abs(flt(r.vr)) < 1 for r in pz
		),
	}


def analyze_leftover_ma_identity(item: str, warehouse: str, voucher: str) -> dict:
	from erpnext_extensions.iran_accounting.domain.stock_ledger_deterministic import (
		irr_avg_rate_from_balance,
	)
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
		classify_leftover_ma_identity,
		find_leftover_ma_patient_zeros,
	)

	chain = _sle_around(item, warehouse, voucher)
	pz_live = find_leftover_ma_patient_zeros(item, warehouse)
	cls = classify_leftover_ma_identity(item, warehouse)
	# Would deterministic Iran formula fix tip if applied?
	tip = chain.get("tip")
	expected_tip_vr = None
	if tip and flt(tip.q) > 0 and flt(tip.sv) > 1:
		expected_tip_vr = irr_avg_rate_from_balance(flt(tip.sv), flt(tip.q), "IRR")
	# Preview: does current tip already match Iran avg?
	tip_matches_iran = (
		tip
		and expected_tip_vr is not None
		and abs(flt(tip.vr) - expected_tip_vr) <= 1
	)
	# Patient row pattern
	patient_pattern = None
	for r in chain.get("patient_sles") or []:
		if flt(r.aq) > 0 and abs(flt(r.ir)) < 1 and abs(flt(r.svd)) <= 1 and flt(r.q) > 0 and flt(r.sv) > 1:
			patient_pattern = "ZERO_INBOUND_ON_VALUED_POSITION"
			break
	verdict = "TECHNICAL_TOOL_GAP"
	if tip_matches_iran and not chain.get("tip_rate0_valued") and not pz_live:
		if chain.get("midchain_rate0_valued_still"):
			verdict = "HISTORICAL_ONLY"
		else:
			verdict = "CURRENT_IRAN_NATIVE_HANDLED"
	elif tip_matches_iran and pz_live:
		# tip healthy but mid-chain poison still present for RIV from start
		verdict = "NATIVE_REPLAY_EXPECTED" if not chain.get("tip_rate0_valued") else "ACTIONABLE_CAUSAL_ROOT"
	elif chain.get("tip_rate0_valued"):
		verdict = "ACTIONABLE_CAUSAL_ROOT"
	elif chain.get("tip_zero_layer"):
		verdict = "LEGITIMATE_HISTORICAL"
	elif not tip_matches_iran and flt(tip.q if tip else 0) > 0:
		verdict = "ACTIONABLE_CAUSAL_ROOT"

	# Refine: if tip healthy and only midchain stamp needed → CURRENT Iran RIV + leftover hook
	if tip_matches_iran and not chain.get("tip_rate0_valued"):
		if pz_live or chain.get("midchain_rate0_valued_still"):
			verdict = "CURRENT_IRAN_NATIVE_HANDLED"
		else:
			verdict = "CURRENT_IRAN_NATIVE_HANDLED"

	return {
		"item": item,
		"warehouse": warehouse,
		"voucher": voucher,
		"classifier": cls,
		"live_patient_zeros": [
			{"voucher": r.voucher_no, "q": r.qty_after_transaction, "sv": r.stock_value, "vr": r.valuation_rate}
			for r in pz_live
		],
		"patient_pattern": patient_pattern,
		"tip": tip,
		"expected_tip_vr": expected_tip_vr,
		"tip_matches_iran_avg": tip_matches_iran,
		"chain": {
			k: chain[k]
			for k in (
				"n_sles",
				"patient_idx",
				"tip_healthy_valued",
				"tip_rate0_valued",
				"tip_zero_layer",
				"midchain_rate0_valued_still",
				"around",
				"patient_sles",
			)
		},
		"verdict": verdict,
		"iran_owner": "domain.stock_ledger_deterministic.apply_irr_deterministic_sle_valuation + leftover_ma RIV hook",
		"historical_repair_responsibility": (
			"NO_DIRECT_MUTATION — native RIV/repost owns avg-rate stamp"
			if verdict == "CURRENT_IRAN_NATIVE_HANDLED"
			else "bridge/stamp only if native RIV leaves tip poisoned"
		),
	}


def analyze_both_lma() -> dict:
	# Resolve warehouses from dashboard findings
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import scan_leftover_ma

	scan = scan_leftover_ma(company=COMPANY, limit=50)
	targets = []
	for r in scan.get("rows") or []:
		item = r.get("item_code") or r.get("item")
		if item in ("13100023", "17000001"):
			targets.append(
				{
					"item": item,
					"warehouse": r.get("warehouse"),
					"voucher": r.get("voucher") or r.get("voucher_no") or r.get("root_voucher"),
					"status": r.get("status") or r.get("leftover_ma_status"),
				}
			)
	# Fallback known pairs
	if not any(t["item"] == "13100023" for t in targets):
		targets.append(
			{
				"item": "13100023",
				"warehouse": "انبار approved اقلام بسته بندی اولیه اسپاد",
				"voucher": "MAT-STE-2026-35344",
			}
		)
	if not any(t["item"] == "17000001" for t in targets):
		targets.append(
			{
				"item": "17000001",
				"warehouse": "انبار approved مواد اولیه اسپاد",
				"voucher": "MAT-STE-2026-25582",
			}
		)
	out = {"scan_count": scan.get("count"), "identities": []}
	for t in targets:
		if not t.get("warehouse") or not t.get("voucher"):
			continue
		out["identities"].append(
			analyze_leftover_ma_identity(t["item"], t["warehouse"], t["voucher"])
		)
	_dump("phase4_leftover_ma_audit.json", out)
	return {
		"scan_count": out["scan_count"],
		"verdicts": {i["item"]: i["verdict"] for i in out["identities"]},
		"identities": [
			{
				"item": i["item"],
				"voucher": i["voucher"],
				"verdict": i["verdict"],
				"tip_matches_iran_avg": i["tip_matches_iran_avg"],
				"live_pz_n": len(i["live_patient_zeros"]),
				"patient_pattern": i["patient_pattern"],
			}
			for i in out["identities"]
		],
	}


def canary_iran_avg() -> dict:
	"""16100066 / 16100226 tip vs Iran avg formula."""
	from erpnext_extensions.iran_accounting.domain.stock_ledger_deterministic import (
		irr_avg_rate_from_balance,
	)

	out = {}
	for item in ("16100066", "16100226"):
		rows = frappe.db.sql(
			"""
			SELECT warehouse, voucher_no, ROUND(qty_after_transaction,6) q,
			       ROUND(stock_value,2) sv, ROUND(valuation_rate,2) vr,
			       ROUND(incoming_rate,2) ir, ROUND(actual_qty,6) aq
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND is_cancelled=0
			ORDER BY posting_datetime DESC, creation DESC
			LIMIT 5
			""",
			(item,),
			as_dict=True,
		)
		# tip per warehouse for primary
		tips = frappe.db.sql(
			"""
			SELECT t.warehouse, ROUND(t.qty_after_transaction,6) q, ROUND(t.stock_value,2) sv,
			       ROUND(t.valuation_rate,2) vr, t.voucher_no
			FROM `tabStock Ledger Entry` t
			INNER JOIN (
			  SELECT warehouse, MAX(CONCAT(DATE_FORMAT(posting_datetime,'%%Y-%%m-%%d %%H:%%i:%%s'), '#', LPAD(creation,30,'0'))) mx
			  FROM `tabStock Ledger Entry` WHERE item_code=%s AND is_cancelled=0
			  GROUP BY warehouse
			) x ON x.warehouse=t.warehouse
			 AND CONCAT(DATE_FORMAT(t.posting_datetime,'%%Y-%%m-%%d %%H:%%i:%%s'), '#', LPAD(t.creation,30,'0'))=x.mx
			WHERE t.item_code=%s AND t.is_cancelled=0
			""",
			(item, item),
			as_dict=True,
		)
		checks = []
		for t in tips:
			exp = irr_avg_rate_from_balance(flt(t.sv), flt(t.q), "IRR") if flt(t.q) > 0 else 0
			ok = True
			if flt(t.q) > 0 and flt(t.sv) > 1:
				ok = abs(flt(t.vr) - exp) <= 1 and flt(t.vr) > 0
			elif flt(t.q) > 0 and abs(flt(t.sv)) <= 1:
				# post-depletion zero layer — vr may be 0
				ok = abs(flt(t.vr)) < 1 or abs(flt(t.sv)) <= 1
			checks.append({**t, "expected_vr": exp, "ok": ok})
		out[item] = {
			"recent": rows,
			"tips": checks,
			"all_ok": all(c["ok"] for c in checks) if checks else False,
		}
	# 36934
	c36934 = frappe.db.sql(
		"""
		SELECT item_code, qty, ROUND(basic_rate,2) br, custom_output_class, secondary_item_type,
		       valuation_type, is_finished_item
		FROM `tabStock Entry Detail`
		WHERE parent='MAT-STE-2026-36934' AND item_code IN ('30500009','20100008')
		ORDER BY idx
		""",
		as_dict=True,
	)
	out["36934"] = {"rows": c36934, "qty_ok": any(flt(r.qty) == 1 and r.item_code == "30500009" for r in c36934)}
	_dump("phase4_canary_iran_avg.json", out)
	return {
		"16100066_ok": out["16100066"]["all_ok"],
		"16100226_ok": out["16100226"]["all_ok"],
		"36934_qty_ok": out["36934"]["qty_ok"],
		"36934_rows": out["36934"]["rows"],
	}


def scrap_manual_evidence() -> dict:
	"""Exhaust BOM/WO/JC evidence for Iran MANUAL assertion vouchers."""
	from erpnext_extensions.iran_accounting.historical_stock.manufacture_native_historical import (
		analyze_iran_native_historical,
	)
	from erpnext_extensions.iran_accounting.historical_stock._validation.phase3_iran_native_waves_0930 import (
		inventory_iran_native_roots,
	)

	# Prefer last inventory dump
	path = OUT / "phase3_iran_native_inventory.json"
	manual_vouchers = []
	if path.exists():
		data = json.loads(path.read_text(encoding="utf-8"))
		manual_vouchers = [m["voucher"] for m in (data.get("manual_sample") or []) if m.get("voucher")]
	if not manual_vouchers:
		inv = inventory_iran_native_roots()
		# re-read
		if path.exists():
			data = json.loads(path.read_text(encoding="utf-8"))
			manual_vouchers = [m["voucher"] for m in (data.get("manual_sample") or []) if m.get("voucher")]

	results = []
	for v in manual_vouchers:
		ev = analyze_iran_native_historical(v)
		se = frappe.db.get_value(
			"Stock Entry",
			v,
			["purpose", "work_order", "job_card", "bom_no", "custom_manufacturing_costing_contract_version"],
			as_dict=True,
		)
		details = frappe.db.sql(
			"""
			SELECT idx, item_code, qty, ROUND(basic_rate,2) br, secondary_item_type, is_scrap_item,
			       is_finished_item, valuation_type, custom_output_class, s_warehouse, t_warehouse, batch_no, uom
			FROM `tabStock Entry Detail` WHERE parent=%s ORDER BY idx
			""",
			(v,),
			as_dict=True,
		)
		wo = se.work_order if se else None
		jc = se.job_card if se else None
		bom = se.bom_no if se else None
		if not bom and wo:
			bom = frappe.db.get_value("Work Order", wo, "bom_no")
		bom_scrap = []
		bom_items = []
		if bom:
			bom_scrap = frappe.db.sql(
				"""
				SELECT item_code, stock_qty, rate, IFNULL(parentfield,'') pf
				FROM `tabBOM Scrap Item` WHERE parent=%s
				""",
				(bom,),
				as_dict=True,
			) or []
			bom_items = frappe.db.sql(
				"""
				SELECT item_code, qty, rate
				FROM `tabBOM Item` WHERE parent=%s
				""",
				(bom,),
				as_dict=True,
			) or []
		jc_scrap = []
		if jc and frappe.db.exists("DocType", "Job Card Scrap Item"):
			jc_scrap = frappe.db.sql(
				"""
				SELECT item_code, stock_qty FROM `tabJob Card Scrap Item` WHERE parent=%s
				""",
				(jc,),
				as_dict=True,
			) or []
		# classify scrap rows
		scrap_rows = [
			d
			for d in details
			if cint(d.is_scrap_item)
			or str(d.secondary_item_type or "").lower() in ("scrap",)
			or "SCRAP" in str(d.custom_output_class or "").upper()
			or "REJECT" in str(d.custom_output_class or "").upper()
		]
		consumed = {d.item_code for d in details if d.s_warehouse and not d.t_warehouse}
		fg = [d for d in details if cint(d.is_finished_item)]
		classifications = []
		for s in scrap_rows:
			code = s.item_code
			main = code  # family
			in_bom_scrap = any(b.item_code == code for b in bom_scrap)
			in_jc_scrap = any(j.item_code == code for j in jc_scrap)
			same_as_fg = any(f.item_code == code for f in fg)
			consumed_match = code in consumed or any(
				c.startswith(code[:6]) for c in consumed
			)  # weak family hint only for reporting
			cls = "TRULY_AMBIGUOUS"
			if same_as_fg or (fg and any(f.item_code == code for f in fg)):
				cls = "MAIN_PRODUCT_REJECT"
			elif code in consumed:
				cls = "COMPONENT_SCRAP"
			elif in_bom_scrap or in_jc_scrap:
				cls = "COMPONENT_SCRAP" if code in consumed or consumed_match else "OTHER_SECONDARY_OUTPUT"
			elif str(s.secondary_item_type) in ("By-Product", "Co-Product"):
				cls = "BY_PRODUCT" if s.secondary_item_type == "By-Product" else "CO_PRODUCT"
			elif str(s.custom_output_class or ""):
				cls = str(s.custom_output_class)
			classifications.append(
				{
					"idx": s.idx,
					"item": code,
					"sec": s.secondary_item_type,
					"oclass": s.custom_output_class,
					"in_bom_scrap": in_bom_scrap,
					"in_jc_scrap": in_jc_scrap,
					"same_as_fg": same_as_fg,
					"in_consumed": code in consumed,
					"proposed_class": cls,
				}
			)
		results.append(
			{
				"voucher": v,
				"analyze": {
					"classification": ev.get("classification"),
					"reason": ev.get("reason"),
					"families": ev.get("families"),
				},
				"wo": wo,
				"jc": jc,
				"bom": bom,
				"bom_scrap_n": len(bom_scrap),
				"jc_scrap_n": len(jc_scrap),
				"scrap_classifications": classifications,
				"ambiguous": [c for c in classifications if c["proposed_class"] == "TRULY_AMBIGUOUS"],
			}
		)
	# pattern group
	patterns = Counter()
	for r in results:
		key = (
			r["analyze"].get("reason") or "",
			tuple(sorted({c["proposed_class"] for c in r["scrap_classifications"]})),
		)
		patterns[str(key)] += 1
	out = {"vouchers": results, "pattern_counts": dict(patterns)}
	_dump("phase4_scrap_evidence.json", out)
	return {
		"n": len(results),
		"ambiguous_vouchers": [r["voucher"] for r in results if r["ambiguous"]],
		"provable": [
			{
				"voucher": r["voucher"],
				"classes": [c["proposed_class"] for c in r["scrap_classifications"]],
				"reason": r["analyze"].get("reason"),
			}
			for r in results
			if not r["ambiguous"]
		],
		"sample": [
			{
				"voucher": r["voucher"],
				"reason": (r["analyze"].get("reason") or "")[:160],
				"scrap": r["scrap_classifications"],
			}
			for r in results
		],
	}


def cint(v):
	try:
		return int(v or 0)
	except (TypeError, ValueError):
		return 0


def transfer_roots_prediction() -> dict:
	"""Predict native replay ownership for TRANSFER_PROPAGATION tool-gap roots."""
	from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import wrong_rate_bucket
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock._validation.phase3_toolgap_matrix_0930 import (
		_family_for_row,
		_voucher_iran_profile,
	)

	scan = scan_wrong_rates(company=COMPANY, limit=3000)
	rows = [r for r in (scan.get("rows") or []) if wrong_rate_bucket(r) == "tool_gap"]
	profiles = {}
	transfer = []
	for r in rows:
		v = r.get("voucher") or r.get("voucher_no")
		if v and v not in profiles:
			profiles[v] = _voucher_iran_profile(v)
		if _family_for_row(r, profiles.get(v) or {}) == "TRANSFER_PROPAGATION":
			transfer.append(r)
	# causal roots: self-PZ or earliest per item+wh
	roots = {}
	for r in sorted(transfer, key=lambda x: str(x.get("posting_datetime") or "")):
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		v = r.get("voucher") or r.get("voucher_no")
		if pz_v and pz_v != v:
			continue
		key = (r.get("item") or r.get("item_code"), r.get("warehouse"))
		if key in roots:
			continue
		purpose = (profiles.get(v) or {}).get("purpose") or r.get("purpose")
		# Transfer valuation authority is source warehouse SLE under ERPNext
		verdict = "NATIVE_REPLAY_EXPECTED"
		if purpose and "Manufacture" in str(purpose):
			verdict = "WAITING_UPSTREAM"
		roots[key] = {
			"item": key[0],
			"warehouse": key[1],
			"voucher": v,
			"purpose": purpose,
			"expected": r.get("expected_rate") or r.get("proposed_rate"),
			"actual": r.get("actual_rate") or r.get("basic_rate"),
			"verdict": verdict,
			"authority": "ERPNext transfer uses source-side valuation_rate; Iran does not invent transfer rates",
		}
	by = Counter(r["verdict"] for r in roots.values())
	out = {
		"raw_transfer": len(transfer),
		"unique_vouchers": len({r.get("voucher") or r.get("voucher_no") for r in transfer}),
		"causal_roots_n": len(roots),
		"by_verdict": dict(by),
		"roots": list(roots.values()),
	}
	_dump("phase4_transfer_prediction.json", out)
	return {
		"raw_transfer": out["raw_transfer"],
		"unique_vouchers": out["unique_vouchers"],
		"causal_roots_n": out["causal_roots_n"],
		"by_verdict": out["by_verdict"],
		"sample_roots": out["roots"][:15],
	}


def failed_riv_one_root() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv_causal import (
		analyze_failed_riv_actionable,
	)

	fr = analyze_failed_riv_actionable(company=COMPANY, limit=500)
	roots = fr.get("causal_roots") or []
	_dump("phase4_failed_riv.json", fr)
	return {
		"actionable_n": fr.get("actionable_n"),
		"by_status": fr.get("by_status"),
		"causal_roots_n": fr.get("causal_roots_n"),
		"roots": roots,
	}


def compress_zero_pz() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock._validation.phase2_reconcile_0930 import (
		classify_tip_rate0,
		compress_pz,
	)

	tip = classify_tip_rate0()
	pz = compress_pz()
	_dump("phase4_zero_pz.json", {"tip": tip, "pz": pz})
	return {"tip": tip, "pz": pz}


def iran_code_paths() -> dict:
	"""Static inventory of current Iran valuation ownership."""
	from erpnext_extensions import __version__

	return {
		"version": __version__,
		"paths": {
			"moving_average_avg_rate": "domain.stock_ledger_deterministic.apply_irr_deterministic_sle_valuation",
			"repost_avg_rate": "domain.repost_determinism → apply_irr_deterministic_sle_valuation",
			"sle_hooks": "stock_ledger.validate/before_insert/after_insert → ledger_rounding",
			"leftover_ma_riv_hook": "historical_stock.leftover_ma.on_repost_item_valuation_update",
			"manufacture_contract": "scrap_costing.apply_iran_manufacture_output_contract",
			"stage_allocator": "manufacture_stage_costing.allocate_stage_output_cost",
			"product_reject": "scrap_costing.allocate_scrap_absorbed_cost + pre-core bridge",
			"component_scrap": "scrap_costing.apply_component_scrap_issued_rates",
			"irr_residual_sa": "domain.manufacture_irr_residual / domain.irr_residual_classification → 621301",
			"round_off_exclusion": "validation.stock_adj_round_off_rows / zero_value_transfer",
			"riv_integrity": "domain.riv_valuation_guard",
			"stock_entry_hooks": "stock_entry.before_validate/validate/before_submit/on_submit",
		},
		"relevant_commits": [
			"d816d67 Phase-3 manufacture_native + causal preflight",
			"e8b7a92 bridge late product rejects",
			"158e394 price stage-equivalent by-products before core",
			"af785ce route proven manufacture IRR residuals to stock adjustment",
			"440ba60 equal-rate By-Product + independent WO CROSS_TIME",
		],
	}


def run_audit() -> dict:
	out = {
		"iran_paths": iran_code_paths(),
		"canaries": canary_iran_avg(),
		"leftover_ma": analyze_both_lma(),
		"scrap": scrap_manual_evidence(),
		"transfer": transfer_roots_prediction(),
		"failed_riv": failed_riv_one_root(),
		"zero_pz": compress_zero_pz(),
	}
	_dump("phase4_iran_audit.json", out)
	return {
		"canaries": out["canaries"],
		"leftover_ma": out["leftover_ma"],
		"scrap": {
			"n": out["scrap"]["n"],
			"ambiguous_vouchers": out["scrap"]["ambiguous_vouchers"],
			"provable": out["scrap"]["provable"],
			"sample": out["scrap"]["sample"],
		},
		"transfer": {
			k: out["transfer"][k]
			for k in ("raw_transfer", "unique_vouchers", "causal_roots_n", "by_verdict")
		}
		| {"sample_roots": out["transfer"]["sample_roots"]},
		"failed_riv": out["failed_riv"],
		"tip": out["zero_pz"]["tip"],
		"pz": {k: out["zero_pz"]["pz"].get(k) for k in list(out["zero_pz"]["pz"] or {})[:12]},
		"iran_paths": out["iran_paths"]["paths"],
	}
