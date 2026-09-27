# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

from __future__ import annotations

import frappe

from erpnext_extensions.asset_usage_depreciation.constants import (
	COMPANY_FIELD_REDUCED_HANDLING,
	HANDLING_ADJUST_FINAL,
	HANDLING_REDISTRIBUTE_LEGACY,
	MODULE,
)
from erpnext_extensions.asset_usage_depreciation.custom_fields import ensure_custom_fields


def _ensure_module_def():
	if frappe.db.exists("Module Def", MODULE):
		return
	doc = frappe.get_doc(
		{
			"doctype": "Module Def",
			"module_name": MODULE,
			"app_name": "erpnext_extensions",
		}
	)
	doc.insert(ignore_permissions=True)


def _migrate_legacy_reduced_handling_option():
	"""Map legacy Company option to Adjust Final Depreciation Installment."""
	if not frappe.db.has_column("Company", COMPANY_FIELD_REDUCED_HANDLING):
		return
	frappe.db.sql(
		f"""
		UPDATE `tabCompany`
		SET `{COMPANY_FIELD_REDUCED_HANDLING}` = %s
		WHERE `{COMPANY_FIELD_REDUCED_HANDLING}` = %s
		""",
		(HANDLING_ADJUST_FINAL, HANDLING_REDISTRIBUTE_LEGACY),
	)


def after_migrate():
	_ensure_module_def()
	ensure_custom_fields()
	_migrate_legacy_reduced_handling_option()
	from erpnext_extensions.asset_usage_depreciation.workflow import ensure_asset_request_workflow

	ensure_asset_request_workflow()
	from erpnext_extensions.asset_usage_depreciation.services.dimension_service import (
		provision_asset_request_accounting_dimensions,
	)

	provision_asset_request_accounting_dimensions()
	from erpnext_extensions.asset_usage_depreciation.services.depr_posting_guard import (
		install_depreciation_idempotency_patch,
	)

	install_depreciation_idempotency_patch()
