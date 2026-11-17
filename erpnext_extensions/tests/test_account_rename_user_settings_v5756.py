# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Regression: Account rename must not fail on oversized User Settings (v5.7.56)."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import frappe

from erpnext_extensions.user_settings_compat import (
	AE_DOCTYPE,
	AE_SOFT_BYTE_BUDGET,
	TEXT_BYTE_LIMIT,
	compact_user_settings_if_needed,
	ensure_user_settings_data_mediumtext,
	install_user_settings_compat_patch,
	read_user_settings_data_column_type,
	sync_user_settings_safe,
)


def _utf8_len(payload: str) -> int:
	return len(payload.encode("utf-8"))


def _force_user_settings_data_type(column_type: str) -> None:
	frappe.db.sql_ddl(f"ALTER TABLE `__UserSettings` MODIFY `data` {column_type}")


def _make_ae_settings(*, token_count: int, token_pad: int, grid_pad: int = 2000) -> dict:
	tokens = {}
	for idx in range(token_count):
		tokens[f"tok{idx:02d}"] = {
			"company": "Test Co",
			"analysis_filters": {"pad": "x" * token_pad},
			"document_scope": {"pad": "y" * (token_pad // 2)},
			"_stored_at": f"2026-01-{idx + 1:02d} 10:00:00",
		}
	return {
		"Grid": {
			"columns": ["account", "debit", "credit"],
			"filters": [["Account", "name", "=", "KEEP-ME"]],
			"pad": "g" * grid_pad,
		},
		"Workspace": {"tokens": tokens},
		"Unrelated": {"theme": "light", "page_length": 50},
	}


def _cache_user_settings(doctype: str, user: str, settings: dict | str) -> None:
	payload = settings if isinstance(settings, str) else json.dumps(settings, ensure_ascii=False)
	frappe.cache.hset("_user_settings", f"{doctype}::{user}", payload)


def _read_db_user_settings(doctype: str, user: str) -> dict:
	row = frappe.db.sql(
		"select data from `__UserSettings` where user=%s and doctype=%s",
		(user, doctype),
	)
	if not row or not row[0][0]:
		return {}
	return json.loads(row[0][0])


def _clear_user_settings_row(doctype: str, user: str) -> None:
	frappe.db.sql(
		"delete from `__UserSettings` where user=%s and doctype=%s",
		(user, doctype),
	)
	frappe.cache.hdel("_user_settings", f"{doctype}::{user}")


def _next_free_account_number(start: int) -> int:
	n = start
	company = frappe.db.get_value("Company", {}, "name")
	while frappe.db.exists("Account", {"account_number": str(n), "company": company}):
		n += 1
	return n


class TestAccountRenameUserSettingsV5756(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		frappe.set_user("Administrator")
		install_user_settings_compat_patch()
		ensure_user_settings_data_mediumtext()
		cls.company = frappe.db.get_value("Company", {}, "name")
		if not cls.company:
			raise unittest.SkipTest("No Company available for Account rename tests")
		cls.root = frappe.db.get_value(
			"Account",
			{"company": cls.company, "is_group": 1, "root_type": "Liability"},
			"name",
		) or frappe.db.get_value("Account", {"company": cls.company, "is_group": 1}, "name")
		if not cls.root:
			raise unittest.SkipTest("No group Account available")

	def setUp(self):
		frappe.set_user("Administrator")
		install_user_settings_compat_patch()
		ensure_user_settings_data_mediumtext()
		self.users: list[tuple[str, str]] = []
		self.accounts: list[str] = []

	def tearDown(self):
		for doctype, user in self.users:
			_clear_user_settings_row(doctype, user)
		for name in self.accounts:
			if frappe.db.exists("Account", name):
				frappe.db.set_value("Account", name, "disabled", 1, update_modified=False)
		ensure_user_settings_data_mediumtext()
		frappe.db.commit()

	def _track_user(self, doctype: str, user: str) -> None:
		self.users.append((doctype, user))

	def _create_leaf_account(self, number: str, account_name: str) -> str:
		doc = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": account_name,
				"account_number": number,
				"parent_account": self.root,
				"company": self.company,
				"is_group": 0,
			}
		)
		doc.flags.ignore_permissions = True
		doc.insert()
		self.accounts.append(doc.name)
		frappe.db.commit()
		return doc.name

	def test_mediumtext_patch_idempotent(self):
		from erpnext_extensions.patches.post_model_sync.expand_user_settings_data_mediumtext_v5756 import (
			execute,
		)

		_force_user_settings_data_type("TEXT")
		self.assertIn("text", (read_user_settings_data_column_type() or "").lower())
		self.assertNotIn("mediumtext", (read_user_settings_data_column_type() or "").lower())
		execute()
		self.assertIn("mediumtext", (read_user_settings_data_column_type() or "").lower())
		execute()
		self.assertIn("mediumtext", (read_user_settings_data_column_type() or "").lower())

	def test_compact_preserves_grid_and_unrelated(self):
		settings = _make_ae_settings(token_count=20, token_pad=2500, grid_pad=8000)
		raw = json.dumps(settings, ensure_ascii=False)
		self.assertGreater(_utf8_len(raw), AE_SOFT_BYTE_BUDGET)
		compacted = json.loads(compact_user_settings_if_needed(AE_DOCTYPE, raw))
		self.assertLessEqual(
			_utf8_len(json.dumps(compacted, ensure_ascii=False, separators=(",", ":"))),
			AE_SOFT_BYTE_BUDGET,
		)
		self.assertEqual(compacted["Grid"]["filters"], settings["Grid"]["filters"])
		self.assertEqual(compacted["Unrelated"], settings["Unrelated"])
		self.assertLess(len(compacted["Workspace"]["tokens"]), 20)

	def test_sync_fails_on_text_succeeds_after_compat(self):
		user = frappe.session.user
		self._track_user(AE_DOCTYPE, user)
		_clear_user_settings_row(AE_DOCTYPE, user)

		settings = _make_ae_settings(token_count=16, token_pad=2800, grid_pad=12000)
		raw = json.dumps(settings, ensure_ascii=False)
		self.assertGreater(_utf8_len(raw), TEXT_BYTE_LIMIT)

		_force_user_settings_data_type("TEXT")
		_cache_user_settings(AE_DOCTYPE, user, raw)

		with self.assertRaises(Exception):
			frappe.db.multisql(
				{
					"mariadb": """INSERT INTO `__UserSettings`(`user`, `doctype`, `data`)
					VALUES (%s, %s, %s)
					ON DUPLICATE key UPDATE `data`=%s""",
					"*": """INSERT INTO `__UserSettings` (`user`, `doctype`, `data`)
					VALUES (%s, %s, %s)
					ON CONFLICT (`user`, `doctype`) DO UPDATE SET `data`=%s""",
				},
				(user, AE_DOCTYPE, raw, raw),
				as_dict=1,
			)

		frappe.db.rollback()
		_cache_user_settings(AE_DOCTYPE, user, raw)
		sync_user_settings_safe()
		frappe.db.commit()

		self.assertIn("mediumtext", (read_user_settings_data_column_type() or "").lower())
		stored = _read_db_user_settings(AE_DOCTYPE, user)
		self.assertEqual(stored["Grid"]["filters"], settings["Grid"]["filters"])
		self.assertEqual(stored["Unrelated"]["theme"], "light")
		self.assertIsInstance(stored.get("Workspace", {}).get("tokens"), dict)

	def test_rename_account_with_normal_user_settings(self):
		from erpnext.accounts.doctype.account.account import update_account_number

		old_number = str(_next_free_account_number(918801))
		new_number = str(_next_free_account_number(int(old_number) + 1))
		name = self._create_leaf_account(old_number, "Rename Normal Prefs")
		user = frappe.session.user
		# Frappe rewrite targets Link fields to Account (e.g. GL Entry.account), not Account.name.
		self._track_user("GL Entry", user)
		_cache_user_settings(
			"GL Entry",
			user,
			{
				"List": {
					"filters": [["GL Entry", "account", "=", name]],
					"order_by": "modified desc",
				},
				"Unrelated": {"ok": 1},
			},
		)
		sync_user_settings_safe()
		frappe.db.commit()

		new_name = update_account_number(name, "Rename Normal Prefs", new_number)
		frappe.db.commit()
		self.assertTrue(new_name)
		self.accounts.append(new_name)
		self.assertFalse(frappe.db.exists("Account", name))
		self.assertEqual(frappe.db.get_value("Account", new_name, "account_number"), new_number)

		stored = _read_db_user_settings("GL Entry", user)
		filters = (stored.get("List") or {}).get("filters") or []
		self.assertEqual(filters[0][3], new_name)
		self.assertEqual((stored.get("Unrelated") or {}).get("ok"), 1)

	def test_rename_with_extensive_ae_and_multi_user_settings(self):
		from erpnext.accounts.doctype.account.account import update_account_number

		old_number = str(_next_free_account_number(918811))
		new_number = str(_next_free_account_number(int(old_number) + 1))
		name = self._create_leaf_account(old_number, "Rename AE Prefs")

		users = [frappe.session.user]
		other = frappe.db.get_value(
			"User",
			{"name": ("not in", ["Administrator", "Guest"]), "enabled": 1},
			"name",
		)
		if other:
			users.append(other)

		for user in users:
			self._track_user(AE_DOCTYPE, user)
			self._track_user("GL Entry", user)
			ae = _make_ae_settings(token_count=14, token_pad=2200, grid_pad=10000)
			_cache_user_settings(AE_DOCTYPE, user, ae)
			_cache_user_settings(
				"GL Entry",
				user,
				{"List": {"filters": [["GL Entry", "account", "=", name]]}},
			)

		sync_user_settings_safe()
		frappe.db.commit()

		new_name = update_account_number(name, "Rename AE Prefs", new_number)
		frappe.db.commit()
		self.assertTrue(new_name)
		self.accounts.append(new_name)

		for user in users:
			ae_stored = _read_db_user_settings(AE_DOCTYPE, user)
			self.assertIn("Grid", ae_stored)
			self.assertEqual(ae_stored["Unrelated"]["theme"], "light")
			gle = _read_db_user_settings("GL Entry", user)
			filters = (gle.get("List") or {}).get("filters") or []
			self.assertEqual(filters[0][3], new_name)

	def test_repeated_renames_do_not_duplicate_settings(self):
		from erpnext.accounts.doctype.account.account import update_account_number

		n0 = _next_free_account_number(918821)
		n1 = _next_free_account_number(n0 + 1)
		n2 = _next_free_account_number(n1 + 1)

		name = self._create_leaf_account(str(n0), "Rename Repeat")
		user = frappe.session.user
		self._track_user("GL Entry", user)
		_cache_user_settings(
			"GL Entry",
			user,
			{
				"List": {
					"filters": [["GL Entry", "account", "=", name]],
					"columns": ["name", "account"],
				}
			},
		)
		sync_user_settings_safe()

		name1 = update_account_number(name, "Rename Repeat", str(n1))
		frappe.db.commit()
		self.accounts.append(name1)
		name2 = update_account_number(name1, "Rename Repeat", str(n2))
		frappe.db.commit()
		self.accounts.append(name2)

		stored = _read_db_user_settings("GL Entry", user)
		filters = (stored.get("List") or {}).get("filters") or []
		self.assertEqual(len(filters), 1)
		self.assertEqual(filters[0][3], name2)
		self.assertEqual(len([k for k in stored if k == "List"]), 1)

	def test_large_serialized_settings_remain_valid_json(self):
		user = frappe.session.user
		self._track_user(AE_DOCTYPE, user)
		settings = _make_ae_settings(token_count=18, token_pad=2600, grid_pad=15000)
		raw = json.dumps(settings, ensure_ascii=False)
		_cache_user_settings(AE_DOCTYPE, user, raw)
		sync_user_settings_safe()
		frappe.db.commit()
		stored_raw = frappe.db.sql(
			"select data from `__UserSettings` where user=%s and doctype=%s",
			(user, AE_DOCTYPE),
		)[0][0]
		parsed = json.loads(stored_raw)
		self.assertIsInstance(parsed, dict)
		json.dumps(parsed)
		self.assertIn("Grid", parsed)
		self.assertIn("Workspace", parsed)

	def test_sync_failure_rolls_back_with_rename(self):
		from erpnext.accounts.doctype.account.account import update_account_number

		old_number = str(_next_free_account_number(918831))
		new_number = str(_next_free_account_number(int(old_number) + 1))
		name = self._create_leaf_account(old_number, "Rename Rollback")

		with patch(
			"erpnext_extensions.user_settings_compat.sync_user_settings_safe",
			side_effect=Exception("simulated sync failure"),
		):
			with self.assertRaises(Exception):
				update_account_number(name, "Rename Rollback", new_number)

		frappe.db.rollback()
		self.assertTrue(frappe.db.exists("Account", name))
		self.assertEqual(frappe.db.get_value("Account", name, "account_number"), old_number)


if __name__ == "__main__":
	unittest.main()
