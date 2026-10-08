# Copyright (c) 2026, ERPNext Extensions contributors
"""Upgrade-guarded IRR protection for update_rate_on_stock_entry during RIV.

Stock Entry Detail remains the accounting source of truth. This module:
1. Fingerprints the vanilla ERPNext method (signature + normalized AST/source).
2. Fail-closes if ERPNext/Frappe is not on the explicit allow-list.
3. Provides the IRR wrapper that, by default, skips basic_rate ← outgoing_rate
   (rate-first preserve on submit and on idempotent RIV).
4. During RIV, when Core MA legitimately diverges from the document on
   transfer / manufacture / repack dependants, accepts an IRR-rounded
   outgoing_rate, forces recalculate, and applies the Iran manufacture
   contract so sync_irr mirrors UPDATED document economics (v5.5.24 FIX B).

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
from frappe.utils import cint, flt

# Manufacture consume only. Leftover-MA reconstruction uses Material Receipt /
# Material Transfer with document-zero by design; writing warehouse MA onto
# those rows exploded leftover value (trillion-scale 16100066).
VALUED_SOURCE_ZERO_OUTGOING_PURPOSES = frozenset(
	{
		"Manufacture",
		"Material Consumption for Manufacture",
	}
)

# Purposes where Core RIV dependant/MA propagation may legitimately move
# document economics. Rate-first still owns submit-time behavior; during RIV
# these may accept a rounded Core outgoing_rate and recalculate amounts.
RIV_PROPAGATED_RATE_PURPOSES = frozenset(
	{
		"Material Transfer",
		"Material Transfer for Manufacture",
		"Send to Subcontractor",
		"Manufacture",
		"Material Consumption for Manufacture",
		"Repack",
	}
)

# IRR quantum: document and Core rates that round to the same integer are equal.
_IRR_RATE_EPS = 0.5


def is_valued_source_zero_outgoing(*, actual_qty, basic_rate, allow_zero_valuation_rate, outgoing_rate, purpose) -> bool:
	"""True when a Manufacture consume has a corrupt document-zero rate but vanilla MA is valued.

	Authority is the RIV-computed outgoing_rate (warehouse moving average),
	not a nearby/future/ABS rate. Legitimate free consumes keep
	allow_zero_valuation_rate=1. Leftover-MA receipt purposes never qualify.
	"""
	if str(purpose or "") not in VALUED_SOURCE_ZERO_OUTGOING_PURPOSES:
		return False
	if flt(actual_qty) >= 0:
		return False
	if cint(allow_zero_valuation_rate):
		return False
	if abs(flt(basic_rate)) > 1e-6:
		return False
	return abs(flt(outgoing_rate)) > 1e-6


def is_through_repost_item_valuation() -> bool:
	"""True while ERPNext ``repost()`` sets ``through_repost_item_valuation``."""
	try:
		return bool(frappe.flags.get("through_repost_item_valuation"))
	except Exception:
		return False


def should_accept_riv_propagated_outgoing_rate(
	*,
	purpose,
	actual_qty,
	basic_rate,
	outgoing_rate,
	through_riv: bool | None = None,
) -> bool:
	"""True when Core RIV MA must update IRR Stock Entry document economics.

	Normal submit / non-RIV paths stay rate-first (document wins).
	Idempotent RIV where Core rate already matches the document stays preserve.
	VALUED_SOURCE_ZERO_OUTGOING is handled separately and must not use this path
	to invent rates onto leftover-MA receipt purposes.
	"""
	if through_riv is None:
		through_riv = is_through_repost_item_valuation()
	if not through_riv:
		return False
	if flt(actual_qty) >= 0:
		return False
	if str(purpose or "") not in RIV_PROPAGATED_RATE_PURPOSES:
		return False
	doc_rate = flt(basic_rate)
	core_rate = flt(outgoing_rate)
	# Compare IRR integers: ignore sub-rial MA noise that must not rewrite docs.
	if abs(round(doc_rate) - round(core_rate)) < 1:
		return False
	if abs(doc_rate - core_rate) <= _IRR_RATE_EPS:
		return False
	return True


def irr_round_outgoing_rate(outgoing_rate) -> float:
	"""Deterministic IRR integer rate for RIV-propagated document writes."""
	return float(round(flt(outgoing_rate)))

# ---------------------------------------------------------------------------
# Explicit support allow-list (major.minor). Unknown versions → BLOCK.
# Fingerprints are normalized AST hashes of the unpatched ERPNext methods.
# Re-validate and extend this table before enabling on a new ERPNext build.
# ---------------------------------------------------------------------------

_SUPPORTED_ERPNEXT_MINOR = frozenset(
	{"16.29", "16.30", "16.31", "16.32", "16.33", "16.34", "16.35", "16.36", "16.37"}
)
_SUPPORTED_FRAPPE_MINOR = frozenset(
	{"16.29", "16.30", "16.31", "16.32", "16.33", "16.34", "16.35", "16.36"}
)

# Fingerprints measured on ERPNext 16.30.0 / Frappe 16.29.0 (also valid for
# 16.29.x / 16.31.x / 16.32.x / 16.33.x when the method bodies are identical —
# revalidated on ERPNext 16.32.0 / Frappe 16.31.0 and ERPNext 16.33.0 /
# Frappe 16.32.0). ERPNext 16.34.1 / Frappe 16.33.0: recalculate body gained
# additional-cost redistributed-row persistence; update_rate / sabb unchanged.
# ERPNext 16.35.0 / Frappe 16.34.0: guarded RIV bodies identical to 16.34.x.
# ERPNext 16.36.x–16.37.0 / Frappe 16.35.x–16.36.1: guarded RIV bodies remain
# identical (update_rate / recalculate / sabb fingerprints unchanged).
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
	live_recalc = cls.recalculate_amounts_in_stock_entry
	saved_recalc = getattr(cls, "_iran_original_recalculate_amounts_in_stock_entry", None)
	if saved_recalc is None and getattr(live_recalc, "_iran_riv_recalculate_wrapper", None):
		saved_recalc = getattr(live_recalc, "_iran_original", None)
	fns = {
		"update_rate_on_stock_entry": getattr(
			cls, "_iran_original_update_rate_on_stock_entry", None
		)
		or cls.update_rate_on_stock_entry,
		"recalculate_amounts_in_stock_entry": saved_recalc or live_recalc,
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
	live_recalc = cls.recalculate_amounts_in_stock_entry
	saved_recalc = getattr(cls, "_iran_original_recalculate_amounts_in_stock_entry", None)
	if saved_recalc is None and getattr(live_recalc, "_iran_riv_recalculate_wrapper", None):
		saved_recalc = getattr(live_recalc, "_iran_original", None)
	targets = {
		"update_rate_on_stock_entry": saved or live_rate,
		"recalculate_amounts_in_stock_entry": saved_recalc or live_recalc,
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
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		apply_irr_stock_entry_contract_after_calculate,
	)

	doc = frappe.get_doc("Stock Entry", voucher_no)
	if not is_irr_company(doc.company):
		return

	apply_irr_stock_entry_contract_after_calculate(doc)

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
			["name", "basic_rate", "allow_zero_valuation_rate", "parent"],
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

		purpose = frappe.db.get_value("Stock Entry", row.parent or sle.voucher_no, "purpose")
		# VALUED_SOURCE_ZERO_OUTGOING: submitted Manufacture basic_rate is 0
		# but vanilla already priced the consume from a valued warehouse layer.
		# Document zero is corrupt, not a free receipt. Authority is this
		# SLE's vanilla outgoing_rate, not a nearby rate.
		if is_valued_source_zero_outgoing(
			actual_qty=getattr(sle, "actual_qty", 0),
			basic_rate=row.basic_rate,
			allow_zero_valuation_rate=row.get("allow_zero_valuation_rate"),
			outgoing_rate=outgoing_rate,
			purpose=purpose,
		):
			original(self, sle, outgoing_rate)
			if sle.dependant_sle_voucher_detail_no and not self.is_manufacture_entry_with_sabb(sle):
				self.recalculate_amounts_in_stock_entry(sle.voucher_no, sle.voucher_detail_no)
			persist_irr_contract_after_recalculate(sle.voucher_no)
			if hasattr(sle, "outgoing_rate"):
				sle.outgoing_rate = flt(outgoing_rate)
			return

		# RIV dependant propagation: Core MA moved because upstream valuation
		# changed. Convert that rate into IRR document economics, recalculate
		# Manufacture/Repack/transfer amounts, then let process_sle sync use the
		# UPDATED Stock Entry row (never stale amount overwrite).
		if should_accept_riv_propagated_outgoing_rate(
			purpose=purpose,
			actual_qty=getattr(sle, "actual_qty", 0),
			basic_rate=row.basic_rate,
			outgoing_rate=outgoing_rate,
		):
			irr_rate = irr_round_outgoing_rate(outgoing_rate)
			original(self, sle, irr_rate)
			# Vanilla skips recalculate when dependant_sle_voucher_detail_no is set
			# (MTfM / Manufacture consume). Force Iran-wrapped recalculate so FG /
			# transfer amounts follow the new IRR rate before SLE sync.
			if sle.dependant_sle_voucher_detail_no and not self.is_manufacture_entry_with_sabb(sle):
				self.recalculate_amounts_in_stock_entry(sle.voucher_no, sle.voucher_detail_no)
			persist_irr_contract_after_recalculate(sle.voucher_no)
			if hasattr(sle, "outgoing_rate"):
				sle.outgoing_rate = irr_rate
			return

		# SKIP vanilla: frappe.db.set_value(..., "basic_rate", outgoing_rate)
		# Keep submitted / contract basic_rate (already integer for IRR).
		# Non-RIV submit paths and idempotent RIV (rates already match) stay here.

		if not sle.dependant_sle_voucher_detail_no or self.is_manufacture_entry_with_sabb(sle):
			self.recalculate_amounts_in_stock_entry(sle.voucher_no, sle.voucher_detail_no)
			persist_irr_contract_after_recalculate(sle.voucher_no)

	update_rate_on_stock_entry._iran_riv_rate_wrapper = True
	update_rate_on_stock_entry._iran_original = original
	return update_rate_on_stock_entry
