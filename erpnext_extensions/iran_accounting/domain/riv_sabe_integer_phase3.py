# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 3 controlled RIV execution for SABE integerization (Development only)."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import frappe
from frappe.utils import cint, flt, now_datetime

APPROVED_DRY_RUN = (
	"iran_riv_sabe_integer_campaign/riv_dry_run_20261105_011853.json"
)

APPROVED_SCOPES = [
	{
		"item_code": "30300084",
		"warehouse": "انبار Quarantine محصول نیمه ساخته اسپاد",
		"posting_date": "2026-06-16",
	},
	{
		"item_code": "13200333",
		"warehouse": "انبار پایکار خط تولید اسپاد فارمد",
		"posting_date": "2026-08-10",
	},
	{
		"item_code": "13200333",
		"warehouse": "انبار approved اقلام بسته بندی ثانویه اسپاد",
		"posting_date": "2026-08-10",
	},
]


def _site_files(*parts: str) -> str:
	path = os.path.join(
		frappe.utils.get_bench_path(),
		"sites",
		frappe.local.site,
		"private",
		"files",
		*parts,
	)
	os.makedirs(os.path.dirname(path) if not path.endswith("/") else path, exist_ok=True)
	return path


def _baseline_dir() -> str:
	path = _site_files("iran_riv_sabe_integer_campaign", "phase3_baselines")
	os.makedirs(path, exist_ok=True)
	return path


def _sha256_file(path: str) -> str:
	h = hashlib.sha256()
	with open(path, "rb") as fh:
		for chunk in iter(lambda: fh.read(1024 * 1024), b""):
			h.update(chunk)
	return h.hexdigest()


def verify_approved_dry_run() -> dict[str, Any]:
	path = _site_files(APPROVED_DRY_RUN)
	if not os.path.exists(path):
		frappe.throw(f"Approved dry-run missing: {path}")
	digest = _sha256_file(path)
	with open(path, encoding="utf-8") as fh:
		data = json.load(fh)
	scopes = [
		{
			"item_code": r["item_code"],
			"warehouse": r["warehouse"],
			"posting_date": r["posting_date"],
		}
		for r in data.get("riv_documents_projected") or []
	]
	if scopes != APPROVED_SCOPES:
		frappe.throw(
			f"Dry-run RIV scopes drifted from approved constant.\n"
			f"file={scopes}\napproved={APPROVED_SCOPES}"
		)
	return {
		"path": path,
		"sha256": digest,
		"fractional_sabe_count": data.get("fractional_sabe_count"),
		"proposed_a2_net": data.get("proposed_a2_net"),
		"outstanding_a2_net_after_dedupe": data.get("outstanding_a2_net_after_dedupe"),
		"scopes": scopes,
	}


def snapshot_ledger_state(tag: str) -> dict[str, Any]:
	"""Capture SABE/SLE/Bin/GL baselines for the campaign items."""
	items = ("13200333", "30300084")
	sabe = frappe.db.sql(
		"""
		SELECT name, parent, item_code, batch_no, warehouse, qty,
		       incoming_rate, outgoing_rate, stock_value_difference,
		       voucher_type, voucher_no, posting_datetime
		FROM `tabSerial and Batch Entry`
		WHERE docstatus=1 AND IFNULL(is_cancelled,0)=0
		  AND item_code IN %(items)s
		ORDER BY item_code, posting_datetime, creation, idx
		""",
		{"items": items},
		as_dict=True,
	)
	fractional = []
	for r in sabe:
		for field in ("incoming_rate", "outgoing_rate", "stock_value_difference"):
			v = flt(r.get(field))
			if abs(v - round(v)) > 1e-6:
				fractional.append(r.name)
				break
	bins = frappe.db.sql(
		"""
		SELECT item_code, warehouse, actual_qty, stock_value, valuation_rate
		FROM `tabBin`
		WHERE item_code IN %(items)s
		ORDER BY item_code, warehouse
		""",
		{"items": items},
		as_dict=True,
	)
	# SLE monetary fingerprint for scoped warehouses
	sle = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, voucher_no, actual_qty,
		       qty_after_transaction, stock_value, stock_value_difference,
		       valuation_rate, incoming_rate, outgoing_rate, posting_datetime
		FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND item_code IN %(items)s
		  AND warehouse IN %(wh)s
		ORDER BY item_code, warehouse, posting_datetime, creation
		""",
		{
			"items": items,
			"wh": tuple({s["warehouse"] for s in APPROVED_SCOPES}),
		},
		as_dict=True,
	)
	sa_gl = frappe.db.sql(
		"""
		SELECT name, voucher_type, voucher_no, account, debit, credit,
		       is_cancelled, posting_date, remarks
		FROM `tabGL Entry`
		WHERE IFNULL(is_cancelled,0)=0
		  AND account LIKE '621301%%'
		  AND voucher_no IN (
		    SELECT DISTINCT voucher_no FROM `tabSerial and Batch Entry`
		    WHERE docstatus=1 AND IFNULL(is_cancelled,0)=0
		      AND item_code IN %(items)s
		  )
		ORDER BY voucher_no, name
		""",
		{"items": items},
		as_dict=True,
	)
	covered = frappe.db.sql(
		"""
		SELECT name, voucher_no, debit, credit, is_cancelled, posting_date
		FROM `tabGL Entry`
		WHERE voucher_no IN ('MAT-STE-2026-30392','MAT-STE-2026-30451')
		  AND account LIKE '621301%%'
		ORDER BY voucher_no, name
		""",
		as_dict=True,
	)
	payload = {
		"at": str(now_datetime()),
		"tag": tag,
		"sabe_count": len(sabe),
		"fractional_sabe_names": fractional,
		"fractional_sabe_count": len(set(fractional)),
		"bins": bins,
		"sle_count": len(sle),
		"sle_fingerprint": _fingerprint_rows(
			sle,
			[
				"name",
				"stock_value",
				"stock_value_difference",
				"valuation_rate",
				"incoming_rate",
				"outgoing_rate",
				"qty_after_transaction",
				"actual_qty",
			],
		),
		"sabe_fingerprint": _fingerprint_rows(
			sabe,
			["name", "incoming_rate", "outgoing_rate", "stock_value_difference", "qty"],
		),
		"bin_fingerprint": _fingerprint_rows(
			bins, ["item_code", "warehouse", "actual_qty", "stock_value", "valuation_rate"]
		),
		"sa_gl": sa_gl,
		"covered_520_58": covered,
		"sabe_rows": sabe,
		"sle_rows": sle,
	}
	out = os.path.join(_baseline_dir(), f"snapshot_{tag}.json")
	with open(out, "w", encoding="utf-8") as fh:
		json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
	payload["path"] = out
	# Keep response smaller for execute return
	slim = {
		"at": payload["at"],
		"tag": tag,
		"path": out,
		"sabe_count": payload["sabe_count"],
		"fractional_sabe_count": payload["fractional_sabe_count"],
		"sle_count": payload["sle_count"],
		"bin_fingerprint": payload["bin_fingerprint"],
		"sabe_fingerprint": payload["sabe_fingerprint"],
		"sle_fingerprint": payload["sle_fingerprint"],
		"covered_520_58": covered,
		"fractional_sabe_names_sample": fractional[:20],
	}
	return slim


def _fingerprint_rows(rows: list[dict], fields: list[str]) -> str:
	h = hashlib.sha256()
	for r in rows:
		parts = []
		for f in fields:
			v = r.get(f)
			if isinstance(v, float):
				parts.append(f"{round(v, 6)}")
			else:
				parts.append(str(v if v is not None else ""))
		h.update(("|".join(parts) + "\n").encode())
	return h.hexdigest()


def create_and_execute_riv(scope: dict[str, str], *, run_tag: str) -> dict[str, Any]:
	"""Insert+submit+execute one Item+Warehouse RIV for the approved scope.

	Must NOT bind ``execute_reposting_entry`` at import time — that freezes the
	pre-bootstrap Core callable and bypasses ``riv_execute_guard`` (MAT-STE-2026-42067
	Debit/Credit −1 under vanilla Stock Entry allowance 0.5).
	"""
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as bootstrap_apply
	from erpnext_extensions.iran_accounting.integration.riv_execute_guard import (
		execute_reposting_entry,
	)
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		ensure_iran_riv_recalculate_wrapper_active,
		ensure_iran_riv_gl_handlers_active,
	)

	bootstrap_apply()
	ensure_iran_riv_recalculate_wrapper_active(bootstrap=False)
	ensure_iran_riv_gl_handlers_active(bootstrap=False)

	riv = frappe.new_doc("Repost Item Valuation")
	riv.based_on = "Item and Warehouse"
	riv.item_code = scope["item_code"]
	riv.warehouse = scope["warehouse"]
	riv.posting_date = scope["posting_date"]
	riv.posting_time = "00:00:01"
	riv.allow_negative_stock = 1
	riv.company = frappe.db.get_value("Warehouse", scope["warehouse"], "company")
	riv.flags.ignore_permissions = True
	riv.insert()
	riv.submit()
	frappe.db.commit()
	try:
		execute_reposting_entry(riv.name, continue_reposting=False)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		# Persist failure status if Core already wrote it
		frappe.db.commit()
		status = frappe.db.get_value(
			"Repost Item Valuation", riv.name, ["status", "error_log"], as_dict=True
		)
		return {
			"riv": riv.name,
			"scope": scope,
			"run_tag": run_tag,
			"status": status.status if status else "Failed",
			"error_log": (status.error_log or "")[:2000] if status else frappe.get_traceback(),
			"ok": False,
		}
	status = frappe.db.get_value(
		"Repost Item Valuation", riv.name, ["status", "error_log"], as_dict=True
	)
	ok = bool(status and status.status == "Completed")
	return {
		"riv": riv.name,
		"scope": scope,
		"run_tag": run_tag,
		"status": status.status if status else None,
		"error_log": ((status.error_log or "")[:2000] if status else None),
		"ok": ok,
	}


def execute_approved_campaign(run_tag: str = "run1") -> dict[str, Any]:
	"""Execute exactly the three approved RIV scopes. Stops on first failure."""
	dry = verify_approved_dry_run()
	before = snapshot_ledger_state(f"{run_tag}_before")
	results = []
	for scope in APPROVED_SCOPES:
		res = create_and_execute_riv(scope, run_tag=run_tag)
		results.append(res)
		if not res.get("ok"):
			after_fail = snapshot_ledger_state(f"{run_tag}_after_FAIL")
			return {
				"ok": False,
				"run_tag": run_tag,
				"dry_run": dry,
				"before": before,
				"after": after_fail,
				"results": results,
				"stopped": True,
				"blocker": res,
			}
	after = snapshot_ledger_state(f"{run_tag}_after")
	report = {
		"ok": True,
		"run_tag": run_tag,
		"at": str(now_datetime()),
		"dry_run": dry,
		"before": before,
		"after": after,
		"results": results,
		"stopped": False,
	}
	out = os.path.join(_baseline_dir(), f"campaign_{run_tag}.json")
	with open(out, "w", encoding="utf-8") as fh:
		json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
	report["path"] = out
	return report


def compare_snapshots(path_a: str, path_b: str) -> dict[str, Any]:
	with open(path_a, encoding="utf-8") as fh:
		a = json.load(fh)
	with open(path_b, encoding="utf-8") as fh:
		b = json.load(fh)
	return {
		"sabe_fingerprint_equal": a["sabe_fingerprint"] == b["sabe_fingerprint"],
		"sle_fingerprint_equal": a["sle_fingerprint"] == b["sle_fingerprint"],
		"bin_fingerprint_equal": a["bin_fingerprint"] == b["bin_fingerprint"],
		"fractional_before": a["fractional_sabe_count"],
		"fractional_after": b["fractional_sabe_count"],
		"sabe_fp_a": a["sabe_fingerprint"],
		"sabe_fp_b": b["sabe_fingerprint"],
		"sle_fp_a": a["sle_fingerprint"],
		"sle_fp_b": b["sle_fingerprint"],
		"bin_fp_a": a["bin_fingerprint"],
		"bin_fp_b": b["bin_fingerprint"],
	}
