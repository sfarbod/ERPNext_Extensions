# Copyright (c) 2026 — temporary validation helpers for I4 runbook (dev only).
from __future__ import annotations

import json
import os
from datetime import datetime

import frappe
from frappe.utils import flt

VOUCHER = "MAT-STE-2026-25791"
BASE = "/workspace/development/frappe-bench/apps/erpnext_extensions/.local-backups/i4_v5213_25791"


def _dump(subdir, name, data):
	d = os.path.join(BASE, subdir)
	os.makedirs(d, exist_ok=True)
	path = os.path.join(d, name)
	with open(path, "w", encoding="utf-8") as f:
		json.dump(data, f, indent=2, default=str, ensure_ascii=False)
	print(f"wrote {path}")
	return path


def collect_baseline():
	OUT = "baseline"
	meta = {
		"collected_at": datetime.utcnow().isoformat() + "Z",
		"voucher": VOUCHER,
		"site": frappe.local.site,
		"version": frappe.get_attr("erpnext_extensions.__version__"),
	}
	se = frappe.db.get_value(
		"Stock Entry",
		VOUCHER,
		[
			"name", "company", "purpose", "stock_entry_type", "work_order",
			"posting_date", "posting_time", "docstatus",
			"total_outgoing_value", "total_incoming_value", "value_difference",
			"modified", "creation",
		],
		as_dict=True,
	)
	items = frappe.db.sql(
		"""
		SELECT name, idx, item_code, s_warehouse, t_warehouse, qty, transfer_qty,
		       basic_rate, basic_amount, valuation_rate, amount,
		       serial_and_batch_bundle, batch_no, uom, conversion_factor
		FROM `tabStock Entry Detail`
		WHERE parent=%s ORDER BY idx
		""",
		VOUCHER,
		as_dict=True,
	)
	_dump(OUT, "01_stock_entry.json", {"stock_entry": se, "items": items})

	sles = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, posting_date, posting_time, posting_datetime,
		       voucher_type, voucher_no, voucher_detail_no, actual_qty, qty_after_transaction,
		       incoming_rate, outgoing_rate, valuation_rate, stock_value, stock_value_difference,
		       batch_no, serial_and_batch_bundle, company, is_cancelled, creation, modified
		FROM `tabStock Ledger Entry`
		WHERE voucher_no=%s
		ORDER BY posting_datetime, creation
		""",
		VOUCHER,
		as_dict=True,
	)
	_dump(OUT, "02_sle_voucher.json", sles)

	identities = []
	seen = set()
	for s in sles:
		key = (s.item_code, s.warehouse)
		if key in seen:
			continue
		seen.add(key)
		identities.append({"item_code": s.item_code, "warehouse": s.warehouse})

	identity_sles, bins, sabbs, sbes = [], [], [], []
	for ident in identities:
		item, wh = ident["item_code"], ident["warehouse"]
		chain = frappe.db.sql(
			"""
			SELECT name, voucher_no, posting_datetime, actual_qty, qty_after_transaction,
			       incoming_rate, outgoing_rate, valuation_rate, stock_value, stock_value_difference,
			       batch_no, serial_and_batch_bundle, is_cancelled
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			ORDER BY posting_datetime, creation
			""",
			(item, wh),
			as_dict=True,
		)
		identity_sles.append({"item_code": item, "warehouse": wh, "sle_count": len(chain), "sles": chain})
		bins.append(
			frappe.db.get_value(
				"Bin",
				{"item_code": item, "warehouse": wh},
				["name", "item_code", "warehouse", "actual_qty", "stock_value", "valuation_rate", "stock_uom"],
				as_dict=True,
			)
		)
		for bn in sorted({r.serial_and_batch_bundle for r in chain if r.serial_and_batch_bundle}):
			sabbs.append(
				frappe.db.get_value(
					"Serial and Batch Bundle",
					bn,
					[
						"name", "item_code", "warehouse", "type_of_transaction", "total_qty",
						"avg_rate", "total_amount", "voucher_no", "docstatus",
					],
					as_dict=True,
				)
			)
			sbes.append(
				{
					"bundle": bn,
					"entries": frappe.db.sql(
						"""
						SELECT name, idx, batch_no, qty, incoming_rate, outgoing_rate,
						       stock_value_difference, warehouse
						FROM `tabSerial and Batch Entry` WHERE parent=%s ORDER BY idx
						""",
						bn,
						as_dict=True,
					),
				}
			)

	_dump(OUT, "03_identity_sle_chains.json", identity_sles)
	_dump(OUT, "04_bins.json", bins)
	_dump(OUT, "05_sabb.json", sabbs)
	_dump(OUT, "06_sbe.json", sbes)

	gl = frappe.db.sql(
		"""
		SELECT name, account, debit, credit, against, cost_center, is_cancelled,
		       posting_date, company
		FROM `tabGL Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s
		ORDER BY creation
		""",
		VOUCHER,
		as_dict=True,
	)
	_dump(
		OUT,
		"07_gl.json",
		{
			"rows": gl,
			"debit": sum(flt(r.debit) for r in gl if not flt(r.is_cancelled)),
			"credit": sum(flt(r.credit) for r in gl if not flt(r.is_cancelled)),
		},
	)

	failed = []
	for ident in identities:
		failed.extend(
			frappe.db.sql(
				"""
				SELECT name, item_code, warehouse, voucher_no, voucher_type, posting_date,
				       status, error_log, based_on, company, modified
				FROM `tabRepost Item Valuation`
				WHERE item_code=%s AND warehouse=%s AND status='Failed' AND docstatus=1
				ORDER BY modified DESC LIMIT 50
				""",
				(ident["item_code"], ident["warehouse"]),
				as_dict=True,
			)
		)
	_dump(OUT, "08_failed_riv.json", failed)

	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
		classify_i4_row,
		find_patient_zero_for_voucher,
		identity_health,
		root_cause_explorer,
	)
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin

	_dump(OUT, "09_patient_zero.json", find_patient_zero_for_voucher(VOUCHER))
	_dump(OUT, "10_root_cause_explorer.json", root_cause_explorer(VOUCHER))
	_dump(OUT, "11_sle_scan_i4.json", scan_sle_bin(voucher=VOUCHER, repair_class="I4_LEFTOVER_REPAIR", limit=50))
	_dump(OUT, "12_identity_health.json", [identity_health(i["item_code"], i["warehouse"]) for i in identities])
	_dump(OUT, "13_i4_classify.json", [classify_i4_row(i["item_code"], i["warehouse"], voucher=VOUCHER) for i in identities])

	print("running scan_all baseline…")
	_dump(OUT, "14_scan_all_dashboard.json", run_full_integrity_scan(company=se.company if se else None))

	if identities:
		primary = identities[0]
		prev = frappe.db.sql(
			"""
			SELECT name, voucher_no, posting_datetime, actual_qty, qty_after_transaction,
			       valuation_rate, stock_value, stock_value_difference, incoming_rate, outgoing_rate
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			  AND posting_datetime < (
			    SELECT MIN(posting_datetime) FROM `tabStock Ledger Entry`
			    WHERE voucher_no=%s AND item_code=%s AND warehouse=%s AND is_cancelled=0
			  )
			ORDER BY posting_datetime DESC, creation DESC LIMIT 3
			""",
			(primary["item_code"], primary["warehouse"], VOUCHER, primary["item_code"], primary["warehouse"]),
			as_dict=True,
		)
		_dump(OUT, "15_prev_healthy_anchor.json", prev)

	meta["identities"] = identities
	meta["sle_on_voucher"] = len(sles)
	meta["gl_rows"] = len(gl)
	meta["failed_riv"] = len(failed)
	_dump(OUT, "00_meta.json", meta)
	return meta


def dry_run_repair():
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import dry_run_i4_repair, classify_i4_row
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin

	scan = scan_sle_bin(voucher=VOUCHER, repair_class="I4_LEFTOVER_REPAIR", limit=20)
	rows = [r for r in (scan.get("rows") or []) if r.get("voucher") == VOUCHER]
	if not rows:
		# fallback classify
		sles = frappe.db.sql(
			"SELECT item_code, warehouse FROM `tabStock Ledger Entry` WHERE voucher_no=%s AND is_cancelled=0 LIMIT 1",
			VOUCHER,
			as_dict=True,
		)
		rows = [classify_i4_row(sles[0].item_code, sles[0].warehouse, voucher=VOUCHER)]
	result = dry_run_i4_repair(rows)
	_dump("dry_run", "dry_run_result.json", {"scan_row": rows[0] if rows else None, "result": result})
	return result


def apply_repair():
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import repair_i4_selected, classify_i4_row
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin

	scan = scan_sle_bin(voucher=VOUCHER, repair_class="I4_LEFTOVER_REPAIR", limit=20)
	rows = [r for r in (scan.get("rows") or []) if r.get("voucher") == VOUCHER and r.get("planner_status") == "READY_I4"]
	if not rows:
		sles = frappe.db.sql(
			"SELECT item_code, warehouse FROM `tabStock Ledger Entry` WHERE voucher_no=%s AND is_cancelled=0 LIMIT 1",
			VOUCHER,
			as_dict=True,
		)
		row = classify_i4_row(sles[0].item_code, sles[0].warehouse, voucher=VOUCHER)
		rows = [row]
	# Require backup confirmation already done by operator authorization in this chat.
	result = repair_i4_selected(rows, dry_run=False)
	_dump("apply", "apply_result.json", result)
	return result


def collect_after(label="validation"):
	"""Re-collect same snapshots as baseline into validation/."""
	# Temporarily redirect by calling baseline collectors into validation folder
	global _FORCE_SUBDIR
	# simpler: duplicate key exports
	from erpnext_extensions.iran_accounting.historical_stock.i4_repair import (
		classify_i4_row,
		find_patient_zero_for_voucher,
		identity_health,
		root_cause_explorer,
	)
	from erpnext_extensions.iran_accounting.historical_stock.scan import run_full_integrity_scan
	from erpnext_extensions.iran_accounting.historical_stock.sle_bin import scan_sle_bin

	OUT = label
	se = frappe.db.get_value(
		"Stock Entry",
		VOUCHER,
		[
			"name", "company", "purpose", "stock_entry_type", "work_order",
			"posting_date", "posting_time", "docstatus",
			"total_outgoing_value", "total_incoming_value", "value_difference",
			"modified", "creation",
		],
		as_dict=True,
	)
	items = frappe.db.sql(
		"""
		SELECT name, idx, item_code, s_warehouse, t_warehouse, qty, transfer_qty,
		       basic_rate, basic_amount, valuation_rate, amount,
		       serial_and_batch_bundle, batch_no
		FROM `tabStock Entry Detail` WHERE parent=%s ORDER BY idx
		""",
		VOUCHER,
		as_dict=True,
	)
	_dump(OUT, "01_stock_entry.json", {"stock_entry": se, "items": items})
	sles = frappe.db.sql(
		"""
		SELECT name, item_code, warehouse, posting_datetime, actual_qty, qty_after_transaction,
		       incoming_rate, outgoing_rate, valuation_rate, stock_value, stock_value_difference,
		       batch_no, serial_and_batch_bundle, is_cancelled
		FROM `tabStock Ledger Entry` WHERE voucher_no=%s ORDER BY posting_datetime, creation
		""",
		VOUCHER,
		as_dict=True,
	)
	_dump(OUT, "02_sle_voucher.json", sles)
	identities = []
	seen = set()
	for s in sles:
		key = (s.item_code, s.warehouse)
		if key in seen:
			continue
		seen.add(key)
		identities.append({"item_code": s.item_code, "warehouse": s.warehouse})

	identity_sles, bins = [], []
	for ident in identities:
		item, wh = ident["item_code"], ident["warehouse"]
		chain = frappe.db.sql(
			"""
			SELECT name, voucher_no, posting_datetime, actual_qty, qty_after_transaction,
			       incoming_rate, outgoing_rate, valuation_rate, stock_value, stock_value_difference,
			       batch_no, serial_and_batch_bundle
			FROM `tabStock Ledger Entry`
			WHERE item_code=%s AND warehouse=%s AND is_cancelled=0
			ORDER BY posting_datetime, creation
			""",
			(item, wh),
			as_dict=True,
		)
		identity_sles.append({"item_code": item, "warehouse": wh, "sle_count": len(chain), "sles": chain})
		bins.append(
			frappe.db.get_value(
				"Bin",
				{"item_code": item, "warehouse": wh},
				["name", "item_code", "warehouse", "actual_qty", "stock_value", "valuation_rate"],
				as_dict=True,
			)
		)
	_dump(OUT, "03_identity_sle_chains.json", identity_sles)
	_dump(OUT, "04_bins.json", bins)

	gl = frappe.db.sql(
		"""
		SELECT name, account, debit, credit, is_cancelled, posting_date
		FROM `tabGL Entry`
		WHERE voucher_type='Stock Entry' AND voucher_no=%s ORDER BY creation
		""",
		VOUCHER,
		as_dict=True,
	)
	_dump(
		OUT,
		"07_gl.json",
		{
			"rows": gl,
			"debit": sum(flt(r.debit) for r in gl if not flt(r.is_cancelled)),
			"credit": sum(flt(r.credit) for r in gl if not flt(r.is_cancelled)),
		},
	)

	failed = []
	for ident in identities:
		failed.extend(
			frappe.db.sql(
				"""
				SELECT name, item_code, warehouse, voucher_no, status, error_log, modified
				FROM `tabRepost Item Valuation`
				WHERE item_code=%s AND warehouse=%s AND status='Failed' AND docstatus=1
				ORDER BY modified DESC LIMIT 50
				""",
				(ident["item_code"], ident["warehouse"]),
				as_dict=True,
			)
		)
	_dump(OUT, "08_failed_riv.json", failed)
	_dump(OUT, "09_patient_zero.json", find_patient_zero_for_voucher(VOUCHER))
	_dump(OUT, "10_root_cause_explorer.json", root_cause_explorer(VOUCHER))
	_dump(OUT, "11_sle_scan_i4.json", scan_sle_bin(voucher=VOUCHER, repair_class="I4_LEFTOVER_REPAIR", limit=50))
	_dump(OUT, "12_identity_health.json", [identity_health(i["item_code"], i["warehouse"]) for i in identities])
	_dump(OUT, "13_i4_classify.json", [classify_i4_row(i["item_code"], i["warehouse"], voucher=VOUCHER) for i in identities])
	print("running scan_all after…")
	_dump(OUT, "14_scan_all_dashboard.json", run_full_integrity_scan(company=se.company if se else None))
	_dump(
		OUT,
		"00_meta.json",
		{
			"collected_at": datetime.utcnow().isoformat() + "Z",
			"voucher": VOUCHER,
			"identities": identities,
			"version": frappe.get_attr("erpnext_extensions.__version__"),
		},
	)
	return {"identities": identities, "sle_count": len(sles)}
