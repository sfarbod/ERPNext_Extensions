# Copyright (c) 2026, ERPNext Extensions contributors
"""One user-facing Historical Repair model.

Internal topic statuses remain implementation details. The UI and campaign
planner answer only:

    WHAT IS WRONG → WHERE IT FIRST BROKE → EXPECTED STATE → SAFE? → STILL CORRECT?

Primary states (exactly six):

    READY / WAITING / MANUAL / LEGITIMATE / REPAIRED / FAILED

Root families (exactly five):

    POSTING_ORDER / VALUATION / MANUFACTURE_FLOW / DERIVED_STATE / ACCOUNTING
"""

from __future__ import annotations

from frappe.utils import now_datetime

PRIMARY_READY = "READY"
PRIMARY_WAITING = "WAITING"
PRIMARY_MANUAL = "MANUAL"
PRIMARY_LEGITIMATE = "LEGITIMATE"
PRIMARY_REPAIRED = "REPAIRED"
PRIMARY_FAILED = "FAILED"

PRIMARY_STATES = (
	PRIMARY_READY,
	PRIMARY_WAITING,
	PRIMARY_MANUAL,
	PRIMARY_LEGITIMATE,
	PRIMARY_REPAIRED,
	PRIMARY_FAILED,
)

FAMILY_POSTING_ORDER = "POSTING_ORDER"
FAMILY_VALUATION = "VALUATION"
FAMILY_MANUFACTURE_FLOW = "MANUFACTURE_FLOW"
FAMILY_DERIVED_STATE = "DERIVED_STATE"
FAMILY_ACCOUNTING = "ACCOUNTING"

ROOT_FAMILIES = (
	FAMILY_POSTING_ORDER,
	FAMILY_VALUATION,
	FAMILY_MANUFACTURE_FLOW,
	FAMILY_DERIVED_STATE,
	FAMILY_ACCOUNTING,
)

# Reason codes live *under* a primary state. They are not user workflows.
REASON_LEFTOVER_MA = "LEFTOVER_MA"
REASON_POSTING_ORDER = "POSTING_ORDER"
REASON_WRONG_RATE = "WRONG_RATE"
REASON_ZERO_RATE = "ZERO_RATE"
REASON_BIN_DRIFT = "BIN_DRIFT"
REASON_GL_DRIFT = "GL_DRIFT"
REASON_MANUFACTURE_FLOW = "MANUFACTURE_FLOW"

_LEGITIMATE_TOKENS = (
	"NO_ACTION",
	"NO_ACTION_REQUIRED",
	"Z0_LEGITIMATE",
	"LEGITIMATE",
	"ZP_PROVEN_LEGITIMATE_ZERO",
	"HEALTHY",
	"G0_HEALTHY",
)
_FAILED_TOKENS = ("FAILED", "FAILED_POSTCONDITION", "FAILED_RIV", "REPORT_LEDGER_MISMATCH")
_REPAIRED_TOKENS = (
	"REPAIRED",
	"RATE_REBUILD_COMPLETE",
	"INTEGRITY_COMPLETE",
	"DOWNSTREAM_COMPLETE",
	"I1_REPAIRED",
	"I4_REPAIRED",
	"LEFTOVER_MA_REPAIRED",
)
_READY_TOKENS = (
	"READY",
	"RECONSTRUCTABLE",
	"SAFE_TO_REPOST",
	"SAFE_TO_RETRY",
	"READY_LOCAL",
	"READY_IDENTITY",
	"READY_BATCH",
	"READY_WORK_ORDER",
)
_WAITING_TOKENS = (
	"WAITING",
	"DEPENDENCY",
	"DOWNSTREAM_PENDING",
	"DOWNSTREAM_REPLAY",
	"PATIENT_ZERO",
	"REPLAY_REQUIRED",
)


def _raw_status(row: dict | None) -> str:
	row = row or {}
	return str(
		row.get("primary_state")
		or row.get("leftover_ma_status")
		or row.get("planner_status")
		or row.get("status")
		or row.get("reason")
		or ""
	)


def primary_state(row: dict | None) -> str:
	"""Map any historical status onto the six primary states."""
	raw = _raw_status(row)
	up = raw.upper()
	if any(tok in up for tok in _FAILED_TOKENS):
		return PRIMARY_FAILED
	if any(tok in up for tok in _REPAIRED_TOKENS) and "FAILED" not in up:
		return PRIMARY_REPAIRED
	if any(tok in up for tok in _LEGITIMATE_TOKENS):
		return PRIMARY_LEGITIMATE
	if any(tok in up for tok in _READY_TOKENS):
		return PRIMARY_READY
	if any(tok in up for tok in _WAITING_TOKENS):
		return PRIMARY_WAITING
	if "MANUAL" in up or "AMBIGUOUS" in up or up in ("BLOCKED", "NO_REPAIR_PATH"):
		return PRIMARY_MANUAL
	return PRIMARY_MANUAL


def root_family(row: dict | None) -> str:
	"""One of five families. Symptoms (wrong rate, leftover MA, …) stay reasons."""
	row = row or {}
	topic = str(row.get("topic") or row.get("repair_class") or row.get("reason") or "").upper()
	if "POSTING" in topic:
		return FAMILY_POSTING_ORDER
	if "MANUFACTURE" in topic or topic in ("I1_NEGATIVE_RATE_REPAIR", "TOPIC_I1", "I1"):
		return FAMILY_MANUFACTURE_FLOW
	if "GL" in topic or "ACCOUNT" in topic:
		return FAMILY_ACCOUNTING
	if "BIN" in topic or topic in ("SLE_BIN", "I4_LEFTOVER", "I4_LEFTOVER_REPAIR", "DERIVED_STATE"):
		return FAMILY_DERIVED_STATE
	return FAMILY_VALUATION


def reason_code(row: dict | None) -> str:
	row = row or {}
	explicit = str(row.get("reason") or row.get("zero_reason") or "").strip()
	if explicit in {
		REASON_LEFTOVER_MA,
		REASON_POSTING_ORDER,
		REASON_WRONG_RATE,
		REASON_ZERO_RATE,
		REASON_BIN_DRIFT,
		REASON_GL_DRIFT,
		REASON_MANUFACTURE_FLOW,
	}:
		return explicit
	topic = str(row.get("topic") or row.get("repair_class") or "").upper()
	if "LEFTOVER_MA" in topic:
		return REASON_LEFTOVER_MA
	if "POSTING" in topic:
		return REASON_POSTING_ORDER
	if "MANUFACTURE" in topic:
		return REASON_MANUFACTURE_FLOW
	if "GL" in topic:
		return REASON_GL_DRIFT
	if "BIN" in topic or "I4" in topic:
		return REASON_BIN_DRIFT
	if "ZERO" in topic:
		return REASON_ZERO_RATE
	if "WRONG" in topic:
		return REASON_WRONG_RATE
	return explicit or topic or "UNSPECIFIED"


def explain_root(row: dict | None) -> dict:
	"""Administrator-facing explanation. No planner-version jargon."""
	row = row or {}
	state = primary_state(row)
	family = root_family(row)
	reason = reason_code(row)
	return {
		"primary_state": state,
		"root_family": family,
		"reason": reason,
		"what_happened": row.get("what_happened") or row.get("reason") or "",
		"expected_state": row.get("expected_state")
		or row.get("expected_ma")
		or row.get("expected_target_rate")
		or "",
		"current_state": row.get("current_state")
		or row.get("current_valuation_rate")
		or row.get("current_target_rate")
		or "",
		"proposed_repair": row.get("proposed_repair")
		or (
			"Repair the earliest root, then let official ERPNext repost/RIV rebuild descendants."
			if state == PRIMARY_READY
			else "No automatic repair."
		),
		"impact": row.get("impact") or row.get("replay_count") or row.get("sql_updates") or 0,
		"why_safe": row.get("why_safe")
		or (
			"Deterministic reconstruction; manufacturing quantities are not changed."
			if state == PRIMARY_READY
			else ""
		),
		"dependencies": row.get("dependencies") or row.get("patient_zero") or "",
		"result": row.get("result") or state,
	}


def annotate_row(row: dict | None) -> dict:
	row = dict(row or {})
	row["primary_state"] = primary_state(row)
	row["root_family"] = root_family(row)
	row["reason_code"] = reason_code(row)
	row["explanation"] = explain_root(row)
	return row


def project_simple_kpis(dashboard: dict | None, *, workers: dict | None = None, scanned_at=None) -> dict:
	"""Roll existing topic KPIs into the six-state dashboard."""
	d = dict(dashboard or {})
	ready = (
		int(d.get("Wrong Rate READY") or 0)
		+ int(d.get("READY_I4") or 0)
		+ int(d.get("READY_I1") or 0)
		+ int(d.get("READY_LEFTOVER_MA") or 0)
		+ int(d.get("GL READY") or 0)
		+ int(d.get("Repairable") or 0)
	)
	# Repairable already includes leftover-MA / I4 / I1 / GL; do not double-count
	# when Repairable is present.
	if d.get("Repairable") not in (None, ""):
		ready = int(d.get("Repairable") or 0)
	waiting = (
		int(d.get("Wrong Rate WAITING") or 0)
		+ int(d.get("WAITING_I4") or 0)
		+ int(d.get("WAITING_I1") or 0)
		+ int(d.get("GL WAITING") or 0)
		+ int(d.get("RIV WAITING") or 0)
	)
	manual = (
		int(d.get("Wrong Rate MANUAL") or 0)
		+ int(d.get("MANUAL_I4") or 0)
		+ int(d.get("MANUAL_I1") or 0)
		+ int(d.get("MANUAL_LEFTOVER_MA") or 0)
		+ int(d.get("GL MANUAL") or 0)
		+ int(d.get("Manual") or 0)
		+ int(d.get("User Action Required") or 0)
	)
	legitimate = (
		int(d.get("Proven Legitimate Zero") or 0)
		+ int(d.get("Zero Rate No Action") or 0)
		+ int(d.get("Legitimate Scrap Zero Rate") or 0)
	)
	failed = int(d.get("Failed RIV Actionable") or d.get("Failed RIV") or 0)
	d["Ready to Repair"] = ready
	d["Needs Review"] = manual
	d["Legitimate / No Action"] = legitimate
	d["Blocked"] = waiting
	d["Failed"] = failed
	if workers is not None:
		d["Workers"] = int((workers or {}).get("workers_for_queue") or 0)
	if scanned_at is not None:
		d["Last Scan"] = str(scanned_at)
	elif "Last Scan" not in d:
		d["Last Scan"] = ""
	return d


def lifecycle() -> tuple[str, ...]:
	return ("SCAN", "ROOT_CAUSE", "DRY_RUN", "REPAIR", "REPOST", "VERIFY")
