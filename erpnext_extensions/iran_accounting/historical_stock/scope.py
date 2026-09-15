# Copyright (c) 2026, ERPNext Extensions contributors
"""Minimal-scope Historical Repair evaluator.

Smallest safe scope first. Escalate to item+warehouse only when simulation
proves a cross-batch moving-average / qty_after / GL dependency.
"""

from __future__ import annotations

from decimal import Decimal

from erpnext_extensions.iran_accounting.stock_posting_order.optimizer import order_with_times, simulate_running
from erpnext_extensions.iran_accounting.stock_posting_order.replay import (
	INVERSION_ARTIFACT_POISONS,
	replay_series,
	sle_poison_reason,
)
from erpnext_extensions.iran_accounting.stock_posting_order.simulation import D

VALUE_EPS = D("0.5")
QTY_EPS = D("0.0001")
RATE_EPS = D("0.5")

SCOPE_LOCAL_VOUCHER = "LOCAL_VOUCHER"
SCOPE_BATCH = "BATCH_SCOPED"
SCOPE_WORK_ORDER = "WORK_ORDER_SCOPED"
SCOPE_IDENTITY = "IDENTITY_SCOPED"
SCOPE_WAREHOUSE = "WAREHOUSE_VALUATION_SCOPED"
SCOPE_UNSAFE = "UNSAFE_GLOBAL_DEPENDENCY"

EDGE_DOCUMENT = "DOCUMENT_DEPENDENCY"
EDGE_BATCH = "BATCH_DEPENDENCY"
EDGE_WO = "WORK_ORDER_DEPENDENCY"
EDGE_VALUATION = "VALUATION_DEPENDENCY"
EDGE_WAREHOUSE_MA = "WAREHOUSE_MA_DEPENDENCY"
EDGE_GL = "GL_DEPENDENCY"
EDGE_PZ = "PATIENT_ZERO_DEPENDENCY"

POISON_LOCAL = "LOCAL_POISON"
POISON_UNRELATED = "UNRELATED_WAREHOUSE_POISON"

PLAN_READY_LOCAL = "READY_LOCAL_REPAIR"
PLAN_READY_BATCH = "READY_BATCH_SCOPED_REPAIR"
PLAN_READY_WO = "READY_WORK_ORDER_REPAIR"
PLAN_READY_IDENTITY = "READY_IDENTITY_REPAIR"
PLAN_WAREHOUSE_ESCALATION = "WAREHOUSE_ESCALATION_REQUIRED"
PLAN_INVALID_GRAPH = "INVALID_DEPENDENCY_GRAPH"

READY_SCOPES = (
	PLAN_READY_LOCAL,
	PLAN_READY_BATCH,
	PLAN_READY_WO,
	PLAN_READY_IDENTITY,
	"READY",
	"READY_I4",
)

SCOPE_ORDER = (
	SCOPE_LOCAL_VOUCHER,
	SCOPE_BATCH,
	SCOPE_WORK_ORDER,
	SCOPE_IDENTITY,
	SCOPE_WAREHOUSE,
	SCOPE_UNSAFE,
)


def evaluate_minimal_scope(row: dict | None, *, cache: dict | None = None) -> dict:
	"""Load the warehouse window (when possible) and choose the smallest safe scope."""
	cache = cache if cache is not None else {}
	row = row or {}
	sles, opening_qty, opening_value = _load_window(row, cache)
	pair = {v for v in (row.get("inbound_document"), row.get("outbound_document")) if v}
	if sles and pair and not any(_g(r, "voucher_no") in pair for r in sles):
		# Loaded rows are not this pair's inversion window (wrong dates / empty site).
		sles = []
	times = _proposed_times(row)
	if not row.get("downstream_vouchers"):
		row = dict(row)
		row["downstream_vouchers"] = _same_batch_downstream(row, cache)
	return analyze_scope(row, sles, times, opening_qty=opening_qty, opening_value=opening_value)


def analyze_scope(row: dict, sles: list, times: dict | None, *, opening_qty=0, opening_value=0) -> dict:
	"""Pure scope decision. ``sles`` must already carry ``batch`` when known."""
	inbound = row.get("inbound_document")
	outbound = row.get("outbound_document")
	batch = row.get("batch") or ""
	wo = row.get("work_order") or ""
	pair = {v for v in (inbound, outbound) if v}
	sles = list(sles or [])
	times = times or {}

	batch_sles = [r for r in sles if _batch_of(r) == batch] if batch else list(sles)
	batch_qty = _qty_sim(batch_sles, 0, times) if batch_sles else None
	wh_qty = _qty_sim(sles, opening_qty, times) if sles else None
	wh_val = _value_sim(sles, opening_qty, opening_value, times, pair=pair, batch=batch) if sles else None

	interleaved = _interleaved_other_batches(sles, pair, batch, times)
	ma_hits = (wh_val or {}).get("other_batch_changes") or []
	poisons = _classify_poisons(sles, pair, batch, wo, wh_val)
	local_poisons = [p for p in poisons if p["class"] == POISON_LOCAL]
	unrelated = [p for p in poisons if p["class"] == POISON_UNRELATED]
	ma_relevant_unrelated = [p for p in unrelated if p.get("effect") and p["effect"] != "NONE"]

	edges = _edges(row, interleaved, ma_hits, local_poisons, batch, wo)
	cyclic = _cyclic(edges, wait_only=True)
	downstream = list(row.get("downstream_vouchers") or [])

	batch_qty_ok = bool(
		batch_qty
		and batch_qty["current_min"] < 0
		and batch_qty["proposed_min"] >= 0
		and batch_qty["final_unchanged"]
	)
	pair_end_value_equal = bool((wh_val or {}).get("pair_end_value_equal"))
	other_need_rewrite = bool(ma_hits)
	warehouse_still_negative = bool(wh_qty and wh_qty["proposed_min"] < 0)

	escalation = None
	chosen = SCOPE_BATCH
	status = PLAN_READY_BATCH
	reason = "READY — batch/SABB posting-order sim is quantity- and value-isolated"

	if cyclic:
		chosen = SCOPE_UNSAFE
		status = PLAN_INVALID_GRAPH
		reason = "INVALID_DEPENDENCY_GRAPH — cycle remains after minimal-scope analysis"
	elif local_poisons:
		chosen = SCOPE_IDENTITY
		status = "WAITING_RATE_REPAIR"
		reason = (
			f"LOCAL_POISON on {local_poisons[0]['voucher']} "
			f"({local_poisons[0]['reason']}, batch {local_poisons[0].get('batch') or ''})"
		)
	elif not batch_qty_ok:
		chosen = SCOPE_WAREHOUSE
		status = PLAN_WAREHOUSE_ESCALATION
		bq = batch_qty or {}
		escalation = (
			"Batch quantity sim failed "
			f"(current min {bq.get('current_min')}, proposed min {bq.get('proposed_min')}, "
			f"final unchanged {bq.get('final_unchanged')})."
		)
		reason = escalation
	elif other_need_rewrite:
		chosen = SCOPE_WAREHOUSE
		status = PLAN_WAREHOUSE_ESCALATION
		names = ", ".join(
			f"{h['voucher']} (batch {h.get('batch') or '?'})" for h in ma_hits[:6]
		) or "later warehouse lots"
		escalation = (
			f"{EDGE_WAREHOUSE_MA}: {len(ma_hits)} other-batch SLE(s) need qty_after/"
			f"stock_value/valuation_rate rewrite after this reorder ({names}). "
			"ERPNext SLE identity is item+warehouse, not batch. "
			f"Pair-end warehouse value equal: {pair_end_value_equal}."
		)
		if warehouse_still_negative:
			escalation += (
				f" Warehouse running qty would still go negative "
				f"(proposed min {(wh_qty or {}).get('proposed_min')})."
			)
		reason = escalation
	elif ma_relevant_unrelated:
		chosen = SCOPE_WAREHOUSE
		status = PLAN_WAREHOUSE_ESCALATION
		p = ma_relevant_unrelated[0]
		escalation = (
			f"{EDGE_WAREHOUSE_MA}: unrelated poison {p['voucher']} is MA-relevant "
			f"({p.get('effect')}). Cannot isolate this batch."
		)
		reason = escalation
	else:
		chosen = SCOPE_BATCH
		status = PLAN_READY_BATCH
		reason = (
			"READY_BATCH_SCOPED_REPAIR — batch qty recoverable, pair-end warehouse "
			"value unchanged, unrelated warehouse poison is not MA-relevant"
		)

	repair_order = _repair_order(inbound, outbound, downstream, status)
	return {
		"smallest_safe_scope": chosen,
		"status": status,
		"reason": reason,
		"escalation_reason": escalation,
		"escalation_required": status == PLAN_WAREHOUSE_ESCALATION,
		"batch_qty": batch_qty,
		"warehouse_qty": {
			"current_min": str((wh_qty or {}).get("current_min")),
			"proposed_min": str((wh_qty or {}).get("proposed_min")),
			"final_unchanged": (wh_qty or {}).get("final_unchanged"),
		}
		if wh_qty
		else None,
		"pair_end_value_equal": pair_end_value_equal,
		"interleaved_other_batches": interleaved,
		"other_batch_rewrite_count": len(ma_hits),
		"other_batch_changes": ma_hits,
		"local_poisons": local_poisons,
		"unrelated_poison": unrelated,
		"edges": edges,
		"cyclic": cyclic,
		"repair_order": repair_order,
		"batch_qty_ok": batch_qty_ok,
		"warehouse_still_negative": warehouse_still_negative,
		"window_loaded": bool(sles),
		"dependency_type": (edges[0]["type"] if edges else EDGE_BATCH),
	}


def _qty_sim(sles, opening, times) -> dict:
	current = simulate_running(sles, opening)
	proposed = simulate_running(sles, opening, times)
	return {
		"current_min": current["min_qty"],
		"proposed_min": proposed["min_qty"],
		"current_final": current["final_qty"],
		"proposed_final": proposed["final_qty"],
		"final_unchanged": current["final_qty"] == proposed["final_qty"],
	}


def _value_sim(sles, opening_qty, opening_value, times, *, pair=None, batch="") -> dict:
	pair = pair or set()
	cur_ord = order_with_times(sles)
	prop_ord = order_with_times(sles, times)
	cur = replay_series(cur_ord, opening_qty, opening_value)
	prop = replay_series(prop_ord, opening_qty, opening_value)
	cur_by_name = {s["name"]: s for s in cur}
	prop_by_name = {s["name"]: s for s in prop}
	row_by_name = {_g(r, "name"): r for r in sles}
	other_changes = []
	effects = {}
	for name, cs in cur_by_name.items():
		ps = prop_by_name.get(name)
		row = row_by_name.get(name)
		if not ps or not row:
			continue
		qty_d = abs(D(cs["qty_after_transaction"]) - D(ps["qty_after_transaction"])) > QTY_EPS
		val_d = abs(D(cs["stock_value"]) - D(ps["stock_value"])) > VALUE_EPS
		rate_d = abs(D(cs["valuation_rate"]) - D(ps["valuation_rate"])) > RATE_EPS
		voucher = _g(row, "voucher_no")
		if not (qty_d or val_d or rate_d):
			effects[voucher] = "NONE"
			continue
		fields = []
		if qty_d:
			fields.append("qty_after")
		if val_d:
			fields.append("stock_value")
		if rate_d:
			fields.append("valuation_rate")
		effects[voucher] = ",".join(fields)
		b = _batch_of(row)
		if voucher not in pair and b != batch:
			other_changes.append({"voucher": voucher, "sle": name, "batch": b, "fields": fields, "work_order": _g(row, "work_order")})
	c_end = _state_after_pair(cur, cur_ord, pair)
	p_end = _state_after_pair(prop, prop_ord, pair)
	value_equal = bool(
		c_end
		and p_end
		and abs(D(c_end["stock_value"]) - D(p_end["stock_value"])) <= VALUE_EPS
		and abs(D(c_end["qty_after_transaction"]) - D(p_end["qty_after_transaction"])) <= QTY_EPS
	)
	return {
		"other_batch_changes": other_changes,
		"effects": effects,
		"pair_end_current": c_end,
		"pair_end_proposed": p_end,
		"pair_end_value_equal": value_equal,
	}


def _state_after_pair(series, ordered, pair: set):
	if not pair:
		return None
	seen = set()
	last = None
	for step, row in zip(series, ordered):
		last = {
			"qty_after_transaction": step["qty_after_transaction"],
			"stock_value": step["stock_value"],
			"valuation_rate": step["valuation_rate"],
			"voucher": _g(row, "voucher_no"),
		}
		seen.add(_g(row, "voucher_no"))
		if pair <= seen:
			return last
	return last


def _interleaved_other_batches(sles, pair, batch, times) -> list[dict]:
	if not pair or not sles:
		return []
	pair_rows = [r for r in sles if _g(r, "voucher_no") in pair]
	if len(pair_rows) < 2:
		return []
	lo = min(_g(r, "posting_datetime") for r in pair_rows)
	hi = max(_g(r, "posting_datetime") for r in pair_rows)
	# include proposed outbound if later
	out = []
	seen = set()
	for r in sles:
		dt = _g(r, "posting_datetime")
		if dt is None or dt < lo or dt > hi:
			continue
		if _g(r, "voucher_no") in pair:
			continue
		if _batch_of(r) == batch:
			continue
		key = (_g(r, "voucher_no"), _batch_of(r))
		if key in seen:
			continue
		seen.add(key)
		out.append(
			{
				"voucher": _g(r, "voucher_no"),
				"batch": _batch_of(r),
				"work_order": _g(r, "work_order"),
				"qty": _g(r, "actual_qty"),
				"edge_type": EDGE_WAREHOUSE_MA if _batch_of(r) != batch else EDGE_BATCH,
			}
		)
	return out


def _classify_poisons(sles, pair, batch, wo, wh_val) -> list[dict]:
	effects = (wh_val or {}).get("effects") or {}
	out = []
	seen = set()
	for r in sles:
		reason = sle_poison_reason(r)
		if not reason or reason in INVERSION_ARTIFACT_POISONS:
			continue
		voucher = _g(r, "voucher_no")
		if voucher in seen:
			continue
		seen.add(voucher)
		b = _batch_of(r)
		local = bool(voucher in pair or (batch and b == batch) or (wo and _g(r, "work_order") == wo and b == batch))
		klass = POISON_LOCAL if local else POISON_UNRELATED
		effect = effects.get(voucher, "NONE")
		if klass == POISON_UNRELATED and effect == "NONE":
			# still NONE even if voucher not in effects map
			effect = effects.get(voucher) or "NONE"
		out.append(
			{
				"voucher": voucher,
				"reason": reason,
				"batch": b,
				"work_order": _g(r, "work_order"),
				"class": klass,
				"effect": effect if klass == POISON_UNRELATED else "LOCAL",
			}
		)
	return out


def _edges(row, interleaved, ma_hits, local_poisons, batch, wo) -> list[dict]:
	edges = []
	inbound = row.get("inbound_document")
	outbound = row.get("outbound_document")
	if inbound and outbound:
		edges.append(
			{
				"from": inbound,
				"to": outbound,
				"type": EDGE_BATCH,
				"why": f"same Batch/SABB {batch} — manufacture then consume",
			}
		)
	prev = outbound or inbound
	for extra in row.get("downstream_vouchers") or []:
		if prev and extra != prev:
			edges.append(
				{
					"from": prev,
					"to": extra,
					"type": EDGE_DOCUMENT,
					"why": f"same-batch downstream {batch}",
				}
			)
			prev = extra
	for hit in interleaved:
		edges.append(
			{
				"from": outbound or inbound,
				"to": hit["voucher"],
				"type": EDGE_WAREHOUSE_MA if hit.get("batch") != batch else EDGE_BATCH,
				"why": (
					f"interleaved batch {hit.get('batch')} in the inversion window; "
					"warehouse qty_after is item+warehouse"
				),
			}
		)
		if wo and hit.get("work_order") == wo:
			edges.append(
				{
					"from": outbound or inbound,
					"to": hit["voucher"],
					"type": EDGE_WO,
					"why": f"same Work Order {wo}",
				}
			)
	for p in local_poisons:
		if p["voucher"] in {inbound, outbound}:
			continue
		edges.append(
			{
				"from": outbound or inbound,
				"to": p["voucher"],
				"type": EDGE_VALUATION,
				"why": f"{POISON_LOCAL}: {p['reason']}",
			}
		)
	# unique by from-to-type
	uniq = []
	seen = set()
	for e in edges:
		key = (e["from"], e["to"], e["type"])
		if key in seen or not e["from"] or not e["to"] or e["from"] == e["to"]:
			continue
		seen.add(key)
		uniq.append(e)
	return uniq


WAIT_EDGE_TYPES = frozenset({EDGE_DOCUMENT, EDGE_BATCH, EDGE_WO, EDGE_VALUATION, EDGE_PZ})


def _cyclic(edges: list[dict], *, wait_only: bool = False) -> bool:
	graph = {}
	for e in edges:
		if wait_only and e.get("type") not in WAIT_EDGE_TYPES:
			continue
		graph.setdefault(e["from"], []).append(e["to"])
	visiting = set()
	seen = set()

	def dfs(n):
		if n in visiting:
			return True
		if n in seen:
			return False
		visiting.add(n)
		for nxt in graph.get(n, []):
			if dfs(nxt):
				return True
		visiting.remove(n)
		seen.add(n)
		return False

	return any(dfs(n) for n in list(graph))


def _repair_order(inbound, outbound, downstream, status) -> list[str]:
	out = []
	for v in (inbound, outbound, *list(downstream or [])):
		if v and v not in out:
			out.append(v)
	return out


def _proposed_times(row) -> dict:
	from frappe.utils import get_datetime

	times = {}
	for m in row.get("moves") or []:
		doc = m.get("document")
		new = m.get("new")
		if doc and new:
			try:
				times[doc] = get_datetime(new)
			except Exception:
				times[doc] = new
	if row.get("outbound_document") and row.get("proposed_outbound_time"):
		try:
			times.setdefault(row["outbound_document"], get_datetime(row["proposed_outbound_time"]))
		except Exception:
			times.setdefault(row["outbound_document"], row["proposed_outbound_time"])
	return times


def _load_window(row, cache) -> tuple[list, Decimal, Decimal]:
	item = row.get("item") or row.get("item_code")
	warehouse = row.get("warehouse")
	from_dt = row.get("current_outbound_time") or row.get("current_inbound_time") or row.get("posting_datetime")
	if not item or not warehouse or not from_dt:
		return [], D(0), D(0)
	key = ("scope_window", item, warehouse, str(from_dt))
	if key in cache:
		return cache[key]
	try:
		import frappe
		from frappe.utils import get_datetime
		from erpnext_extensions.iran_accounting.stock_posting_order.replay import _fetch_previous

		start = get_datetime(from_dt)
		rows = frappe.db.sql(
			"""
			SELECT sle.name, sle.voucher_no, sle.actual_qty, sle.qty_after_transaction,
			       sle.incoming_rate, sle.outgoing_rate, sle.valuation_rate, sle.stock_value,
			       sle.stock_value_difference, sle.posting_datetime, sle.creation,
			       sle.serial_and_batch_bundle, sle.batch_no,
			       se.purpose, se.work_order,
			       (SELECT sbe.batch_no FROM `tabSerial and Batch Entry` sbe
			         WHERE sbe.parent=sle.serial_and_batch_bundle LIMIT 1) sabb_batch
			FROM `tabStock Ledger Entry` sle
			LEFT JOIN `tabStock Entry` se ON se.name=sle.voucher_no
			WHERE sle.item_code=%s AND sle.warehouse=%s AND sle.is_cancelled=0
			  AND sle.posting_datetime >= %s
			ORDER BY sle.posting_datetime, sle.creation
			LIMIT 4000
			""",
			(item, warehouse, start),
			as_dict=True,
		)
		for r in rows:
			r.batch = r.sabb_batch or r.batch_no or ""
		prev = _fetch_previous(item, warehouse, start)
		oq = D(prev.qty_after_transaction) if prev else D(0)
		ov = D(prev.stock_value) if prev else D(0)
	except Exception:
		rows, oq, ov = [], D(0), D(0)
	cache[key] = (rows, oq, ov)
	return cache[key]


def _same_batch_downstream(row, cache) -> list[str]:
	item = row.get("item") or row.get("item_code")
	batch = row.get("batch")
	outbound = row.get("outbound_document")
	from_dt = row.get("proposed_outbound_time") or row.get("current_inbound_time") or row.get("current_outbound_time")
	if not (item and batch and from_dt):
		return []
	key = ("down", item, batch, str(from_dt), outbound or "")
	if key in cache:
		return cache[key]
	out = []
	try:
		import frappe
		from frappe.utils import get_datetime

		rows = frappe.db.sql(
			"""
			SELECT DISTINCT sle.voucher_no, sle.posting_datetime
			FROM `tabStock Ledger Entry` sle
			JOIN `tabSerial and Batch Entry` sbe ON sbe.parent=sle.serial_and_batch_bundle
			WHERE sle.item_code=%s AND sle.is_cancelled=0
			  AND sbe.batch_no=%s AND sle.posting_datetime >= %s
			ORDER BY sle.posting_datetime, sle.creation
			LIMIT 20
			""",
			(item, batch, get_datetime(from_dt)),
			as_dict=True,
		)
		skip = {outbound, row.get("inbound_document")}
		for r in rows:
			if r.voucher_no not in skip and r.voucher_no not in out:
				out.append(r.voucher_no)
	except Exception:
		out = []
	cache[key] = out
	return out


def _batch_of(row) -> str:
	return str(_g(row, "batch") or _g(row, "batch_no") or _g(row, "sabb_batch") or "")


def _g(row, key, default=None):
	if isinstance(row, dict):
		return row.get(key, default)
	return getattr(row, key, default)
