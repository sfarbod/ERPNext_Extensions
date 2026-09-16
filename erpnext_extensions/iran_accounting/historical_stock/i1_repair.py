# Copyright (c) 2026, ERPNext Extensions contributors
"""I1 negative incoming rate repair — Manufacture secondary inbound priced from the same document.

Guard invariant I1 (``riv_valuation_guard.assert_incoming_rate_not_negative``) refuses an
incoming movement carrying a negative ``incoming_rate``. On a Manufacture Stock Entry the
finished good is the residual of the value pool::

    fg_amount = outgoing_pool + capitalized_cost - other_incoming

When a secondary inbound row (material returned to store / by-product) is priced from a
poisoned warehouse valuation, ``other_incoming`` explodes and the FG residual is pushed
negative. Repairing the FG rate directly would hide the cause; the wrong number is the
secondary inbound rate, not the FG.

EXACT source of truth, single-document only: when the secondary inbound item is also
**issued** in the same Stock Entry, the issue rate of that document is the rate the returned
material must carry. Nothing outside the voucher is consulted. When the secondary item is not
issued in the document, the rate can only come from warehouse valuation, which is exactly what
is poisoned — those rows stay WAITING for upstream Zero/Wrong Rate repair.

Never: abs() or clamp a negative rate, price a secondary inbound from live Bin, repair the FG
rate without correcting its cause, or write when the corrected pool is still negative.
"""

from __future__ import annotations

from collections import defaultdict
from time import perf_counter

import frappe
from frappe.utils import flt, get_datetime

from erpnext_extensions.iran_accounting.historical_stock import (
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	HISTORICAL_REPAIR_FLAG,
	I1_MANUAL,
	I1_NEGATIVE_RATE_REPAIR,
	I1_READY,
	I1_REPAIRED,
	I1_WAITING,
	QTY_EPS,
	RATE_EPS,
	STATUS_BLOCKED,
	STATUS_REPAIRED,
	TOPIC_I1,
	VALUE_EPS,
)
from erpnext_extensions.iran_accounting.historical_stock.audit import append_entry, finish_run, start_run
from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
	_fetch_previous,
	_fetch_sles,
	sle_poison_reason,
)

# Poisons that make ``replay_from_patient_zero`` refuse to rewrite an identity.
HARD_REPLAY_POISONS = (
	"negative_incoming_rate",
	"sign_inverted_incoming_svd",
	"exploded_rate",
)

SE_ROW_FIELDS = (
	"name",
	"idx",
	"item_code",
	"qty",
	"transfer_qty",
	"basic_rate",
	"basic_amount",
	"valuation_rate",
	"amount",
	"additional_cost",
	"landed_cost_voucher_amount",
	"s_warehouse",
	"t_warehouse",
	"is_finished_item",
	"secondary_item_type",
	"serial_and_batch_bundle",
	"batch_no",
)


def _row_qty(row) -> float:
	value = row.get("transfer_qty")
	if value in (None, ""):
		value = row.get("qty")
	return flt(value)


def _is_outgoing(row) -> bool:
	return bool(row.get("s_warehouse")) and not row.get("t_warehouse")


def _is_incoming(row) -> bool:
	return bool(row.get("t_warehouse")) and not row.get("s_warehouse")


def _is_fg(row) -> bool:
	return bool(row.get("is_finished_item")) and bool(row.get("t_warehouse"))


def _fetch_se_rows(voucher_no: str) -> list[dict]:
	return frappe.db.sql(
		f"""
		SELECT {", ".join(SE_ROW_FIELDS)}
		FROM `tabStock Entry Detail`
		WHERE parent=%s
		ORDER BY idx
		""",
		voucher_no,
		as_dict=True,
	)


def document_issue_rates(rows: list[dict]) -> dict:
	"""Weighted issue rate per item from this document's outgoing rows only."""
	pool: dict = defaultdict(lambda: [0.0, 0.0])
	for row in rows or []:
		if not _is_outgoing(row):
			continue
		qty = _row_qty(row)
		if qty <= QTY_EPS:
			continue
		bucket = pool[row["item_code"]]
		bucket[0] += qty
		bucket[1] += flt(row.get("amount"))
	return {item: (v[1] / v[0]) for item, v in pool.items() if v[0] > QTY_EPS}


def classify_i1_voucher(voucher_no: str) -> dict:
	"""Classify one Manufacture voucher against the I1 pool contract."""
	header = frappe.db.get_value(
		"Stock Entry",
		voucher_no,
		["name", "purpose", "company", "posting_date", "posting_time", "work_order", "docstatus"],
		as_dict=True,
	)
	base = {
		"topic": TOPIC_I1,
		"repair_class": I1_NEGATIVE_RATE_REPAIR,
		"voucher": voucher_no,
		"eligible": False,
	}
	if not header:
		return {**base, "status": "NOT_I1", "i1_status": "NOT_I1", "message": "Stock Entry not found"}
	if header.purpose != "Manufacture":
		return {
			**base,
			"status": "NOT_I1",
			"i1_status": "NOT_I1",
			"message": f"purpose={header.purpose} is not Manufacture",
		}

	rows = _fetch_se_rows(voucher_no)
	outgoing = sum(flt(r.get("amount")) for r in rows if _is_outgoing(r))
	capitalized = sum(
		flt(r.get("additional_cost")) + flt(r.get("landed_cost_voucher_amount"))
		for r in rows
		if r.get("t_warehouse")
	)
	fg_rows = [r for r in rows if _is_fg(r)]
	secondary = [r for r in rows if _is_incoming(r) and not _is_fg(r) and _row_qty(r) > QTY_EPS]
	issue_rates = document_issue_rates(rows)

	negative_incoming = [
		r for r in rows if _is_incoming(r) and (flt(r.get("amount")) < 0 or flt(r.get("basic_rate")) < 0)
	]
	common = {
		**base,
		"company": header.company,
		"work_order": header.work_order,
		"posting_date": str(header.posting_date) if header.posting_date else None,
		"posting_datetime": f"{header.posting_date or ''} {header.posting_time or ''}".strip(),
		"outgoing_pool": outgoing,
		"capitalized_cost": capitalized,
		"source_of_truth": "same_document_issue_rate",
		"patient_zero": {"voucher_no": voucher_no, "reason": "negative_incoming_rate"},
	}
	if not negative_incoming:
		return {
			**common,
			"status": "NOT_I1",
			"i1_status": "NOT_I1",
			"message": "No negative incoming valuation on this voucher",
		}
	if len(fg_rows) != 1:
		return {
			**common,
			"status": I1_MANUAL,
			"i1_status": I1_MANUAL,
			"confidence": CONFIDENCE_LIKELY,
			"message": f"MANUAL — {len(fg_rows)} finished-good rows; single-FG pool contract does not apply",
		}

	fg = fg_rows[0]
	fg_qty = _row_qty(fg)
	if fg_qty <= QTY_EPS:
		return {
			**common,
			"status": I1_MANUAL,
			"i1_status": I1_MANUAL,
			"confidence": CONFIDENCE_LIKELY,
			"message": f"MANUAL — finished-good qty {fg_qty} is not positive",
		}

	proposed_secondary = []
	corrected_total = 0.0
	for row in secondary:
		qty = _row_qty(row)
		rate = issue_rates.get(row["item_code"])
		if rate is None:
			return {
				**common,
				"status": I1_WAITING,
				"i1_status": I1_WAITING,
				"confidence": CONFIDENCE_LIKELY,
				"blocked_item": row["item_code"],
				"message": (
					f"WAITING_I1 — secondary inbound {row['item_code']} is not issued in this document; "
					"its rate can only come from warehouse valuation. Repair Zero/Wrong Rate upstream first."
				),
			}
		if rate <= RATE_EPS:
			return {
				**common,
				"status": I1_WAITING,
				"i1_status": I1_WAITING,
				"confidence": CONFIDENCE_LIKELY,
				"blocked_item": row["item_code"],
				"message": (
					f"WAITING_I1 — issue rate for {row['item_code']} is zero in this document; "
					"repair Zero Rate on the issue row first."
				),
			}
		amount = qty * rate
		corrected_total += amount
		proposed_secondary.append(
			{
				"voucher_detail": row["name"],
				"idx": row["idx"],
				"item": row["item_code"],
				"warehouse": row.get("t_warehouse"),
				"qty": qty,
				"current_rate": flt(row.get("basic_rate")),
				"current_amount": flt(row.get("amount")),
				"proposed_rate": rate,
				"proposed_amount": amount,
				"serial_and_batch_bundle": row.get("serial_and_batch_bundle"),
				"batch": row.get("batch_no"),
			}
		)

	corrected_fg_amount = outgoing + capitalized - corrected_total
	if corrected_fg_amount <= VALUE_EPS:
		return {
			**common,
			"status": I1_MANUAL,
			"i1_status": I1_MANUAL,
			"confidence": CONFIDENCE_LIKELY,
			"corrected_fg_amount": corrected_fg_amount,
			"message": (
				f"MANUAL — corrected pool {corrected_fg_amount} is not positive; "
				"outgoing consumption is understated. Repair the issue rates first."
			),
		}

	# Readiness needs a replayable chain, not just a solvable pool. The apply must rewrite
	# running balances on every touched identity; when one is poisoned by other vouchers the
	# replay refuses, and writing only the Stock Entry would leave SE and SLE disagreeing.
	identities = [(fg["item_code"], fg.get("t_warehouse"))] + [
		(r["item"], r["warehouse"]) for r in proposed_secondary
	]
	replay_blockers = []
	for item_code, warehouse in identities:
		replay_blockers.extend(
			f"{item_code}: {reason}"
			for reason in _replay_blockers(item_code, warehouse, common["posting_datetime"])
		)
	if replay_blockers:
		return {
			**common,
			"status": I1_WAITING,
			"i1_status": I1_WAITING,
			"confidence": CONFIDENCE_LIKELY,
			"replay_blockers": replay_blockers,
			"message": (
				"WAITING_I1 — the pool is solvable but identity replay is blocked by upstream "
				f"poison ({replay_blockers[0]}). Repair that chain first; a Stock-Entry-only "
				"write would leave SE and SLE disagreeing."
			),
		}

	proposed_fg_rate = corrected_fg_amount / fg_qty
	sql_updates = len(proposed_secondary) + 1  # secondary rows + FG row
	sql_updates += 1  # Stock Entry header totals
	return {
		**common,
		"status": I1_READY,
		"i1_status": I1_READY,
		"confidence": CONFIDENCE_EXACT,
		"eligible": True,
		"item": fg["item_code"],
		"warehouse": fg.get("t_warehouse"),
		"batch": fg.get("batch_no"),
		"serial_and_batch_bundle": fg.get("serial_and_batch_bundle"),
		"fg_voucher_detail": fg["name"],
		"fg_qty": fg_qty,
		"current_rate": flt(fg.get("basic_rate")),
		"current_amount": flt(fg.get("amount")),
		"proposed_rate": proposed_fg_rate,
		"proposed_amount": corrected_fg_amount,
		"corrected_fg_amount": corrected_fg_amount,
		"secondary_rows": proposed_secondary,
		"secondary_current_total": sum(flt(r.get("amount")) for r in secondary),
		"secondary_corrected_total": corrected_total,
		"sql_updates": sql_updates,
		"sql_updates_estimate": sql_updates,
		"replay_count": len(proposed_secondary) + 1,
		"message": (
			f"READY_I1 — {len(proposed_secondary)} secondary inbound row(s) repriced from this "
			f"document's issue rate; finished good {fg['item_code']} restored to {proposed_fg_rate:.2f}."
		),
	}


def scan_i1_negative_rate(
	company=None,
	voucher=None,
	item_code=None,
	warehouse=None,
	work_order=None,
	from_date=None,
	to_date=None,
	limit=2000,
) -> dict:
	"""Scan live SLEs violating I1 (incoming qty with negative incoming rate)."""
	conds = ["sle.is_cancelled=0", "sle.actual_qty > %s", "sle.incoming_rate < 0", "sle.voucher_type='Stock Entry'"]
	args: list = [QTY_EPS]
	if company:
		conds.append("sle.company=%s")
		args.append(company)
	if voucher:
		conds.append("sle.voucher_no=%s")
		args.append(voucher)
	if item_code:
		conds.append("sle.item_code=%s")
		args.append(item_code)
	if warehouse:
		conds.append("sle.warehouse=%s")
		args.append(warehouse)
	if from_date:
		conds.append("sle.posting_date>=%s")
		args.append(from_date)
	if to_date:
		conds.append("sle.posting_date<=%s")
		args.append(to_date)
	join = ""
	if work_order:
		join = " JOIN `tabStock Entry` se ON se.name = sle.voucher_no "
		conds.append("se.work_order=%s")
		args.append(work_order)
	vouchers = frappe.db.sql(
		f"""
		SELECT DISTINCT sle.voucher_no
		FROM `tabStock Ledger Entry` sle
		{join}
		WHERE {" AND ".join(conds)}
		ORDER BY sle.posting_date, sle.creation
		LIMIT {int(limit)}
		""",
		args,
		pluck=True,
	)
	rows = []
	for name in vouchers:
		row = classify_i1_voucher(name)
		if row.get("i1_status") != "NOT_I1":
			rows.append(row)
	from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

	return stamp_scan_result(
		{"count": len(rows), "rows": rows, "scanned": len(vouchers), "by_status": _count(rows, "i1_status")}
	)


def dry_run_i1_repair(rows: list[dict] | None = None) -> dict:
	"""Preview the exact writes for each selected I1 voucher. Never writes."""
	out = []
	for raw in rows or []:
		voucher = raw.get("voucher") or raw.get("voucher_no")
		classified = classify_i1_voucher(voucher)
		gl_n = _gl_voucher_count(voucher)
		out.append(
			{
				**classified,
				"dry_run": True,
				"affected_se_rows": len(classified.get("secondary_rows") or []) + 1,
				"affected_sle": classified.get("replay_count") or 0,
				"affected_bin": 1 if classified.get("eligible") else 0,
				"affected_gl": gl_n,
				"affected_riv": _waiting_riv_count(voucher),
				"estimated_runtime_seconds": max(0.05, (classified.get("replay_count") or 0) * 0.05),
				"expected_result": {
					"incoming_rate": "positive on every inbound row",
					"fg_rate": classified.get("proposed_rate"),
					"message": "Pool contract restored; guard I1/I5 pass; blocked RIV may be retried.",
				},
				"preview_chain": [
					f"Secondary inbound {classified.get('secondary_current_total')}",
					"↓",
					f"Repriced from document issue rate {classified.get('secondary_corrected_total')}",
					"↓",
					f"Finished good {classified.get('current_amount')} → {classified.get('corrected_fg_amount')}",
					"↓",
					"Stock Entry header totals",
					"↓",
					"SLE incoming_rate + SVD",
					"↓",
					"SABB avg_rate",
					"↓",
					f"Selective GL ({gl_n} voucher)",
					"↓",
					"DATABASE BACKUP REQUIRED",
				],
			}
		)
	from erpnext_extensions.iran_accounting.historical_stock.planner import stamp_scan_result

	return stamp_scan_result({"dry_run": True, "count": len(out), "rows": out})


def repair_i1_selected(rows: list[dict], *, dry_run=True) -> dict:
	"""Apply READY_I1 vouchers: reprice secondary inbound, restore FG residual, replay identity."""
	applied = []
	blocked = []
	log = start_run("I1_NEGATIVE_RATE", dry_run=dry_run)
	frappe.flags[HISTORICAL_REPAIR_FLAG] = True
	savepoint = None
	t0 = perf_counter()
	try:
		prepared = []
		for raw in rows or []:
			voucher = raw.get("voucher") or raw.get("voucher_no")
			classified = classify_i1_voucher(voucher)
			merged = {**classified, **{k: v for k, v in (raw or {}).items() if v not in (None, "")}}
			merged["topic"] = TOPIC_I1
			merged["repair_class"] = I1_NEGATIVE_RATE_REPAIR
			from erpnext_extensions.iran_accounting.historical_stock.planner import assert_ready

			try:
				assert_ready(merged)
				prepared.append(merged)
			except Exception as exc:
				blocked.append({"row": raw, "error": str(exc), "status": STATUS_BLOCKED})
		if blocked and not dry_run:
			finish_run(log, applied=0, blocked=len(blocked), error="aborted: no partial commits")
			return {
				"dry_run": False,
				"aborted": True,
				"reason": blocked[0].get("error"),
				"sql_updates_executed": 0,
				"applied": [],
				"blocked": blocked,
				"repair_run_id": getattr(log, "repair_run_id", None),
			}
		if dry_run:
			preview = dry_run_i1_repair(prepared)
			finish_run(log, applied=0, blocked=0)
			return {**preview, "blocked": blocked, "repair_run_id": getattr(log, "repair_run_id", None)}

		savepoint = f"i1_{frappe.generate_hash(length=8)}"
		frappe.db.savepoint(savepoint)
		sql_executed = 0
		for merged in prepared:
			# Capture before-images first so rollback_run can restore this voucher.
			from erpnext_extensions.iran_accounting.historical_stock.snapshot import (
				capture_identity_snapshot,
			)

			snapshot = capture_identity_snapshot(merged["voucher"])
			result = apply_i1_voucher(merged["voucher"])
			sql_executed += int(result.get("sql_updates") or 0)
			row_out = {
				**merged,
				**result,
				"snapshot_before": snapshot,
				"full_rollback_possible": snapshot.get("full_rollback_possible"),
				"written": True,
				"status": STATUS_REPAIRED,
				"i1_status": I1_REPAIRED,
			}
			applied.append(row_out)
			append_entry(log, row_out, written=True)
		frappe.db.commit()
		finish_run(log, applied=len(applied), blocked=0)
		return {
			"dry_run": False,
			"aborted": False,
			"applied": applied,
			"blocked": blocked,
			"sql_updates_executed": sql_executed,
			"savepoint_created": True,
			"transaction_committed": True,
			"elapsed_seconds": round(perf_counter() - t0, 3),
			"database_backup_recommended": True,
			"repair_run_id": getattr(log, "repair_run_id", None),
			"riv": "NOT_INVOKED",
			"global_replay": False,
		}
	except Exception as exc:
		if savepoint:
			frappe.db.rollback(save_point=savepoint)
		finish_run(log, applied=0, blocked=1, error=str(exc))
		raise
	finally:
		frappe.flags[HISTORICAL_REPAIR_FLAG] = False


def _replay_blockers(item_code, warehouse, from_dt) -> list[str]:
	"""``replay_from_patient_zero``'s own refusal predicate, evaluated before any write.

	That function scans the identity and returns ``ok=False`` without writing when it meets a
	hard poison. Calling it and ignoring the flag is how a Stock-Entry-only half-repair gets
	committed, so readiness is decided here instead.
	"""
	if not item_code or not warehouse or not from_dt:
		return ["missing identity or posting datetime"]
	try:
		from_dt = get_datetime(from_dt)
	except Exception:
		return ["unparsable posting datetime"]
	blockers: list[str] = []
	previous = _fetch_previous(item_code, warehouse, from_dt)
	if previous:
		reason = sle_poison_reason(previous)
		if (
			reason
			and reason != "qty_after_zero_nonzero_value"
			and abs(flt(previous.qty_after_transaction)) > QTY_EPS
		):
			blockers.append(f"opening SLE {previous.voucher_no} is {reason}")
	for row in _fetch_sles(item_code, warehouse, from_dt, before=False):
		reason = sle_poison_reason(row)
		if reason in HARD_REPLAY_POISONS:
			blockers.append(f"{row.voucher_no} is {reason}")
			break
	return blockers


def _write_inbound_sle_from_se(voucher_no: str) -> int:
	"""Carry corrected Stock Entry amounts onto the inbound SLE legs.

	Matched 1:1 through ``voucher_detail_no``, so the outgoing leg of an item that is both
	issued and returned in the same voucher is never touched. ``valuation_rate`` is left to
	the replay: it is the warehouse moving average after the movement, not this row's rate.

	``write_sle_transaction_rates`` cannot be used here — it derives the rate from the SLE's
	existing ``stock_value_difference``, so it can never carry a corrected Stock Entry amount.
	"""
	rows = frappe.db.sql(
		"""
		SELECT sle.name, sle.actual_qty, sed.amount
		FROM `tabStock Ledger Entry` sle
		JOIN `tabStock Entry Detail` sed ON sed.name = sle.voucher_detail_no
		WHERE sle.voucher_type='Stock Entry' AND sle.voucher_no=%s
		  AND sle.is_cancelled=0 AND sle.actual_qty > %s
		""",
		(voucher_no, QTY_EPS),
		as_dict=True,
	)
	written = 0
	for row in rows:
		qty = flt(row.actual_qty)
		amount = flt(row.amount)
		frappe.db.set_value(
			"Stock Ledger Entry",
			row.name,
			{
				"incoming_rate": amount / qty,
				"outgoing_rate": 0,
				"stock_value_difference": amount,
			},
			update_modified=False,
		)
		written += 1
	return written


def apply_i1_voucher(voucher_no: str) -> dict:
	"""Write the corrected pool, rewrite the SLE legs, replay, then rebuild GL.

	Fail-closed at every step: refuses before any write when an identity cannot replay, and
	raises (rolling back the caller's savepoint) when the replay, the GL rebuild, or the
	post-write verification does not hold.
	"""
	plan = classify_i1_voucher(voucher_no)
	if plan.get("i1_status") != I1_READY:
		raise frappe.ValidationError(
			f"Cannot apply I1 on {voucher_no}: status={plan.get('i1_status')} — {plan.get('message')}"
		)

	# Re-check immediately before writing: the scan that produced this row may be stale.
	preflight = []
	for item_code, warehouse in [(plan["item"], plan["warehouse"])] + [
		(r["item"], r["warehouse"]) for r in plan.get("secondary_rows") or []
	]:
		preflight.extend(
			f"{item_code}: {reason}"
			for reason in _replay_blockers(item_code, warehouse, plan.get("posting_datetime"))
		)
	if preflight:
		raise frappe.ValidationError(
			f"Cannot apply I1 on {voucher_no}: identity replay is blocked — {preflight[0]}"
		)

	touched_identities = []
	sql_updates = 0
	for row in plan.get("secondary_rows") or []:
		frappe.db.set_value(
			"Stock Entry Detail",
			row["voucher_detail"],
			{
				"basic_rate": flt(row["proposed_rate"]),
				"valuation_rate": flt(row["proposed_rate"]),
				"basic_amount": flt(row["proposed_amount"]),
				"amount": flt(row["proposed_amount"]),
			},
			update_modified=False,
		)
		sql_updates += 1
		touched_identities.append((row["item"], row["warehouse"]))

	frappe.db.set_value(
		"Stock Entry Detail",
		plan["fg_voucher_detail"],
		{
			"basic_rate": flt(plan["proposed_rate"]),
			"valuation_rate": flt(plan["proposed_rate"]),
			"basic_amount": flt(plan["proposed_amount"]),
			"amount": flt(plan["proposed_amount"]),
		},
		update_modified=False,
	)
	sql_updates += 1
	touched_identities.append((plan["item"], plan["warehouse"]))

	sql_updates += _refresh_header_totals(voucher_no)

	# Carry the corrected amounts onto the inbound SLE legs, then re-derive SABB from them.
	sql_updates += _write_inbound_sle_from_se(voucher_no)

	from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import sync_sabb_from_sle

	for item_code, _warehouse in touched_identities:
		sync_sabb_from_sle(voucher_no, item_code)
		sql_updates += 1

	# Forward identity replay from this voucher only — never global. Honour the ok flag:
	# a refusal here means the running balances were NOT rewritten, so the write must abort.
	from erpnext_extensions.iran_accounting.historical_stock.replay import replay_from_patient_zero

	replays = []
	for item_code, warehouse in touched_identities:
		if not item_code or not warehouse:
			continue
		result = replay_from_patient_zero(
			item_code, warehouse, None, from_dt=plan.get("posting_datetime")
		)
		if not result.get("ok"):
			raise frappe.ValidationError(
				f"I1 replay refused on {item_code} @ {warehouse}: "
				f"{result.get('status')} {result.get('reason') or ''} "
				f"{result.get('voucher') or ''}".strip()
			)
		replays.append({"item": item_code, "warehouse": warehouse, "replay": result})
		sql_updates += int(result.get("rows") or 0)

	# Selective GL last: ERPNext builds the Stock Entry GL map from SLE stock_value_difference,
	# so rebuilding before the SLE legs are correct would post the poisoned magnitude.
	from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import rebuild_gl_for_voucher

	gl = rebuild_gl_for_voucher(voucher_no, dry_run=False)
	if not gl.get("written"):
		raise frappe.ValidationError(
			f"I1 GL rebuild refused on {voucher_no}: {gl.get('reason') or gl.get('gl_class')}"
		)
	sql_updates += 1

	verify = _verify_repair(voucher_no, touched_identities)
	if not verify["ok"]:
		raise frappe.ValidationError(
			f"I1 apply failed post-write check on {voucher_no}: {verify['message']}"
		)
	return {
		"ok": True,
		"status": "I1_REPAIRED",
		"voucher": voucher_no,
		"sql_updates": sql_updates,
		"touched_vouchers": [voucher_no],
		"touched_identities": [{"item": i, "warehouse": w} for i, w in touched_identities],
		"replays": replays,
		"gl": gl,
		"verification": verify,
		"fg_rate": flt(plan["proposed_rate"]),
		"fg_amount": flt(plan["proposed_amount"]),
		"negative_incoming_cleared": True,
	}


def _refresh_header_totals(voucher_no: str) -> int:
	totals = frappe.db.sql(
		"""
		SELECT
		  SUM(CASE WHEN t_warehouse IS NOT NULL AND t_warehouse != '' THEN amount ELSE 0 END) incoming,
		  SUM(CASE WHEN s_warehouse IS NOT NULL AND s_warehouse != '' THEN amount ELSE 0 END) outgoing
		FROM `tabStock Entry Detail` WHERE parent=%s
		""",
		voucher_no,
		as_dict=True,
	)[0]
	frappe.db.set_value(
		"Stock Entry",
		voucher_no,
		{
			"total_incoming_value": flt(totals.incoming),
			"total_outgoing_value": flt(totals.outgoing),
			"value_difference": flt(totals.incoming) - flt(totals.outgoing),
		},
		update_modified=False,
	)
	return 1


def _verify_repair(voucher_no: str, identities) -> dict:
	"""Post-write gate across every layer this repair touched.

	Checks ``valuation_rate`` as well as ``incoming_rate``: an inbound leg can carry a positive
	incoming rate while its moving-average valuation is still negative, which is exactly the
	state that made an earlier incoming-rate-only check report a false pass.
	"""
	for sle in frappe.db.sql(
		"""
		SELECT name, item_code, actual_qty, incoming_rate, valuation_rate, stock_value_difference
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND is_cancelled=0 AND actual_qty > %s
		""",
		(voucher_no, QTY_EPS),
		as_dict=True,
	):
		if flt(sle.incoming_rate) < 0:
			return {"ok": False, "message": f"{sle.name} ({sle.item_code}) incoming_rate is still negative"}
		if flt(sle.valuation_rate) < 0:
			return {
				"ok": False,
				"message": (
					f"{sle.name} ({sle.item_code}) valuation_rate is still negative "
					f"({flt(sle.valuation_rate)})"
				),
			}
		if flt(sle.stock_value_difference) < -VALUE_EPS:
			return {"ok": False, "message": f"{sle.name} ({sle.item_code}) inbound SVD is negative"}

	for item_code, warehouse in identities or []:
		if not item_code or not warehouse:
			continue
		last = frappe.db.sql(
			"""
			SELECT qty_after_transaction, stock_value FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			ORDER BY posting_datetime DESC, creation DESC LIMIT 1
			""",
			(item_code, warehouse),
			as_dict=True,
		)
		binrow = frappe.db.sql(
			"SELECT actual_qty, stock_value FROM `tabBin` WHERE item_code=%s AND warehouse=%s",
			(item_code, warehouse),
			as_dict=True,
		)
		if not last or not binrow:
			continue
		if abs(flt(binrow[0].actual_qty) - flt(last[0].qty_after_transaction)) > 0.5:
			return {"ok": False, "message": f"Bin qty disagrees with last SLE on {item_code} @ {warehouse}"}
		if abs(flt(binrow[0].stock_value) - flt(last[0].stock_value)) > 1:
			return {"ok": False, "message": f"Bin value disagrees with last SLE on {item_code} @ {warehouse}"}

	gl = frappe.db.sql(
		"""
		SELECT IFNULL(SUM(debit),0) d, IFNULL(SUM(credit),0) c FROM `tabGL Entry`
		WHERE voucher_no=%s AND IFNULL(is_cancelled,0)=0
		""",
		voucher_no,
		as_dict=True,
	)[0]
	if abs(flt(gl.d) - flt(gl.c)) > VALUE_EPS:
		return {"ok": False, "message": f"GL is unbalanced after rebuild (debit {gl.d} credit {gl.c})"}
	return {"ok": True, "message": "SLE rates, Bin and GL verified"}


def i1_root_for_identity(item_code, warehouse):
	"""Earliest I1 voucher on an identity — the root a Failed RIV is waiting on."""
	if not item_code or not warehouse:
		return None
	rows = frappe.db.sql(
		"""
		SELECT voucher_no, posting_date
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND actual_qty > %s AND incoming_rate < 0
		ORDER BY posting_datetime, creation
		LIMIT 1
		""",
		(item_code, warehouse, QTY_EPS),
		as_dict=True,
	)
	return rows[0].voucher_no if rows else None


def i1_root_cause(voucher: str) -> dict:
	"""Operator panel: why this voucher is negative and what unblocks it."""
	plan = classify_i1_voucher(voucher)
	rows = _fetch_se_rows(voucher)
	issue_rates = document_issue_rates(rows)
	evidence = []
	for row in rows:
		if not _is_incoming(row) or _is_fg(row):
			continue
		qty = _row_qty(row)
		if qty <= QTY_EPS:
			continue
		issued = issue_rates.get(row["item_code"])
		evidence.append(
			{
				"idx": row["idx"],
				"item": row["item_code"],
				"qty": qty,
				"stored_rate": flt(row.get("basic_rate")),
				"document_issue_rate": issued,
				"ratio": (flt(row.get("basic_rate")) / issued) if issued else None,
				"verdict": "REPRICE" if issued and issued > RATE_EPS else "NO_IN_DOCUMENT_SOURCE",
			}
		)
	return {
		"voucher": voucher,
		"i1_status": plan.get("i1_status"),
		"message": plan.get("message"),
		"outgoing_pool": plan.get("outgoing_pool"),
		"secondary_current_total": plan.get("secondary_current_total"),
		"secondary_corrected_total": plan.get("secondary_corrected_total"),
		"fg_current_amount": plan.get("current_amount"),
		"fg_corrected_amount": plan.get("corrected_fg_amount"),
		"evidence": evidence,
		"blocked_riv": _waiting_riv_count(voucher),
		"repair_order": [
			"Reprice secondary inbound from document issue rate",
			"Restore finished-good residual",
			"Stock Entry header totals",
			"SLE + SABB",
			"Identity replay",
			"Selective GL",
			"Retry Failed RIV",
		],
	}


def _gl_voucher_count(voucher) -> int:
	if not voucher:
		return 0
	try:
		return int(
			frappe.db.sql(
				"""
				SELECT COUNT(DISTINCT voucher_no) FROM `tabGL Entry`
				WHERE voucher_type='Stock Entry' AND voucher_no=%s AND IFNULL(is_cancelled,0)=0
				""",
				voucher,
			)[0][0]
			or 0
		)
	except Exception:
		return 0


def _waiting_riv_count(voucher) -> int:
	"""Failed RIV rows whose error names this voucher — the unlock fan-out."""
	if not voucher:
		return 0
	try:
		return int(
			frappe.db.sql(
				"""
				SELECT COUNT(*) FROM `tabRepost Item Valuation`
				WHERE status='Failed' AND docstatus=1 AND error_log LIKE %s
				""",
				f"%{voucher}%",
			)[0][0]
			or 0
		)
	except Exception:
		return 0


def _count(rows, key):
	out = defaultdict(int)
	for row in rows or []:
		out[str(row.get(key) or "")] += 1
	return dict(out)
