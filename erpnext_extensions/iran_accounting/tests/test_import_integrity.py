# Copyright (c) 2026, ERPNext Extensions contributors

from __future__ import annotations

import ast
import importlib
import unittest
from collections import defaultdict
from pathlib import Path

import erpnext_extensions.iran_accounting.core.rounding as core_rounding
import erpnext_extensions.iran_accounting.domain.currency as domain_currency
import erpnext_extensions.iran_accounting.domain.ledger_rounding as ledger_rounding
import erpnext_extensions.iran_accounting.rounding as compat_rounding
from erpnext_extensions.iran_accounting.integration.bootstrap import apply
from erpnext_extensions.iran_accounting.worker.guard import (
	COMPAT_FACADE_EXPECTED,
	CORE_REQUIRED,
	DOMAIN_CURRENCY_REQUIRED,
	DOMAIN_LEDGER_ROUNDING_REQUIRED,
	ensure_runtime_ready,
	report_deployment_integrity,
)


class TestImportIntegrity(unittest.TestCase):
	def test_core_rounding_exports_required_symbols(self):
		for name in CORE_REQUIRED:
			self.assertTrue(hasattr(core_rounding, name), name)

	def test_domain_currency_exports_required_symbols(self):
		for name in DOMAIN_CURRENCY_REQUIRED:
			self.assertTrue(hasattr(domain_currency, name), name)

	def test_domain_ledger_rounding_exports_required_symbols(self):
		for name in DOMAIN_LEDGER_ROUNDING_REQUIRED:
			self.assertTrue(hasattr(ledger_rounding, name), name)

	def test_compat_facade_reexports_expected_symbols(self):
		for name in COMPAT_FACADE_EXPECTED:
			self.assertTrue(hasattr(compat_rounding, name), name)

	def test_ensure_runtime_ready_idempotent(self):
		ensure_runtime_ready()
		apply()
		for name in CORE_REQUIRED:
			self.assertTrue(hasattr(core_rounding, name), name)
		for name in DOMAIN_LEDGER_ROUNDING_REQUIRED:
			self.assertTrue(hasattr(ledger_rounding, name), name)

	def test_before_request_simulation(self):
		"""hooks before_request → bootstrap.apply → ensure_runtime_ready."""
		apply()
		ensure_runtime_ready()
		for name in DOMAIN_LEDGER_ROUNDING_REQUIRED:
			self.assertTrue(hasattr(ledger_rounding, name), name)

	def test_before_job_simulation(self):
		"""hooks before_job uses the same bootstrap.apply entrypoint."""
		apply()
		for name in DOMAIN_CURRENCY_REQUIRED:
			self.assertTrue(hasattr(domain_currency, name), name)

	def test_legacy_import_path_still_works(self):
		mod = importlib.import_module("erpnext_extensions.iran_accounting.rounding")
		from erpnext_extensions.iran_accounting.rounding import (  # noqa: F401
			round_gl_entry_amounts,
			round_sle_monetary_fields,
			round_stock_entry_totals,
		)

		self.assertIs(round_sle_monetary_fields, mod.round_sle_monetary_fields)
		self.assertIs(round_sle_monetary_fields, ledger_rounding.round_sle_monetary_fields)

	def test_canonical_import_path(self):
		from erpnext_extensions.iran_accounting.domain.ledger_rounding import (
			round_gl_entry_amounts,
			round_sle_monetary_fields,
			round_stock_entry_totals,
		)

		self.assertTrue(callable(round_sle_monetary_fields))
		self.assertTrue(callable(round_gl_entry_amounts))
		self.assertTrue(callable(round_stock_entry_totals))

	def test_deployment_integrity_report_shape(self):
		report = report_deployment_integrity()
		self.assertIn("canonical", report)
		self.assertIn("compat_facade", report)
		self.assertIn("erpnext_extensions_package_roots", report)
		self.assertFalse(report["duplicate_package_install"])
		self.assertTrue(report["compat_facade"]["complete"])
		self.assertEqual(report["partial_reload_suspects"], [])

	def test_no_import_cycle_monkey_patches_stock_reconciliation(self):
		base = Path(__file__).resolve().parents[1]
		mods = {
			"core.rounding": base / "core" / "rounding.py",
			"domain.qty_rate_amount": base / "domain" / "qty_rate_amount.py",
			"domain.stock_reconciliation": base / "domain" / "stock_reconciliation.py",
			"domain.stock_reconciliation_sync": base / "domain" / "stock_reconciliation_sync.py",
			"domain.stock_ledger": base / "domain" / "stock_ledger.py",
			"integration.monkey_patches": base / "integration" / "monkey_patches.py",
		}
		edges: dict[str, set[str]] = defaultdict(set)
		for mod, path in mods.items():
			tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
			for node in ast.walk(tree):
				if isinstance(node, ast.ImportFrom) and node.module:
					m = node.module
					if not m.startswith("erpnext_extensions.iran_accounting."):
						continue
					dep = m.replace("erpnext_extensions.iran_accounting.", "")
					parts = dep.split(".")
					label = ".".join(parts[:2]) if len(parts) > 1 else parts[0]
					if label in mods and label != mod:
						edges[mod].add(label)
					elif parts[0] in ("core", "domain", "integration", "worker"):
						short = f"{parts[0]}.{parts[1]}" if len(parts) > 1 else parts[0]
						if short in mods and short != mod:
							edges[mod].add(short)
		self.assertNotIn(
			"domain.stock_reconciliation",
			edges.get("integration.monkey_patches", set()),
		)
		self.assertIn("domain.stock_reconciliation_sync", edges.get("integration.monkey_patches", set()))

	def test_qty_rate_amount_imports_domain_currency(self):
		src = (Path(__file__).resolve().parents[1] / "domain" / "qty_rate_amount.py").read_text(
			encoding="utf-8"
		)
		self.assertIn("import erpnext_extensions.iran_accounting.domain.currency as rounding", src)
		importlib.import_module("erpnext_extensions.iran_accounting.domain.qty_rate_amount")

	def test_repost_determinism_uses_canonical_ledger_rounding(self):
		src = (Path(__file__).resolve().parents[1] / "domain" / "repost_determinism.py").read_text(
			encoding="utf-8"
		)
		self.assertIn(
			"from erpnext_extensions.iran_accounting.domain.ledger_rounding import round_stock_entry_totals",
			src,
		)
		self.assertNotIn(
			"from erpnext_extensions.iran_accounting.rounding import round_stock_entry_totals",
			src,
		)

	def test_compat_facade_has_no_business_logic_defs(self):
		"""Facade must re-export only — no local def of ledger/currency rounders."""
		src = (Path(__file__).resolve().parents[1] / "rounding.py").read_text(encoding="utf-8")
		tree = ast.parse(src)
		defined = {
			node.name
			for node in tree.body
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
		}
		forbidden = set(DOMAIN_LEDGER_ROUNDING_REQUIRED) | {
			"round_currency",
			"round_currency_amount",
			"round_row_amount",
			"round_monetary_rate",
		}
		self.assertFalse(defined & forbidden, defined & forbidden)

	def test_guard_no_longer_owns_compat_facade(self):
		src = (Path(__file__).resolve().parents[1] / "worker" / "guard.py").read_text(encoding="utf-8")
		self.assertIn("ensure_domain_ledger_rounding", src)
		self.assertNotIn("ensure_legacy_rounding_shim", src)
		# ensure_runtime_ready must not call attrs check on the facade module name as owner.
		self.assertIn("DOMAIN_LEDGER_ROUNDING_MODULE", src)
