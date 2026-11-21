# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Monthly credit-side Labor/Overhead Control allocation (v5.8.5).

Reuses MFG Cost Batch Expense Allocation shares and residual policy.
Does not post GL/SLE/SED/SABE/RIV.
"""

from __future__ import annotations

DOCTYPE = "MFG Cost Control Credit Allocation"
SOURCE_CHILD = "MFG Cost Control Credit Source"
BATCH_CHILD = "MFG Cost Control Credit Batch Row"
PRODUCT_CHILD = "MFG Cost Control Credit Product Row"

ACTIVE_STATUSES = frozenset({"Draft", "Calculated", "Submitted"})
CONSUMING_STATUSES = frozenset({"Calculated", "Submitted"})
