# Copyright (c) 2026, ERPNext Extensions contributors
"""Report-only discovery of SCR service-cost correction candidates.

This patch never mutates business documents and never enqueues RIV.
Execute corrections through the whitelisted API after review:

	erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.api.correct_subcontracting_receipt_service_cost
"""

from __future__ import annotations

import frappe


def execute():
	from erpnext_extensions.iran_accounting.subcontracting_receipt_service_cost.service import (
		discover_eligible_subcontracting_receipts,
	)

	# Run as Administrator context already provided by patch runner.
	report = discover_eligible_subcontracting_receipts(limit=500)
	frappe.logger("scr_service_cost_correction").info(
		{
			"event": "scr_service_cost_correction_report",
			"candidate_count": report.get("candidate_count"),
			"eligible_count": report.get("eligible_count"),
			"errors": report.get("errors"),
		}
	)
	# Surface a concise summary in the migrate log without dumping every row.
	print(
		f"SCR service-cost correction report: "
		f"candidates={report.get('candidate_count')} "
		f"eligible={report.get('eligible_count')} "
		f"errors={len(report.get('errors') or [])}"
	)
