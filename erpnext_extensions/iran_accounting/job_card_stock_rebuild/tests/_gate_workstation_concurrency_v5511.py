# Copyright (c) 2026, ERPNext Extensions contributors
"""Restored-fixture gate: Dry Run + Apply with concurrent Workstation updates."""

from __future__ import annotations

import frappe
import pymysql
from frappe.utils import flt, now_datetime

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests._controlled_apply_08760 import (
	DISP,
	_plan_input,
	rescan,
)

JC = "PO-JOB08760"
WS = "Packaging Rooms"
ITEM = "13200544"


def _independent_ws_update(status: str) -> None:
	conf = frappe.conf
	conn = pymysql.connect(
		host=conf.get("db_host") or "127.0.0.1",
		user=conf.get("db_user") or conf.get("db_name"),
		password=conf.get("db_password") or "",
		database=conf.get("db_name"),
		port=int(conf.get("db_port") or 3306),
		autocommit=True,
		charset="utf8mb4",
	)
	try:
		with conn.cursor() as cur:
			cur.execute(
				"UPDATE `tabWorkstation` SET `status`=%s, `modified`=%s WHERE `name`=%s",
				(status, now_datetime(), WS),
			)
	finally:
		conn.close()


def _ws_status() -> str:
	frappe.db.rollback()
	return frappe.db.sql("select status from `tabWorkstation` where name=%s", WS)[0][0]


def run_dry_run_gate():
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import run_repair

	_independent_ws_update("Off")
	hits = {"n": 0}

	def progress(code, **extra):
		if code in ("T02", "T04", "T08", "T10", "T11_T12", "T13"):
			hits["n"] += 1
			_independent_ws_update("Idle")

	pre = rescan()
	if not pre.get("apply_allowed"):
		return {"ok": False, "phase": "precheck", "pre": pre, "ws_status": _ws_status()}
	result = run_repair(
		JC,
		plan_input=_plan_input(pre["fingerprint"]),
		dry_run=True,
		progress_cb=progress,
	)
	return {
		"ok": bool(result.get("ok")) and result.get("status") == "DRY_RUN_PASS",
		"status": result.get("status"),
		"error": result.get("error"),
		"mutated": result.get("mutated"),
		"committed": result.get("committed"),
		"ws_status": _ws_status(),
		"ws_survived": _ws_status() == "Idle",
		"concurrent_hits": hits["n"],
		"no_1020": "1020" not in str(result.get("error") or ""),
		"canonical": result.get("canonical_name"),
		"pre": {"issued": pre["row"]["issued"], "remaining": pre["row"]["remaining_wip"]},
	}


def run_apply_gate():
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.atomic_repair import run_repair
	from erpnext_extensions.iran_accounting.job_card_stock_rebuild.golden_rule import scan_golden_rule

	_independent_ws_update("Off")
	hits = {"n": 0}

	def progress(code, **extra):
		if code in ("T02", "T04", "T08", "T10", "T11_T12", "T13", "T17"):
			hits["n"] += 1
			_independent_ws_update("Production")

	pre = rescan()
	if not pre.get("apply_allowed"):
		return {"ok": False, "phase": "precheck", "pre": pre, "ws_status": _ws_status()}
	result = run_repair(
		JC,
		plan_input=_plan_input(pre["fingerprint"]),
		dry_run=False,
		confirm=1,
		progress_cb=progress,
	)
	frappe.db.rollback()
	scan = scan_golden_rule(JC)
	row = next((r for r in scan["rows"] if r["item_code"] == ITEM), {})
	mfg = (scan.get("manufactures") or [{}])[0]
	jci_ok = None
	total = 0
	if mfg.get("name"):
		null_jci, total = frappe.db.sql(
			"""
			select
			  sum(case when ifnull(sed.job_card_item,'')='' then 1 else 0 end),
			  count(*)
			from `tabStock Entry Detail` sed
			inner join `tabJob Card Item` jci
			  on jci.parent=%s and jci.item_code=sed.item_code
			where sed.parent=%s and sed.docstatus=1
			  and ifnull(sed.s_warehouse,'')!=''
			  and ifnull(sed.is_finished_item,0)=0
			""",
			(JC, mfg["name"]),
		)[0]
		jci_ok = int(null_jci or 0) == 0 and int(total or 0) > 0
	custom6 = frappe.db.get_value(
		"Server Script",
		"Custom 6 - Manufacturing Integrity Validator",
		"disabled",
	)
	ws = _ws_status()
	return {
		"ok": bool(result.get("ok")) and result.get("status") == "APPLY_PASS",
		"status": result.get("status"),
		"error": result.get("error"),
		"mutated": result.get("mutated"),
		"committed": result.get("committed"),
		"ws_status": ws,
		"ws_survived": ws == "Production",
		"concurrent_hits": hits["n"],
		"no_1020": "1020" not in str(result.get("error") or ""),
		"item": {
			"issued": flt(row.get("issued")),
			"returned": flt(row.get("returned")),
			"consumed": flt(row.get("consumed")),
			"remaining": flt(row.get("remaining_wip")),
		},
		"jci_ok": jci_ok,
		"jci_total": int(total or 0),
		"custom6_enabled": not bool(int(custom6 or 0)),
		"canonical": result.get("canonical_name"),
		"dispositions": DISP,
	}
