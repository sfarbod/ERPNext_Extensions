# Copyright (c) 2026, ERPNext Extensions contributors
"""Manufacturing Cost Adjustment (MCA) constants — v5.7.35."""

from __future__ import annotations

MCA_DOCTYPE = "Manufacturing Cost Adjustment"
MCA_COST_DOCTYPE = "Manufacturing Cost Adjustment Cost"
MODULE = "erpnext_extensions"

# processing_status values (operational; distinct from docstatus)
PS_DRAFT = "Draft"
PS_QUEUED = "Queued"
PS_APPENDING = "Appending Costs"
PS_ALLOCATING = "Allocating"
PS_REBUILDING_SLE = "Rebuilding SLE"
PS_REBUILDING_GL = "Rebuilding GL"
PS_VALIDATING = "Validating Contract"
PS_PENDING_RIV = "Pending RIV"
PS_RECONCILING = "Reconciling"
PS_COMPLETED = "Completed"
PS_FAILED = "Failed"
PS_VOIDED = "VoidedByStockEntryCancel"

PROCESSING_STATUSES = (
	PS_DRAFT,
	PS_QUEUED,
	PS_APPENDING,
	PS_ALLOCATING,
	PS_REBUILDING_SLE,
	PS_REBUILDING_GL,
	PS_VALIDATING,
	PS_PENDING_RIV,
	PS_RECONCILING,
	PS_COMPLETED,
	PS_FAILED,
	PS_VOIDED,
)

TERMINAL_OK = frozenset({PS_COMPLETED, PS_VOIDED})
NON_TERMINAL = frozenset(
	{
		PS_QUEUED,
		PS_APPENDING,
		PS_ALLOCATING,
		PS_REBUILDING_SLE,
		PS_REBUILDING_GL,
		PS_VALIDATING,
		PS_PENDING_RIV,
		PS_RECONCILING,
		PS_FAILED,
	}
)

# Checkpoints (durable resume points)
CP_NONE = ""
CP_COSTS_APPENDED = "costs_appended"
CP_ALLOCATED = "allocated"
CP_SLE_REFRESHED = "sle_refreshed"
CP_GL_REFRESHED = "gl_refreshed"
CP_CONTRACT_OK = "contract_ok"
CP_RIV_QUEUED = "riv_queued"
CP_COMPLETED = "completed"

# Repost status
RS_NOT_REQUIRED = "Not Required"
RS_REQUIRED = "Required"
RS_QUEUED = "Queued"
RS_IN_PROGRESS = "In Progress"
RS_COMPLETED = "Completed"
RS_FAILED = "Failed"

# Provenance field on Landed Cost Taxes and Charges
CF_MCA = "custom_manufacturing_cost_adjustment"
CF_MCA_COST_ROW = "custom_mca_cost_row"
CF_COMPANY_ENABLE = "custom_enable_manufacturing_cost_adjustment"

LOCK_PREFIX = "mca:se:"
MAX_RETRY_ATTEMPTS = 50
RETRY_BACKOFF_SECONDS = (60, 120, 300, 600, 900)

# RQ queue for MCA orchestration (process_mca).
# Must be a standard Frappe queue: short | default | long (custom names
# raise ``Queue should be one of short, default, long`` on enqueue).
# Independent Stock Entries may run concurrently (configurable long workers).
# Same-SE exclusivity is enforced by ``locks.acquire_se_lock`` (per document).
# Heavy Repost Item Valuation execution also uses ``long`` (see
# ``_kick_riv_execution``); per-SE locks keep MCA/RIV for the same SE serial.
MCA_RQ_QUEUE = "long"
MCA_RIV_RQ_QUEUE = "long"
VALID_FRAPPE_QUEUES = frozenset({"short", "default", "long"})
MCA_JOB_TIMEOUT = 3600
# Default parallel MCA workers. Override via common_site_config
# ``workers.mca.concurrency`` or env ``MCA_WORKER_CONCURRENCY``.
MCA_DEFAULT_CONCURRENCY = 3

# Permanent business failures — scheduler must not auto-retry these.
PERMANENT_ERROR_MARKERS = (
	"negative pool_value",
)

# Operational quarantine — never auto-enqueue / process_mca / mass-run.
# Documents stay as-is (no delete/cancel). See QUARANTINE_MCA_00004.md.
# Cleared after 20261123_131558 Production restore: MCA-2026-00004 is a clean
# Farvardin draft for MAT-STE-2026-25911-2 (not the prior Dev split-brain doc).
QUARANTINED_MCA_NAMES = frozenset()

TRANSIENT_ERROR_MARKERS = (
	"Deadlock",
	"Lock wait timeout",
	"Request timed out",
	"JobTimeoutException",
	"TooManyWrites",
	"OperationalError",
	"InterfaceError",
	"Connection",
	"Temporary failure",
	"Queue",
	"lock_busy",
	"Record has changed",
)
