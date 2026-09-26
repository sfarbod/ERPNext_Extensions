# Copyright (c) 2026, ERPNext Extensions contributors
"""Allowlisted Production Historical Repair execution.

APPLY ONLY the frozen manifest. Never APPLY ALL READY. Never open earliest-SLE RIV.
Never retry Failed RIV. Never invent rates or quantities.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import frappe
from frappe.utils import flt, now_datetime

from erpnext_extensions import __version__

COMPANY = "اسپاد فارمد دارو"
MANIFEST_DIR = Path(__file__).resolve().parent / "production_manifests"
DEFAULT_MANIFEST_ID = "HR-PROD-20260926-SEGMENTED-v1"
SITE_OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/private/files/hr_prod_ready_0926"
)
FORBIDDEN_RIV = (
	"full_company_riv",
	"open_earliest_sle_riv",
	"failed_riv_retry",
	"apply_all_ready_drain",
)


def _jdump(path: Path, obj) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def load_manifest(manifest_id: str = DEFAULT_MANIFEST_ID) -> dict:
	path = MANIFEST_DIR / f"{manifest_id}.json"
	if not path.exists():
		frappe.throw(f"Unknown Production manifest: {manifest_id}")
	data = json.loads(path.read_text())
	if data.get("manifest_id") != manifest_id:
		frappe.throw("Manifest id mismatch inside file")
	allow = "\n".join(sorted(r["root_id"] for r in data.get("roots") or []))
	digest = hashlib.sha256(allow.encode()).hexdigest()
	if data.get("allowlist_sha256") != digest:
		frappe.throw("Manifest allowlist hash mismatch — refuse mutation")
	return data


def allowlist_ids(manifest: dict) -> set[str]:
	return {r["root_id"] for r in manifest.get("roots") or []}


def refuse_if_not_allowlisted(root_id: str, manifest: dict) -> None:
	if root_id not in allowlist_ids(manifest):
		frappe.throw(f"REFUSED: root {root_id} is not in Production allowlist {manifest.get('manifest_id')}")


def _gates() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import (
		capture_safety_fingerprint,
	)

	fp = capture_safety_fingerprint()
	neg_after = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND qty_after_transaction < -0.0001"""
	)[0][0]
	neg_rate = frappe.db.sql(
		"""SELECT COUNT(*) FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND (
			incoming_rate < -0.0001 OR outgoing_rate < -0.0001 OR valuation_rate < -0.0001
		)"""
	)[0][0]
	return {
		"i1": int(fp.get("i1") or 0),
		"neg_qty": int(fp.get("neg_qty") or 0),
		"bin_mismatch": int(fp.get("bin_value_mismatch") or 0),
		"neg_after": int(neg_after),
		"neg_rate": int(neg_rate),
		"mfg": dict(fp.get("manufacturing") or {}),
	}


def mfg_fingerprint() -> dict:
	from erpnext_extensions.iran_accounting.historical_stock._validation.prod_ready_0926 import (
		mfg_fingerprint as _fp,
	)

	return _fp()


def fingerprint_wrong_rate(root: dict) -> dict:
	v, item = root["voucher"], root.get("item")
	se = frappe.db.get_value(
		"Stock Entry",
		v,
		["docstatus", "purpose", "posting_date", "posting_time"],
		as_dict=True,
	)
	det = {}
	if item:
		det = (
			frappe.db.sql(
				"""SELECT qty, basic_rate, valuation_rate, batch_no, s_warehouse, t_warehouse
				FROM `tabStock Entry Detail` WHERE parent=%s AND item_code=%s LIMIT 1""",
				(v, item),
				as_dict=True,
			)
			or [{}]
		)[0]
	sle = frappe.db.sql(
		"""SELECT ROUND(SUM(stock_value_difference),4) svd,
			ROUND(SUM(actual_qty),4) qty,
			COUNT(*) n
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND is_cancelled=0 AND (%s IS NULL OR item_code=%s)""",
		(v, item, item),
		as_dict=True,
	)[0]
	payload = {
		"docstatus": se.docstatus if se else None,
		"purpose": se.purpose if se else None,
		"posting_date": str(se.posting_date) if se else None,
		"posting_time": str(se.posting_time) if se else None,
		"item": item,
		"qty": flt(det.get("qty")),
		"basic_rate": flt(det.get("basic_rate")),
		"batch": det.get("batch_no"),
		"sle_svd": flt(sle.svd),
		"sle_qty": flt(sle.qty),
		"sle_n": int(sle.n or 0),
		"expected_rate": flt(root.get("expected_rate")),
		"strategy": root.get("strategy"),
	}
	payload["sha256"] = hashlib.sha256(
		json.dumps(payload, sort_keys=True, default=str).encode()
	).hexdigest()
	return payload


def fingerprint_posting_order(root: dict) -> dict:
	rows = frappe.db.sql(
		"""SELECT item_code, warehouse, posting_datetime, actual_qty, qty_after_transaction
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation""",
		root["voucher"],
		as_dict=True,
	)
	payload = {
		"voucher": root["voucher"],
		"item": root.get("item"),
		"sles": [
			{
				"item": r.item_code,
				"warehouse": r.warehouse,
				"posting_datetime": str(r.posting_datetime),
				"actual_qty": flt(r.actual_qty),
				"qty_after": flt(r.qty_after_transaction),
			}
			for r in rows
		],
	}
	payload["sha256"] = hashlib.sha256(
		json.dumps(payload, sort_keys=True, default=str).encode()
	).hexdigest()
	return payload


def fingerprint_leftover_ma(root: dict) -> dict:
	row = frappe.db.sql(
		"""SELECT actual_qty, stock_value, valuation_rate FROM `tabBin`
		WHERE item_code=%s AND warehouse=%s""",
		(root["item"], root["warehouse"]),
		as_dict=True,
	)
	b = row[0] if row else {}
	payload = {
		"item": root["item"],
		"warehouse": root["warehouse"],
		"actual_qty": flt(b.get("actual_qty")),
		"stock_value": round(flt(b.get("stock_value")), 2),
		"valuation_rate": round(flt(b.get("valuation_rate")), 6),
	}
	payload["sha256"] = hashlib.sha256(
		json.dumps(payload, sort_keys=True, default=str).encode()
	).hexdigest()
	return payload


def capture_precondition(root: dict) -> dict:
	fam = root["family"]
	if fam == "WRONG_RATE":
		return fingerprint_wrong_rate(root)
	if fam == "POSTING_ORDER":
		return fingerprint_posting_order(root)
	if fam == "LEFTOVER_MA":
		return fingerprint_leftover_ma(root)
	frappe.throw(f"Unknown family {fam}")


def classify_preflight_root(root: dict, locked_fp: dict | None) -> str:
	"""MATCHED / CHANGED / MISSING / ALREADY_REPAIRED / BLOCKED."""
	if root["family"] == "WRONG_RATE":
		if not frappe.db.exists("Stock Entry", root["voucher"]):
			return "MISSING"
		live = fingerprint_wrong_rate(root)
		if locked_fp and live.get("sha256") == locked_fp.get("sha256"):
			return "MATCHED"
		# Already at expected rate
		if abs(flt(live.get("basic_rate")) - flt(root.get("expected_rate"))) < 0.5 and flt(live.get("basic_rate")) > 0:
			return "ALREADY_REPAIRED"
		if locked_fp:
			return "CHANGED"
		return "MATCHED"
	if root["family"] == "POSTING_ORDER":
		if not frappe.db.exists("Stock Entry", root["voucher"]):
			return "MISSING"
		live = fingerprint_posting_order(root)
		if locked_fp and live.get("sha256") == locked_fp.get("sha256"):
			return "MATCHED"
		# Healed if quarantine qty_after no longer negative
		neg = any(flt(s.get("qty_after")) < -0.0001 for s in live.get("sles") or [])
		if not neg and locked_fp:
			return "ALREADY_REPAIRED"
		if locked_fp:
			return "CHANGED"
		return "MATCHED"
	if root["family"] == "LEFTOVER_MA":
		live = fingerprint_leftover_ma(root)
		if not live.get("actual_qty"):
			return "MISSING"
		if locked_fp and live.get("sha256") == locked_fp.get("sha256"):
			return "MATCHED"
		if abs(flt(live.get("valuation_rate")) - flt(root.get("expected_rate"))) < 1:
			return "ALREADY_REPAIRED"
		if locked_fp:
			return "CHANGED"
		return "MATCHED"
	return "BLOCKED"


def lock_preconditions(manifest_id: str = DEFAULT_MANIFEST_ID) -> dict:
	"""Capture clean-backup fingerprints. Call only on unrepaired 20260926 restore."""
	man = load_manifest(manifest_id)
	locked = []
	for root in man["roots"]:
		fp = capture_precondition(root)
		locked.append({**root, "precondition": fp})
	out = {
		**man,
		"preconditions_locked_at": str(now_datetime()),
		"roots": locked,
	}
	allow = "\n".join(sorted(r["root_id"] for r in locked))
	out["allowlist_sha256"] = hashlib.sha256(allow.encode()).hexdigest()
	_jdump(SITE_OUT / f"{manifest_id}.locked.json", out)
	return out


def _load_locked(manifest_id: str) -> dict:
	for p in (
		SITE_OUT / f"{manifest_id}.locked.json",
		MANIFEST_DIR / f"{manifest_id}.locked.json",
	):
		if p.exists():
			return json.loads(p.read_text())
	return load_manifest(manifest_id)


def preflight(manifest_id: str = DEFAULT_MANIFEST_ID) -> dict:
	man = _load_locked(manifest_id)
	rows = []
	counts = {"MATCHED": 0, "CHANGED": 0, "MISSING": 0, "ALREADY_REPAIRED": 0, "BLOCKED": 0}
	for root in man.get("roots") or []:
		status = classify_preflight_root(root, root.get("precondition"))
		counts[status] = counts.get(status, 0) + 1
		rows.append({"root_id": root["root_id"], "family": root["family"], "voucher": root.get("voucher"), "status": status})
	out = {
		"manifest_id": man.get("manifest_id"),
		"allowlist_sha256": man.get("allowlist_sha256"),
		"counts": counts,
		"rows": rows,
		"eligible_to_mutate": [r["root_id"] for r in rows if r["status"] == "MATCHED"],
		"must_not_mutate": [r["root_id"] for r in rows if r["status"] in ("CHANGED", "MISSING", "BLOCKED")],
	}
	_jdump(SITE_OUT / "production_preflight.json", out)
	return out


def _hard_stop(tag: str, baseline: dict, live: dict, extra: str | None = None) -> None:
	reasons = []
	if int(live["i1"]) > int(baseline.get("i1") or 0):
		reasons.append(f"I1 {live['i1']} > baseline {baseline.get('i1')}")
	if int(live["neg_rate"]) > 0:
		reasons.append("negative rate")
	if int(live["neg_after"]) > int(baseline.get("neg_after") or 0):
		reasons.append("new negative stock")
	if extra:
		reasons.append(extra)
	if reasons:
		payload = {"tag": tag, "reasons": reasons, "gates": live}
		_jdump(SITE_OUT / f"ABORT_{tag}.json", payload)
		frappe.throw("HARD STOP: " + "; ".join(reasons))


def _apply_posting_order(root: dict, *, dry_run: bool) -> dict:
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan
	from erpnext_extensions.iran_accounting.stock_posting_order.api import (
		repair_posting_order_selected,
	)

	po = run_full_history_scan(company=COMPANY)
	chosen = []
	for raw in po.get("rows") or []:
		r = attach_plan(dict(raw))
		outb = r.get("outbound_document") or r.get("voucher") or r.get("outbound")
		if outb == root["voucher"] or r.get("inbound_document") == root["voucher"]:
			if str(r.get("planner_status") or "").startswith("READY") and int(r.get("sql_updates") or 0) > 0:
				chosen.append(r)
	if not chosen:
		return {"root_id": root["root_id"], "ok": False, "reason": "no_ready_po_row", "dry_run": dry_run}
	if dry_run:
		return {"root_id": root["root_id"], "ok": True, "dry_run": True, "sql_updates": chosen[0].get("sql_updates")}
	applied = repair_posting_order_selected(rows=chosen, dry_run=False)
	frappe.db.commit()
	return {"root_id": root["root_id"], "ok": True, "dry_run": False, "applied": applied}


def _apply_leftover_ma(root: dict, *, dry_run: bool) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.leftover_ma import (
		classify_leftover_ma_identity,
	)
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import plan, execute

	c = classify_leftover_ma_identity(root["item"], root["warehouse"])
	if not c.get("eligible"):
		return {"root_id": root["root_id"], "ok": False, "reason": c.get("leftover_ma_status"), "dry_run": dry_run}
	row = {**c, "topic": "LEFTOVER_MA", "item": root["item"], "warehouse": root["warehouse"]}
	if dry_run:
		return {"root_id": root["root_id"], "ok": True, "dry_run": True, "expected": c.get("expected_ma")}
	live = execute(plan(row), dry_run=False)
	frappe.db.commit()
	return {
		"root_id": root["root_id"],
		"ok": live.get("ok"),
		"dry_run": False,
		"state": live.get("primary_state"),
		"riv": "strategy_scoped_only",
	}


def _apply_wrong_rate(root: dict, *, dry_run: bool) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.repair_pipeline import plan, execute

	scan = scan_wrong_rates(company=COMPANY, voucher=root["voucher"], limit=30)
	rows = [r for r in (scan.get("rows") or []) if r.get("eligible")]
	if root.get("item"):
		rows = [r for r in rows if r.get("item") == root["item"]] or rows
	if not rows:
		return {"root_id": root["root_id"], "ok": False, "reason": "no_eligible_row", "dry_run": dry_run}
	row = rows[0]
	if dry_run:
		preview = execute(plan(dict(row)), dry_run=True)
		return {"root_id": root["root_id"], "ok": True, "dry_run": True, "preview_ok": preview.get("ok")}
	live = execute(plan(dict(row)), dry_run=False)
	frappe.db.commit()
	return {"root_id": root["root_id"], "ok": live.get("ok"), "dry_run": False, "state": live.get("primary_state")}


def rebuild_bins_derived() -> dict:
	"""SLE-authoritative Bin sync only. No fabricated inventory."""
	from erpnext_extensions.iran_accounting.historical_stock._validation.prod_ready_0926 import rebuild_bins

	return rebuild_bins()


def dry_run_manifest(manifest_id: str = DEFAULT_MANIFEST_ID) -> dict:
	from erpnext_extensions.iran_accounting.historical_stock.permissions import require_read

	require_read()
	pre = preflight(manifest_id)
	if pre["must_not_mutate"]:
		# Dry run still reports; apply will refuse CHANGED/MISSING/BLOCKED
		pass
	man = _load_locked(manifest_id)
	ops = []
	for root in man["roots"]:
		refuse_if_not_allowlisted(root["root_id"], man)
		status = next(r["status"] for r in pre["rows"] if r["root_id"] == root["root_id"])
		if status != "MATCHED":
			ops.append({"root_id": root["root_id"], "skipped": status})
			continue
		if root["family"] == "POSTING_ORDER":
			ops.append(_apply_posting_order(root, dry_run=True))
		elif root["family"] == "LEFTOVER_MA":
			ops.append(_apply_leftover_ma(root, dry_run=True))
		else:
			ops.append(_apply_wrong_rate(root, dry_run=True))
	out = {
		"manifest_id": manifest_id,
		"allowlist_sha256": man.get("allowlist_sha256"),
		"preflight": pre["counts"],
		"planned_ops": ops,
		"planned_mutate": sum(1 for o in ops if o.get("ok") and o.get("dry_run")),
		"forbidden": list(FORBIDDEN_RIV),
		"version": __version__,
	}
	_jdump(SITE_OUT / "production_dry_run.json", out)
	return out


def apply_manifest(*, manifest_id: str = DEFAULT_MANIFEST_ID, confirm: str | None = None) -> dict:
	"""Mutate only MATCHED allowlisted roots. confirm must equal manifest_id."""
	from erpnext_extensions.iran_accounting.historical_stock.permissions import require_write_if_applying

	require_write_if_applying(False)
	if confirm != manifest_id:
		frappe.throw("APPLY refused: confirm must equal the frozen manifest_id")
	man = _load_locked(manifest_id)
	pre = preflight(manifest_id)
	if pre["must_not_mutate"]:
		frappe.throw(f"APPLY refused: non-MATCHED roots {pre['must_not_mutate'][:8]}")
	baseline = _gates()
	mfg0 = mfg_fingerprint()
	audit = {
		"started_at": str(now_datetime()),
		"manifest_id": manifest_id,
		"allowlist_sha256": man.get("allowlist_sha256"),
		"version": __version__,
		"preflight": pre,
		"baseline": baseline,
		"operations": [],
	}
	# Order: PO → LMA → WR
	order = ["POSTING_ORDER", "LEFTOVER_MA", "WRONG_RATE"]
	for fam in order:
		for root in man["roots"]:
			if root["family"] != fam:
				continue
			refuse_if_not_allowlisted(root["root_id"], man)
			if root["family"] == "POSTING_ORDER":
				res = _apply_posting_order(root, dry_run=False)
			elif root["family"] == "LEFTOVER_MA":
				res = _apply_leftover_ma(root, dry_run=False)
			else:
				res = _apply_wrong_rate(root, dry_run=False)
			audit["operations"].append(res)
			live = _gates()
			md = {k: flt(mfg_fingerprint().get(k)) - flt(mfg0.get(k)) for k in mfg0 if k != "captured_at"}
			extra = None
			if any(abs(v) > 1e-6 for v in md.values()):
				extra = f"mfg quantity changed {md}"
			_hard_stop(f"after_{root['root_id']}", baseline, live, extra)
	bins = rebuild_bins_derived()
	audit["bins"] = bins
	audit["gates_final"] = _gates()
	audit["mfg_delta"] = {
		k: flt(mfg_fingerprint().get(k)) - flt(mfg0.get(k)) for k in mfg0 if k != "captured_at"
	}
	audit["ended_at"] = str(now_datetime())
	_jdump(SITE_OUT / "production_apply_audit.json", audit)
	return audit


def run_locked_rehearsal(*, tag: str, manifest_id: str = DEFAULT_MANIFEST_ID) -> dict:
	"""Clean-restore caller invokes this. Locks fingerprints if missing, dry-runs, applies."""
	from erpnext_extensions.iran_accounting.historical_stock.api import scan_all
	from erpnext_extensions.iran_accounting.historical_stock._validation.prod_ready_0926 import (
		canaries,
		dash_slice,
		verify_backup_identity,
	)

	ident = verify_backup_identity()
	if not ident.get("looks_like_new_backup"):
		frappe.throw("DB is not the 20260926 backup")
	locked_path = SITE_OUT / f"{manifest_id}.locked.json"
	if not locked_path.exists():
		lock_preconditions(manifest_id)
	dry = dry_run_manifest(manifest_id)
	applied = apply_manifest(manifest_id=manifest_id, confirm=manifest_id)
	scan = scan_all(company=COMPANY)
	can = canaries()
	out = {
		"tag": tag,
		"identity": ident,
		"dry_run_mutate": dry.get("planned_mutate"),
		"applied_ok": sum(1 for o in applied.get("operations") or [] if o.get("ok")),
		"applied_n": len(applied.get("operations") or []),
		"gates_final": applied.get("gates_final"),
		"mfg_delta": applied.get("mfg_delta"),
		"dashboard": dash_slice(scan),
		"canaries": {
			"30470_621301": (can.get("30470") or {}).get("uses_621301"),
			"30470_622515": (can.get("30470") or {}).get("uses_622515_for_stock"),
			"28696": (can.get("28696") or {}).get("rates"),
			"33937": (can.get("33937") or {}).get("current_basic_rate"),
		},
	}
	_jdump(SITE_OUT / f"locked_rehearsal_{tag}.json", out)
	return out
