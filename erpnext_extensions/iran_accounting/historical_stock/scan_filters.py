# Copyright (c) 2026, ERPNext Extensions contributors
"""Shared server-side scan scope for Historical Repair.

Every Scan API must honor these filters in SQL. Do not fetch the whole ledger
and filter in JavaScript.
"""

from __future__ import annotations

from typing import Any


def normalize_scope(
	*,
	company=None,
	voucher=None,
	item_code=None,
	warehouse=None,
	batch=None,
	serial_and_batch_bundle=None,
	work_order=None,
	from_date=None,
	to_date=None,
	repair_class=None,
	planner_status=None,
	patient_zero=None,
) -> dict[str, Any]:
	"""Return cleaned scope values (empty string → None)."""

	def _one(v):
		if v is None:
			return None
		if isinstance(v, str):
			v = v.strip()
			return v or None
		return v

	return {
		"company": _one(company),
		"voucher": _one(voucher),
		"item_code": _one(item_code),
		"warehouse": _one(warehouse),
		"batch": _one(batch),
		"serial_and_batch_bundle": _one(serial_and_batch_bundle),
		"work_order": _one(work_order),
		"from_date": _one(from_date),
		"to_date": _one(to_date),
		"repair_class": _one(repair_class),
		"planner_status": _one(planner_status),
		"patient_zero": _one(patient_zero),
	}


def append_sle_scope(
	conds: list[str],
	args: list,
	scope: dict,
	*,
	alias: str = "sle",
	date_field: str = "posting_date",
	join_se: bool = False,
) -> tuple[list[str], list, str]:
	"""Append Stock Ledger Entry scope. Returns (conds, args, optional JOIN SQL)."""
	join_sql = ""
	if scope.get("company"):
		conds.append(f"{alias}.company=%s")
		args.append(scope["company"])
	if scope.get("voucher"):
		conds.append(f"{alias}.voucher_no=%s")
		args.append(scope["voucher"])
	if scope.get("item_code"):
		conds.append(f"{alias}.item_code=%s")
		args.append(scope["item_code"])
	if scope.get("warehouse"):
		conds.append(f"{alias}.warehouse=%s")
		args.append(scope["warehouse"])
	if scope.get("serial_and_batch_bundle"):
		conds.append(f"{alias}.serial_and_batch_bundle=%s")
		args.append(scope["serial_and_batch_bundle"])
	if scope.get("from_date"):
		conds.append(f"{alias}.{date_field}>=%s")
		args.append(scope["from_date"])
	if scope.get("to_date"):
		conds.append(f"{alias}.{date_field}<=%s")
		args.append(scope["to_date"])
	if scope.get("batch"):
		conds.append(
			f"(IFNULL({alias}.batch_no,'')=%s OR EXISTS ("
			f"SELECT 1 FROM `tabSerial and Batch Entry` sbe "
			f"WHERE sbe.parent={alias}.serial_and_batch_bundle AND sbe.batch_no=%s))"
		)
		args.extend([scope["batch"], scope["batch"]])
	if scope.get("work_order"):
		join_sql = (
			f" LEFT JOIN `tabStock Entry` se_scope ON se_scope.name={alias}.voucher_no "
			f"AND {alias}.voucher_type='Stock Entry'"
		)
		conds.append("se_scope.work_order=%s")
		args.append(scope["work_order"])
		join_se = True
	elif join_se and "se_scope" not in join_sql and "JOIN `tabStock Entry`" not in " ".join(conds):
		pass
	return conds, args, join_sql


def append_stock_entry_scope(conds: list[str], args: list, scope: dict, *, se_alias: str = "se", sed_alias: str = "sed") -> None:
	"""Append Stock Entry / Detail scope (zero-rate / manufacture scans)."""
	if scope.get("company"):
		conds.append(f"{se_alias}.company=%s")
		args.append(scope["company"])
	if scope.get("voucher"):
		conds.append(f"{se_alias}.name=%s")
		args.append(scope["voucher"])
	if scope.get("item_code"):
		conds.append(f"{sed_alias}.item_code=%s")
		args.append(scope["item_code"])
	if scope.get("warehouse"):
		conds.append(f"({sed_alias}.s_warehouse=%s OR {sed_alias}.t_warehouse=%s)")
		args.extend([scope["warehouse"], scope["warehouse"]])
	if scope.get("batch"):
		conds.append(
			f"(IFNULL({sed_alias}.batch_no,'')=%s OR EXISTS ("
			f"SELECT 1 FROM `tabSerial and Batch Entry` sbe "
			f"WHERE sbe.parent={sed_alias}.serial_and_batch_bundle AND sbe.batch_no=%s))"
		)
		args.extend([scope["batch"], scope["batch"]])
	if scope.get("serial_and_batch_bundle"):
		conds.append(f"{sed_alias}.serial_and_batch_bundle=%s")
		args.append(scope["serial_and_batch_bundle"])
	if scope.get("work_order"):
		conds.append(f"{se_alias}.work_order=%s")
		args.append(scope["work_order"])
	if scope.get("from_date"):
		conds.append(f"{se_alias}.posting_date>=%s")
		args.append(scope["from_date"])
	if scope.get("to_date"):
		conds.append(f"{se_alias}.posting_date<=%s")
		args.append(scope["to_date"])


def filter_rows_by_planner(
	rows: list[dict],
	*,
	repair_class=None,
	planner_status=None,
	patient_zero=None,
) -> list[dict]:
	"""Post-plan filters that require stamped planner fields (indexed SQL already applied)."""
	out = []
	for row in rows or []:
		if repair_class and str(row.get("repair_class") or "") != str(repair_class):
			continue
		if planner_status and str(row.get("planner_status") or "") != str(planner_status):
			continue
		if patient_zero:
			pz = row.get("patient_zero")
			pz_name = pz.get("voucher_no") if isinstance(pz, dict) else pz
			root = row.get("root_blocker") or row.get("root_patient_zero")
			if str(pz_name or "") != str(patient_zero) and str(root or "") != str(patient_zero):
				continue
		out.append(row)
	return out
