# Copyright (c) 2026, ERPNext Extensions contributors
"""Account.advance_account_type custom field (v5.7.42)."""

from __future__ import annotations

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from erpnext_extensions.advance_accounts.constants import (
	ADVANCE_ACCOUNT_TYPE_OPTIONS,
	CF_ADVANCE_ACCOUNT_TYPE,
	MODULE,
)


def get_custom_fields() -> dict:
	return {
		"Account": [
			{
				"fieldname": CF_ADVANCE_ACCOUNT_TYPE,
				"label": "Advance Account Type",
				"fieldtype": "Select",
				"options": ADVANCE_ACCOUNT_TYPE_OPTIONS,
				"insert_after": "account_type",
				"description": (
					"Marks this ledger as a separate-party Advance Received (Liability/Receivable) "
					"or Advance Paid (Asset/Payable) account. Used for multi-account advance "
					"recognition on Payment Entry and invoice allocation."
				),
				"in_standard_filter": 1,
				"module": MODULE,
			},
		],
	}


def ensure_custom_fields() -> None:
	create_custom_fields(get_custom_fields(), update=True)


def after_migrate() -> None:
	ensure_custom_fields()
	from erpnext_extensions.advance_accounts.bootstrap import apply

	apply()
