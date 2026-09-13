# Copyright (c) 2026, ERPNext Extensions contributors
"""5.2.2 I4: valid Material Transfer / consume-to-zero must submit; real leftover still blocks."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt, random_string

from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
	ValuationIntegrityError,
	assert_zero_qty_stock_value,
	mark_sle_running_balance_processed,
)
from erpnext_extensions.iran_accounting.e2e_bootstrap import (
	apply_stock_entry_site_defaults,
	enable_perpetual_inventory,
	ensure_test_item,
	get_irr_company,
	get_second_warehouse,
	get_warehouse,
	submit_material_receipt,
)
from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_bootstrap
from erpnext_extensions.iran_accounting.tests.hardening.builders import run_riv


QTY = 300
RATE = 15_180_000
VALUE = 4_554_000_000


def _bin(item: str, warehouse: str):
	row = frappe.db.get_value(
		"Bin",
		{"item_code": item, "warehouse": warehouse},
		["actual_qty", "valuation_rate", "stock_value"],
		as_dict=True,
	)
	return row or frappe._dict(actual_qty=0, valuation_rate=0, stock_value=0)


def _sles(voucher_no: str, warehouse: str | None = None):
	filters = {
		"voucher_type": "Stock Entry",
		"voucher_no": voucher_no,
		"is_cancelled": 0,
	}
	if warehouse:
		filters["warehouse"] = warehouse
	return frappe.get_all(
		"Stock Ledger Entry",
		filters=filters,
		fields=[
			"name",
			"warehouse",
			"actual_qty",
			"qty_after_transaction",
			"valuation_rate",
			"stock_value",
			"stock_value_difference",
			"incoming_rate",
			"outgoing_rate",
			"serial_and_batch_bundle",
			"batch_no",
		],
		order_by="actual_qty asc, creation asc",
	)


def _fill_required_batch_fields(batch) -> None:
	"""Populate site-required Batch fields through the document API (not SQL)."""
	meta = frappe.get_meta("Batch")
	ident = batch.batch_id or batch.name or random_string(8)
	if meta.has_field("custom_batch_no") and not batch.get("custom_batch_no"):
		batch.custom_batch_no = ident
	for df in meta.fields:
		if not df.reqd or batch.get(df.fieldname) not in (None, ""):
			continue
		if df.fieldname in ("naming_series", "item", "batch_id"):
			continue
		if df.fieldtype in ("Data", "Small Text", "Text", "Long Text"):
			batch.set(df.fieldname, ident)


def make_valid_batch(item_code: str) -> str:
	ident = f"I4B-{random_string(8)}"
	batch = frappe.new_doc("Batch")
	batch.item = item_code
	batch.batch_id = ident
	_fill_required_batch_fields(batch)
	batch.insert(ignore_permissions=True)
	frappe.db.commit()
	return batch.name


class TestI4MaterialTransferLifecycle(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		apply_bootstrap()
		frappe.set_user("Administrator")
		frappe.flags.iran_gate_defaults = True
		cls.company = get_irr_company("ESPAD")
		enable_perpetual_inventory(cls.company)
		cls.source = get_warehouse(cls.company)
		cls.target = get_second_warehouse(cls.company, cls.source)
		if cls.target == cls.source:
			raise unittest.SkipTest("Need two warehouses for Material Transfer tests")

	def setUp(self):
		frappe.flags.iran_gate_defaults = True
		frappe.flags.ignore_serial_batch_bundle_validation = False

	def _item(self, tag: str, *, batch: bool = False) -> str:
		item = ensure_test_item(self.company, f"I4-{tag}")
		values = {
			"is_stock_item": 1,
			"valuation_method": "Moving Average",
			"has_serial_no": 0,
		}
		if batch:
			values.update({"has_batch_no": 1, "create_new_batch": 0})
		else:
			values.update({"has_batch_no": 0})
		frappe.db.set_value("Item", item, values, update_modified=False)
		frappe.db.commit()
		return item

	def _submit_se(
		self,
		*,
		purpose: str,
		item: str,
		qty: float,
		rate=None,
		source=None,
		target=None,
		batch_no=None,
		is_finished_item=0,
	):
		from erpnext.stock.doctype.stock_entry.stock_entry_utils import make_stock_entry

		kwargs = dict(
			item_code=item,
			qty=qty,
			company=self.company,
			purpose=purpose,
			do_not_save=True,
			do_not_submit=True,
		)
		if source:
			kwargs["source"] = source
		if target:
			kwargs["target"] = target
		if rate is not None:
			kwargs["rate"] = rate
		if batch_no:
			kwargs["batch_no"] = batch_no
			kwargs["use_serial_batch_fields"] = 1
		se = make_stock_entry(**kwargs)
		if is_finished_item:
			for row in se.items:
				row.is_finished_item = 1
		apply_stock_entry_site_defaults(se)
		se.insert(ignore_permissions=True)
		se.submit()
		frappe.db.commit()
		return se

	def _assert_balanced_transfer(self, se, expected_value=None):
		se.reload()
		self.assertEqual(flt(se.value_difference), 0, se.as_dict())
		if expected_value is not None:
			self.assertEqual(flt(se.total_outgoing_value), expected_value)
			self.assertEqual(flt(se.total_incoming_value), expected_value)

	def test_a_partial_transfer_299_of_300(self):
		item = self._item("A")
		submit_material_receipt(self.company, item, QTY, RATE, self.source)
		se = self._submit_se(
			purpose="Material Transfer",
			item=item,
			qty=299,
			source=self.source,
			target=self.target,
		)
		self.assertEqual(se.docstatus, 1)
		self.assertEqual(flt(_bin(item, self.source).actual_qty), 1)

	def test_b_full_non_batch_transfer_incident_economics(self):
		item = self._item("B")
		submit_material_receipt(self.company, item, QTY, RATE, self.source)
		before = _bin(item, self.source)
		self.assertEqual(flt(before.actual_qty), QTY)
		self.assertEqual(flt(before.valuation_rate), RATE)
		self.assertEqual(flt(before.stock_value), VALUE)

		se = self._submit_se(
			purpose="Material Transfer",
			item=item,
			qty=QTY,
			source=self.source,
			target=self.target,
		)
		self.assertEqual(se.docstatus, 1)
		self._assert_balanced_transfer(se, VALUE)

		after_source = _bin(item, self.source)
		after_target = _bin(item, self.target)
		self.assertEqual(flt(after_source.actual_qty), 0)
		self.assertEqual(flt(after_source.stock_value), 0)
		self.assertEqual(flt(after_target.actual_qty), QTY)
		self.assertEqual(flt(after_target.stock_value), VALUE)

		source_sles = [s for s in _sles(se.name, self.source) if flt(s.actual_qty) < 0]
		target_sles = [s for s in _sles(se.name, self.target) if flt(s.actual_qty) > 0]
		self.assertTrue(source_sles)
		self.assertTrue(target_sles)
		self.assertEqual(flt(source_sles[0].qty_after_transaction), 0)
		self.assertEqual(flt(source_sles[0].stock_value), 0)
		self.assertEqual(flt(target_sles[0].actual_qty), QTY)
		self.assertEqual(flt(target_sles[0].qty_after_transaction), QTY)
		self.assertEqual(flt(target_sles[0].stock_value_difference), VALUE)

	def test_c_partial_transfer_100_of_300(self):
		item = self._item("C")
		submit_material_receipt(self.company, item, QTY, RATE, self.source)
		se = self._submit_se(
			purpose="Material Transfer",
			item=item,
			qty=100,
			source=self.source,
			target=self.target,
		)
		self.assertEqual(se.docstatus, 1)
		self.assertEqual(flt(_bin(item, self.source).actual_qty), 200)
		self.assertEqual(flt(_bin(item, self.target).actual_qty), 100)

	def test_d_full_same_rate_material_transfer(self):
		item = self._item("D")
		rate = 1_000_000
		qty = 50
		submit_material_receipt(self.company, item, qty, rate, self.source)
		se = self._submit_se(
			purpose="Material Transfer",
			item=item,
			qty=qty,
			source=self.source,
			target=self.target,
		)
		self.assertEqual(se.docstatus, 1)
		self._assert_balanced_transfer(se, qty * rate)
		self.assertEqual(flt(_bin(item, self.source).actual_qty), 0)
		self.assertEqual(flt(_bin(item, self.source).stock_value), 0)

	def test_e_full_material_issue_to_zero(self):
		item = self._item("E")
		submit_material_receipt(self.company, item, QTY, RATE, self.source)
		se = self._submit_se(
			purpose="Material Issue",
			item=item,
			qty=QTY,
			source=self.source,
		)
		self.assertEqual(se.docstatus, 1)
		self.assertEqual(flt(_bin(item, self.source).actual_qty), 0)
		self.assertEqual(flt(_bin(item, self.source).stock_value), 0)
		outgoing = [s for s in _sles(se.name, self.source) if flt(s.actual_qty) < 0]
		self.assertEqual(flt(outgoing[0].qty_after_transaction), 0)
		self.assertEqual(flt(outgoing[0].stock_value), 0)

	def test_f_manufacture_full_consume(self):
		rm = self._item("F-RM")
		fg = self._item("F-FG")
		submit_material_receipt(self.company, rm, QTY, RATE, self.source)
		se = frappe.new_doc("Stock Entry")
		se.company = self.company
		se.stock_entry_type = "Manufacture"
		se.purpose = "Manufacture"
		se.append(
			"items",
			{
				"item_code": rm,
				"qty": QTY,
				"transfer_qty": QTY,
				"conversion_factor": 1,
				"s_warehouse": self.source,
				"basic_rate": RATE,
			},
		)
		se.append(
			"items",
			{
				"item_code": fg,
				"qty": QTY,
				"transfer_qty": QTY,
				"conversion_factor": 1,
				"t_warehouse": self.target,
				"is_finished_item": 1,
			},
		)
		apply_stock_entry_site_defaults(se)
		se.insert(ignore_permissions=True)
		se.submit()
		frappe.db.commit()
		self.assertEqual(se.docstatus, 1)
		self.assertEqual(flt(_bin(rm, self.source).actual_qty), 0)
		self.assertEqual(flt(_bin(rm, self.source).stock_value), 0)
		self.assertGreaterEqual(flt(_bin(fg, self.target).stock_value), 0)

	def test_g_processed_leftover_value_still_blocks_i4(self):
		sle = frappe._dict(
			actual_qty=-300,
			qty_after_transaction=0,
			stock_value=100_000,
			stock_value_difference=-4_554_000_000,
			company=self.company,
			voucher_type="Stock Entry",
			valuation_method="Moving Average",
			item_code="I4-CORRUPTION-FIXTURE",
			warehouse=self.source,
		)
		mark_sle_running_balance_processed(sle)
		with self.assertRaises(ValuationIntegrityError) as ctx:
			assert_zero_qty_stock_value(sle)
		msg = str(ctx.exception)
		self.assertIn("Stock valuation integrity (I4)", msg)
		self.assertIn("qty_after_transaction is 0 but stock_value leftover exceeds ±1 IRR quantum", msg)

	def test_h_incident_incoming_transient_is_not_i4(self):
		sle = frappe._dict(
			actual_qty=300,
			qty_after_transaction=0,
			incoming_rate=RATE,
			outgoing_rate=0,
			valuation_rate=RATE,
			stock_value_difference=VALUE,
			stock_value=VALUE,
			company=self.company,
			voucher_type="Stock Entry",
			valuation_method="Moving Average",
			item_code="I4-TRANSIENT-FIXTURE",
			warehouse=self.target,
		)
		assert_zero_qty_stock_value(sle)
		mark_sle_running_balance_processed(sle)
		assert_zero_qty_stock_value(sle)

	def test_i_batch_sabb_full_material_transfer(self):
		item = self._item("I-SABB", batch=True)
		batch_no = make_valid_batch(item)
		receipt = self._submit_se(
			purpose="Material Receipt",
			item=item,
			qty=QTY,
			rate=RATE,
			target=self.source,
			batch_no=batch_no,
		)
		self.assertEqual(receipt.docstatus, 1)
		self.assertTrue(
			any(r.serial_and_batch_bundle or r.batch_no for r in _sles(receipt.name)),
			"receipt must persist a batch / SABB",
		)

		se = self._submit_se(
			purpose="Material Transfer",
			item=item,
			qty=QTY,
			source=self.source,
			target=self.target,
			batch_no=batch_no,
		)
		self.assertEqual(se.docstatus, 1)
		self._assert_balanced_transfer(se, VALUE)
		self.assertEqual(flt(_bin(item, self.source).actual_qty), 0)
		self.assertEqual(flt(_bin(item, self.source).stock_value), 0)
		self.assertEqual(flt(_bin(item, self.target).actual_qty), QTY)
		self.assertEqual(flt(_bin(item, self.target).stock_value), VALUE)

		source_sles = [s for s in _sles(se.name, self.source) if flt(s.actual_qty) < 0]
		target_sles = [s for s in _sles(se.name, self.target) if flt(s.actual_qty) > 0]
		self.assertTrue(source_sles and target_sles)
		self.assertTrue(source_sles[0].serial_and_batch_bundle or source_sles[0].batch_no)
		self.assertTrue(target_sles[0].serial_and_batch_bundle or target_sles[0].batch_no)
		self.assertEqual(flt(source_sles[0].qty_after_transaction), 0)
		self.assertEqual(flt(source_sles[0].stock_value), 0)
		self.assertEqual(flt(target_sles[0].stock_value_difference), VALUE)

	def test_repost_full_consume_to_zero_still_balanced(self):
		item = self._item("RIV")
		submit_material_receipt(self.company, item, QTY, RATE, self.source)
		se = self._submit_se(
			purpose="Material Transfer",
			item=item,
			qty=QTY,
			source=self.source,
			target=self.target,
		)
		self._assert_balanced_transfer(se, VALUE)
		riv = run_riv(self.company, "Stock Entry", se.name)
		self.assertIsNotNone(riv)
		riv.reload()
		self.assertNotIn(riv.status, ("Failed", "Skipped"))
		se.reload()
		self._assert_balanced_transfer(se, VALUE)
		self.assertEqual(flt(_bin(item, self.source).actual_qty), 0)
		self.assertEqual(flt(_bin(item, self.source).stock_value), 0)
		source_sles = [s for s in _sles(se.name, self.source) if flt(s.actual_qty) < 0]
		self.assertEqual(flt(source_sles[0].qty_after_transaction), 0)
		self.assertEqual(flt(source_sles[0].stock_value), 0)
		# Genuine leftover on a processed SLE must still raise I4 after the same wrapper.
		corrupt = frappe._dict(source_sles[0])
		corrupt.stock_value = 100_000
		mark_sle_running_balance_processed(corrupt)
		with self.assertRaises(ValuationIntegrityError) as ctx:
			assert_zero_qty_stock_value(corrupt)
		self.assertIn("Stock valuation integrity (I4)", str(ctx.exception))
