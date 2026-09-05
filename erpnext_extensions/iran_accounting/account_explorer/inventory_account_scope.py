# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Account chart membership under inventory filters.

v5.1.1 Case A: Account *measures* under Item/Item Group/Warehouse filters are
SLE-scoped stock attribution (``sle_scoped_stock``), not voucher-scoped GL.

Chart membership (``included_account_names``) stays the company account tree;
rows without attributed stock activity hide via ``hide_zero_rows``.
Do not shrink the chart to inventory accounts alone — Case B Account browsing
still needs the full tree when inventory filters clear.
"""

from __future__ import annotations

from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec

__all__ = [
	"apply_inventory_related_account_scope",
]


def apply_inventory_related_account_scope(spec: AccountExplorerQuerySpec) -> None:
	"""No-op: keep company chart membership; Case A measures are SLE-scoped."""
	return
