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

TRANSIENT_ERROR_MARKERS = (
	"Deadlock",
	"Lock wait timeout",
	"Request timed out",
	"JobTimeoutException",
	"OperationalError",
	"InterfaceError",
	"Connection",
	"Temporary failure",
	"Queue",
)
