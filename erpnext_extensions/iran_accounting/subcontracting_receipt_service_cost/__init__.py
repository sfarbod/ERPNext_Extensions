# Copyright (c) 2026, ERPNext Extensions contributors
"""Reusable Subcontracting Receipt service-cost correction from linked PO amounts.

Resolves applicable Purchase Order service row amounts (never ``last_purchase_rate``),
allocates them across SCO/SCR chains, and corrects submitted SCR item
``service_cost_per_qty`` / rate / amount via ERPNext's ``calculate_items_qty_and_amount``.

RIV / ETO downstream propagation is a separately controlled phase — see
``docs/investigations/ETO_SCR_REPOST_PROPAGATION.md``.
"""

from __future__ import annotations

API_VERSION = "scr-service-cost-correction-v1"
CONFIRM_APPLY = "APPLY_SCR_SERVICE_COST_CORRECTION"
# Absolute IRR tolerance when comparing intended vs current service_cost_per_qty.
SERVICE_COST_TOLERANCE = 0.01
# Soft cap on allowlisted SCR names per API call.
MAX_ALLOWLIST = 100
