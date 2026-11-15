# Copyright (c) 2026, ERPNext Extensions contributors
"""Constants for advance account classification (v5.7.42)."""

from __future__ import annotations

ADVANCE_RECEIVED = "Advance Received"
ADVANCE_PAID = "Advance Paid"
CF_ADVANCE_ACCOUNT_TYPE = "advance_account_type"
MODULE = "ERPNext Extensions"

# Select options: leading empty = None
ADVANCE_ACCOUNT_TYPE_OPTIONS = f"\n{ADVANCE_RECEIVED}\n{ADVANCE_PAID}"

CLASSIFICATION_RULES = {
	ADVANCE_RECEIVED: {"root_type": "Liability", "account_type": "Receivable"},
	ADVANCE_PAID: {"root_type": "Asset", "account_type": "Payable"},
}

DIRECTION_RECEIVED = "received"
DIRECTION_PAID = "paid"
