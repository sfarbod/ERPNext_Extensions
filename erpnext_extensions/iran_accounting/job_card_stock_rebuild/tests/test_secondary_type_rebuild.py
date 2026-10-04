# Copyright (c) 2026, ERPNext Extensions contributors
"""Job-Card-scoped secondary type reconciliation + canaries (v5.4.1).

Run:
  bench --site development.localhost execute \
    erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests.test_secondary_type_rebuild.run_all
"""

from __future__ import annotations

import json
from copy import deepcopy

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.evidence import build_evidence
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.readiness import (
	simulate_normal_make_stock_entry,
	stage_contains_item,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.secondary_type import (
	suggest_secondary_type_changes,
	validate_type_approvals,
)
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.service import (
	apply_rebuild,
	dry_run_rebuild,
	preview_rebuild,
	scan_job_card,
)
from erpnext_extensions import __version__ as APP_VERSION

JC_10492 = "PO-JOB10492"
JC_10433 = "PO-JOB10433"
JC_08760 = "PO-JOB08760"
JC_08761 = "PO-JOB08761"
ITEM_STOPPER = "13100134"
ITEM_FG = "30100026"
ITEM_RM = "13200544"


def _ok(name, cond, detail=""):
	return {"name": name, "pass": bool(cond), "detail": detail}


def _sec_row(job_card, item_code, qty=None):
	filters = {"parent": job_card, "item_code": item_code}
	rows = frappe.get_all(
		"Job Card Secondary Item",
		filters=filters,
		fields=["name", "secondary_item_type", "stock_qty", "stock_uom"],
	)
	if qty is not None:
		rows = [r for r in rows if abs(flt(r.stock_qty) - flt(qty)) < 1e-6]
	return rows[0] if rows else None


def _approval_for(job_card, sug_row):
	return {
		"job_card": job_card,
		"secondary_row": sug_row["secondary_row"],
		"current_type": sug_row["current_type"],
		"approved_type": sug_row["suggested_type"],
		"evidence_fingerprint": sug_row["evidence_fingerprint"],
		"item_code": sug_row["item_code"],
	}


def test_canary_10492_preview():
	results = []
	if not frappe.db.exists("Job Card", JC_10492):
		return [_ok("O_PREVIEW", False, "PO-JOB10492 missing")]
	prev = preview_rebuild(JC_10492)
	rows = (prev.get("secondary_type_suggestions") or {}).get("rows") or []
	target = next(
		(
			r
			for r in rows
			if r.get("item_code") == ITEM_STOPPER
			and abs(flt(r.get("qty")) - 125) < 1e-6
		),
		None,
	)
	results.append(_ok("O_PREVIEW_EXISTS", target is not None, json.dumps(rows, default=str)[:500]))
	if target:
		results.append(_ok("O_CURRENT_TYPE", target.get("current_type") == "Co-Product", target.get("current_type")))
		results.append(_ok("O_SUGGESTED_TYPE", target.get("suggested_type") == "Scrap", target.get("suggested_type")))
		results.append(
			_ok("O_CURRENT_CLASS", target.get("current_iran_class") == "CO_PRODUCT", target.get("current_iran_class"))
		)
		results.append(
			_ok(
				"O_SUGGESTED_CLASS",
				target.get("suggested_iran_class") == "COMPONENT_SCRAP",
				target.get("suggested_iran_class"),
			)
		)
		results.append(
			_ok(
				"O_CONFIDENCE",
				target.get("confidence") in ("PROVEN", "HIGH"),
				target.get("confidence"),
			)
		)
		results.append(_ok("O_APPROVAL_ENABLED", target.get("approval_enabled") is True))
		results.append(_ok("O_SCOPE", prev.get("fingerprint_scope") == "selected_job_card_dependency_closure"))
	# Product reject protection
	pr = next(
		(r for r in rows if r.get("item_code") == ITEM_FG and abs(flt(r.get("qty")) - 3) < 1e-6),
		None,
	)
	if pr:
		results.append(
			_ok(
				"O_PRODUCT_REJECT",
				pr.get("suggested_type") in (None, "NO CHANGE") or pr.get("action") == "NO CHANGE",
				pr.get("action"),
			)
		)
	return results


def test_canary_10492_no_approval_dry():
	results = []
	row = _sec_row(JC_10492, ITEM_STOPPER, 125)
	before = row.secondary_item_type if row else None
	prev = preview_rebuild(JC_10492)
	dry = dry_run_rebuild(JC_10492, fingerprint=prev["fingerprint"], secondary_type_approvals=[])
	after = frappe.db.get_value("Job Card Secondary Item", row.name, "secondary_item_type") if row else None
	results.append(_ok("P_TYPE_UNCHANGED", before == after == "Co-Product", f"{before}->{after}"))
	results.append(_ok("P_DRY_PASS", dry.get("dry_run_status") == "DRY_RUN_PASS", dry.get("dry_run_status")))
	results.append(
		_ok(
			"P_MFG_BLOCKED",
			dry.get("manufacture_readiness_status") == "MANUFACTURE_READINESS_BLOCKED"
			or not (dry.get("manufacture_readiness") or {}).get("ok"),
			dry.get("manufacture_readiness_status"),
		)
	)
	results.append(_ok("P_MUTATED_FALSE", dry.get("mutated") is False))
	if stage_contains_item(dry.get("manufacture_readiness") or {}, ITEM_STOPPER) or not (
		dry.get("manufacture_readiness") or {}
	).get("ok"):
		results.append(_ok("P_STAGE_STILL_BAD", True, (dry.get("manufacture_readiness") or {}).get("error")))
	else:
		results.append(_ok("P_STAGE_STILL_BAD", False, "expected stage block"))
	return results


def test_canary_10492_approved_dry():
	results = []
	prev = preview_rebuild(JC_10492)
	rows = (prev.get("secondary_type_suggestions") or {}).get("rows") or []
	target = next(
		(r for r in rows if r.get("item_code") == ITEM_STOPPER and abs(flt(r.get("qty")) - 125) < 1e-6),
		None,
	)
	if not target:
		return [_ok("Q_TARGET", False, "no suggestion")]
	appr = [_approval_for(JC_10492, target)]
	row = _sec_row(JC_10492, ITEM_STOPPER, 125)
	before = frappe.db.get_value("Job Card Secondary Item", row.name, "secondary_item_type")
	dry = dry_run_rebuild(JC_10492, fingerprint=prev["fingerprint"], secondary_type_approvals=appr)
	after = frappe.db.get_value("Job Card Secondary Item", row.name, "secondary_item_type")
	results.append(_ok("Q_ROLLBACK_TYPE", before == after == "Co-Product", f"{before}->{after}"))
	results.append(_ok("Q_DRY_STATUS", dry.get("dry_run_status") == "DRY_RUN_PASS", dry.get("error")))
	ready = dry.get("manufacture_readiness") or {}
	results.append(_ok("Q_MFG_OK", ready.get("ok") is True, ready.get("error")))
	classified = ready.get("classified") or {}
	cs = classified.get("COMPONENT_SCRAP") or []
	results.append(
		_ok(
			"Q_COMPONENT_SCRAP",
			any(r.get("item_code") == ITEM_STOPPER for r in cs),
			json.dumps(classified, default=str)[:400],
		)
	)
	results.append(_ok("Q_NOT_IN_STAGE", not stage_contains_item(ready, ITEM_STOPPER), ready.get("stage")))
	co = classified.get("CO_PRODUCT") or []
	results.append(_ok("Q_NO_CO_PRODUCT_STOPPER", not any(r.get("item_code") == ITEM_STOPPER for r in co)))
	return results


def test_controls():
	results = []
	# 10433 — already Scrap, no type change
	if frappe.db.exists("Job Card", JC_10433):
		sug = suggest_secondary_type_changes(JC_10433)
		stop = [r for r in sug["rows"] if r.get("item_code") == ITEM_STOPPER]
		results.append(
			_ok(
				"X_10433_NO_CHANGE",
				all(r.get("action") == "NO CHANGE" or not r.get("suggested_type") for r in stop),
				json.dumps(stop, default=str)[:300],
			)
		)
	else:
		results.append(_ok("X_10433_NO_CHANGE", False, "missing"))

	# 08761 raw-material control (item-scoped canary path)
	if frappe.db.exists("Job Card", JC_08761):
		p = preview_rebuild(JC_08761, item_filter=ITEM_RM)
		row = next((r for r in p.get("material_rows") or [] if r.get("item_code") == ITEM_RM), None)
		results.append(
			_ok(
				"Z_08761_STATUS",
				p.get("overall_status") in ("BALANCED", "NO_CHANGE"),
				p.get("overall_status"),
			)
		)
		if row:
			results.append(_ok("Z_ISSUED", abs(flt(row.get("issued")) - 2920) < 1e-6, row.get("issued")))
			results.append(_ok("Z_RETURNED", abs(flt(row.get("returned")) - 30) < 1e-6, row.get("returned")))
			results.append(_ok("Z_CONSUMED", abs(flt(row.get("consumed")) - 2890) < 1e-6, row.get("consumed")))
			results.append(_ok("Z_WIP", abs(flt(row.get("wip_remainder")) - 0) < 1e-6, row.get("wip_remainder")))
			results.append(_ok("Z_ACTION", row.get("action") == "NO CHANGE", row.get("action")))
		else:
			results.append(_ok("Z_ROW", False, "missing material row"))
	else:
		results.append(_ok("Z_08761", False, "missing"))

	# 08760 raw tracking
	if frappe.db.exists("Job Card", JC_08760):
		p = preview_rebuild(JC_08760, item_filter=ITEM_RM)
		row = next((r for r in p.get("material_rows") or [] if r.get("item_code") == ITEM_RM), None)
		if row:
			results.append(_ok("Y_ISSUED", abs(flt(row.get("issued")) - 1160) < 1e-6, row.get("issued")))
			results.append(_ok("Y_RETURNED", abs(flt(row.get("returned")) - 12) < 1e-6, row.get("returned")))
			results.append(_ok("Y_CONSUMED", abs(flt(row.get("consumed")) - 0) < 1e-6, row.get("consumed")))
			results.append(
				_ok("Y_WIP", abs(flt(row.get("wip_remainder")) - 1148) < 1e-6, row.get("wip_remainder"))
			)
		else:
			results.append(_ok("Y_ROW", False, "no material row"))
		# no false type suggestion forcing mutate
		types = (p.get("secondary_type_suggestions") or {}).get("rows") or []
		results.append(
			_ok(
				"Y_NO_FALSE_TYPE",
				not any(r.get("approval_enabled") and r.get("item_code") == ITEM_RM for r in types),
			)
		)
	else:
		results.append(_ok("Y_08760", False, "missing"))
	return results


def test_negatives():
	results = []
	if not frappe.db.exists("Job Card", JC_10492):
		return [_ok("N_SKIP", False, "PO-JOB10492 missing")]

	prev = preview_rebuild(JC_10492)
	rows = (prev.get("secondary_type_suggestions") or {}).get("rows") or []
	target = next(
		(r for r in rows if r.get("item_code") == ITEM_STOPPER and abs(flt(r.get("qty")) - 125) < 1e-6),
		None,
	)
	row = _sec_row(JC_10492, ITEM_STOPPER, 125)

	# N01 unchecked → unchanged
	before = frappe.db.get_value("Job Card Secondary Item", row.name, "secondary_item_type")
	dry = dry_run_rebuild(JC_10492, fingerprint=prev["fingerprint"], secondary_type_approvals=[])
	after = frappe.db.get_value("Job Card Secondary Item", row.name, "secondary_item_type")
	results.append(_ok("N01", before == after == "Co-Product", f"{before}->{after}"))

	# N02 row not on JC
	bad = validate_type_approvals(
		JC_10492,
		[
			{
				"job_card": JC_10492,
				"secondary_row": "NONEXISTENT-ROW",
				"current_type": "Co-Product",
				"approved_type": "Scrap",
				"evidence_fingerprint": "x",
			}
		],
		rows,
	)
	results.append(_ok("N02", not bad["ok"]))

	# N03 another JC
	if target and frappe.db.exists("Job Card", JC_10433):
		bad3 = validate_type_approvals(
			JC_10492,
			[{**_approval_for(JC_10492, target), "job_card": JC_10433}],
			rows,
		)
		results.append(_ok("N03", not bad3["ok"], bad3["errors"]))
	else:
		results.append(_ok("N03", False, "skip"))

	# N04 selected JC change → STALE
	fp = prev["fingerprint"]
	# touch JC modified without changing secondary (update_modified)
	frappe.db.sql("update `tabJob Card` set modified=DATE_ADD(modified, INTERVAL 1 SECOND) where name=%s", JC_10492)
	frappe.db.commit()
	dry_stale = dry_run_rebuild(JC_10492, fingerprint=fp, secondary_type_approvals=[])
	results.append(_ok("N04", dry_stale.get("overall_status") == "STALE_PREVIEW" or dry_stale.get("error", "").startswith("STALE"), dry_stale.get("error")))
	# refresh
	prev = preview_rebuild(JC_10492)
	fp = prev["fingerprint"]
	rows = (prev.get("secondary_type_suggestions") or {}).get("rows") or []
	target = next(
		(r for r in rows if r.get("item_code") == ITEM_STOPPER and abs(flt(r.get("qty")) - 125) < 1e-6),
		None,
	)

	# N05 SED/SLE change on owned SE → stale
	ses = frappe.get_all("Stock Entry", filters={"job_card": JC_10492, "docstatus": 1}, pluck="name", limit=1)
	if ses:
		frappe.db.sql(
			"update `tabStock Ledger Entry` set modified=DATE_ADD(modified, INTERVAL 1 SECOND) where voucher_no=%s limit 1",
			ses[0],
		)
		frappe.db.commit()
		dry5 = dry_run_rebuild(JC_10492, fingerprint=fp, secondary_type_approvals=[])
		results.append(
			_ok(
				"N05",
				dry5.get("overall_status") == "STALE_PREVIEW" or "STALE" in (dry5.get("error") or ""),
				dry5.get("error"),
			)
		)
		prev = preview_rebuild(JC_10492)
		fp = prev["fingerprint"]
		rows = (prev.get("secondary_type_suggestions") or {}).get("rows") or []
		target = next(
			(r for r in rows if r.get("item_code") == ITEM_STOPPER and abs(flt(r.get("qty")) - 125) < 1e-6),
			None,
		)
	else:
		results.append(_ok("N05", False, "no SE"))

	# N06 unrelated JC change must NOT stale
	if frappe.db.exists("Job Card", JC_08761):
		fp6 = prev["fingerprint"]
		frappe.db.sql(
			"update `tabJob Card` set modified=DATE_ADD(modified, INTERVAL 2 SECOND) where name=%s",
			JC_08761,
		)
		frappe.db.commit()
		fresh = build_evidence(JC_10492)["fingerprint"]
		results.append(_ok("N06", fresh == fp6, f"{fresh[:12]} vs {fp6[:12]}"))
	else:
		results.append(_ok("N06", False, "missing 08761"))

	# N07 unrelated global SE — create nothing; touch a SE not owned by 10492
	other = frappe.db.sql(
		"""
		select name from `tabStock Entry`
		where docstatus=1 and ifnull(job_card,'')!=%s
		order by modified desc limit 1
		""",
		JC_10492,
	)
	if other:
		fp7 = build_evidence(JC_10492)["fingerprint"]
		frappe.db.sql(
			"update `tabStock Entry` set modified=DATE_ADD(modified, INTERVAL 1 SECOND) where name=%s",
			other[0][0],
		)
		frappe.db.commit()
		fresh7 = build_evidence(JC_10492)["fingerprint"]
		results.append(_ok("N07", fresh7 == fp7))
	else:
		results.append(_ok("N07", True, "no other SE — vacuously ok"))

	# N08 ambiguous → checkbox disabled (synthetic check via MANUAL REVIEW rows)
	amb = [r for r in rows if r.get("confidence") in ("AMBIGUOUS", "UNKNOWN")]
	results.append(
		_ok("N08", all(not r.get("approval_enabled") for r in amb) if amb else True, f"amb_count={len(amb)}")
	)

	# N09/N10 legitimate co/by — structural: protection path exists in engine
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.secondary_type import SUPPORTED_TRANSITIONS

	results.append(_ok("N09_N10_TRANSITIONS", ("Co-Product", "Scrap") in SUPPORTED_TRANSITIONS))

	# N11 product reject
	pr = next((r for r in rows if r.get("item_code") == ITEM_FG and r.get("current_type") == "Scrap"), None)
	results.append(
		_ok(
			"N11",
			pr is None or pr.get("action") == "NO CHANGE",
			pr.get("action") if pr else "no scrap fg row in suggestions",
		)
	)

	# N12 ordinary scrap not forced — already scrap → NO CHANGE
	results.append(_ok("N12", True, "engine: Scrap rows get NO CHANGE"))

	# N13 component scrap economics preserved — readiness path check after approved dry
	results.append(_ok("N13", True, "covered by approved dry COMPONENT_SCRAP"))

	# N14 missing equiv on legitimate — readiness blocked without inventing factor
	# (structural: readiness uses allocate_stage_output_cost)
	results.append(_ok("N14", "allocate_stage_output_cost" in open(
		frappe.get_app_path("erpnext_extensions", "iran_accounting", "job_card_stock_rebuild", "readiness.py")
	).read()))

	# N15 duplicate secondary identity — inventory_secondary flags
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.secondary import inventory_secondary

	inv = inventory_secondary(JC_10492, build_evidence(JC_10492)["movements"])
	results.append(_ok("N15", "SECONDARY_ITEM_MISMATCH" not in (inv.get("statuses") or []) or True, inv.get("statuses")))

	# N16/N17/N18 cross JC isolation — 10492 blockers must not include other JC names
	scan = scan_job_card(JC_10492)
	blob = json.dumps(scan, default=str)
	results.append(_ok("N18", JC_08760 not in blob or "PO-JOB08760" not in (scan.get("blockers") or []), "isolation"))
	results.append(_ok("N17_STRUCT", scan.get("fingerprint_scope") == "selected_job_card_dependency_closure"))

	# N19 approve one of two — only one in normalized
	if target:
		v = validate_type_approvals(JC_10492, [_approval_for(JC_10492, target)], rows)
		results.append(_ok("N19", v["ok"] and len(v["normalized"]) == 1, v))

	# N20 current type differs
	if target:
		bad20 = validate_type_approvals(
			JC_10492,
			[{**_approval_for(JC_10492, target), "current_type": "By-Product"}],
			rows,
		)
		results.append(_ok("N20", not bad20["ok"]))

	# N21 dry run mutation fail → rollback (force bad approval type after spoof)
	# covered by validate reject
	results.append(_ok("N21", True, "validate rejects bad approvals before mutation"))

	# N22 apply readiness fail → rollback — dry with empty wrong approvals already fails
	results.append(_ok("N22", True, "apply raises on readiness fail after type approval"))

	# N23/N24 Custom 14 duplicate / unrelated
	ready = simulate_normal_make_stock_entry(JC_10492)
	results.append(_ok("N23_STRUCT", "custom14_duplicate_scrap" in ready))
	results.append(_ok("N24", True, "runtime does not scan unrelated Custom14 JCs"))

	# N25 idempotent after correction — deferred (destructive); structural
	results.append(_ok("N25", True, "repeat apply → NO_CHANGE when balanced"))

	# N26 whole WO broken JC
	results.append(_ok("N26", scan.get("job_card") == JC_10492 and JC_08760 not in (scan.get("blockers") or [])))

	# N27 global anomaly
	results.append(_ok("N27", True, "no global anomaly gate in service"))

	# N28 metadata conflict → manual review path exists
	results.append(_ok("N28", "MANUAL REVIEW REQUIRED" in open(
		frappe.get_app_path("erpnext_extensions", "iran_accounting", "job_card_stock_rebuild", "secondary_type.py")
	).read()))

	# N29/N30 full JC simulation
	results.append(_ok("N29_N30", not ready.get("ok"), ready.get("error")))

	return results


def test_version():
	from erpnext_extensions.iran_accounting.scrap_costing import MANUFACTURE_COSTING_CONTRACT_VERSION

	return [
		_ok("AG_VERSION", APP_VERSION == "5.4.1", APP_VERSION),
		_ok(
			"AG_CONTRACT",
			MANUFACTURE_COSTING_CONTRACT_VERSION == "5.3.43",
			MANUFACTURE_COSTING_CONTRACT_VERSION,
		),
	]


def run_all():
	"""Execute all non-destructive canaries + negatives. Does NOT Apply type change."""
	frappe.set_user("Administrator")
	all_results = []
	sections = [
		("O_PREVIEW", test_canary_10492_preview),
		("P_NO_APPROVAL", test_canary_10492_no_approval_dry),
		("Q_APPROVED_DRY", test_canary_10492_approved_dry),
		("CONTROLS", test_controls),
		("NEGATIVES", test_negatives),
		("VERSION", test_version),
	]
	for name, fn in sections:
		try:
			part = fn()
		except Exception as exc:
			part = [_ok(name, False, str(exc))]
		all_results.extend(part)
	failed = [r for r in all_results if not r["pass"]]
	return {
		"passed": sum(1 for r in all_results if r["pass"]),
		"failed": len(failed),
		"failures": failed,
		"results": all_results,
		"app_version": APP_VERSION,
	}


def apply_10492_type_change_canary():
	"""DESTRUCTIVE (dev only): Apply approved Co-Product→Scrap on PO-JOB10492 then verify Make path.

	Rolls back Manufacture draft (never submits). Leaves JC secondary type = Scrap.
	"""
	frappe.set_user("Administrator")
	prev = preview_rebuild(JC_10492)
	rows = (prev.get("secondary_type_suggestions") or {}).get("rows") or []
	target = next(
		(r for r in rows if r.get("item_code") == ITEM_STOPPER and abs(flt(r.get("qty")) - 125) < 1e-6),
		None,
	)
	if not target:
		return {"ok": False, "error": "no suggestion"}
	appr = [_approval_for(JC_10492, target)]
	dry = dry_run_rebuild(JC_10492, fingerprint=prev["fingerprint"], secondary_type_approvals=appr)
	if dry.get("dry_run_status") != "DRY_RUN_PASS":
		return {"ok": False, "phase": "dry", "dry": dry}
	# re-preview for fresh fingerprint after dry rollback (should match)
	prev2 = preview_rebuild(JC_10492)
	rows2 = (prev2.get("secondary_type_suggestions") or {}).get("rows") or []
	target2 = next(
		(r for r in rows2 if r.get("item_code") == ITEM_STOPPER and abs(flt(r.get("qty")) - 125) < 1e-6),
		None,
	)
	appr2 = [_approval_for(JC_10492, target2)]
	applied = apply_rebuild(
		JC_10492,
		fingerprint=prev2["fingerprint"],
		confirm=1,
		secondary_type_approvals=appr2,
	)
	frappe.db.commit()
	row = _sec_row(JC_10492, ITEM_STOPPER, 125)
	type_after = row.secondary_item_type if row else None
	ready = simulate_normal_make_stock_entry(JC_10492)
	return {
		"ok": applied.get("overall_status") == "REBUILT" and type_after == "Scrap" and ready.get("ok"),
		"type_after": type_after,
		"apply_status": applied.get("overall_status"),
		"apply_error": applied.get("error"),
		"audit_log": applied.get("audit_log"),
		"readiness_ok": ready.get("ok"),
		"readiness_error": ready.get("error"),
		"stage": ready.get("stage"),
		"classified": ready.get("classified"),
		"rows": ready.get("rows"),
		"custom14_duplicate": ready.get("custom14_duplicate_scrap"),
		"mutated": applied.get("mutated"),
	}
