# Copyright (c) 2026, ERPNext Extensions contributors

from __future__ import annotations

import importlib
import sys
import unittest

import erpnext_extensions.iran_accounting.core.rounding as core_rounding
import erpnext_extensions.iran_accounting.domain.ledger_rounding as ledger_rounding
from erpnext_extensions.iran_accounting.worker.guard import (
	DOMAIN_LEDGER_ROUNDING_REQUIRED,
	ensure_runtime_ready,
	report_deployment_integrity,
)


class TestImportStability(unittest.TestCase):
	def test_core_rounding_import_100_times(self):
		for _ in range(100):
			mod = importlib.import_module("erpnext_extensions.iran_accounting.core.rounding")
			self.assertTrue(hasattr(mod, "round_row_amount"))
			self.assertTrue(hasattr(mod, "round_currency_amount"))
			self.assertEqual(mod.round_row_amount(3, 10.5, 0), 33)  # rate-first: 11×3

	def test_fresh_reload_ledger_rounding(self):
		mod = importlib.import_module("erpnext_extensions.iran_accounting.domain.ledger_rounding")
		mod = importlib.reload(mod)
		for name in DOMAIN_LEDGER_ROUNDING_REQUIRED:
			self.assertTrue(hasattr(mod, name), name)

	def test_repeated_reload_ledger_rounding(self):
		mod = importlib.import_module("erpnext_extensions.iran_accounting.domain.ledger_rounding")
		for _ in range(20):
			mod = importlib.reload(mod)
			for name in DOMAIN_LEDGER_ROUNDING_REQUIRED:
				self.assertTrue(hasattr(mod, name), name)

	def test_reload_after_delattr_restores_canonical_symbols(self):
		ensure_runtime_ready()
		mod = sys.modules["erpnext_extensions.iran_accounting.domain.ledger_rounding"]
		for name in DOMAIN_LEDGER_ROUNDING_REQUIRED:
			if hasattr(mod, name):
				delattr(mod, name)
		ensure_runtime_ready()
		for name in DOMAIN_LEDGER_ROUNDING_REQUIRED:
			self.assertTrue(hasattr(mod, name), name)
		self.assertTrue(hasattr(core_rounding, "round_row_amount"))

	def test_compat_facade_reload_restores_reexports(self):
		facade = importlib.import_module("erpnext_extensions.iran_accounting.rounding")
		for name in DOMAIN_LEDGER_ROUNDING_REQUIRED:
			if hasattr(facade, name):
				delattr(facade, name)
		facade = importlib.reload(facade)
		for name in DOMAIN_LEDGER_ROUNDING_REQUIRED:
			self.assertTrue(hasattr(facade, name), name)
			self.assertIs(getattr(facade, name), getattr(ledger_rounding, name))

	def test_worker_guard_does_not_require_facade_ownership(self):
		"""Canonical guard passes even if facade attrs were deleted (facade is not owner)."""
		ensure_runtime_ready()
		facade = importlib.import_module("erpnext_extensions.iran_accounting.rounding")
		deleted = []
		for name in DOMAIN_LEDGER_ROUNDING_REQUIRED:
			if hasattr(facade, name):
				delattr(facade, name)
				deleted.append(name)
		ensure_runtime_ready()
		# Canonical still healthy.
		for name in DOMAIN_LEDGER_ROUNDING_REQUIRED:
			self.assertTrue(hasattr(ledger_rounding, name), name)
		# Facade may remain incomplete until explicitly reloaded (compat-only).
		importlib.reload(facade)
		for name in deleted:
			self.assertTrue(hasattr(facade, name), name)

	def test_deployment_integrity_read_only(self):
		before = id(sys.modules.get("erpnext_extensions.iran_accounting.domain.ledger_rounding"))
		report = report_deployment_integrity()
		after = id(sys.modules.get("erpnext_extensions.iran_accounting.domain.ledger_rounding"))
		self.assertEqual(before, after)
		self.assertIn("stale_worker_import_risk", report)
		self.assertFalse(report["stale_worker_import_risk"])
