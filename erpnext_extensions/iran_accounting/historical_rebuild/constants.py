# Copyright (c) 2026, ERPNext Extensions contributors
"""Constants for Historical Rebuild Campaign."""

from __future__ import annotations

CAMPAIGN_DOCTYPE = "Historical Rebuild Campaign"
ITEM_DOCTYPE = "Historical Rebuild Campaign Item"
LOG_DOCTYPE = "Historical Rebuild Campaign Log"

STATUS_DRAFT = "Draft"
STATUS_READY = "Ready"
STATUS_RUNNING = "Running"
STATUS_PAUSED = "Paused"
STATUS_STOPPED = "Stopped"
STATUS_COMPLETED = "Completed"
STATUS_FAILED = "Failed"

ITEM_PENDING = "Pending"
ITEM_PROCESSING = "Processing"
ITEM_SOURCE = "Source Rebuild"
ITEM_RIV_LINKED = "RIV Linked"
ITEM_COMPLETED = "Completed"
ITEM_FAILED = "Failed"
ITEM_BLOCKED = "Blocked"
ITEM_SKIPPED = "Skipped Covered"

SOURCE_NOT_REQUIRED = "Not Required"
SOURCE_REQUIRED = "Required"
SOURCE_DONE = "Done"
SOURCE_FAILED = "Failed"
SOURCE_BLOCKED = "Blocked"

TVA_PENDING = "Pending"
TVA_NA = "Not Applicable"
TVA_SUPERSEDED = "Superseded"
TVA_PRESERVED = "Preserved"
TVA_FAILED = "Failed"

# Submitted stock-affecting vouchers that create SLE.
SOURCE_SPECS = (
	{"doctype": "Stock Entry", "filters": {"docstatus": 1}},
	{"doctype": "Purchase Receipt", "filters": {"docstatus": 1}},
	{"doctype": "Delivery Note", "filters": {"docstatus": 1}},
	{"doctype": "Stock Reconciliation", "filters": {"docstatus": 1}},
	{"doctype": "Subcontracting Receipt", "filters": {"docstatus": 1}},
	{"doctype": "Purchase Invoice", "filters": {"docstatus": 1, "update_stock": 1}},
	{"doctype": "Sales Invoice", "filters": {"docstatus": 1, "update_stock": 1}},
)

DEFAULT_INTERNAL_BATCH = 25
MAX_LOG_ROWS = 200
RIV_SETTLE_POLL_SECONDS = 1
RIV_SETTLE_MAX_WAITS = 120  # ~2 min poll after sync execute; pending left for resume
MAX_ITEM_RETRIES = 3
# Backpressure: do not create more Core RIV while this many campaign-linked
# Queued/In Progress rows are still unsettled. Prevents Redis job floods.
MAX_OUTSTANDING_RIV = 8
