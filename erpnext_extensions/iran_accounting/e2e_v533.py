# Copyright (c) 2026, ERPNext Extensions contributors
"""Development-only Playwright fixtures for Manufacture valuation v5.3.3."""

from __future__ import annotations

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.custom_fields import ensure_custom_fields
from erpnext_extensions.iran_accounting.e2e_bootstrap import (
	ensure_test_item,
	get_irr_company,
	get_second_warehouse,
	get_warehouse,
)
from erpnext_extensions.iran_accounting.manufacture_stage_costing import (
	CONTRACT_VERSION_FIELD,
	EQUIV_QTY_FIELD,
	PHYSICAL_CONV_FIELD,
	validate_job_card_secondary_match,
)
from erpnext_extensions.iran_accounting.scrap_costing import apply_iran_manufacture_output_contract

PREFIX = "IA-V533"


@frappe.whitelist()
def mint_dev_sid():
	"""Development-only Administrator SID for Playwright (no Production)."""
	if frappe.conf.get("e2e_marker") != "DEV_SITE" and frappe.conf.get("developer_mode") != 1:
		frappe.throw("mint_dev_sid is allowed only on the development site")
	from types import SimpleNamespace

	from frappe.sessions import Session

	frappe.set_user("Administrator")
	frappe.local.form_dict = frappe._dict()
	frappe.local.request_ip = "127.0.0.1"
	frappe.local.request = SimpleNamespace(cookies={}, headers={}, path="/")
	session = Session(
		"Administrator",
		resume=False,
		full_name="Administrator",
		user_type="System User",
	)
	frappe.db.commit()
	return {"sid": session.sid, "user": "Administrator"}


def _seed_item(company: str, prefix: str, stock_uom: str | None = None, rate: float = 1) -> str:
	item = ensure_test_item(company, prefix=prefix, stock_uom=stock_uom)
	frappe.db.set_value("Item", item, "valuation_rate", rate)
	frappe.db.set_value("Item", item, "include_item_in_manufacturing", 1)
	return item


def _ensure_uom(name: str) -> str:
	if not frappe.db.exists("UOM", name):
		frappe.get_doc({"doctype": "UOM", "uom_name": name, "enabled": 1}).insert(ignore_permissions=True)
	return name


def _ensure_conversion(item: str, uom: str, factor: float) -> None:
	if frappe.db.exists("UOM Conversion Detail", {"parent": item, "uom": uom}):
		frappe.db.set_value(
			"UOM Conversion Detail", {"parent": item, "uom": uom}, "conversion_factor", factor
		)
		return
	item_doc = frappe.get_doc("Item", item)
	item_doc.append("uoms", {"uom": uom, "conversion_factor": factor})
	item_doc.save(ignore_permissions=True)


def _stock_entry_type() -> str | None:
	return frappe.db.get_value("Stock Entry Type", {"purpose": "Manufacture"}, "name")


def _new_draft(company: str, items: list[dict], job_card: str | None = None, apply: bool = True):
	wip = get_warehouse(company)
	fg_wh = get_second_warehouse(company, wip)
	doc = frappe.new_doc("Stock Entry")
	doc.company = company
	doc.purpose = "Manufacture"
	doc.stock_entry_type = _stock_entry_type()
	doc.set_posting_time = 1
	if job_card:
		doc.job_card = job_card
	frappe.flags.iran_gate_defaults = True
	for row in items:
		payload = dict(row)
		if payload.pop("s_warehouse", None) is True:
			payload["s_warehouse"] = wip
			payload["t_warehouse"] = None
		if payload.pop("t_warehouse", None) is True:
			payload["t_warehouse"] = fg_wh
			payload["s_warehouse"] = None
		if payload.get("t_warehouse") and not payload.get("basic_rate"):
			payload.setdefault("allow_zero_valuation_rate", 1)
		doc.append("items", payload)
	if apply:
		apply_iran_manufacture_output_contract(doc)
	doc.flags.ignore_validate = True
	doc.insert(ignore_permissions=True, ignore_mandatory=True)
	frappe.db.commit()
	return frappe.get_doc("Stock Entry", doc.name)


def _row_view(row) -> dict:
	return {
		"idx": row.idx,
		"item_code": row.item_code,
		"qty": flt(row.qty),
		"basic_rate": flt(row.basic_rate),
		"basic_amount": flt(row.basic_amount),
		"additional_cost": flt(row.additional_cost),
		"amount": flt(row.amount),
		"allow_zero_valuation_rate": row.get("allow_zero_valuation_rate"),
		"is_finished_item": row.get("is_finished_item"),
		"secondary_item_type": row.get("secondary_item_type"),
		"custom_output_class": row.get("custom_output_class"),
		"custom_physical_conversion": row.get(PHYSICAL_CONV_FIELD),
		"custom_equivalent_qty": row.get(EQUIV_QTY_FIELD),
	}


def _context(doc, extra=None) -> dict:
	payload = {
		"stock_entry": doc.name,
		"company": doc.company,
		"docstatus": doc.docstatus,
		"purpose": doc.purpose,
		"desk_url": f"/desk/stock-entry/{doc.name}",
		"contract_version": doc.get(CONTRACT_VERSION_FIELD),
		"items": [_row_view(row) for row in doc.items],
		"total_incoming_value": flt(doc.total_incoming_value),
		"total_outgoing_value": flt(doc.total_outgoing_value),
	}
	if extra:
		payload.update(extra)
	return payload


@frappe.whitelist()
def prepare_v533_ui_scenario(scenario: str, company: str | None = None):
	"""Create a development draft for Playwright. Never touches Production."""
	frappe.set_user("Administrator")
	ensure_custom_fields()
	company = get_irr_company(company)
	if scenario == "gap1_scrap":
		return _prepare_gap1(company)
	if scenario == "gap2_valid":
		return _prepare_gap2_valid(company)
	if scenario == "gap2_mismatch":
		return _prepare_gap2_mismatch(company)
	if scenario == "gap2_missing_equivalence":
		return _prepare_missing_equivalence(company)
	if scenario == "independent_by_product":
		return _prepare_independent_by_product(company)
	frappe.throw(f"Unknown v5.3.3 UI scenario: {scenario}")


def _prepare_gap1(company: str) -> dict:
	rm = _seed_item(company, f"{PREFIX}-RM", rate=35297)
	fg = _seed_item(company, f"{PREFIX}-FG", rate=1)
	doc = _new_draft(
		company,
		[
			{
				"item_code": rm,
				"qty": 10,
				"transfer_qty": 10,
				"basic_rate": 35297,
				"basic_amount": 352970,
				"amount": 352970,
				"s_warehouse": True,
			},
			{
				"item_code": fg,
				"qty": 8,
				"transfer_qty": 8,
				"is_finished_item": 1,
				"t_warehouse": True,
			},
			{
				"item_code": rm,
				"qty": 2,
				"transfer_qty": 2,
				"secondary_item_type": "Scrap",
				"t_warehouse": True,
				"allow_zero_valuation_rate": 1,
			},
		],
	)
	if not flt(next(row.basic_rate for row in doc.items if row.secondary_item_type == "Scrap")):
		apply_iran_manufacture_output_contract(doc)
		doc.db_update()
		for row in doc.items:
			row.db_update()
		frappe.db.commit()
		doc = frappe.get_doc("Stock Entry", doc.name)
	scrap = next(row for row in doc.items if row.secondary_item_type == "Scrap")
	return _context(
		doc,
		{
			"expected_scrap_rate": 35297,
			"expected_scrap_amount": 70594,
			"scrap_item": rm,
			"scrap_rate": flt(scrap.basic_rate),
		},
	)


def _prepare_gap2_valid(company: str) -> dict:
	box = _ensure_uom("BOX(2pfs)")
	syringe = _ensure_uom("سرنگ")
	rm = _seed_item(company, f"{PREFIX}-RMEQ", rate=2857000)
	fg = _seed_item(company, f"{PREFIX}-FGBOX", stock_uom=box, rate=1)
	cp = _seed_item(company, f"{PREFIX}-CPSYR", stock_uom=syringe, rate=1)
	_ensure_conversion(fg, syringe, 2)
	doc = _new_draft(
		company,
		[
			{
				"item_code": rm,
				"qty": 1,
				"transfer_qty": 1,
				"basic_rate": 2857000,
				"basic_amount": 2857000,
				"amount": 2857000,
				"s_warehouse": True,
			},
			{
				"item_code": fg,
				"qty": 1428,
				"transfer_qty": 1428,
				"is_finished_item": 1,
				"stock_uom": box,
				"t_warehouse": True,
			},
			{
				"item_code": cp,
				"qty": 1,
				"transfer_qty": 1,
				"secondary_item_type": "Co-Product",
				"stock_uom": syringe,
				"t_warehouse": True,
			},
		],
	)
	if not flt(next(row.basic_rate for row in doc.items if row.secondary_item_type == "Co-Product")):
		apply_iran_manufacture_output_contract(doc)
		doc.db_update()
		for row in doc.items:
			row.db_update()
		frappe.db.commit()
		doc = frappe.get_doc("Stock Entry", doc.name)
	fg_row = next(row for row in doc.items if row.is_finished_item)
	cp_row = next(row for row in doc.items if row.secondary_item_type == "Co-Product")
	return _context(
		doc,
		{
			"fg_item": fg,
			"cp_item": cp,
			"expected_fg_rate": 2000,
			"expected_cp_rate": 1000,
			"fg_rate": flt(fg_row.basic_rate),
			"cp_rate": flt(cp_row.basic_rate),
			"fg_eq_qty": flt(fg_row.get(EQUIV_QTY_FIELD)),
			"cp_eq_qty": flt(cp_row.get(EQUIV_QTY_FIELD)),
		},
	)


def _prepare_gap2_mismatch(company: str) -> dict:
	rm = _seed_item(company, f"{PREFIX}-RMMIS", rate=100)
	fg = _seed_item(company, f"{PREFIX}-FGMIS", rate=1)
	actual = "30500003" if frappe.db.exists("Item", "30500003") else _seed_item(company, f"{PREFIX}-CPACT", rate=1)
	jc_name = "PO-JOB06720" if frappe.db.exists("Job Card", "PO-JOB06720") else None
	if not jc_name:
		jc_name = frappe.db.get_value(
			"Job Card Secondary Item",
			{"secondary_item_type": "By-Product", "item_code": ("!=", actual)},
			"parent",
		)
	if not jc_name:
		frappe.throw("No submitted Job Card with a different By-Product is available for the mismatch fixture")
	expected = frappe.db.get_value(
		"Job Card Secondary Item",
		{"parent": jc_name, "secondary_item_type": ("in", ["By-Product", "Co-Product"])},
		"item_code",
	)
	error = None
	try:
		probe = frappe.new_doc("Stock Entry")
		probe.company = company
		probe.purpose = "Manufacture"
		probe.job_card = jc_name
		probe.append(
			"items",
			{
				"item_code": actual,
				"qty": 1,
				"secondary_item_type": "By-Product",
				"t_warehouse": get_warehouse(company),
			},
		)
		validate_job_card_secondary_match(probe)
	except frappe.ValidationError as exc:
		error = str(exc)
	doc = _new_draft(
		company,
		[
			{"item_code": rm, "qty": 1, "basic_rate": 100, "basic_amount": 100, "s_warehouse": True},
			{"item_code": fg, "qty": 1, "is_finished_item": 1, "t_warehouse": True},
			{"item_code": actual, "qty": 1, "secondary_item_type": "By-Product", "t_warehouse": True},
		],
		job_card=jc_name,
		apply=False,
	)
	return _context(
		doc,
		{
			"job_card": jc_name,
			"expected_item": expected,
			"actual_item": actual,
			"expected_error": error,
			"blocked": bool(error),
		},
	)


def _prepare_missing_equivalence(company: str) -> dict:
	box = _ensure_uom("BOX(2pfs)")
	other = _ensure_uom("Unit")
	rm = _seed_item(company, f"{PREFIX}-RMNEQ", rate=100)
	fg = _seed_item(company, f"{PREFIX}-FGNEQ", stock_uom=box, rate=1)
	cp = _seed_item(company, f"{PREFIX}-CPNEQ", stock_uom=other, rate=1)
	error = None
	try:
		probe = frappe.new_doc("Stock Entry")
		probe.company = company
		probe.purpose = "Manufacture"
		probe.append(
			"items",
			{
				"item_code": rm,
				"qty": 1,
				"basic_rate": 100,
				"basic_amount": 100,
				"s_warehouse": get_warehouse(company),
			},
		)
		probe.append(
			"items",
			{
				"item_code": fg,
				"qty": 1,
				"is_finished_item": 1,
				"stock_uom": box,
				"t_warehouse": get_warehouse(company),
			},
		)
		probe.append(
			"items",
			{
				"item_code": cp,
				"qty": 1,
				"secondary_item_type": "Co-Product",
				"stock_uom": other,
				"t_warehouse": get_warehouse(company),
			},
		)
		apply_iran_manufacture_output_contract(probe)
	except frappe.ValidationError as exc:
		error = str(exc)
	doc = _new_draft(
		company,
		[
			{"item_code": rm, "qty": 1, "basic_rate": 100, "basic_amount": 100, "s_warehouse": True},
			{"item_code": fg, "qty": 1, "is_finished_item": 1, "stock_uom": box, "t_warehouse": True},
			{
				"item_code": cp,
				"qty": 1,
				"secondary_item_type": "Co-Product",
				"stock_uom": other,
				"t_warehouse": True,
			},
		],
		apply=False,
	)
	return _context(
		doc,
		{
			"expected_error": error,
			"blocked": bool(error),
			"mentions_no_1_to_1": bool(error and "1:1" in error),
		},
	)


def _prepare_independent_by_product(company: str) -> dict:
	"""Within-pool Valuation Rate By-Product stays independent; over-pool still I5."""
	from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
		ValuationIntegrityError,
		assert_manufacture_value_pool,
	)

	rm = _seed_item(company, f"{PREFIX}-RMIND", rate=1000)
	fg = _seed_item(company, f"{PREFIX}-FGIND", rate=1)
	by_item = _seed_item(company, f"{PREFIX}-BYIND", rate=100)
	doc = _new_draft(
		company,
		[
			{
				"item_code": rm,
				"qty": 10,
				"transfer_qty": 10,
				"basic_rate": 1000,
				"basic_amount": 10_000,
				"amount": 10_000,
				"s_warehouse": True,
			},
			{
				"item_code": fg,
				"qty": 9,
				"transfer_qty": 9,
				"is_finished_item": 1,
				"t_warehouse": True,
			},
			{
				"item_code": by_item,
				"qty": 1,
				"transfer_qty": 1,
				"secondary_item_type": "By-Product",
				"valuation_type": "Valuation Rate",
				"basic_rate": 100,
				"basic_amount": 100,
				"amount": 100,
				"t_warehouse": True,
			},
		],
	)
	by_row = next(row for row in doc.items if row.secondary_item_type == "By-Product")
	fg_row = next(row for row in doc.items if row.is_finished_item)
	if flt(by_row.basic_rate) != 100:
		apply_iran_manufacture_output_contract(doc)
		doc.db_update()
		for row in doc.items:
			row.db_update()
		frappe.db.commit()
		doc = frappe.get_doc("Stock Entry", doc.name)
		by_row = next(row for row in doc.items if row.secondary_item_type == "By-Product")
		fg_row = next(row for row in doc.items if row.is_finished_item)

	i5_error = None
	try:
		probe = frappe.new_doc("Stock Entry")
		probe.company = company
		probe.purpose = "Manufacture"
		wip = get_warehouse(company)
		fg_wh = get_second_warehouse(company, wip)
		probe.append(
			"items",
			{
				"item_code": rm,
				"qty": 10,
				"transfer_qty": 10,
				"basic_rate": 1000,
				"basic_amount": 10_000,
				"amount": 10_000,
				"s_warehouse": wip,
			},
		)
		probe.append(
			"items",
			{
				"item_code": fg,
				"qty": 9,
				"transfer_qty": 9,
				"is_finished_item": 1,
				"t_warehouse": fg_wh,
			},
		)
		probe.append(
			"items",
			{
				"item_code": by_item,
				"qty": 1,
				"transfer_qty": 1,
				"secondary_item_type": "By-Product",
				"valuation_type": "Valuation Rate",
				"basic_rate": 5_000_000,
				"basic_amount": 5_000_000,
				"amount": 5_000_000,
				"t_warehouse": fg_wh,
			},
		)
		apply_iran_manufacture_output_contract(probe)
		assert_manufacture_value_pool(probe)
	except (ValuationIntegrityError, frappe.ValidationError) as exc:
		i5_error = str(exc)

	return _context(
		doc,
		{
			"by_item": by_item,
			"expected_by_rate": 100,
			"expected_fg_amount": 9900,
			"by_rate": flt(by_row.basic_rate),
			"fg_amount": flt(fg_row.basic_amount),
			"i5_blocked": bool(i5_error and "I5" in i5_error),
			"expected_error": i5_error,
		},
	)
