# Copyright (c) 2026, ERPNext Extensions contributors
"""Operator Decision Wizard — guided choices with full evidence."""

from __future__ import annotations

from frappe.utils import flt


def build_decision_card(
	row: dict,
	*,
	evidence: dict | None = None,
	resolution: dict | None = None,
	impact: dict | None = None,
	shortage: dict | None = None,
) -> dict:
	"""Produce a complete operator-facing decision package."""
	evidence = evidence or {}
	resolution = resolution or {}
	impact = impact or {}
	shortage = shortage or {}

	voucher = row.get("voucher") or row.get("voucher_no")
	item = row.get("item") or row.get("item_code")
	topic = row.get("topic") or "UNKNOWN"
	ps = row.get("planner_status") or row.get("status")

	what_wrong = _what_is_wrong(row, evidence)
	why_stopped = _why_stopped(row, resolution, shortage)
	choices = _choices(row, resolution, shortage)
	suggested = resolution.get("expected") or (resolution.get("scored") or {}).get("recommended")
	if isinstance(suggested, dict):
		suggested_rate = suggested.get("rate")
		suggested_source = suggested.get("top_source")
	else:
		suggested_rate = suggested
		suggested_source = resolution.get("source")

	return {
		"voucher": voucher,
		"item": item,
		"warehouse": row.get("warehouse"),
		"topic": topic,
		"planner_status": ps,
		"assisted_status": resolution.get("promote_to") or shortage.get("outcome"),
		"what_is_wrong": what_wrong,
		"why_automatic_repair_stopped": why_stopped,
		"possible_choices": choices,
		"expected_consequence": _consequences(choices, impact),
		"suggested_choice": {
			"rate": suggested_rate,
			"source": suggested_source,
			"label": resolution.get("reason") or shortage.get("reason"),
		},
		"confidence": flt(resolution.get("confidence") or 0),
		"estimated_downstream_repairs_unlocked": int(impact.get("unlocked_estimate") or 0),
		"impact_rank_score": flt(impact.get("score") or 0),
		"evidence_summary": {
			"dependency_chain": evidence.get("dependency_chain") or [],
			"rate_sources": evidence.get("rate_sources") or {},
			"previous_sle": evidence.get("previous_sle"),
			"purchase_receipts_n": len(evidence.get("purchase_receipts") or []),
			"reconciliations_n": len(evidence.get("stock_reconciliations") or []),
			"gl": evidence.get("gl"),
			"options": (resolution.get("scored") or {}).get("options") or [],
			"hidden_inbound_actionable": (shortage.get("actionable") or [])[:5],
		},
	}


def _what_is_wrong(row, evidence) -> str:
	flags = row.get("flags") or []
	cur = flt(row.get("current") if row.get("current") is not None else row.get("current_rate"))
	if row.get("topic") in ("WRONG_RATE", "ZERO_RATE") or flags:
		return (
			f"Rate mismatch on {row.get('voucher')}: current={cur}, "
			f"reconstructed candidates present ({len(evidence.get('rate_sources') or {})} sources), "
			f"flags={flags or row.get('zero_class') or row.get('gl_class') or 'n/a'}"
		)
	opt = row.get("optimizer_status") or row.get("status")
	if opt == "REAL_STOCK_SHORTAGE" or "SHORTAGE" in str(opt or ""):
		return f"Stock goes negative before outbound {row.get('outbound_document') or row.get('voucher')}"
	if row.get("gl_class"):
		return f"GL class {row.get('gl_class')} on {row.get('voucher')}"
	return f"Residual issue on {row.get('voucher')} ({row.get('planner_status')})"


def _why_stopped(row, resolution, shortage) -> str:
	if shortage.get("proven_shortage"):
		return shortage.get("reason") or "Proven real stock shortage"
	if resolution.get("reason"):
		return resolution["reason"]
	ps = str(row.get("planner_status") or "")
	if "AMBIGUOUS" in ps:
		return "Reconstruction sources disagree and no unique cluster reached assisted threshold"
	if "MANUAL" in ps:
		return "Planner required operator review (MANUAL / Z0 / LIKELY)"
	return row.get("reason") or "Automatic path exhausted"


def _choices(row, resolution, shortage) -> list[dict]:
	if shortage.get("actionable"):
		out = []
		for i, c in enumerate(shortage["actionable"][:5]):
			out.append(
				{
					"id": f"restore_{i}",
					"label": f"Investigate {c.get('kind')} {c.get('voucher')}",
					"payload": c,
				}
			)
		out.append({"id": "accept_shortage", "label": "Accept REAL_STOCK_SHORTAGE", "payload": {}})
		return out
	if resolution.get("promote_to") == "ASSISTED_READY" or (resolution.get("scored") or {}).get("options"):
		opts = (resolution.get("scored") or {}).get("options") or []
		out = []
		for i, o in enumerate(opts[:5]):
			out.append(
				{
					"id": f"rate_{i}",
					"label": f"Use rate {o['rate']} ({o['confidence']:.0%} via {', '.join(o['sources'])})",
					"rate": o["rate"],
					"confidence": o["confidence"],
					"sources": o["sources"],
				}
			)
		out.append({"id": "skip", "label": "Defer — need external business evidence", "rate": None})
		return out
	wh = resolution.get("choices")
	if wh:
		return wh
	return [
		{"id": "manual_rate", "label": "Enter rate manually", "rate": None},
		{"id": "skip", "label": "Defer — missing external evidence", "rate": None},
	]


def _consequences(choices, impact) -> list[dict]:
	unlocked = int(impact.get("unlocked_estimate") or 0)
	out = []
	for c in choices or []:
		out.append(
			{
				"choice_id": c.get("id"),
				"consequence": c.get("consequence")
				or (
					f"May unlock ~{unlocked} downstream WAITING repairs"
					if c.get("id") != "skip"
					else "No automatic progress"
				),
			}
		)
	return out
