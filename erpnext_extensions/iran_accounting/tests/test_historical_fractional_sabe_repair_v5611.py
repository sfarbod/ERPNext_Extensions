# Copyright (c) 2026, ERPNext Extensions contributors
"""Auditable fractional IRR SABE repair + RIV short-pool hardening (v5.6.11)."""

from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair import (
	assert_bundle_integer_irr,
	integerize_bundle_sabe,
	integerize_sabe_row,
	log_riv_short_pool_event,
	repair_batch_pool_fractional_irr,
)
from erpnext_extensions.iran_accounting.domain.physical_two_slice import (
	AllocationError,
	allocate_physical_two_slice,
)


class TestFractionalSabeRepairUnitV5611(unittest.TestCase):
	def test_rhu_rate_matches_sed_integer_for_mt_float_ma(self):
		"""MAT-STE-2026-33527 style: Core float rate RHU → SED integer 250654."""
		rate = Decimal("250653.505702364")
		qty = Decimal("32355")
		from erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair import (
			_rhu,
		)

		self.assertEqual(int(_rhu(rate)), 250654)
		self.assertEqual(_rhu(qty * Decimal(250654)), Decimal("8109910170"))

	def test_allocate_refuses_fractional_without_repair(self):
		with self.assertRaises(AllocationError):
			allocate_physical_two_slice(10, Decimal("3904078944.0016685"), 1)

	def test_allocate_ok_after_rhu_pool(self):
		plan = allocate_physical_two_slice(10, Decimal("3904078944"), 1)
		self.assertEqual(plan.out_value_abs, Decimal("390407894"))
		self.assertTrue(all(s.rate == int(s.rate) for s in plan.slices))

	def test_integerize_row_noop_when_already_integer(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.frappe"
		) as f:
			f.db.get_value.return_value = SimpleNamespace(
				name="x",
				qty=10,
				incoming_rate=23209,
				stock_value_difference=232090,
				item_code="13100012",
				batch_no="b",
				warehouse="w",
				voucher_type="Stock Entry",
				voucher_no="SE",
				company="C",
			)
			with mock.patch(
				"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.is_irr_company",
				return_value=True,
			):
				out = integerize_sabe_row("x", company="C")
			self.assertIsNone(out)
			f.db.set_value.assert_not_called()

	def test_integerize_row_repairs_float_ma(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.frappe"
		) as f:
			f.db.get_value.return_value = SimpleNamespace(
				name="x",
				qty=32355.0,
				incoming_rate=250653.505702364,
				stock_value_difference=8109894176.999987,
				item_code="20100006",
				batch_no="7060-20100006-MR265001D01",
				warehouse="wh",
				voucher_type="Stock Entry",
				voucher_no="MAT-STE-2026-33527",
				company="C",
			)
			with mock.patch(
				"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.is_irr_company",
				return_value=True,
			):
				out = integerize_sabe_row("x", company="C")
			self.assertIsNotNone(out)
			self.assertEqual(out["new_rate"], 250654)
			self.assertEqual(out["new_svd"], "8109910170")
			f.db.set_value.assert_called_once()

	def test_integerize_idempotent_second_pass(self):
		"""Running repair twice must not mutate already-integer rows."""
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.frappe"
		) as f:
			f.db.get_value.return_value = SimpleNamespace(
				name="x",
				qty=32355.0,
				incoming_rate=250654,
				stock_value_difference=8109910170,
				item_code="20100006",
				batch_no="7060-20100006-MR265001D01",
				warehouse="wh",
				voucher_type="Stock Entry",
				voucher_no="MAT-STE-2026-33527",
			)
			with mock.patch(
				"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.is_irr_company",
				return_value=True,
			):
				self.assertIsNone(integerize_sabe_row("x", company="C"))
				self.assertIsNone(integerize_sabe_row("x", company="C"))
			f.db.set_value.assert_not_called()

	def test_assert_bundle_integer_irr_fail_closed(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.frappe"
		) as f:
			f.get_all.return_value = [
				SimpleNamespace(
					name="e1",
					incoming_rate=250653.505702364,
					stock_value_difference=8109894176.999987,
					qty=32355,
				)
			]
			f.throw.side_effect = Exception("fail-closed")
			with self.assertRaises(Exception):
				assert_bundle_integer_irr("BUNDLE-1")
			f.throw.assert_called_once()

	def test_integerize_bundle_asserts_after_mutation(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.frappe"
		) as f, mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.integerize_sabe_row",
			return_value={
				"name": "e1",
				"new_rate": 250654,
				"new_svd": "8109910170",
			},
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.assert_bundle_integer_irr"
		) as assert_fn, mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.flt",
			side_effect=lambda x: float(x or 0),
		):
			f.get_all.return_value = ["e1"]
			f.db.sql.side_effect = [((32355.0,),), ((8109910170.0,),)]
			out = integerize_bundle_sabe("BUNDLE-1", company="C")
			self.assertEqual(out["repaired"], 1)
			assert_fn.assert_called_once_with("BUNDLE-1")

	def test_repair_batch_pool_rolls_back_savepoint_on_failure(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.frappe"
		) as f, mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.integerize_sabe_row",
			side_effect=RuntimeError("mid-repair boom"),
		):
			f.generate_hash.return_value = "abcd1234"
			f.db.sql.return_value = [SimpleNamespace(name="e1", parent="B1")]
			with self.assertRaises(RuntimeError):
				repair_batch_pool_fractional_irr("I", "B", "W", company="C")
			f.db.savepoint.assert_called_once()
			f.db.rollback.assert_called_once_with(save_point="iran_sabe_abcd1234")
			f.db.release_savepoint.assert_not_called()

	def test_repair_batch_pool_audit_only_after_savepoint_release(self):
		with tempfile.TemporaryDirectory() as tmp, mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.frappe"
		) as f, mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair._audit_dir",
			return_value=tmp,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.integerize_sabe_row",
			return_value={
				"name": "e1",
				"old_rate": "250653.505702364",
				"new_rate": 250654,
				"old_svd": "8109894176.999987",
				"new_svd": "8109910170",
				"batch_no": "7060-20100006-MR265001D01",
				"voucher_no": "MAT-STE-2026-33527",
			},
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.integerize_bundle_sabe",
			return_value={"repaired": 1},
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.now_datetime"
		) as nd:
			nd.return_value.strftime.return_value = "20261024_210000"
			nd.return_value.__str__ = lambda self: "2026-10-24 21:00:00"
			f.generate_hash.return_value = "abcd1234"
			f.local.site = "development.localhost"
			f.flags.get.return_value = None
			f.db.sql.return_value = [
				SimpleNamespace(name="e1", parent="B1"),
			]
			payload = repair_batch_pool_fractional_irr(
				"20100006",
				"7060-20100006-MR265001D01",
				"wh",
				company="C",
				reason="unit_test",
			)
			f.db.release_savepoint.assert_called_once_with("iran_sabe_abcd1234")
			self.assertEqual(payload["repaired"], 1)
			self.assertIn("audit_path", payload)
			with open(payload["audit_path"], encoding="utf-8") as fh:
				audit = json.load(fh)
			self.assertEqual(audit["reason"], "unit_test")
			self.assertEqual(audit["rows"][0]["new_rate"], 250654)
			self.assertEqual(audit["rows"][0]["voucher_no"], "MAT-STE-2026-33527")
			self.assertTrue(audit["committed_by_caller"])

	def test_log_riv_short_pool_event_writes_policy_audit(self):
		with tempfile.TemporaryDirectory() as tmp, mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.frappe"
		) as f, mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair._audit_dir",
			return_value=tmp,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.now_datetime"
		) as nd:
			nd.return_value.strftime.return_value = "20261024_210100"
			nd.return_value.__str__ = lambda self: "2026-10-24 21:01:00"
			f.local.site = "development.localhost"
			f.flags.get.side_effect = lambda k: (
				True if k == "through_repost_item_valuation" else None
			)
			path = log_riv_short_pool_event(
				item_code="13100012",
				batch_no="7060-20100006-MR265001D01",
				warehouse="wh",
				pool_qty=Decimal("1"),
				out_qty=Decimal("2"),
				voucher_type="Delivery Note",
				voucher_no="MAT-DN-2026-00073",
				bundle="SABB-1",
			)
			with open(path, encoding="utf-8") as fh:
				audit = json.load(fh)
			self.assertEqual(audit["event"], "riv_short_pool_fallthrough")
			self.assertTrue(audit["through_repost_item_valuation"])
			self.assertIn("integerize_bundle_sabe", audit["policy"])
			self.assertIn("fail-closed", audit["policy"])


class TestRivShortPoolBundlePolicyV5611(unittest.TestCase):
	def test_live_submit_raises_on_short_pool(self):
		from erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle import (
			rewrite_outgoing_bundle_after_core,
		)

		sle = {
			"voucher_type": "Delivery Note",
			"voucher_no": "DN-1",
			"actual_qty": -2,
			"serial_and_batch_bundle": "B1",
			"warehouse": "wh",
			"item_code": "I1",
			"voucher_detail_no": "d1",
			"company": "C",
		}
		self_obj = SimpleNamespace(company="C")
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.frappe"
		) as f, mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.read_batch_pool",
			return_value=(Decimal("1"), Decimal("100")),
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.flt",
			side_effect=lambda x: float(x or 0),
		):
			f.flags.get.return_value = False
			f.get_all.return_value = [
				SimpleNamespace(
					name="e1",
					batch_no="BATCH",
					qty=-2,
					warehouse="wh",
					item_code="I1",
				)
			]
			with self.assertRaises(AllocationError) as ctx:
				rewrite_outgoing_bundle_after_core(self_obj, sle, None)
			self.assertIn("empty/short pool", str(ctx.exception))

	def test_riv_short_pool_audits_and_returns_none(self):
		from erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle import (
			rewrite_outgoing_bundle_after_core,
		)

		sle = {
			"voucher_type": "Delivery Note",
			"voucher_no": "DN-1",
			"actual_qty": -2,
			"serial_and_batch_bundle": "B1",
			"warehouse": "wh",
			"item_code": "I1",
			"voucher_detail_no": "d1",
			"company": "C",
		}
		self_obj = SimpleNamespace(company="C")
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.frappe"
		) as f, mock.patch(
			"erpnext_extensions.iran_accounting.domain.currency.is_irr_company",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.read_batch_pool",
			return_value=(Decimal("1"), Decimal("100")),
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.physical_two_slice_bundle.flt",
			side_effect=lambda x: float(x or 0),
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.historical_fractional_sabe_repair.log_riv_short_pool_event"
		) as log_fn:
			f.flags.get.return_value = True  # through_repost_item_valuation
			f.get_all.return_value = [
				SimpleNamespace(
					name="e1",
					batch_no="BATCH",
					qty=-2,
					warehouse="wh",
					item_code="I1",
				)
			]
			out = rewrite_outgoing_bundle_after_core(self_obj, sle, None)
			self.assertIsNone(out)
			log_fn.assert_called_once()
			kwargs = log_fn.call_args.kwargs
			self.assertEqual(kwargs["item_code"], "I1")
			self.assertEqual(kwargs["batch_no"], "BATCH")
			self.assertEqual(kwargs["pool_qty"], Decimal("1"))
			self.assertEqual(kwargs["out_qty"], Decimal("2"))


if __name__ == "__main__":
	unittest.main()
