# Copyright (c) 2026, ERPNext Extensions contributors
"""Resumable Historical Repair campaign for the NEXT fresh Production backup.

Default is dry-run. Hard-gate failure requires a fresh restore — never resume
an uncertain partial economic state. Never names a backup Production-ready
automatically.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from erpnext_extensions.iran_accounting.historical_stock.next_downtime.categories import (
	MANUAL_BUSINESS_EVIDENCE_REQUIRED,
)
from erpnext_extensions.iran_accounting.historical_stock.next_downtime.preflight import (
	full_repost_preflight,
)

PHASES = (
	"PRECHECK",
	"SCAN",
	"CLASSIFY",
	"MANUAL_REVIEW",
	"DRY_RUN",
	"REPAIR_PROVEN_ROOTS",
	"WAIT_WORKERS",
	"REPOST",
	"VERIFY",
	"FULL_REPOST_PREFLIGHT",
	"FULL_REPOST",
	"FINAL_VERIFY",
	"BACKUP_READY",
)

FROZEN_CANDIDATE_2 = "20260927_051943-development_localhost-database.sql.gz"
FROZEN_CANDIDATE_2_SHA256 = "6b75231de5b609326a72f349ec036e592445c0b57065b5375cc7f5f8c9e2843e"


def _now() -> str:
	return datetime.now(timezone.utc).isoformat()


def campaign_id(source_sha256: str, code_head: str) -> str:
	raw = f"{source_sha256}:{code_head}".encode()
	return "HR-" + hashlib.sha256(raw).hexdigest()[:12]


def manual_gate(findings: list[dict]) -> dict:
	"""Stop mutation when unresolved P0/P1 business-evidence roots exist."""
	blocking = []
	for row in findings or []:
		if row.get("category") != MANUAL_BUSINESS_EVIDENCE_REQUIRED:
			continue
		pri = str(row.get("priority") or "")
		if pri in ("P0", "P1"):
			blocking.append(row)
	return {
		"stop": bool(blocking),
		"blocking": blocking,
		"message": "Unresolved P0/P1 MANUAL_BUSINESS_EVIDENCE_REQUIRED — inspect before repair"
		if blocking
		else "",
	}


def backup_candidate_eligible(snapshot: dict) -> dict:
	"""Never auto-label Production-ready. Eligibility only."""
	need = {
		"phases_complete": bool(snapshot.get("phases_complete")),
		"workers_settled": bool(snapshot.get("workers_settled")),
		"open_riv": int(snapshot.get("open_riv") or 0) == 0,
		"hard_gates": bool(snapshot.get("hard_gates_pass")),
		"verify_complete": bool(snapshot.get("verify_complete")),
		"db_committed": bool(snapshot.get("db_committed")),
	}
	return {"eligible": all(need.values()), "checks": need}


def refuse_mutate_frozen_candidate(backup_filename: str | None, backup_sha256: str | None) -> dict:
	fn = str(backup_filename or "")
	sha = str(backup_sha256 or "").lower()
	frozen = fn.endswith(FROZEN_CANDIDATE_2) or sha == FROZEN_CANDIDATE_2_SHA256
	return {
		"frozen": frozen,
		"allow_experiment": not frozen,
		"reason": "Candidate 2 is immutable — restore a NEW Production dump"
		if frozen
		else "",
	}


def historical_repair_campaign(
	*,
	source_sha256: str,
	code_head: str,
	app_version: str,
	findings: list | None = None,
	snapshot: dict | None = None,
	apply: bool = False,
	resume_state: dict | None = None,
) -> dict:
	"""Orchestrator. ``apply=False`` is dry-run. Does not write the database."""
	snap = dict(snapshot or {})
	findings = list(findings or [])
	cid = campaign_id(source_sha256, code_head)
	frozen = refuse_mutate_frozen_candidate(snap.get("backup_filename"), snap.get("backup_sha256"))
	if frozen["frozen"] and apply:
		return {
			"ok": False,
			"campaign_id": cid,
			"phase": "PRECHECK",
			"reason": frozen["reason"],
			"apply": False,
		}
	if resume_state and resume_state.get("hard_gate_failed"):
		return {
			"ok": False,
			"campaign_id": cid,
			"phase": "PRECHECK",
			"reason": "hard gate failed — require fresh restore, do not resume",
			"apply": False,
		}
	gate = manual_gate(findings)
	log = [
		{"phase": "PRECHECK", "at": _now(), "ok": True},
		{"phase": "SCAN", "at": _now(), "ok": True, "finding_n": len(findings)},
		{"phase": "CLASSIFY", "at": _now(), "ok": True},
		{"phase": "MANUAL_REVIEW", "at": _now(), "ok": not gate["stop"], "gate": gate},
	]
	if gate["stop"]:
		return {
			"ok": False,
			"campaign_id": cid,
			"phase": "MANUAL_REVIEW",
			"manual_gate": gate,
			"apply": False,
			"log": log,
		}
	log.append({"phase": "DRY_RUN", "at": _now(), "ok": True})
	if not apply:
		pre = full_repost_preflight(snap)
		log.append({"phase": "FULL_REPOST_PREFLIGHT", "at": _now(), **pre})
		return {
			"ok": True,
			"campaign_id": cid,
			"phase": "DRY_RUN",
			"apply": False,
			"app_version": app_version,
			"code_head": code_head,
			"source_sha256": source_sha256,
			"preflight": pre,
			"log": log,
		}
	# apply=True is the operator path on a *fresh* dump, not implemented here
	# as a silent writer. Callers must run proven repair functions themselves.
	return {
		"ok": False,
		"campaign_id": cid,
		"phase": "REPAIR_PROVEN_ROOTS",
		"reason": "apply requires an explicit proven-root runner on a fresh dump",
		"apply": False,
		"log": log,
	}


def build_audit_manifest(payload: dict) -> dict:
	"""Audit evidence only — never economic authority."""
	body = json.dumps(payload, default=str, sort_keys=True)
	return {
		"kind": "HISTORICAL_REPAIR_MANIFEST",
		"authority": False,
		"sha256": hashlib.sha256(body.encode()).hexdigest(),
		"payload": payload,
	}
