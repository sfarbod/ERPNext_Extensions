# Copyright (c) 2026, ERPNext Extensions contributors
"""RIV valuation-scope integrity gating (unit tests, no site DB required)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
	ValuationIntegrityError,
	assert_incoming_rate_not_negative,
	assert_sle_valuation_integrity_after_sync,
	assert_sle_valuation_integrity_before_vanilla,
)
from erpnext_extensions.iran_accounting.domain.riv_valuation_scope import (
	clear_out_of_scope_anomaly_buffers,
	get_out_of_scope_anomalies,
	get_riv_target_item_codes,
	is_sle_in_riv_blocking_scope,
	is_stock_entry_in_riv_blocking_scope,
	log_out_of_scope_integrity_anomaly,
	run_integrity_assert_in_riv_scope,
)


def _engine(*, item_code=None, warehouse=None, items_to_be_repost=None, args_item=None, args_wh=None):
	repost = SimpleNamespace(
		name="RIV-TEST",
		item_code=item_code,
		warehouse=warehouse,
		items_to_be_repost=items_to_be_repost,
	)
	args = frappe._dict(
		{
			"item_code": args_item or item_code,
			"warehouse": args_wh or warehouse,
			"items_to_be_repost": None,
		}
	)
	return SimpleNamespace(repost_doc=repost, args=args, company="اسپاد فارمد دارو")


def _sle(*, item_code, warehouse="WH-A", incoming_rate=0, actual_qty=1, name="SLE-1", **extra):
	data = frappe._dict(
		{
			"name": name,
			"item_code": item_code,
			"warehouse": warehouse,
			"incoming_rate": incoming_rate,
			"actual_qty": actual_qty,
			"valuation_rate": incoming_rate,
			"stock_value_difference": flt(incoming_rate) * flt(actual_qty),
			"qty_after_transaction": actual_qty,
			"stock_value": flt(incoming_rate) * flt(actual_qty),
			"voucher_type": "Stock Entry",
			"voucher_no": "STE-1",
			"voucher_detail_no": "row-1",
			"company": "اسپاد فارمد دارو",
			"batch_no": extra.pop("batch_no", None),
		}
	)
	data.update(extra)
	return data


class TestRIVValuationScopeHelpers(unittest.TestCase):
	def test_target_items_from_riv_item_code(self):
		engine = _engine(item_code="13100134", warehouse="WH-Q")
		self.assertEqual(get_riv_target_item_codes(engine), {"13100134"})

	def test_target_items_from_items_to_be_repost_json(self):
		engine = _engine(
			item_code=None,
			items_to_be_repost='[{"item_code":"A","warehouse":"W1"},{"item_code":"B","warehouse":"W2"}]',
		)
		self.assertEqual(get_riv_target_item_codes(engine), {"A", "B"})

	def test_no_engine_is_fail_closed(self):
		self.assertIsNone(get_riv_target_item_codes(None))

	def test_same_item_any_warehouse_is_blocking(self):
		engine = _engine(item_code="13100134", warehouse="WH-Q")
		sle = _sle(item_code="13100134", warehouse="WH-OTHER")
		self.assertTrue(is_sle_in_riv_blocking_scope(engine, sle))

	def test_different_item_is_not_blocking(self):
		engine = _engine(item_code="13100134", warehouse="WH-Q")
		sle = _sle(item_code="20100064", warehouse="WH-FG")
		self.assertFalse(is_sle_in_riv_blocking_scope(engine, sle))

	def test_dependant_args_item_code_does_not_expand_targets(self):
		"""ERPNext mutates args.item_code while walking dependants — must not widen scope."""
		engine = _engine(item_code="13100134", warehouse="WH-Q")
		engine.args.item_code = "20100064"
		engine.args.items_to_be_repost = [
			{"item_code": "13100134", "warehouse": "WH-Q"},
			{"item_code": "20100064", "warehouse": "WH-FG"},
			{"item_code": "30100033", "warehouse": "WH-FG"},
		]
		self.assertEqual(get_riv_target_item_codes(engine), {"13100134"})
		self.assertFalse(
			is_sle_in_riv_blocking_scope(engine, _sle(item_code="20100064", warehouse="WH-FG"))
		)

	def test_stock_entry_scope_requires_target_item_on_voucher(self):
		engine = _engine(item_code="13100134")
		doc_unrelated = SimpleNamespace(
			doctype="Stock Entry",
			name="STE-X",
			items=[SimpleNamespace(item_code="20100064"), SimpleNamespace(item_code="13200036")],
		)
		doc_related = SimpleNamespace(
			doctype="Stock Entry",
			name="STE-Y",
			items=[SimpleNamespace(item_code="20100064"), SimpleNamespace(item_code="13100134")],
		)
		self.assertFalse(is_stock_entry_in_riv_blocking_scope(engine, doc_unrelated))
		self.assertTrue(is_stock_entry_in_riv_blocking_scope(engine, doc_related))


class TestRIVScopeAwareIntegrityAsserts(unittest.TestCase):
	def setUp(self):
		clear_out_of_scope_anomaly_buffers()
		self._irr_patch = patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.is_irr_company",
			return_value=True,
		)
		self._irr_patch.start()

	def tearDown(self):
		self._irr_patch.stop()
		clear_out_of_scope_anomaly_buffers()

	def test_i1_on_target_item_blocks(self):
		engine = _engine(item_code="13100134", warehouse="WH-Q")
		sle = _sle(item_code="13100134", warehouse="WH-Q", incoming_rate=-10, actual_qty=5)
		with self.assertRaises(ValuationIntegrityError):
			assert_sle_valuation_integrity_before_vanilla(engine, sle)

	def test_i1_on_unrelated_item_does_not_block(self):
		engine = _engine(item_code="13100134", warehouse="WH-Q")
		sle = _sle(
			item_code="20100064",
			warehouse="WH-FG",
			incoming_rate=-8220316.05,
			actual_qty=1256,
			voucher_no="MAT-STE-2026-24872-2",
		)
		with patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_guard.frappe.db.get_value",
			return_value=None,
		), patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_scope.frappe.log_error",
			create=True,
		):
			assert_sle_valuation_integrity_before_vanilla(engine, sle)
		anomalies = get_out_of_scope_anomalies()
		self.assertTrue(anomalies)
		self.assertEqual(anomalies[-1]["offending_item_code"], "20100064")
		self.assertEqual(anomalies[-1]["target_items"], ["13100134"])
		self.assertIn("outside declared RIV target", anomalies[-1]["reason"])

	def test_i1_after_sync_same_rules(self):
		engine = _engine(item_code="13100134", warehouse="WH-Q")
		bad_target = _sle(item_code="13100134", incoming_rate=-1, actual_qty=1)
		with self.assertRaises(ValuationIntegrityError):
			assert_sle_valuation_integrity_after_sync(bad_target, engine=engine)
		with patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_scope.frappe.log_error",
			create=True,
		):
			bad_other = _sle(item_code="20100064", incoming_rate=-1, actual_qty=1)
			assert_sle_valuation_integrity_after_sync(bad_other, engine=engine)

	def test_non_riv_context_remains_fail_closed(self):
		sle = _sle(item_code="20100064", incoming_rate=-1, actual_qty=1)
		with self.assertRaises(ValuationIntegrityError):
			assert_incoming_rate_not_negative(sle)

	def test_run_integrity_assert_does_not_swallow_non_valuation_errors(self):
		engine = _engine(item_code="13100134")

		def boom():
			raise RuntimeError("not integrity")

		with self.assertRaises(RuntimeError):
			run_integrity_assert_in_riv_scope(engine, _sle(item_code="20100064"), boom)

	def test_multi_item_stock_entry_membership_is_not_automatic_dependency(self):
		engine = _engine(item_code="13100134")
		self.assertFalse(
			is_stock_entry_in_riv_blocking_scope(
				engine,
				SimpleNamespace(
					items=[
						SimpleNamespace(item_code="20100064"),
						SimpleNamespace(item_code="13200036"),
					]
				),
			)
		)
		self.assertTrue(
			is_stock_entry_in_riv_blocking_scope(
				engine,
				SimpleNamespace(
					items=[
						SimpleNamespace(item_code="13100134"),
						SimpleNamespace(item_code="30100033"),
					]
				),
			)
		)

	def test_se_level_assert_scopes_to_offending_item_not_voucher_membership(self):
		"""Manufacture that consumes the RIV target but poisons an unrelated FG.

		SE-level I2 must not abort the target RIV merely because the voucher also
		lists the target item — only the offending row's item_code is blocking.
		"""
		from erpnext_extensions.iran_accounting.domain.riv_valuation_guard import (
			ValuationIntegrityError,
		)

		engine = _engine(item_code="13100023")
		doc = SimpleNamespace(
			name="MAT-STE-2026-25740",
			items=[
				SimpleNamespace(item_code="13100023"),
				SimpleNamespace(item_code="30100053"),
			],
		)

		def poison_unrelated_fg():
			raise ValuationIntegrityError(
				"Stock valuation integrity (I2).\nitem_code=30100053\namount=-1"
			)

		with patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_scope.frappe.log_error",
			create=True,
		):
			run_integrity_assert_in_riv_scope(engine, None, poison_unrelated_fg, doc=doc)
		anomalies = get_out_of_scope_anomalies()
		self.assertTrue(anomalies)
		self.assertIn("30100053", anomalies[-1].get("exception") or "")

		def poison_target_row():
			raise ValuationIntegrityError(
				"Stock valuation integrity (I2).\nitem_code=13100023\namount=-1"
			)

		with self.assertRaises(ValuationIntegrityError):
			run_integrity_assert_in_riv_scope(engine, None, poison_target_row, doc=doc)

	def test_batch_identity_is_logged_but_scope_is_item(self):
		engine = _engine(item_code="13100134", warehouse="WH-Q")
		sle = _sle(
			item_code="20100064",
			warehouse="WH-FG",
			incoming_rate=-5,
			actual_qty=1,
			batch_no="BATCH-POISON",
		)
		with patch(
			"erpnext_extensions.iran_accounting.domain.riv_valuation_scope.frappe.log_error",
			create=True,
		):
			log_out_of_scope_integrity_anomaly(
				engine,
				sle=sle,
				invariant="I1",
				detail="incoming_rate is negative on an incoming movement",
			)
		payload = get_out_of_scope_anomalies()[-1]
		self.assertEqual(payload["offending_batch_no"], "BATCH-POISON")
		self.assertFalse(is_sle_in_riv_blocking_scope(engine, sle))
		same_item_other_batch = _sle(
			item_code="13100134", warehouse="WH-Q", incoming_rate=-5, actual_qty=1, batch_no="OTHER"
		)
		self.assertTrue(is_sle_in_riv_blocking_scope(engine, same_item_other_batch))


class TestPurchaseInvoiceAdjustmentExpectationMath(unittest.TestCase):
	"""Pure arithmetic contract for the known PI→PR adjustment case."""

	def test_expected_amount_difference_and_srbnb_clear(self):
		pr_rate_times_conv = 0.09 * 1_500_000  # 135_000
		pi_base_rate = 254_811
		qty = 54_000
		amount_difference = (pi_base_rate - pr_rate_times_conv) * qty
		self.assertEqual(amount_difference, 6_469_794_000)
		pr_inventory_after = (0.09 * 1_500_000) * qty + amount_difference
		self.assertEqual(pr_inventory_after, 13_759_794_000)
		pi_srbnb = 13_759_794_000
		self.assertEqual(pi_srbnb - pr_inventory_after, 0)


if __name__ == "__main__":
	unittest.main()
