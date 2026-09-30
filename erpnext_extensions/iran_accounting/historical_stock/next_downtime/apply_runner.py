# Copyright (c) 2026, ERPNext Extensions contributors
"""Proven-root apply runner for Historical Repair.

Executes only roots with deterministic provenance, supported strategy,
passed dry-run, no unresolved manual blocker, and current Iran Accounting
compatibility. No unrestricted READY drain.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import frappe

from erpnext_extensions.iran_accounting.historical_stock.next_downtime.categories import (
	AUTO_REPAIRABLE,
	MANUAL_BUSINESS_EVIDENCE_REQUIRED,
)
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.cross_time import (
	CROSS_TIME_EXACT,
	INDEPENDENT_PRODUCTION_FLOWS,
	classify_cross_time,
)

SUPPORTED_STRATEGIES = frozenset(
	{
		"MANUFACTURE_STAGE_EQUAL_RATE_BYPRODUCT",
		"CROSS_TIME_EXACT",
		"MANUFACTURE_SCRAP_FG",
		"VALUED_SOURCE_ZERO_OUTGOING",
	}
)

OUT_DIR = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/"
	"private/files/hr_correction_20260930"
)


def _now() -> str:
	return datetime.now(timezone.utc).isoformat()


def _record(path: Path, payload: dict) -> dict:
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
	return payload


def root_eligible(root: dict) -> dict:
	"""Gate one proposed root before mutation."""
	strategy = str(root.get("strategy") or "")
	category = str(root.get("category") or "")
	priority = str(root.get("priority") or "")
	reasons = []
	if strategy not in SUPPORTED_STRATEGIES:
		reasons.append("unsupported_strategy")
	if category == MANUAL_BUSINESS_EVIDENCE_REQUIRED and priority in ("P0", "P1"):
		reasons.append("unresolved_manual_blocker")
	if root.get("detection") == "CROSS_TIME":
		cls = classify_cross_time(root)
		if cls.get("class") == INDEPENDENT_PRODUCTION_FLOWS:
			reasons.append("independent_production_flows_no_timestamp_repair")
		elif cls.get("class") != CROSS_TIME_EXACT or not cls.get("repair"):
			reasons.append(f"cross_time_not_exact:{cls.get('class')}")
	if not root.get("dry_run_passed"):
		reasons.append("dry_run_not_passed")
	if not root.get("deterministic_provenance"):
		reasons.append("missing_deterministic_provenance")
	if not root.get("iran_accounting_compatible", True):
		reasons.append("iran_accounting_incompatible")
	return {
		"eligible": not reasons,
		"reasons": reasons,
		"strategy": strategy,
		"category": category or AUTO_REPAIRABLE,
	}


def apply_proven_roots(roots: list[dict], *, dry_run: bool = True) -> dict:
	"""Apply only eligible proven roots. Default dry-run.

	Each operation records: root, strategy, authority, before fingerprint,
	expected postcondition, native repost scope, after fingerprint.
	"""
	results = []
	applied = 0
	skipped = 0
	for root in roots or []:
		gate = root_eligible(root)
		entry = {
			"root": root.get("root") or root.get("voucher") or root.get("name"),
			"strategy": root.get("strategy"),
			"authority": root.get("authority"),
			"before_fingerprint": root.get("before_fingerprint"),
			"expected_postcondition": root.get("expected_postcondition"),
			"native_repost_scope": root.get("native_repost_scope"),
			"gate": gate,
			"ts": _now(),
		}
		if not gate["eligible"]:
			entry["status"] = "SKIPPED"
			skipped += 1
			results.append(entry)
			continue
		strategy = root["strategy"]
		voucher = entry["root"]
		if strategy == "MANUFACTURE_STAGE_EQUAL_RATE_BYPRODUCT":
			from erpnext_extensions.iran_accounting.historical_stock.manufacture_equal_rate_byproduct import (
				apply_equal_rate_byproduct,
			)

			outcome = apply_equal_rate_byproduct(voucher, dry_run=dry_run)
			entry["outcome"] = outcome
			entry["after_fingerprint"] = {
				"after_fg": outcome.get("after_fg"),
				"after_secondaries": outcome.get("after_secondaries"),
				"quantity_fingerprint_ok": outcome.get("quantity_fingerprint_ok"),
			}
			entry["status"] = (
				"APPLIED"
				if outcome.get("applied")
				else ("DRY_RUN_OK" if outcome.get("allocate_applied") else "FAILED")
			)
			if outcome.get("applied"):
				applied += 1
			elif not dry_run:
				skipped += 1
		else:
			entry["status"] = "TECHNICAL_TOOL_GAP"
			entry["reason"] = f"apply_path_not_wired:{strategy}"
			skipped += 1
		results.append(entry)

	payload = {
		"dry_run": dry_run,
		"applied": applied,
		"skipped": skipped,
		"results": results,
		"ts": _now(),
	}
	_record(OUT_DIR / ("apply_dry_run.json" if dry_run else "apply_results.json"), payload)
	return payload
