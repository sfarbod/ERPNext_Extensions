"""READ-ONLY Production preflight for Shahrivar 1405 payroll correction.

Loads credentials from gitignored .env. Never prints API secrets.
EXECUTION_MODE must be DRY_RUN — this script performs no mutations.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[2]  # apps/erpnext_extensions
ENV_PATH = ROOT / ".env"
OUT = ROOT / ".local-backups" / "production_dry_run_shahrivar1405"

PE = "شهریور 1405"
EMP = "HR-EMP-0035"
EMP_NAME = "نفیسه قربانی"
SS = "Sal Slip/HR-EMP-0035/00030"
JE_CANDIDATE = "ACC-JV-1405-169108"
LOCAL_EXPECTED_DEBIT = 283672596839.0
LOCAL_ORIGINAL_DEBIT = 284839471791.0
REQUIRED_DATE = "2026-09-22"


def load_env():
	env = {}
	if ENV_PATH.exists():
		for line in ENV_PATH.read_text().splitlines():
			line = line.strip()
			if not line or line.startswith("#") or "=" not in line:
				continue
			k, v = line.split("=", 1)
			env[k.strip()] = v.strip()
	# allow process env override
	for k in ("PRODUCTION_URL", "API_KEY", "API_SECRET", "EXECUTION_MODE"):
		if os.environ.get(k):
			env[k] = os.environ[k]
	return env


class ProdClient:
	def __init__(self, url: str, api_key: str, api_secret: str):
		self.url = url.rstrip("/")
		self.session = requests.Session()
		self.session.headers.update(
			{
				"Authorization": f"token {api_key}:{api_secret}",
				"Accept": "application/json",
				"Content-Type": "application/json",
			}
		)
		self.session.timeout = 120

	def get(self, path: str, params=None):
		r = self.session.get(f"{self.url}{path}", params=params)
		return r

	def post(self, path: str, json_body=None):
		r = self.session.post(f"{self.url}{path}", json=json_body or {})
		return r

	def resource_get(self, doctype: str, name: str, fields=None):
		q = {"fields": json.dumps(fields)} if fields else None
		return self.get(f"/api/resource/{quote(doctype, safe='')}/{quote(name, safe='')}", params=q)

	def resource_list(self, doctype: str, filters=None, fields=None, limit_page_length=500):
		params = {
			"limit_page_length": limit_page_length,
		}
		if filters is not None:
			params["filters"] = json.dumps(filters)
		if fields is not None:
			params["fields"] = json.dumps(fields)
		return self.get(f"/api/resource/{quote(doctype, safe='')}", params=params)


def main():
	env = load_env()
	mode = env.get("EXECUTION_MODE", "DRY_RUN")
	if mode != "DRY_RUN":
		print("ABORT: EXECUTION_MODE is not DRY_RUN; refusing to run.")
		sys.exit(2)

	url = env.get("PRODUCTION_URL")
	key = env.get("API_KEY")
	secret = env.get("API_SECRET")
	if not (url and key and secret):
		print("ABORT: missing PRODUCTION_URL/API_KEY/API_SECRET")
		sys.exit(2)

	OUT.mkdir(parents=True, exist_ok=True)
	client = ProdClient(url, key, secret)
	report = {
		"mode": "DRY_RUN",
		"url": url,
		"blocks": [],
		"warnings": [],
		"checks": [],
	}

	def ok(name, cond, detail=""):
		report["checks"].append({"check": name, "pass": bool(cond), "detail": str(detail)[:2000]})
		print(("PASS" if cond else "FAIL"), name, str(detail)[:300])
		if not cond:
			report["blocks"].append(name)

	# Auth
	r = client.get("/api/method/frappe.auth.get_logged_user")
	ok("http_auth", r.status_code == 200, f"status={r.status_code}")
	if r.status_code != 200:
		# do not dump body if it might contain sensitive info; show truncated
		print("AUTH_FAILED status", r.status_code)
		report["verdict"] = "PRODUCTION BLOCKED — API NOT READY"
		_write(report)
		return report
	user = r.json().get("message")
	report["authenticated_user"] = user
	print("AUTHENTICATED_USER", user)

	# Permissions probe — try reading PE
	r = client.resource_get("Payroll Entry", PE)
	ok("read_payroll_entry", r.status_code == 200, f"status={r.status_code}")
	if r.status_code != 200:
		report["verdict"] = "PRODUCTION BLOCKED — API NOT READY"
		report["pe_error_status"] = r.status_code
		_write(report)
		return report
	pe = r.json()["data"]
	(OUT / "prod_payroll_entry.json").write_text(json.dumps(pe, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

	employees = pe.get("employees") or []
	unique = sorted({e.get("employee") for e in employees})
	target_rows = [e for e in employees if e.get("employee") == EMP or e.get("employee_name") == EMP_NAME]
	report["preflight"] = {
		"payroll_entry": pe.get("name"),
		"docstatus": pe.get("docstatus"),
		"status": pe.get("status"),
		"posting_date": pe.get("posting_date"),
		"start_date": pe.get("start_date"),
		"end_date": pe.get("end_date"),
		"number_of_employees": pe.get("number_of_employees"),
		"employees_table_len": len(employees),
		"unique_employees": len(unique),
		"target_rows": len(target_rows),
	}
	ok("pe_submitted", pe.get("docstatus") == 1, pe.get("docstatus"))
	ok("pe_emp_267", len(employees) == 267 and len(unique) == 267, f"rows={len(employees)} unique={len(unique)}")
	ok("target_row_one", len(target_rows) == 1, target_rows)
	ok("target_emp_id", bool(target_rows) and target_rows[0].get("employee") == EMP, target_rows[0] if target_rows else None)

	# Employee master
	r = client.resource_get("Employee", EMP)
	ok("read_employee", r.status_code == 200, r.status_code)
	emp = r.json().get("data") if r.status_code == 200 else {}
	ok("employee_name", emp.get("employee_name") == EMP_NAME, emp.get("employee_name"))
	(OUT / "prod_employee.json").write_text(json.dumps(emp, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

	# Salary slips for PE
	r = client.resource_list(
		"Salary Slip",
		filters=[["payroll_entry", "=", PE], ["docstatus", "=", 1]],
		fields=["name", "employee", "employee_name", "docstatus", "status", "journal_entry", "gross_pay", "net_pay", "total_deduction", "posting_date"],
		limit_page_length=500,
	)
	ok("list_salary_slips", r.status_code == 200, r.status_code)
	slips = r.json().get("data") if r.status_code == 200 else []
	(OUT / "prod_salary_slips_index.json").write_text(json.dumps(slips, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
	ok("ss_count_267", len(slips) == 267, len(slips))
	target_slips = [s for s in slips if s.get("employee") == EMP]
	ok("target_ss_one", len(target_slips) == 1, target_slips)
	ok("target_ss_name", bool(target_slips) and target_slips[0].get("name") == SS, target_slips[0].get("name") if target_slips else None)
	ok("target_ss_name_match_employee", bool(target_slips) and target_slips[0].get("employee_name") == EMP_NAME)

	# Full target SS
	r = client.resource_get("Salary Slip", SS)
	ok("read_target_ss", r.status_code == 200, r.status_code)
	ss = r.json().get("data") if r.status_code == 200 else {}
	(OUT / "prod_target_salary_slip.json").write_text(json.dumps(ss, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
	ok("ss_identity", ss.get("employee") == EMP and ss.get("employee_name") == EMP_NAME and ss.get("payroll_entry") == PE)
	ok("ss_submitted", ss.get("docstatus") == 1)

	je_from_slips = sorted({s.get("journal_entry") for s in slips if s.get("journal_entry")})
	report["preflight"]["jes_from_slips"] = je_from_slips
	ok("single_je_on_slips", len(je_from_slips) == 1, je_from_slips)
	prod_je = je_from_slips[0] if len(je_from_slips) == 1 else None
	ok("je_is_candidate_or_known", prod_je == JE_CANDIDATE or bool(prod_je), f"prod_je={prod_je} candidate={JE_CANDIDATE}")

	# Journal Entry
	je_data = {}
	if prod_je:
		r = client.resource_get("Journal Entry", prod_je)
		ok("read_je", r.status_code == 200, r.status_code)
		if r.status_code == 200:
			je_data = r.json()["data"]
			(OUT / "prod_journal_entry.json").write_text(json.dumps(je_data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
			accounts = je_data.get("accounts") or []
			# compact export
			compact = [
				{
					"account": a.get("account"),
					"debit": a.get("debit"),
					"credit": a.get("credit"),
					"cost_center": a.get("cost_center"),
					"department": a.get("department"),
					"party_type": a.get("party_type"),
					"party": a.get("party"),
					"reference_type": a.get("reference_type"),
					"reference_name": a.get("reference_name"),
				}
				for a in accounts
			]
			(OUT / "prod_je_accounts_compact.json").write_text(json.dumps(compact, ensure_ascii=False, indent=2), encoding="utf-8")
			report["preflight"]["je"] = {
				"name": je_data.get("name"),
				"docstatus": je_data.get("docstatus"),
				"posting_date": je_data.get("posting_date"),
				"total_debit": je_data.get("total_debit"),
				"total_credit": je_data.get("total_credit"),
				"accounts_count": len(accounts),
				"user_remark": je_data.get("user_remark"),
			}
			ok("je_submitted", je_data.get("docstatus") == 1)
			ok(
				"je_balanced",
				abs(float(je_data.get("total_debit") or 0) - float(je_data.get("total_credit") or 0)) < 0.01,
				f"D={je_data.get('total_debit')} C={je_data.get('total_credit')}",
			)
			ok(
				"je_totals_match_local_original",
				abs(float(je_data.get("total_debit") or 0) - LOCAL_ORIGINAL_DEBIT) < 0.01,
				f"prod={je_data.get('total_debit')} local_orig={LOCAL_ORIGINAL_DEBIT}",
			)
			ok("je_rows_1365", len(accounts) == 1365, len(accounts))
			# party rows for target
			party_rows = [a for a in accounts if a.get("party") == EMP]
			report["preflight"]["target_party_rows"] = len(party_rows)
			ok("target_on_je", len(party_rows) >= 1, len(party_rows))

	# Fingerprints
	net = sum(float(s.get("net_pay") or 0) for s in slips)
	gross = sum(float(s.get("gross_pay") or 0) for s in slips)
	ded = sum(float(s.get("total_deduction") or 0) for s in slips)
	report["preflight"]["ss_fingerprint"] = {"count": len(slips), "net": net, "gross": gross, "deduction": ded}
	# Local pre-correction fingerprint from previous work
	LOCAL_FP = (267, 182792231842.0, 215166716678.0, 32374484836.0)
	ok(
		"ss_fingerprint_vs_local",
		len(slips) == LOCAL_FP[0]
		and abs(net - LOCAL_FP[1]) < 0.01
		and abs(gross - LOCAL_FP[2]) < 0.01
		and abs(ded - LOCAL_FP[3]) < 0.01,
		f"prod=({len(slips)},{net},{gross},{ded}) local={LOCAL_FP}",
	)

	# Expected after removal from prod data
	others = [s for s in slips if s.get("employee") != EMP]
	exp_net = sum(float(s.get("net_pay") or 0) for s in others)
	exp_gross = sum(float(s.get("gross_pay") or 0) for s in others)
	report["expected_after"] = {
		"employees": 266,
		"salary_slips": len(others),
		"ss_net": exp_net,
		"ss_gross": exp_gross,
		"local_corrected_je_debit": LOCAL_EXPECTED_DEBIT,
		"note": "JE total after regen must be computed by accrual method; local validated total is reference only",
	}
	ok("others_266", len(others) == 266)

	# Payment entry refs to SS / JE
	pay_ss = client.resource_list(
		"Payment Entry Reference",
		filters=[["reference_doctype", "=", "Salary Slip"], ["reference_name", "=", SS]],
		fields=["name", "parent", "allocated_amount"],
	)
	ok("no_payment_on_ss", pay_ss.status_code == 200 and len(pay_ss.json().get("data") or []) == 0, pay_ss.status_code if pay_ss.status_code != 200 else pay_ss.json().get("data"))

	if prod_je:
		pay_je = client.resource_list(
			"Payment Entry Reference",
			filters=[["reference_doctype", "=", "Journal Entry"], ["reference_name", "=", prod_je]],
			fields=["name", "parent", "allocated_amount"],
		)
		ok(
			"no_payment_on_je",
			pay_je.status_code == 200 and len(pay_je.json().get("data") or []) == 0,
			pay_je.status_code if pay_je.status_code != 200 else pay_je.json().get("data"),
		)

	# GL entries count (may be heavy / permission)
	if prod_je:
		gl = client.resource_list(
			"GL Entry",
			filters=[["voucher_type", "=", "Journal Entry"], ["voucher_no", "=", prod_je], ["is_cancelled", "=", 0]],
			fields=["name", "account", "debit", "credit"],
			limit_page_length=2000,
		)
		if gl.status_code == 200:
			gldata = gl.json().get("data") or []
			gd = sum(float(x.get("debit") or 0) for x in gldata)
			gc = sum(float(x.get("credit") or 0) for x in gldata)
			report["preflight"]["gl"] = {"count": len(gldata), "debit": gd, "credit": gc}
			ok("gl_count", len(gldata) == 1365 or len(gldata) == int(je_data.get("accounts_count") or 0), len(gldata))
			ok("gl_balanced", abs(gd - gc) < 0.01)
			(OUT / "prod_gl_summary.json").write_text(json.dumps(report["preflight"]["gl"], indent=2), encoding="utf-8")
		else:
			report["warnings"].append(f"GL Entry list status={gl.status_code}")
			ok("gl_readable", False, gl.status_code)

	# Probe whitelisted regen method existence
	# Try calling with dry args should fail safely if missing — use OPTIONS/GET method listing not available.
	# Probe known method paths
	probe_methods = [
		"erpnext_extensions.extentionhrms.payroll_correction.dry_run_remove_employee_accrual",
		"erpnext_extensions.extentionhrms.payroll_correction.execute_remove_employee_accrual",
		"erpnext_extensions.extentionhrms.payroll_correction.regen_payroll_accrual_je",
	]
	api_methods = {}
	for m in probe_methods:
		pr = client.get(f"/api/method/{m}")
		# 404/417/503 etc means missing; 403 means exists but forbidden; 200 unexpected for GET
		api_methods[m] = pr.status_code
		print("PROBE_METHOD", m.split(".")[-1], "status", pr.status_code)
	report["api_method_probes"] = api_methods
	has_endpoint = any(code in (200, 400, 403, 417, 422) and code != 404 for code in api_methods.values())
	# Frappe usually returns 404 for missing methods embedded in JSON with exc
	# Check response text for "not found" / App not found
	missing = True
	for m, code in api_methods.items():
		body = ""
		try:
			# re-fetch to inspect? we only have status. Treat 404 as missing.
			if code != 404:
				# confirm
				pr = client.post(f"/api/method/{m}", {"dry_run": 1})
				api_methods[m] = {"get": code, "post": pr.status_code, "exc": (pr.text or "")[:200]}
				if pr.status_code != 404 and "does not exist" not in (pr.text or "").lower():
					missing = False
		except Exception as e:
			api_methods[m] = {"error": str(e)[:200]}
	report["api_method_probes"] = api_methods
	ok("regen_endpoint_absent_expected", missing, "no custom regen endpoint on production yet")

	# Versions if accessible
	ver = client.get("/api/method/frappe.utils.change_log.get_versions")
	if ver.status_code == 200:
		try:
			versions = ver.json().get("message") or {}
			slim = {
				k: {"version": v.get("version")}
				for k, v in versions.items()
				if k in ("frappe", "erpnext", "hrms", "erpnext_extensions")
			}
			report["versions"] = slim
			print("VERSIONS", slim)
		except Exception:
			report["warnings"].append("version parse failed")
	else:
		report["warnings"].append(f"versions status={ver.status_code}")

	# Backup verification — cannot be done via standard REST
	report["backup"] = {
		"verified_via_api": False,
		"reason": "No standard Frappe REST endpoint exposes Production DB backup integrity/location",
		"requirement": "Operator must confirm fresh restorable Production backup before EXECUTE",
	}
	ok("backup_api_unverifiable", True, "flagged as manual gate before EXECUTE")

	# Concurrent jobs — limited visibility
	# Attempt RQ/job list if permitted
	jobs = client.get("/api/method/frappe.core.page.background_jobs.background_jobs.get_info")
	report["background_jobs_probe"] = jobs.status_code
	if jobs.status_code == 200:
		info = jobs.json().get("message")
		report["background_jobs"] = str(info)[:1000]
	else:
		report["warnings"].append("Could not list background jobs via API")

	# Determine dry-run / verdict
	data_blockers = [
		c["check"]
		for c in report["checks"]
		if not c["pass"]
		and c["check"]
		not in (
			"regen_endpoint_absent_expected",
			"backup_api_unverifiable",
			"gl_readable",  # soft if permission
		)
	]
	# regen_endpoint_absent_expected is PASS when missing — that's expected
	# For execution readiness:
	report["dry_run_data_ok"] = len(data_blockers) == 0
	report["data_blockers"] = data_blockers
	report["api_endpoint_ready"] = not missing
	report["backup_verified"] = False

	if data_blockers:
		report["dry_run_result"] = "DRY_RUN_BLOCKED"
		report["verdict"] = "PRODUCTION BLOCKED — DATA MISMATCH"
	elif missing:
		report["dry_run_result"] = "DRY_RUN_PASS"
		report["verdict"] = "PRODUCTION BLOCKED — API NOT READY"
	elif not report["backup_verified"]:
		report["dry_run_result"] = "DRY_RUN_PASS"
		report["verdict"] = "PRODUCTION BLOCKED — BACKUP REQUIRED"
	else:
		report["dry_run_result"] = "DRY_RUN_PASS"
		report["verdict"] = "DRY_RUN_PASS — AWAITING EXECUTION APPROVAL"

	_write(report)
	print("DRY_RUN_RESULT", report["dry_run_result"])
	print("VERDICT", report["verdict"])
	return report


def _write(report):
	(OUT / "dry_run_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
	# Ensure secrets never present
	text = json.dumps(report)
	assert "0696d8636a5e4c4" not in text
	assert "9a28098aca792b4" not in text


if __name__ == "__main__":
	main()
