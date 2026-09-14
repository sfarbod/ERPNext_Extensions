# Copyright (c) 2026, ERPNext Extensions contributors
"""Re-run Historical Repair navigation so Stock Tools sidebar and workspace card exist."""

from erpnext_extensions.patches.post_model_sync.ensure_historical_repair_navigation import execute as _run


def execute():
	_run()
