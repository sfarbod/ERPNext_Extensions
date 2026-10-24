# Copyright (c) 2026 — experimental harness retained for rollback demos.
"""Prototype package kept for historical harness; production code lives in domain/."""

from erpnext_extensions.iran_accounting.domain.physical_two_slice import (  # noqa: F401
	AllocationError,
	AllocationPlan,
	ValuationSlice,
	allocate_physical_two_slice,
	simulate_sequential,
)
