# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Authoritative RQ routing for Account Explorer background work (v5.1.5).

Policy (production 3 long + 4 short, no custom queues required):

- Interactive prepared summaries (Account / Voucher) → ``short``
  with an elevated job timeout and ``at_front=True`` so they are not
  starved behind bulk exports on ``long``.

- Large asynchronous exports → ``long``
  (heavy GL materialization; keeps short free for interactive work).

This isolates export/prepared contention **by construction** using the
existing worker topology. No dedicated Docker queue is required for
v5.1.5.
"""

from __future__ import annotations

from typing import Any

# Interactive prepared builds (Account Levels / Voucher).
AE_PREPARED_QUEUE = "short"
AE_PREPARED_TIMEOUT_SECONDS = 900

# Bulk export (CSV/XLSX background jobs).
AE_EXPORT_QUEUE = "long"
# Rely on Frappe long-queue default timeout (1500s) unless overridden.
AE_EXPORT_TIMEOUT_SECONDS = None


def prepared_enqueue_kwargs(**extra: Any) -> dict[str, Any]:
	"""Keyword args for ``frappe.enqueue`` of prepared-result builds."""
	kwargs: dict[str, Any] = {
		"queue": AE_PREPARED_QUEUE,
		"timeout": AE_PREPARED_TIMEOUT_SECONDS,
		"at_front": True,
	}
	kwargs.update(extra)
	return kwargs


def export_enqueue_kwargs(**extra: Any) -> dict[str, Any]:
	"""Keyword args for ``frappe.enqueue`` of background exports."""
	kwargs: dict[str, Any] = {
		"queue": AE_EXPORT_QUEUE,
	}
	if AE_EXPORT_TIMEOUT_SECONDS is not None:
		kwargs["timeout"] = AE_EXPORT_TIMEOUT_SECONDS
	kwargs.update(extra)
	return kwargs


def assert_queues_isolated() -> None:
	"""Fail fast if policy regresses into a shared starvation domain."""
	if AE_PREPARED_QUEUE == AE_EXPORT_QUEUE:
		raise RuntimeError(
			"Account Explorer prepared and export queues must differ "
			f"(both currently {AE_PREPARED_QUEUE!r})."
		)
