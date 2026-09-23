# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.3 equivalent-unit Stage Output costing for Manufacture.

Component Scrap is priced elsewhere (issued rate). This module allocates the
remaining material pool and operating cost across MAIN_FG, MAIN_PRODUCT_REJECT,
CO_PRODUCT and CO_PRODUCT_REJECT using a common equivalent unit.
"""

from __future__ import annotations

from collections import Counter, defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.domain.currency import (
	integer_valuation_rate_from_amount,
	round_monetary_rate,
)
from erpnext_extensions.iran_accounting.rounding import (
	get_company_currency,
	is_irr_company,
	round_currency,
)
from erpnext_extensions.iran_accounting.scrap_costing import (
	CLASS_CO_PRODUCT,
	CLASS_CO_PRODUCT_REJECT,
	CLASS_COMPONENT_SCRAP,
	CLASS_MAIN_FG,
	CLASS_MAIN_PRODUCT_REJECT,
	CLASS_OTHER_OUTPUT,
	MANUFACTURE_COSTING_CONTRACT_VERSION,
	STAGE_SECONDARY_TYPES,
	_capitalized,
	_is_incoming,
	_is_source_only,
	_row_label,
	_row_qty,
	classify_manufacture_outputs,
	secondary_item_type_of,
)

CONTRACT_VERSION_FIELD = "custom_manufacturing_costing_contract_version"
OUTPUT_CLASS_FIELD = "custom_output_class"
PHYSICAL_CONV_FIELD = "custom_physical_conversion"
EQUIV_FACTOR_FIELD = "custom_output_equivalent_factor"
EQUIV_QTY_FIELD = "custom_equivalent_qty"
COMMON_UOM_FIELD = "custom_common_uom"
PARENT_CO_PRODUCT_FIELD = "custom_parent_co_product"


def uses_v533_contract(doc) -> bool:
	"""Drafts adopt v5.3.3; submitted historical docs need an explicit stamp."""
	version = str(doc.get(CONTRACT_VERSION_FIELD) or "").strip()
	if version == MANUFACTURE_COSTING_CONTRACT_VERSION:
		return True
	if cint(doc.get("docstatus")) >= 1 and not version:
		return False
	return True


def stamp_contract_version(doc) -> None:
	_set_field(doc, CONTRACT_VERSION_FIELD, MANUFACTURE_COSTING_CONTRACT_VERSION)


def _set_field(obj, field: str, value) -> None:
	if hasattr(obj, "set"):
		try:
			obj.set(field, value)
			return
		except Exception:
			pass
	setattr(obj, field, value)


def _is_finance_excluded(row) -> bool:
	valuation_type = row.get("valuation_type")
	if valuation_type == "Manual" and cint(row.get("set_basic_rate_manually")):
		return True
	if valuation_type == "% of Component Cost" and row.get("bom_secondary_item"):
		return True
	return False


def _class_of(row, classified) -> str:
	for name in (
		CLASS_MAIN_FG,
		CLASS_MAIN_PRODUCT_REJECT,
		CLASS_CO_PRODUCT,
		CLASS_CO_PRODUCT_REJECT,
		CLASS_COMPONENT_SCRAP,
		CLASS_OTHER_OUTPUT,
	):
		if row in classified[name]:
			return name
	return CLASS_OTHER_OUTPUT


def snapshot_output_classification(doc, classified=None) -> dict:
	classified = classified or classify_manufacture_outputs(doc)
	for name, rows in classified.items():
		for row in rows:
			_set_field(row, OUTPUT_CLASS_FIELD, name)
	return classified


def snapshot_equivalent_factors_from_sources(doc) -> None:
	"""Copy JC / BOM equivalent factors onto SE rows when the row has none."""
	by_item: dict[tuple[str | None, str | None], float] = {}
	job_card = doc.get("job_card")
	if job_card:
		for row in frappe.get_all(
			"Job Card Secondary Item",
			filters={"parent": job_card},
			fields=["item_code", "secondary_item_type", EQUIV_FACTOR_FIELD],
		):
			factor = row.get(EQUIV_FACTOR_FIELD)
			if factor not in (None, "") and flt(factor) > 0:
				by_item[(row.secondary_item_type, row.item_code)] = flt(factor)

	for row in doc.get("items") or []:
		if not _is_incoming(row):
			continue
		if _explicit_factor(row) is not None:
			continue
		key = (secondary_item_type_of(row), row.get("item_code"))
		if key in by_item:
			_set_field(row, EQUIV_FACTOR_FIELD, by_item[key])
			continue
		bom_ref = row.get("bom_secondary_item")
		if not bom_ref:
			continue
		factor = frappe.db.get_value("BOM Secondary Item", bom_ref, EQUIV_FACTOR_FIELD)
		if factor not in (None, "") and flt(factor) > 0:
			_set_field(row, EQUIV_FACTOR_FIELD, flt(factor))


def validate_job_card_secondary_match(doc) -> None:
	"""Job Card Co/By-Product / Additional FG must match the Manufacture rows."""
	if doc.doctype != "Stock Entry" or doc.purpose != "Manufacture":
		return
	job_card = doc.get("job_card")
	if not job_card:
		return

	jc_rows = frappe.get_all(
		"Job Card Secondary Item",
		filters={"parent": job_card},
		fields=["item_code", "secondary_item_type", "idx"],
		order_by="idx",
	)
	jc_pairs = [
		(row.secondary_item_type, row.item_code)
		for row in jc_rows
		if row.secondary_item_type in STAGE_SECONDARY_TYPES and row.item_code
	]
	se_pairs = []
	se_rows = []
	for row in doc.get("items") or []:
		stype = secondary_item_type_of(row)
		if stype in STAGE_SECONDARY_TYPES and _is_incoming(row):
			se_pairs.append((stype, row.get("item_code")))
			se_rows.append(row)

	if not jc_pairs and not se_pairs:
		return
	if Counter(jc_pairs) == Counter(se_pairs):
		return

	jc_items_by_type = defaultdict(list)
	jc_type_by_item = {}
	for stype, item in jc_pairs:
		jc_items_by_type[stype].append(item)
		jc_type_by_item[item] = stype

	for row in se_rows:
		stype = secondary_item_type_of(row)
		item = row.get("item_code")
		if (stype, item) in jc_pairs:
			continue
		if item in jc_type_by_item and jc_type_by_item[item] != stype:
			frappe.throw(
				_(
					"Job Card {0} defines {1} {2}, but Manufacture Stock Entry {3} "
					"contains {2} as {4}. Correct the Job Card / Stock Entry secondary "
					"output before submission."
				).format(job_card, jc_type_by_item[item], item, _row_label(row), stype),
				frappe.ValidationError,
			)
		expected_item = jc_items_by_type[stype][0] if jc_items_by_type[stype] else _("(none)")
		frappe.throw(
			_(
				"Job Card {0} defines {1} {2}, but Manufacture Stock Entry {3} contains {4}. "
				"Correct the Job Card / Stock Entry secondary output before submission."
			).format(job_card, stype or _("secondary item"), expected_item, _row_label(row), item),
			frappe.ValidationError,
		)

	expected = ", ".join(f"{stype} {item}" for stype, item in jc_pairs) or _("(none)")
	actual = ", ".join(f"{stype} {item}" for stype, item in se_pairs) or _("(none)")
	frappe.throw(
		_(
			"Job Card {0} defines {1}, but this Manufacture Stock Entry has {2}. "
			"Correct the Job Card / Stock Entry secondary output before submission."
		).format(job_card, expected, actual),
		frappe.ValidationError,
	)


def _item_stock_uom(row) -> str | None:
	return row.get("stock_uom") or (
		frappe.db.get_value("Item", row.get("item_code"), "stock_uom") if row.get("item_code") else None
	)


def _item_uom_factor(item_code: str, uom: str) -> float | None:
	if not item_code or not uom:
		return None
	factor = frappe.db.get_value(
		"UOM Conversion Detail",
		{"parent": item_code, "parenttype": "Item", "uom": uom},
		"conversion_factor",
	)
	if factor in (None, ""):
		return None
	return flt(factor)


def _reachable_uoms(item_code: str, stock_uom: str | None) -> set[str]:
	uoms: set[str] = set()
	if stock_uom:
		uoms.add(stock_uom)
	if not item_code:
		return uoms
	for uom in frappe.get_all(
		"UOM Conversion Detail",
		filters={"parent": item_code, "parenttype": "Item"},
		pluck="uom",
	):
		if uom:
			uoms.add(uom)
	return uoms


def resolve_common_uom(stage_rows) -> str | None:
	sets: list[set[str]] = []
	for row in stage_rows:
		stock_uom = _item_stock_uom(row)
		reachable = _reachable_uoms(row.get("item_code"), stock_uom)
		if not reachable:
			return None
		sets.append(reachable)
	if not sets:
		return None
	common = set.intersection(*sets)
	if not common:
		return None
	non_fg_stock = {
		_item_stock_uom(row)
		for row in stage_rows
		if not row.get("is_finished_item") and _item_stock_uom(row) in common
	}
	if len(non_fg_stock) == 1:
		return next(iter(non_fg_stock))
	if non_fg_stock:
		return sorted(uom for uom in non_fg_stock if uom)[0]
	return sorted(common)[0]


def physical_conversion_to_common(row, common_uom: str | None) -> float | None:
	if not common_uom:
		return None
	stock_uom = _item_stock_uom(row)
	if stock_uom == common_uom:
		return 1.0
	factor = _item_uom_factor(row.get("item_code"), common_uom)
	if factor and factor > 0:
		# Approved site convention: conversion_factor on the common UOM is
		# common units per 1 stock UOM (1 BOX(2pfs) → 2 سرنگ).
		return factor
	return None


def _explicit_factor(row) -> float | None:
	raw = row.get(EQUIV_FACTOR_FIELD)
	if raw in (None, ""):
		return None
	return flt(raw)


def equivalent_qty_for_row(
	row, common_uom: str | None, *, require_physical: bool
) -> tuple[float, float, float]:
	"""Return (equivalent_qty, physical_conversion, explicit_factor)."""
	stock_qty = _row_qty(row)
	physical = physical_conversion_to_common(row, common_uom) if common_uom else None
	explicit = _explicit_factor(row)
	if physical is None:
		if require_physical or explicit is None:
			frappe.throw(
				_(
					"{0}: cannot convert {1} ({2}) to a common equivalent unit. "
					"Add a UOM conversion or a positive output equivalent factor. "
					"1:1 is not assumed."
				).format(
					_row_label(row),
					row.get("item_code") or "",
					_item_stock_uom(row) or _("unknown UOM"),
				),
				frappe.ValidationError,
			)
		physical = 1.0
	if explicit is None:
		explicit = 1.0
	if explicit <= 0:
		frappe.throw(
			_("{0}: output equivalent factor must be greater than zero.").format(_row_label(row)),
			frappe.ValidationError,
		)
	return stock_qty * physical * explicit, physical, explicit


def resolve_parent_co_product_item(row, co_items: set[str]) -> str | None:
	item = row.get("item_code")
	main = frappe.db.get_value("Item", item, "custom_main_item_code") if item else None
	candidates: set[str] = set()
	if item in co_items:
		candidates.add(item)
	if main and main in co_items:
		candidates.add(main)
	if len(candidates) == 1:
		return next(iter(candidates))
	return None


def _consumed_material(doc) -> float:
	return sum(flt(row.get("basic_amount")) for row in doc.get("items") or [] if _is_source_only(row))


def _operating_pool(doc, stage_rows, excluded_rows) -> float:
	header = flt(doc.get("total_additional_costs"))
	if header:
		excluded_oh = sum(flt(row.get("additional_cost")) for row in excluded_rows)
		return max(header - excluded_oh, 0.0)
	return sum(flt(row.get("additional_cost")) for row in stage_rows)


def allocate_stage_output_cost(doc) -> bool:
	"""Allocate material + operating cost across equivalent stage outputs."""
	classified = snapshot_output_classification(doc)
	finance_excluded = [
		row
		for row in classified[CLASS_CO_PRODUCT] + classified[CLASS_CO_PRODUCT_REJECT]
		if _is_finance_excluded(row)
	]
	stage = (
		list(classified[CLASS_MAIN_FG])
		+ list(classified[CLASS_MAIN_PRODUCT_REJECT])
		+ [row for row in classified[CLASS_CO_PRODUCT] if row not in finance_excluded]
		+ [row for row in classified[CLASS_CO_PRODUCT_REJECT] if row not in finance_excluded]
	)
	if not classified[CLASS_CO_PRODUCT] and not classified[CLASS_CO_PRODUCT_REJECT]:
		return False
	if len(classified[CLASS_MAIN_FG]) != 1:
		frappe.throw(
			_("Equivalent-unit Manufacture costing requires exactly one finished good."),
			frappe.ValidationError,
		)
	if not stage:
		return False

	co_items = {row.get("item_code") for row in classified[CLASS_CO_PRODUCT] if row.get("item_code")}
	for row in classified[CLASS_CO_PRODUCT_REJECT]:
		parent = resolve_parent_co_product_item(row, co_items)
		if not parent:
			frappe.throw(
				_(
					"{0} is Co-Product scrap but its parent Co-Product cannot be determined. "
					"It is not treated as Component Scrap."
				).format(_row_label(row)),
				frappe.ValidationError,
			)
		_set_field(row, PARENT_CO_PRODUCT_FIELD, parent)
		parent_row = next(
			(candidate for candidate in classified[CLASS_CO_PRODUCT] if candidate.get("item_code") == parent),
			None,
		)
		if parent_row and _explicit_factor(row) is None and _explicit_factor(parent_row) is not None:
			_set_field(row, EQUIV_FACTOR_FIELD, _explicit_factor(parent_row))

	common_uom = resolve_common_uom(stage)
	require_physical = bool(common_uom)
	if not common_uom:
		missing_factor = [row for row in stage if _explicit_factor(row) is None]
		if missing_factor:
			frappe.throw(
				_(
					"Stage outputs {0} do not share a common UOM conversion and have no "
					"output equivalent factor. 1:1 is not assumed."
				).format(", ".join(_row_label(row) for row in missing_factor)),
				frappe.ValidationError,
			)

	eq_map: dict[int, tuple[float, float, float]] = {}
	total_eq = 0.0
	for row in stage:
		eq_qty, physical, factor = equivalent_qty_for_row(
			row, common_uom, require_physical=require_physical
		)
		if eq_qty <= 0:
			frappe.throw(
				_("{0}: equivalent quantity must be greater than zero.").format(_row_label(row)),
				frappe.ValidationError,
			)
		eq_map[id(row)] = (eq_qty, physical, factor)
		total_eq += eq_qty
		_set_field(row, PHYSICAL_CONV_FIELD, physical)
		_set_field(row, EQUIV_FACTOR_FIELD, factor)
		_set_field(row, EQUIV_QTY_FIELD, eq_qty)
		if common_uom:
			_set_field(row, COMMON_UOM_FIELD, common_uom)

	currency = get_company_currency(doc.company)
	component_value = sum(flt(row.get("basic_amount")) for row in classified[CLASS_COMPONENT_SCRAP])
	excluded_value = sum(flt(row.get("basic_amount")) for row in finance_excluded)
	material_pool = round_currency(_consumed_material(doc) - component_value - excluded_value, currency)
	operating_pool = round_currency(_operating_pool(doc, stage, finance_excluded), currency)
	if material_pool < 0:
		frappe.throw(
			_("Stage material pool is negative after deducting Component Scrap."),
			frappe.ValidationError,
		)

	fg = classified[CLASS_MAIN_FG][0]
	others = [row for row in stage if row is not fg]
	assigned_mat = 0.0
	assigned_oh = 0.0
	for row in others:
		eq_qty, _physical, _factor = eq_map[id(row)]
		mat = round_currency(material_pool * eq_qty / total_eq, currency) if total_eq else 0.0
		oh = round_currency(operating_pool * eq_qty / total_eq, currency) if total_eq else 0.0
		_apply_stage_row(row, mat, oh, currency)
		assigned_mat += mat
		assigned_oh += oh
	_apply_stage_row(
		fg,
		round_currency(material_pool - assigned_mat, currency),
		round_currency(operating_pool - assigned_oh, currency),
		currency,
	)

	for row in classified[CLASS_COMPONENT_SCRAP]:
		row.additional_cost = 0
		row.amount = round_currency(flt(row.get("basic_amount")) + _capitalized(row), currency)
	return True


def _apply_stage_row(row, material: float, operating: float, currency: str) -> None:
	qty = _row_qty(row)
	if operating < 0:
		operating = 0.0
	row.basic_amount = material
	row.basic_rate = round_monetary_rate(material / qty, currency) if qty else 0
	row.additional_cost = operating
	row.amount = round_currency(material + _capitalized(row), currency)
	row.valuation_rate = integer_valuation_rate_from_amount(row.amount, qty, currency)
	if flt(row.amount):
		row.allow_zero_valuation_rate = 0


def apply_stage_output_contract(doc) -> bool:
	if doc.doctype != "Stock Entry" or doc.purpose != "Manufacture":
		return False
	if not is_irr_company(doc.company):
		return False
	if not uses_v533_contract(doc):
		return False
	stamp_contract_version(doc)
	validate_job_card_secondary_match(doc)
	snapshot_equivalent_factors_from_sources(doc)
	return allocate_stage_output_cost(doc)
