# Copyright (c) 2026, ERPNext Extensions contributors
"""Reusable ETO Subcontracting Receipt valuation propagation (v5.8.3).

Orchestrates supported discovery + Repost Item Valuation so a corrected SCR
batch rate reaches Material Transfer for Manufacture, Manufacture consumption,
finished goods, and later issues — without direct SLE/GL/Bin/Batch edits.

Depends on ``eto_scr_repost_scope`` (v5.8.1) for integer SABE outward-rate
selection during ETO-scoped reposts. See
``docs/investigations/ETO_SCR_REPOST_PROPAGATION.md``.
"""

from __future__ import annotations

API_VERSION = "eto-scr-valuation-propagation-v1"
CONFIRM_APPLY = "APPLY_ETO_SCR_VALUATION_PROPAGATION"
MAX_ALLOWLIST = 50
# Absolute IRR tolerance when comparing expected vs observed rates.
RATE_TOLERANCE = 0.5
