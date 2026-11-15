# Copyright (c) 2026, ERPNext Extensions contributors
"""Apply advance-account runtime patches (idempotent)."""

from __future__ import annotations

_APPLIED = False


def apply() -> None:
	global _APPLIED
	from erpnext_extensions.advance_accounts.custom_fields import ensure_custom_fields
	from erpnext_extensions.advance_accounts.discovery import apply_discovery_patch

	ensure_custom_fields()
	apply_discovery_patch()
	_APPLIED = True
