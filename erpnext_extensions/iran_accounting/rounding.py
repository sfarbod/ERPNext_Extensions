# Copyright (c) 2026, ERPNext Extensions contributors
"""Compatibility facade for Iran Accounting rounding.

This module contains **no business logic**. It only re-exports the public API
from canonical owners:

	core.rounding → domain.currency → domain.ledger_rounding

New code should import from ``domain.currency`` / ``domain.ledger_rounding``
(or ``core.rounding``). Legacy callers may keep importing from this module.
"""

from __future__ import annotations

from erpnext_extensions.iran_accounting.domain.currency import (  # noqa: F401
	amount_is_fractional,
	amount_rate_qty_residual,
	cint_safe,
	get_company_currency,
	get_currency_precision,
	integer_valuation_rate_from_amount,
	is_irr_company,
	is_irr_currency,
	is_zero_decimal_currency,
	rate_is_fractional,
	round_currency,
	round_currency_amount,
	round_if_irr,
	round_irr_rate,
	round_monetary_rate,
	round_row_amount,
)
from erpnext_extensions.iran_accounting.domain.ledger_rounding import (  # noqa: F401
	GL_AMOUNT_FIELDS,
	SLE_MONETARY_FIELDS,
	STOCK_ENTRY_ITEM_MONETARY_FIELDS,
	STOCK_ENTRY_ITEM_RATE_FIELDS,
	STOCK_ENTRY_TOTAL_FIELDS,
	reconcile_irr_sle_after_rounding,
	round_gl_entry_amounts,
	round_sle_monetary_fields,
	round_stock_entry_totals,
)
