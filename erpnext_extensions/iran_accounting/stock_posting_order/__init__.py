# Copyright (c) 2026, ERPNext Extensions contributors
"""Production stock posting-order repair and prevention (5.2.1).

ERPNext 16.34.2 processes Stock Ledger Entries by
``(posting_datetime ASC, creation ASC)``. Same-second prerequisite inbound and
dependent outbound movements can therefore invert when the outbound SLE was
created first, producing a temporary negative running quantity.

This package:

* groups SLE by item + warehouse + canonical batch (SABB-aware) + posting time
* historically repairs only when the current ``(posting_datetime, creation)``
  sequence creates a temporary negative, using the minimum seconds required
* never globally monkey-patches Stock Entry timestamps
* future prevention still forces proven prerequisite → dependent chronology
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
STATUS_NO_REPAIR_NEEDED = "NO_REPAIR_NEEDED"
STATUS_REPAIRABLE_SECONDS = "REPAIRABLE_SECONDS"
STATUS_INSUFFICIENT_STOCK = "INSUFFICIENT_STOCK"
STATUS_REAL_STOCK_SHORTAGE = "REAL_STOCK_SHORTAGE"
STATUS_MIDNIGHT = "MANUAL_REVIEW_MIDNIGHT"
STATUS_MIDNIGHT_REVIEW = "MIDNIGHT_REVIEW"
STATUS_CYCLE = "CYCLE"
STATUS_DEPENDENCY_CONFLICT = "DEPENDENCY_CONFLICT"
STATUS_CROSS_ITEM_CONFLICT = "CROSS_ITEM_CONFLICT"
STATUS_AMBIGUOUS_RELATIONSHIP = "AMBIGUOUS_RELATIONSHIP"
STATUS_VALUATION_POISON = "VALUATION_POISON_DEPENDENCY"
STATUS_STALE = "STALE_PREVIEW"
STATUS_CANCELLED = "CANCELLED"
STATUS_DRAFT = "DRAFT"
STATUS_REPAIRED = "REPAIRED"
STATUS_DRY_RUN = "DRY_RUN"
STATUS_BLOCKED = "BLOCKED"

MAX_OFFSET_SECONDS = 60
