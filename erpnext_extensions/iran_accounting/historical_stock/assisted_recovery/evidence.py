# Copyright (c) 2026, ERPNext Extensions contributors
"""Build a complete evidence package for one residual recovery record."""

from __future__ import annotations

from frappe.utils import flt


def build_evidence_package(row: dict, *, cache: dict | None = None) -> dict:
	"""Gather every available provenance signal for assisted recovery."""
	import frappe

	cache = cache if cache is not None else {}
	voucher = row.get("voucher") or row.get("voucher_no")
	item = row.get("item") or row.get("item_code")
	warehouse = row.get("warehouse") or row.get("s_warehouse") or row.get("t_warehouse")
	batch = row.get("batch") or row.get("batch_no")
	pz = row.get("patient_zero")
	pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz

	pkg = {
		"voucher": voucher,
		"item": item,
		"warehouse": warehouse,
		"batch": batch,
		"patient_zero": pz_v,
		"topic": row.get("topic"),
		"planner_status": row.get("planner_status"),
		"confidence": row.get("confidence"),
		"current_rate": flt(row.get("current") if row.get("current") is not None else row.get("current_rate")),
		"expected_rate": flt(row.get("expected") if row.get("expected") is not None else row.get("proposed_rate")),
		"source_of_truth": row.get("source") or row.get("source_of_truth"),
		"flags": list(row.get("flags") or []),
		"dependency_chain": _dependency_chain(row, cache),
		"previous_sle": None,
		"next_sle": None,
		"voucher_sle": [],
		"purchase_receipts": [],
		"stock_reconciliations": [],
		"material_receipts": [],
		"stock_entry": None,
		"gl": None,
		"batch_inward": None,
		"sabb": [],
		"version_history": [],
		"audit_trail": [],
		"repair_log": [],
		"rate_sources": {},
		"hidden_inbound_candidates": [],
	}

	if not item or not warehouse:
		pkg["incomplete"] = "missing item/warehouse"
		return pkg

	pkg["previous_sle"], pkg["next_sle"] = _prev_next_sle(item, warehouse, voucher, cache)
	pkg["voucher_sle"] = _voucher_sle(voucher, item) if voucher else []
	pkg["purchase_receipts"] = _related_purchases(item, warehouse, batch)
	pkg["stock_reconciliations"] = _related_reco(item, warehouse)
	pkg["material_receipts"] = _material_receipts(item, warehouse)
	pkg["stock_entry"] = _stock_entry_summary(voucher) if voucher else None
	pkg["gl"] = _gl_summary(voucher) if voucher else None
	pkg["batch_inward"] = _batch_inward_rate(item, warehouse, batch)
	pkg["sabb"] = _sabb_rows(voucher, item) if voucher else []
	pkg["version_history"] = _version_history(voucher) if voucher else []
	pkg["audit_trail"] = _audit_trail(voucher) if voucher else []
	pkg["repair_log"] = _repair_log(voucher, item) if voucher else []
	pkg["rate_sources"] = _collect_rate_sources(pkg, row)
	return pkg


def _dependency_chain(row: dict, cache: dict) -> list[str]:
	chain = []
	seen = set()
	cur = row
	by_v = cache.get("rows_by_voucher") or {}
	for _ in range(12):
		v = cur.get("voucher") if isinstance(cur, dict) else None
		if v and v not in seen:
			chain.append(v)
			seen.add(v)
		pz = cur.get("patient_zero") if isinstance(cur, dict) else None
		pz_v = pz.get("voucher_no") if isinstance(pz, dict) else pz
		if not pz_v or pz_v in seen:
			break
		chain.append(pz_v)
		seen.add(pz_v)
		nxt = by_v.get(pz_v)
		if not nxt:
			break
		cur = nxt
	return chain


def _prev_next_sle(item, warehouse, voucher, cache):
	import frappe

	key = ("sle_window", item, warehouse, voucher or "")
	if key in cache:
		return cache[key]
	rows = frappe.db.sql(
		"""
		SELECT name, voucher_no, voucher_type, actual_qty, incoming_rate, outgoing_rate,
		       valuation_rate, stock_value_difference, qty_after_transaction,
		       posting_datetime, batch_no
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		ORDER BY posting_datetime, creation
		LIMIT 4000
		""",
		(item, warehouse),
		as_dict=True,
	)
	prev = nxt = None
	for i, r in enumerate(rows or []):
		if voucher and r.voucher_no == voucher:
			prev = rows[i - 1] if i > 0 else None
			nxt = rows[i + 1] if i + 1 < len(rows) else None
			break
	if prev is None and rows:
		# No voucher match — use latest healthy prior
		for r in reversed(rows):
			if abs(flt(r.valuation_rate) or flt(r.incoming_rate)) > 0:
				prev = r
				break
	out = (_sle_brief(prev), _sle_brief(nxt))
	cache[key] = out
	return out


def _sle_brief(r):
	if not r:
		return None
	return {
		"name": r.get("name") if isinstance(r, dict) else r.name,
		"voucher_no": r.get("voucher_no") if isinstance(r, dict) else r.voucher_no,
		"voucher_type": r.get("voucher_type") if isinstance(r, dict) else r.voucher_type,
		"actual_qty": flt(r.get("actual_qty") if isinstance(r, dict) else r.actual_qty),
		"incoming_rate": flt(r.get("incoming_rate") if isinstance(r, dict) else r.incoming_rate),
		"outgoing_rate": flt(r.get("outgoing_rate") if isinstance(r, dict) else r.outgoing_rate),
		"valuation_rate": flt(r.get("valuation_rate") if isinstance(r, dict) else r.valuation_rate),
		"qty_after": flt(
			r.get("qty_after_transaction") if isinstance(r, dict) else r.qty_after_transaction
		),
		"posting_datetime": str(
			r.get("posting_datetime") if isinstance(r, dict) else r.posting_datetime
		),
		"batch_no": r.get("batch_no") if isinstance(r, dict) else r.batch_no,
	}


def _voucher_sle(voucher, item):
	import frappe

	rows = frappe.db.sql(
		"""
		SELECT name, warehouse, actual_qty, incoming_rate, outgoing_rate, valuation_rate,
		       stock_value_difference, qty_after_transaction, batch_no
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s AND item_code=%s AND is_cancelled=0
		""",
		(voucher, item),
		as_dict=True,
	)
	return [_sle_brief(r) for r in rows or []]


def _related_purchases(item, warehouse, batch):
	import frappe

	conds = ["sle.item_code=%s", "sle.is_cancelled=0", "sle.voucher_type='Purchase Receipt'", "sle.actual_qty>0"]
	args = [item]
	if warehouse:
		conds.append("sle.warehouse=%s")
		args.append(warehouse)
	if batch:
		conds.append("(sle.batch_no=%s OR sle.batch_no IS NULL OR sle.batch_no='')")
		args.append(batch)
	rows = frappe.db.sql(
		f"""
		SELECT sle.voucher_no, sle.incoming_rate, sle.valuation_rate, sle.actual_qty,
		       sle.posting_datetime, sle.batch_no
		FROM `tabStock Ledger Entry` sle
		WHERE {" AND ".join(conds)}
		ORDER BY sle.posting_datetime DESC
		LIMIT 8
		""",
		args,
		as_dict=True,
	)
	return [
		{
			"voucher": r.voucher_no,
			"rate": max(abs(flt(r.incoming_rate)), abs(flt(r.valuation_rate))),
			"qty": flt(r.actual_qty),
			"posting_datetime": str(r.posting_datetime),
			"batch_no": r.batch_no,
		}
		for r in rows or []
	]


def _related_reco(item, warehouse):
	import frappe

	rows = frappe.db.sql(
		"""
		SELECT voucher_no, incoming_rate, valuation_rate, actual_qty, stock_value_difference,
		       posting_datetime
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
		  AND voucher_type='Stock Reconciliation'
		ORDER BY posting_datetime DESC
		LIMIT 5
		""",
		(item, warehouse),
		as_dict=True,
	)
	return [
		{
			"voucher": r.voucher_no,
			"rate": max(abs(flt(r.incoming_rate)), abs(flt(r.valuation_rate))),
			"qty": flt(r.actual_qty),
			"svd": flt(r.stock_value_difference),
			"posting_datetime": str(r.posting_datetime),
		}
		for r in rows or []
	]


def _material_receipts(item, warehouse):
	import frappe

	rows = frappe.db.sql(
		"""
		SELECT se.name, sed.basic_rate, sed.valuation_rate, sed.qty, se.posting_date, se.posting_time
		FROM `tabStock Entry Detail` sed
		JOIN `tabStock Entry` se ON se.name=sed.parent
		WHERE se.docstatus=1 AND se.purpose IN ('Material Receipt','Material Transfer')
		  AND sed.item_code=%s AND (sed.t_warehouse=%s OR sed.s_warehouse=%s)
		ORDER BY se.posting_date DESC, se.posting_time DESC
		LIMIT 8
		""",
		(item, warehouse, warehouse),
		as_dict=True,
	)
	return [
		{
			"voucher": r.name,
			"rate": max(abs(flt(r.basic_rate)), abs(flt(r.valuation_rate))),
			"qty": flt(r.qty),
			"posting_date": str(r.posting_date),
		}
		for r in rows or []
	]


def _stock_entry_summary(voucher):
	import frappe

	if not frappe.db.exists("Stock Entry", voucher):
		return None
	se = frappe.db.get_value(
		"Stock Entry",
		voucher,
		["name", "purpose", "posting_date", "posting_time", "work_order", "company", "docstatus"],
		as_dict=True,
	)
	return dict(se) if se else None


def _gl_summary(voucher):
	try:
		from erpnext_extensions.iran_accounting.historical_stock.gl_integrity import classify_stock_entry_gl

		g = classify_stock_entry_gl(voucher)
		return {
			"gl_class": g.get("gl_class"),
			"difference": g.get("difference"),
			"eligible": g.get("eligible"),
			"sle_poisoned": g.get("sle_poisoned"),
		}
	except Exception as exc:
		return {"error": str(exc)}


def _batch_inward_rate(item, warehouse, batch):
	if not batch:
		return None
	import frappe

	r = frappe.db.sql(
		"""
		SELECT voucher_no, incoming_rate, valuation_rate, actual_qty, posting_datetime
		FROM `tabStock Ledger Entry`
		WHERE item_code=%s AND warehouse=%s AND batch_no=%s AND is_cancelled=0 AND actual_qty>0
		ORDER BY posting_datetime ASC
		LIMIT 1
		""",
		(item, warehouse, batch),
		as_dict=True,
	)
	if not r:
		return None
	s = r[0]
	return {
		"voucher": s.voucher_no,
		"rate": max(abs(flt(s.incoming_rate)), abs(flt(s.valuation_rate))),
		"qty": flt(s.actual_qty),
		"posting_datetime": str(s.posting_datetime),
	}


def _sabb_rows(voucher, item):
	import frappe

	if not frappe.db.exists("DocType", "Serial and Batch Bundle"):
		return []
	rows = frappe.db.sql(
		"""
		SELECT name, voucher_no, item_code, warehouse, avg_rate, total_qty, type_of_transaction
		FROM `tabSerial and Batch Bundle`
		WHERE voucher_no=%s AND item_code=%s AND docstatus<2
		LIMIT 10
		""",
		(voucher, item),
		as_dict=True,
	)
	return [dict(r) for r in rows or []]


def _version_history(voucher):
	import frappe

	rows = frappe.db.sql(
		"""
		SELECT name, ref_doctype, docname, data, creation, owner
		FROM `tabVersion`
		WHERE ref_doctype IN ('Stock Entry','Purchase Receipt','Stock Reconciliation')
		  AND docname=%s
		ORDER BY creation DESC
		LIMIT 5
		""",
		voucher,
		as_dict=True,
	)
	out = []
	for r in rows or []:
		out.append(
			{
				"name": r.name,
				"ref_doctype": r.ref_doctype,
				"creation": str(r.creation),
				"owner": r.owner,
				"data_head": (r.data or "")[:240],
			}
		)
	return out


def _audit_trail(voucher):
	import frappe

	if not frappe.db.exists("DocType", "Activity Log"):
		return []
	rows = frappe.db.sql(
		"""
		SELECT name, subject, status, creation, user
		FROM `tabActivity Log`
		WHERE reference_name=%s
		ORDER BY creation DESC
		LIMIT 8
		""",
		voucher,
		as_dict=True,
	)
	return [dict(r) for r in rows or []]


def _repair_log(voucher, item):
	import frappe

	if not frappe.db.exists("DocType", "Historical Repair Log"):
		# Fall back to campaign artifact table if present
		return []
	try:
		rows = frappe.db.sql(
			"""
			SELECT name, creation, status, topic
			FROM `tabHistorical Repair Log`
			WHERE voucher=%s OR item=%s
			ORDER BY creation DESC
			LIMIT 8
			""",
			(voucher, item),
			as_dict=True,
		)
		return [dict(r) for r in rows or []]
	except Exception:
		return []


def _collect_rate_sources(pkg: dict, row: dict) -> dict:
	"""Merge reconstruction sources already on the row with evidence probes."""
	sources = {}
	existing = row.get("reconstruction_sources") or row.get("sources") or {}
	if isinstance(existing, dict):
		for k, v in existing.items():
			if k == "bin":
				continue
			if abs(flt(v)) > 0:
				sources[k] = flt(v)
	# Evidence-derived
	prev = pkg.get("previous_sle") or {}
	if prev:
		rate = max(abs(flt(prev.get("valuation_rate"))), abs(flt(prev.get("incoming_rate"))))
		if rate > 0:
			sources.setdefault("previous_healthy_sle", rate)
	bi = pkg.get("batch_inward") or {}
	if bi and abs(flt(bi.get("rate"))) > 0:
		sources.setdefault("batch_inward", flt(bi["rate"]))
	prs = pkg.get("purchase_receipts") or []
	if prs and abs(flt(prs[0].get("rate"))) > 0:
		sources.setdefault("purchase_receipt", flt(prs[0]["rate"]))
	recos = pkg.get("stock_reconciliations") or []
	if recos and abs(flt(recos[0].get("rate"))) > 0:
		sources.setdefault("stock_reconciliation", flt(recos[0]["rate"]))
	mrs = pkg.get("material_receipts") or []
	if mrs and abs(flt(mrs[0].get("rate"))) > 0:
		sources.setdefault("transfer_source", flt(mrs[0]["rate"]))
	# Stamped expected as version-like hint only when source says version
	src = str(row.get("source") or row.get("source_of_truth") or "")
	exp = flt(row.get("expected") if row.get("expected") is not None else row.get("proposed_rate"))
	if abs(exp) > 0 and "version" in src:
		sources.setdefault("version", exp)
	elif abs(exp) > 0 and src and src not in sources:
		sources.setdefault(src.split("+")[0], exp)
	return sources
