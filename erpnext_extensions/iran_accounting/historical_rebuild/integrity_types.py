# Copyright (c) 2026, ERPNext Extensions contributors
"""Classification / severity contracts for historical valuation integrity (v5.6.1).

Campaign ``item_status`` Select values stay compatible with prior releases.
Evidence-based verification classes are stored on campaign items via
``verification_class`` / ``verification_detail`` and mapped onto existing
``item_status`` values by re-evaluate / coverage paths.
"""

from __future__ import annotations

# Evidence classifications (scanner + coverage verify)
CONFIRMED_CORRUPTION = "CONFIRMED_CORRUPTION"
SUSPECTED_CORRUPTION = "SUSPECTED_CORRUPTION"
LEGITIMATE_ACCOUNTING_BEHAVIOR = "LEGITIMATE_ACCOUNTING_BEHAVIOR"
NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"
REPAIRED_AND_VERIFIED = "REPAIRED_AND_VERIFIED"

# Campaign re-evaluation intent (mapped to item_status)
VERIFIED_CLEAN = "VERIFIED_CLEAN"  # legacy inspect-only — NOT a final success state
REPOST_REQUIRED = "REPOST_REQUIRED"
REPOST_PLANNED = "REPOST_PLANNED"
REPOST_IN_PROGRESS = "REPOST_IN_PROGRESS"
REPOST_RUNNING = "REPOST_RUNNING"
REPOST_COMPLETED_PENDING_VERIFICATION = "REPOST_COMPLETED_PENDING_VERIFICATION"
REPOST_FAILED = "REPOST_FAILED"
MANUAL_REVIEW = "MANUAL_REVIEW"

# Final successful classifications (v5.6.1 no-skip policy)
# Coverage = actual recalculation by a session RIV, not inspect-only cleanliness.
DIRECT_REPOSTED_AND_VERIFIED = "DIRECT_REPOSTED_AND_VERIFIED"
COVERED_BY_REPOSTED_AND_VERIFIED = "COVERED_BY_REPOSTED_AND_VERIFIED"

FINAL_SUCCESS_CLASSES = frozenset(
	{DIRECT_REPOSTED_AND_VERIFIED, COVERED_BY_REPOSTED_AND_VERIFIED}
)

# Remarks marker written on every RIV created during a controlled rebuild session
SESSION_RIV_REMARK_PREFIX = "HRC561_SESSION:"

# Anomaly type codes
ANOMALY_ZERO_QTY_RESIDUAL = "ZERO_QTY_RESIDUAL"
ANOMALY_INVALID_OUTGOING = "INVALID_OUTGOING_VALUATION"  # I3
ANOMALY_INVALID_INCOMING = "INVALID_INCOMING_VALUATION"  # I4-ish
ANOMALY_NEGATIVE_STOCK_VALUE = "NEGATIVE_STOCK_VALUE"
ANOMALY_VALUATION_CONTINUITY = "VALUATION_CONTINUITY"
ANOMALY_QTY_CONTINUITY = "QTY_CONTINUITY"
ANOMALY_SLE_GL = "SLE_GL_INTEGRITY"
ANOMALY_TVA = "TVA_UNRESOLVED"
ANOMALY_RIV_FAILURE = "RIV_FAILURE"
ANOMALY_BIN = "BIN_INCONSISTENCY"
ANOMALY_DOWNSTREAM = "DOWNSTREAM_DEPENDENCY"

SEVERITY_CRITICAL = "CRITICAL"
SEVERITY_HIGH = "HIGH"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_LOW = "LOW"
SEVERITY_INFO = "INFO"

# Quantum floors (IRR integer currency → 1; float currencies use precision)
DEFAULT_VALUE_TOLERANCE = 1.0
DEFAULT_QTY_TOLERANCE = 1e-6
