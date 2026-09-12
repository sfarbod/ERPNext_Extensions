# Copyright (c) 2026, ERPNext Extensions contributors
"""Production stock posting-order repair and prevention (5.2.1).

ERPNext 16.34.2 processes Stock Ledger Entries by
``(posting_datetime ASC, creation ASC)``. Same-second prerequisite inbound and
dependent outbound movements can therefore invert when the outbound SLE was
created first, producing a temporary negative running quantity.

This package:

* detects EXACT production dependencies only for auto-repair
* assigns the minimum +1s offsets on the dependent documents
* never globally monkey-patches Stock Entry timestamps
"""

from __future__ import annotations

MINIMUM_DEPENDENT_SECONDS = 1

PRODUCTION_PURPOSES = (
	"Material Transfer for Manufacture",
	"Material Consumption for Manufacture",
	"Manufacture",
	"Material Transfer",
	"Material Issue",
)

DEPENDENT_PURPOSES = (
	"Material Consumption for Manufacture",
	"Manufacture",
	"Material Transfer",
	"Material Issue",
)

PREREQUISITE_PURPOSES = (
	"Material Transfer for Manufacture",
	"Manufacture",
	"Material Transfer",
	"Material Receipt",
)

CONFIDENCE_EXACT = "EXACT"
CONFIDENCE_LIKELY = "LIKELY"
CONFIDENCE_AMBIGUOUS = "AMBIGUOUS"

STATUS_ELIGIBLE = "ELIGIBLE"
STATUS_NO_QTY_DEFICIT = "NO_QTY_DEFICIT"
STATUS_INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
STATUS_MIDNIGHT = "MANUAL_REVIEW_MIDNIGHT"
STATUS_CYCLE = "CYCLE"
STATUS_STALE = "STALE_PREVIEW"
STATUS_CANCELLED = "CANCELLED"
STATUS_DRAFT = "DRAFT"
STATUS_REPAIRED = "REPAIRED"
STATUS_DRY_RUN = "DRY_RUN"
STATUS_BLOCKED = "BLOCKED"
