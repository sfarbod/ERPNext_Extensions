# Copyright (c) 2026, ERPNext Extensions contributors
"""Atomic equivalent recreate of shared Material Transfer logistics (v5.5.0).

DEDICATED / SHARED_RECREATE_SAFE / SHARED_BLOCKED

MVP: only Stock Entry purpose=Material Transfer may be SHARED_RECREATE_SAFE.
No row-split. Unrelated co-moved items do not seed repair dependency traversal.
Cancel safety must hold for the repair cancel-set alone — if foreign docs would
be required, classify SHARED_BLOCKED.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any, Iterator

import frappe
from frappe.utils import cint, flt, get_datetime


DEDICATED = "DEDICATED"
SHARED_RECREATE_SAFE = "SHARED_RECREATE_SAFE"
SHARED_BLOCKED = "SHARED_BLOCKED"
UNRELATED = "UNRELATED"

_SHARED_RECREATE_PURPOSES = {"Material Transfer"}


@contextmanager
def _owned_cancel_probe_savepoint(sp: str) -> Iterator[None]:
	"""Own the DB transaction boundary for a cancel probe.

	Nested ``frappe.db.rollback()`` (full) or ``frappe.db.commit()`` destroys
	MySQL/MariaDB SAVEPOINTs. The probe's ``finally`` then raises
	OperationalError 1305 ("SAVEPOINT ... does not exist") and aborts Scan.

	During the probe:
	- full rollback is demoted to rollback-to-our-savepoint
	- commit is blocked (Scan must remain read-only from the user's view)
	"""
	frappe.db.savepoint(sp)
	orig_rollback = frappe.db.rollback
	orig_commit = frappe.db.commit

	def _guarded_commit(*_a, **_k):
		frappe.throw(
			"commit blocked during shared-logistics cancel probe",
			frappe.ValidationError,
		)

	def _guarded_rollback(*, save_point=None, chain=False):
		if save_point:
			return orig_rollback(save_point=save_point, chain=chain)
		# Nested full rollback would drop our SAVEPOINT; demote to it instead.
		return orig_rollback(save_point=sp)

	frappe.db.commit = _guarded_commit  # type: ignore[method-assign]
	frappe.db.rollback = _guarded_rollback  # type: ignore[method-assign]
	try:
		yield
	finally:
		frappe.db.commit = orig_commit  # type: ignore[method-assign]
		frappe.db.rollback = orig_rollback  # type: ignore[method-assign]
		try:
			orig_rollback(save_point=sp)
		except Exception:
			# Boundary already lost (raw SQL COMMIT/ROLLBACK, reconnect).
			# Full rollback restores a clean request transaction; never leave
			# probe mutations hanging. Caller treats probe as failed.
			try:
				orig_rollback()
			except Exception:
				pass
			raise


def detail_batch(row: dict) -> str:
	batch = (row.get("batch_no") or "").strip()
	if batch:
		return batch
	bundle = row.get("serial_and_batch_bundle")
	if not bundle:
		return ""
	bb = frappe.db.sql(
		"select batch_no from `tabSerial and Batch Entry` where parent=%s and ifnull(batch_no,'')!='' limit 1",
		bundle,
	)
	return bb[0][0] if bb else ""


def se_lines(name: str) -> list[dict]:
	rows = frappe.db.sql(
		"""
		select name, idx, item_code, batch_no, serial_and_batch_bundle, qty, transfer_qty,
		       uom, stock_uom, conversion_factor, s_warehouse, t_warehouse,
		       basic_rate, valuation_rate, expense_account, cost_center, project,
		       job_card_item, allow_zero_valuation_rate
		from `tabStock Entry Detail` where parent=%s order by idx
		""",
		name,
		as_dict=1,
	)
	for r in rows:
		r.batch_no = detail_batch(r)
	return rows


def batch_qty(item_code: str, batch_no: str, warehouse: str) -> float:
	"""Effective batch qty at warehouse (direct SLE batch_no or via SABB)."""
	if not item_code or not warehouse:
		return 0.0
	if not batch_no:
		return flt(
			frappe.db.sql(
				"""
				select coalesce(sum(actual_qty),0) from `tabStock Ledger Entry`
				where item_code=%s and warehouse=%s and is_cancelled=0
				  and ifnull(batch_no,'')=''
				""",
				(item_code, warehouse),
			)[0][0]
		)
	direct = flt(
		frappe.db.sql(
			"""
			select coalesce(sum(actual_qty),0) from `tabStock Ledger Entry`
			where item_code=%s and warehouse=%s and batch_no=%s and is_cancelled=0
			""",
			(item_code, warehouse, batch_no),
		)[0][0]
	)
	via = flt(
		frappe.db.sql(
			"""
			select coalesce(sum(sle.actual_qty),0)
			from `tabStock Ledger Entry` sle
			join `tabSerial and Batch Entry` sbe
			  on sbe.parent=sle.serial_and_batch_bundle
			where sle.item_code=%s and sle.warehouse=%s and sbe.batch_no=%s
			  and sle.is_cancelled=0 and ifnull(sle.batch_no,'')=''
			""",
			(item_code, warehouse, batch_no),
		)[0][0]
	)
	return direct + via


def snapshot_stock_entry(name: str) -> dict[str, Any]:
	"""In-memory reconstruction snapshot (not a persistent DocType)."""
	se = frappe.get_doc("Stock Entry", name)
	header = {
		"name": se.name,
		"purpose": se.purpose,
		"stock_entry_type": se.stock_entry_type,
		"company": se.company,
		"posting_date": str(se.posting_date),
		"posting_time": str(se.posting_time),
		"set_posting_time": cint(se.set_posting_time),
		"job_card": se.job_card,
		"work_order": se.work_order,
		"from_warehouse": se.from_warehouse,
		"to_warehouse": se.to_warehouse,
		"project": se.project,
		"remarks": se.remarks,
		"custom_rahkaran_no": getattr(se, "custom_rahkaran_no", None),
	}
	rows = []
	for ln in se.items:
		batch = detail_batch(ln.as_dict())
		rows.append(
			{
				"idx": ln.idx,
				"item_code": ln.item_code,
				"qty": flt(ln.qty),
				"transfer_qty": flt(ln.transfer_qty or ln.qty),
				"uom": ln.uom,
				"stock_uom": ln.stock_uom,
				"conversion_factor": flt(ln.conversion_factor) or 1.0,
				"s_warehouse": ln.s_warehouse,
				"t_warehouse": ln.t_warehouse,
				"batch_no": batch,
				"cost_center": ln.cost_center,
				"project": getattr(ln, "project", None),
				"expense_account": ln.expense_account,
				"basic_rate": flt(ln.basic_rate),
				"valuation_rate": flt(ln.valuation_rate),
				"job_card_item": getattr(ln, "job_card_item", None),
			}
		)
	sle_fp = frappe.db.sql(
		"""
		select item_code, warehouse, sum(actual_qty) q, sum(stock_value_difference) v
		from `tabStock Ledger Entry`
		where voucher_no=%s and is_cancelled=0
		group by item_code, warehouse order by item_code, warehouse
		""",
		name,
		as_dict=1,
	)
	gl_fp = frappe.db.sql(
		"""
		select account, sum(debit) d, sum(credit) c
		from `tabGL Entry`
		where voucher_type='Stock Entry' and voucher_no=%s and is_cancelled=0
		group by account order by account
		""",
		name,
		as_dict=1,
	)
	return {
		"header": header,
		"rows": rows,
		"sle_fingerprint": [
			(r.item_code, r.warehouse, flt(r.q), flt(r.v)) for r in sle_fp
		],
		"gl_fingerprint": [(r.account, flt(r.d), flt(r.c)) for r in gl_fp],
	}


def simulate_cancel_stock_ok(cancel_names_desc: list[str]) -> tuple[bool, str]:
	"""Cancel-safety via savepoint probe (authoritative ERPNext cancel).

	Does NOT absorb foreign documents. Always rolls back. Cached per request.
	"""
	names = list(cancel_names_desc or [])
	if not names:
		return True, "ok"
	cache = frappe.flags.setdefault("jc_shared_cancel_probe_cache", {})
	key = tuple(names)
	if key in cache:
		return cache[key]

	for name in names:
		purpose = frappe.db.get_value("Stock Entry", name, "purpose")
		if purpose not in _SHARED_RECREATE_PURPOSES:
			cache[key] = (False, f"{name}: purpose {purpose} not supported for shared recreate")
			return cache[key]
		if cint(frappe.db.get_value("Stock Entry", name, "docstatus")) != 1:
			cache[key] = (False, f"{name}: not submitted")
			return cache[key]

	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.sync_valuation import (
		suppress_auto_riv,
	)

	# Stable unique savepoint (avoid hash randomization / collisions).
	sp = f"jc_shared_cancel_{uuid.uuid4().hex[:12]}"
	ok = True
	reason = "ok"
	# Cancel probes may raise expected Insufficient Stock for bridge planning.
	# Capture and restore message_log so probe evidence does not leak as a
	# user-facing modal (PROBE EVIDENCE ≠ TERMINAL BLOCKER).
	prior_messages = list(getattr(frappe.local, "message_log", None) or [])
	try:
		with _owned_cancel_probe_savepoint(sp):
			with suppress_auto_riv():
				for name in names:
					doc = frappe.get_doc("Stock Entry", name)
					doc.flags.ignore_permissions = True
					doc.cancel()
	except Exception as exc:
		ok = False
		# Strip HTML anchors from Core stock messages for compact blockers.
		msg = frappe.as_unicode(exc)
		msg = frappe.utils.strip_html(msg) if hasattr(frappe.utils, "strip_html") else msg
		reason = f"{names}: {msg}"[:500]
	finally:
		frappe.local.message_log = prior_messages

	cache[key] = (ok, reason)
	return cache[key]


def classify_shared_logistics(
	name: str,
	fg_keys: set[tuple[str, str]],
	cancel_set_desc: list[str] | None = None,
) -> dict[str, Any]:
	"""Classify one logistics SE for repair participation."""
	meta = frappe.db.get_value(
		"Stock Entry",
		name,
		[
			"name",
			"purpose",
			"docstatus",
			"posting_date",
			"posting_time",
			"job_card",
			"work_order",
			"company",
		],
		as_dict=1,
	) or frappe._dict(name=name)
	lines = se_lines(name)
	keys = {(ln.item_code, ln.batch_no) for ln in lines if ln.item_code and ln.batch_no}
	fg_on = keys & fg_keys
	other = keys - fg_keys
	related_rows = [
		{
			"item_code": ln.item_code,
			"batch_no": ln.batch_no,
			"qty": flt(ln.qty),
			"s_warehouse": ln.s_warehouse,
			"t_warehouse": ln.t_warehouse,
			"affected": True,
		}
		for ln in lines
		if (ln.item_code, ln.batch_no) in fg_on
	]
	unrelated_rows = [
		{
			"item_code": ln.item_code,
			"batch_no": ln.batch_no,
			"qty": flt(ln.qty),
			"s_warehouse": ln.s_warehouse,
			"t_warehouse": ln.t_warehouse,
			"affected": False,
		}
		for ln in lines
		if (ln.item_code, ln.batch_no) in other
	]

	reason = ""
	if not fg_on:
		cls = UNRELATED
		reason = "No Manufacture FG Item×Batch on document"
	elif cint(meta.get("docstatus")) != 1:
		cls = SHARED_BLOCKED
		reason = "Not submitted"
	elif not other:
		cls = DEDICATED
		reason = "Only repair-scope FG movement"
	elif meta.get("purpose") not in _SHARED_RECREATE_PURPOSES:
		cls = SHARED_BLOCKED
		reason = f"Purpose {meta.get('purpose')} not supported for shared recreate MVP"
	else:
		# Shared Material Transfer — classify this document alone first.
		# Set-level safety is enforced by discover_downstream (all seeds must be safe).
		from erpnext_extensions.iran_accounting.job_card_stock_rebuild.manufacture_plan import (
			_se_sort_key,
		)

		alone_ok, alone_why = simulate_cancel_stock_ok([name])
		probe_set = sorted(set(cancel_set_desc or [name]), key=_se_sort_key, reverse=True)
		set_ok, set_why = (True, "ok")
		if len(probe_set) > 1:
			set_ok, set_why = simulate_cancel_stock_ok(probe_set)
		if alone_ok:
			cls = SHARED_RECREATE_SAFE
			if set_ok:
				reason = "Entire Material Transfer can be cancelled/recreated without foreign docs"
			else:
				reason = (
					"Document alone is cancel-safe, but sibling cancel-set is not: " + set_why
				)
		else:
			cls = SHARED_BLOCKED
			reason = alone_why

	# Downstream note for unrelated rows (informational; does not seed repair)
	for ur in unrelated_rows:
		ur["downstream_note"] = _unrelated_downstream_note(name, ur)

	return {
		"name": name,
		"purpose": meta.get("purpose"),
		"docstatus": meta.get("docstatus"),
		"posting_date": meta.get("posting_date"),
		"posting_time": meta.get("posting_time"),
		"job_card": meta.get("job_card"),
		"work_order": meta.get("work_order"),
		"shared_class": cls,
		"reason": reason,
		"related_rows": related_rows,
		"unrelated_rows": unrelated_rows,
		"n_lines": len(lines),
	}


def _unrelated_downstream_note(se_name: str, row: dict) -> str:
	"""Describe later outbounds of an unrelated row — audit only, not repair seed."""
	if not row.get("t_warehouse") or not row.get("batch_no"):
		return ""
	later = frappe.db.sql(
		"""
		select se.name, se.purpose, se.posting_date
		from `tabStock Entry Detail` sed
		join `tabStock Entry` se on se.name=sed.parent
		where se.docstatus=1 and sed.item_code=%s and ifnull(sed.batch_no,'')=%s
		  and sed.s_warehouse=%s and se.name!=%s
		order by se.posting_date, se.posting_time
		limit 5
		""",
		(row["item_code"], row["batch_no"], row["t_warehouse"], se_name),
		as_dict=1,
	)
	if not later:
		return "no later outbound from target"
	return "later: " + ", ".join(f"{x.name}({x.purpose})" for x in later)


def physical_row_key(row: dict) -> tuple:
	return (
		row.get("item_code"),
		row.get("batch_no") or "",
		flt(row.get("qty")),
		row.get("s_warehouse") or "",
		row.get("t_warehouse") or "",
		str(row.get("posting_date") or ""),
		str(row.get("posting_time") or ""),
	)


def compare_recreated_equivalence(
	original_snap: dict,
	recreated_name: str,
	fg_keys: set[tuple[str, str]],
) -> dict[str, Any]:
	"""Compare original snapshot vs recreated SE for physical + valuation classes."""
	rec = snapshot_stock_entry(recreated_name)
	oh, rh = original_snap["header"], rec["header"]
	errors = []
	# Header posting
	if oh.get("posting_date") != rh.get("posting_date") or str(oh.get("posting_time")) != str(
		rh.get("posting_time")
	):
		errors.append(
			f"posting datetime drift {oh.get('posting_date')} {oh.get('posting_time')} "
			f"→ {rh.get('posting_date')} {rh.get('posting_time')}"
		)
	if oh.get("purpose") != rh.get("purpose"):
		errors.append(f"purpose {oh.get('purpose')} → {rh.get('purpose')}")

	orig_rows = list(original_snap["rows"])
	rec_rows = list(rec["rows"])
	if len(orig_rows) != len(rec_rows):
		errors.append(f"row count {len(orig_rows)} → {len(rec_rows)}")

	row_reports = []
	# Match by idx then item×batch×qty
	used = set()
	for o in orig_rows:
		match = None
		for j, r in enumerate(rec_rows):
			if j in used:
				continue
			if cint(o.get("idx")) == cint(r.get("idx")) and o.get("item_code") == r.get("item_code"):
				match = r
				used.add(j)
				break
		if not match:
			for j, r in enumerate(rec_rows):
				if j in used:
					continue
				if (
					o.get("item_code") == r.get("item_code")
					and (o.get("batch_no") or "") == (r.get("batch_no") or "")
					and abs(flt(o.get("qty")) - flt(r.get("qty"))) <= 1e-6
				):
					match = r
					used.add(j)
					break
		affected = (o.get("item_code"), o.get("batch_no") or "") in fg_keys
		phys_ok = False
		val_class = "UNCHANGED"
		if match:
			phys_ok = (
				o.get("item_code") == match.get("item_code")
				and (o.get("batch_no") or "") == (match.get("batch_no") or "")
				and abs(flt(o.get("qty")) - flt(match.get("qty"))) <= 1e-6
				and (o.get("s_warehouse") or "") == (match.get("s_warehouse") or "")
				and (o.get("t_warehouse") or "") == (match.get("t_warehouse") or "")
				and abs(flt(o.get("conversion_factor") or 1) - flt(match.get("conversion_factor") or 1))
				<= 1e-9
			)
			if not phys_ok:
				errors.append(
					f"PHYSICAL mismatch {o.get('item_code')}/{o.get('batch_no')}: "
					f"{o.get('qty')} {o.get('s_warehouse')}→{o.get('t_warehouse')} vs "
					f"{match.get('qty')} {match.get('s_warehouse')}→{match.get('t_warehouse')}"
				)
			# Valuation class from SLE value on item (approx via rates)
			o_rate = flt(o.get("valuation_rate") or o.get("basic_rate"))
			r_rate = flt(match.get("valuation_rate") or match.get("basic_rate"))
			if abs(o_rate - r_rate) <= 1e-4:
				val_class = "UNCHANGED"
			elif affected:
				val_class = "EXPECTED_REVALUATION"
			else:
				val_class = "UNEXPECTED_DRIFT"
				# Unrelated valuation drift — fail
				errors.append(
					f"UNRELATED_VALUATION_DRIFT {o.get('item_code')}/{o.get('batch_no')}: "
					f"{o_rate} → {r_rate}"
				)
		else:
			errors.append(f"missing recreated row for {o.get('item_code')}/{o.get('batch_no')}")
		row_reports.append(
			{
				"item_code": o.get("item_code"),
				"batch_no": o.get("batch_no"),
				"qty": o.get("qty"),
				"affected": affected,
				"physical_equivalent": "YES" if phys_ok else "NO",
				"valuation": val_class,
			}
		)

	# SLE qty fingerprint (physical)
	def sle_qty_map(fp):
		return {(i, w): q for i, w, q, _v in fp}

	o_sle = sle_qty_map(original_snap.get("sle_fingerprint") or [])
	r_sle = sle_qty_map(rec.get("sle_fingerprint") or [])
	if set(o_sle.keys()) != set(r_sle.keys()) or any(
		abs(o_sle[k] - r_sle.get(k, 0)) > 1e-6 for k in o_sle
	):
		errors.append(f"SLE qty fingerprint mismatch {o_sle} vs {r_sle}")

	return {
		"ok": not errors,
		"errors": errors,
		"original": original_snap["header"]["name"],
		"recreated": recreated_name,
		"rows": row_reports,
	}


def foreign_docs_excluded(plan_logistics_names: list[str], forbidden: list[str]) -> bool:
	s = set(plan_logistics_names or [])
	return not (s & set(forbidden))
