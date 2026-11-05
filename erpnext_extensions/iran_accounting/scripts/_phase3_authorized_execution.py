# Copyright (c) 2026 — Authorized Phase 3 Historical RIV execution (Development only).
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import Any

import frappe
from frappe.utils import flt, now_datetime

from erpnext_extensions.iran_accounting.domain.riv_sabe_integer_phase3 import (
	APPROVED_SCOPES,
	_baseline_dir,
	compare_snapshots,
	create_and_execute_riv,
	snapshot_ledger_state,
	verify_approved_dry_run,
)

SA_PREFIX = "621301"
COITEM = "30200079"
CAMPAIGN_ITEMS = ("13200333", "30300084")
COVERED = {
	"MAT-STE-2026-30392": {"credit": 520.0, "debit": 0.0},
	"MAT-STE-2026-30451": {"credit": 0.0, "debit": 58.0},
}


def _sha256(path: str) -> str:
	h = hashlib.sha256()
	with open(path, "rb") as fh:
		for chunk in iter(lambda: fh.read(1024 * 1024), b""):
			h.update(chunk)
	return h.hexdigest()


def _report_path(name: str) -> str:
	path = os.path.join(_baseline_dir(), name)
	os.makedirs(os.path.dirname(path), exist_ok=True)
	return path


def take_backup() -> dict[str, Any]:
	"""Fresh site backup; return path + checksum + gzip integrity."""
	site = frappe.local.site
	bench = frappe.utils.get_bench_path()
	# Database + site_config (omit --with-files; private/public files not required for ledger restore)
	cmd = ["bench", "--site", site, "backup"]
	proc = subprocess.run(
		cmd,
		cwd=bench,
		capture_output=True,
		text=True,
		timeout=1800,
	)
	if proc.returncode != 0:
		return {
			"ok": False,
			"stdout": (proc.stdout or "")[-2000:],
			"stderr": (proc.stderr or "")[-2000:],
		}
	backup_dir = os.path.join(bench, "sites", site, "private", "backups")
	candidates = sorted(
		[
			os.path.join(backup_dir, f)
			for f in os.listdir(backup_dir)
			if f.endswith("-database.sql.gz")
		],
		key=os.path.getmtime,
	)
	if not candidates:
		return {"ok": False, "error": "no backup file found"}
	path = candidates[-1]
	# gzip integrity
	gz = subprocess.run(["gzip", "-t", path], capture_output=True, text=True)
	digest = _sha256(path)
	meta = {
		"ok": gz.returncode == 0,
		"path": path,
		"sha256": digest,
		"size": os.path.getsize(path),
		"gzip_test": "OK" if gz.returncode == 0 else (gz.stderr or "FAIL"),
		"stdout_tail": (proc.stdout or "")[-500:],
	}
	with open(_report_path("phase3_exec_backup.json"), "w", encoding="utf-8") as fh:
		json.dump(meta, fh, ensure_ascii=False, indent=2)
	return meta


def snapshot_value_difference(tag: str) -> dict[str, Any]:
	"""Snapshot SE value_difference for campaign-related vouchers with fractional SABE."""
	rows = frappe.db.sql(
		"""
		SELECT DISTINCT sabe.voucher_no
		FROM `tabSerial and Batch Entry` sabe
		WHERE sabe.docstatus=1 AND IFNULL(sabe.is_cancelled,0)=0
		  AND sabe.item_code IN %(items)s
		  AND (
			ABS(sabe.incoming_rate-ROUND(sabe.incoming_rate))>1e-6
			OR ABS(sabe.outgoing_rate-ROUND(sabe.outgoing_rate))>1e-6
			OR ABS(sabe.stock_value_difference-ROUND(sabe.stock_value_difference))>1e-6
		  )
		""",
		{"items": CAMPAIGN_ITEMS},
		as_dict=True,
	)
	# Also include known covered vouchers
	vnos = {r.voucher_no for r in rows} | set(COVERED)
	# After integerization fractional set shrinks — also pull from all SE touching items
	extra = frappe.db.sql(
		"""
		SELECT DISTINCT voucher_no FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND item_code IN %(items)s
		""",
		{"items": CAMPAIGN_ITEMS},
	)
	vnos |= {r[0] for r in extra}
	vnos = sorted(v for v in vnos if v and frappe.db.exists("Stock Entry", v))
	data = frappe.db.sql(
		"""
		SELECT name, purpose, value_difference, total_incoming_value, total_outgoing_value
		FROM `tabStock Entry` WHERE name IN %(names)s ORDER BY name
		""",
		{"names": vnos or ["__none__"]},
		as_dict=True,
	)
	h = hashlib.sha256()
	for r in data:
		h.update(
			f"{r.name}|{flt(r.value_difference)}|{flt(r.total_incoming_value)}|{flt(r.total_outgoing_value)}\n".encode()
		)
	payload = {
		"tag": tag,
		"at": str(now_datetime()),
		"count": len(data),
		"fingerprint": h.hexdigest(),
		"rows": data,
	}
	path = _report_path(f"vd_snapshot_{tag}.json")
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
	payload["path"] = path
	return {
		"tag": tag,
		"path": path,
		"count": len(data),
		"fingerprint": payload["fingerprint"],
	}


def snapshot_coitem_30200079_gaps(tag: str) -> dict[str, Any]:
	"""Three documented co-item SED≠SLE gaps — must not change unintentionally."""
	targets = [
		("MAT-STE-2026-25331", "52upiv0brq"),
		("MAT-STE-2026-25414", "9po00st13r"),
		("MAT-STE-2026-40953", "6rgv8olk1k"),
	]
	rows = []
	for vno, sed in targets:
		sed_row = frappe.db.get_value(
			"Stock Entry Detail",
			sed,
			["name", "item_code", "amount", "basic_amount"],
			as_dict=True,
		)
		sle = frappe.db.sql(
			"""
			SELECT name, stock_value_difference, actual_qty
			FROM `tabStock Ledger Entry`
			WHERE voucher_no=%s AND voucher_detail_no=%s AND is_cancelled=0
			LIMIT 1
			""",
			(vno, sed),
			as_dict=True,
		)
		svd = abs(flt(sle[0].stock_value_difference)) if sle else None
		amt = abs(flt(sed_row.amount)) if sed_row else None
		rows.append(
			{
				"voucher_no": vno,
				"sed": sed,
				"item_code": sed_row.item_code if sed_row else None,
				"sed_amount": amt,
				"sle_svd_abs": svd,
				"gap": float(svd - amt) if svd is not None and amt is not None else None,
			}
		)
	payload = {"tag": tag, "at": str(now_datetime()), "rows": rows}
	path = _report_path(f"coitem_30200079_{tag}.json")
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
	return {"tag": tag, "path": path, "rows": rows}


def snapshot_sa_gl(tag: str) -> dict[str, Any]:
	rows = frappe.db.sql(
		"""
		SELECT name, voucher_no, account, debit, credit, remarks, posting_date, is_cancelled
		FROM `tabGL Entry`
		WHERE account LIKE %s AND IFNULL(is_cancelled,0)=0
		  AND voucher_type='Stock Entry'
		  AND voucher_no IN (
			SELECT DISTINCT voucher_no FROM `tabStock Ledger Entry`
			WHERE is_cancelled=0 AND item_code IN ('13200333','30300084')
		  )
		ORDER BY voucher_no, name
		""",
		(SA_PREFIX + "%",),
		as_dict=True,
	)
	# Also force-include 520/58 vouchers
	extra = frappe.db.sql(
		"""
		SELECT name, voucher_no, account, debit, credit, remarks, posting_date, is_cancelled
		FROM `tabGL Entry`
		WHERE account LIKE %s AND IFNULL(is_cancelled,0)=0
		  AND voucher_no IN ('MAT-STE-2026-30392','MAT-STE-2026-30451')
		ORDER BY voucher_no, name
		""",
		(SA_PREFIX + "%",),
		as_dict=True,
	)
	by_name = {r.name: r for r in rows}
	for r in extra:
		by_name[r.name] = r
	rows = sorted(by_name.values(), key=lambda r: (r.voucher_no, r.name))
	h = hashlib.sha256()
	for r in rows:
		h.update(
			f"{r.voucher_no}|{flt(r.debit)}|{flt(r.credit)}|{str(r.remarks or '')[:80]}\n".encode()
		)
	payload = {
		"tag": tag,
		"at": str(now_datetime()),
		"count": len(rows),
		"fingerprint": h.hexdigest(),
		"rows": rows,
		"covered_520_count": sum(
			1 for r in rows if r.voucher_no == "MAT-STE-2026-30392" and abs(flt(r.credit) - 520) < 0.5
		),
		"covered_58_count": sum(
			1 for r in rows if r.voucher_no == "MAT-STE-2026-30451" and abs(flt(r.debit) - 58) < 0.5
		),
	}
	path = _report_path(f"sa_gl_{tag}.json")
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
	return {
		"tag": tag,
		"path": path,
		"count": len(rows),
		"fingerprint": payload["fingerprint"],
		"covered_520_count": payload["covered_520_count"],
		"covered_58_count": payload["covered_58_count"],
		"net_credit_minus_debit": float(
			sum(flt(r.credit) - flt(r.debit) for r in rows)
		),
	}


def validate_gl_balance_for_campaign_vouchers() -> dict[str, Any]:
	vnos = frappe.db.sql(
		"""
		SELECT DISTINCT voucher_no FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND item_code IN %(items)s AND voucher_type='Stock Entry'
		""",
		{"items": CAMPAIGN_ITEMS},
	)
	imbalanced = []
	checked = 0
	for (vno,) in vnos:
		row = frappe.db.sql(
			"""
			SELECT ROUND(SUM(debit),2) d, ROUND(SUM(credit),2) c
			FROM `tabGL Entry`
			WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
			""",
			vno,
		)[0]
		d, c = flt(row[0]), flt(row[1])
		checked += 1
		if abs(d - c) > 1e-9:
			imbalanced.append({"voucher_no": vno, "debit": d, "credit": c, "diff": float(d - c)})
	return {"checked": checked, "imbalanced": imbalanced, "ok": not imbalanced}


def validate_no_a2_round_off() -> dict[str, Any]:
	"""New IRR SABE A2 remarks must not use Round Off as the partner account in GL."""
	# Find SA rows with SABE A2 remarks
	sa = frappe.db.sql(
		"""
		SELECT name, voucher_no, debit, credit, remarks, against
		FROM `tabGL Entry`
		WHERE account LIKE %s AND IFNULL(is_cancelled,0)=0
		  AND (remarks LIKE '%%IRR SABE integer localization%%' OR remarks LIKE '%%IRR_SABE_A2|%%')
		""",
		(SA_PREFIX + "%",),
		as_dict=True,
	)
	round_off = frappe.db.get_value(
		"Company",
		{"name": frappe.db.get_value("Stock Entry", "MAT-STE-2026-42067", "company") or frappe.db.get_single_value("Global Defaults", "default_company")},
		"round_off_account",
	)
	# Also resolve company from first SA row voucher
	issues = []
	for r in sa:
		company = frappe.db.get_value("Stock Entry", r.voucher_no, "company")
		ro = frappe.get_cached_value("Company", company, "round_off_account") if company else round_off
		# Partner may be encoded in against field
		against = str(r.against or "")
		if ro and ro in against:
			# Check if voucher purpose is Manufacture — Round Off against is forbidden
			purpose = frappe.db.get_value("Stock Entry", r.voucher_no, "purpose")
			if purpose in ("Manufacture", "Repack"):
				issues.append(
					{
						"voucher_no": r.voucher_no,
						"gl": r.name,
						"against": against,
						"round_off": ro,
						"purpose": purpose,
					}
				)
	return {"a2_sa_rows": len(sa), "round_off_issues": issues, "ok": not issues, "sample": sa[:20]}


def validate_coverage_not_duplicated() -> dict[str, Any]:
	c520 = frappe.db.sql(
		"""
		SELECT name, debit, credit, remarks FROM `tabGL Entry`
		WHERE voucher_no='MAT-STE-2026-30392' AND account LIKE %s
		  AND IFNULL(is_cancelled,0)=0 AND ROUND(credit)=520
		""",
		(SA_PREFIX + "%",),
		as_dict=True,
	)
	c58 = frappe.db.sql(
		"""
		SELECT name, debit, credit, remarks FROM `tabGL Entry`
		WHERE voucher_no='MAT-STE-2026-30451' AND account LIKE %s
		  AND IFNULL(is_cancelled,0)=0 AND ROUND(debit)=58
		""",
		(SA_PREFIX + "%",),
		as_dict=True,
	)
	# Also detect extra IRR_SABE_A2 on those vouchers totaling same amount
	extra_520 = frappe.db.sql(
		"""
		SELECT name, debit, credit, remarks FROM `tabGL Entry`
		WHERE voucher_no='MAT-STE-2026-30392' AND account LIKE %s
		  AND IFNULL(is_cancelled,0)=0
		  AND (remarks LIKE '%%IRR SABE%%' OR remarks LIKE '%%IRR_SABE_A2|%%')
		""",
		(SA_PREFIX + "%",),
		as_dict=True,
	)
	extra_58 = frappe.db.sql(
		"""
		SELECT name, debit, credit, remarks FROM `tabGL Entry`
		WHERE voucher_no='MAT-STE-2026-30451' AND account LIKE %s
		  AND IFNULL(is_cancelled,0)=0
		  AND (remarks LIKE '%%IRR SABE%%' OR remarks LIKE '%%IRR_SABE_A2|%%')
		""",
		(SA_PREFIX + "%",),
		as_dict=True,
	)
	return {
		"gl_520_count": len(c520),
		"gl_58_count": len(c58),
		"a2_marked_on_30392": len(extra_520),
		"a2_marked_on_30451": len(extra_58),
		"ok": len(c520) == 1 and len(c58) == 1 and len(extra_520) == 0 and len(extra_58) == 0,
		"c520": c520,
		"c58": c58,
	}


def validate_consume_to_zero() -> dict[str, Any]:
	rows = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, voucher_no,
		       qty_after_transaction, stock_value, stock_value_difference
		FROM `tabStock Ledger Entry`
		WHERE is_cancelled=0 AND item_code IN %(items)s
		  AND ABS(qty_after_transaction)<=1e-9 AND actual_qty < 0
		""",
		{"items": CAMPAIGN_ITEMS},
		as_dict=True,
	)
	failures = [
		r
		for r in rows
		if abs(flt(r.qty_after_transaction)) > 1e-9 or abs(flt(r.stock_value)) > 1e-6
	]
	return {"count": len(rows), "failures": failures[:20], "ok": not failures}


def per_scope_validation(scope: dict, tag: str) -> dict[str, Any]:
	gl = validate_gl_balance_for_campaign_vouchers()
	cov = validate_coverage_not_duplicated()
	ro = validate_no_a2_round_off()
	c0 = validate_consume_to_zero()
	frac = frappe.db.sql(
		"""
		SELECT COUNT(*) FROM `tabSerial and Batch Entry` sabe
		WHERE docstatus=1 AND IFNULL(is_cancelled,0)=0
		  AND item_code IN %(items)s
		  AND (
			ABS(incoming_rate-ROUND(incoming_rate))>1e-6
			OR ABS(outgoing_rate-ROUND(outgoing_rate))>1e-6
			OR ABS(stock_value_difference-ROUND(stock_value_difference))>1e-6
		  )
		""",
		{"items": CAMPAIGN_ITEMS},
	)[0][0]
	ok = gl["ok"] and cov["ok"] and ro["ok"] and c0["ok"]
	return {
		"tag": tag,
		"scope": scope,
		"ok": ok,
		"gl_balance": gl,
		"coverage": cov,
		"a2_no_round_off": ro,
		"consume_to_zero": c0,
		"fractional_sabe_remaining": int(frac),
	}


def reconcile_a2_776(pre_sa: dict, post_sa: dict) -> dict[str, Any]:
	"""Reconcile expected outstanding +776 against SA GL on the eight dry-run vouchers.

	After RIV, Core Manufacture remake may book the localization into 621301 with
	generic remarks while Iran A2 markers appear only when in-map SA did not
	already cover the amount. Reconcile by per-voucher SA net *delta* on the
	approved outstanding voucher set (sum must equal +776), and flag duplicates.
	"""
	EXPECTED_LEGS = {
		"MAT-STE-2026-25331": 948.0,
		"MAT-STE-2026-25355": 436.0,
		"MAT-STE-2026-25414": 596.0,
		"MAT-STE-2026-30330-1": 159.0,
		"MAT-STE-2026-30453-1": 13.0,
		"MAT-STE-2026-33476-1": -448.0,
		"MAT-STE-2026-33612-1": -147.0,
		"MAT-STE-2026-40953": -781.0,
	}
	with open(pre_sa["path"], encoding="utf-8") as fh:
		pre = json.load(fh)
	with open(post_sa["path"], encoding="utf-8") as fh:
		post = json.load(fh)
	pre_names = {r["name"] for r in pre["rows"]}
	new_rows = [r for r in post["rows"] if r["name"] not in pre_names]

	def _net_by_voucher(rows):
		out = {}
		for r in rows:
			out[r["voucher_no"]] = out.get(r["voucher_no"], 0.0) + (
				flt(r["credit"]) - flt(r["debit"])
			)
		return out

	pre_by = _net_by_voucher(pre["rows"])
	post_by = _net_by_voucher(post["rows"])
	per_voucher = []
	delta_sum = 0.0
	duplicates = []
	for vno, expected in EXPECTED_LEGS.items():
		# Post-state rows on voucher
		vrows = [r for r in post["rows"] if r["voucher_no"] == vno]
		# Duplicate detection: two credits (or debits) of the same abs expected amount
		same_amt = [
			r
			for r in vrows
			if abs(flt(r["credit"]) - abs(expected)) < 0.5
			or abs(flt(r["debit"]) - abs(expected)) < 0.5
		]
		if abs(expected) >= 0.5 and len(same_amt) > 1:
			duplicates.append({"voucher_no": vno, "rows": same_amt, "expected": expected})
		delta = float(post_by.get(vno, 0.0) - pre_by.get(vno, 0.0))
		# For vouchers that already had unrelated SA pre-RIV, compare post net to expected
		# when pre was nonzero — prefer delta when pre was 0, else check post contains expected
		match_delta = abs(delta - expected) < 0.5
		match_post = abs(post_by.get(vno, 0.0) - expected) < 0.5
		per_voucher.append(
			{
				"voucher_no": vno,
				"expected": expected,
				"pre_net": float(pre_by.get(vno, 0.0)),
				"post_net": float(post_by.get(vno, 0.0)),
				"delta": delta,
				"match_delta": match_delta,
				"match_post_net": match_post,
				"ok": match_delta or match_post,
			}
		)
		delta_sum += delta

	a2_new = [
		r
		for r in new_rows
		if "IRR SABE" in str(r.get("remarks") or "") or "IRR_SABE_A2|" in str(r.get("remarks") or "")
	]
	a2_net = float(sum(flt(r["credit"]) - flt(r["debit"]) for r in a2_new))
	# Economic reconciliation: sum of OK per-voucher matches, or delta_sum ~= 776
	# when pre nets on those vouchers were clean.
	all_ok = all(p["ok"] for p in per_voucher) and not duplicates
	matches_776 = abs(delta_sum - 776.0) < 0.5 or all_ok
	return {
		"expected_outstanding_a2": 776.0,
		"expected_legs": EXPECTED_LEGS,
		"per_voucher": per_voucher,
		"delta_sum_on_expected_vouchers": delta_sum,
		"new_sa_row_count": len(new_rows),
		"new_sa_net_credit_minus_debit": float(
			sum(flt(r["credit"]) - flt(r["debit"]) for r in new_rows)
		),
		"a2_marked_new_count": len(a2_new),
		"a2_marked_net": a2_net,
		"duplicates": duplicates,
		"matches_776": bool(matches_776) and not duplicates,
		"all_expected_vouchers_ok": all_ok,
		"new_rows_sample": new_rows[:30],
		"coverage_520_after": post_sa.get("covered_520_count"),
		"coverage_58_after": post_sa.get("covered_58_count"),
	}


def run(run_tag: str = "authorized_run1") -> dict[str, Any]:
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as bootstrap_apply
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		ensure_iran_riv_gl_handlers_active,
		ensure_iran_riv_recalculate_wrapper_active,
	)
	from erpnext_extensions.iran_accounting.domain.riv_sabe_integer_campaign import (
		dry_run_riv_sabe_integer_campaign,
	)

	bootstrap_apply()
	ensure_iran_riv_recalculate_wrapper_active(bootstrap=False)
	ensure_iran_riv_gl_handlers_active(bootstrap=False)

	# Confirm validation packet exists
	val_path = os.path.join(
		frappe.utils.get_bench_path(),
		"sites",
		frappe.local.site,
		"private",
		"files",
		"iran_riv_sabe_integer_campaign",
		"phase3_baselines",
		"phase3_readonly_validation.json",
	)
	if not os.path.exists(val_path):
		frappe.throw(f"Missing validation packet: {val_path}")
	with open(val_path, encoding="utf-8") as fh:
		validation = json.load(fh)
	if validation.get("verdict") != "READY FOR SEPARATE RIV EXECUTION AUTHORIZATION":
		frappe.throw(f"Validation verdict not ready: {validation.get('verdict')}")

	# 1) Backup
	backup = take_backup()
	if not backup.get("ok"):
		return {"ok": False, "verdict": "BLOCKED — BACKUP FAILED", "backup": backup}

	# 2) Pre fingerprints
	dry_verify = verify_approved_dry_run()
	before = snapshot_ledger_state(f"{run_tag}_before")
	vd_before = snapshot_value_difference(f"{run_tag}_before")
	sa_before = snapshot_sa_gl(f"{run_tag}_before")
	coitem_before = snapshot_coitem_30200079_gaps(f"{run_tag}_before")

	# 3) Sequential scopes
	results = []
	scope_validations = []
	stopped = False
	blocker = None
	for idx, scope in enumerate(APPROVED_SCOPES):
		# Hard assert scope matches validation packet
		val_scope = validation["scopes"][idx]["scope"]
		if (
			scope["item_code"] != val_scope["item_code"]
			or scope["warehouse"] != val_scope["warehouse"]
			or scope["posting_date"] != val_scope["posting_date"]
		):
			return {
				"ok": False,
				"verdict": "BLOCKED — SCOPE DRIFT",
				"expected": scope,
				"validation_scope": val_scope,
			}

		res = create_and_execute_riv(scope, run_tag=run_tag)
		results.append(res)
		if not res.get("ok"):
			stopped = True
			blocker = res
			after_fail = snapshot_ledger_state(f"{run_tag}_after_FAIL_scope{idx+1}")
			report = {
				"ok": False,
				"verdict": "BLOCKED — RIV EXECUTION FAILURE",
				"run_tag": run_tag,
				"backup": backup,
				"dry_verify": dry_verify,
				"before": before,
				"vd_before": vd_before,
				"sa_before": sa_before,
				"coitem_before": coitem_before,
				"results": results,
				"scope_validations": scope_validations,
				"after_fail": after_fail,
				"blocker": blocker,
				"stopped_at_scope_index": idx,
				"restore_required": True,
				"backup_path": backup.get("path"),
				"backup_sha256": backup.get("sha256"),
			}
			path = _report_path(f"phase3_execution_{run_tag}_FAILED.json")
			with open(path, "w", encoding="utf-8") as fh:
				json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
			report["report_path"] = path
			return report

		v = per_scope_validation(scope, f"{run_tag}_after_scope{idx+1}")
		scope_validations.append(v)
		if not v["ok"]:
			stopped = True
			blocker = v
			after_fail = snapshot_ledger_state(f"{run_tag}_after_FAIL_validate_scope{idx+1}")
			report = {
				"ok": False,
				"verdict": "BLOCKED — POST-SCOPE VALIDATION FAILURE",
				"run_tag": run_tag,
				"backup": backup,
				"results": results,
				"scope_validations": scope_validations,
				"after_fail": after_fail,
				"blocker": blocker,
				"stopped_at_scope_index": idx,
				"restore_required": True,
				"backup_path": backup.get("path"),
				"backup_sha256": backup.get("sha256"),
			}
			path = _report_path(f"phase3_execution_{run_tag}_FAILED.json")
			with open(path, "w", encoding="utf-8") as fh:
				json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
			report["report_path"] = path
			return report

	# 4) Post-execution
	after = snapshot_ledger_state(f"{run_tag}_after")
	vd_after = snapshot_value_difference(f"{run_tag}_after")
	sa_after = snapshot_sa_gl(f"{run_tag}_after")
	coitem_after = snapshot_coitem_30200079_gaps(f"{run_tag}_after")
	fp_compare = compare_snapshots(before["path"], after["path"])
	a2_recon = reconcile_a2_776(sa_before, sa_after)

	coitem_unchanged = coitem_before["rows"] == coitem_after["rows"]

	# value_difference changes — document
	with open(vd_before["path"], encoding="utf-8") as fh:
		vd_b = {r["name"]: r for r in json.load(fh)["rows"]}
	with open(vd_after["path"], encoding="utf-8") as fh:
		vd_a = {r["name"]: r for r in json.load(fh)["rows"]}
	vd_changes = []
	for name, row in vd_a.items():
		old = vd_b.get(name)
		if not old:
			continue
		if abs(flt(old["value_difference"]) - flt(row["value_difference"])) >= 0.5:
			vd_changes.append(
				{
					"voucher_no": name,
					"before": flt(old["value_difference"]),
					"after": flt(row["value_difference"]),
					"delta": float(flt(row["value_difference"]) - flt(old["value_difference"])),
					"purpose": row.get("purpose"),
				}
			)

	# Post dry-runs (read-only, twice for determinism)
	post_dry1 = dry_run_riv_sabe_integer_campaign(item_codes=list(CAMPAIGN_ITEMS))
	post_dry2 = dry_run_riv_sabe_integer_campaign(item_codes=list(CAMPAIGN_ITEMS))
	post_dry_det = {
		"fractional_equal": post_dry1["fractional_sabe_count"] == post_dry2["fractional_sabe_count"],
		"proposed_equal": post_dry1["proposed_a2_net"] == post_dry2["proposed_a2_net"],
		"outstanding_equal": post_dry1["outstanding_a2_net_after_dedupe"]
		== post_dry2["outstanding_a2_net_after_dedupe"],
		"ok": True,
	}
	post_dry_det["ok"] = (
		post_dry_det["fractional_equal"]
		and post_dry_det["proposed_equal"]
		and post_dry_det["outstanding_equal"]
	)

	final_gl = validate_gl_balance_for_campaign_vouchers()
	final_cov = validate_coverage_not_duplicated()
	final_ro = validate_no_a2_round_off()
	final_c0 = validate_consume_to_zero()

	ok = all(
		[
			all(r.get("ok") for r in results),
			all(v.get("ok") for v in scope_validations),
			final_gl["ok"],
			final_cov["ok"],
			final_ro["ok"],
			final_c0["ok"],
			coitem_unchanged,
			post_dry_det["ok"],
			bool(a2_recon.get("matches_776")),
			sa_after.get("covered_520_count") == 1,
			sa_after.get("covered_58_count") == 1,
		]
	)

	verdict = (
		"PHASE 3 RIV COMPLETED — RECONCILED"
		if ok
		else "BLOCKED — POST-EXECUTION RECONCILIATION FAILURE"
	)

	report = {
		"verdict": verdict,
		"ok": ok,
		"run_tag": run_tag,
		"at": str(now_datetime()),
		"authorization": "User-authorized Phase 3 Historical RIV — three approved scopes only",
		"boundary": "MAT-STE-2026-43536 NOT submitted; Production NOT touched",
		"backup": backup,
		"dry_verify": dry_verify,
		"before": before,
		"after": after,
		"fingerprint_compare": fp_compare,
		"vd_before": vd_before,
		"vd_after": vd_after,
		"vd_changes": vd_changes,
		"sa_before": sa_before,
		"sa_after": sa_after,
		"a2_776_reconciliation": a2_recon,
		"coitem_before": coitem_before,
		"coitem_after": coitem_after,
		"coitem_30200079_unchanged": coitem_unchanged,
		"results": results,
		"scope_validations": scope_validations,
		"final_gl_balance": final_gl,
		"final_coverage": final_cov,
		"final_a2_no_round_off": final_ro,
		"final_consume_to_zero": final_c0,
		"post_dry_run": {
			"fractional_sabe_count": post_dry1["fractional_sabe_count"],
			"proposed_a2_net": post_dry1["proposed_a2_net"],
			"outstanding_a2_net_after_dedupe": post_dry1["outstanding_a2_net_after_dedupe"],
			"path": post_dry1.get("audit_path"),
			"determinism": post_dry_det,
		},
		"target_43536": "NOT_TOUCHED",
	}
	path = _report_path(f"phase3_execution_{run_tag}.json")
	with open(path, "w", encoding="utf-8") as fh:
		json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
	# Slim return
	return {
		"verdict": verdict,
		"ok": ok,
		"report_path": path,
		"backup_path": backup.get("path"),
		"backup_sha256": backup.get("sha256"),
		"riv_results": [
			{"riv": r.get("riv"), "scope": r.get("scope"), "status": r.get("status"), "ok": r.get("ok")}
			for r in results
		],
		"fractional_before": before.get("fractional_sabe_count"),
		"fractional_after": after.get("fractional_sabe_count"),
		"a2_776_recon": {
			"matches_776": a2_recon.get("matches_776"),
			"new_sa_net": a2_recon.get("new_sa_net_credit_minus_debit"),
			"a2_marked_net": a2_recon.get("a2_marked_net"),
			"new_row_count": a2_recon.get("new_sa_row_count"),
		},
		"coverage_520_58": {
			"520": sa_after.get("covered_520_count"),
			"58": sa_after.get("covered_58_count"),
			"ok": final_cov.get("ok"),
		},
		"gl_balanced": final_gl.get("ok"),
		"coitem_unchanged": coitem_unchanged,
		"vd_change_count": len(vd_changes),
		"post_dry_outstanding": post_dry1["outstanding_a2_net_after_dedupe"],
		"post_dry_fractional": post_dry1["fractional_sabe_count"],
		"post_dry_determinism": post_dry_det.get("ok"),
	}
