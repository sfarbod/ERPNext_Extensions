# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Install adjusted-gross disposal helpers onto ERPNext depreciation module.

Sale / regain paths call ``get_gl_entries_on_asset_disposal`` /
``get_asset_disposal_values`` directly; patching keeps them aligned with scrap.
Idempotent for before_request / before_job.
"""

from __future__ import annotations

_APPLIED = False


def apply() -> None:
	global _APPLIED
	if _APPLIED:
		return

	from erpnext.assets.doctype.asset import depreciation as depr_mod

	from erpnext_extensions.asset_usage_depreciation.services.disposal_revalued_gross import (
		get_adjusted_asset_disposal_values,
		get_gl_entries_on_asset_disposal_adjusted,
		get_gl_entries_on_asset_regain_adjusted,
	)

	if getattr(depr_mod, "_erpnext_extensions_disposal_gross_patched", False):
		_APPLIED = True
		return

	depr_mod.get_asset_disposal_values = get_adjusted_asset_disposal_values

	def _disposal(*args, **kwargs):
		# Adjusted gross only — never transfer revaluation surplus in disposal JE.
		kwargs["include_surplus_transfer"] = False
		return get_gl_entries_on_asset_disposal_adjusted(*args, **kwargs)

	depr_mod.get_gl_entries_on_asset_disposal = _disposal
	depr_mod.get_gl_entries_on_asset_regain = get_gl_entries_on_asset_regain_adjusted
	depr_mod._erpnext_extensions_disposal_gross_patched = True
	_APPLIED = True
