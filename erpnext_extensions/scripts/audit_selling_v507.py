"""One-off audit helper — run: bench --site SITE execute erpnext_extensions.scripts.audit_selling_v507.run"""
from __future__ import annotations

import frappe
from frappe.utils import cint

SELLING_ROOT_DOCTYPES = ("Sales Order", "Delivery Note", "Sales Invoice")

RATE_EXACT = frozenset(
	{
		"rate",
		"price_list_rate",
		"base_price_list_rate",
		"stock_uom_rate",
		"net_rate",
		"base_net_rate",
		"incoming_rate",
		"valuation_rate",
		"blanket_order_rate",
		"ref_exchange_rate",
	}
)
RATE_SUBSTR = ("conversion_rate", "plc_conversion_rate", "commission_rate", "tax_rate")
NON_AMOUNT_FLOAT = frozenset({"total_billing_hours", "billing_hours", "discount"})


def discover_related_doctypes() -> tuple[str, ...]:
	related: set[str] = set(SELLING_ROOT_DOCTYPES)
	changed = True
	while changed:
		changed = False
		for dt in list(related):
			if not frappe.db.exists("DocType", dt):
				continue
			meta = frappe.get_meta(dt, cached=False)
			for df in meta.fields:
				if df.fieldtype == "Table" and df.options and df.options not in related:
					related.add(df.options)
					changed = True
	return tuple(sorted(related))


def classify_selling_field(df) -> str | None:
	"""Return amount | rate_pct | virtual | None (non monetary)."""
	if cint(getattr(df, "is_virtual", 0)):
		return "virtual"
	ft = df.fieldtype
	if ft == "Percent":
		return "rate_pct"
	if ft not in ("Currency", "Float"):
		return None
	fn = (df.fieldname or "").lower()
	if fn in RATE_EXACT:
		return "rate_pct"
	for sub in RATE_SUBSTR:
		if sub in fn:
			return "rate_pct"
	if fn.endswith("_rate") and "amount" not in fn:
		return "rate_pct"
	if fn.endswith("_percentage") or fn in {"discount_percentage", "margin_rate_or_amount", "allocated_percentage"}:
		return "rate_pct"
	if fn.endswith("_qty") or fn in {"qty", "conversion_factor"} or "weight" in fn:
		return "rate_pct"
	if fn == "invoice_portion":
		return "rate_pct"
	if ft == "Float" and fn in NON_AMOUNT_FLOAT:
		return "rate_pct"
	return "amount"


def run() -> None:
	related = discover_related_doctypes()
	amounts: dict[str, list[str]] = {}
	excluded: dict[str, list[tuple[str, str, str]]] = {}
	custom_amounts: dict[str, list[str]] = {}

	for dt in related:
		meta = frappe.get_meta(dt, cached=False)
		for df in meta.fields:
			cls = classify_selling_field(df)
			if cls == "amount":
				amounts.setdefault(dt, []).append(df.fieldname)
			elif cls in ("rate_pct", "virtual"):
				excluded.setdefault(dt, []).append((df.fieldname, df.fieldtype, cls))

		for cf in frappe.get_all(
			"Custom Field",
			filters={"dt": dt},
			fields=["fieldname", "fieldtype", "is_virtual"],
		):
			df = frappe._dict(cf)
			df.is_virtual = cf.get("is_virtual") or 0
			cls = classify_selling_field(df)
			if cls == "amount":
				custom_amounts.setdefault(dt, []).append(cf.fieldname)

	db_name = frappe.db.sql("SELECT DATABASE()")[0][0]
	not_30 = []
	for dt, fields in sorted(amounts.items()):
		table = f"tab{dt}"
		for field in sorted(fields):
			row = frappe.db.sql(
				"""
				SELECT COLUMN_TYPE, NUMERIC_PRECISION, NUMERIC_SCALE
				FROM INFORMATION_SCHEMA.COLUMNS
				WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s
				""",
				(db_name, table, field),
				as_dict=True,
			)
			if not row:
				continue
			p, s = row[0].get("NUMERIC_PRECISION"), row[0].get("NUMERIC_SCALE")
			if not (p == 30 and s == 9):
				not_30.append((dt, field, row[0].get("COLUMN_TYPE")))

	print("RELATED", len(related), related)
	print("AMOUNT_FIELDS", sum(len(v) for v in amounts.values()))
	for dt in sorted(amounts):
		print(f"  {dt!r}: {tuple(sorted(amounts[dt]))}")
	print("CUSTOM", custom_amounts)
	print("NOT_30_9", len(not_30))
	for r in not_30:
		print(" ", r)

	print("\n# EXCLUDED_RATE_PERCENT_FIELDS_BY_DOCTYPE")
	for dt in sorted(excluded):
		fields = tuple(sorted({f for f, _ft, _c in excluded[dt] if _c == "rate_pct"}))
		if fields:
			print(f"\t{dt!r}: {fields},")

	print("\n# EXCLUDED_VIRTUAL_FIELDS_BY_DOCTYPE")
	for dt in sorted(excluded):
		fields = tuple(sorted({f for f, _ft, _c in excluded[dt] if _c == "virtual"}))
		if fields:
			print(f"\t{dt!r}: {fields},")


def print_post_migration_stats() -> None:
	from erpnext_extensions.selling_documents_decimal_precision_v507 import (
		SELLING_AMOUNT_FIELDS_BY_DOCTYPE,
		audit_report_rows,
		selling_related_doctypes,
	)

	rows = audit_report_rows()
	amount_rows = [r for r in rows if r["classification"] == "Monetary Amount"]
	excluded_rows = [r for r in rows if "Excluded" in r["classification"]]
	print("STATS")
	print("related_doctypes", len(selling_related_doctypes()))
	print("inspected_fields", len(rows))
	print("monetary_amount_fields", len(amount_rows))
	print("excluded_fields", len(excluded_rows))
	print("allowlist_fields", sum(len(v) for v in SELLING_AMOUNT_FIELDS_BY_DOCTYPE.values()))
	for r in sorted(amount_rows, key=lambda x: (x["doctype"], x["field"])):
		print(
			f"{r['doctype']}|{r['table']}|{r['field']}|{r['fieldtype']}|{r['classification']}|{r['old_sql']}|{r['new_sql']}"
		)
