# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Whitelisted Web Service API for Manufacturing Cost Adjustment operations.

Production sites without Bench/SSH must drive MCA generation, submission, and
status monitoring through these endpoints (plus standard ``frappe.client.submit``).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import flt, getdate

from erpnext_extensions.manufacturing_cost_adjustment import constants as C
from erpnext_extensions.manufacturing_cost_adjustment.doctype.manufacturing_cost_adjustment.manufacturing_cost_adjustment import (
	make_mca_from_stock_entry,
)

API_VERSION = "1.0.0"
LABOR_ACCT_PREFIX = "211238"
OH_ACCT_PREFIX = "211237"
DEFAULT_LABOR_ACCT = "211238 - کنترل دستمزد - E"
DEFAULT_OH_ACCT = "211237 - کنترل سربار - E"


def _require_mca_write():
	frappe.has_permission(C.MCA_DOCTYPE, "write", throw=True)


def _require_mca_create():
	frappe.has_permission(C.MCA_DOCTYPE, "create", throw=True)


def _require_mca_read():
	frappe.has_permission(C.MCA_DOCTYPE, "read", throw=True)


def _parse_jsonish(value):
	if isinstance(value, str):
		value = value.strip()
		if not value:
			return None
		import json

		try:
			return json.loads(value)
		except Exception:
			return value
	return value


def _labor_oh_from_costs(costs) -> tuple[float, float]:
	lab = oh = 0.0
	for c in costs or []:
		acct = (c.get("expense_account") if isinstance(c, dict) else c.expense_account) or ""
		amt = flt(c.get("amount") if isinstance(c, dict) else c.amount)
		if LABOR_ACCT_PREFIX in acct:
			lab += amt
		elif OH_ACCT_PREFIX in acct:
			oh += amt
	return lab, oh


def _breakdown_by_stock_entry(persian_period: str) -> list[dict]:
	return frappe.db.sql(
		"""
		SELECT bd.source_document AS stock_entry,
		       SUM(bd.unallocated_labor) AS labor,
		       SUM(bd.unallocated_overhead) AS overhead,
		       SUM(bd.quantity) AS qty,
		       GROUP_CONCAT(DISTINCT pa.name ORDER BY pa.name) AS product_allocations,
		       GROUP_CONCAT(DISTINCT pa.item_code ORDER BY pa.item_code) AS products,
		       GROUP_CONCAT(DISTINCT pa.batch_no ORDER BY pa.batch_no) AS batches
		FROM `tabMFG Cost Product Allocation Document Breakdown` bd
		INNER JOIN `tabMFG Cost Product Allocation` pa ON pa.name = bd.parent
		WHERE pa.persian_period = %s
		  AND bd.source_document_type = 'Stock Entry'
		  AND IFNULL(bd.source_document, '') != ''
		GROUP BY bd.source_document
		ORDER BY MIN(CONCAT(pa.name, '-', LPAD(bd.idx, 6, '0')))
		""",
		(persian_period,),
		as_dict=True,
	)


def _existing_active_mca(stock_entry: str) -> dict | None:
	return frappe.db.get_value(
		C.MCA_DOCTYPE,
		{"stock_entry": stock_entry, "docstatus": ("<", 2)},
		["name", "docstatus", "processing_status", "posting_date", "total_additional_costs"],
		as_dict=True,
	)


@frappe.whitelist(methods=["GET", "POST"])
def get_mca_api_status(company=None):
	"""Read-only capability probe for MCA REST operations."""
	_require_mca_read()
	from erpnext_extensions.manufacturing_cost_adjustment.serialization import (
		get_serialization_status,
	)

	return {
		"ok": True,
		"read_only": True,
		"api_version": API_VERSION,
		"site": getattr(frappe.local, "site", None),
		"user": frappe.session.user,
		"capabilities": {
			"get_mca_api_status": True,
			"get_mca_status": True,
			"get_serialization_status": True,
			"generate_mcas_from_persian_period": True,
			"submit_mca": True,
			"retry_mca_processing": True,
			"make_mca_from_stock_entry": True,
			"frappe_client_submit_supported": True,
		},
		"queues": {"mca": C.MCA_RQ_QUEUE, "riv": C.MCA_RIV_RQ_QUEUE},
		"serialization": get_serialization_status(company),
		"notes": [
			"Architecture: Option A — serialized standard Transaction RIV per MCA.",
			"submit_mca is blocked company-wide while any MCA is Queued/In Progress/Pending RIV/Failed.",
			"generate_mcas_from_persian_period is idempotent per Stock Entry.",
			"Long-running RIV work is asynchronous; poll get_mca_status.",
		],
	}


@frappe.whitelist(methods=["GET", "POST"])
def get_serialization_status(company=None):
	"""Read-only MCA company serialization gate status."""
	_require_mca_read()
	from erpnext_extensions.manufacturing_cost_adjustment.serialization import (
		get_serialization_status as _status,
	)

	return _status(company)


@frappe.whitelist(methods=["GET", "POST"])
def get_mca_status(name: str | None = None):
	"""Return MCA document + linked RIV statuses.

	Soft-nudges orphaned In Progress RIVs (dead RQ worker / Job OK after Core
	1020-recoverable leave-In-Progress). Never marks RIV Completed. Safe for
	operators polling while scheduler is disabled.
	"""
	_require_mca_read()
	if not name:
		frappe.throw(_("name is required"))
	if not frappe.db.exists(C.MCA_DOCTYPE, name):
		return {"ok": False, "reason": "not_found", "name": name}
	doc = frappe.get_doc(C.MCA_DOCTYPE, name)
	doc.check_permission("read")
	riv_nos = []
	raw = (doc.riv_voucher_nos or "").strip()
	if raw:
		try:
			parsed = frappe.parse_json(raw) if raw.startswith("[") else [x.strip() for x in raw.split(",") if x.strip()]
			riv_nos = list(parsed) if isinstance(parsed, list) else [raw]
		except Exception:
			riv_nos = [x.strip() for x in raw.split(",") if x.strip()]
	# Heal orphans before reading statuses so poll loops recover Job-OK/1020 stalls.
	if int(doc.docstatus or 0) == 1 and doc.processing_status == C.PS_PENDING_RIV:
		try:
			from erpnext_extensions.manufacturing_cost_adjustment.repost_engine import (
				_heal_orphaned_riv_in_progress,
				_kick_riv_execution,
			)

			for riv in riv_nos:
				st = frappe.db.get_value("Repost Item Valuation", riv, "status")
				if st == "Queued":
					_kick_riv_execution(riv)
				elif st == "In Progress":
					_heal_orphaned_riv_in_progress(riv)
			frappe.db.commit()
		except Exception:
			frappe.log_error(title=f"MCA status heal nudge failed {name}")
	rivs = []
	for riv in riv_nos:
		if not frappe.db.exists("Repost Item Valuation", riv):
			rivs.append({"name": riv, "exists": False})
			continue
		row = frappe.db.get_value(
			"Repost Item Valuation",
			riv,
			["name", "status", "item_code", "warehouse", "voucher_no", "modified"],
			as_dict=True,
		)
		row["exists"] = True
		rivs.append(row)
	lab, oh = _labor_oh_from_costs(doc.get("costs") or [])
	return {
		"ok": True,
		"api_version": API_VERSION,
		"name": doc.name,
		"docstatus": doc.docstatus,
		"processing_status": doc.processing_status,
		"checkpoint": doc.checkpoint,
		"repost_status": doc.repost_status,
		"stock_entry": doc.stock_entry,
		"company": doc.company,
		"posting_date": str(doc.posting_date) if doc.posting_date else None,
		"total_additional_costs": flt(doc.total_additional_costs),
		"labor": lab,
		"overhead": oh,
		"rivs": rivs,
		"quarantined": doc.name in C.QUARANTINED_MCA_NAMES,
		"last_repost_error": (doc.last_repost_error or "")[:2000],
	}


@frappe.whitelist(methods=["POST"])
def generate_mcas_from_persian_period(
	persian_period: str | None = None,
	confirm: str | None = None,
	labor_account: str | None = None,
	overhead_account: str | None = None,
	posting_date: str | None = None,
):
	"""Idempotently create draft MCAs from Product Allocation unallocated amounts.

	confirm must equal ``GENERATE_MCA_FROM_PERIOD``.
	Reuses an existing non-cancelled MCA for the same Stock Entry (no duplicates).
	"""
	_require_mca_create()
	if str(confirm or "").strip() != "GENERATE_MCA_FROM_PERIOD":
		frappe.throw(_("confirm must equal GENERATE_MCA_FROM_PERIOD"), frappe.ValidationError)
	if not persian_period:
		frappe.throw(_("persian_period is required (e.g. 1405-01)"))

	labor_account = labor_account or DEFAULT_LABOR_ACCT
	overhead_account = overhead_account or DEFAULT_OH_ACCT
	force_posting = getdate(posting_date) if posting_date else None

	rows = _breakdown_by_stock_entry(persian_period)
	created = []
	skipped_zero = []
	for row in rows:
		lab = flt(row.labor)
		oh = flt(row.overhead)
		se = row.stock_entry
		if lab == 0 and oh == 0:
			skipped_zero.append(se)
			continue
		existing = _existing_active_mca(se)
		if existing:
			doc = frappe.get_doc(C.MCA_DOCTYPE, existing.name)
			doc.check_permission("read")
			mca_lab, mca_oh = _labor_oh_from_costs(doc.get("costs") or [])
			created.append(
				{
					"mca": doc.name,
					"action": "reused",
					"docstatus": doc.docstatus,
					"processing_status": doc.processing_status,
					"stock_entry": se,
					"labor": lab,
					"overhead": oh,
					"mca_labor": mca_lab,
					"mca_overhead": mca_oh,
					"match": abs(mca_lab - lab) < 1 and abs(mca_oh - oh) < 1,
					"products": (row.products or "").split(","),
					"batches": (row.batches or "").split(","),
				}
			)
			continue

		doc = make_mca_from_stock_entry(se)
		if force_posting:
			doc.posting_date = force_posting
		else:
			se_date = frappe.db.get_value("Stock Entry", se, "posting_date")
			doc.posting_date = getdate(se_date) if se_date else getdate()
		if lab:
			doc.append(
				"costs",
				{
					"expense_account": labor_account,
					"description": f"Unallocated Labor | {persian_period}",
					"amount": lab,
					"base_amount": lab,
					"exchange_rate": 1,
					"account_currency": "IRR",
				},
			)
		if oh:
			doc.append(
				"costs",
				{
					"expense_account": overhead_account,
					"description": f"Unallocated Overhead | {persian_period}",
					"amount": oh,
					"base_amount": oh,
					"exchange_rate": 1,
					"account_currency": "IRR",
				},
			)
		doc.insert()
		mca_lab, mca_oh = _labor_oh_from_costs(doc.get("costs") or [])
		created.append(
			{
				"mca": doc.name,
				"action": "created_draft",
				"docstatus": 0,
				"processing_status": doc.processing_status,
				"stock_entry": se,
				"labor": lab,
				"overhead": oh,
				"mca_labor": mca_lab,
				"mca_overhead": mca_oh,
				"match": abs(mca_lab - lab) < 1 and abs(mca_oh - oh) < 1,
				"products": (row.products or "").split(","),
				"batches": (row.batches or "").split(","),
			}
		)
	frappe.db.commit()
	return {
		"ok": True,
		"api_version": API_VERSION,
		"persian_period": persian_period,
		"created": created,
		"skipped_zero": skipped_zero,
		"total": len(created),
		"all_match": all(c.get("match") for c in created),
		"sum_labor": sum(flt(c["labor"]) for c in created),
		"sum_overhead": sum(flt(c["overhead"]) for c in created),
	}


@frappe.whitelist(methods=["POST"])
def submit_mca(name: str | None = None, confirm: str | None = None):
	"""Submit a draft MCA via document lifecycle (idempotent if already submitted).

	confirm must equal ``SUBMIT_MCA``. Does not set docstatus manually.
	"""
	_require_mca_write()
	if str(confirm or "").strip() != "SUBMIT_MCA":
		frappe.throw(_("confirm must equal SUBMIT_MCA"), frappe.ValidationError)
	if not name:
		frappe.throw(_("name is required"))
	if name in C.QUARANTINED_MCA_NAMES:
		frappe.throw(
			_("MCA {0} is quarantined and cannot be submitted until recovery is authorized.").format(name)
		)
	if not frappe.db.exists(C.MCA_DOCTYPE, name):
		frappe.throw(_("MCA {0} not found").format(name))

	doc = frappe.get_doc(C.MCA_DOCTYPE, name)
	doc.check_permission("write")
	if doc.docstatus == 1:
		return {
			"ok": True,
			"idempotent": True,
			"name": doc.name,
			"docstatus": 1,
			"processing_status": doc.processing_status,
			"message": "already submitted",
		}
	if doc.docstatus != 0:
		frappe.throw(_("MCA {0} is not in Draft (docstatus={1})").format(name, doc.docstatus))

	doc.submit()
	frappe.db.commit()
	doc.reload()
	return {
		"ok": True,
		"idempotent": False,
		"name": doc.name,
		"docstatus": doc.docstatus,
		"processing_status": doc.processing_status,
		"stock_entry": doc.stock_entry,
		"total_additional_costs": flt(doc.total_additional_costs),
		"api_version": API_VERSION,
	}


@frappe.whitelist(methods=["POST"])
def retry_mca_processing(name: str | None = None, confirm: str | None = None):
	"""Controlled retry for Failed / Pending RIV MCA (Web Service API).

	confirm must equal ``RETRY_MCA``. Reuses the doctype whitelist path so
	quarantine / status guards stay centralized. Never recreates a Completed RIV.
	"""
	_require_mca_write()
	if str(confirm or "").strip() != "RETRY_MCA":
		frappe.throw(_("confirm must equal RETRY_MCA"), frappe.ValidationError)
	if not name:
		frappe.throw(_("name is required"))
	from erpnext_extensions.manufacturing_cost_adjustment.doctype.manufacturing_cost_adjustment.manufacturing_cost_adjustment import (
		retry_mca_processing as _retry,
	)

	out = _retry(name)
	frappe.db.commit()
	doc = frappe.get_doc(C.MCA_DOCTYPE, name)
	return {
		"ok": True,
		"api_version": API_VERSION,
		"name": doc.name,
		"processing_status": doc.processing_status,
		"repost_status": doc.repost_status,
		"checkpoint": doc.checkpoint,
		"detail": out,
	}
