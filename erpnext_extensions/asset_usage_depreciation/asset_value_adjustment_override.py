# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Asset Value Adjustment override for non-depreciating assets.

ERPNext's ``AssetValueAdjustment.update_asset`` always calls
``reschedule_depreciation``. That path builds and submits an
``Asset Depreciation Schedule``, and ``AssetDepreciationSchedule.validate_asset``
throws when ``calculate_depreciation = 0``.

Production non-depreciating assets often retain orphan ``Asset Finance Book``
child rows (the table is cleared only on Asset save), so AVA submit fails even
though no depreciation schedule should exist.

This override keeps valuation + Journal Entry behaviour unchanged, and only
skips depreciation schedule rebuild when depreciation is disabled. Assets with
``calculate_depreciation = 1`` follow the core path unchanged.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint

from erpnext.assets.doctype.asset_depreciation_schedule.asset_depreciation_schedule import (
	reschedule_depreciation,
)
from erpnext.assets.doctype.asset_value_adjustment.asset_value_adjustment import (
	AssetValueAdjustment as CoreAssetValueAdjustment,
)


class AssetValueAdjustment(CoreAssetValueAdjustment):
	def update_asset(self):
		"""Update carrying value; rebuild ADS only when depreciation is enabled."""
		asset = self.update_asset_value_after_depreciation()
		note = self.get_adjustment_note()

		if cint(asset.calculate_depreciation):
			reschedule_depreciation(asset, note)
		else:
			# Explicitly avoid creating/rebuilding depreciation schedules for
			# intentionally non-depreciating assets (including those with orphan
			# finance-book rows).
			frappe.logger("erpnext_extensions.ava").info(
				"Skipping depreciation reschedule for non-depreciating asset %s on AVA %s",
				asset.name,
				self.name,
			)

		asset.set_status()
