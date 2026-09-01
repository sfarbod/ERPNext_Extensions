# Copyright (c) 2026, ERPNext Extensions contributors
"""Runtime ownership guard for Iran Accounting rounding.

Canonical ownership (fail-closed):

	core.rounding
	  → domain.currency
	    → domain.ledger_rounding

``iran_accounting.rounding`` is a compatibility re-export facade only and is
**not** an ownership surface for ``ensure_runtime_ready``.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

import erpnext_extensions.iran_accounting.core.rounding as core_rounding

CORE_MODULE = "erpnext_extensions.iran_accounting.core.rounding"
DOMAIN_CURRENCY_MODULE = "erpnext_extensions.iran_accounting.domain.currency"
DOMAIN_LEDGER_ROUNDING_MODULE = "erpnext_extensions.iran_accounting.domain.ledger_rounding"
COMPAT_FACADE_MODULE = "erpnext_extensions.iran_accounting.rounding"

CORE_REQUIRED = ("round_currency", "round_currency_amount", "round_row_amount", "round_rate")

DOMAIN_CURRENCY_REQUIRED = (
	"round_currency_amount",
	"round_row_amount",
	"round_monetary_rate",
	"round_irr_rate",
	"get_currency_precision",
	"amount_is_fractional",
	"rate_is_fractional",
)

DOMAIN_LEDGER_ROUNDING_REQUIRED = (
	"round_sle_monetary_fields",
	"round_gl_entry_amounts",
	"round_stock_entry_totals",
)

# Symbols the compatibility facade is expected to re-export (diagnostics only).
COMPAT_FACADE_EXPECTED = DOMAIN_CURRENCY_REQUIRED + DOMAIN_LEDGER_ROUNDING_REQUIRED


def _module_meta(module_name: str, mod: Any | None = None) -> dict[str, Any]:
	mod = mod if mod is not None else sys.modules.get(module_name)
	spec = getattr(mod, "__spec__", None) if mod is not None else None
	find_spec = importlib.util.find_spec(module_name)
	return {
		"module": module_name,
		"loaded": mod is not None,
		"file": getattr(mod, "__file__", None) if mod is not None else None,
		"spec_origin": getattr(spec, "origin", None) if spec is not None else None,
		"spec_cached": getattr(spec, "cached", None) if spec is not None else None,
		"find_spec_origin": getattr(find_spec, "origin", None) if find_spec is not None else None,
		"loader": type(getattr(spec, "loader", None)).__name__
		if spec is not None and getattr(spec, "loader", None) is not None
		else None,
		"sys_modules_id": id(mod) if mod is not None else None,
	}


def _missing_attrs(mod: Any, required: tuple[str, ...]) -> list[str]:
	return [name for name in required if not hasattr(mod, name)]


def _facade_status() -> dict[str, Any]:
	mod = sys.modules.get(COMPAT_FACADE_MODULE)
	meta = _module_meta(COMPAT_FACADE_MODULE, mod)
	if mod is None:
		return {**meta, "missing": list(COMPAT_FACADE_EXPECTED), "complete": False}
	missing = _missing_attrs(mod, COMPAT_FACADE_EXPECTED)
	return {**meta, "missing": missing, "complete": not missing}


def _format_runtime_failure(
	module_name: str,
	missing: list[str],
	mod: Any | None,
) -> str:
	meta = _module_meta(module_name, mod)
	facade = _facade_status()
	return (
		f"canonical module {module_name} incomplete after reload; "
		f"missing: {missing}; "
		f"file={meta.get('file')!r}; "
		f"spec_origin={meta.get('spec_origin')!r}; "
		f"find_spec_origin={meta.get('find_spec_origin')!r}; "
		f"loader={meta.get('loader')!r}; "
		f"legacy_facade_complete={facade.get('complete')}; "
		f"legacy_facade_file={facade.get('file')!r}; "
		f"legacy_facade_missing={facade.get('missing')}"
	)


def _ensure_module_attrs(module_name: str, required: tuple[str, ...]) -> None:
	mod = sys.modules.get(module_name)
	if mod is None:
		mod = importlib.import_module(module_name)
	missing = _missing_attrs(mod, required)
	if not missing:
		return
	mod = importlib.reload(mod)
	missing = _missing_attrs(mod, required)
	if missing:
		raise ImportError(_format_runtime_failure(module_name, missing, mod))


def ensure_core_rounding() -> None:
	_ensure_module_attrs(CORE_MODULE, CORE_REQUIRED)


def ensure_domain_currency() -> None:
	_ensure_module_attrs(DOMAIN_CURRENCY_MODULE, DOMAIN_CURRENCY_REQUIRED)


def ensure_domain_ledger_rounding() -> None:
	_ensure_module_attrs(DOMAIN_LEDGER_ROUNDING_MODULE, DOMAIN_LEDGER_ROUNDING_REQUIRED)


def ensure_runtime_ready() -> None:
	"""Validate canonical rounding owners (idempotent, fail-closed).

	Does **not** treat ``iran_accounting.rounding`` as an ownership surface.
	"""
	ensure_core_rounding()
	ensure_domain_currency()
	ensure_domain_ledger_rounding()
	for name in CORE_REQUIRED:
		if not hasattr(core_rounding, name):
			raise ImportError(
				_format_runtime_failure(CORE_MODULE, [name], core_rounding)
			)


def report_deployment_integrity() -> dict[str, Any]:
	"""Read-only deployment / import-path integrity report (does not mutate runtime)."""
	canonical = {
		"core.rounding": _module_meta(CORE_MODULE),
		"domain.currency": _module_meta(DOMAIN_CURRENCY_MODULE),
		"domain.ledger_rounding": _module_meta(DOMAIN_LEDGER_ROUNDING_MODULE),
	}
	# Ensure meta reflects loadable origins even if not yet imported.
	for key, module_name in (
		("core.rounding", CORE_MODULE),
		("domain.currency", DOMAIN_CURRENCY_MODULE),
		("domain.ledger_rounding", DOMAIN_LEDGER_ROUNDING_MODULE),
	):
		if not canonical[key]["loaded"]:
			canonical[key] = _module_meta(module_name)

	facade = _facade_status()

	package_roots: list[str] = []
	seen: set[str] = set()
	for entry in sys.path:
		candidate = Path(entry) / "erpnext_extensions"
		init_py = candidate / "__init__.py"
		if init_py.is_file():
			resolved = str(candidate.resolve())
			if resolved not in seen:
				seen.add(resolved)
				package_roots.append(resolved)

	files = [
		canonical["core.rounding"].get("file"),
		canonical["domain.currency"].get("file"),
		canonical["domain.ledger_rounding"].get("file"),
		facade.get("file"),
	]
	distinct_files = sorted({f for f in files if f})

	# Partial reload heuristic: loaded module file differs from find_spec origin.
	partial_reload_suspects: list[str] = []
	for label, meta in {**canonical, "compat.rounding": facade}.items():
		loaded_file = meta.get("file")
		origin = meta.get("find_spec_origin") or meta.get("spec_origin")
		if loaded_file and origin and Path(loaded_file).resolve() != Path(origin).resolve():
			partial_reload_suspects.append(label)

	return {
		"canonical": canonical,
		"compat_facade": facade,
		"erpnext_extensions_package_roots": package_roots,
		"duplicate_package_install": len(package_roots) > 1,
		"distinct_rounding_files": distinct_files,
		"partial_reload_suspects": partial_reload_suspects,
		"stale_worker_import_risk": bool(partial_reload_suspects) or len(package_roots) > 1,
	}
