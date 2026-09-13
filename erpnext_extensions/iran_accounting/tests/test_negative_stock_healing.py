# Copyright (c) 2026, ERPNext Extensions contributors
"""5.2.3: incoming movements that heal an existing negative running qty may proceed."""

from __future__ import annotations

import unittest

import frappe
from frappe.utils import flt

from erpnext.stock.stock_ledger import NegativeStockError
from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
	BatchNegativeStockError,
)

from erpnext_extensions.iran_accounting.domain.negative_stock_healing import (
	is_negative_stock_healing_movement,
)
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
)
from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_bootstrap
from erpnext_extensions.iran_accounting.tests.hardening.builders import run_riv
from erpnext_extensions.iran_accounting.tests.test_i4_material_transfer_lifecycle import (
	make_valid_batch,
)


PREV = -565.17
INCOMING = 300
HEALED = -265.17
RATE = 1_000_000


class TestNegativeStockHealingPredicate(unittest.TestCase):
	def test_production_shaped_still_negative(self):
		self.assertTrue(is_negative_stock_healing_movement(PREV, INCOMING, HEALED))

	def test_heal_to_positive(self):
		self.assertTrue(is_negative_stock_healing_movement(PREV, 600, 34.83))

	def test_heal_to_zero(self):
		self.assertTrue(is_negative_stock_healing_movement(PREV, 565.17, 0))

	def test_outgoing_creates_negative(self):
		self.assertFalse(is_negative_stock_healing_movement(100, -300, -200))

	def test_outgoing_from_zero(self):
		self.assertFalse(is_negative_stock_healing_movement(0, -100, -100))

	def test_worsens_existing_negative(self):
		self.assertFalse(is_negative_stock_healing_movement(PREV, -100, -665.17))

	def test_incoming_into_zero_is_not_healing(self):
		self.assertFalse(is_negative_stock_healing_movement(0, 300, 300))


class TestNegativeStockHealingIntegration(unittest.TestCase):
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
			raise unittest.SkipTest("Need two warehouses")
		if not frappe.db.exists("UOM", "Gram"):
			uom = frappe.new_doc("UOM")
			uom.uom_name = "Gram"
			uom.insert(ignore_permissions=True)
			frappe.db.commit()
		cls._orig_neg = frappe.db.get_single_value("Stock Settings", "allow_negative_stock")
		cls._orig_neg_batch = frappe.db.get_single_value("Stock Settings", "allow_negative_stock_for_batch")

	def setUp(self):
		frappe.flags.iran_gate_defaults = True
		frappe.flags.ignore_serial_batch_bundle_validation = False
		self._set_allow_negative(0)

	def tearDown(self):
		self._set_allow_negative(self._orig_neg, batch=self._orig_neg_batch)

	def _set_allow_negative(self, on, *, batch=None):
		frappe.db.set_single_value("Stock Settings", "allow_negative_stock", 1 if on else 0)
		if batch is not None:
			frappe.db.set_single_value("Stock Settings", "allow_negative_stock_for_batch", 1 if batch else 0)
		elif on:
			# Seeding a negative *batch* ledger also needs the batch-level flag.
			frappe.db.set_single_value("Stock Settings", "allow_negative_stock_for_batch", 1)
		else:
			frappe.db.set_single_value("Stock Settings", "allow_negative_stock_for_batch", 0)
		frappe.clear_cache()
		frappe.clear_document_cache("Stock Settings", "Stock Settings")

	def _item(self, tag, *, batch=False):
		item = ensure_test_item(self.company, f"H523-{tag}", stock_uom="Gram")
		values = {
			"is_stock_item": 1,
			"valuation_method": "Moving Average",
			"has_serial_no": 0,
			"allow_negative_stock": 0,
			"stock_uom": "Gram",
		}
		if batch:
			values.update({"has_batch_no": 1, "create_new_batch": 0})
		else:
			values["has_batch_no"] = 0
		frappe.db.set_value("Item", item, values, update_modified=False)
		frappe.db.commit()
		return item

	def _submit_se(self, *, purpose, item, qty, rate=None, source=None, target=None, batch_no=None):
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
		apply_stock_entry_site_defaults(se)
		se.insert(ignore_permissions=True)
		se.submit()
		frappe.db.commit()
		return se

	def _submit_se_allowing_seeded_negative(self, **kwargs):
		"""Seed a historical negative warehouse/batch ledger without weakening production guards.

		SABB.validate defaults allow_negative_stock=False, and update_batch_qty rejects a
		negative Batch.batch_qty even when Stock Settings temporarily allow warehouse
		negative stock. Both are test-seed only.
		"""
		from erpnext.stock.doctype.serial_and_batch_bundle.serial_and_batch_bundle import (
			SerialandBatchBundle,
		)
		import erpnext.stock.serial_batch_bundle as sbb_mod

		saved_neg = SerialandBatchBundle.validate_negative_batch
		saved_inv = SerialandBatchBundle.validate_batch_inventory
		saved_throw = sbb_mod.throw_negative_batch_validation
		SerialandBatchBundle.validate_negative_batch = lambda self, batch_no, available_qty: None
		SerialandBatchBundle.validate_batch_inventory = lambda self: None
		sbb_mod.throw_negative_batch_validation = lambda *a, **k: None
		try:
			return self._submit_se(**kwargs)
		finally:
			SerialandBatchBundle.validate_negative_batch = saved_neg
			SerialandBatchBundle.validate_batch_inventory = saved_inv
			sbb_mod.throw_negative_batch_validation = saved_throw

	def _sle_qty(self, item, warehouse):
		row = frappe.db.sql(
			"""
			select qty_after_transaction, valuation_rate, stock_value, stock_value_difference, actual_qty
			from `tabStock Ledger Entry`
			where item_code=%s and warehouse=%s and is_cancelled=0
			order by posting_datetime desc, creation desc
			limit 1
			""",
			(item, warehouse),
			as_dict=True,
		)
		return row[0] if row else frappe._dict()

	def _seed_negative_target(self, item, qty=PREV, *, batch_no=None):
		"""Leave target running qty = qty (negative) using a temporary allow-negative seed."""
		self._set_allow_negative(1)
		positive = 100
		issue = positive - qty  # 100 - (-565.17) = 665.17
		self._submit_se(
			purpose="Material Receipt",
			item=item,
			qty=positive,
			rate=RATE,
			target=self.target,
			batch_no=batch_no,
		)
		self._submit_se_allowing_seeded_negative(
			purpose="Material Issue",
			item=item,
			qty=issue,
			source=self.target,
			batch_no=batch_no,
		)
		self._set_allow_negative(0)
		self.assertAlmostEqual(flt(self._sle_qty(item, self.target).qty_after_transaction), qty, places=2)
		if batch_no:
			# Production-like desync: SLE running qty is negative, Batch.batch_qty is not.
			# The healing transfer nets to 0 on global batch qty and must not be blocked by it.
			source_qty = flt(self._sle_qty(item, self.source).qty_after_transaction)
			frappe.db.set_value("Batch", batch_no, "batch_qty", max(source_qty, 0), update_modified=False)
			frappe.db.commit()

	def test_a_production_shaped_healing(self):
		item = self._item("A")
		self._submit_se(
			purpose="Material Receipt", item=item, qty=INCOMING, rate=RATE, target=self.source
		)
		self._seed_negative_target(item)
		before = self._sle_qty(item, self.target)
		se = self._submit_se(
			purpose="Material Transfer",
			item=item,
			qty=INCOMING,
			source=self.source,
			target=self.target,
		)
		self.assertEqual(se.docstatus, 1)
		after = self._sle_qty(item, self.target)
		self.assertAlmostEqual(flt(after.qty_after_transaction), HEALED, places=2)
		self.assertAlmostEqual(flt(self._sle_qty(item, self.source).qty_after_transaction), 0, places=2)
		qty_after = flt(after.qty_after_transaction)
		rate = flt(after.valuation_rate)
		value = flt(after.stock_value)
		self.assertNotEqual(qty_after, 0)
		self.assertAlmostEqual(qty_after * rate, value, delta=1)
		self.last_target_valuation = {
			"before_qty": flt(before.qty_after_transaction),
			"qty_after": flt(after.qty_after_transaction),
			"valuation_rate": flt(after.valuation_rate),
			"stock_value": flt(after.stock_value),
			"stock_value_difference": flt(after.stock_value_difference),
			"actual_qty": flt(after.actual_qty),
		}
		print("TEST_A_TARGET_VALUATION", self.last_target_valuation)

	def test_b_heal_to_positive(self):
		item = self._item("B")
		self._submit_se(purpose="Material Receipt", item=item, qty=600, rate=RATE, target=self.source)
		self._seed_negative_target(item)
		se = self._submit_se(
			purpose="Material Transfer", item=item, qty=600, source=self.source, target=self.target
		)
		self.assertEqual(se.docstatus, 1)
		self.assertAlmostEqual(flt(self._sle_qty(item, self.target).qty_after_transaction), 34.83, places=2)

	def test_c_heal_exactly_to_zero(self):
		item = self._item("C")
		self._submit_se(purpose="Material Receipt", item=item, qty=565.17, rate=RATE, target=self.source)
		self._seed_negative_target(item)
		se = self._submit_se(
			purpose="Material Transfer", item=item, qty=565.17, source=self.source, target=self.target
		)
		self.assertEqual(se.docstatus, 1)
		self.assertAlmostEqual(flt(self._sle_qty(item, self.target).qty_after_transaction), 0, places=2)

	def test_d_incoming_into_zero(self):
		item = self._item("D")
		self._submit_se(purpose="Material Receipt", item=item, qty=INCOMING, rate=RATE, target=self.target)
		self.assertEqual(flt(self._sle_qty(item, self.target).qty_after_transaction), INCOMING)

	def test_e_incoming_into_positive(self):
		item = self._item("E")
		self._submit_se(purpose="Material Receipt", item=item, qty=100, rate=RATE, target=self.target)
		self._submit_se(purpose="Material Receipt", item=item, qty=INCOMING, rate=RATE, target=self.target)
		self.assertEqual(flt(self._sle_qty(item, self.target).qty_after_transaction), 400)

	def test_f_outgoing_creates_negative_blocks(self):
		item = self._item("F")
		self._submit_se(purpose="Material Receipt", item=item, qty=100, rate=RATE, target=self.source)
		with self.assertRaises(NegativeStockError) as ctx:
			self._submit_se(purpose="Material Issue", item=item, qty=300, source=self.source)
		self.assertIn(self.source, str(ctx.exception))

	def test_g_outgoing_from_zero_blocks(self):
		item = self._item("G")
		self._submit_se(purpose="Material Receipt", item=item, qty=1, rate=RATE, target=self.source)
		self._submit_se(purpose="Material Issue", item=item, qty=1, source=self.source)
		self.assertAlmostEqual(flt(self._sle_qty(item, self.source).qty_after_transaction), 0, places=2)
		with self.assertRaises(NegativeStockError) as ctx:
			self._submit_se(purpose="Material Issue", item=item, qty=100, source=self.source)
		self.assertIn(self.source, str(ctx.exception))

	def test_h_worsening_existing_negative_blocks(self):
		item = self._item("H")
		self._seed_negative_target(item)
		with self.assertRaises(NegativeStockError) as ctx:
			self._submit_se(purpose="Material Issue", item=item, qty=100, source=self.target)
		self.assertIn(self.target, str(ctx.exception))

	def test_i_source_shortage_names_source(self):
		item = self._item("I")
		self._submit_se(purpose="Material Receipt", item=item, qty=34.83, rate=RATE, target=self.source)
		self._submit_se(purpose="Material Receipt", item=item, qty=300, rate=RATE, target=self.target)
		with self.assertRaises(NegativeStockError) as ctx:
			self._submit_se(
				purpose="Material Transfer", item=item, qty=300, source=self.source, target=self.target
			)
		msg = str(ctx.exception)
		self.assertIn("265.17", msg)
		self.assertIn(self.source, msg)
		self.assertNotIn(self.target, msg)

	def test_j_batch_sabb_healing_transfer(self):
		item = self._item("J", batch=True)
		batch = make_valid_batch(item)
		self._submit_se(
			purpose="Material Receipt",
			item=item,
			qty=INCOMING,
			rate=RATE,
			target=self.source,
			batch_no=batch,
		)
		self._seed_negative_target(item, batch_no=batch)
		se = self._submit_se(
			purpose="Material Transfer",
			item=item,
			qty=INCOMING,
			source=self.source,
			target=self.target,
			batch_no=batch,
		)
		self.assertEqual(se.docstatus, 1)
		sles = frappe.get_all(
			"Stock Ledger Entry",
			filters={"voucher_no": se.name, "is_cancelled": 0},
			fields=[
				"warehouse",
				"actual_qty",
				"qty_after_transaction",
				"valuation_rate",
				"stock_value",
				"stock_value_difference",
				"serial_and_batch_bundle",
			],
		)
		by_wh = {s.warehouse: s for s in sles}
		src = by_wh[self.source]
		tgt = by_wh[self.target]
		self.assertEqual(flt(src.actual_qty), -INCOMING)
		self.assertAlmostEqual(flt(src.qty_after_transaction), 0, places=2)
		self.assertEqual(flt(tgt.actual_qty), INCOMING)
		self.assertAlmostEqual(flt(tgt.qty_after_transaction), HEALED, places=2)
		src_b = frappe.db.get_value(
			"Serial and Batch Bundle",
			src.serial_and_batch_bundle,
			["warehouse", "type_of_transaction", "total_qty"],
			as_dict=True,
		)
		tgt_b = frappe.db.get_value(
			"Serial and Batch Bundle",
			tgt.serial_and_batch_bundle,
			["warehouse", "type_of_transaction", "total_qty"],
			as_dict=True,
		)
		self.assertEqual(src_b.type_of_transaction, "Outward")
		self.assertEqual(flt(src_b.total_qty), -INCOMING)
		self.assertEqual(tgt_b.type_of_transaction, "Inward")
		self.assertEqual(flt(tgt_b.total_qty), INCOMING)
		self.assertNotEqual(src.serial_and_batch_bundle, tgt.serial_and_batch_bundle)
		riv = run_riv(self.company, "Stock Entry", se.name)
		self.assertIsNotNone(riv)
		riv.reload()
		self.assertNotIn(riv.status, ("Failed", "Skipped"))
		self.assertAlmostEqual(flt(self._sle_qty(item, self.target).qty_after_transaction), HEALED, places=2)
		print(
			"TEST_J_SLES",
			{
				"source": {
					"actual_qty": flt(src.actual_qty),
					"qty_after": flt(src.qty_after_transaction),
					"valuation_rate": flt(src.valuation_rate),
					"stock_value": flt(src.stock_value),
					"stock_value_difference": flt(src.stock_value_difference),
					"sabb": src.serial_and_batch_bundle,
					"sabb_type": src_b.type_of_transaction,
					"sabb_qty": flt(src_b.total_qty),
				},
				"target": {
					"actual_qty": flt(tgt.actual_qty),
					"qty_after": flt(tgt.qty_after_transaction),
					"valuation_rate": flt(tgt.valuation_rate),
					"stock_value": flt(tgt.stock_value),
					"stock_value_difference": flt(tgt.stock_value_difference),
					"sabb": tgt.serial_and_batch_bundle,
					"sabb_type": tgt_b.type_of_transaction,
					"sabb_qty": flt(tgt_b.total_qty),
				},
			},
		)

	def test_k_batch_source_shortage_blocks(self):
		item = self._item("K", batch=True)
		batch = make_valid_batch(item)
		self._submit_se(
			purpose="Material Receipt", item=item, qty=100, rate=RATE, target=self.source, batch_no=batch
		)
		with self.assertRaises(BatchNegativeStockError) as ctx:
			self._submit_se(
				purpose="Material Transfer",
				item=item,
				qty=300,
				source=self.source,
				target=self.target,
				batch_no=batch,
			)
		self.assertIn(self.source, str(ctx.exception))

	def test_l_i4_genuine_corruption_still_blocks(self):
		sle = frappe._dict(
			actual_qty=-300,
			qty_after_transaction=0,
			stock_value=100_000,
			stock_value_difference=-4_554_000_000,
			company=self.company,
			voucher_type="Stock Entry",
			valuation_method="Moving Average",
		)
		mark_sle_running_balance_processed(sle)
		with self.assertRaises(ValuationIntegrityError) as ctx:
			assert_zero_qty_stock_value(sle)
		self.assertIn("Stock valuation integrity (I4)", str(ctx.exception))

	def test_m_v522_incoming_transient_is_not_i4(self):
		sle = frappe._dict(
			actual_qty=300,
			qty_after_transaction=0,
			incoming_rate=15_180_000,
			stock_value_difference=4_554_000_000,
			stock_value=4_554_000_000,
			company=self.company,
			voucher_type="Stock Entry",
			valuation_method="Moving Average",
		)
		assert_zero_qty_stock_value(sle)
		mark_sle_running_balance_processed(sle)
		assert_zero_qty_stock_value(sle)
