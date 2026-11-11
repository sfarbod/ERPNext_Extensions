# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Ensure Assigned DP → Registered (Return from Debt Purchase) Desk Workflow transition.

Re-runs the Debt Purchase workflow ensure so sites that already applied the older
Assigned-bounce-only set pick up the unassign exit without a full fixtures reload.
"""

from __future__ import annotations

from erpnext_extensions.patches.post_model_sync.ensure_debt_purchase_pdc_workflow import execute as _sync


def execute():
	_sync()
