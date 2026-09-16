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
from frappe.utils import flt

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
			result = apply_i1_voucher(merged["voucher"])
			sql_executed += int(result.get("sql_updates") or 0)
			row_out = {
				**merged,
				**result,
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


def apply_i1_voucher(voucher_no: str) -> dict:
	"""Write the corrected pool for one voucher, then replay each touched identity."""
	plan = classify_i1_voucher(voucher_no)
	if plan.get("i1_status") != I1_READY:
		raise frappe.ValidationError(
			f"Cannot apply I1 on {voucher_no}: status={plan.get('i1_status')} — {plan.get('message')}"
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

	# SLE transaction rates + SABB follow the Stock Entry rows for every touched item.
	from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
		sync_sabb_from_sle,
		write_sle_transaction_rates,
	)

	for item_code, _warehouse in touched_identities:
		write_sle_transaction_rates(voucher_no, item_code)
		sync_sabb_from_sle(voucher_no, item_code)
		sql_updates += 1

	# Forward identity replay from this voucher only — never global.
	from erpnext_extensions.iran_accounting.historical_stock.replay import replay_from_patient_zero

	replays = []
	for item_code, warehouse in touched_identities:
		if not item_code or not warehouse:
			continue
		replays.append(
			{
				"item": item_code,
				"warehouse": warehouse,
				"replay": replay_from_patient_zero(
					item_code, warehouse, None, from_dt=plan.get("posting_datetime")
				),
			}
		)

	verify = _verify_no_negative_incoming(voucher_no)
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


def _verify_no_negative_incoming(voucher_no: str) -> dict:
	"""Post-write gate: the guard invariant must now hold for this voucher."""
	bad = frappe.db.sql(
		"""
		SELECT name, item_code, actual_qty, incoming_rate
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND is_cancelled=0 AND actual_qty > %s AND incoming_rate < 0
		LIMIT 5
		""",
		(voucher_no, QTY_EPS),
		as_dict=True,
	)
	if bad:
		return {
			"ok": False,
			"message": f"{len(bad)} inbound SLE still negative (e.g. {bad[0].name} {bad[0].item_code})",
			"rows": bad,
		}
	return {"ok": True, "message": "no negative incoming rate remains", "rows": []}


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
