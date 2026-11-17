# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Compatibility layer for Frappe ``__UserSettings.data`` overflow on DocType rename.

Root cause (Frappe 16.54.0):
  ``rename_doc`` → ``update_user_settings`` → ``sync_user_settings()`` flushes the
  entire Redis ``_user_settings`` hash into ``__UserSettings``. That table is created
  with ``data TEXT`` (max 65,535 bytes). Account Explorer (erpnext_extensions) stores
  Grid preferences plus Workspace share tokens in the same blob; several users already
  sit at ~64–65 KB. Any further growth — or a Redis copy slightly larger than TEXT —
  raises ``MySQLdb.DataError: (1406, "Data too long for column 'data'")`` and aborts
  Account number renames (and any other rename that syncs user settings).

Fix:
  1. Widen ``__UserSettings.data`` to ``MEDIUMTEXT`` (idempotent DDL).
  2. Monkey-patch ``sync_user_settings`` so rename paths use the widened column and
     compact Account Explorer token maps that still exceed a soft byte budget.
  3. Monkey-patch ``rename_doc.update_user_settings`` so Linked DocType filter
     rewrites use a contains-match (``LIKE %old%``). Frappe 16.54.0 issues
     ``LIKE 'old'`` without wildcards, which never matches JSON blobs.
  4. Never swallow unrelated DB errors; never truncate arbitrary JSON blindly.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

import frappe
from frappe import safe_decode
from frappe.model.utils.user_settings import update_user_settings_data
from frappe.utils import cstr

TEXT_BYTE_LIMIT = 65_535
# Soft budget kept under legacy TEXT so AE clients remain compatible even before DDL.
AE_SOFT_BYTE_BUDGET = 60_000
AE_DOCTYPE = "Account Explorer"
AE_WORKSPACE_SECTION = "Workspace"
AE_TOKENS_KEY = "tokens"

_PATCHED = False


def _like_contains_pattern(value: str) -> str:
	"""Build a parameterized LIKE pattern that contains ``value`` (escape % / _)."""
	escaped = cstr(value).replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
	return f"%{escaped}%"


def read_user_settings_data_column_type() -> str | None:
	row = frappe.db.sql(
		"""
		SELECT COLUMN_TYPE
		FROM INFORMATION_SCHEMA.COLUMNS
		WHERE TABLE_SCHEMA = DATABASE()
		  AND TABLE_NAME = '__UserSettings'
		  AND COLUMN_NAME = 'data'
		""",
		as_dict=True,
	)
	return (row[0].get("COLUMN_TYPE") if row else None)


def ensure_user_settings_data_mediumtext(*, logger=None) -> str:
	"""Idempotently widen ``__UserSettings.data`` from TEXT to MEDIUMTEXT."""
	logger = logger or frappe.logger("erpnext_extensions.user_settings_compat")
	current = (read_user_settings_data_column_type() or "").lower()
	if not current:
		# Table may not exist yet on a brand-new site; let Frappe create it first.
		try:
			frappe.db.create_user_settings_table()
		except Exception:
			logger.warning("Could not ensure __UserSettings table exists")
		current = (read_user_settings_data_column_type() or "").lower()
		if not current:
			return "SKIP_MISSING_TABLE"

	if "mediumtext" in current or "longtext" in current:
		return "SKIP_ALREADY_WIDE"

	if "text" not in current:
		logger.warning("Unexpected __UserSettings.data type %s; leaving unchanged", current)
		return "SKIP_UNEXPECTED_TYPE"

	frappe.db.sql_ddl("ALTER TABLE `__UserSettings` MODIFY `data` MEDIUMTEXT")
	logger.info("Widened __UserSettings.data from %s to MEDIUMTEXT", current)
	return "ALTER_TO_MEDIUMTEXT"


def _payload_bytes(data: Any) -> int:
	if data is None:
		return 0
	if isinstance(data, (bytes, bytearray)):
		return len(data)
	return len(cstr(data).encode("utf-8"))


def _prune_ae_tokens_to_budget(settings: dict[str, Any], *, budget: int = AE_SOFT_BYTE_BUDGET) -> dict[str, Any]:
	"""FIFO-prune Account Explorer Workspace.tokens until JSON fits ``budget`` bytes.

	Preserves Grid and all non-token sections. Only removes oldest tokens by
	``_stored_at`` (same policy as the JS token store).
	"""
	workspace = settings.get(AE_WORKSPACE_SECTION)
	if not isinstance(workspace, dict):
		return settings
	tokens = workspace.get(AE_TOKENS_KEY)
	if not isinstance(tokens, dict) or not tokens:
		return settings

	def encoded_size() -> int:
		return _payload_bytes(json.dumps(settings, ensure_ascii=False, separators=(",", ":")))

	if encoded_size() <= budget:
		return settings

	ordered = sorted(
		tokens.keys(),
		key=lambda key: str((tokens.get(key) or {}).get("_stored_at") or ""),
	)
	# Drop oldest first until under budget or empty.
	for key in ordered:
		if encoded_size() <= budget:
			break
		tokens.pop(key, None)
	workspace[AE_TOKENS_KEY] = tokens
	settings[AE_WORKSPACE_SECTION] = workspace
	return settings


def compact_user_settings_if_needed(doctype: str, data: Any) -> str:
	"""Return JSON string safe to persist; compact AE tokens when over soft budget."""
	raw = data
	if isinstance(raw, (bytes, bytearray)):
		raw = raw.decode("utf-8", errors="replace")
	raw = cstr(raw)
	if _payload_bytes(raw) <= AE_SOFT_BYTE_BUDGET:
		return raw
	if doctype != AE_DOCTYPE:
		# Non-AE oversized blobs: leave intact for MEDIUMTEXT persistence.
		return raw
	try:
		parsed = json.loads(raw)
	except Exception:
		return raw
	if not isinstance(parsed, dict):
		return raw
	compacted = _prune_ae_tokens_to_budget(parsed, budget=AE_SOFT_BYTE_BUDGET)
	return json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))


def sync_user_settings_safe() -> None:
	"""Drop-in replacement for ``frappe.model.utils.user_settings.sync_user_settings``."""
	ensure_user_settings_data_mediumtext()

	cache_items = frappe.cache.hgetall("_user_settings") or {}
	for key, data in cache_items.items():
		key = safe_decode(key)
		if "::" not in key:
			continue
		doctype, user = key.split("::", 1)
		if data is None:
			continue
		payload = compact_user_settings_if_needed(doctype, data)
		frappe.db.multisql(
			{
				"mariadb": """INSERT INTO `__UserSettings`(`user`, `doctype`, `data`)
				VALUES (%s, %s, %s)
				ON DUPLICATE key UPDATE `data`=%s""",
				"*": """INSERT INTO `__UserSettings` (`user`, `doctype`, `data`)
				VALUES (%s, %s, %s)
				ON CONFLICT (`user`, `doctype`) DO UPDATE SET `data`=%s""",
			},
			(user, doctype, payload, payload),
			as_dict=1,
		)


def update_user_settings_on_rename(old: str, new: str, link_fields: list[dict]) -> None:
	"""Compat replacement for ``frappe.model.rename_doc.update_user_settings``.

	Preserves Frappe's filter rewrite semantics, but:
	- Uses the safe sync (MEDIUMTEXT + AE compact).
	- Matches User Settings rows whose JSON *contains* the old name.
	"""
	sync_user_settings_safe()

	if not link_fields:
		return

	linked_doctypes = {d.parent for d in link_fields if not getattr(d, "issingle", False)}
	if not linked_doctypes:
		return

	# Parameterized contains-match — Frappe core uses LIKE without wildcards.
	user_settings_details = frappe.db.sql(
		"""
		SELECT `user`, `doctype`, `data`
		FROM `__UserSettings`
		WHERE `data` LIKE %s
		  AND `doctype` IN ({doctypes})
		""".format(doctypes=", ".join(["%s"] * len(linked_doctypes))),
		(_like_contains_pattern(old), *linked_doctypes),
		as_dict=True,
	)

	user_settings_dict: dict[str, list] = defaultdict(list)
	for user_setting in user_settings_details:
		user_settings_dict[user_setting.doctype].append(user_setting)

	for fields in link_fields:
		parent = getattr(fields, "parent", None) or fields.get("parent")
		fieldname = getattr(fields, "fieldname", None) or fields.get("fieldname")
		rows = user_settings_dict.get(parent)
		if not rows:
			continue
		for user_setting in rows:
			update_user_settings_data(user_setting, "value", old, new, "docfield", fieldname)


def install_user_settings_compat_patch() -> None:
	"""Monkey-patch Frappe user-settings sync + rename filter rewrite."""
	global _PATCHED
	import frappe.model.rename_doc as rename_doc
	import frappe.model.utils.user_settings as user_settings

	user_settings.sync_user_settings = sync_user_settings_safe
	# rename_doc imported sync_user_settings by name — rebind the module attribute.
	rename_doc.sync_user_settings = sync_user_settings_safe
	rename_doc.update_user_settings = update_user_settings_on_rename
	_PATCHED = True


def apply() -> None:
	install_user_settings_compat_patch()
