# Copyright (c) 2026, ERPNext Extensions contributors
"""One Historical Repair mutation pipeline (v5.3.5+).

    scan → find_root → plan → repair → repost → verify

Public automatic repairs should enter here. Family/reason are strategy
selectors — not separate user workflows or mutation engines.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from time import perf_counter
from typing import Any

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock.simple_model import (
	FAMILY_ACCOUNTING,
	FAMILY_DERIVED_STATE,
	FAMILY_MANUFACTURE_FLOW,
	FAMILY_POSTING_ORDER,
	FAMILY_VALUATION,
	PRIMARY_FAILED,
	PRIMARY_LEGITIMATE,
	PRIMARY_MANUAL,
	PRIMARY_READY,
	PRIMARY_REPAIRED,
	PRIMARY_WAITING,
	REASON_BIN_DRIFT,
	REASON_GL_DRIFT,
	REASON_LEFTOVER_MA,
	REASON_MANUFACTURE_FLOW,
	REASON_POSTING_ORDER,
	REASON_WRONG_RATE,
	REASON_ZERO_RATE,
	annotate_row,
	primary_state,
	reason_code,
	root_family,
)

MANUFACTURE_PURPOSES = frozenset(
	{
		"Manufacture",
		"Material Transfer for Manufacture",
		"Material Consumption for Manufacture",
		"Repack",
	}
)


@dataclass
class RepairPlan:
	"""Single portable plan for any READY root."""

	root_id: str
	family: str
	reason: str
	primary_state: str
	item: str | None = None
	warehouse: str | None = None
	voucher: str | None = None
	voucher_type: str | None = None
	batch: str | None = None
	expected_state: dict = field(default_factory=dict)
	current_state: dict = field(default_factory=dict)
	evidence: dict = field(default_factory=dict)
	operations: list[str] = field(default_factory=list)
	postconditions: list[str] = field(default_factory=list)
	downstream_findings: int = 0
	row: dict = field(default_factory=dict)
	why_safe: str = ""
	blocked_reason: str | None = None

	def to_dict(self) -> dict:
		d = asdict(self)
		return d


def _purpose(row: dict) -> str:
	purpose = str(row.get("purpose") or "")
	if purpose:
		return purpose
	voucher = row.get("voucher") or row.get("voucher_no")
	if voucher and frappe.db.exists("Stock Entry", voucher):
		return str(frappe.db.get_value("Stock Entry", voucher, "purpose") or "")
	return ""


def is_manufacture_adjacent(row: dict) -> bool:
	"""Generic receipt/wrong-rate algorithms must not mutate these."""
	purpose = _purpose(row)
	if purpose in MANUFACTURE_PURPOSES:
		return True
	wh = str(row.get("warehouse") or row.get("s_warehouse") or row.get("t_warehouse") or "")
	low = wh.lower()
	if "پایکار" in wh or "paykar" in low:
		return True
	if any(tok in low for tok in ("reject", "scrap", "waste", "ضایعات", "اسقاط")):
		return True
	if cint_safe(row.get("is_scrap_item")) or cint_safe(row.get("is_finished_item")):
		return True
	return False


def cint_safe(v) -> int:
	try:
		from frappe.utils import cint

		return cint(v)
	except Exception:
		return 0


def find_root(row: dict) -> dict:
	"""Earliest causal root for a finding row."""
	row = annotate_row(row)
	pz = row.get("patient_zero") or row.get("root_patient_zero") or {}
	if isinstance(pz, str):
		pz = {"voucher_no": pz}
	root_voucher = (
		(pz or {}).get("voucher_no")
		or (pz or {}).get("voucher")
		or row.get("root_voucher")
		or row.get("voucher")
		or row.get("voucher_no")
	)
	return {
		"root_voucher": root_voucher,
		"item": row.get("item") or row.get("item_code"),
		"warehouse": row.get("warehouse") or row.get("s_warehouse") or row.get("t_warehouse"),
		"batch": row.get("batch") or row.get("batch_no"),
		"family": row.get("root_family") or root_family(row),
		"reason": row.get("reason_code") or reason_code(row),
		"primary_state": row.get("primary_state") or primary_state(row),
		"patient_zero": pz,
		"row": row,
	}


def compress_roots(rows: list[dict]) -> dict:
	"""Collapse findings into causal roots. One root may explain many rows."""
	by_root: dict[str, dict] = {}
	for raw in rows or []:
		root = find_root(raw)
		key = "|".join(
			[
				str(root.get("root_voucher") or ""),
				str(root.get("item") or ""),
				str(root.get("warehouse") or ""),
				str(root.get("family") or ""),
			]
		)
		bucket = by_root.setdefault(
			key,
			{
				"root_id": key,
				"root_voucher": root.get("root_voucher"),
				"item": root.get("item"),
				"warehouse": root.get("warehouse"),
				"batch": root.get("batch"),
				"family": root.get("family"),
				"reason": root.get("reason"),
				"primary_state": root.get("primary_state"),
				"findings": 0,
				"rows": [],
				"manufacturing": False,
			},
		)
		bucket["findings"] += 1
		bucket["rows"].append(raw)
		if is_manufacture_adjacent(raw):
			bucket["manufacturing"] = True
			if bucket["primary_state"] == PRIMARY_READY:
				bucket["primary_state"] = PRIMARY_MANUAL
				bucket["reason"] = REASON_MANUFACTURE_FLOW
	roots = sorted(by_root.values(), key=lambda r: (-int(r["findings"]), str(r["root_voucher"] or "")))
	return {
		"finding_count": len(rows or []),
		"root_count": len(roots),
		"ready_roots": [r for r in roots if r["primary_state"] == PRIMARY_READY and not r["manufacturing"]],
		"waiting_roots": [r for r in roots if r["primary_state"] == PRIMARY_WAITING],
		"manual_roots": [r for r in roots if r["primary_state"] == PRIMARY_MANUAL or r["manufacturing"]],
		"legitimate_roots": [r for r in roots if r["primary_state"] == PRIMARY_LEGITIMATE],
		"roots": roots,
	}


def plan(row: dict) -> RepairPlan:
	"""Build a RepairPlan. Manufacture uses native residual; generic WR stays MANUAL."""
	annotated = annotate_row(row)
	root = find_root(annotated)
	family = root["family"]
	reason = root["reason"]
	state = root["primary_state"]
	mfg = is_manufacture_adjacent(annotated)
	native = annotated.get("manufacture_native") or {}
	native_ready = (
		mfg
		and reason in (REASON_WRONG_RATE, REASON_ZERO_RATE)
		and annotated.get("eligible")
		and str(annotated.get("source_of_truth") or "") == "historical_manufacture_consumed_svd"
		and str(native.get("classification") or "") == "EXACT"
	)
	if mfg and reason in (REASON_WRONG_RATE, REASON_ZERO_RATE, REASON_LEFTOVER_MA):
		family = FAMILY_MANUFACTURE_FLOW
		reason = REASON_MANUFACTURE_FLOW
		if native_ready:
			state = PRIMARY_READY
		elif annotated.get("no_action_required") or native.get("classification") in (
			"LEGITIMATE",
			"HEALTHY",
		):
			state = PRIMARY_LEGITIMATE
		elif (
			native.get("classification") == "WAITING_UPSTREAM"
			or annotated.get("manual_lane") == "WAITING_UPSTREAM"
		):
			state = PRIMARY_WAITING
		else:
			state = PRIMARY_MANUAL
	ops: list[str] = []
	if state == PRIMARY_READY:
		if reason == REASON_LEFTOVER_MA:
			ops = ["stamp_leftover_ma", "narrow_riv", "desk_postcondition"]
		elif reason == REASON_MANUFACTURE_FLOW:
			ops = ["write_manufacture_fg_residual", "narrow_riv", "verify_rate"]
		elif reason in (REASON_WRONG_RATE, REASON_ZERO_RATE):
			ops = ["write_authoritative_rate", "identity_replay", "narrow_riv", "verify_rate"]
		elif reason == REASON_POSTING_ORDER:
			ops = ["fix_posting_order", "repost", "verify"]
		elif reason == REASON_BIN_DRIFT:
			ops = ["rebuild_bin"]
		elif reason == REASON_GL_DRIFT:
			ops = ["rebuild_gl"]
		else:
			ops = ["generic_ready_repair"]
	blocked = None
	if state != PRIMARY_READY:
		blocked = annotated.get("message") or annotated.get("reason") or f"not READY ({state})"
	return RepairPlan(
		root_id="|".join(
			[
				str(root.get("root_voucher") or ""),
				str(root.get("item") or ""),
				str(root.get("warehouse") or ""),
				str(family),
			]
		),
		family=family,
		reason=reason,
		primary_state=state,
		item=root.get("item"),
		warehouse=root.get("warehouse"),
		voucher=root.get("root_voucher") or annotated.get("voucher"),
		voucher_type=annotated.get("voucher_type"),
		batch=root.get("batch"),
		expected_state={
			"rate": annotated.get("proposed_rate")
			or annotated.get("expected_value")
			or annotated.get("expected_ma")
			or native.get("expected_target_rate"),
			"source": annotated.get("source_of_truth")
			or annotated.get("expected_source")
			or annotated.get("reconstruction_source"),
		},
		current_state={
			"rate": annotated.get("current_value")
			or annotated.get("incoming_rate")
			or annotated.get("current_valuation_rate"),
			"qty": annotated.get("current_qty") or annotated.get("qty"),
		},
		evidence={
			"confidence": annotated.get("confidence"),
			"patient_zero": root.get("patient_zero"),
			"flags": annotated.get("flags"),
			"surface": annotated.get("surface"),
			"manufacture_native": {
				k: native.get(k)
				for k in ("classification", "consumed_value", "scrap_value", "reason")
				if native
			},
		},
		operations=ops,
		postconditions=[
			"i1_unchanged",
			"neg_stock_unchanged",
			"bin_not_worse",
			"gl_not_worse",
			"no_mfg_qty_delta",
			"riv_stable",
			"idempotent",
		],
		downstream_findings=int(annotated.get("downstream_count") or annotated.get("sql_updates") or 0),
		row=annotated,
		why_safe=(
			"Manufacture FG residual from this voucher's consumed SLE SVD; quantities untouched."
			if state == PRIMARY_READY and reason == REASON_MANUFACTURE_FLOW
			else (
				"Authoritative expected rate; manufacturing quantities untouched; official RIV rebuilds descendants."
				if state == PRIMARY_READY
				else ""
			)
		),
		blocked_reason=blocked,
	)


def capture_safety_fingerprint(company: str | None = None) -> dict:
	"""Central safety snapshot used before/after every automatic repair."""
	conds = ["IFNULL(is_cancelled,0)=0"]
	args: list = []
	if company:
		conds.append(
			"(voucher_type<>'Stock Entry' OR EXISTS (SELECT 1 FROM `tabStock Entry` se WHERE se.name=voucher_no AND se.company=%s))"
		)
		args.append(company)
	where = " AND ".join(conds)
	i1 = frappe.db.sql(
		f"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE {where} AND incoming_rate < -0.0001",
		args,
	)[0][0]
	neg = frappe.db.sql(
		f"SELECT COUNT(*) FROM `tabStock Ledger Entry` WHERE {where} AND qty_after_transaction < -0.0001",
		args,
	)[0][0]
	# Cheap stock-value vs Bin drift sample (not full Scan All).
	bin_mismatch = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabBin` b
		WHERE EXISTS (
			SELECT 1 FROM `tabStock Ledger Entry` sle
			WHERE sle.item_code=b.item_code AND sle.warehouse=b.warehouse AND IFNULL(sle.is_cancelled,0)=0
		)
		AND ABS(IFNULL(b.stock_value,0) - IFNULL((
			SELECT sle2.stock_value FROM `tabStock Ledger Entry` sle2
			WHERE sle2.item_code=b.item_code AND sle2.warehouse=b.warehouse AND IFNULL(sle2.is_cancelled,0)=0
			ORDER BY sle2.posting_datetime DESC, sle2.creation DESC LIMIT 1
		),0)) > 1
		"""
	)[0][0]
	mfg = {
		"wo_produced": flt(frappe.db.sql("SELECT IFNULL(SUM(produced_qty),0) FROM `tabWork Order`")[0][0]),
		"jc_for": flt(frappe.db.sql("SELECT IFNULL(SUM(for_quantity),0) FROM `tabJob Card`")[0][0]),
		"jc_done": flt(
			frappe.db.sql("SELECT IFNULL(SUM(IFNULL(total_completed_qty,0)),0) FROM `tabJob Card`")[0][0]
		),
	}
	return {
		"i1": int(i1),
		"neg_qty": int(neg),
		"bin_value_mismatch": int(bin_mismatch),
		"manufacturing": mfg,
		"captured_at": str(frappe.utils.now_datetime()),
	}


def compare_safety(before: dict, after: dict) -> dict:
	"""Return ok=False when any gate regresses."""
	failures = []
	if int(after.get("i1") or 0) > int(before.get("i1") or 0):
		failures.append(f"I1 {before.get('i1')}→{after.get('i1')}")
	if int(after.get("neg_qty") or 0) > int(before.get("neg_qty") or 0):
		failures.append(f"neg_qty {before.get('neg_qty')}→{after.get('neg_qty')}")
	if int(after.get("bin_value_mismatch") or 0) > int(before.get("bin_value_mismatch") or 0):
		failures.append(
			f"bin_value_mismatch {before.get('bin_value_mismatch')}→{after.get('bin_value_mismatch')}"
		)
	bm = before.get("manufacturing") or {}
	am = after.get("manufacturing") or {}
	for key in ("wo_produced", "jc_for", "jc_done"):
		if abs(flt(am.get(key)) - flt(bm.get(key))) > 0.0001:
			failures.append(f"mfg.{key} delta {flt(am.get(key)) - flt(bm.get(key))}")
	return {"ok": not failures, "failures": failures, "before": before, "after": after}


def execute(plan_obj: RepairPlan, *, dry_run: bool = True) -> dict:
	"""Dispatch ONE mutation path by family/reason."""
	t0 = perf_counter()
	if plan_obj.primary_state in (PRIMARY_REPAIRED, PRIMARY_LEGITIMATE):
		return {
			"ok": True,
			"dry_run": dry_run,
			"primary_state": plan_obj.primary_state,
			"status": "ALREADY_REPAIRED" if plan_obj.primary_state == PRIMARY_REPAIRED else PRIMARY_LEGITIMATE,
			"message": "NO_ACTION",
			"economic_writes": 0,
			"plan": plan_obj.to_dict(),
			"elapsed_seconds": round(perf_counter() - t0, 3),
		}
	if plan_obj.primary_state != PRIMARY_READY:
		return {
			"ok": False,
			"dry_run": dry_run,
			"primary_state": plan_obj.primary_state,
			"status": PRIMARY_MANUAL if plan_obj.primary_state == PRIMARY_MANUAL else plan_obj.primary_state,
			"reason": plan_obj.blocked_reason or "not READY",
			"plan": plan_obj.to_dict(),
			"elapsed_seconds": round(perf_counter() - t0, 3),
		}

	row = dict(plan_obj.row or {})
	reason = plan_obj.reason
	safety_before = capture_safety_fingerprint()

	if reason == REASON_LEFTOVER_MA:
		from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
			repair_leftover_ma_selected,
		)

		out = repair_leftover_ma_selected(
			[{"item": plan_obj.item, "warehouse": plan_obj.warehouse, **row}],
			dry_run=dry_run,
		)
	elif reason == REASON_MANUFACTURE_FLOW:
		from erpnext_extensions.iran_accounting.historical_stock.manufacture_native import (
			repair_manufacture_valuation,
		)

		out = repair_manufacture_valuation(plan_obj.voucher or row.get("voucher"), dry_run=dry_run)
	elif reason in (REASON_WRONG_RATE, REASON_ZERO_RATE):
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.apply import (
			apply_wrong_rate_root,
		)

		out = apply_wrong_rate_root(row, dry_run=dry_run)
	elif reason == REASON_POSTING_ORDER:
		from erpnext_extensions.iran_accounting.stock_posting_order.api import (
			repair_posting_order_selected,
		)

		out = repair_posting_order_selected([row], dry_run=dry_run)
	elif reason == REASON_BIN_DRIFT:
		from erpnext_extensions.iran_accounting.stock_posting_order.replay import _bin_from_last_sle

		if dry_run:
			out = {
				"ok": True,
				"dry_run": True,
				"would": "rebuild_bin_from_last_sle",
				"item": plan_obj.item,
				"warehouse": plan_obj.warehouse,
			}
		else:
			_bin_from_last_sle(plan_obj.item, plan_obj.warehouse)
			out = {"ok": True, "written": True, "economic_writes": 0, "path": "bin_rebuild_from_last_sle"}
	elif reason == REASON_GL_DRIFT:
		from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import rebuild_gl_for_voucher

		out = rebuild_gl_for_voucher(plan_obj.voucher, dry_run=dry_run)
	else:
		return {
			"ok": False,
			"dry_run": dry_run,
			"status": PRIMARY_MANUAL,
			"reason": f"no strategy for {plan_obj.family}/{reason}",
			"plan": plan_obj.to_dict(),
		}

	ok = bool(out.get("ok", True)) and not out.get("aborted")
	safety = None
	if ok and not dry_run:
		safety = compare_safety(safety_before, capture_safety_fingerprint())
		if not safety["ok"]:
			ok = False
			out = {**(out if isinstance(out, dict) else {"raw": out}), "safety": safety, "aborted": True}
	status = PRIMARY_REPAIRED if ok and not dry_run else (PRIMARY_READY if dry_run and ok else PRIMARY_FAILED)
	if dry_run and ok:
		status = PRIMARY_READY
	return {
		"ok": ok,
		"dry_run": dry_run,
		"primary_state": status,
		"family": plan_obj.family,
		"reason": reason,
		"plan": plan_obj.to_dict(),
		"result": out,
		"safety": safety,
		"economic_writes": _economic_writes(out),
		"elapsed_seconds": round(perf_counter() - t0, 3),
	}


def _economic_writes(out: Any) -> int:
	if not isinstance(out, dict):
		return 0
	if out.get("economic_writes") is not None:
		return int(out.get("economic_writes") or 0)
	applied = out.get("applied") or []
	if isinstance(applied, list) and applied:
		return sum(int(a.get("economic_writes") or (1 if a.get("written") else 0)) for a in applied)
	if out.get("written"):
		return 1
	return 0


def verify(plan_obj: RepairPlan) -> dict:
	"""Postcondition check for a repaired root."""
	item = plan_obj.item
	warehouse = plan_obj.warehouse
	voucher = plan_obj.voucher
	out: dict[str, Any] = {"ok": True, "checks": {}}
	if item and warehouse:
		from erpnext_extensions.iran_accounting.historical_stock.integrity import chain_integrity

		try:
			out["checks"]["chain"] = chain_integrity(item, warehouse)
		except Exception as exc:
			out["checks"]["chain_error"] = str(exc)[:300]
	if plan_obj.reason == REASON_LEFTOVER_MA and item and warehouse and voucher:
		from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
			classify_leftover_ma_identity,
		)

		c = classify_leftover_ma_identity(item, warehouse)
		out["checks"]["leftover_ma"] = {
			"status": c.get("leftover_ma_status"),
			"eligible": c.get("eligible"),
		}
		if c.get("leftover_ma_status") not in ("LEFTOVER_MA_REPAIRED", "NO_ACTION") and c.get("eligible"):
			out["ok"] = False
	if plan_obj.reason in (REASON_WRONG_RATE, REASON_ZERO_RATE) and voucher and item:
		from erpnext_extensions.iran_accounting.historical_stock.wrong_rate_engine.classifier import (
			classify_wrong_rate_row,
		)

		c = classify_wrong_rate_row({**plan_obj.row, "voucher": voucher, "item": item, "warehouse": warehouse})
		out["checks"]["wrong_rate"] = {
			"rate_status": c.get("rate_status"),
			"eligible": c.get("eligible"),
			"current": c.get("current_value"),
			"expected": c.get("expected_value") or c.get("proposed_rate"),
		}
		# Repaired identity should no longer be READY with sql_updates > 0
		if c.get("rate_status") == "READY_WRONG_RATE" and int(c.get("sql_updates") or 0) > 0:
			out["ok"] = False
	out["safety"] = capture_safety_fingerprint()
	return out


def repair_safe_roots(rows: list[dict], *, dry_run: bool = True) -> dict:
	"""Public entry: plan + execute for each READY non-manufacturing root."""
	applied = []
	blocked = []
	plans = []
	for raw in rows or []:
		p = plan(raw)
		plans.append(p.to_dict())
		result = execute(p, dry_run=dry_run)
		if result.get("ok"):
			applied.append(result)
		else:
			blocked.append(result)
	return {
		"dry_run": dry_run,
		"applied": applied,
		"blocked": blocked,
		"plans": plans,
		"count_applied": len(applied),
		"count_blocked": len(blocked),
	}
