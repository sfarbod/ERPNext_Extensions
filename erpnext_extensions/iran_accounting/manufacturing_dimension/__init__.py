# Copyright (c) 2026, ERPNext Extensions contributors
"""Manufacturing Dimension Uniformity Guard + single-document Dimension Repair."""

from __future__ import annotations

GUARD_PURPOSES = frozenset(
	{
		"Material Transfer for Manufacture",
		"Manufacture",
	}
)
