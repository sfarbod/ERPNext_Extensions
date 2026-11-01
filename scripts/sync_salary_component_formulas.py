#!/usr/bin/env python3
"""Synchronize Salary Component formula/condition fields: STAGING → PRODUCTION.

One-way sync via Frappe REST API only. Default mode is dry-run (no production writes).

Identified Salary Structure sync actions (from HRMS Salary Component):
  UI label group: "Update Salary Structures"
  UI buttons:     "Sync Condition", "Sync Formula"
  Backend method: Salary Component.update_salary_structures(field, value, structures=None)
  Helper method:  Salary Component.get_structures_to_be_updated()
  REST endpoint:  POST /api/method/run_doc_method
                  with dt="Salary Component", dn=<name>, method=<...>, args=<...>

Never logs or prints API credentials.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import traceback
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

# ---------------------------------------------------------------------------
# Configuration — credentials MUST come from environment variables.
# Do not embed real credentials. Never print these values.
# Required env vars:
#   SALARY_SYNC_SOURCE_URL, SALARY_SYNC_SOURCE_API_KEY, SALARY_SYNC_SOURCE_API_SECRET
#   SALARY_SYNC_DESTINATION_URL, SALARY_SYNC_DESTINATION_API_KEY,
#   SALARY_SYNC_DESTINATION_API_SECRET
# ---------------------------------------------------------------------------
SOURCE_URL = os.environ.get("SALARY_SYNC_SOURCE_URL", "https://erpstage.espadpharmed.com")
SOURCE_API_KEY = os.environ.get("SALARY_SYNC_SOURCE_API_KEY", "")
SOURCE_API_SECRET = os.environ.get("SALARY_SYNC_SOURCE_API_SECRET", "")

DESTINATION_URL = os.environ.get(
	"SALARY_SYNC_DESTINATION_URL", "https://erp.espadpharmed.com"
)
DESTINATION_API_KEY = os.environ.get("SALARY_SYNC_DESTINATION_API_KEY", "")
DESTINATION_API_SECRET = os.environ.get("SALARY_SYNC_DESTINATION_API_SECRET", "")

DRY_RUN = True

REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0
PAGE_SIZE = 100

FIELDS_TO_SYNC = ("amount_based_on_formula", "condition", "formula")

# Document methods invoked by the form buttons under "Update Salary Structures"
DOC_METHOD_GET_STRUCTURES = "get_structures_to_be_updated"
DOC_METHOD_UPDATE_STRUCTURES = "update_salary_structures"

REPORT_DIR = Path(__file__).resolve().parent / "salary_component_sync_output"
JSON_REPORT_NAME = "salary_component_sync_report.json"
CSV_REPORT_NAME = "salary_component_sync_report.csv"
SNAPSHOT_NAME = "production_pre_update_snapshot.json"


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
class FrappeClient:
	"""Authenticated Frappe REST client with timeouts and bounded retries."""

	def __init__(self, base_url: str, api_key: str, api_secret: str, label: str):
		self.base_url = base_url.rstrip("/")
		self.label = label
		self.session = requests.Session()
		self.session.headers.update(
			{
				"Authorization": f"token {api_key}:{api_secret}",
				"Content-Type": "application/json",
				"Accept": "application/json",
			}
		)

	def _request(
		self,
		method: str,
		path: str,
		*,
		params: dict | None = None,
		json_body: dict | None = None,
		idempotent: bool = True,
	) -> dict[str, Any]:
		url = f"{self.base_url}{path}"
		last_error: Exception | None = None
		attempts = MAX_RETRIES if idempotent else 1

		for attempt in range(1, attempts + 1):
			try:
				resp = self.session.request(
					method,
					url,
					params=params,
					json=json_body,
					timeout=REQUEST_TIMEOUT_SECONDS,
				)
				if resp.status_code in (429, 502, 503, 504) and attempt < attempts:
					time.sleep(RETRY_BACKOFF_SECONDS * attempt)
					continue
				if resp.status_code >= 400:
					raise ApiError(
						site=self.label,
						method=method,
						path=path,
						status_code=resp.status_code,
						message=_sanitize_error_body(resp),
					)
				if not resp.content:
					return {}
				return resp.json()
			except (requests.Timeout, requests.ConnectionError) as exc:
				last_error = exc
				if attempt < attempts:
					time.sleep(RETRY_BACKOFF_SECONDS * attempt)
					continue
				raise ApiError(
					site=self.label,
					method=method,
					path=path,
					status_code=None,
					message=f"Transport error: {type(exc).__name__}",
				) from exc

		raise ApiError(
			site=self.label,
			method=method,
			path=path,
			status_code=None,
			message=f"Request failed after retries: {last_error}",
		)

	def ping(self) -> dict[str, Any]:
		return self._request("GET", "/api/method/frappe.auth.get_logged_user")

	def list_resource(
		self,
		doctype: str,
		*,
		fields: list[str] | None = None,
		filters: list | dict | None = None,
		limit_page_length: int = PAGE_SIZE,
	) -> list[dict]:
		encoded = quote(doctype, safe="")
		path = f"/api/resource/{encoded}"
		results: list[dict] = []
		start = 0
		while True:
			params: dict[str, Any] = {
				"limit_start": start,
				"limit_page_length": limit_page_length,
			}
			if fields is not None:
				params["fields"] = json.dumps(fields)
			if filters is not None:
				params["filters"] = json.dumps(filters)
			payload = self._request("GET", path, params=params)
			batch = payload.get("data") or []
			results.extend(batch)
			if len(batch) < limit_page_length:
				break
			start += limit_page_length
		return results

	def get_doc(self, doctype: str, name: str) -> dict:
		encoded_dt = quote(doctype, safe="")
		encoded_name = quote(name, safe="")
		payload = self._request("GET", f"/api/resource/{encoded_dt}/{encoded_name}")
		return payload.get("data") or {}

	def update_doc(self, doctype: str, name: str, fields: dict) -> dict:
		encoded_dt = quote(doctype, safe="")
		encoded_name = quote(name, safe="")
		payload = self._request(
			"PUT",
			f"/api/resource/{encoded_dt}/{encoded_name}",
			json_body=fields,
			idempotent=True,
		)
		return payload.get("data") or {}

	def run_doc_method(
		self,
		doctype: str,
		name: str,
		method: str,
		args: dict | None = None,
		*,
		idempotent: bool = False,
	) -> Any:
		"""Call a whitelisted document method via /api/method/run_doc_method."""
		body: dict[str, Any] = {
			"dt": doctype,
			"dn": name,
			"method": method,
		}
		if args is not None:
			body["args"] = args
		payload = self._request(
			"POST",
			"/api/method/run_doc_method",
			json_body=body,
			idempotent=idempotent,
		)
		return payload.get("message")


class ApiError(Exception):
	def __init__(
		self,
		*,
		site: str,
		method: str,
		path: str,
		status_code: int | None,
		message: str,
	):
		self.site = site
		self.method = method
		self.path = path
		self.status_code = status_code
		self.message = message
		super().__init__(f"[{site}] {method} {path} -> {status_code}: {message}")

	def as_dict(self) -> dict:
		return {
			"site": self.site,
			"http_method": self.method,
			"path": self.path,
			"status_code": self.status_code,
			"error": self.message,
		}


def _sanitize_error_body(resp: requests.Response) -> str:
	try:
		data = resp.json()
	except Exception:
		text = (resp.text or "")[:500]
		return _strip_secrets(text)

	parts = []
	for key in ("exc_type", "exception", "message", "_server_messages"):
		if key in data and data[key]:
			val = data[key]
			if isinstance(val, (dict, list)):
				val = json.dumps(val, ensure_ascii=False)[:800]
			else:
				val = str(val)[:800]
			parts.append(f"{key}={_strip_secrets(val)}")
	if not parts:
		parts.append(_strip_secrets(str(data)[:500]))
	return "; ".join(parts)


def _strip_secrets(text: str) -> str:
	# Defensive: never echo credential-looking substrings from error payloads.
	for secret in (
		SOURCE_API_KEY,
		SOURCE_API_SECRET,
		DESTINATION_API_KEY,
		DESTINATION_API_SECRET,
	):
		if secret and secret in text:
			text = text.replace(secret, "***")
	return text


# ---------------------------------------------------------------------------
# Field comparison helpers
# ---------------------------------------------------------------------------
def _norm_flag(value: Any) -> int:
	if value in (True, 1, "1"):
		return 1
	return 0


def _norm_text(value: Any) -> str:
	if value is None:
		return ""
	return str(value)


def fields_equal(src: dict, dst: dict) -> bool:
	return (
		_norm_flag(src.get("amount_based_on_formula"))
		== _norm_flag(dst.get("amount_based_on_formula"))
		and _norm_text(src.get("condition")) == _norm_text(dst.get("condition"))
		and _norm_text(src.get("formula")) == _norm_text(dst.get("formula"))
	)


def extract_sync_fields(doc: dict) -> dict:
	return {
		"amount_based_on_formula": _norm_flag(doc.get("amount_based_on_formula")),
		"condition": doc.get("condition"),
		"formula": doc.get("formula"),
	}


# ---------------------------------------------------------------------------
# Sync planning / execution
# ---------------------------------------------------------------------------
def validate_connectivity(source: FrappeClient, dest: FrappeClient) -> dict:
	result = {"source": {}, "destination": {}}
	for client, key in ((source, "source"), (dest, "destination")):
		try:
			user = client.ping()
			# Confirm Salary Component resource is reachable
			client.list_resource(
				"Salary Component",
				fields=["name"],
				limit_page_length=1,
			)
			result[key] = {
				"ok": True,
				"user": user.get("message"),
				"salary_component_api": True,
			}
		except ApiError as exc:
			result[key] = {"ok": False, "error": exc.as_dict()}
	return result


def discover_sync_methods(dest: FrappeClient, sample_name: str | None) -> dict:
	"""Confirm the documented document methods exist on production.

	Does not mutate data: only calls get_structures_to_be_updated (read-only).
	"""
	info = {
		"ui_labels": {
			"group": "Update Salary Structures",
			"sync_condition": "Sync Condition",
			"sync_formula": "Sync Formula",
		},
		"backend": {
			"endpoint": "POST /api/method/run_doc_method",
			"get_structures": DOC_METHOD_GET_STRUCTURES,
			"update_structures": DOC_METHOD_UPDATE_STRUCTURES,
			"update_args": {
				"field": "condition | formula",
				"value": "<expression from Salary Component>",
				"structures": "<optional list; defaults to get_structures_to_be_updated()>",
			},
			"source_files": [
				"hrms/payroll/doctype/salary_component/salary_component.js",
				"hrms/payroll/doctype/salary_component/salary_component.py",
			],
			"effects": (
				"Updates matching Salary Detail rows inside non-cancelled Salary Structures "
				"that reference the component. For field='formula' also sets "
				"amount_based_on_formula on those detail rows from the component. "
				"Writes version history labeled 'via Salary Component sync'. "
				"Does not modify Salary Structure Assignments, Salary Slips, or GL."
			),
		},
		"probe": {"attempted": False},
	}
	if not sample_name:
		info["probe"] = {"attempted": False, "reason": "no sample component available"}
		return info

	try:
		structures = dest.run_doc_method(
			"Salary Component",
			sample_name,
			DOC_METHOD_GET_STRUCTURES,
			idempotent=True,
		)
		info["probe"] = {
			"attempted": True,
			"method": DOC_METHOD_GET_STRUCTURES,
			"sample_component": sample_name,
			"ok": True,
			"structure_count": len(structures or []),
		}
	except ApiError as exc:
		info["probe"] = {
			"attempted": True,
			"method": DOC_METHOD_GET_STRUCTURES,
			"sample_component": sample_name,
			"ok": False,
			"error": exc.as_dict(),
		}
	return info


def fetch_all_source_components(source: FrappeClient) -> tuple[list[dict], list[dict]]:
	"""Return (all_components_summary, eligible_full_docs)."""
	all_rows = source.list_resource(
		"Salary Component",
		fields=[
			"name",
			"amount_based_on_formula",
			"condition",
			"formula",
			"type",
			"disabled",
		],
	)
	eligible_names = [
		row["name"]
		for row in all_rows
		if _norm_flag(row.get("amount_based_on_formula")) == 1
	]
	eligible_docs = []
	for name in eligible_names:
		doc = source.get_doc("Salary Component", name)
		eligible_docs.append(doc)
	return all_rows, eligible_docs


def build_plan(
	eligible_docs: list[dict],
	dest: FrappeClient,
) -> dict:
	already_identical: list[dict] = []
	requires_update: list[dict] = []
	missing_in_production: list[dict] = []
	snapshot: dict[str, dict] = {}

	for src in eligible_docs:
		name = src["name"]
		src_fields = extract_sync_fields(src)
		try:
			dst = dest.get_doc("Salary Component", name)
		except ApiError as exc:
			if exc.status_code == 404:
				missing_in_production.append(
					{
						"name": name,
						"source_fields": src_fields,
					}
				)
				continue
			raise

		dst_fields = extract_sync_fields(dst)
		entry = {
			"name": name,
			"type": src.get("type"),
			"source_fields": src_fields,
			"destination_fields": dst_fields,
		}
		if fields_equal(src_fields, dst_fields):
			already_identical.append(entry)
		else:
			requires_update.append(entry)
			snapshot[name] = deepcopy(dst_fields)

	return {
		"already_identical": already_identical,
		"requires_update": requires_update,
		"missing_in_production": missing_in_production,
		"production_pre_update_snapshot": snapshot,
	}


def sync_salary_structures(
	dest: FrappeClient,
	name: str,
	field: str,
	value: str,
) -> dict:
	"""Execute one Sync Condition / Sync Formula action on production."""
	# Step A: discover structures (same as form button)
	try:
		structures = dest.run_doc_method(
			"Salary Component",
			name,
			DOC_METHOD_GET_STRUCTURES,
			idempotent=True,
		)
	except ApiError as exc:
		return {
			"ok": False,
			"stage": "get_structures_to_be_updated",
			"field": field,
			"structures": [],
			"error": exc.as_dict(),
		}

	structures = structures or []
	if not structures:
		# Form shows "No Salary Structures" and skips update — treat as success (nothing to sync)
		return {
			"ok": True,
			"stage": "no_structures",
			"field": field,
			"structures": [],
			"message": "Component not used in any Salary Structure; sync skipped (matches UI behavior).",
		}

	# Step B: update structures — non-idempotent; do not auto-retry on ambiguous timeout
	try:
		dest.run_doc_method(
			"Salary Component",
			name,
			DOC_METHOD_UPDATE_STRUCTURES,
			args={
				"field": field,
				"value": value if value is not None else "",
				"structures": structures,
			},
			idempotent=False,
		)
		return {
			"ok": True,
			"stage": "update_salary_structures",
			"field": field,
			"structures": structures,
		}
	except ApiError as exc:
		return {
			"ok": False,
			"stage": "update_salary_structures",
			"field": field,
			"structures": structures,
			"error": exc.as_dict(),
			"manual_intervention_required": True,
			"note": (
				"Non-idempotent sync action failed or timed out. "
				"Inspect Salary Structures before retrying."
			),
		}


def verify_structure_fields(
	dest: FrappeClient,
	component_name: str,
	component_type: str | None,
	expected_condition: str | None,
	expected_formula: str | None,
) -> dict:
	"""Best-effort verification that Salary Detail rows match synced values."""
	parentfield_map = {
		"Earning": "earnings",
		"Deduction": "deductions",
		"Employer Contribution": "employer_contributions",
	}
	try:
		structures = dest.run_doc_method(
			"Salary Component",
			component_name,
			DOC_METHOD_GET_STRUCTURES,
			idempotent=True,
		) or []
	except ApiError as exc:
		return {"ok": False, "error": exc.as_dict()}

	mismatches = []
	checked = 0
	parentfield = parentfield_map.get(component_type or "", "earnings")

	for structure_name in structures:
		try:
			ss = dest.get_doc("Salary Structure", structure_name)
		except ApiError as exc:
			mismatches.append(
				{"structure": structure_name, "error": exc.as_dict()}
			)
			continue
		rows = ss.get(parentfield) or []
		# Fallback: search all known child tables if type unknown / row not found
		if not any(r.get("salary_component") == component_name for r in rows):
			for pf in parentfield_map.values():
				rows = ss.get(pf) or []
				if any(r.get("salary_component") == component_name for r in rows):
					break
		row = next(
			(r for r in rows if r.get("salary_component") == component_name),
			None,
		)
		if not row:
			mismatches.append(
				{"structure": structure_name, "error": "component row not found"}
			)
			continue
		checked += 1
		if _norm_text(row.get("condition")) != _norm_text(expected_condition):
			mismatches.append(
				{
					"structure": structure_name,
					"field": "condition",
					"expected": expected_condition,
					"actual": row.get("condition"),
				}
			)
		if _norm_text(row.get("formula")) != _norm_text(expected_formula):
			mismatches.append(
				{
					"structure": structure_name,
					"field": "formula",
					"expected": expected_formula,
					"actual": row.get("formula"),
				}
			)
		if _norm_flag(row.get("amount_based_on_formula")) != 1:
			mismatches.append(
				{
					"structure": structure_name,
					"field": "amount_based_on_formula",
					"expected": 1,
					"actual": row.get("amount_based_on_formula"),
				}
			)

	return {
		"ok": len(mismatches) == 0,
		"structures_checked": checked,
		"structure_names": structures,
		"mismatches": mismatches,
	}


def apply_updates(
	dest: FrappeClient,
	requires_update: list[dict],
) -> list[dict]:
	results = []
	for entry in requires_update:
		name = entry["name"]
		src_fields = entry["source_fields"]
		prev_fields = entry["destination_fields"]
		record: dict[str, Any] = {
			"name": name,
			"type": entry.get("type"),
			"previous_fields": prev_fields,
			"new_fields": src_fields,
			"field_update": {"ok": False},
			"sync_condition": {"ok": False, "skipped": True},
			"sync_formula": {"ok": False, "skipped": True},
			"structure_verification": None,
			"final_status": "PENDING",
		}

		payload = {
			"amount_based_on_formula": 1,
			"condition": src_fields.get("condition"),
			"formula": src_fields.get("formula"),
		}

		# Step 1 — update Salary Component fields
		try:
			dest.update_doc("Salary Component", name, payload)
		except ApiError as exc:
			record["field_update"] = {"ok": False, "error": exc.as_dict()}
			record["final_status"] = "FAILED_FIELD_UPDATE"
			record["manual_intervention_required"] = True
			results.append(record)
			continue

		# Step 2 — verify fields
		try:
			verified = dest.get_doc("Salary Component", name)
			verified_fields = extract_sync_fields(verified)
			match = fields_equal(src_fields, verified_fields)
			record["field_update"] = {
				"ok": match,
				"verified_fields": verified_fields,
			}
			if not match:
				record["final_status"] = "FAILED_FIELD_VERIFY"
				record["manual_intervention_required"] = True
				results.append(record)
				continue
		except ApiError as exc:
			record["field_update"] = {
				"ok": False,
				"error": exc.as_dict(),
				"note": "Update may have succeeded; verification GET failed.",
			}
			record["final_status"] = "FAILED_FIELD_VERIFY"
			record["manual_intervention_required"] = True
			results.append(record)
			continue

		# Step 3 — Sync Condition then Sync Formula (production only)
		cond_result = sync_salary_structures(
			dest,
			name,
			"condition",
			_norm_text(src_fields.get("condition")),
		)
		record["sync_condition"] = cond_result

		if not cond_result.get("ok"):
			record["final_status"] = "FAILED_SYNC_CONDITION"
			record["manual_intervention_required"] = True
			results.append(record)
			continue

		formula_result = sync_salary_structures(
			dest,
			name,
			"formula",
			_norm_text(src_fields.get("formula")),
		)
		record["sync_formula"] = formula_result

		if not formula_result.get("ok"):
			record["final_status"] = "FAILED_SYNC_FORMULA"
			record["manual_intervention_required"] = True
			results.append(record)
			continue

		# Optional structure-level verification
		structure_check = verify_structure_fields(
			dest,
			name,
			entry.get("type"),
			src_fields.get("condition"),
			src_fields.get("formula"),
		)
		record["structure_verification"] = structure_check

		if structure_check.get("ok"):
			record["final_status"] = "FULLY_COMPLETED"
		else:
			# Fields + both actions succeeded; structure rows may still diverge
			record["final_status"] = "UPDATED_WITH_STRUCTURE_MISMATCH"
			record["manual_intervention_required"] = True

		results.append(record)

	return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def build_report(
	*,
	dry_run: bool,
	connectivity: dict,
	method_info: dict,
	total_source: int,
	eligible: list[dict],
	excluded_disabled: list[dict],
	plan: dict,
	apply_results: list[dict] | None,
	started_at: str,
	finished_at: str,
) -> dict:
	apply_results = apply_results or []
	updated_ok = [r for r in apply_results if r["field_update"].get("ok")]
	fully = [r for r in apply_results if r["final_status"] == "FULLY_COMPLETED"]
	failed_updates = [
		r
		for r in apply_results
		if r["final_status"] in ("FAILED_FIELD_UPDATE", "FAILED_FIELD_VERIFY")
	]
	failed_sync_condition = [
		r for r in apply_results if r["final_status"] == "FAILED_SYNC_CONDITION"
	]
	failed_sync_formula = [
		r for r in apply_results if r["final_status"] == "FAILED_SYNC_FORMULA"
	]

	if dry_run:
		verdict = "DRY RUN COMPLETE"
	elif not apply_results and plan["requires_update"]:
		verdict = "FAILED"
	elif plan["requires_update"] and len(fully) == len(plan["requires_update"]):
		verdict = "SUCCESS"
	elif fully or updated_ok:
		verdict = "PARTIAL SUCCESS"
	elif not plan["requires_update"]:
		verdict = "SUCCESS"  # nothing to update; already synchronized
	else:
		verdict = "FAILED"

	errors = []
	for r in apply_results:
		if r["final_status"] == "FULLY_COMPLETED":
			continue
		if r["final_status"] in ("PENDING",) and dry_run:
			continue
		err = {
			"salary_component": r["name"],
			"failed_operation": r["final_status"],
			"field_update_succeeded": bool(r["field_update"].get("ok")),
			"sync_condition_succeeded": bool(r["sync_condition"].get("ok")),
			"sync_formula_succeeded": bool(r["sync_formula"].get("ok")),
			"manual_intervention_required": r.get("manual_intervention_required", False),
		}
		for stage_key in ("field_update", "sync_condition", "sync_formula"):
			stage = r.get(stage_key) or {}
			if stage.get("error"):
				err["http_status_code"] = stage["error"].get("status_code")
				err["sanitized_api_error"] = stage["error"].get("error")
				err["failed_stage_detail"] = stage_key
				break
		if r.get("structure_verification") and not r["structure_verification"].get("ok"):
			err["structure_mismatches"] = r["structure_verification"].get("mismatches")
		errors.append(err)

	return {
		"environment": {
			"source_url": SOURCE_URL,
			"destination_url": DESTINATION_URL,
			"execution_mode": "dry_run" if dry_run else "apply",
			"started_at": started_at,
			"finished_at": finished_at,
			"connectivity": connectivity,
			"sync_method_discovery": method_info,
		},
		"summary": {
			"total_source_salary_components": total_source,
			"eligible_source_components": len(eligible),
			"already_synchronized": len(plan["already_identical"]),
			"requiring_updates": len(plan["requires_update"]),
			"updated_successfully": len(updated_ok) if not dry_run else 0,
			"missing_in_production": len(plan["missing_in_production"]),
			"failed_updates": len(failed_updates),
			"failed_sync_condition_actions": len(failed_sync_condition),
			"failed_sync_formula_actions": len(failed_sync_formula),
			"fully_completed": len(fully) if not dry_run else 0,
			"excluded_source_formula_disabled": len(excluded_disabled),
		},
		"updated_components": apply_results if not dry_run else [],
		"planned_updates": plan["requires_update"] if dry_run else [],
		"skipped": {
			"already_identical": [
				{"name": e["name"], "fields": e["source_fields"]}
				for e in plan["already_identical"]
			],
			"missing_in_production": plan["missing_in_production"],
			"excluded_source_formula_disabled": excluded_disabled,
		},
		"production_pre_update_snapshot": plan["production_pre_update_snapshot"],
		"errors": errors,
		"verdict": verdict,
	}


def write_reports(report: dict, report_dir: Path) -> tuple[Path, Path, Path]:
	report_dir.mkdir(parents=True, exist_ok=True)
	json_path = report_dir / JSON_REPORT_NAME
	csv_path = report_dir / CSV_REPORT_NAME
	snapshot_path = report_dir / SNAPSHOT_NAME

	with json_path.open("w", encoding="utf-8") as f:
		json.dump(report, f, ensure_ascii=False, indent=2)

	with snapshot_path.open("w", encoding="utf-8") as f:
		json.dump(
			report.get("production_pre_update_snapshot") or {},
			f,
			ensure_ascii=False,
			indent=2,
		)

	rows = []
	source_rows = report.get("updated_components") or report.get("planned_updates") or []
	for item in source_rows:
		prev = item.get("previous_fields") or item.get("destination_fields") or {}
		new = item.get("new_fields") or item.get("source_fields") or {}
		rows.append(
			{
				"Salary Component": item.get("name"),
				"Previous Condition": prev.get("condition"),
				"New Condition": new.get("condition"),
				"Previous Formula": prev.get("formula"),
				"New Formula": new.get("formula"),
				"Previous amount_based_on_formula": prev.get("amount_based_on_formula"),
				"New amount_based_on_formula": new.get("amount_based_on_formula"),
				"Sync Condition": (
					(item.get("sync_condition") or {}).get("ok")
					if "sync_condition" in item
					else "N/A (dry-run)"
				),
				"Sync Formula": (
					(item.get("sync_formula") or {}).get("ok")
					if "sync_formula" in item
					else "N/A (dry-run)"
				),
				"Final Status": item.get("final_status", "PLANNED"),
			}
		)

	fieldnames = [
		"Salary Component",
		"Previous Condition",
		"New Condition",
		"Previous Formula",
		"New Formula",
		"Previous amount_based_on_formula",
		"New amount_based_on_formula",
		"Sync Condition",
		"Sync Formula",
		"Final Status",
	]
	with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
		writer = csv.DictWriter(f, fieldnames=fieldnames)
		writer.writeheader()
		for row in rows:
			writer.writerow(row)

	return json_path, csv_path, snapshot_path


def print_human_summary(report: dict, paths: tuple[Path, Path, Path]) -> None:
	env = report["environment"]
	summary = report["summary"]
	json_path, csv_path, snapshot_path = paths

	print("=" * 72)
	print("Salary Component Formula/Condition Sync Report")
	print("=" * 72)
	print(f"Source:      {env['source_url']}")
	print(f"Destination: {env['destination_url']}")
	print(f"Mode:        {env['execution_mode']}")
	print(f"Started:     {env['started_at']}")
	print(f"Finished:    {env['finished_at']}")
	print()
	print("Connectivity:")
	for site in ("source", "destination"):
		c = env["connectivity"].get(site, {})
		status = "OK" if c.get("ok") else "FAILED"
		user = c.get("user") or ""
		print(f"  {site}: {status}" + (f" (user={user})" if user else ""))
		if not c.get("ok"):
			print(f"    error: {c.get('error')}")
	print()
	print("Sync method discovery:")
	mi = env.get("sync_method_discovery") or {}
	backend = mi.get("backend") or {}
	print(f"  Endpoint: {backend.get('endpoint')}")
	print(f"  get_structures: {backend.get('get_structures')}")
	print(f"  update_structures: {backend.get('update_structures')}")
	probe = mi.get("probe") or {}
	if probe.get("attempted"):
		print(
			f"  Probe ({probe.get('method')} on {probe.get('sample_component')}): "
			f"{'OK' if probe.get('ok') else 'FAILED'}"
			+ (
				f" — {probe.get('structure_count')} structures"
				if probe.get("ok")
				else f" — {probe.get('error')}"
			)
		)
	print()
	print("Summary:")
	for key, label in (
		("total_source_salary_components", "Total source Salary Components"),
		("eligible_source_components", "Eligible (amount_based_on_formula=1)"),
		("already_synchronized", "Already identical"),
		("requiring_updates", "Requiring updates"),
		("updated_successfully", "Updated successfully"),
		("missing_in_production", "Missing in production"),
		("failed_updates", "Failed updates"),
		("failed_sync_condition_actions", "Failed Sync Condition"),
		("failed_sync_formula_actions", "Failed Sync Formula"),
		("fully_completed", "Fully completed"),
		("excluded_source_formula_disabled", "Excluded (source formula disabled)"),
	):
		print(f"  {label}: {summary.get(key)}")
	print()

	planned = report.get("planned_updates") or []
	updated = report.get("updated_components") or []
	show = updated or planned
	if show:
		print("Components requiring / receiving updates:")
		for item in show:
			name = item.get("name")
			prev = item.get("previous_fields") or item.get("destination_fields") or {}
			new = item.get("new_fields") or item.get("source_fields") or {}
			status = item.get("final_status", "PLANNED")
			print(f"  - {name} [{status}]")
			print(f"      amount_based_on_formula: {prev.get('amount_based_on_formula')} -> {new.get('amount_based_on_formula')}")
			print(f"      condition changed: {_norm_text(prev.get('condition')) != _norm_text(new.get('condition'))}")
			print(f"      formula changed:   {_norm_text(prev.get('formula')) != _norm_text(new.get('formula'))}")
		print()

	missing = report["skipped"]["missing_in_production"]
	if missing:
		print("Missing in production (will NOT be created):")
		for m in missing:
			print(f"  - {m['name']}")
		print()

	if report.get("errors"):
		print("Errors:")
		for err in report["errors"]:
			print(f"  - {err.get('salary_component')}: {err.get('failed_operation')}")
			if err.get("sanitized_api_error"):
				print(f"      {err.get('sanitized_api_error')}")
		print()

	print(f"JSON report:  {json_path}")
	print(f"CSV report:   {csv_path}")
	print(f"Snapshot:     {snapshot_path}")
	print()
	print(f"FINAL VERDICT: {report['verdict']}")
	print("=" * 72)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Sync Salary Component formulas/conditions from staging to production."
	)
	mode = parser.add_mutually_exclusive_group()
	mode.add_argument(
		"--dry-run",
		action="store_true",
		default=None,
		help="Analyze only; no production writes (default).",
	)
	mode.add_argument(
		"--apply",
		action="store_true",
		help="Apply updates to production. Requires prior dry-run review.",
	)
	parser.add_argument(
		"--i-approve-production-changes",
		action="store_true",
		help="Required safety flag together with --apply.",
	)
	parser.add_argument(
		"--report-dir",
		type=Path,
		default=REPORT_DIR,
		help="Directory for JSON/CSV reports and snapshot.",
	)
	return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
	args = parse_args(argv)
	if args.apply:
		if not args.i_approve_production_changes:
			print(
				"Refusing apply mode without --i-approve-production-changes. "
				"Review the dry-run report first.",
				file=sys.stderr,
			)
			return 2
		dry_run = False
	elif args.dry_run is True:
		dry_run = True
	else:
		dry_run = DRY_RUN

	started_at = datetime.now(timezone.utc).isoformat()
	missing_creds = [
		name
		for name, value in (
			("SALARY_SYNC_SOURCE_API_KEY", SOURCE_API_KEY),
			("SALARY_SYNC_SOURCE_API_SECRET", SOURCE_API_SECRET),
			("SALARY_SYNC_DESTINATION_API_KEY", DESTINATION_API_KEY),
			("SALARY_SYNC_DESTINATION_API_SECRET", DESTINATION_API_SECRET),
		)
		if not (value or "").strip()
	]
	if missing_creds:
		print(
			"Missing required credential environment variables: "
			+ ", ".join(missing_creds)
			+ ". Refusing to run with embedded secrets.",
			file=sys.stderr,
		)
		return 2

	source = FrappeClient(SOURCE_URL, SOURCE_API_KEY, SOURCE_API_SECRET, "staging")
	dest = FrappeClient(
		DESTINATION_URL, DESTINATION_API_KEY, DESTINATION_API_SECRET, "production"
	)

	try:
		connectivity = validate_connectivity(source, dest)
		if not connectivity["source"].get("ok") or not connectivity["destination"].get("ok"):
			finished_at = datetime.now(timezone.utc).isoformat()
			report = {
				"environment": {
					"source_url": SOURCE_URL,
					"destination_url": DESTINATION_URL,
					"execution_mode": "dry_run" if dry_run else "apply",
					"started_at": started_at,
					"finished_at": finished_at,
					"connectivity": connectivity,
				},
				"summary": {},
				"errors": [
					{
						"failed_operation": "connectivity_validation",
						"sanitized_api_error": "API connectivity or Salary Component API check failed.",
						"manual_intervention_required": True,
					}
				],
				"verdict": "FAILED",
				"skipped": {
					"already_identical": [],
					"missing_in_production": [],
					"excluded_source_formula_disabled": [],
				},
				"updated_components": [],
				"planned_updates": [],
				"production_pre_update_snapshot": {},
			}
			paths = write_reports(report, args.report_dir)
			print_human_summary(report, paths)
			return 1

		all_rows, eligible_docs = fetch_all_source_components(source)
		excluded_disabled = [
			{
				"name": row["name"],
				"amount_based_on_formula": row.get("amount_based_on_formula"),
			}
			for row in all_rows
			if _norm_flag(row.get("amount_based_on_formula")) != 1
		]

		plan = build_plan(eligible_docs, dest)

		sample = None
		if plan["requires_update"]:
			sample = plan["requires_update"][0]["name"]
		elif plan["already_identical"]:
			sample = plan["already_identical"][0]["name"]
		elif eligible_docs:
			sample = eligible_docs[0]["name"]

		method_info = discover_sync_methods(dest, sample)
		probe = method_info.get("probe") or {}
		if probe.get("attempted") and not probe.get("ok"):
			finished_at = datetime.now(timezone.utc).isoformat()
			report = build_report(
				dry_run=True,
				connectivity=connectivity,
				method_info=method_info,
				total_source=len(all_rows),
				eligible=eligible_docs,
				excluded_disabled=excluded_disabled,
				plan=plan,
				apply_results=None,
				started_at=started_at,
				finished_at=finished_at,
			)
			report["verdict"] = "FAILED"
			report["errors"].append(
				{
					"failed_operation": "sync_method_discovery",
					"sanitized_api_error": probe.get("error"),
					"manual_intervention_required": True,
					"note": (
						"Could not reliably invoke get_structures_to_be_updated on production. "
						"Stopping before any production changes."
					),
				}
			)
			paths = write_reports(report, args.report_dir)
			print_human_summary(report, paths)
			return 1

		apply_results = None
		if not dry_run:
			# Revalidate plan immediately before writes
			plan = build_plan(eligible_docs, dest)
			apply_results = apply_updates(dest, plan["requires_update"])

		finished_at = datetime.now(timezone.utc).isoformat()
		report = build_report(
			dry_run=dry_run,
			connectivity=connectivity,
			method_info=method_info,
			total_source=len(all_rows),
			eligible=eligible_docs,
			excluded_disabled=excluded_disabled,
			plan=plan,
			apply_results=apply_results,
			started_at=started_at,
			finished_at=finished_at,
		)
		paths = write_reports(report, args.report_dir)
		print_human_summary(report, paths)

		if report["verdict"] in ("FAILED",):
			return 1
		if report["verdict"] == "PARTIAL SUCCESS":
			return 3
		return 0

	except Exception as exc:
		finished_at = datetime.now(timezone.utc).isoformat()
		err_msg = _strip_secrets(f"{type(exc).__name__}: {exc}")
		print(f"Unhandled error: {err_msg}", file=sys.stderr)
		print(_strip_secrets(traceback.format_exc()), file=sys.stderr)
		report = {
			"environment": {
				"source_url": SOURCE_URL,
				"destination_url": DESTINATION_URL,
				"execution_mode": "dry_run" if dry_run else "apply",
				"started_at": started_at,
				"finished_at": finished_at,
			},
			"summary": {},
			"errors": [
				{
					"failed_operation": "unhandled_exception",
					"sanitized_api_error": err_msg,
					"manual_intervention_required": True,
				}
			],
			"verdict": "FAILED",
			"skipped": {
				"already_identical": [],
				"missing_in_production": [],
				"excluded_source_formula_disabled": [],
			},
			"updated_components": [],
			"planned_updates": [],
			"production_pre_update_snapshot": {},
		}
		try:
			paths = write_reports(report, args.report_dir)
			print_human_summary(report, paths)
		except Exception:
			pass
		return 1


if __name__ == "__main__":
	sys.exit(main())
