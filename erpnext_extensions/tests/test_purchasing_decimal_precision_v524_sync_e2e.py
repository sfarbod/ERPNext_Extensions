# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Integration: purchasing/asset DECIMAL(30,9) v5.2.4 schema + large IRR regression."""

from __future__ import annotations

import unittest
from decimal import Decimal

import frappe
from frappe.utils import nowdate

from erpnext_extensions.approved_decimal_precision import read_column_schema
from erpnext_extensions.decimal_precision_after_migrate import after_migrate as coordinator_after_migrate
from erpnext_extensions.patches.post_model_sync.expand_purchasing_amount_precision_v524 import (
	execute as expand_purchasing_amount_precision_v524_execute,
)
from erpnext_extensions.patches.pre_model_sync.set_purchasing_amount_decimal_metadata_v524 import (
	execute as set_purchasing_amount_decimal_metadata_v524_execute,
)
from erpnext_extensions.purchasing_decimal_precision_v524 import (
	CRITICAL_ASSET_FIELDS,
	PURCHASING_ROOT_DOCTYPES,
	assert_purchasing_decimal_schema,
	assert_purchasing_field_classification_completeness,
	get_purchasing_precision_status,
	purchasing_field_targets,
	repair_purchasing_decimal_schema,
)

PROD_NET_PURCHASE = Decimal("4250664632505")
PROD_FRACTION = Decimal("4250664632505.123456789")
LARGE_IRR_B = Decimal("10277543566399.460000000")

UPDATEDB_DOCTYPES = (
	"Purchase Order",
	"Purchase Order Item",
	"Purchase Receipt",
	"Purchase Receipt Item",
	"Purchase Invoice",
	"Purchase Invoice Item",
	"Purchase Taxes and Charges",
	"Asset",
	"Asset Finance Book",
	"Asset Value Adjustment",
)


def _col(table: str, column: str) -> tuple[int | None, int | None, str | None]:
	info = read_column_schema(table, column)
	if not info:
		return None, None, None
	return info.get("NUMERIC_PRECISION"), info.get("NUMERIC_SCALE"), info.get("COLUMN_TYPE")


def _as_decimal(value) -> Decimal:
	return Decimal(str(value))


def _force_decimal(table: str, column: str, precision: int) -> None:
	info = read_column_schema(table, column)
	null_sql = "NULL" if str(info.get("IS_NULLABLE")).upper() == "YES" else "NOT NULL"
	default = info.get("COLUMN_DEFAULT")
	default_sql = f" DEFAULT {frappe.db.escape(str(default))}" if default is not None else ""
	frappe.db.sql_ddl(
		f"ALTER TABLE `{table}` MODIFY `{column}` DECIMAL({precision},9) {null_sql}{default_sql}"
	)


def _apply_flags(doc):
	doc.flags.ignore_mandatory = True
	doc.flags.ignore_validate = True
	doc.flags.ignore_links = True
	doc.flags.ignore_permissions = True


def _fixtures():
	company = frappe.db.get_value("Company", {}, "name")
	supplier = frappe.db.get_value("Supplier", {}, "name")
	item = frappe.db.get_value("Item", {"disabled": 0, "is_purchase_item": 1}, "name") or frappe.db.get_value(
		"Item", {}, "name"
	)
	warehouse = (
		frappe.db.get_value("Warehouse", {"is_group": 0, "company": company}, "name") if company else None
	)
	if not company or not supplier or not item:
		return None
	return company, supplier, item, warehouse


class TestPurchasingDecimalPrecisionV524SyncE2E(unittest.TestCase):
	def test_patch_schema_guard_updatedb_and_idempotency(self):
		set_purchasing_amount_decimal_metadata_v524_execute()
		expand_purchasing_amount_precision_v524_execute()

		for target in purchasing_field_targets():
			if not frappe.db.exists("DocType", target.doctype):
				continue
			meta = frappe.get_meta(target.doctype)
			if not meta.get_field(target.fieldname) and not read_column_schema(target.table, target.fieldname):
				continue
			if not read_column_schema(target.table, target.fieldname):
				continue
			self.assertEqual(
				_col(target.table, target.fieldname),
				(30, 9, "decimal(30,9)"),
				f"{target.table}.{target.fieldname}",
			)

		for doctype in UPDATEDB_DOCTYPES:
			if frappe.db.exists("DocType", doctype):
				frappe.clear_cache(doctype=doctype)
				frappe.db.updatedb(doctype)

		for target in purchasing_field_targets():
			if target.doctype not in UPDATEDB_DOCTYPES:
				continue
			if not frappe.get_meta(target.doctype).get_field(target.fieldname):
				continue
			if not read_column_schema(target.table, target.fieldname):
				continue
			self.assertEqual(
				_col(target.table, target.fieldname),
				(30, 9, "decimal(30,9)"),
				f"sync {target.table}.{target.fieldname}",
			)

		before = get_purchasing_precision_status()
		set_purchasing_amount_decimal_metadata_v524_execute()
		second = repair_purchasing_decimal_schema(run_completeness_guard=True)
		self.assertEqual(second["repaired"], [])
		after = get_purchasing_precision_status()
		self.assertEqual(before["incorrect"], 0)
		self.assertEqual(after["incorrect"], 0)
		assert_purchasing_decimal_schema()
		assert_purchasing_field_classification_completeness()

	def test_updatedb_narrows_without_property_setter_and_repair_heals(self):
		ps_filters = {
			"doc_type": "Asset",
			"field_name": "net_purchase_amount",
			"property": "length",
			"doctype_or_field": "DocField",
		}
		set_purchasing_amount_decimal_metadata_v524_execute()
		expand_purchasing_amount_precision_v524_execute()
		ps_name = frappe.db.get_value("Property Setter", ps_filters, "name")
		self.assertTrue(ps_name, "Property Setter for Asset.net_purchase_amount must exist")

		_force_decimal("tabAsset", "net_purchase_amount", 30)
		self.assertEqual(_col("tabAsset", "net_purchase_amount")[2], "decimal(30,9)")

		frappe.delete_doc("Property Setter", ps_name, force=True, ignore_permissions=True)
		frappe.clear_cache(doctype="Asset")
		frappe.db.commit()
		frappe.db.updatedb("Asset")
		self.assertEqual(
			_col("tabAsset", "net_purchase_amount"),
			(21, 9, "decimal(21,9)"),
			"updatedb without length=30 Property Setter must narrow net_purchase_amount",
		)

		result = repair_purchasing_decimal_schema(run_completeness_guard=False)
		self.assertIn("tabAsset.net_purchase_amount", result["repaired"])
		self.assertEqual(_col("tabAsset", "net_purchase_amount"), (30, 9, "decimal(30,9)"))
		assert_purchasing_decimal_schema()

		frappe.clear_cache(doctype="Asset")
		frappe.db.updatedb("Asset")
		self.assertEqual(_col("tabAsset", "net_purchase_amount"), (30, 9, "decimal(30,9)"))

	def test_coordinator_after_migrate_heals_forced_drift(self):
		_force_decimal("tabAsset", "net_purchase_amount", 21)
		self.assertEqual(_col("tabAsset", "net_purchase_amount")[0], 21)
		coordinator_after_migrate()
		self.assertEqual(_col("tabAsset", "net_purchase_amount"), (30, 9, "decimal(30,9)"))
		status = get_purchasing_precision_status()
		self.assertEqual(status["incorrect"], 0)

	def test_production_asset_net_purchase_amount_insert(self):
		"""Mandatory regression for production DataError 1264 on Asset.net_purchase_amount."""
		set_purchasing_amount_decimal_metadata_v524_execute()
		expand_purchasing_amount_precision_v524_execute()
		self.assertEqual(_col("tabAsset", "net_purchase_amount"), (30, 9, "decimal(30,9)"))

		company = frappe.db.get_value("Company", {}, "name")
		if not company:
			self.skipTest("Missing Company")
		item = frappe.db.get_value("Item", {"is_fixed_asset": 1, "disabled": 0}, "name")
		asset_category = frappe.db.get_value("Asset Category", {}, "name")
		if not item:
			# Fall back to any item; Asset insert ignores validate
			item = frappe.db.get_value("Item", {"disabled": 0}, "name")
		if not item:
			self.skipTest("Missing Item")

		asset = frappe.new_doc("Asset")
		asset.name = f"v524-test-{frappe.generate_hash(length=8)}"
		asset.company = company
		asset.item_code = item
		asset.asset_name = asset.name
		asset.asset_category = asset_category
		asset.purchase_date = nowdate()
		asset.available_for_use_date = nowdate()
		asset.net_purchase_amount = float(PROD_NET_PURCHASE)
		asset.purchase_amount = float(PROD_NET_PURCHASE)
		if asset.meta.get_field("gross_purchase_amount"):
			asset.gross_purchase_amount = float(PROD_NET_PURCHASE)
		asset.total_asset_cost = float(PROD_NET_PURCHASE)
		asset.is_existing_asset = 1
		_apply_flags(asset)
		asset.insert(ignore_permissions=True)
		frappe.db.commit()

		reloaded = frappe.get_doc("Asset", asset.name)
		self.assertEqual(_as_decimal(reloaded.net_purchase_amount), PROD_NET_PURCHASE)

		frappe.db.sql(
			"UPDATE `tabAsset` SET net_purchase_amount=%s WHERE name=%s",
			(str(PROD_FRACTION), asset.name),
		)
		frappe.db.commit()
		raw = frappe.db.sql(
			"SELECT CAST(net_purchase_amount AS CHAR) FROM `tabAsset` WHERE name=%s",
			asset.name,
		)[0][0]
		self.assertEqual(_as_decimal(raw), PROD_FRACTION)

		frappe.delete_doc("Asset", asset.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_large_irr_purchase_order_round_trip(self):
		set_purchasing_amount_decimal_metadata_v524_execute()
		expand_purchasing_amount_precision_v524_execute()
		fixtures = _fixtures()
		if not fixtures:
			self.skipTest("Missing Company/Supplier/Item")
		company, supplier, item, _warehouse = fixtures
		amount = float(PROD_NET_PURCHASE)

		po = frappe.new_doc("Purchase Order")
		po.company = company
		po.supplier = supplier
		po.transaction_date = nowdate()
		po.schedule_date = nowdate()
		po.append("items", {"item_code": item, "qty": 1, "rate": amount, "schedule_date": nowdate()})
		_apply_flags(po)
		po.insert(ignore_permissions=True)
		frappe.db.commit()

		doc = frappe.get_doc("Purchase Order", po.name)
		doc.set("grand_total", amount)
		doc.set("base_grand_total", amount)
		doc.set("total", amount)
		doc.set("net_total", amount)
		if doc.items:
			doc.items[0].rate = amount
			doc.items[0].amount = amount
			doc.items[0].net_amount = amount
			doc.items[0].base_amount = amount
		_apply_flags(doc)
		doc.save(ignore_permissions=True)
		frappe.db.commit()

		reloaded = frappe.get_doc("Purchase Order", po.name)
		self.assertEqual(_as_decimal(reloaded.grand_total), PROD_NET_PURCHASE)
		self.assertEqual(_as_decimal(reloaded.items[0].amount), PROD_NET_PURCHASE)
		self.assertEqual(_as_decimal(reloaded.items[0].rate), PROD_NET_PURCHASE)

		frappe.delete_doc("Purchase Order", po.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_large_irr_purchase_receipt_and_invoice(self):
		set_purchasing_amount_decimal_metadata_v524_execute()
		expand_purchasing_amount_precision_v524_execute()
		fixtures = _fixtures()
		if not fixtures:
			self.skipTest("Missing Company/Supplier/Item")
		company, supplier, item, warehouse = fixtures
		amount = float(LARGE_IRR_B)

		pr = frappe.new_doc("Purchase Receipt")
		pr.company = company
		pr.supplier = supplier
		pr.posting_date = nowdate()
		pr.append(
			"items",
			{
				"item_code": item,
				"qty": 1,
				"rate": amount,
				"warehouse": warehouse,
			},
		)
		_apply_flags(pr)
		pr.insert(ignore_permissions=True)
		frappe.db.commit()
		pr_doc = frappe.get_doc("Purchase Receipt", pr.name)
		pr_doc.set("grand_total", amount)
		pr_doc.set("total", amount)
		if pr_doc.items:
			pr_doc.items[0].rate = amount
			pr_doc.items[0].amount = amount
		_apply_flags(pr_doc)
		pr_doc.save(ignore_permissions=True)
		frappe.db.commit()
		self.assertEqual(_as_decimal(frappe.get_doc("Purchase Receipt", pr.name).grand_total), LARGE_IRR_B)

		pi = frappe.new_doc("Purchase Invoice")
		pi.company = company
		pi.supplier = supplier
		pi.posting_date = nowdate()
		pi.append(
			"items",
			{
				"item_code": item,
				"qty": 1,
				"rate": amount,
				"warehouse": warehouse,
			},
		)
		_apply_flags(pi)
		pi.insert(ignore_permissions=True)
		frappe.db.commit()
		pi_doc = frappe.get_doc("Purchase Invoice", pi.name)
		pi_doc.set("grand_total", amount)
		pi_doc.set("outstanding_amount", amount)
		pi_doc.set("total", amount)
		if pi_doc.items:
			pi_doc.items[0].rate = amount
			pi_doc.items[0].amount = amount
		_apply_flags(pi_doc)
		pi_doc.save(ignore_permissions=True)
		frappe.db.commit()
		reloaded_pi = frappe.get_doc("Purchase Invoice", pi.name)
		self.assertEqual(_as_decimal(reloaded_pi.grand_total), LARGE_IRR_B)
		self.assertEqual(_as_decimal(reloaded_pi.outstanding_amount), LARGE_IRR_B)

		frappe.delete_doc("Purchase Invoice", pi.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Purchase Receipt", pr.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_landed_cost_and_asset_value_adjustment(self):
		set_purchasing_amount_decimal_metadata_v524_execute()
		expand_purchasing_amount_precision_v524_execute()
		amount = float(PROD_NET_PURCHASE)

		# Landed Cost Item amount/rate already owned by stock_repost; assert schema + SQL round-trip.
		self.assertEqual(_col("tabLanded Cost Item", "amount"), (30, 9, "decimal(30,9)"))
		self.assertEqual(_col("tabLanded Cost Voucher", "total_taxes_and_charges"), (30, 9, "decimal(30,9)"))

		company = frappe.db.get_value("Company", {}, "name")
		asset_name = frappe.db.get_value("Asset", {"docstatus": ["<", 2]}, "name")
		if not company:
			self.skipTest("Missing Company")

		adj = frappe.new_doc("Asset Value Adjustment")
		adj.company = company
		adj.asset = asset_name
		adj.date = nowdate()
		adj.current_asset_value = amount
		adj.new_asset_value = float(LARGE_IRR_B)
		adj.difference_amount = float(Decimal(str(LARGE_IRR_B)) - PROD_NET_PURCHASE)
		_apply_flags(adj)
		adj.insert(ignore_permissions=True)
		frappe.db.commit()
		reloaded = frappe.get_doc("Asset Value Adjustment", adj.name)
		self.assertEqual(_as_decimal(reloaded.current_asset_value), PROD_NET_PURCHASE)
		self.assertEqual(_as_decimal(reloaded.new_asset_value), LARGE_IRR_B)
		frappe.delete_doc("Asset Value Adjustment", adj.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_fixed_asset_acquisition_amount_path(self):
		"""PI/PR-scale acquisition value can land on Asset.net_purchase_amount."""
		set_purchasing_amount_decimal_metadata_v524_execute()
		expand_purchasing_amount_precision_v524_execute()
		fixtures = _fixtures()
		if not fixtures:
			self.skipTest("Missing fixtures")
		company, supplier, item, warehouse = fixtures
		amount = float(PROD_NET_PURCHASE)

		pi = frappe.new_doc("Purchase Invoice")
		pi.company = company
		pi.supplier = supplier
		pi.posting_date = nowdate()
		pi.append("items", {"item_code": item, "qty": 1, "rate": amount, "warehouse": warehouse})
		_apply_flags(pi)
		pi.insert(ignore_permissions=True)
		frappe.db.commit()
		pi_doc = frappe.get_doc("Purchase Invoice", pi.name)
		pi_doc.set("grand_total", amount)
		if pi_doc.items:
			pi_doc.items[0].amount = amount
			pi_doc.items[0].rate = amount
		_apply_flags(pi_doc)
		pi_doc.save(ignore_permissions=True)
		frappe.db.commit()

		asset = frappe.new_doc("Asset")
		asset.name = f"v524-acq-{frappe.generate_hash(length=8)}"
		asset.company = company
		asset.item_code = item
		asset.asset_name = asset.name
		asset.purchase_date = nowdate()
		asset.available_for_use_date = nowdate()
		asset.purchase_invoice = pi.name
		asset.net_purchase_amount = amount
		asset.purchase_amount = amount
		asset.total_asset_cost = amount
		asset.is_existing_asset = 1
		_apply_flags(asset)
		asset.insert(ignore_permissions=True)
		frappe.db.commit()

		self.assertEqual(_as_decimal(frappe.get_doc("Asset", asset.name).net_purchase_amount), PROD_NET_PURCHASE)
		self.assertEqual(_as_decimal(frappe.get_doc("Purchase Invoice", pi.name).grand_total), PROD_NET_PURCHASE)

		frappe.delete_doc("Asset", asset.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Purchase Invoice", pi.name, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_root_doctypes_listed(self):
		for dt in (
			"Purchase Order",
			"Purchase Receipt",
			"Purchase Invoice",
			"Asset",
			"Landed Cost Voucher",
		):
			self.assertIn(dt, PURCHASING_ROOT_DOCTYPES)
		for field in CRITICAL_ASSET_FIELDS:
			if field == "gross_purchase_amount" and not read_column_schema("tabAsset", field):
				continue
			self.assertEqual(_col("tabAsset", field)[2], "decimal(30,9)", field)
