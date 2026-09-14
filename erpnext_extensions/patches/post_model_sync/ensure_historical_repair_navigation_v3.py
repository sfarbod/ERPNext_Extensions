# Copyright (c) 2026, ERPNext Extensions contributors
"""Put Historical Repair on the Stock Tools card, shortcut chip, and Tools sidebar."""

from erpnext_extensions.patches.post_model_sync.ensure_historical_repair_navigation import execute as _run


def execute():
	_run()
