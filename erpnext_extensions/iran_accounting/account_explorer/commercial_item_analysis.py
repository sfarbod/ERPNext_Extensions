# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""Party-identity commercial Item / Item Group analysis (invoice-authoritative).

Analytical source semantics (do not mix):

A. Sales commercial amounts — submitted Sales Invoice Items (docstatus=1).
B. Purchase commercial amounts — submitted Purchase Invoice Items (docstatus=1).
C. Physical stock movement — SLE (Case A); not used here.
D. Accounting GL activity — posted GL Entry (Case B); not used here.

Deduplication contract:
  Delivery Note / Purchase Receipt / Sales Order / Purchase Order are NOT
  included in commercial totals. Invoice rows are the sole commercial source.
  Signed return quantities and amounts come from is_return invoice lines.
"""

from __future__ import annotations

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import cint, flt

from erpnext_extensions.iran_accounting.account_explorer.constants import (
	COMMERCIAL_ITEM_GROUP_SORTABLE_FIELDS,
	COMMERCIAL_ITEM_SORTABLE_FIELDS,
)
from erpnext_extensions.iran_accounting.account_explorer.pagination import (
	exclude_empty_classification_rows,
	sort_rows,
)
from erpnext_extensions.iran_accounting.account_explorer.schemas import AccountExplorerQuerySpec

ANALYSIS_SOURCE_COMMERCIAL = "commercial_transactions"
ANALYSIS_SOURCE_STOCK = "stock_movements"
ANALYSIS_SOURCE_GL = "accounting_gl"

COMMERCIAL_PARTY_TYPES = frozenset({"Customer", "Supplier"})
ITEM_TRANSACTION_PARTY_TYPES = frozenset({"Customer", "Supplier"})

COMMERCIAL_ITEM_COLUMNS = [
	{"id": "display_code", "label": "Item", "fieldtype": "Data", "width": 120},
	{"id": "display_title", "label": "Item Name", "fieldtype": "Data", "width": 200},
	{"id": "item_group", "label": "Item Group", "fieldtype": "Data", "width": 130},
	{"id": "transaction_direction", "label": "Direction", "fieldtype": "Data", "width": 90},
	{"id": "party_type", "label": "Party Type", "fieldtype": "Data", "width": 100},
	{"id": "party", "label": "Party", "fieldtype": "Data", "width": 110},
	{"id": "quantity", "label": "Quantity", "fieldtype": "Float", "width": 100},
	{"id": "stock_quantity", "label": "Stock Qty", "fieldtype": "Float", "width": 100},
	{"id": "uom", "label": "UOM", "fieldtype": "Data", "width": 80},
	{"id": "net_amount", "label": "Net Amount", "fieldtype": "Currency", "width": 130},
	{"id": "base_net_amount", "label": "Base Net Amount", "fieldtype": "Currency", "width": 140},
	{"id": "currency", "label": "Currency", "fieldtype": "Data", "width": 80},
	{"id": "document_count", "label": "Documents", "fieldtype": "Int", "width": 90},
	{"id": "return_quantity", "label": "Return Qty", "fieldtype": "Float", "width": 100},
	{"id": "return_base_net_amount", "label": "Return Amount (Base)", "fieldtype": "Currency", "width": 140},
]

COMMERCIAL_ITEM_GROUP_COLUMNS = [
	{"id": "display_code", "label": "Item Group", "fieldtype": "Data", "width": 160},
	{"id": "display_title", "label": "Title", "fieldtype": "Data", "width": 200},
	{"id": "sales_base_net_amount", "label": "Sales Net (Base)", "fieldtype": "Currency", "width": 140},
	{"id": "purchase_base_net_amount", "label": "Purchase Net (Base)", "fieldtype": "Currency", "width": 150},
	{"id": "return_base_net_amount", "label": "Return Amount (Base)", "fieldtype": "Currency", "width": 140},
	{"id": "document_count", "label": "Documents", "fieldtype": "Int", "width": 90},
	{"id": "item_count", "label": "Items", "fieldtype": "Int", "width": 80},
]


def partition_identity_commercial_roles(
	tuples: list[tuple[str, str]] | None,
) -> dict[str, list[str]]:
	"""Split identity OR-tuples into Customer / Supplier / other party lists."""
	customers: list[str] = []
	suppliers: list[str] = []
	unsupported: list[tuple[str, str]] = []
	seen_c: set[str] = set()
	seen_s: set[str] = set()
	for party_type, party in tuples or []:
		pt = (party_type or "").strip()
		p = (party or "").strip()
		if not pt or not p:
			continue
		if pt == "Customer":
			if p not in seen_c:
				seen_c.add(p)
				customers.append(p)
		elif pt == "Supplier":
			if p not in seen_s:
				seen_s.add(p)
				suppliers.append(p)
		else:
			unsupported.append((pt, p))
	return {
		"customers": customers,
		"suppliers": suppliers,
		"unsupported": unsupported,
	}


def identity_requires_commercial_item_mode(spec: AccountExplorerQuerySpec) -> bool:
	"""True when Unified Party identity OR-tuples are active on Item / Item Group."""
	return bool(spec.resolved_member_tuples) and spec.view_axis in {"item", "item_group"}


def _shareholder_only_empty_result(spec: AccountExplorerQuerySpec, *, columns_key: str) -> dict:
	msg = _(
		"Shareholder has no supported item transaction source. "
		"Select Customer and/or Supplier roles to analyze sales or purchase items."
	)
	msg_fa = (
		"سهامدار منبع تراکنش کالایی پشتیبانی‌شده ندارد. "
		"برای تحلیل اقلام فروش یا خرید، نقش مشتری و/یا تأمین‌کننده را انتخاب کنید."
	)
	return {
		"rows": [],
		"totals": _empty_commercial_totals(columns_key),
		"pagination": {
			"page": int(spec.pagination.page or 1),
			"page_size": int(spec.pagination.page_size or 50),
			"total_rows": 0,
			"has_next": 0,
		},
		"warnings": [msg, msg_fa],
		"unsupported_filter_combination": {
			"axis": spec.view_axis,
			"filter": "party_identity",
			"reason": "shareholder_has_no_item_transactions",
			"message": msg,
			"message_fa": msg_fa,
		},
		"analysis_source": ANALYSIS_SOURCE_COMMERCIAL,
		"commercial_semantics": _semantics_meta(),
	}


def _empty_commercial_totals(columns_key: str) -> dict:
	if columns_key == "item_group":
		return {
			"sales_base_net_amount": 0.0,
			"purchase_base_net_amount": 0.0,
			"return_base_net_amount": 0.0,
			"document_count": 0,
			"item_count": 0,
			"base_net_amount": 0.0,
		}
	return {
		"quantity": 0.0,
		"stock_quantity": 0.0,
		"net_amount": 0.0,
		"base_net_amount": 0.0,
		"document_count": 0,
		"return_quantity": 0.0,
		"return_base_net_amount": 0.0,
	}


def _semantics_meta() -> dict:
	return {
		"sales_source": "Sales Invoice Item (submitted)",
		"purchase_source": "Purchase Invoice Item (submitted)",
		"excluded_from_commercial_totals": [
			"Delivery Note",
			"Purchase Receipt",
			"Sales Order",
			"Purchase Order",
		],
		"stock_source": "SLE (Case A) — not mixed into commercial amounts",
		"gl_source": "GL Entry (Case B) — not allocated to invoice items",
	}


def _limitation_warnings(spec: AccountExplorerQuerySpec, roles: dict) -> list[str]:
	warnings: list[str] = []
	unsupported = roles.get("unsupported") or []
	if unsupported:
		labels = ", ".join(f"{pt}:{p}" for pt, p in unsupported)
		warnings.append(
			_(
				"Selected identity roles without item transaction sources are ignored "
				"for commercial Item analysis: {0}."
			).format(labels)
		)
		warnings.append(
			"نقش‌های بدون منبع تراکنش کالایی (مانند سهامدار) در تحلیل اقلام تجاری لحاظ نمی‌شوند: "
			+ labels
		)
	if spec.finance_book:
		warnings.append(
			_(
				"Finance Book is a GL-axis filter and is not applied to Sales/Purchase "
				"Invoice commercial item rows."
			)
		)
	if spec.include_cancelled_entries:
		warnings.append(
			_(
				"Include Cancelled Entries applies to GL/SLE axes. Commercial item "
				"analysis uses submitted invoices only (docstatus=1)."
			)
		)
	warnings.append(
		_(
			"Commercial amounts use submitted invoice items only. Delivery Notes / "
			"Purchase Receipts are fulfillment documents and are not added to invoice totals."
		)
	)
	return warnings


def _inventory_item_filter(spec: AccountExplorerQuerySpec) -> list[str] | None:
	inv = spec.document_scope.inventory
	items = inv.item
	if not items:
		return None
	if isinstance(items, str):
		return [items] if items.strip() else None
	return [cstr for cstr in items if cstr]


def _inventory_item_group_filter(spec: AccountExplorerQuerySpec) -> list[str] | None:
	inv = spec.document_scope.inventory
	groups = inv.item_group
	if not groups:
		selected = getattr(spec.analysis.item_group_scope, "selected_item_group", None)
		if selected:
			from erpnext_extensions.iran_accounting.account_explorer.item_group_hierarchy import (
				get_descendant_item_groups,
			)

			return get_descendant_item_groups(selected, include_self=True)
		return None
	raw = [groups] if isinstance(groups, str) else list(groups)
	raw = [g for g in raw if g]
	if not raw:
		return None
	from erpnext_extensions.iran_accounting.account_explorer.item_group_hierarchy import (
		resolve_item_group_scope_names,
	)

	return resolve_item_group_scope_names(raw)


def _inventory_warehouse_filter(spec: AccountExplorerQuerySpec) -> list[str] | None:
	wh = spec.document_scope.inventory.warehouse
	if not wh:
		return None
	if isinstance(wh, str):
		return [wh] if wh.strip() else None
	return [w for w in wh if w]


def _sql_in_placeholders(values: list[str]) -> str:
	return ", ".join(["%s"] * len(values))


def _fetch_sales_aggregates(spec: AccountExplorerQuerySpec, customers: list[str]) -> list[dict]:
	if not customers:
		return []
	params: list = [
		spec.company,
		spec.from_date,
		spec.to_date,
		*customers,
	]
	extra = ""
	items = _inventory_item_filter(spec)
	if items:
		extra += f" AND sii.item_code IN ({_sql_in_placeholders(items)})"
		params.extend(items)
	groups = _inventory_item_group_filter(spec)
	if groups:
		extra += (
			f" AND COALESCE(NULLIF(sii.item_group, ''), i.item_group) IN ({_sql_in_placeholders(groups)})"
		)
		params.extend(groups)
	warehouses = _inventory_warehouse_filter(spec)
	if warehouses:
		extra += f" AND sii.warehouse IN ({_sql_in_placeholders(warehouses)})"
		params.extend(warehouses)

	selected_item = getattr(spec.analysis.item_scope, "selected_item", None)
	if selected_item:
		extra += " AND sii.item_code = %s"
		params.append(selected_item)

	sql = f"""
		SELECT
			sii.item_code AS item_code,
			MAX(COALESCE(NULLIF(sii.item_name, ''), i.item_name, sii.item_code)) AS item_name,
			MAX(COALESCE(NULLIF(sii.item_group, ''), i.item_group, '')) AS item_group,
			si.customer AS party,
			si.currency AS currency,
			MAX(sii.uom) AS uom_sample,
			COUNT(DISTINCT sii.uom) AS uom_distinct,
			SUM(sii.qty) AS quantity,
			SUM(sii.stock_qty) AS stock_quantity,
			SUM(sii.net_amount) AS net_amount,
			SUM(sii.base_net_amount) AS base_net_amount,
			COUNT(DISTINCT si.name) AS document_count,
			SUM(CASE WHEN IFNULL(si.is_return, 0) = 1 THEN sii.qty ELSE 0 END) AS return_quantity,
			SUM(CASE WHEN IFNULL(si.is_return, 0) = 1 THEN sii.base_net_amount ELSE 0 END)
				AS return_base_net_amount
		FROM `tabSales Invoice` si
		INNER JOIN `tabSales Invoice Item` sii ON sii.parent = si.name
		LEFT JOIN `tabItem` i ON i.name = sii.item_code
		WHERE si.docstatus = 1
			AND si.company = %s
			AND si.posting_date BETWEEN %s AND %s
			AND si.customer IN ({_sql_in_placeholders(customers)})
			{extra}
		GROUP BY si.customer, sii.item_code, si.currency
	"""
	return frappe.db.sql(sql, params, as_dict=True)


def _fetch_purchase_aggregates(spec: AccountExplorerQuerySpec, suppliers: list[str]) -> list[dict]:
	if not suppliers:
		return []
	params: list = [
		spec.company,
		spec.from_date,
		spec.to_date,
		*suppliers,
	]
	extra = ""
	items = _inventory_item_filter(spec)
	if items:
		extra += f" AND pii.item_code IN ({_sql_in_placeholders(items)})"
		params.extend(items)
	groups = _inventory_item_group_filter(spec)
	if groups:
		extra += (
			f" AND COALESCE(NULLIF(pii.item_group, ''), i.item_group) IN ({_sql_in_placeholders(groups)})"
		)
		params.extend(groups)
	warehouses = _inventory_warehouse_filter(spec)
	if warehouses:
		extra += f" AND pii.warehouse IN ({_sql_in_placeholders(warehouses)})"
		params.extend(warehouses)

	selected_item = getattr(spec.analysis.item_scope, "selected_item", None)
	if selected_item:
		extra += " AND pii.item_code = %s"
		params.append(selected_item)

	sql = f"""
		SELECT
			pii.item_code AS item_code,
			MAX(COALESCE(NULLIF(pii.item_name, ''), i.item_name, pii.item_code)) AS item_name,
			MAX(COALESCE(NULLIF(pii.item_group, ''), i.item_group, '')) AS item_group,
			pi.supplier AS party,
			pi.currency AS currency,
			MAX(pii.uom) AS uom_sample,
			COUNT(DISTINCT pii.uom) AS uom_distinct,
			SUM(pii.qty) AS quantity,
			SUM(pii.stock_qty) AS stock_quantity,
			SUM(pii.net_amount) AS net_amount,
			SUM(pii.base_net_amount) AS base_net_amount,
			COUNT(DISTINCT pi.name) AS document_count,
			SUM(CASE WHEN IFNULL(pi.is_return, 0) = 1 THEN pii.qty ELSE 0 END) AS return_quantity,
			SUM(CASE WHEN IFNULL(pi.is_return, 0) = 1 THEN pii.base_net_amount ELSE 0 END)
				AS return_base_net_amount
		FROM `tabPurchase Invoice` pi
		INNER JOIN `tabPurchase Invoice Item` pii ON pii.parent = pi.name
		LEFT JOIN `tabItem` i ON i.name = pii.item_code
		WHERE pi.docstatus = 1
			AND pi.company = %s
			AND pi.posting_date BETWEEN %s AND %s
			AND pi.supplier IN ({_sql_in_placeholders(suppliers)})
			{extra}
		GROUP BY pi.supplier, pii.item_code, pi.currency
	"""
	return frappe.db.sql(sql, params, as_dict=True)


def _uom_label(sample: str | None, distinct_count: int) -> str:
	if cint(distinct_count) > 1:
		return _("Mixed UOM")
	return sample or ""


def _row_has_commercial_activity(row: dict) -> bool:
	return any(
		flt(row.get(k))
		for k in (
			"quantity",
			"stock_quantity",
			"net_amount",
			"base_net_amount",
			"return_quantity",
			"return_base_net_amount",
			"sales_base_net_amount",
			"purchase_base_net_amount",
			"document_count",
		)
	)


def _paginate_commercial_rows(
	rows: list[dict],
	spec: AccountExplorerQuerySpec,
	*,
	sortable_fields,
	total_keys: list[str],
) -> dict:
	rows = exclude_empty_classification_rows(rows)
	if spec.hide_zero_rows:
		rows = [r for r in rows if _row_has_commercial_activity(r)]
	rows = sort_rows(rows, spec, sortable_fields)

	totals = {k: 0.0 for k in total_keys}
	# Do not sum quantity across mixed UOMs into a single footer number when
	# more than one UOM appears; still sum base currency amounts.
	uoms = {str(r.get("uom") or "") for r in rows if r.get("uom")}
	mixed_uom = len(uoms) > 1 or _("Mixed UOM") in uoms
	currencies = {str(r.get("currency") or "") for r in rows if r.get("currency")}
	mixed_currency = len(currencies) > 1

	for row in rows:
		for key in total_keys:
			if key in {"quantity", "stock_quantity", "return_quantity"} and mixed_uom:
				continue
			if key == "net_amount" and mixed_currency:
				continue
			totals[key] = flt(totals[key]) + flt(row.get(key))

	if mixed_uom:
		totals["qty_footer_policy"] = "suppressed_mixed_uom"
	if mixed_currency:
		totals["currency_footer_policy"] = "base_only_mixed_transaction_currency"

	total_rows = len(rows)
	page = int(spec.pagination.page or 1)
	page_size = int(spec.pagination.page_size or 50)
	offset = (page - 1) * page_size
	page_rows = rows[offset : offset + page_size]
	return {
		"rows": page_rows,
		"totals": totals,
		"pagination": {
			"page": page,
			"page_size": page_size,
			"total_rows": total_rows,
			"has_next": int(offset + page_size < total_rows),
		},
	}


def build_commercial_item_summary(spec: AccountExplorerQuerySpec) -> dict:
	roles = partition_identity_commercial_roles(spec.resolved_member_tuples)
	customers = roles["customers"]
	suppliers = roles["suppliers"]
	if not customers and not suppliers:
		return _shareholder_only_empty_result(spec, columns_key="item")

	rows: list[dict] = []
	for agg in _fetch_sales_aggregates(spec, customers):
		item_code = agg.get("item_code") or ""
		if not item_code:
			continue
		party = agg.get("party") or ""
		currency = agg.get("currency") or ""
		uom = _uom_label(agg.get("uom_sample"), agg.get("uom_distinct") or 0)
		rows.append(
			{
				"row_key": f"commercial:sales:{party}:{item_code}:{currency}",
				"item_code": item_code,
				"item_group": agg.get("item_group") or "",
				"display_code": item_code,
				"display_title": agg.get("item_name") or item_code,
				"transaction_direction": "Sales",
				"party_type": "Customer",
				"party": party,
				"quantity": flt(agg.get("quantity")),
				"stock_quantity": flt(agg.get("stock_quantity")),
				"uom": uom,
				"net_amount": flt(agg.get("net_amount")),
				"base_net_amount": flt(agg.get("base_net_amount")),
				"currency": currency,
				"document_count": cint(agg.get("document_count")),
				"return_quantity": flt(agg.get("return_quantity")),
				"return_base_net_amount": flt(agg.get("return_base_net_amount")),
				"is_virtual_group": 0,
				"drill_down_enabled": 0,
				"analysis_source": ANALYSIS_SOURCE_COMMERCIAL,
			}
		)

	for agg in _fetch_purchase_aggregates(spec, suppliers):
		item_code = agg.get("item_code") or ""
		if not item_code:
			continue
		party = agg.get("party") or ""
		currency = agg.get("currency") or ""
		uom = _uom_label(agg.get("uom_sample"), agg.get("uom_distinct") or 0)
		rows.append(
			{
				"row_key": f"commercial:purchase:{party}:{item_code}:{currency}",
				"item_code": item_code,
				"item_group": agg.get("item_group") or "",
				"display_code": item_code,
				"display_title": agg.get("item_name") or item_code,
				"transaction_direction": "Purchase",
				"party_type": "Supplier",
				"party": party,
				"quantity": flt(agg.get("quantity")),
				"stock_quantity": flt(agg.get("stock_quantity")),
				"uom": uom,
				"net_amount": flt(agg.get("net_amount")),
				"base_net_amount": flt(agg.get("base_net_amount")),
				"currency": currency,
				"document_count": cint(agg.get("document_count")),
				"return_quantity": flt(agg.get("return_quantity")),
				"return_base_net_amount": flt(agg.get("return_base_net_amount")),
				"is_virtual_group": 0,
				"drill_down_enabled": 0,
				"analysis_source": ANALYSIS_SOURCE_COMMERCIAL,
			}
		)

	result = _paginate_commercial_rows(
		rows,
		spec,
		sortable_fields=COMMERCIAL_ITEM_SORTABLE_FIELDS,
		total_keys=[
			"quantity",
			"stock_quantity",
			"net_amount",
			"base_net_amount",
			"document_count",
			"return_quantity",
			"return_base_net_amount",
		],
	)
	result["warnings"] = _limitation_warnings(spec, roles)
	result["analysis_source"] = ANALYSIS_SOURCE_COMMERCIAL
	result["commercial_semantics"] = _semantics_meta()
	result["unsupported_filter_combination"] = None
	return result


def build_commercial_item_group_summary(spec: AccountExplorerQuerySpec) -> dict:
	"""Leaf Item Group aggregation of commercial invoice item rows (no parent double-count)."""
	roles = partition_identity_commercial_roles(spec.resolved_member_tuples)
	customers = roles["customers"]
	suppliers = roles["suppliers"]
	if not customers and not suppliers:
		return _shareholder_only_empty_result(spec, columns_key="item_group")

	bucket: dict[str, dict] = defaultdict(
		lambda: {
			"sales_base_net_amount": 0.0,
			"purchase_base_net_amount": 0.0,
			"return_base_net_amount": 0.0,
			"document_count": 0,
			"items": set(),
		}
	)

	for agg in _fetch_sales_aggregates(spec, customers):
		group = (agg.get("item_group") or "").strip() or _("Unspecified Item Group")
		b = bucket[group]
		b["sales_base_net_amount"] += flt(agg.get("base_net_amount"))
		b["return_base_net_amount"] += flt(agg.get("return_base_net_amount"))
		b["document_count"] += cint(agg.get("document_count"))
		if agg.get("item_code"):
			b["items"].add(agg["item_code"])

	for agg in _fetch_purchase_aggregates(spec, suppliers):
		group = (agg.get("item_group") or "").strip() or _("Unspecified Item Group")
		b = bucket[group]
		b["purchase_base_net_amount"] += flt(agg.get("base_net_amount"))
		b["return_base_net_amount"] += flt(agg.get("return_base_net_amount"))
		b["document_count"] += cint(agg.get("document_count"))
		if agg.get("item_code"):
			b["items"].add(agg["item_code"])

	# Leaf-only presentation: skip is_group parents to avoid parent/child double display.
	rows: list[dict] = []
	from erpnext_extensions.iran_accounting.account_explorer.item_group_hierarchy import (
		get_item_group_node,
	)

	for group_name, b in bucket.items():
		meta = get_item_group_node(group_name) or {}
		if meta and cint(meta.get("is_group")):
			# Parent groups are filter expansion targets, not commercial grid rows.
			continue
		rows.append(
			{
				"row_key": f"commercial:item_group:{group_name}",
				"item_group": group_name,
				"display_code": group_name,
				"display_title": group_name,
				"sales_base_net_amount": flt(b["sales_base_net_amount"]),
				"purchase_base_net_amount": flt(b["purchase_base_net_amount"]),
				"return_base_net_amount": flt(b["return_base_net_amount"]),
				"base_net_amount": flt(b["sales_base_net_amount"]) + flt(b["purchase_base_net_amount"]),
				"document_count": cint(b["document_count"]),
				"item_count": len(b["items"]),
				"is_virtual_group": 0,
				"drill_down_enabled": 1,
				"is_group": 0,
				"analysis_source": ANALYSIS_SOURCE_COMMERCIAL,
			}
		)

	selected = None
	item_group_scope = getattr(spec.analysis, "item_group_scope", None)
	if item_group_scope:
		selected = item_group_scope.selected_item_group

	result = _paginate_commercial_rows(
		rows,
		spec,
		sortable_fields=COMMERCIAL_ITEM_GROUP_SORTABLE_FIELDS,
		total_keys=[
			"sales_base_net_amount",
			"purchase_base_net_amount",
			"return_base_net_amount",
			"base_net_amount",
			"document_count",
			"item_count",
		],
	)
	result["warnings"] = _limitation_warnings(spec, roles)
	result["analysis_source"] = ANALYSIS_SOURCE_COMMERCIAL
	result["commercial_semantics"] = _semantics_meta()
	result["selected_item_group"] = selected
	result["presentation_groups"] = []
	result["unsupported_filter_combination"] = None
	return result
