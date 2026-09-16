# Copyright (c) 2026, ERPNext Extensions contributors
"""Assisted Recovery engine — reclassify residuals into actionable outcomes."""

from __future__ import annotations

from erpnext_extensions.iran_accounting.historical_stock.util import resolve_company

from collections import Counter
from time import perf_counter

from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_EXACT,
	RATE_EPS,
	STATUS_RECONSTRUCTABLE,
)
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery import (
	ASSISTED_READY,
	BUCKET_ASSISTED,
	BUCKET_AUTO,
	BUCKET_NO_EVIDENCE,
	BUCKET_OPERATOR,
	BUCKET_REAL_SHORTAGE,
	DEFAULT_ASSISTED_THRESHOLD,
	NO_EVIDENCE,
	OPERATOR_DECISION,
	REAL_STOCK_SHORTAGE,
)
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.ambiguity import (
	resolve_rate_ambiguity,
	resolve_warehouse_transfer_cycle,
)
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.evidence import (
	build_evidence_package,
)
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.gl_assist import (
	assist_g2_missing,
)
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.impact_rank import (
	estimate_unlock_impact,
)
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.riv_assist import (
	refine_riv_root_cause,
)
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.shortage import (
	search_hidden_inbound,
)
from erpnext_extensions.iran_accounting.historical_stock.assisted_recovery.wizard import (
	build_decision_card,
)


def reclassify_row(
	row: dict,
	*,
	universe: list[dict] | None = None,
	cache: dict | None = None,
	threshold: float = DEFAULT_ASSISTED_THRESHOLD,
) -> dict:
	"""Convert one residual row into READY / ASSISTED_READY / OPERATOR / NO_EVIDENCE / SHORTAGE."""
	cache = cache if cache is not None else {}
	universe = universe or []
	topic = str(row.get("topic") or "")
	ps = str(row.get("planner_status") or row.get("status") or "")
	impact = estimate_unlock_impact(row, universe)

	# --- Failed RIV ---
	if topic == "FAILED_RIV" or row.get("riv_status"):
		riv = refine_riv_root_cause(row)
		bucket = BUCKET_AUTO if riv.get("safe_to_retry") else BUCKET_OPERATOR
		if riv.get("waiting_bucket") == "UNKNOWN":
			bucket = BUCKET_NO_EVIDENCE
		card = build_decision_card(row, impact=impact, resolution={"promote_to": riv["waiting_bucket"], "reason": riv["guidance"]})
		return _pack(row, riv["waiting_bucket"], bucket, riv["guidance"], impact, card, extra={"riv": riv})

	# --- GL ---
	if topic == "GL" or row.get("gl_class"):
		gclass = str(row.get("gl_class") or "")
		if gclass == "G2_MISSING":
			gl = assist_g2_missing(row.get("voucher"))
			outcome = gl.get("outcome") or OPERATOR_DECISION
			bucket = _outcome_bucket(outcome)
			card = build_decision_card(row, impact=impact, resolution={"promote_to": outcome, "reason": gl.get("reason"), "confidence": gl.get("confidence")})
			return _pack(row, outcome, bucket, gl.get("reason"), impact, card, extra={"gl_assist": gl})
		if gclass in ("G3_UNBALANCED", "G4_BUILT_FROM_POISONED_SLE"):
			return _pack(
				row,
				OPERATOR_DECISION,
				BUCKET_OPERATOR,
				"GL waiting on SLE health — not assisted-rebuildable yet",
				impact,
				build_decision_card(row, impact=impact),
			)

	# --- Warehouse / Posting Order shortage & ambiguity ---
	opt = str(row.get("optimizer_status") or row.get("status") or "")
	if opt == "REAL_STOCK_SHORTAGE" or "SHORTAGE" in ps or topic == "POSTING_ORDER" and "SHORTAGE" in opt:
		shortage = search_hidden_inbound(row, cache=cache)
		outcome = shortage.get("outcome") or REAL_STOCK_SHORTAGE
		if outcome == "UNIQUE_EXTERNAL_INBOUND" and shortage.get("safe_to_auto"):
			# Build a synthetic CROSS/MULTI proposal against the unique inbound.
			ready = _unique_inbound_ready_row(row, shortage)
			if ready:
				card = build_decision_card(
					row,
					impact=impact,
					shortage=shortage,
					resolution={
						"promote_to": ASSISTED_READY,
						"reason": shortage.get("reason"),
						"confidence": 0.97,
					},
				)
				return _pack(
					row,
					ASSISTED_READY,
					BUCKET_AUTO,
					shortage.get("reason"),
					impact,
					card,
					extra={"shortage": shortage},
					ready_row=ready,
				)
		bucket = BUCKET_REAL_SHORTAGE if outcome == REAL_STOCK_SHORTAGE else BUCKET_OPERATOR
		card = build_decision_card(row, impact=impact, shortage=shortage)
		return _pack(row, outcome, bucket, shortage.get("reason"), impact, card, extra={"shortage": shortage})

	if "WAREHOUSE_AMBIGUOUS" in ps or "transfer order cycle" in str(row.get("reason") or ""):
		wh = resolve_warehouse_transfer_cycle(row.get("item"), row.get("warehouse"), row.get("reason") or "")
		card = build_decision_card(row, impact=impact, resolution=wh)
		return _pack(row, OPERATOR_DECISION, BUCKET_OPERATOR, wh.get("reason"), impact, card, extra={"warehouse": wh})

	# --- Rate residuals (Wrong / Zero / MANUAL / AMBIGUOUS / WAITING) ---
	evidence = build_evidence_package(row, cache=cache)
	resolution = resolve_rate_ambiguity(row, evidence, threshold=threshold)
	promote = resolution.get("promote_to")

	# Unique READY candidate — stamp EXACT fields for downstream auto apply
	if promote == "READY" and abs(flt(resolution.get("expected"))) > RATE_EPS:
		exp = flt(resolution["expected"])
		obs = flt(row.get("observed") or row.get("current_rate") or row.get("rate"))
		if obs and abs(exp - obs) <= max(1.0, abs(exp) * 1e-9):
			# Valuation rate already matches — Wrong Rate flag is SVD/transfer residue.
			card = build_decision_card(
				row,
				evidence=evidence,
				resolution={
					**resolution,
					"promote_to": OPERATOR_DECISION,
					"reason": (
						f"Rate already matches expected {exp}; residual is SVD/transfer "
						f"incoming_rate=0 residue — needs selective value replay, not rate write"
					),
				},
				impact=impact,
			)
			return _pack(
				row,
				OPERATOR_DECISION,
				BUCKET_OPERATOR,
				"RATE_MATCHED_SVD_RESIDUE",
				impact,
				card,
				extra={"evidence": evidence, "resolution": resolution, "matched_rate": exp},
			)
		stamped = dict(row)
		stamped["confidence"] = CONFIDENCE_EXACT
		stamped["expected"] = resolution["expected"]
		stamped["proposed_rate"] = resolution["expected"]
		stamped["source"] = resolution.get("source")
		stamped["source_of_truth"] = resolution.get("source")
		stamped["status"] = STATUS_RECONSTRUCTABLE
		stamped["eligible"] = True
		stamped["assisted_promotion"] = "READY"
		# Clear foreign patient-zero so apply does not re-block; only when
		# evidence already proved the rate uniquely (external/healthy PZ).
		pz = stamped.get("patient_zero")
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		if pz_v and pz_v != stamped.get("voucher"):
			# Still blocked on an upstream voucher → ASSISTED, not AUTO
			resolution = {
				**resolution,
				"promote_to": ASSISTED_READY,
				"reason": f"Unique rate {resolution['expected']} but upstream patient-zero {pz_v} must be confirmed/repaired first",
				"confidence": max(flt(resolution.get("confidence")), 0.95),
			}
			card = build_decision_card(row, evidence=evidence, resolution=resolution, impact=impact)
			return _pack(
				row,
				ASSISTED_READY,
				BUCKET_ASSISTED,
				resolution.get("reason"),
				impact,
				card,
				extra={"evidence": _evidence_brief(evidence), "resolution": resolution},
			)
		# Re-evaluate through planner for true READY_* status
		from erpnext_extensions.iran_accounting.historical_stock.planner import evaluate_row, READY_STATUSES

		planned = evaluate_row(stamped, cache=cache)
		if planned.get("planner_status") in READY_STATUSES and int(planned.get("sql_updates") or 0) > 0:
			card = build_decision_card(stamped, evidence=evidence, resolution=resolution, impact=impact)
			return _pack(
				stamped,
				planned["planner_status"],
				BUCKET_AUTO,
				resolution.get("reason"),
				impact,
				card,
				extra={"evidence": _evidence_brief(evidence), "resolution": resolution, "planned": planned},
				ready_row=stamped,
			)
		# Planner still waiting — surface as assisted with evidence
		promote = ASSISTED_READY
		resolution = {
			**resolution,
			"promote_to": ASSISTED_READY,
			"reason": f"Unique rate found but planner blocked: {planned.get('reason')}",
			"confidence": max(flt(resolution.get("confidence")), 0.95),
		}

	if promote == ASSISTED_READY:
		card = build_decision_card(row, evidence=evidence, resolution=resolution, impact=impact)
		return _pack(
			row,
			ASSISTED_READY,
			BUCKET_ASSISTED,
			resolution.get("reason"),
			impact,
			card,
			extra={"evidence": _evidence_brief(evidence), "resolution": resolution},
		)

	# Z0 / allow_zero — operator business decision
	if "Z0" in str(row.get("status") or "") or str(row.get("source") or "") == "allow_zero_valuation_rate":
		card = build_decision_card(
			row,
			evidence=evidence,
			resolution={
				"promote_to": OPERATOR_DECISION,
				"reason": "Legitimate zero valuation flag — confirm whether FG should inherit zero or prior rate",
				"confidence": 0,
			},
			impact=impact,
		)
		return _pack(row, OPERATOR_DECISION, BUCKET_OPERATOR, card["why_automatic_repair_stopped"], impact, card)

	if not (evidence.get("rate_sources") or {}):
		card = build_decision_card(
			row,
			evidence=evidence,
			resolution={"promote_to": NO_EVIDENCE, "reason": "No reconstruction sources after full evidence sweep"},
			impact=impact,
		)
		return _pack(row, NO_EVIDENCE, BUCKET_NO_EVIDENCE, "No evidence", impact, card, extra={"evidence": _evidence_brief(evidence)})

	card = build_decision_card(row, evidence=evidence, resolution=resolution, impact=impact)
	return _pack(
		row,
		OPERATOR_DECISION,
		BUCKET_OPERATOR,
		resolution.get("reason") or "Operator decision required",
		impact,
		card,
		extra={"evidence": _evidence_brief(evidence), "resolution": resolution},
	)


def run_assisted_campaign(
	*,
	company: str | None = None,
	threshold: float = DEFAULT_ASSISTED_THRESHOLD,
	max_rate: int = 400,
	max_shortage: int = 80,
	max_gl: int = 60,
	max_riv: int = 200,
	include_warehouse: bool = True,
) -> dict:
	"""Scan residuals and reclassify; return AUTO-promoted READY rows + wizard cards."""
	company = resolve_company(company)
	t0 = perf_counter()
	from erpnext_extensions.iran_accounting.historical_stock.wrong_rate import scan_wrong_rates
	from erpnext_extensions.iran_accounting.historical_stock.zero_rate import scan_zero_rate_rows
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import scan_gl_integrity
	from erpnext_extensions.iran_accounting.historical_stock.failed_riv import scan_failed_riv
	from erpnext_extensions.iran_accounting.stock_posting_order.scanner import run_full_history_scan
	from erpnext_extensions.iran_accounting.historical_stock.planner import attach_plan

	universe: list[dict] = []
	residuals: list[dict] = []

	wr = scan_wrong_rates(company=company, limit=max(3000, max_rate * 5))
	for r in wr.get("rows") or []:
		r = dict(r)
		r.setdefault("topic", "WRONG_RATE")
		universe.append(r)
		ps = str(r.get("planner_status") or "")
		if ps in ("RATE_AMBIGUOUS", "RATE_MANUAL", "AMBIGUOUS", "MANUAL", "WAITING_PATIENT_ZERO") or (
			"WAITING" in ps and r.get("confidence") in ("AMBIGUOUS", "LIKELY", "EXACT", "MANUAL")
		):
			residuals.append(r)

	zr = scan_zero_rate_rows(company=company, limit=max(500, max_rate))
	for r in zr.get("rows") or []:
		r = dict(r)
		r.setdefault("topic", "ZERO_RATE")
		universe.append(r)
		ps = str(r.get("planner_status") or "")
		if ps in ("AMBIGUOUS", "MANUAL") or "WAITING" in ps:
			residuals.append(r)

	gl = scan_gl_integrity(company=company, limit=max_gl)
	for r in gl.get("rows") or []:
		r = dict(r)
		r.setdefault("topic", "GL")
		universe.append(r)
		if r.get("gl_class") in ("G2_MISSING", "G3_UNBALANCED", "G4_BUILT_FROM_POISONED_SLE"):
			residuals.append(r)

	riv = scan_failed_riv(company=company, limit=max_riv)
	for r in riv.get("rows") or []:
		r = dict(r)
		r.setdefault("topic", "FAILED_RIV")
		universe.append(r)
		residuals.append(r)

	po = run_full_history_scan(company=company)
	for r in (po.get("rows") or [])[: max_shortage * 3]:
		r = dict(r)
		r.setdefault("topic", "POSTING_ORDER")
		planned = attach_plan(r)
		universe.append(planned)
		opt = str(planned.get("optimizer_status") or planned.get("status") or "")
		if opt == "REAL_STOCK_SHORTAGE" or "SHORTAGE" in opt:
			residuals.append(planned)

	if include_warehouse:
		try:
			from erpnext_extensions.iran_accounting.historical_stock.warehouse_engine.optimizer import (
				discover_warehouse_campaigns,
			)

			disc = discover_warehouse_campaigns(company=company)
			for c in disc.get("campaigns") or []:
				if str(c.get("planner_status") or c.get("global_planner_status") or "") in (
					"WAREHOUSE_AMBIGUOUS",
				) or "AMBIGUITY" in str(c.get("reason") or ""):
					residuals.append(
						{
							"topic": "WAREHOUSE",
							"item": c.get("item"),
							"warehouse": c.get("warehouse"),
							"planner_status": c.get("planner_status") or "WAREHOUSE_AMBIGUOUS",
							"reason": c.get("reason"),
							"voucher": None,
						}
					)
		except Exception:
			pass

	# Deduplicate by (topic, voucher, item)
	seen = set()
	unique = []
	for r in residuals:
		key = (r.get("topic"), r.get("voucher"), r.get("item"), r.get("warehouse"))
		if key in seen:
			continue
		seen.add(key)
		unique.append(r)

	cache = {"rows_by_voucher": {r.get("voucher"): r for r in universe if r.get("voucher")}}
	results = []
	ready_rows = []
	for r in unique[: max_rate + max_shortage + max_gl + max_riv]:
		out = reclassify_row(r, universe=universe, cache=cache, threshold=threshold)
		results.append(out)
		if out.get("bucket") == BUCKET_AUTO and out.get("ready_row"):
			ready_rows.append(out["ready_row"])

	by_bucket = Counter(r.get("bucket") for r in results)
	by_outcome = Counter(r.get("assisted_status") for r in results)
	return {
		"company": company,
		"threshold": threshold,
		"elapsed_seconds": round(perf_counter() - t0, 2),
		"n_universe": len(universe),
		"n_residuals": len(unique),
		"n_reclassified": len(results),
		"by_bucket": dict(by_bucket),
		"by_outcome": dict(by_outcome),
		"n_auto_ready": len(ready_rows),
		"ready_rows": ready_rows[:120],
		"assisted_cards": [r.get("decision_card") for r in results if r.get("bucket") == BUCKET_ASSISTED][:40],
		"operator_cards": [r.get("decision_card") for r in results if r.get("bucket") == BUCKET_OPERATOR][:40],
		"shortage_proven": [r for r in results if r.get("bucket") == BUCKET_REAL_SHORTAGE][:20],
		"no_evidence": [r for r in results if r.get("bucket") == BUCKET_NO_EVIDENCE][:20],
		"results": results,
		"universe": universe,
	}


def _outcome_bucket(outcome: str) -> str:
	if outcome in ("READY", "READY_WRONG_RATE", "READY_I4") or str(outcome).startswith("READY"):
		return BUCKET_AUTO
	if outcome == ASSISTED_READY:
		return BUCKET_ASSISTED
	if outcome == REAL_STOCK_SHORTAGE:
		return BUCKET_REAL_SHORTAGE
	if outcome == NO_EVIDENCE:
		return BUCKET_NO_EVIDENCE
	return BUCKET_OPERATOR


def _evidence_brief(evidence: dict) -> dict:
	return {
		"dependency_chain": evidence.get("dependency_chain"),
		"rate_sources": evidence.get("rate_sources"),
		"previous_sle": evidence.get("previous_sle"),
		"n_pr": len(evidence.get("purchase_receipts") or []),
		"n_reco": len(evidence.get("stock_reconciliations") or []),
		"gl": evidence.get("gl"),
	}


def _unique_inbound_ready_row(row: dict, shortage: dict) -> dict | None:
	"""Turn a UNIQUE_EXTERNAL_INBOUND shortage into an applyable Posting Order row."""
	import frappe
	from erpnext_extensions.iran_accounting.stock_posting_order.multi_move import (
		propose_multi_move_after_inbound,
	)
	from erpnext_extensions.iran_accounting.stock_posting_order.negative_interval import (
		propose_outbound_after_inbound,
	)
	from erpnext_extensions.iran_accounting.stock_posting_order.ordering import format_datetime
	from frappe.utils import get_datetime

	uniq = shortage.get("unique_inbound") or {}
	in_v = uniq.get("voucher")
	out_v = row.get("outbound_document") or row.get("voucher")
	item = row.get("item")
	wh = row.get("warehouse")
	if not (in_v and out_v and item and wh):
		return None
	series = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, batch_no, voucher_type, voucher_no, actual_qty,
		       posting_datetime, posting_date, creation, modified, company,
		       serial_and_batch_bundle
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation
		""",
		(item, wh),
		as_dict=True,
	)
	outbound = next((r for r in series if r.voucher_no == out_v and flt(r.actual_qty) < 0), None)
	inbound = next((r for r in series if r.voucher_no == in_v and flt(r.actual_qty) > 0), None)
	if not outbound or not inbound:
		return None
	interval = {"outbound": outbound, "inbound": inbound, "recovered": True}
	proposal = propose_outbound_after_inbound(interval, series)
	if not proposal.get("ok"):
		proposal = propose_multi_move_after_inbound(interval, series)
	if not proposal.get("ok"):
		return None
	ready = dict(row)
	ready.update(
		{
			"topic": "POSTING_ORDER",
			"inbound_document": in_v,
			"outbound_document": out_v,
			"optimizer_status": proposal.get("status") or "CROSS_TIME_REPAIRABLE",
			"confidence": "LIKELY",
			"eligible": True,
			"planner_status": "READY_BATCH_SCOPED_REPAIR",
			"moves": proposal.get("moves") or [],
			"proposed_outbound_time": format_datetime(proposal["proposed_outbound"]),
			"proposed_inbound_time": format_datetime(proposal["proposed_inbound"]),
			"current_outbound_time": format_datetime(get_datetime(outbound.posting_datetime)),
			"current_inbound_time": format_datetime(get_datetime(inbound.posting_datetime)),
			"min_qty_before": str(proposal["current"]["min_qty"]),
			"min_qty_after": str(proposal["proposed"]["min_qty"]),
			"final_qty_before": str(proposal["current"]["final_qty"]),
			"final_qty_after": str(proposal["proposed"]["final_qty"]),
			"valuation_impact": "QUANTITY-ONLY",
			"sql_updates": max(1, len(proposal.get("moves") or [])),
			"assisted_promotion": "UNIQUE_EXTERNAL_INBOUND",
		}
	)
	return ready


def _pack(row, status, bucket, reason, impact, card, extra=None, ready_row=None) -> dict:
	out = {
		"voucher": row.get("voucher"),
		"item": row.get("item") or row.get("item_code"),
		"warehouse": row.get("warehouse"),
		"topic": row.get("topic"),
		"assisted_status": status,
		"bucket": bucket,
		"reason": reason,
		"impact": impact,
		"decision_card": card,
	}
	if extra:
		out.update(extra)
	if ready_row is not None:
		out["ready_row"] = ready_row
	return out
