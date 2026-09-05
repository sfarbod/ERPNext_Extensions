# Copyright (c) 2026, ERPNext Extensions contributors
"""Upgrade-guarded IRR protection for update_rate_on_stock_entry during RIV.

Stock Entry Detail remains the accounting source of truth. This module:
1. Fingerprints the vanilla ERPNext method (signature + normalized AST/source).
2. Fail-closes if ERPNext/Frappe is not on the explicit allow-list.
3. Provides the IRR wrapper that skips ONLY basic_rate ← outgoing_rate.

No second rounding engine — after ERPNext recalculate, call align_stock_entry_item_amounts.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import re
import textwrap
from typing import Any

import frappe
from frappe import _
from frappe.utils import flt

# ---------------------------------------------------------------------------
# Explicit support allow-list (major.minor). Unknown versions → BLOCK.
# Fingerprints are normalized AST hashes of the unpatched ERPNext methods.
# Re-validate and extend this table before enabling on a new ERPNext build.
# ---------------------------------------------------------------------------

_SUPPORTED_ERPNEXT_MINOR = frozenset({"16.29", "16.30", "16.31", "16.32", "16.33", "16.34"})
_SUPPORTED_FRAPPE_MINOR = frozenset({"16.29", "16.30", "16.31", "16.32", "16.33"})

# Fingerprints measured on ERPNext 16.30.0 / Frappe 16.29.0 (also valid for
# 16.29.x / 16.31.x / 16.32.x / 16.33.x when the method bodies are identical —
# revalidated on ERPNext 16.32.0 / Frappe 16.31.0 and ERPNext 16.33.0 /
# Frappe 16.32.0). ERPNext 16.34.1 / Frappe 16.33.0: recalculate body gained
# additional-cost redistributed-row persistence; update_rate / sabb unchanged.
_FN_FINGERPRINTS = {
	"update_rate_on_stock_entry": {
		"signature": "(self, sle, outgoing_rate)",
		"source_sha256": "4665fb8ed4681e52fca822e129105dbc4d9dcac22cc44ba592f0fe0bb3644810",
		"must_contain": ("basic_rate", "recalculate_amounts_in_stock_entry"),
	},
	"recalculate_amounts_in_stock_entry": {
		"signature": "(self, voucher_no, voucher_detail_no)",
		# ERPNext 16.34.x — also db_update incoming rows when additional_costs
		"source_sha256": "d54d175ca9c3a170df15362415fe230b63fe6a55f4ca52b0796fddb7a6e00247",
		"source_sha256_alternates": (
			# ERPNext 16.29–16.33 (only voucher_detail_no / FG-scrap / Manufacture|Repack)
			"62f15a743e48a8ed39d1a004c5c64e23e5a708bb07de82346f14ff39643de0ac",
		),
		"must_contain": ("reset_outgoing_rate=False", "calculate_rate_and_amount"),
	},
	"is_manufacture_entry_with_sabb": {
		"signature": "(self, sle)",
		"source_sha256": "7b6a23a3726b9f5254bf0dfd7a8c2d3d42bf863c7b08a1e90db108ebb68fe61f",
		"must_contain": ("Manufacture", "Repack"),
	},
}


def major_minor(version: str | None) -> str:
	"""Return 'major.minor' from a version string like '16.30.0'."""
	parts = (version or "").split(".")
	if len(parts) < 2:
		return version or ""
	return f"{parts[0]}.{parts[1]}"


def normalize_callable_signature(fn) -> str:
	"""Signature fingerprint ignoring type hints.

	Compares parameter names, kinds, and defaults only. Return annotations and
	parameter annotations are stripped so ``(doc)``, ``(doc) -> None``, and
	``(doc) -> 'None'`` fingerprint identically. Executable arity / defaults
	mismatches still fail.
	"""
	sig = inspect.signature(fn)
	params = [p.replace(annotation=inspect.Parameter.empty) for p in sig.parameters.values()]
	clean = sig.replace(parameters=params, return_annotation=inspect.Signature.empty)
	return str(clean)


def _strip_type_hints(tree: ast.AST) -> ast.AST:
	"""Drop type hints from an AST while preserving executable structure.

	Strips:
	- function / async function return annotations
	- parameter annotations
	- function type parameters (PEP 695)
	- converts ``AnnAssign`` with a value to plain ``Assign``
	- drops annotation-only ``AnnAssign`` declarations

	Does **not** remove: calls, control flow, SQL/string constants used in calls,
	assignments, or other executable statements.
	"""

	for node in ast.walk(tree):
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
			node.returns = None
			if hasattr(node, "type_comment"):
				node.type_comment = None
			if hasattr(node, "type_params"):
				node.type_params = []
			arg_nodes = [
				*node.args.posonlyargs,
				*node.args.args,
				*node.args.kwonlyargs,
			]
			if node.args.vararg is not None:
				arg_nodes.append(node.args.vararg)
			if node.args.kwarg is not None:
				arg_nodes.append(node.args.kwarg)
			for arg in arg_nodes:
				arg.annotation = None
				if hasattr(arg, "type_comment"):
					arg.type_comment = None

	class _AnnAssignToAssign(ast.NodeTransformer):
		def visit_AnnAssign(self, node: ast.AnnAssign):
			self.generic_visit(node)
			if node.value is None:
				return None
			return ast.copy_location(
				ast.Assign(targets=[node.target], value=node.value),
				node,
			)

	tree = _AnnAssignToAssign().visit(tree)
	ast.fix_missing_locations(tree)
	return tree


def normalize_function_source(fn) -> str:
	"""Dedent + drop docstring + strip type hints + AST-unparse + collapse whitespace.

	Fingerprints executable behavior, not annotations/formatting/comments.
	"""
	src = textwrap.dedent(inspect.getsource(fn))
	tree = ast.parse(src)
	for node in ast.walk(tree):
		if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
			if (
				node.body
				and isinstance(node.body[0], ast.Expr)
				and isinstance(node.body[0].value, ast.Constant)
				and isinstance(node.body[0].value.value, str)
			):
				node.body = node.body[1:]
	tree = _strip_type_hints(tree)
	out = ast.unparse(tree)
	return re.sub(r"\s+", " ", out).strip()


def source_sha256(fn) -> str:
	return hashlib.sha256(normalize_function_source(fn).encode()).hexdigest()


def _version_pair() -> tuple[str, str]:
	import erpnext

	return major_minor(getattr(erpnext, "__version__", "")), major_minor(
		getattr(frappe, "__version__", "")
	)


def collect_fingerprint_report() -> dict[str, Any]:
	"""Inspect live unpatched (or currently bound) class methods for diagnostics."""
	import erpnext.stock.stock_ledger as sl

	cls = sl.update_entries_after
	# Prefer saved originals when already patched.
	fns = {
		"update_rate_on_stock_entry": getattr(
			cls, "_iran_original_update_rate_on_stock_entry", None
		)
		or cls.update_rate_on_stock_entry,
		"recalculate_amounts_in_stock_entry": cls.recalculate_amounts_in_stock_entry,
		"is_manufacture_entry_with_sabb": cls.is_manufacture_entry_with_sabb,
	}
	erp_mm, fr_mm = _version_pair()
	methods = {}
	for name, fn in fns.items():
		norm = normalize_function_source(fn)
		methods[name] = {
			"signature": normalize_callable_signature(fn),
			"source_sha256": hashlib.sha256(norm.encode()).hexdigest(),
			"normalized_source": norm,
		}
	return {
		"erpnext_major_minor": erp_mm,
		"frappe_major_minor": fr_mm,
		"methods": methods,
	}


def assert_erpnext_riv_rate_patch_supported() -> None:
	"""Fail-closed upgrade guard. Raises if versions/fingerprints are unsupported."""
	import erpnext
	import erpnext.stock.stock_ledger as sl

	erp_ver = getattr(erpnext, "__version__", "")
	fr_ver = getattr(frappe, "__version__", "")
	erp_mm, fr_mm = major_minor(erp_ver), major_minor(fr_ver)

	if erp_mm not in _SUPPORTED_ERPNEXT_MINOR:
		raise RuntimeError(
			_(
				"IRR RIV rate guard: ERPNext {0} is not on the explicit support allow-list "
				"({1}). Wrapper not installed."
			).format(erp_ver, ", ".join(sorted(_SUPPORTED_ERPNEXT_MINOR)))
		)
	if fr_mm not in _SUPPORTED_FRAPPE_MINOR:
		raise RuntimeError(
			_(
				"IRR RIV rate guard: Frappe {0} is not on the explicit support allow-list "
				"({1}). Wrapper not installed."
			).format(fr_ver, ", ".join(sorted(_SUPPORTED_FRAPPE_MINOR)))
		)

	cls = sl.update_entries_after
	live_rate = cls.update_rate_on_stock_entry
	# Prefer true vanilla original (saved on install, or wrapper._iran_original).
	saved = getattr(cls, "_iran_original_update_rate_on_stock_entry", None)
	if saved is None and getattr(live_rate, "_iran_riv_rate_wrapper", None):
		saved = getattr(live_rate, "_iran_original", None)
	targets = {
		"update_rate_on_stock_entry": saved or live_rate,
		"recalculate_amounts_in_stock_entry": cls.recalculate_amounts_in_stock_entry,
		"is_manufacture_entry_with_sabb": cls.is_manufacture_entry_with_sabb,
	}

	errors: list[str] = []
	for name, expected in _FN_FINGERPRINTS.items():
		fn = targets[name]
		sig = normalize_callable_signature(fn)
		if sig != expected["signature"]:
			errors.append(f"{name}: signature {sig!r} != {expected['signature']!r}")
			continue
		norm = normalize_function_source(fn)
		digest = hashlib.sha256(norm.encode()).hexdigest()
		accepted = {expected["source_sha256"], *(expected.get("source_sha256_alternates") or ())}
		if digest not in accepted:
			errors.append(
				f"{name}: source fingerprint {digest} != allow-list {sorted(accepted)}"
			)
		for token in expected.get("must_contain") or ():
			if token not in norm:
				errors.append(f"{name}: normalized source missing required token {token!r}")

	if errors:
		raise RuntimeError(
			"IRR RIV rate guard: ERPNext stock_ledger fingerprint mismatch — "
			"wrapper NOT installed (fail-closed).\n" + "\n".join(errors)
		)


def resolve_company_for_sle(engine, sle) -> str | None:
	company = getattr(engine, "company", None)
	if company:
		return company
	if hasattr(sle, "get"):
		company = sle.get("company")
	else:
		company = getattr(sle, "company", None)
	if company:
		return company
	voucher_no = getattr(sle, "voucher_no", None) or (sle.get("voucher_no") if hasattr(sle, "get") else None)
	if voucher_no:
		return frappe.db.get_value("Stock Entry", voucher_no, "company")
	return None


def persist_irr_contract_after_recalculate(voucher_no: str) -> None:
	"""Re-apply the single IRR contract engine and persist SE rows/header."""
	from erpnext_extensions.iran_accounting.domain.currency import is_irr_company
	from erpnext_extensions.iran_accounting.domain.qty_rate_amount import (
		align_stock_entry_item_amounts,
	)
	from erpnext_extensions.iran_accounting.manufacture_rounding import (
		align_manufacture_finished_good_residual,
	)

	doc = frappe.get_doc("Stock Entry", voucher_no)
	if not is_irr_company(doc.company):
		return

	align_stock_entry_item_amounts(doc)
	if doc.purpose in ("Manufacture", "Repack"):
		align_manufacture_finished_good_residual(doc)
	if hasattr(doc, "set_total_incoming_outgoing_value"):
		doc.set_total_incoming_outgoing_value()

	for row in doc.get("items") or []:
		frappe.db.set_value(
			"Stock Entry Detail",
			row.name,
			{
				"basic_rate": row.basic_rate,
				"basic_amount": row.get("basic_amount"),
				"amount": row.amount,
				"valuation_rate": row.valuation_rate,
				"additional_cost": row.get("additional_cost"),
				"landed_cost_voucher_amount": row.get("landed_cost_voucher_amount"),
			},
			update_modified=False,
		)
	doc.db_set(
		{
			"total_incoming_value": doc.total_incoming_value,
			"total_outgoing_value": doc.total_outgoing_value,
			"value_difference": doc.value_difference,
		},
		update_modified=False,
	)


def make_update_rate_on_stock_entry_wrapper(original):
	"""Build the IRR-aware wrapper around vanilla update_rate_on_stock_entry."""
	from erpnext_extensions.iran_accounting.domain.currency import is_irr_company

	def update_rate_on_stock_entry(self, sle, outgoing_rate):
		company = resolve_company_for_sle(self, sle)
		if not company:
			frappe.throw(
				_("IRR RIV rate guard: cannot resolve company — refusing MA basic_rate overwrite"),
				title=_("IRR Rate Guard"),
			)

		if not is_irr_company(company):
			return original(self, sle, outgoing_rate)

		detail_no = getattr(sle, "voucher_detail_no", None)
		if not detail_no:
			frappe.throw(
				_("IRR RIV rate guard: missing voucher_detail_no — refusing MA basic_rate overwrite"),
				title=_("IRR Rate Guard"),
			)

		row = frappe.db.get_value(
			"Stock Entry Detail",
			detail_no,
			["name", "basic_rate"],
			as_dict=True,
		)
		if not row:
			frappe.throw(
				_("IRR RIV rate guard: Stock Entry Detail {0} not found").format(detail_no),
				title=_("IRR Rate Guard"),
			)
		if row.basic_rate in (None, ""):
			frappe.throw(
				_(
					"IRR RIV rate guard: preserved basic_rate missing on {0} — "
					"refusing MA outgoing_rate {1}"
				).format(detail_no, outgoing_rate),
				title=_("IRR Rate Guard"),
			)

		# SKIP vanilla: frappe.db.set_value(..., "basic_rate", outgoing_rate)
		# Keep submitted / contract basic_rate (already integer for IRR).

		if not sle.dependant_sle_voucher_detail_no or self.is_manufacture_entry_with_sabb(sle):
			self.recalculate_amounts_in_stock_entry(sle.voucher_no, sle.voucher_detail_no)
			persist_irr_contract_after_recalculate(sle.voucher_no)

	update_rate_on_stock_entry._iran_riv_rate_wrapper = True
	update_rate_on_stock_entry._iran_original = original
	return update_rate_on_stock_entry
