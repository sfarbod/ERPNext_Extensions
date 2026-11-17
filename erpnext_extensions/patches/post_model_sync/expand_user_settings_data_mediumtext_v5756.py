# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Post-model-sync: widen ``__UserSettings.data`` TEXT → MEDIUMTEXT (v5.7.56).

Justification
-------------
Frappe 16.54.0 creates ``__UserSettings.data`` as ``TEXT`` (65,535 bytes).
Account Explorer (erpnext_extensions) stores Grid preferences plus Workspace
share tokens in a single User Settings blob. Production rows already approach
the TEXT ceiling (~64–65 KB). Account renames call ``sync_user_settings()``
which flushes every Redis ``_user_settings`` entry; oversized Account Explorer
rows raise ``MySQLdb.DataError: (1406, "Data too long for column 'data'")``
and abort the rename transaction.

MEDIUMTEXT (16 MB) is the minimal, reversible schema change that preserves all
user preferences without truncating JSON. Rollback: if every ``data`` value is
≤ 65,535 bytes, ``ALTER ... MODIFY data TEXT`` is safe; otherwise keep
MEDIUMTEXT.

No DocType metadata change is required — ``__UserSettings`` is an internal
Frappe table without a DocType definition.
"""

from __future__ import annotations

import frappe

from erpnext_extensions.user_settings_compat import ensure_user_settings_data_mediumtext


def execute() -> None:
	logger = frappe.logger("erpnext_extensions.expand_user_settings_data_mediumtext_v5756")
	logger.info("Starting expand_user_settings_data_mediumtext_v5756")
	action = ensure_user_settings_data_mediumtext(logger=logger)
	logger.info("Completed expand_user_settings_data_mediumtext_v5756 action=%s", action)
