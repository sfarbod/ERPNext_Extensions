# Copyright (c) 2026, ERPNext Extensions contributors
"""Per-voucher Asset Purchase Receipt account correction via RIV (5.7.18 local).

Corrects source Purchase Receipt ``expense_account`` mappings and rebuilds
accounting through Transaction RIV with ``repost_only_accounting_ledgers``.
Does not write GL/SLE via SQL and does not create or cancel Journal Entries.
Finance owns ``ACC-JV-1405-169046`` separately — this module must never touch it.
"""

from __future__ import annotations

API_VERSION = "asset-pr-correction-v1"
CONFIRM_APPLY = "APPLY_ASSET_PR_CORRECTION"
LAND_ACCOUNT_CODE_PREFIX = "121000"
DEFAULT_COMPANY = "اسپاد فارمد دارو"
# Explicit denylist — never cancel/amend/reverse from this workflow.
FINANCE_OWNED_JOURNAL_ENTRIES = frozenset({"ACC-JV-1405-169046"})
