# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""v5.5.8b: re-run Asset Movement DocType Link cleanup.

The first v558 patch filtered ``parenttype='DocType'`` only. Customize Form
custom links use ``parenttype='Customize Form'`` and were skipped. This patch
re-invokes the corrected matcher (idempotent).
"""

from __future__ import annotations

from erpnext_extensions.patches.post_model_sync.remove_duplicate_asset_movement_doctype_link_v558 import (
	execute as _execute,
)


def execute():
	_execute()
