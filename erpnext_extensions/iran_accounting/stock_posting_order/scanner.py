# Copyright (c) 2026, ERPNext Extensions contributors
"""Full-history same-time group scan using the batch-aware optimizer."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta

import frappe
from frappe.utils import get_datetime

from erpnext_extensions.iran_accounting.stock_posting_order import (
	CONFIDENCE_EXACT,
	CONFIDENCE_LIKELY,
	MAX_OFFSET_SECONDS,
)
from erpnext_extensions.iran_accounting.stock_posting_order.batch_identity import canonical_batch_no
from erpnext_extensions.iran_accounting.stock_posting_order.dependency import classify_edge
from erpnext_extensions.iran_accounting.stock_posting_order.optimizer import (
	minimum_seconds_label,
	optimize_group,
)
from erpnext_extensions.iran_accounting.stock_posting_order.ordering import format_datetime
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D, classify_valuation_impact, order_sles


def _norm_time(value) -> str:
	if value is None:
		return "00:00:00"
	if hasattr(value, "total_seconds") and not isinstance(value, datetime):
		secs = int(value.total_seconds())
		h, rem = divmod(secs, 3600)
		m, s = divmod(rem, 60)
		return f"{h:02d}:{m:02d}:{s:02d}"
	text = str(value)
	if len(text) >= 8 and text[2] == ":":
		return text[:8]
	return text


def _signature(payload: dict) -> str:
	blob = json.dumps(payload, sort_keys=True, default=str)
	return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fetch_ledger_rows(company=None) -> list:
	conds = ["sle.is_cancelled=0"]
	params: dict = {}
	if company:
		conds.append("(se.company=%(company)s OR se.company IS NULL)")
		params["company"] = company
	sql = f"""
		SELECT
			sle.name, sle.item_code, sle.warehouse, sle.posting_date, sle.posting_time,
			sle.posting_datetime, sle.creation, sle.actual_qty, sle.qty_after_transaction,
			sle.batch_no, sle.serial_and_batch_bundle, sle.voucher_no, sle.voucher_type,
			sle.voucher_detail_no, sle.incoming_rate, sle.valuation_rate, sle.stock_value,
			sle.stock_value_difference,
			se.purpose, se.work_order, se.job_card, se.company, se.modified,
			se.docstatus se_docstatus,
			sbe.batch_no sabb_batch_no, sbe.batch_count
		FROM `tabStock Ledger Entry` sle
		LEFT JOIN `tabStock Entry` se
			ON se.name = sle.voucher_no AND sle.voucher_type='Stock Entry'
		LEFT JOIN (
			SELECT parent,
			       MIN(batch_no) batch_no,
			       COUNT(DISTINCT batch_no) batch_count
			FROM `tabSerial and Batch Entry`
			WHERE IFNULL(batch_no,'') != ''
			GROUP BY parent
		) sbe ON sbe.parent = sle.serial_and_batch_bundle
		WHERE {" AND ".join(conds)}
		ORDER BY sle.item_code, sle.warehouse, sle.posting_datetime, sle.creation
		"""
	if params:
		return frappe.db.sql(sql, params, as_dict=True)
	return frappe.db.sql(sql, as_dict=True)


def _annotate(row) -> dict:
	d = dict(row)
	if int(d.get("batch_count") or 0) > 1 and not (d.get("batch_no") or "").strip():
		d["canonical_batch"] = "*MULTI*"
	else:
		d["canonical_batch"] = canonical_batch_no(
			d, [{"batch_no": d.get("sabb_batch_no")}] if d.get("sabb_batch_no") else []
		) or ""
	d["posting_time_norm"] = _norm_time(d.get("posting_time"))
	return d


def _group_key(row) -> tuple:
	return (
		row["item_code"],
		row["warehouse"],
		row.get("canonical_batch") or "",
		str(row.get("posting_date") or ""),
		row.get("posting_time_norm") or "",
	)


def _identity_key(row) -> tuple:
	return (row["item_code"], row["warehouse"], row.get("canonical_batch") or "")


def _opening_before(series: list, t_dt, t_creation) -> float:
	total = D(0)
	for row in series:
		dt = get_datetime(row["posting_datetime"])
		cr = str(row.get("creation") or "")
		if dt < t_dt or (dt == t_dt and cr < t_creation):
			total += D(row.get("actual_qty"))
		else:
			# series is ordered; later rows cannot be before T
			if dt > t_dt:
				break
	return total


def _voucher_edges(group_rows: list, against_map: dict) -> tuple[list, str, str]:
	by_v: dict = {}
	net: dict = {}
	for r in group_rows:
		vn = r["voucher_no"]
		by_v.setdefault(vn, r)
		net[vn] = net.get(vn, D(0)) + D(r.get("actual_qty"))
	vouchers = list(by_v)
	edges = []
	confidences = []
	reasons = []
	for a in vouchers:
		if net[a] <= 0:
			continue
		ra = by_v[a]
		for b in vouchers:
			if a == b or net[b] >= 0:
				continue
			rb = by_v[b]
			against = against_map.get((b, a), False)
			same_wo = bool(ra.get("work_order") and ra.get("work_order") == rb.get("work_order"))
			same_jc = bool(ra.get("job_card") and ra.get("job_card") == rb.get("job_card"))
			same_batch = bool((ra.get("canonical_batch") or "").strip()) and ra.get(
				"canonical_batch"
			) == rb.get("canonical_batch")
			conf, reason = classify_edge(
				{"item_code": ra["item_code"], "warehouse": ra["warehouse"], "batch_no": ra.get("canonical_batch")},
				{"item_code": rb["item_code"], "warehouse": rb["warehouse"], "batch_no": rb.get("canonical_batch")},
				same_work_order=same_wo,
				same_job_card=same_jc,
				against_stock_entry=against,
				same_batch=same_batch,
				inbound_purpose=ra.get("purpose"),
				outbound_purpose=rb.get("purpose"),
			)
			confidences.append(conf)
			reasons.append(reason)
			if conf in (CONFIDENCE_EXACT, CONFIDENCE_LIKELY):
				edges.append((a, b))
	if CONFIDENCE_EXACT in confidences:
		confidence = CONFIDENCE_EXACT
	elif CONFIDENCE_LIKELY in confidences:
		confidence = CONFIDENCE_LIKELY
	else:
		confidence = "AMBIGUOUS"
	reason = "+".join(sorted(set(reasons))) if reasons else "unproven"
	return edges, confidence, reason


def _against_map(voucher_names: list) -> dict:
	if not voucher_names:
		return {}
	out = {}
	chunk = 500
	for i in range(0, len(voucher_names), chunk):
		part = voucher_names[i : i + chunk]
		rows = frappe.db.sql(
			"""
			SELECT parent, against_stock_entry
			FROM `tabStock Entry Detail`
			WHERE parent IN %(names)s AND IFNULL(against_stock_entry,'') != ''
			""",
			{"names": part},
			as_dict=True,
		)
		for r in rows:
			out[(r.parent, r.against_stock_entry)] = True
	return out


def scan_same_time_groups(
	*,
	company: str | None = None,
	from_date: str | None = None,
	to_date: str | None = None,
	include_no_repair: bool = True,
	include_likely: bool = True,
	max_seconds: int = MAX_OFFSET_SECONDS,
) -> dict:
	"""Scan complete history. Returns rows plus summary counts."""
	raw = fetch_ledger_rows(company=company)
	annotated = [_annotate(r) for r in raw]
	by_identity: dict[tuple, list] = defaultdict(list)
	groups: dict[tuple, list] = defaultdict(list)
	multi_skipped = 0
	from_s = str(from_date) if from_date else None
	to_s = str(to_date) if to_date else None
	for row in annotated:
		if row.get("canonical_batch") == "*MULTI*":
			multi_skipped += 1
			continue
		by_identity[_identity_key(row)].append(row)
		# grouping / repair is Stock Entry only; opening uses full identity series
		if row.get("voucher_type") != "Stock Entry":
			continue
		if int(row.get("se_docstatus") or 0) != 1:
			continue
		posting_date = str(row.get("posting_date") or "")
		if from_s and posting_date < from_s:
			continue
		if to_s and posting_date > to_s:
			continue
		groups[_group_key(row)].append(row)

	voucher_names = sorted({r["voucher_no"] for rows in groups.values() for r in rows})
	against_map = _against_map(voucher_names)

	by_voucher_all: dict[str, list] = defaultdict(list)
	for row in annotated:
		if row.get("voucher_type") == "Stock Entry":
			by_voucher_all[row["voucher_no"]].append(row)

	summary = defaultdict(int)
	seconds_dist = defaultdict(int)
	out_rows = []
	summary["multi_batch_skipped"] = multi_skipped
	for key, rows in groups.items():
		if len(rows) < 2:
			continue
		qtys = [D(r.get("actual_qty")) for r in rows]
		has_in = any(q > 0 for q in qtys)
		has_out = any(q < 0 for q in qtys)
		if not (has_in and has_out):
			continue
		summary["same_time_groups"] += 1
		if rows[0].get("canonical_batch"):
			summary["batch_sabb_groups"] += 1
		else:
			summary["non_batch_groups"] += 1

		base_t = get_datetime(rows[0]["posting_datetime"])
		min_creation = min(str(r.get("creation") or "") for r in rows)
		series = by_identity[_identity_key(rows[0])]
		opening = _opening_before(series, base_t, min_creation)

		horizon = base_t + timedelta(seconds=int(max_seconds))
		idk = _identity_key(rows[0])
		group_names = {r["name"] for r in rows}
		collisions = [
			r
			for r in series
			if r["name"] not in group_names
			and get_datetime(r["posting_datetime"]) > base_t
			and get_datetime(r["posting_datetime"]) <= horizon
		]

		edges, confidence, reason = _voucher_edges(rows, against_map)

		moved_vouchers = {r["voucher_no"] for r in rows}
		cross_windows = {}
		for vn in moved_vouchers:
			for r in by_voucher_all.get(vn, []):
				ck = _identity_key(r)
				if ck == idk:
					continue
				if ck not in cross_windows:
					cseries = by_identity.get(ck, [])
					window = [
						x
						for x in cseries
						if get_datetime(x["posting_datetime"]) >= base_t
						and get_datetime(x["posting_datetime"]) <= horizon
					]
					cross_windows[ck] = {
						"sles": window,
						"opening": _opening_before(cseries, base_t, min_creation),
					}

		result = optimize_group(
			sles=rows,
			opening=opening,
			base_t=base_t,
			edges=edges,
			collisions=collisions,
			cross_windows=cross_windows or None,
			max_seconds=max_seconds,
		)
		status = result["status"]
		summary[status] += 1
		sec = result.get("minimum_seconds_required")
		if status == "REPAIRABLE_SECONDS":
			if sec == 0:
				seconds_dist["0"] += 1
			elif sec == 1:
				seconds_dist["1"] += 1
			elif sec == 2:
				seconds_dist["2"] += 1
			elif sec == 3:
				seconds_dist["3"] += 1
			elif sec and 4 <= int(sec) <= 10:
				seconds_dist["4-10"] += 1
			else:
				seconds_dist[">10"] += 1

		keep = True
		if status == "NO_REPAIR_NEEDED" and not include_no_repair:
			if confidence not in (CONFIDENCE_EXACT, CONFIDENCE_LIKELY):
				keep = False
			else:
				keep = True
		if confidence == "LIKELY" and not include_likely and status != "REPAIRABLE_SECONDS":
			keep = False
		if keep:
			out_rows.append(_build_row(rows, result, confidence, reason, opening, base_t))

	summary["seconds_distribution"] = dict(seconds_dist)
	return {"rows": out_rows, "summary": dict(summary), "sle_scanned": len(annotated)}


def run_full_history_scan(**kwargs) -> dict:
	start = datetime.now()
	result = scan_same_time_groups(**kwargs)
	end = datetime.now()
	elapsed = (end - start).total_seconds()
	sle_n = int(result.get("sle_scanned") or 0)
	grp_n = int((result.get("summary") or {}).get("same_time_groups") or 0)
	result["timing"] = {
		"start_local": start.strftime("%Y-%m-%d %H:%M:%S"),
		"end_local": end.strftime("%Y-%m-%d %H:%M:%S"),
		"elapsed_seconds": elapsed,
		"groups_per_sec": (grp_n / elapsed) if elapsed else None,
		"sle_per_sec": (sle_n / elapsed) if elapsed else None,
	}
	return result


def _build_row(rows, result, confidence, reason, opening, base_t) -> dict:
	ins = [r for r in rows if D(r.get("actual_qty")) > 0]
	outs = [r for r in rows if D(r.get("actual_qty")) < 0]
	inbound = ins[0] if ins else rows[0]
	outbound = outs[0] if outs else rows[-1]
	status = result["status"]
	eligible = status == "REPAIRABLE_SECONDS" and confidence == CONFIDENCE_EXACT
	ui_status = status
	if eligible:
		ui_status = "ELIGIBLE"
	elif status == "REPAIRABLE_SECONDS" and confidence == CONFIDENCE_LIKELY:
		ui_status = "MANUAL_APPROVAL"
	times = result.get("times") or {}
	proposed_out = times.get(outbound["voucher_no"]) or format_datetime(base_t)
	proposed_in = times.get(inbound["voucher_no"]) or format_datetime(base_t)
	cur = result.get("current") or {}
	prop = result.get("proposed") or cur
	val_impact = "QUANTITY-ONLY"
	if status == "REPAIRABLE_SECONDS":
		val_impact = classify_valuation_impact(
			order_sles(rows),
			order_sles(rows, proposed_times=times),
		)
	moves = result.get("moves") or []
	min_sec = result.get("minimum_seconds_required")
	min_label = minimum_seconds_label(status, min_sec)
	row_sim = []
	cur_map = {s["name"]: s for s in (cur.get("series") or []) if s.get("name")}
	prop_map = {s["name"]: s for s in (prop.get("series") or []) if s.get("name")}
	for r in rows:
		cs = cur_map.get(r["name"]) or {}
		ps = prop_map.get(r["name"]) or {}
		row_sim.append(
			{
				"voucher": r["voucher_no"],
				"purpose": r.get("purpose"),
				"current_time": format_datetime(get_datetime(r["posting_datetime"])),
				"actual_qty": str(D(r.get("actual_qty"))),
				"current_running_qty": str(cs.get("running_qty_after", "")),
				"proposed_time": str(ps.get("proposed_time") or times.get(r["voucher_no"]) or ""),
				"proposed_running_qty": str(ps.get("running_qty_after", "")),
				"seconds_shifted": next((m["seconds"] for m in moves if m["document"] == r["voucher_no"]), 0),
			}
		)
	payload = {
		"inbound_document": inbound["voucher_no"],
		"outbound_document": outbound["voucher_no"],
		"inbound_purpose": inbound.get("purpose"),
		"outbound_purpose": outbound.get("purpose"),
		"item": inbound["item_code"],
		"warehouse": inbound["warehouse"],
		"batch": inbound.get("canonical_batch") or inbound.get("batch_no"),
		"sabb_inbound": inbound.get("serial_and_batch_bundle"),
		"sabb_outbound": outbound.get("serial_and_batch_bundle"),
		"current_inbound_time": format_datetime(get_datetime(inbound["posting_datetime"])),
		"current_outbound_time": format_datetime(get_datetime(outbound["posting_datetime"])),
		"proposed_inbound_time": proposed_in,
		"proposed_outbound_time": proposed_out,
		"opening_qty": str(D(opening)),
		"min_qty_before": str(cur.get("min_qty", "")),
		"min_qty_after": str(prop.get("min_qty", "")),
		"final_qty_before": str(cur.get("final_qty", "")),
		"final_qty_after": str(prop.get("final_qty", "")),
		"minimum_seconds_required": min_sec,
		"minimum_seconds_label": min_label,
		"moves": moves,
		"row_simulation": row_sim,
		"valuation_impact": val_impact,
		"gl_impact": "RIV_REQUIRED" if val_impact == "VALUATION-IMPACTING" else "NONE",
		"confidence": confidence,
		"dependency_reason": reason,
		"status": ui_status,
		"optimizer_status": status,
		"inbound_modified": str(inbound.get("modified") or ""),
		"outbound_modified": str(outbound.get("modified") or ""),
		"work_order": inbound.get("work_order") or outbound.get("work_order"),
		"inbound_job_card": inbound.get("job_card"),
		"outbound_job_card": outbound.get("job_card"),
		"company": inbound.get("company"),
		"eligible": eligible,
		"chain": f"{inbound['voucher_no']} → {outbound['voucher_no']}",
		"posting_date": str(inbound.get("posting_date") or ""),
		"has_batch": bool(inbound.get("canonical_batch")),
		"current_series": cur.get("series") or [],
		"proposed_series": prop.get("series") or [],
	}
	payload["dependency_signature"] = _signature(
		{
			"in": payload["inbound_document"],
			"out": payload["outbound_document"],
			"item": payload["item"],
			"wh": payload["warehouse"],
			"batch": payload["batch"],
			"in_time": payload["current_inbound_time"],
			"out_time": payload["current_outbound_time"],
			"in_mod": payload["inbound_modified"],
			"out_mod": payload["outbound_modified"],
			"reason": reason,
			"status": status,
			"moves": moves,
		}
	)
	return payload
