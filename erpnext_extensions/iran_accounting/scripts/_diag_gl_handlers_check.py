# Copyright (c) 2026 — prove Iran GL handlers active after bootstrap / phase3 path.
from __future__ import annotations

from typing import Any


def run() -> dict[str, Any]:
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as bootstrap_apply
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		ensure_iran_riv_gl_handlers_active,
		ensure_iran_riv_recalculate_wrapper_active,
		is_iran_riv_gl_handlers_active,
		is_iran_riv_recalculate_wrapper_active,
	)
	import erpnext.accounts.general_ledger as gl
	from erpnext.controllers.stock_controller import StockController
	from erpnext_extensions.iran_accounting.integration import riv_execute_guard as guard
	import erpnext.stock.doctype.repost_item_valuation.repost_item_valuation as riv_mod
	import erpnext_extensions.iran_accounting.domain.riv_sabe_integer_phase3 as phase3
	import inspect

	bootstrap_apply()
	ensure_iran_riv_recalculate_wrapper_active(bootstrap=False)
	ensure_iran_riv_gl_handlers_active(bootstrap=False)

	import ast

	create_src = inspect.getsource(phase3.create_and_execute_riv)
	phase3_src = inspect.getsource(phase3)
	import_bound = False
	for node in ast.parse(phase3_src).body:
		if isinstance(node, ast.ImportFrom):
			if any(a.name == "execute_reposting_entry" for a in node.names):
				import_bound = True
	return {
		"gl_handlers_active": is_iran_riv_gl_handlers_active(),
		"recalculate_wrapper_active": is_iran_riv_recalculate_wrapper_active(),
		"process_dcd_module": getattr(gl.process_debit_credit_difference, "__module__", None),
		"make_gl_entries_module": getattr(StockController.make_gl_entries, "__module__", None),
		"riv_mod_execute_is_guard": riv_mod.execute_reposting_entry is guard.execute_reposting_entry,
		"phase3_uses_guard": "riv_execute_guard" in create_src,
		"phase3_asserts_gl": "ensure_iran_riv_gl_handlers_active" in create_src,
		"phase3_no_import_bind": not import_bound,
	}
