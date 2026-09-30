# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase-3: root-compress WR TECHNICAL_TOOL_GAP → capability families (read-first)."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import frappe
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.historical_stock.kpi_buckets import wrong_rate_bucket
from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
from erpnext_extensions.iran_accounting.historical_stock.i4_repair import scan_i4_leftover
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.categories import (
	classify_lane,
)
from erpnext_extensions.iran_accounting.historical_stock._validation.phase2_dev_waves_0930 import (
	_gates,
)
from erpnext_extensions.iran_accounting.historical_stock._validation.phase2_wr_waves_0930 import (
	_canary_36934,
)

OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/"
	"private/files/hr_correction_20260930"
)
COMPANY = "اسپاد فارمد دارو"

FAMILIES = (
	"PRODUCT_REJECT",
	"VR_SCRAP",
	"STAGE_CO_PRODUCT",
	"BY_PRODUCT",
	"AFG",
	"STAGE_EQUIVALENT",
	"MANUFACTURE_VALUED_SOURCE",
	"SABB",
	"CROSS_TIME",
	"TRANSFER_PROPAGATION",
	"POISONED_OPENING",
	"FAILED_RIV_VALUATION_INTEGRITY",
	"ZERO_RATE_LOST_VALUATION",
	"UNPROMOTED_LIKELY",
	"AMBIGUOUS_SOURCES",
	"OTHER",
)


def _dump(name: str, payload: dict) -> None:
	OUT.mkdir(parents=True, exist_ok=True)
	(OUT / name).write_text(
		json.dumps(payload, indent=2, default=str, ensure_ascii=False), encoding="utf-8"
	)


def _voucher_iran_profile(voucher: str) -> dict:
	if not voucher:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT item_code, IFNULL(is_finished_item,0) is_fg, IFNULL(is_scrap_item,0) is_scrap,
		       IFNULL(secondary_item_type,'') sec, IFNULL(valuation_type,'') vt,
		       IFNULL(custom_output_class,'') oclass,
		       IFNULL(custom_physical_conversion,0) phys,
		       IFNULL(custom_output_equivalent_factor,0) factor
		FROM `tabStock Entry Detail` WHERE parent=%s
		""",
		voucher,
		as_dict=True,
	)
	se = frappe.db.sql(
		"""
		SELECT purpose, work_order, job_card,
		       IFNULL(custom_manufacturing_costing_contract_version,'') stamp
		FROM `tabStock Entry` WHERE name=%s
		""",
		voucher,
		as_dict=True,
	)
	meta = se[0] if se else {}
	fg_items = {r.item_code for r in rows if cint(r.is_fg)}
	flags = set()
	for r in rows:
		oc = r.oclass or ""
		sec = r.sec or ""
		vt = r.vt or ""
		if "REJECT" in oc or (
			(cint(r.is_scrap) or sec == "Scrap") and vt == "Valuation Rate" and r.item_code in fg_items
		):
			flags.add("PRODUCT_REJECT")
		if (cint(r.is_scrap) or sec == "Scrap") and vt == "Valuation Rate" and r.item_code not in fg_items:
			flags.add("VR_SCRAP")
		if sec == "By-Product" or oc == "CO_PRODUCT" and "REJECT" not in oc:
			if sec == "By-Product":
				flags.add("BY_PRODUCT")
			else:
				flags.add("STAGE_CO_PRODUCT")
		if sec == "Co-Product":
			flags.add("STAGE_CO_PRODUCT")
		if sec == "Additional Finished Good":
			flags.add("AFG")
		if flt(r.phys) > 0 or flt(r.factor) > 0:
			flags.add("STAGE_EQUIVALENT")
		if oc == "COMPONENT_SCRAP" or (sec == "Scrap" and vt != "Valuation Rate"):
			flags.add("COMPONENT_SCRAP")
	return {
		"purpose": meta.get("purpose"),
		"work_order": meta.get("work_order"),
		"job_card": meta.get("job_card"),
		"stamp": meta.get("stamp"),
		"flags": sorted(flags),
		"fg_n": len(fg_items),
		"detail_n": len(rows),
	}


def _family_for_row(row: dict, profile: dict) -> str:
	conf = str(row.get("confidence") or "").upper()
	src = str(row.get("source_of_truth") or row.get("expected_source") or row.get("rate_source") or "")
	reason = str(row.get("reason") or row.get("message") or row.get("wrong_reason") or "")
	purpose = str(row.get("purpose") or profile.get("purpose") or "")
	flags = set(profile.get("flags") or [])
	lane = str(row.get("manual_lane") or "")
	tool_code = str(row.get("tool_gap_code") or row.get("wrong_reason") or "")

	if tool_code in flags or tool_code in FAMILIES:
		if tool_code == "IRAN_PRODUCT_REJECT" or "PRODUCT_REJECT" in tool_code:
			return "PRODUCT_REJECT"
		if tool_code == "IRAN_VR_SCRAP_REVIEW" or "VR_SCRAP" in tool_code:
			return "VR_SCRAP"
		if "STAGE_CO" in tool_code or "COPRODUCT" in tool_code:
			return "STAGE_CO_PRODUCT"

	if "PRODUCT_REJECT" in flags:
		return "PRODUCT_REJECT"
	if "BY_PRODUCT" in flags:
		return "BY_PRODUCT"
	if "AFG" in flags:
		return "AFG"
	if "STAGE_CO_PRODUCT" in flags:
		return "STAGE_CO_PRODUCT"
	if "STAGE_EQUIVALENT" in flags and purpose == "Manufacture":
		return "STAGE_EQUIVALENT"
	if "VR_SCRAP" in flags:
		return "VR_SCRAP"
	if "batch_inward_sabb" in src or "sabb" in src.lower():
		return "SABB"
	if "CROSS_TIME" in reason.upper() or "cross_time" in src:
		return "CROSS_TIME"
	if purpose in ("Material Transfer", "Material Transfer for Manufacture", "Send to Subcontractor"):
		return "TRANSFER_PROPAGATION"
	if "poison" in reason.lower() or "opening" in reason.lower():
		return "POISONED_OPENING"
	if conf == "LIKELY":
		return "UNPROMOTED_LIKELY"
	if conf == "AMBIGUOUS" or "sources disagree" in reason.lower():
		return "AMBIGUOUS_SOURCES"
	if purpose == "Manufacture" and "manufacture" in src.lower():
		return "MANUFACTURE_VALUED_SOURCE"
	if lane == "TOOL_LIMIT" or "TOOL" in reason.upper():
		return "OTHER"
	return "OTHER"


_CAPABILITY = {
	"PRODUCT_REJECT": {
		"owner": "scrap_costing.allocate_scrap_absorbed_cost / MAIN_PRODUCT_REJECT",
		"why_blocked": "Historical rows excluded from generic SVD; need absorbed FG-rate native replay",
		"deterministic": True,
		"required": "historical contract adoption + classify + absorbed-cost RIV",
	},
	"VR_SCRAP": {
		"owner": "scrap_costing.apply_component_scrap_issued_rates (component) or finance-excluded VR",
		"why_blocked": "Valuation Rate scrap may be Core auto-default or explicit; not generic SVD",
		"deterministic": "partial",
		"required": "prove Core auto vs explicit; component scrap = same-voucher issued rate",
	},
	"STAGE_CO_PRODUCT": {
		"owner": "manufacture_stage_costing.allocate_stage_output_cost",
		"why_blocked": "Multi-output stage pool; generic single-FG residual wrong",
		"deterministic": True,
		"required": "historical stamp/origin + clear failed VR + stage allocate + RIV",
	},
	"BY_PRODUCT": {
		"owner": "allocate_stage_output_cost (CO_PRODUCT bucket)",
		"why_blocked": "Same as stage co-product; 36934 canary path",
		"deterministic": True,
		"required": "generalize 36934 historical adoption",
	},
	"AFG": {
		"owner": "allocate_stage_output_cost (CO_PRODUCT)",
		"why_blocked": "Additional Finished Good is stage participant",
		"deterministic": True,
		"required": "stage adoption path",
	},
	"STAGE_EQUIVALENT": {
		"owner": "allocate_stage_output_cost equivalent-unit",
		"why_blocked": "Needs physical/factor/stamp; not stock-UOM equality",
		"deterministic": True,
		"required": "prove factors from WO/JC/BOM or SED customs",
	},
	"MANUFACTURE_VALUED_SOURCE": {
		"owner": "manufacture_native SVD residual (single FG)",
		"why_blocked": "Upstream source SLE poison or multi-output gate",
		"deterministic": "partial",
		"required": "upstream source repair or class-specific path",
	},
	"SABB": {
		"owner": "transfer_valuation.batch_inward_sabb_rate",
		"why_blocked": "SABB/batch provenance incomplete or circular",
		"deterministic": "partial",
		"required": "SABB chain repair / MTFM provenance",
	},
	"CROSS_TIME": {
		"owner": "next_downtime.cross_time",
		"why_blocked": "Independent production flows must not be paired",
		"deterministic": True,
		"required": "scanner reclass INDEPENDENT_PRODUCTION_FLOWS (no mutate)",
	},
	"TRANSFER_PROPAGATION": {
		"owner": "transfer_convergence / transfer_valuation",
		"why_blocked": "Downstream of unrepaired source rate",
		"deterministic": "partial",
		"required": "upstream root then transfer replay",
	},
	"POISONED_OPENING": {
		"owner": "i4_repair / opening provenance",
		"why_blocked": "No authoritative opening valuation for auto-clear",
		"deterministic": False,
		"required": "opening authority analyzer; else MANUAL_BUSINESS or tool gap",
	},
	"FAILED_RIV_VALUATION_INTEGRITY": {
		"owner": "failed_riv + riv_preflight",
		"why_blocked": "RIV refused; must not blind-retry",
		"deterministic": "analyzer",
		"required": "FAILED_RIV_CAUSAL_ANALYZER → upstream WR/I4 fix",
	},
	"ZERO_RATE_LOST_VALUATION": {
		"owner": "zero_rate / manufacture_native",
		"why_blocked": "Lost rate without reconstructable source",
		"deterministic": "partial",
		"required": "link to WR/manufacture roots",
	},
	"UNPROMOTED_LIKELY": {
		"owner": "wrong_rate classifier promotion",
		"why_blocked": "LIKELY preview only; Repair Selected requires EXACT",
		"deterministic": True,
		"required": "promote when Iran-native evidence is EXACT",
	},
	"AMBIGUOUS_SOURCES": {
		"owner": "expected rate reconstruction",
		"why_blocked": "Reconstruction sources disagree",
		"deterministic": False,
		"required": "resolve authority order or MANUAL_BUSINESS",
	},
	"OTHER": {
		"owner": "unknown",
		"why_blocked": "Unclassified scanner residue",
		"deterministic": False,
		"required": "inspect samples and rebucket",
	},
}


def build_wr_toolgap_matrix(*, limit: int = 3000) -> dict:
	scan = scan_wrong_rates(company=COMPANY, limit=limit)
	rows = [r for r in (scan.get("rows") or []) if wrong_rate_bucket(r) == "tool_gap"]
	by_family = defaultdict(list)
	profiles = {}
	for r in rows:
		v = r.get("voucher") or r.get("voucher_no")
		if v and v not in profiles:
			profiles[v] = _voucher_iran_profile(v)
		fam = _family_for_row(r, profiles.get(v) or {})
		by_family[fam].append(r)

	matrix = {}
	for fam in FAMILIES:
		flist = by_family.get(fam) or []
		if not flist and fam not in by_family:
			continue
		vouchers = {r.get("voucher") or r.get("voucher_no") for r in flist}
		items = {r.get("item") or r.get("item_code") for r in flist}
		# causal: earliest unique item+warehouse among self-PZ
		seen = set()
		roots = []
		for r in sorted(flist, key=lambda x: str(x.get("posting_datetime") or x.get("voucher") or "")):
			pz = r.get("patient_zero")
			pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
			if pz_v and pz_v != (r.get("voucher") or r.get("voucher_no")):
				continue
			key = (r.get("item") or r.get("item_code"), r.get("warehouse"))
			if key in seen:
				continue
			seen.add(key)
			roots.append(
				{
					"voucher": r.get("voucher") or r.get("voucher_no"),
					"item": key[0],
					"warehouse": key[1],
					"purpose": r.get("purpose"),
					"confidence": r.get("confidence"),
					"source": r.get("source_of_truth") or r.get("expected_source"),
				}
			)
		cap = _CAPABILITY.get(fam, _CAPABILITY["OTHER"])
		matrix[fam] = {
			"raw_findings": len(flist),
			"unique_vouchers": len(vouchers - {None, ""}),
			"unique_items": len(items - {None, ""}),
			"causal_roots": len(roots),
			"sample_roots": roots[:12],
			"why_blocked": cap["why_blocked"],
			"iran_owner": cap["owner"],
			"deterministic": cap["deterministic"],
			"required_implementation": cap["required"],
		}

	out = {
		"raw_tool_gap": len(rows),
		"by_kpi": (scan.get("by_kpi_bucket") or {}),
		"family_raw_sum": sum(m["raw_findings"] for m in matrix.values()),
		"matrix": matrix,
		"gates": _gates(),
		"canary_36934": _canary_36934(),
	}
	_dump("phase3_toolgap_matrix.json", out)
	return {
		"raw_tool_gap": out["raw_tool_gap"],
		"family_raw_sum": out["family_raw_sum"],
		"families": {k: {kk: vv for kk, vv in v.items() if kk != "sample_roots"} for k, v in matrix.items()},
		"top_families": sorted(
			((k, v["raw_findings"], v["causal_roots"]) for k, v in matrix.items()),
			key=lambda x: -x[1],
		)[:12],
	}


def analyze_i4_blockers() -> dict:
	scan = scan_i4_leftover(
		company=COMPANY, from_date="2026-03-21", to_date=str(frappe.utils.nowdate()), limit=5000
	)
	rows = scan.get("rows") or []
	waiting = [r for r in rows if "WAITING" in str(r.get("status") or r.get("planner_status") or "")]
	manualish = [r for r in rows if str(r.get("status") or "") == "MANUAL"]
	# reclass
	waiting_roots = {}
	poison_roots = {}
	dust = []
	for r in waiting:
		pz = r.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		root = pz_v or r.get("voucher")
		key = (r.get("item") or r.get("item_code"), r.get("warehouse"), root)
		waiting_roots[key] = {
			"item": key[0],
			"warehouse": key[1],
			"patient_zero": root,
			"voucher": r.get("voucher"),
			"reason": str(r.get("reason") or r.get("message") or "")[:200],
		}
	for r in manualish:
		reason = str(r.get("reason") or r.get("message") or "")
		if "PRECISION_DUST" in reason.upper():
			dust.append(r)
			continue
		key = (r.get("item") or r.get("item_code"), r.get("warehouse"))
		poison_roots[key] = {
			"item": key[0],
			"warehouse": key[1],
			"voucher": r.get("voucher"),
			"reason": reason[:220],
			"status": r.get("status"),
		}

	# enrich poison with earliest SLE
	poison_detail = []
	for key, info in poison_roots.items():
		item, wh = key
		earliest = frappe.db.sql(
			"""
			SELECT name, posting_datetime, voucher_no, voucher_type,
			       ROUND(actual_qty,6) aq, ROUND(qty_after_transaction,6) q,
			       ROUND(incoming_rate,2) ir, ROUND(outgoing_rate,2) ogr,
			       ROUND(valuation_rate,2) vr, ROUND(stock_value,2) sv,
			       ROUND(stock_value_difference,2) svd
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			ORDER BY posting_datetime ASC, creation ASC
			LIMIT 3
			""",
			(item, wh),
			as_dict=True,
		)
		terminal = frappe.db.sql(
			"""
			SELECT name, posting_datetime, voucher_no,
			       ROUND(qty_after_transaction,6) q, ROUND(stock_value,2) sv,
			       ROUND(valuation_rate,2) vr
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			ORDER BY posting_datetime DESC, creation DESC
			LIMIT 1
			""",
			(item, wh),
			as_dict=True,
		)
		poison_detail.append(
			{
				**info,
				"earliest_sles": earliest,
				"terminal": terminal[0] if terminal else None,
				"repairability": _poison_repairability(info, earliest),
			}
		)

	# manufacturing dependency graph for waiting
	mfg_dep = []
	for info in waiting_roots.values():
		pz = info.get("patient_zero")
		prof = _voucher_iran_profile(pz) if pz else {}
		cls = "WAITING_ON_WR_ROOT"
		if prof.get("purpose") == "Manufacture":
			if "PRODUCT_REJECT" in (prof.get("flags") or []):
				cls = "NATIVE_MANUFACTURE_REPAIRABLE"
			elif any(f in (prof.get("flags") or []) for f in ("BY_PRODUCT", "STAGE_CO_PRODUCT", "AFG")):
				cls = "NATIVE_MANUFACTURE_REPAIRABLE"
			else:
				cls = "WAITING_ON_WR_ROOT"
		elif prof.get("purpose") and "Transfer" in str(prof.get("purpose")):
			cls = "WAITING_ON_TRANSFER_ROOT"
		if "poison" in (info.get("reason") or "").lower():
			cls = "TECHNICAL_TOOL_GAP"
		mfg_dep.append({**info, "pz_profile": prof, "class": cls})

	by_cls = Counter(x["class"] for x in mfg_dep)
	out = {
		"i4_raw": len(rows),
		"waiting_n": len(waiting),
		"manual_scanner_n": len(manualish),
		"dust_n": len(dust),
		"independent_waiting_roots": len(waiting_roots),
		"independent_poison_roots": len(poison_roots),
		"mfg_dependency_by_class": dict(by_cls),
		"mfg_dependency": mfg_dep,
		"poison_detail": poison_detail,
		"dust_sample": [
			{
				"item": r.get("item") or r.get("item_code"),
				"warehouse": r.get("warehouse"),
				"voucher": r.get("voucher"),
				"reason": str(r.get("reason") or "")[:160],
			}
			for r in dust
		],
	}
	_dump("phase3_i4_blockers.json", out)
	return {
		"i4_raw": out["i4_raw"],
		"waiting_n": out["waiting_n"],
		"independent_waiting_roots": out["independent_waiting_roots"],
		"independent_poison_roots": out["independent_poison_roots"],
		"dust_n": out["dust_n"],
		"mfg_dependency_by_class": out["mfg_dependency_by_class"],
		"poison_repairability": Counter(p["repairability"] for p in poison_detail),
	}


def _poison_repairability(info: dict, earliest: list) -> str:
	reason = (info.get("reason") or "").upper()
	if "PRECISION_DUST" in reason:
		return "LEGITIMATE"
	if not earliest:
		return "MANUAL_BUSINESS_EVIDENCE_REQUIRED"
	e0 = earliest[0]
	# opening with qty and value both zeroish but later leftover → lost rate
	if abs(flt(e0.q)) < 1e-9 and abs(flt(e0.sv)) > 1:
		return "TECHNICAL_TOOL_GAP"
	if abs(flt(e0.ir)) < 1e-9 and abs(flt(e0.aq)) > 1e-9 and abs(flt(e0.svd)) < 1:
		return "TECHNICAL_TOOL_GAP"
	if "POISON" in reason or "NOT AUTO" in reason or "OPENING" in reason:
		return "TECHNICAL_TOOL_GAP"
	return "TECHNICAL_TOOL_GAP"


def dry_iran_native(voucher: str) -> dict:
	"""In-memory Iran manufacture contract on a submitted voucher (no persist)."""
	from erpnext_extensions.iran_accounting.manufacture_stage_costing import uses_v533_contract
	from erpnext_extensions.iran_accounting.scrap_costing import (
		apply_iran_manufacture_output_contract,
	)

	doc = frappe.get_doc("Stock Entry", voucher)
	before = {
		r.name: {
			"item": r.item_code,
			"br": flt(r.basic_rate),
			"amt": flt(r.basic_amount),
			"class": r.custom_output_class,
			"vt": r.valuation_type,
			"sec": r.secondary_item_type,
			"fg": cint(r.is_finished_item),
			"qty": flt(r.qty),
		}
		for r in doc.items
	}
	doc._iran_historical_stage_repair = True
	# Ensure contract visible for uses_v533 / historical repair path
	if not doc.get("custom_manufacturing_costing_contract_version"):
		from erpnext_extensions.iran_accounting.scrap_costing import (
			MANUFACTURE_COSTING_CONTRACT_VERSION,
		)

		doc.set("custom_manufacturing_costing_contract_version", MANUFACTURE_COSTING_CONTRACT_VERSION)
	applied = apply_iran_manufacture_output_contract(doc)
	deltas = []
	outputs = []
	for r in doc.items:
		b = before[r.name]
		if abs(flt(r.basic_rate) - b["br"]) > 1 or abs(flt(r.basic_amount) - b["amt"]) > 1:
			deltas.append(
				{
					"item": r.item_code,
					"before_br": b["br"],
					"after_br": flt(r.basic_rate),
					"class": r.custom_output_class,
					"fg": cint(r.is_finished_item),
					"sec": r.secondary_item_type,
				}
			)
		if (
			cint(r.is_finished_item)
			or (r.custom_output_class or "")
			in ("MAIN_PRODUCT_REJECT", "COMPONENT_SCRAP", "CO_PRODUCT", "MAIN_FG")
			or r.secondary_item_type
		):
			outputs.append(
				{
					"item": r.item_code,
					"br": flt(r.basic_rate),
					"qty": flt(r.qty),
					"class": r.custom_output_class,
					"sec": r.secondary_item_type,
					"vt": r.valuation_type,
					"fg": cint(r.is_finished_item),
					"before_br": b["br"],
				}
			)
	return {
		"voucher": voucher,
		"uses_v533_before_flag": uses_v533_contract(frappe.get_doc("Stock Entry", voucher)),
		"applied": bool(applied),
		"delta_n": len(deltas),
		"deltas": deltas[:20],
		"outputs": outputs,
		"already_stable": bool(applied) and len(deltas) == 0,
	}


def run() -> dict:
	wr = build_wr_toolgap_matrix()
	i4 = analyze_i4_blockers()
	out = {"wr_matrix": wr, "i4_blockers": i4, "gates": _gates(), "canary": _canary_36934()}
	_dump("phase3_baseline_analysis.json", out)
	return out
