# Copyright (c) 2026, ERPNext Extensions contributors
"""Targeted financial correction campaigns (5.7.18 local).

Two independently scoped cohorts:
  A. Asset Purchase Receipt Land account corrections (8 vouchers)
  B. Stock Entry fractional IRR rate identities (from investigation)

Dry-run / manifest preparation only until explicitly authorized.
Never touches ACC-JV-1405-169046. Never company-wide rebuild.
"""

from __future__ import annotations

API_VERSION = "financial-correction-campaign-v1"
DENYLIST_JOURNAL_ENTRIES = frozenset({"ACC-JV-1405-169046"})
DEFAULT_COMPANY = "اسپاد فارمد دارو"

# Approved Asset PR cohort: voucher → account number prefix (resolved per company).
ASSET_PR_APPROVED_ACCOUNT_PREFIX = {
	"MAT-PRE-2026-01907": "121013",
	"MAT-PRE-2026-00953": "121013",
	"MAT-PRE-2026-01311": "121006",
	"MAT-PRE-2026-00747": "121006",
	"MAT-PRE-2026-01413": "121003",
	"MAT-PRE-2026-00880": "121006",
	"MAT-PRE-2026-01241": "121003",
	"MAT-PRE-2026-01266": "121006",
}

CONFIRM_EXECUTE_ASSET_PR = "EXECUTE_ASSET_PR_COHORT"
CONFIRM_EXECUTE_STE_FRAC = "EXECUTE_STE_FRACTIONAL_COHORT"
