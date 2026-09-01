# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

MODULE = "Asset Usage Depreciation"

MODE_NORMAL = "Normal"
MODE_PERCENTAGE = "Percentage"
MODE_NO_DEPRECIATION = "No Depreciation"

HANDLING_EXTEND = "Extend Depreciation Schedule"
HANDLING_ADJUST_FINAL = "Adjust Final Depreciation Installment"
# Legacy stored Company option (pre-4.1.2); treated as HANDLING_ADJUST_FINAL
HANDLING_REDISTRIBUTE_LEGACY = "Redistribute Within Remaining Schedule"
# Backward-compatible alias for imports that still use the old name
HANDLING_REDISTRIBUTE = HANDLING_ADJUST_FINAL

COMPANY_FIELD_REDUCED_HANDLING = "custom_reduced_depreciation_handling"

# Asset Request (v4.4.0) — acquisition of a new fixed asset only
COMPANY_FIELD_AR_REQUIRE_PLANNING = "custom_ar_require_planning_approval"
COMPANY_FIELD_AR_REQUIRE_CEO = "custom_ar_require_ceo_approval"
COMPANY_FIELD_AR_CEO_MIN_QTY = "custom_ar_ceo_min_qty"
COMPANY_FIELD_AR_POOL_LOCATION = "custom_ar_asset_pool_location"
COMPANY_FIELD_AR_DEFAULT_TARGET_LOCATION = "custom_ar_default_target_location"

ASSET_REQUEST_DOCTYPE = "Asset Request"
ASSET_REQUEST_ITEM_DOCTYPE = "Asset Request Item"
ASSET_REQUEST_ALLOCATION_DOCTYPE = "Asset Request Allocation"
ASSET_REQUEST_SETTINGS_DOCTYPE = "Asset Request Settings"

ASSET_REQUEST_NAMING_SERIES = "AUD-AR-.YYYY.-"

ROLE_AR_MANAGER = "Asset Request Manager"
ROLE_AR_PLANNER = "Asset Request Planner"
ROLE_AR_EXECUTIVE = "Asset Request Executive"
ROLE_ASSET_MANAGER = "Asset Manager"

WF_ASSET_REQUEST = "Asset Request Workflow"
WF_STATE_DRAFT = "Draft"
WF_STATE_PENDING_MANAGER = "Pending Manager Approval"
WF_STATE_PENDING_PLANNING = "Pending Planning Approval"
WF_STATE_PENDING_CEO = "Pending CEO Approval"
WF_STATE_APPROVED = "Approved"
WF_STATE_REJECTED = "Rejected"
WF_STATE_CANCELLED = "Cancelled"

ACTION_SUBMIT = "AR Submit for Approval"
ACTION_APPROVE = "AR Approve"
ACTION_REJECT = "AR Reject"
ACTION_SEND_BACK = "AR Send Back"

# Native Workflow condition: only the stamped line manager may act at manager stage.
MANAGER_APPROVER_CONDITION = "doc.manager_approver == frappe.session.user"

STATUS_DRAFT = "Draft"
STATUS_PENDING_MANAGER = "Pending Manager Approval"
STATUS_PENDING_PLANNING = "Pending Planning Approval"
STATUS_PENDING_CEO = "Pending CEO Approval"
STATUS_APPROVED = "Approved"
STATUS_PARTIALLY_FULFILLED = "Partially Fulfilled"
STATUS_FULFILLED = "Fulfilled"
STATUS_CLOSED = "Closed"
STATUS_REJECTED = "Rejected"
STATUS_CANCELLED = "Cancelled"

STATUS_OPTIONS = (
	f"{STATUS_DRAFT}\n{STATUS_PENDING_MANAGER}\n{STATUS_PENDING_PLANNING}\n"
	f"{STATUS_PENDING_CEO}\n{STATUS_APPROVED}\n{STATUS_PARTIALLY_FULFILLED}\n"
	f"{STATUS_FULFILLED}\n{STATUS_CLOSED}\n{STATUS_REJECTED}\n{STATUS_CANCELLED}"
)

ACTIVE_REQUEST_STATUSES = (
	STATUS_DRAFT,
	STATUS_PENDING_MANAGER,
	STATUS_PENDING_PLANNING,
	STATUS_PENDING_CEO,
	STATUS_APPROVED,
	STATUS_PARTIALLY_FULFILLED,
)

METHOD_PENDING = "Pending"
METHOD_ISSUE = "Issue Existing"
METHOD_PURCHASE = "Purchase"
METHOD_MIXED = "Mixed"

LINE_OPEN = "Open"
LINE_RESERVED = "Reserved"
LINE_ISSUED = "Issued"
LINE_PURCHASE_REQUESTED = "Purchase Requested"
LINE_RECEIVED = "Received"
LINE_CLOSED = "Closed"
LINE_CANCELLED = "Cancelled"

ALLOC_RESERVED = "Reserved"
ALLOC_MOVEMENT_DRAFT = "Movement Draft"
ALLOC_ISSUED = "Issued"
ALLOC_MR_DRAFT = "MR Draft"
ALLOC_MR_SUBMITTED = "MR Submitted"
ALLOC_ORDERED = "Ordered"
ALLOC_RECEIVED = "Received"
ALLOC_CANCELLED = "Cancelled"

# Fulfillment lifecycle (independent of workflow status)
FULFILLMENT_WAITING = "Waiting for fulfillment"
FULFILLMENT_ISSUED_FROM_POOL = "Issued from pool"
FULFILLMENT_PURCHASE_REQUESTED = "Purchase requested"
FULFILLMENT_FULFILLED = "Fulfilled"
FULFILLMENT_STATUS_OPTIONS = (
	f"{FULFILLMENT_WAITING}\n{FULFILLMENT_ISSUED_FROM_POOL}\n"
	f"{FULFILLMENT_PURCHASE_REQUESTED}\n{FULFILLMENT_FULFILLED}"
)

UNAVAILABLE_ASSET_STATUSES = (
	"Draft",
	"Cancelled",
	"Sold",
	"Scrapped",
	"Out of Order",
	"Work In Progress",
	"Capitalized",
)

OPEN_ALLOCATION_STATUSES = (
	ALLOC_RESERVED,
	ALLOC_MOVEMENT_DRAFT,
	ALLOC_ISSUED,
	ALLOC_MR_DRAFT,
	ALLOC_MR_SUBMITTED,
	ALLOC_ORDERED,
	ALLOC_RECEIVED,
)

MAX_MODE_A_EXTENSION_PERIODS = 1200

# ---------------------------------------------------------------------------
# LOCKED DEFAULT USAGE RULE
# ---------------------------------------------------------------------------
# If no submitted Asset Usage Period covers a given date, the effective
# depreciation mode is Normal (100%). This is the central timeline fallback.
#
# Applies when:
#   - the Asset has no Usage Period records at all
#   - the date is before the first Usage Period
#   - there is a gap between two Usage Periods
#   - a closed period has ended and no later period covers the date
#
# Do NOT create automatic Normal Usage Period rows on Asset creation.
# Do NOT require explicit Normal records to establish the default state.
# Explicit Normal records are optional status-change markers only.
#
# factor_on_date / day_weighted_factor MUST return this factor for uncovered dates.
DEFAULT_USAGE_FACTOR = 1.0
