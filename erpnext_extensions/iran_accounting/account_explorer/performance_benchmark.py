# Copyright (c) 2026, Farbod Siyahpoosh and contributors

from __future__ import annotations

import time
from typing import Any

import frappe
from frappe import _

from erpnext_extensions.iran_accounting.account_explorer.permissions import assert_diagnostics_allowed
from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
	AccountExplorerQuerySpec_from_client,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec

BENCHMARK_SCALES = (100_000, 500_000, 1_000_000)
BENCHMARK_SCENARIOS = ("account_level", "party", "dimension", "voucher", "item_group", "item")

INDEX_RECOMMENDATIONS = [
	{
		"table": "GL Entry",
		"columns": ["company", "posting_date", "is_cancelled"],
		"reason": _("Speeds up company/date scoped Account Explorer queries."),
	},
	{
		"table": "GL Entry",
		"columns": ["company", "party_type", "party"],
		"reason": _("May improve party axis aggregation when party filters are active."),
	},
]


def count_company_gl_entries(company: str) -> int:
	return frappe.db.count("GL Entry", {"company": company})


def _benchmark_payload(
	company: str,
	fiscal_year: str,
	from_date: str,
	to_date: str,
	view_axis: str,
) -> dict:
	analysis = {"view_axis": view_axis, "detail_mode": "summary", "page_size": 50}
	if view_axis == "dimension":
		from erpnext_extensions.iran_accounting.account_explorer.dimension_discovery import (
			get_default_dimension_type,
		)

		dimension_type = get_default_dimension_type()
		if not dimension_type:
			return {}
		analysis["dimension_scope"] = {"dimension_type": dimension_type}
	return {
		"document_scope": {
			"company": company,
			"fiscal_year": fiscal_year,
			"from_date": from_date,
			"to_date": to_date,
			"hide_zero_rows": 0,
			"status": {
				"include_opening_entries": 1,
				"include_cancelled_entries": 0,
				"include_default_finance_book_entries": 1,
				"include_period_closing_vouchers": 0,
			},
		},
		"analysis_context": analysis,
	}


def _run_summary_for_spec(spec: AccountExplorerQuerySpec) -> dict:
	axis = spec.view_axis
	if axis == "account_level":
		from erpnext_extensions.iran_accounting.account_explorer.query_builder import (
			build_account_level_summary,
		)

		return build_account_level_summary(spec)
	if axis == "party":
		from erpnext_extensions.iran_accounting.account_explorer.party_summary import build_party_summary

		return build_party_summary(spec)
	if axis == "dimension":
		from erpnext_extensions.iran_accounting.account_explorer.dimension_summary import (
			build_dimension_summary,
		)

		return build_dimension_summary(spec)
	if axis == "voucher":
		from erpnext_extensions.iran_accounting.account_explorer.voucher_summary import (
			build_voucher_summary,
		)

		return build_voucher_summary(spec)
	if axis == "item_group":
		from erpnext_extensions.iran_accounting.account_explorer.item_group_summary import (
			build_item_group_summary,
		)

		return build_item_group_summary(spec)
	if axis == "item":
		from erpnext_extensions.iran_accounting.account_explorer.item_summary import build_item_summary

		return build_item_summary(spec)
	frappe.throw(_("Unsupported benchmark scenario."))


def measure_summary_scenario(
	company: str,
	fiscal_year: str,
	from_date: str,
	to_date: str,
	view_axis: str,
) -> dict[str, Any]:
	payload = _benchmark_payload(company, fiscal_year, from_date, to_date, view_axis)
	if not payload:
		return {
			"scenario": view_axis,
			"skipped": 1,
			"reason": _("No dimension type available for benchmark."),
		}

	spec = AccountExplorerQuerySpec_from_client(payload, require_dates=True)
	start = time.perf_counter()
	result = _run_summary_for_spec(spec)
	elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
	pagination = result.get("pagination") or {}
	return {
		"scenario": view_axis,
		"skipped": 0,
		"elapsed_ms": elapsed_ms,
		"result_row_count": pagination.get("total_rows") or len(result.get("rows") or []),
		"page_size": pagination.get("page_size"),
	}


def propose_index_recommendations(results: list[dict], *, query_timeout_seconds: int) -> list[dict]:
	timeout_ms = query_timeout_seconds * 1000
	slow = [row for row in results if not row.get("skipped") and row.get("elapsed_ms", 0) > timeout_ms]
	recommendations = []
	for item in INDEX_RECOMMENDATIONS:
		recommendations.append(
			{
				**item,
				"status": "recommended_if_slow" if slow else "documentation_only",
				"auto_apply": 0,
			}
		)
	if slow:
		recommendations.insert(
			0,
			{
				"table": "GL Entry",
				"columns": ["company", "posting_date"],
				"reason": _(
					"Measured summary execution exceeded query_timeout_seconds ({0}s) on this site."
				).format(query_timeout_seconds),
				"status": "evidence_triggered",
				"auto_apply": 0,
				"slow_scenarios": [row["scenario"] for row in slow],
			},
		)
	return recommendations


def run_account_explorer_performance_benchmark(
	company: str,
	fiscal_year: str,
	from_date: str,
	to_date: str,
	*,
	scales: tuple[int, ...] | None = None,
	scenarios: tuple[str, ...] | None = None,
) -> dict[str, Any]:
	assert_diagnostics_allowed()
	gl_row_count = count_company_gl_entries(company)
	settings = frappe.get_single("Iran Accounting Settings")
	scales = scales or BENCHMARK_SCALES
	scenarios = scenarios or BENCHMARK_SCENARIOS

	measurements: list[dict] = []
	for scale in scales:
		for scenario in scenarios:
			measurement = measure_summary_scenario(company, fiscal_year, from_date, to_date, scenario)
			measurement["target_gl_rows"] = scale
			measurement["actual_gl_rows"] = gl_row_count
			measurement["scale_reached"] = gl_row_count >= scale
			measurements.append(measurement)

	recommendations = propose_index_recommendations(
		measurements,
		query_timeout_seconds=int(settings.query_timeout_seconds or 30),
	)

	return {
		"company": company,
		"gl_row_count": gl_row_count,
		"target_scales": list(scales),
		"scenarios": list(scenarios),
		"measurements": measurements,
		"index_recommendations": recommendations,
		"documentation_only": 1,
		"note": _(
			"Benchmark results are observational. No database indexes are created automatically."
		),
	}
