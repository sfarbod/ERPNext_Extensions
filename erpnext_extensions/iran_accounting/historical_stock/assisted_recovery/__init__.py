# Copyright (c) 2026, ERPNext Extensions contributors
"""Phase 3 — Assisted Recovery taxonomy.

Converts residual MANUAL / AMBIGUOUS / WAITING cases into:

  READY | ASSISTED_READY | OPERATOR_DECISION | NO_EVIDENCE | REAL_STOCK_SHORTAGE

ASSISTED_READY is never auto-executed — operator confirms via the decision wizard.
"""

from __future__ import annotations

# Assisted outcome classes (bucket labels for Master Assisted Recovery Plan)
BUCKET_AUTO = "AUTO"
BUCKET_ASSISTED = "ASSISTED"
BUCKET_OPERATOR = "OPERATOR_DECISION"
BUCKET_NO_EVIDENCE = "NO_EVIDENCE"
BUCKET_REAL_SHORTAGE = "REAL_STOCK_SHORTAGE"

# Planner / outcome statuses
ASSISTED_READY = "ASSISTED_READY"
OPERATOR_DECISION = "OPERATOR_DECISION"
NO_EVIDENCE = "NO_EVIDENCE"
REAL_STOCK_SHORTAGE = "REAL_STOCK_SHORTAGE"

# Default promotion threshold for ASSISTED_READY (overridable per call)
DEFAULT_ASSISTED_THRESHOLD = 0.95

# Source reliability weights for confidence scoring (Bin is never admitted)
SOURCE_WEIGHTS = {
	"transfer_source": 0.95,
	"purchase_receipt": 0.92,
	"previous_healthy_sle": 0.90,
	"stock_reconciliation": 0.88,
	"batch_inward": 0.85,
	"implied_svd": 0.80,
	"version": 0.35,
	"manufacture_pool": 0.40,
	"moving_average": 0.25,
	"manual": 0.0,
}

__all__ = [
	"BUCKET_AUTO",
	"BUCKET_ASSISTED",
	"BUCKET_OPERATOR",
	"BUCKET_NO_EVIDENCE",
	"BUCKET_REAL_SHORTAGE",
	"ASSISTED_READY",
	"OPERATOR_DECISION",
	"NO_EVIDENCE",
	"REAL_STOCK_SHORTAGE",
	"DEFAULT_ASSISTED_THRESHOLD",
	"SOURCE_WEIGHTS",
]
