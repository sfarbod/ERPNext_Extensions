# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Re-apply Costing English desk sidebar after LinkValidationError hotfix.

Idempotent. Covers sites where ``ensure_costing_english_desk_v5727`` already
completed before the DocType-ensure fix, and complements a production retry of
the original patch when it failed before Patch Log insertion.
"""

from __future__ import annotations

from erpnext_extensions.patches.post_model_sync.ensure_costing_english_desk_v5727 import (
	execute as run_english_desk,
)


def execute():
	run_english_desk()
