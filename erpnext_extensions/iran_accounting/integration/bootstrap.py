# Copyright (c) 2026, ERPNext Extensions contributors
"""Runtime bootstrap: worker guard + monkey patches (never at import time)."""

from __future__ import annotations


def apply() -> None:
	from erpnext_extensions.iran_accounting.worker.guard import ensure_runtime_ready

	ensure_runtime_ready()
	from erpnext_extensions.iran_accounting.integration.monkey_patches import (
		_patch_repost_entry_bootstrap,
		apply_monkey_patches,
	)
	from erpnext_extensions.safe_error_transport import apply_safe_error_transport

	# Install RIV entry-point wrap before heavier patches so a nested
	# execute_reposting_entry during apply still re-enters bootstrap safely.
	_patch_repost_entry_bootstrap()
	apply_monkey_patches()
	apply_safe_error_transport()
	from erpnext_extensions.asset_usage_depreciation.services.depr_posting_guard import (
		install_depreciation_idempotency_patch,
	)

	install_depreciation_idempotency_patch()
