# Copyright (c) 2026, ERPNext Extensions contributors
"""Upgrade-guarded IRR binding for update_regional_item_valuation_rate (end of UVR).

Fail-closed twin of riv_rate_guard:
1. Explicit ERPNext/Frappe major.minor allow-list (no wildcards).
2. Fingerprint BuyingController.update_valuation_rate + vanilla
   update_regional_item_valuation_rate (annotation-insensitive signature +
   normalized AST/source of executable body).
3. Assert UVR still invokes update_regional_item_valuation_rate (token + AST).
4. Monkey patch installs only after every check passes.

Patch-state hardening (5.1.3): never treat the Iran override as the upstream
ERPNext original. Fingerprint only callables proven to come from ``erpnext.*``.

Uses the same normalize_function_source / normalize_callable_signature strategy
as riv_rate_guard. Type hints (``(doc)`` vs ``(doc) -> None``) do not diverge
fingerprints; executable control-flow / call / assignment changes still fail closed.
No accounting / rounding / Class A·B changes — upgrade safety only.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import textwrap
from enum import Enum
from typing import Any

import frappe
from frappe import _

from erpnext_extensions.iran_accounting.domain.riv_rate_guard import (
	major_minor,
	normalize_callable_signature,
	normalize_function_source,
	source_sha256,
)

# ---------------------------------------------------------------------------
# Explicit support allow-list (major.minor). Unknown versions → BLOCK.
# Fingerprints measured on ERPNext 16.31.1 / Frappe 16.30.0; revalidated
# identical on ERPNext 16.32.0 / Frappe 16.31.0, ERPNext 16.33.0 /
# Frappe 16.32.0, and ERPNext 16.34.1 / Frappe 16.33.0 (guarded UVR /
# regional bodies unchanged).
# UVR body changed in 16.31 (round_floats_in gains do_not_round_fields for
# conversion_factor); regional stub + hook call site remain compatible.
# ---------------------------------------------------------------------------

_SUPPORTED_ERPNEXT_MINOR = frozenset({"16.29", "16.30", "16.31", "16.32", "16.33", "16.34"})
_SUPPORTED_FRAPPE_MINOR = frozenset({"16.29", "16.30", "16.31", "16.32", "16.33"})

_FN_FINGERPRINTS = {
	"update_valuation_rate": {
		"signature": "(self, reset_outgoing_rate=True)",
		# 16.31.x–16.34.x normalized source (identical on 16.34.1). Older minors
		# keep passing when their digest matches this table or the legacy
		# alternate below.
		"source_sha256": "a09fe875f076df168c16faaf18a281da824c052160672dcf44c163c6f1166f63",
		"source_sha256_alternates": (
			# ERPNext 16.29 / 16.30 (round_floats_in without do_not_round_fields)
			"5e898d6e97ff7b39f56c0f710b83b9e69a7c0ae08a97dae10eaf32f0c12c7bac",
		),
		"must_contain": ("update_regional_item_valuation_rate",),
	},
	"update_regional_item_valuation_rate": {
		"signature": "(doc)",
		"source_sha256": "0148e05ecb21260fd810be4fd884e83947cc1df9899ced741c6b979acac065e4",
		"must_contain": ("allow_regional",),
	},
}

# Module markers on erpnext.controllers.buying_controller
_ATTR_LIVE = "update_regional_item_valuation_rate"
_ATTR_SAVED = "_iran_original_regional_valuation_rate"
_ATTR_FLAG = "_iran_patched_regional_valuation_rate"

_IRAN_OVERRIDE_MODULE = "erpnext_extensions.iran_accounting.buying_selling"


class UVRPatchState(str, Enum):
	"""Deterministic monkey-patch installation states."""

	CLEAN = "clean"  # live vanilla; no saved; flag false
	HEALTHY = "healthy"  # live Iran; saved vanilla; flag true
	FLAG_LOST = "flag_lost"  # live Iran; saved vanilla; flag false
	POISONED_ORIGINAL = "poisoned_original"  # saved is Iran (or non-vanilla)
	UNKNOWN_THIRD_PARTY = "unknown_third_party"  # live neither vanilla nor Iran
	LIVE_IRAN_NO_SAVED = "live_iran_no_saved"  # live Iran; saved absent


def _version_pair() -> tuple[str, str]:
	import erpnext

	return major_minor(getattr(erpnext, "__version__", "")), major_minor(
		getattr(frappe, "__version__", "")
	)


def _callable_module(fn) -> str:
	return getattr(fn, "__module__", "") or ""


def is_erpnext_module_callable(fn) -> bool:
	"""True for erpnext.* symbols that are not erpnext_extensions.*."""
	mod = _callable_module(fn)
	return mod.startswith("erpnext.") and not mod.startswith("erpnext_extensions.")


def is_iran_uvr_override(fn) -> bool:
	"""True when *fn* is the Iran Accounting UVR regional override."""
	if fn is None:
		return False
	mod = _callable_module(fn)
	if mod == _IRAN_OVERRIDE_MODULE:
		return True
	# Any erpnext_extensions binding of this symbol name is never upstream.
	if mod.startswith("erpnext_extensions.") and getattr(fn, "__name__", "") == _ATTR_LIVE:
		return True
	return False


def _regional_expected() -> dict[str, Any]:
	return _FN_FINGERPRINTS["update_regional_item_valuation_rate"]


def _fingerprint_errors(name: str, fn, expected: dict[str, Any]) -> list[str]:
	errors: list[str] = []
	sig = normalize_callable_signature(fn)
	if sig != expected["signature"]:
		errors.append(f"{name}: signature {sig!r} != {expected['signature']!r}")
		return errors
	norm = normalize_function_source(fn)
	digest = hashlib.sha256(norm.encode()).hexdigest()
	accepted = {expected["source_sha256"], *(expected.get("source_sha256_alternates") or ())}
	if digest not in accepted:
		errors.append(f"{name}: source fingerprint {digest} != allow-list {sorted(accepted)}")
	for token in expected.get("must_contain") or ():
		if token not in norm:
			errors.append(f"{name}: normalized source missing required token {token!r}")
	return errors


def describe_uvr_regional_callable(fn) -> str:
	"""Short diagnostic identity for logs / errors."""
	if fn is None:
		return "<missing>"
	return f"{_callable_module(fn)}.{getattr(fn, '__name__', type(fn).__name__)}"


def validate_vanilla_uvr_regional(fn, *, role: str = "upstream original") -> None:
	"""Fail-closed: *fn* must be the vanilla ERPNext regional stub.

	Raises RuntimeError with an explicit Iran-override message when the Iran
	implementation is supplied where vanilla ERPNext was expected — never
	mislabel that as an unsupported ERPNext fingerprint.
	"""
	if fn is None:
		raise RuntimeError(
			_("IRR Upgrade Guard: UVR {0} is missing — UVR integerization patch not installed.").format(
				role
			)
		)

	if is_iran_uvr_override(fn):
		raise RuntimeError(
			_(
				"IRR Upgrade Guard: UVR guard received Iran override where vanilla ERPNext "
				"original was expected ({0}={1}). Refusing to fingerprint "
				"erpnext_extensions as upstream. UVR integerization patch not installed."
			).format(role, describe_uvr_regional_callable(fn))
		)

	if not is_erpnext_module_callable(fn):
		raise RuntimeError(
			_(
				"IRR Upgrade Guard: UVR {0} is not an erpnext.* callable ({1}). "
				"Unknown third-party patch — UVR integerization patch not installed."
			).format(role, describe_uvr_regional_callable(fn))
		)

	# Prefer the decorated ERPNext stub source (includes @erpnext.allow_regional).
	# __wrapped__ alone is the undecorated pass body and must not be fingerprinted
	# as the allow-listed regional contract.
	errors = _fingerprint_errors(_ATTR_LIVE, fn, _regional_expected())
	if errors:
		raise RuntimeError(
			_(
				"IRR Upgrade Guard: vanilla ERPNext update_regional_item_valuation_rate "
				"contract failed for {0} ({1}).\n{2}"
			).format(role, describe_uvr_regional_callable(fn), "\n".join(errors))
		)


def classify_uvr_patch_state(buying_controller) -> UVRPatchState:
	"""Classify live / saved / flag consistency for the UVR regional monkey patch."""
	live = getattr(buying_controller, _ATTR_LIVE, None)
	saved = getattr(buying_controller, _ATTR_SAVED, None)
	flag = bool(getattr(buying_controller, _ATTR_FLAG, False))

	live_iran = is_iran_uvr_override(live)
	live_vanilla = False
	if live is not None and is_erpnext_module_callable(live) and not live_iran:
		try:
			validate_vanilla_uvr_regional(live, role="live attribute")
			live_vanilla = True
		except RuntimeError:
			live_vanilla = False

	saved_vanilla = False
	saved_iran = is_iran_uvr_override(saved)
	if saved is not None and not saved_iran and is_erpnext_module_callable(saved):
		try:
			validate_vanilla_uvr_regional(saved, role="saved original")
			saved_vanilla = True
		except RuntimeError:
			saved_vanilla = False

	if saved is not None and not saved_vanilla:
		# Poisoned or non-vanilla saved pointer (Iran or foreign/broken).
		return UVRPatchState.POISONED_ORIGINAL

	if live_vanilla and saved is None:
		# Flag may be stale; live vanilla with no saved original is installable CLEAN.
		return UVRPatchState.CLEAN

	if live_iran and saved_vanilla and flag:
		return UVRPatchState.HEALTHY

	if live_iran and saved_vanilla and not flag:
		return UVRPatchState.FLAG_LOST

	if live_iran and saved is None:
		return UVRPatchState.LIVE_IRAN_NO_SAVED

	if live is not None and not live_vanilla and not live_iran:
		return UVRPatchState.UNKNOWN_THIRD_PARTY

	# live vanilla + valid saved (any flag)
	if live_vanilla and saved_vanilla:
		return UVRPatchState.HEALTHY if flag else UVRPatchState.FLAG_LOST

	return UVRPatchState.UNKNOWN_THIRD_PARTY

def resolve_vanilla_uvr_regional_original(buying_controller):
	"""Return a proven vanilla ERPNext regional stub — never the Iran override.

	Resolution order:
	1. Existing saved original — only if it validates as ERPNext vanilla.
	2. Current live attribute — only if it validates as ERPNext vanilla.
	3. Otherwise fail closed (no guessing / no Iran / no third-party).
	"""
	saved = getattr(buying_controller, _ATTR_SAVED, None)
	live = getattr(buying_controller, _ATTR_LIVE, None)

	if saved is not None:
		if is_iran_uvr_override(saved):
			# Do not fall through to fingerprinting Iran as ERPNext.
			# Recovery only if live is independently proven vanilla.
			if live is not None and not is_iran_uvr_override(live):
				validate_vanilla_uvr_regional(live, role="live attribute (poisoned saved ignored)")
				return live
			raise RuntimeError(
				_(
					"IRR Upgrade Guard: UVR patch state corruption — saved original is the Iran "
					"override ({0}) and no separately verified vanilla ERPNext callable is "
					"available. UVR integerization patch not installed."
				).format(describe_uvr_regional_callable(saved))
			)
		validate_vanilla_uvr_regional(saved, role="saved original")
		return saved

	if live is None:
		raise RuntimeError(
			_(
				"IRR Upgrade Guard: update_regional_item_valuation_rate missing — "
				"UVR integerization patch not installed."
			)
		)

	if is_iran_uvr_override(live):
		raise RuntimeError(
			_(
				"IRR Upgrade Guard: UVR guard received Iran override where vanilla ERPNext "
				"original was expected (live={0}, saved original absent). Refusing to "
				"treat erpnext_extensions as upstream. UVR integerization patch not installed."
			).format(describe_uvr_regional_callable(live))
		)

	validate_vanilla_uvr_regional(live, role="live attribute")
	return live


# Back-compat alias used by older tests / call sites.
def _resolve_vanilla_regional(buying_controller):
	return resolve_vanilla_uvr_regional_original(buying_controller)


def _uvr_calls_regional_hook(fn) -> bool:
	"""True if normalized AST contains a Call to update_regional_item_valuation_rate."""
	src = textwrap.dedent(inspect.getsource(fn))
	tree = ast.parse(src)
	for node in ast.walk(tree):
		if not isinstance(node, ast.Call):
			continue
		func = node.func
		if isinstance(func, ast.Name) and func.id == "update_regional_item_valuation_rate":
			return True
		if isinstance(func, ast.Attribute) and func.attr == "update_regional_item_valuation_rate":
			return True
	return False


def collect_fingerprint_report() -> dict[str, Any]:
	"""Inspect live vanilla UVR / regional symbols for diagnostics."""
	import erpnext.controllers.buying_controller as buying_controller
	from erpnext.controllers.buying_controller import BuyingController

	uvr = BuyingController.update_valuation_rate
	regional = resolve_vanilla_uvr_regional_original(buying_controller)
	erp_mm, fr_mm = _version_pair()
	methods = {}
	for name, fn in (
		("update_valuation_rate", uvr),
		("update_regional_item_valuation_rate", regional),
	):
		norm = normalize_function_source(fn)
		methods[name] = {
			"signature": normalize_callable_signature(fn),
			"source_sha256": hashlib.sha256(norm.encode()).hexdigest(),
			"normalized_source": norm,
			"module": _callable_module(fn),
		}
	methods["update_valuation_rate"]["calls_regional_hook"] = _uvr_calls_regional_hook(uvr)
	return {
		"erpnext_major_minor": erp_mm,
		"frappe_major_minor": fr_mm,
		"patch_state": classify_uvr_patch_state(buying_controller).value,
		"methods": methods,
	}


def assert_erpnext_uvr_regional_patch_supported() -> None:
	"""Fail-closed upgrade guard. Raises if versions/fingerprints are unsupported."""
	import erpnext
	import erpnext.controllers.buying_controller as buying_controller
	from erpnext.controllers.buying_controller import BuyingController

	erp_ver = getattr(erpnext, "__version__", "")
	fr_ver = getattr(frappe, "__version__", "")
	erp_mm, fr_mm = major_minor(erp_ver), major_minor(fr_ver)

	if erp_mm not in _SUPPORTED_ERPNEXT_MINOR:
		raise RuntimeError(
			_(
				"IRR Upgrade Guard: update_valuation_rate fingerprint unsupported on ERPNext {0}. "
				"UVR integerization patch not installed. Allow-list: {1}."
			).format(erp_ver, ", ".join(sorted(_SUPPORTED_ERPNEXT_MINOR)))
		)
	if fr_mm not in _SUPPORTED_FRAPPE_MINOR:
		raise RuntimeError(
			_(
				"IRR Upgrade Guard: Frappe {0} is not on the explicit support allow-list ({1}). "
				"UVR integerization patch not installed."
			).format(fr_ver, ", ".join(sorted(_SUPPORTED_FRAPPE_MINOR)))
		)

	if not hasattr(BuyingController, "update_valuation_rate"):
		raise RuntimeError(
			_(
				"IRR Upgrade Guard: BuyingController.update_valuation_rate missing on ERPNext {0}. "
				"UVR integerization patch not installed."
			).format(erp_ver)
		)
	if not hasattr(buying_controller, _ATTR_LIVE):
		raise RuntimeError(
			_(
				"IRR Upgrade Guard: update_regional_item_valuation_rate missing on ERPNext {0}. "
				"UVR integerization patch not installed."
			).format(erp_ver)
		)

	# Resolve vanilla regional first — raises explicit Iran/state-corruption errors
	# instead of mislabeling the Iran hash as an unsupported ERPNext fingerprint.
	regional = resolve_vanilla_uvr_regional_original(buying_controller)
	validate_vanilla_uvr_regional(regional, role="resolved upstream original")

	uvr = BuyingController.update_valuation_rate
	targets = {
		"update_valuation_rate": uvr,
		"update_regional_item_valuation_rate": regional,
	}

	errors: list[str] = []
	for name, expected in _FN_FINGERPRINTS.items():
		fn = targets[name]
		if name == _ATTR_LIVE and not is_erpnext_module_callable(fn):
			errors.append(
				f"{name}: refused non-erpnext module {_callable_module(fn)!r} "
				"(Iran override must never be fingerprinted as upstream)"
			)
			continue
		errors.extend(_fingerprint_errors(name, fn, expected))

	if not _uvr_calls_regional_hook(uvr):
		errors.append(
			"update_valuation_rate: AST does not call update_regional_item_valuation_rate "
			"(regional hook removed or bypassed)"
		)

	if errors:
		raise RuntimeError(
			_(
				"IRR Upgrade Guard: update_valuation_rate fingerprint unsupported on ERPNext {0}. "
				"UVR integerization patch not installed.\n{1}"
			).format(erp_ver, "\n".join(errors))
		)


def ensure_uvr_regional_patch_markers(buying_controller, vanilla, iran_fn) -> str:
	"""Install or repair UVR regional monkey-patch markers. Returns action taken.

	Deterministic handling of CLEAN / HEALTHY / FLAG_LOST / POISONED / UNKNOWN.
	Never stores an ``erpnext_extensions`` callable as the upstream original.
	"""
	validate_vanilla_uvr_regional(vanilla, role="install upstream original")
	if not is_iran_uvr_override(iran_fn):
		raise RuntimeError(
			_(
				"IRR Upgrade Guard: refused to install non-Iran UVR override ({0})."
			).format(describe_uvr_regional_callable(iran_fn))
		)

	state = classify_uvr_patch_state(buying_controller)
	live = getattr(buying_controller, _ATTR_LIVE, None)
	saved = getattr(buying_controller, _ATTR_SAVED, None)

	if state == UVRPatchState.HEALTHY:
		# Ensure markers stay consistent (idempotent).
		setattr(buying_controller, _ATTR_FLAG, True)
		return "noop_healthy"

	if state == UVRPatchState.FLAG_LOST:
		# live Iran + valid saved vanilla — restore flag only; never overwrite saved.
		if saved is not vanilla and saved is not None:
			validate_vanilla_uvr_regional(saved, role="saved original")
		else:
			setattr(buying_controller, _ATTR_SAVED, vanilla)
		setattr(buying_controller, _ATTR_FLAG, True)
		# Keep live as Iran if already bound; else bind iran_fn.
		if not is_iran_uvr_override(live):
			setattr(buying_controller, _ATTR_LIVE, iran_fn)
		return "restore_flag"

	if state == UVRPatchState.POISONED_ORIGINAL:
		# Only recover when live (or provided vanilla) is proven ERPNext vanilla.
		if is_iran_uvr_override(saved):
			# Drop poisoned pointer; require a proven vanilla (argument or live).
			if hasattr(buying_controller, _ATTR_SAVED):
				delattr(buying_controller, _ATTR_SAVED)
		validate_vanilla_uvr_regional(vanilla, role="recovery upstream original")
		setattr(buying_controller, _ATTR_SAVED, vanilla)
		setattr(buying_controller, _ATTR_LIVE, iran_fn)
		setattr(buying_controller, _ATTR_FLAG, True)
		return "recovered_poisoned"

	if state == UVRPatchState.LIVE_IRAN_NO_SAVED:
		# Cannot invent upstream from Iran live — vanilla must come from resolver.
		validate_vanilla_uvr_regional(vanilla, role="recovery upstream original")
		setattr(buying_controller, _ATTR_SAVED, vanilla)
		setattr(buying_controller, _ATTR_LIVE, iran_fn)
		setattr(buying_controller, _ATTR_FLAG, True)
		return "recovered_live_iran"

	if state == UVRPatchState.UNKNOWN_THIRD_PARTY:
		raise RuntimeError(
			_(
				"IRR Upgrade Guard: unknown third-party update_regional_item_valuation_rate "
				"({0}) — UVR integerization patch not installed."
			).format(describe_uvr_regional_callable(live))
		)

	# CLEAN (or live vanilla with no Iran yet)
	setattr(buying_controller, _ATTR_SAVED, vanilla)
	setattr(buying_controller, _ATTR_LIVE, iran_fn)
	setattr(buying_controller, _ATTR_FLAG, True)
	return "installed"


# Re-export for diagnostics / tests that mirror riv_rate_guard API surface.
__all__ = [
	"UVRPatchState",
	"_ATTR_FLAG",
	"_ATTR_LIVE",
	"_ATTR_SAVED",
	"_FN_FINGERPRINTS",
	"_SUPPORTED_ERPNEXT_MINOR",
	"_SUPPORTED_FRAPPE_MINOR",
	"_resolve_vanilla_regional",
	"assert_erpnext_uvr_regional_patch_supported",
	"classify_uvr_patch_state",
	"collect_fingerprint_report",
	"describe_uvr_regional_callable",
	"ensure_uvr_regional_patch_markers",
	"is_erpnext_module_callable",
	"is_iran_uvr_override",
	"major_minor",
	"normalize_callable_signature",
	"normalize_function_source",
	"resolve_vanilla_uvr_regional_original",
	"source_sha256",
	"validate_vanilla_uvr_regional",
]
