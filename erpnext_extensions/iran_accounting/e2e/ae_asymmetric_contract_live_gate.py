"""Live Case A/B forensic gate for asymmetric contract. No commit."""
from __future__ import annotations

import json
import time

import frappe
from frappe.utils import flt


def run():
	frappe.set_user("Administrator")
	from erpnext_extensions.iran_accounting.account_explorer.api import (
		get_account_summary,
		get_item_group_summary,
		get_item_summary,
	)
	from erpnext_extensions.iran_accounting.account_explorer.inventory_account_measures import (
		get_inventory_account_attribution,
	)
	from erpnext_extensions.iran_accounting.account_explorer.query_spec import (
		AccountExplorerQuerySpec_from_client,
	)
	from erpnext_extensions.iran_accounting.account_explorer.cross_tab_numeric_contract import (
		relation,
	)

	company = "اسپاد فارمد دارو"
	fy = frappe.db.sql(
		"""
		select fy.name, fy.year_start_date, fy.year_end_date from `tabFiscal Year` fy
		inner join `tabFiscal Year Company` fyc on fyc.parent=fy.name
		where fyc.company=%s order by fy.year_start_date desc limit 1
		""",
		company,
		as_dict=1,
	)[0]

	def payload(axis, inventory=None):
		return {
			"document_scope": {
				"company": company,
				"fiscal_year": fy.name,
				"from_date": str(fy.year_start_date),
				"to_date": str(fy.year_end_date),
				"hide_zero_rows": 1,
				"status": {
					"include_opening_entries": 1,
					"include_cancelled_entries": 0,
					"include_default_finance_book_entries": 1,
					"include_period_closing_vouchers": 0,
				},
				"inventory": inventory or {},
			},
			"analysis_context": {
				"view_axis": axis,
				"level_sequence": 3,
				"page_size": 500,
				"detail_mode": "summary",
			},
			"prepared_mode": "live",
		}

	timings = {}
	def timed(name, fn):
		t0 = time.perf_counter()
		out = fn()
		timings[name] = round((time.perf_counter() - t0) * 1000, 1)
		return out

	samples = {k: [] for k in ["ig", "item", "ac_case_a", "ac_case_b"]}
	for _ in range(5):
		samples["ig"].append(timed("ig", lambda: get_item_group_summary(payload("item_group", {"item_group": "API"}))))
		samples["ac_case_a"].append(
			timed("ac_a", lambda: get_account_summary(payload("account_level", {"item_group": "API"})))
		)
	# keep last
	ig = samples["ig"][-1]
	ac = samples["ac_case_a"][-1]
	item_sum = timed("item_all_api", lambda: get_item_summary(payload("item", {"item_group": "API"})))
	ac_bare = timed("ac_b", lambda: get_account_summary(payload("account_level", {})))

	ig_t = ig["totals"]
	ac_t = ac["totals"]
	it_t = item_sum["totals"]

	# Item 13200023 multi-account
	item_code = "13200023"
	it_one = get_item_summary(payload("item", {"item": item_code}))
	ac_one = get_account_summary(payload("account_level", {"item": item_code}))
	spec_one = AccountExplorerQuerySpec_from_client(
		payload("account_level", {"item": item_code}), require_dates=True
	)
	attr_one = get_inventory_account_attribution(spec_one)

	# Case B reverse: pick inventory account with stock + compare to items that map to it
	inv_acc = next(iter(attr_one.rows_by_account.keys()), None)
	case_b = {
		"account_fact_engine": ac_bare.get("account_fact_engine"),
		"account_period_debit": ac_bare["totals"].get("period_debit"),
		"relation_account_to_ig": relation("account_level", "item_group", "period_debit"),
		"ig_inward_unfiltered": get_item_group_summary(payload("item_group", {}))["totals"].get(
			"inward_value"
		),
	}

	def pct(vals):
		s = sorted(vals)
		n = len(s)
		return {"p50": s[n // 2], "p90": s[max(0, int(n * 0.9) - 1)], "p95": s[-1], "samples": s}

	# re-sample timings cleanly
	perf = {}
	for key, call in [
		("item_group_API", lambda: get_item_group_summary(payload("item_group", {"item_group": "API"}))),
		("item_item_group_API", lambda: get_item_summary(payload("item", {"item_group": "API"}))),
		("account_case_a_API", lambda: get_account_summary(payload("account_level", {"item_group": "API"}))),
		("account_case_b", lambda: get_account_summary(payload("account_level", {}))),
	]:
		xs = []
		for _ in range(5):
			t0 = time.perf_counter()
			call()
			xs.append(round((time.perf_counter() - t0) * 1000, 1))
		perf[key] = pct(xs)

	out = {
		"fy": [str(fy.year_start_date), str(fy.year_end_date)],
		"case_a_engine": ac.get("account_fact_engine"),
		"case_a_axis_engine": ac.get("account_axis_engine"),
		"ig": ig_t,
		"item_sum_under_api": it_t,
		"account_under_api": ac_t,
		"delta_ig_item": {
			"inward": flt(ig_t.get("inward_value")) - flt(it_t.get("inward_value")),
			"outward": flt(ig_t.get("outward_value")) - flt(it_t.get("outward_value")),
			"balance": flt(ig_t.get("balance_value")) - flt(it_t.get("balance_value")),
		},
		"delta_ig_account": {
			"inward": flt(ig_t.get("inward_value")) - flt(ac_t.get("period_debit")),
			"outward": flt(ig_t.get("outward_value")) - flt(ac_t.get("period_credit")),
			"balance": flt(ig_t.get("balance_value")) - flt(ac_t.get("net_balance")),
		},
		"item_13200023": {
			"item": it_one["totals"],
			"account": ac_one["totals"],
			"engine": ac_one.get("account_fact_engine"),
			"accounts": {
				a: {
					"inward": v.get("inward_value"),
					"outward": v.get("outward_value"),
					"bal": v.get("balance_value"),
				}
				for a, v in attr_one.rows_by_account.items()
			},
			"delta": {
				"inward": flt(it_one["totals"].get("inward_value"))
				- flt(ac_one["totals"].get("period_debit")),
				"outward": flt(it_one["totals"].get("outward_value"))
				- flt(ac_one["totals"].get("period_credit")),
				"balance": flt(it_one["totals"].get("balance_value"))
				- flt(ac_one["totals"].get("net_balance")),
			},
		},
		"case_b": case_b,
		"perf_ms": perf,
		"gates": {
			"A_ig_eq_account": all(
				abs(flt(ig_t.get(k1)) - flt(ac_t.get(k2))) < 0.02
				for k1, k2 in (
					("inward_value", "period_debit"),
					("outward_value", "period_credit"),
					("balance_value", "net_balance"),
				)
			),
			"B_ig_eq_item": all(
				abs(flt(ig_t.get(k)) - flt(it_t.get(k))) < 0.02
				for k in ("inward_value", "outward_value", "balance_value")
			),
			"C_item_multi_account": all(
				abs(v) < 0.02 for v in [
					flt(it_one["totals"].get("inward_value")) - flt(ac_one["totals"].get("period_debit")),
					flt(it_one["totals"].get("outward_value")) - flt(ac_one["totals"].get("period_credit")),
					flt(it_one["totals"].get("balance_value")) - flt(ac_one["totals"].get("net_balance")),
				]
			),
			"D_case_b_engine_posted": ac_bare.get("account_fact_engine") == "posted_gl",
			"E_reverse_not_equal": relation("account_level", "item_group", "period_debit")
			== "RECONCILABLE",
			"F_case_a_engine": ac.get("account_fact_engine") == "sle_scoped_stock"
			and ac.get("account_axis_engine") == "sle_scoped_stock",
		},
	}
	path = "/workspace/development/frappe-bench/apps/erpnext_extensions/erpnext_extensions/iran_accounting/e2e/screenshots/asymmetric_contract_live_gate.json"
	with open(path, "w", encoding="utf-8") as f:
		json.dump(out, f, ensure_ascii=False, indent=2, default=str)
	print(json.dumps({"wrote": path, "gates": out["gates"], "perf_ms": perf, "deltas": {"ig_ac": out["delta_ig_account"], "ig_item": out["delta_ig_item"], "item132": out["item_13200023"]["delta"]}}, ensure_ascii=False, indent=2))
	return out
