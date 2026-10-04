# Copyright (c) 2026, ERPNext Extensions contributors
"""Authoritative Job Card returnable quantities (reuse v5.4.1 evidence).

Equation (per Job Card × Item × Batch):

	ISSUED − RETURNED − CONSUMED − COMPONENT_SCRAP − OTHER_CLASSIFIED_WIP_OUTFLOW
	= STILL IN WIP / RETURNABLE

``OTHER`` in evidence covers unclassified WIP outflows; PRODUCT_REJECT /
ORDINARY_SCRAP are tracked separately and are not returnable component stock.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.job_card_stock_rebuild.evidence import build_evidence


def get_returnable_by_item_batch(job_card: str) -> dict[tuple[str, str], dict]:
	"""Return map ``(item_code, batch_no) → returnable evidence summary``."""
	if not job_card:
		return {}
	ev = build_evidence(job_card)
	out: dict[tuple[str, str], dict] = {}
	for it in ev.get("items") or []:
		item = it.get("item_code") or ""
		batch = it.get("batch_no") or ""
		wip_warehouses: set[str] = set()
		return_destinations: set[str] = set()
		jci_names: set[str] = set()
		for m in it.get("evidence") or []:
			if m.get("bucket") == "ISSUE":
				if m.get("t_warehouse"):
					wip_warehouses.add(m["t_warehouse"])
				if m.get("s_warehouse"):
					return_destinations.add(m["s_warehouse"])
				if m.get("job_card_item"):
					jci_names.add(m["job_card_item"])
			elif m.get("bucket") == "RETURN":
				# return source is WIP; destination is original/source warehouse
				if m.get("s_warehouse"):
					wip_warehouses.add(m["s_warehouse"])
				if m.get("t_warehouse"):
					return_destinations.add(m["t_warehouse"])
				if m.get("job_card_item"):
					jci_names.add(m["job_card_item"])
		returnable = max(0.0, flt(it.get("wip_remainder")))
		out[(item, batch)] = {
			"item_code": item,
			"batch_no": batch,
			"issued": flt(it.get("issued")),
			"returned": flt(it.get("returned")),
			"consumed": flt(it.get("consumed")),
			"component_scrap": flt(it.get("component_scrap")),
			"ordinary_scrap": flt(it.get("ordinary_scrap")),
			"product_reject": flt(it.get("product_reject")),
			"other": flt(it.get("other")),
			"still_in_wip": returnable,
			"returnable": returnable,
			"wip_warehouses": wip_warehouses,
			"return_destinations": return_destinations,
			"job_card_items": jci_names,
		}
	return out


def sum_document_return_qty(stock_entry, item_code: str, batch_no: str) -> float:
	"""Sum return qty for item/batch on the Stock Entry being validated."""
	total = 0.0
	for row in stock_entry.get("items") or []:
		if (row.item_code or "") != item_code:
			continue
		if (row.batch_no or "") != (batch_no or ""):
			continue
		total += flt(row.transfer_qty or row.qty)
	return total


def jc_item_codes(job_card: str) -> set[str]:
	return set(
		frappe.get_all("Job Card Item", filters={"parent": job_card}, pluck="item_code") or []
	)


def jc_item_names(job_card: str) -> set[str]:
	return set(frappe.get_all("Job Card Item", filters={"parent": job_card}, pluck="name") or [])


def aggregate_returnable_report(job_card: str) -> list[dict]:
	"""Flat list for diagnostics / canaries."""
	rows = []
	for (_item, _batch), rec in sorted(get_returnable_by_item_batch(job_card).items()):
		rows.append(
			{
				**{k: rec[k] for k in rec if k not in ("wip_warehouses", "return_destinations", "job_card_items")},
				"wip_warehouses": sorted(rec["wip_warehouses"]),
				"return_destinations": sorted(rec["return_destinations"]),
				"job_card_items": sorted(rec["job_card_items"]),
			}
		)
	return rows
