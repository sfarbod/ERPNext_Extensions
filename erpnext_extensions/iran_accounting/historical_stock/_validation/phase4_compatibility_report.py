# Copyright (c) 2026, ERPNext Extensions contributors
"""IRAN_ACCOUNTING_COMPATIBILITY_REPORT for Sep-30 Phase-4 freeze."""

from __future__ import annotations

import json
from pathlib import Path

from erpnext_extensions import __version__

OUT = Path(
	"/workspace/development/frappe-bench/sites/development.localhost/"
	"private/files/hr_correction_20260930"
)

MATRIX = [
	{
		"family": "LEFTOVER_MA",
		"old_historical": "HR stamp vr=sv/qty + downstream replay",
		"current_iran": "apply_irr_deterministic_sle_valuation + leftover_ma RIV on_change restore",
		"owner": "Iran Accounting",
		"hr_responsibility": "BRIDGE_ONLY (RIV survival hook); NO_DIRECT mid-chain mutation for dashboard zero",
		"native_replay": True,
		"obsolete_workaround": "LEGACY_COMPATIBILITY — stamp_patient_zero still required if Core leaves tip rate0",
	},
	{
		"family": "WRONG_RATE",
		"old_historical": "generic SVD expected-rate apply",
		"current_iran": "Manufacture outputs owned by allocate_* contracts; transfers by Core source-side",
		"owner": "Iran Accounting / ERPNext Core",
		"hr_responsibility": "detect + EXACT roots only; manufacture via IRAN_NATIVE_HISTORICAL bridge",
		"native_replay": "partial",
		"obsolete_workaround": "STILL_REQUIRED for EXACT non-manufacture roots",
	},
	{
		"family": "ZERO_RATE",
		"old_historical": "force nonzero",
		"current_iran": "legitimate zero receipts/scrap/reject preserved; avg from balance when valued",
		"owner": "Iran Accounting",
		"hr_responsibility": "classify only; never force legitimate zeros",
		"native_replay": True,
		"obsolete_workaround": "OBSOLETE force-nonzero paths",
	},
	{
		"family": "TRANSFER_PROPAGATION",
		"old_historical": "ad-hoc descendant SVD",
		"current_iran": "ERPNext transfer uses source warehouse valuation",
		"owner": "ERPNext Core",
		"hr_responsibility": "NATIVE_REPLAY_EXPECTED — no direct descendant repair",
		"native_replay": True,
		"obsolete_workaround": "DANGEROUS_DUPLICATION if HR invents transfer rates",
	},
	{
		"family": "COMPONENT_SCRAP",
		"old_historical": "copy FG rate",
		"current_iran": "apply_component_scrap_issued_rates (same-voucher issued)",
		"owner": "Iran Accounting",
		"hr_responsibility": "bridge stamp → invoke native",
		"native_replay": True,
		"obsolete_workaround": "OBSOLETE FG-rate scrap",
	},
	{
		"family": "PRODUCT_REJECT",
		"old_historical": "excluded / co-product rules",
		"current_iran": "allocate_scrap_absorbed_cost + pre-core bridge (5.3.37)",
		"owner": "Iran Accounting",
		"hr_responsibility": "adopt MAIN_PRODUCT_REJECT when same-as-FG Scrap proven; never stage-bridge Scrap",
		"native_replay": True,
		"obsolete_workaround": "DANGEROUS_DUPLICATION fixed in v5.3.41 (stage flag on Scrap)",
	},
	{
		"family": "CO_PRODUCT / BY_PRODUCT / AFG / STAGE_EQUIVALENT",
		"old_historical": "stock-UOM equality",
		"current_iran": "allocate_stage_output_cost equivalent-unit",
		"owner": "Iran Accounting",
		"hr_responsibility": "historical stamp + bridge; qty untouched",
		"native_replay": True,
		"obsolete_workaround": "OBSOLETE stock-UOM equality requirement",
	},
	{
		"family": "PRIMARY_FG_REMAINDER / IRR_RESIDUAL",
		"old_historical": "absorb into additional_cost or dump Round Off",
		"current_iran": "TYPE C → Stock Adjustment 621301; Round Off 622515 excluded for stock residual",
		"owner": "Iran Accounting",
		"hr_responsibility": "none — never invent 621301 balancing",
		"native_replay": True,
		"obsolete_workaround": "LEGACY_COMPATIBILITY for stamp 5.3.3 leftover-in-ac",
	},
	{
		"family": "RIV_SURVIVAL / FAILED_RIV_VALUATION_INTEGRITY",
		"old_historical": "blind retry",
		"current_iran": "riv_valuation_guard fail-closed; leftover_ma restore after Completed",
		"owner": "Iran Accounting",
		"hr_responsibility": "FAILED_RIV_CAUSAL_ANALYZER — no blind retry",
		"native_replay": "after root fix",
		"obsolete_workaround": "OBSOLETE blind retry",
	},
	{
		"family": "VALUED_SOURCE_ZERO_OUTGOING",
		"old_historical": "SQL patch",
		"current_iran": "integrity guard blocks poison persist",
		"owner": "Iran Accounting",
		"hr_responsibility": "upstream root repair then native replay",
		"native_replay": True,
		"obsolete_workaround": "DANGEROUS_DUPLICATION SQL-zero",
	},
]


def build_report() -> dict:
	report = {
		"title": "IRAN_ACCOUNTING_COMPATIBILITY_REPORT",
		"erpnext_extensions_version": __version__,
		"valuation_entry_points": {
			"sle_avg_rate": "domain.stock_ledger_deterministic.apply_irr_deterministic_sle_valuation",
			"repost": "domain.repost_determinism → apply_irr_deterministic_sle_valuation",
			"manufacture": "scrap_costing.apply_iran_manufacture_output_contract",
			"stage": "manufacture_stage_costing.allocate_stage_output_cost",
			"product_reject": "allocate_scrap_absorbed_cost + permit_product_reject_zero_valuation",
			"component_scrap": "apply_component_scrap_issued_rates",
			"bulk_scrap": "apply_bulk_scrap_zero_valuation (intentional zero; not issued-rate)",
			"leftover_ma_hook": "historical_stock.leftover_ma.on_repost_item_valuation_update",
			"riv_guard": "domain.riv_valuation_guard",
			"irr_sa": "domain.manufacture_irr_residual → Company.stock_adjustment_account (621301)",
		},
		"hooks": {
			"Stock Entry": "before_validate/validate/before_submit/on_submit → stock_entry.py",
			"Stock Ledger Entry": "validate/before_insert/after_insert → stock_ledger.py",
			"Repost Item Valuation": "on_change → leftover_ma.on_repost_item_valuation_update",
		},
		"matrix": MATRIX,
		"leftover_ma_verdicts": {
			"13100023": "CURRENT_IRAN_NATIVE_HANDLED — tip valued; mid-chain rate0 cleared by native RIV/repost; no direct HR mutation in frozen plan",
			"17000001": "CURRENT_IRAN_NATIVE_HANDLED — same",
		},
		"true_business": [
			{
				"pattern": "BULK_SCRAP_INTENTIONAL_ZERO",
				"example": "MAT-STE-2026-37603 row 30100101",
				"class": "BULK_SCRAP",
				"rate": 0,
				"valuation_contribution": 0,
				"classification": "LEGITIMATE_ZERO",
				"business_question": "RESOLVED",
				"status": "RESOLVED",
				"authority": (
					"Business decision: 30100101 = Bulk Scrap / ضایعات بالک; "
					"generic contract BULK_SCRAP → intentional zero valuation "
					"(distinct from COMPONENT_SCRAP issued-rate)."
				),
				"generic_rule": (
					"Scrap with custom_output_class=BULK_SCRAP, or valuation_type=Manual "
					"+ zero rate + allow_zero/set_basic_rate_manually, and not "
					"MAIN_PRODUCT_REJECT / COMPONENT_SCRAP / CO_PRODUCT_REJECT."
				),
			}
		],
		"bulk_scrap_contract": {
			"class": "BULK_SCRAP",
			"rate": 0,
			"enters_stage_pool": False,
			"uses_component_issued_rate": False,
			"uses_fg_absorbed_rate": False,
			"wrong_rate_actionable": False,
			"zero_rate_actionable": False,
			"vs_component_scrap": "COMPONENT_SCRAP keeps issued-rate; BULK_SCRAP stays 0",
		},
	}
	OUT.mkdir(parents=True, exist_ok=True)
	(OUT / "IRAN_ACCOUNTING_COMPATIBILITY_REPORT.json").write_text(
		json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
	)
	return {
		"version": __version__,
		"families": len(MATRIX),
		"true_business_n": len(report["true_business"]),
		"path": str(OUT / "IRAN_ACCOUNTING_COMPATIBILITY_REPORT.json"),
	}
