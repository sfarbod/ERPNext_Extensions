# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.7.1 — Reject-only Manufacture SED → SLE rate during RIV.

Core ``get_produced_row_rate`` keeps historical SLE incoming_rate=0 when no
``is_finished_item`` absorber exists. After Iran SED repair, RIV must select the
corrected SED rate, keep SABE from clobbering it, and leave Main+Reject /
component / bulk / ordinary Manufacture unchanged.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from frappe.utils import flt

from erpnext_extensions.iran_accounting.domain.reject_only_riv_rate import (
	clear_reject_only_riv_cache,
	get_iran_reject_only_produced_rate,
	reject_only_sed_sle_lag_identities,
	seed_sabe_rows_from_sle,
	should_seed_sabe_from_document_sle,
)
from erpnext_extensions.iran_accounting.domain.sabe_integer_valuation import (
	apply_integer_sabe_policy_to_sle,
)
from erpnext_extensions.iran_accounting.scrap_costing import (
	CLASS_BULK_SCRAP,
	CLASS_COMPONENT_SCRAP,
	CLASS_MAIN_FG,
	CLASS_MAIN_PRODUCT_REJECT,
)


def _row(**kwargs):
	base = {
		"name": "r",
		"idx": 1,
		"item_code": "30100015",
		"qty": 200,
		"transfer_qty": 200,
		"basic_rate": 357596,
		"basic_amount": 71519200,
		"valuation_rate": 357596,
		"amount": 71519200,
		"additional_cost": 0,
		"landed_cost_voucher_amount": 0,
		"allow_zero_valuation_rate": 0,
		"is_finished_item": 0,
		"is_scrap_item": 0,
		"secondary_item_type": "Scrap",
		"s_warehouse": None,
		"t_warehouse": "Reject",
		"custom_output_class": None,
	}
	base.update(kwargs)
	ns = SimpleNamespace(**base)
	ns.get = lambda k, d=None: ns.__dict__.get(k, d)
	return ns


def _reject_only_doc(*, rej_rate=357596.0, rej_qty=200.0):
	consume = _row(
		name="c1",
		idx=1,
		item_code="RAW",
		qty=200,
		basic_rate=357596,
		basic_amount=71519200,
		amount=71519200,
		s_warehouse="WIP",
		t_warehouse=None,
		secondary_item_type=None,
	)
	rej = _row(
		name="rej1",
		idx=2,
		qty=rej_qty,
		basic_rate=rej_rate,
		basic_amount=rej_rate * rej_qty,
		valuation_rate=rej_rate,
		amount=rej_rate * rej_qty,
	)
	return SimpleNamespace(
		name="MAT-STE-TEST-RO",
		doctype="Stock Entry",
		purpose="Manufacture",
		company="E",
		docstatus=1,
		items=[consume, rej],
		get=lambda k, d=None: {"name": "MAT-STE-TEST-RO", "purpose": "Manufacture", "company": "E", "docstatus": 1}.get(
			k, d
		),
	)


class TestRejectOnlyProducedRate(unittest.TestCase):
	def tearDown(self):
		clear_reject_only_riv_cache()

	def test_selects_sed_rate_when_historical_sle_zero(self):
		doc = _reject_only_doc()
		sle = SimpleNamespace(
			voucher_type="Stock Entry",
			voucher_no=doc.name,
			voucher_detail_no="rej1",
			actual_qty=200.0,
			incoming_rate=0.0,
			company="E",
			get=lambda k, d=None: getattr(sle, k, d),
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.frappe"
		) as frappe_mock:
			frappe_mock.local = mock.Mock()
			# no cache attr → empty
			del frappe_mock.local._iran_reject_only_riv_cache
			frappe_mock.local = SimpleNamespace()
			frappe_mock.db.get_value.side_effect = lambda *a, **k: (
				SimpleNamespace(purpose="Manufacture", company="E", docstatus=1)
				if a[0] == "Stock Entry"
				else None
			)
			frappe_mock.get_doc.return_value = doc
			with mock.patch(
				"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.is_irr_company",
				return_value=True,
			), mock.patch(
				"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.classify_manufacture_outputs",
				return_value={
					CLASS_MAIN_FG: [],
					CLASS_MAIN_PRODUCT_REJECT: [doc.items[1]],
					CLASS_COMPONENT_SCRAP: [],
					CLASS_BULK_SCRAP: [],
					"CO_PRODUCT": [],
					"CO_PRODUCT_REJECT": [],
					"OTHER_OUTPUT": [],
				},
			), mock.patch(
				"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate._has_reject_only",
				return_value=True,
			):
				rate = get_iran_reject_only_produced_rate(sle)
		self.assertEqual(rate, 357596.0)

	def test_main_plus_reject_returns_none(self):
		"""CASE F / Main+Reject: Core absorber path must remain unchanged."""
		fg = _row(name="fg1", is_finished_item=1, t_warehouse="FG", secondary_item_type=None)
		rej = _row(name="rej1")
		doc = SimpleNamespace(
			name="MAT-STE-TEST-F",
			doctype="Stock Entry",
			purpose="Manufacture",
			company="E",
			docstatus=1,
			items=[fg, rej],
		)
		sle = SimpleNamespace(
			voucher_type="Stock Entry",
			voucher_no=doc.name,
			voucher_detail_no="rej1",
			actual_qty=200.0,
			incoming_rate=0.0,
			get=lambda k, d=None: getattr(sle, k, d),
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.frappe"
		) as frappe_mock:
			frappe_mock.local = SimpleNamespace()
			frappe_mock.db.get_value.return_value = SimpleNamespace(
				purpose="Manufacture", company="E", docstatus=1
			)
			frappe_mock.get_doc.return_value = doc
			with mock.patch(
				"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.is_irr_company",
				return_value=True,
			), mock.patch(
				"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate._has_reject_only",
				return_value=False,
			):
				self.assertIsNone(get_iran_reject_only_produced_rate(sle))

	def test_intentional_zero_allow_flag_returns_none(self):
		doc = _reject_only_doc(rej_rate=0.0)
		doc.items[1].allow_zero_valuation_rate = 1
		doc.items[1].amount = 0
		doc.items[1].valuation_rate = 0
		sle = SimpleNamespace(
			voucher_type="Stock Entry",
			voucher_no=doc.name,
			voucher_detail_no="rej1",
			actual_qty=200.0,
			incoming_rate=0.0,
			get=lambda k, d=None: getattr(sle, k, d),
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.frappe"
		) as frappe_mock:
			frappe_mock.local = SimpleNamespace()
			frappe_mock.db.get_value.return_value = SimpleNamespace(
				purpose="Manufacture", company="E", docstatus=1
			)
			frappe_mock.get_doc.return_value = doc
			with mock.patch(
				"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.is_irr_company",
				return_value=True,
			), mock.patch(
				"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate._has_reject_only",
				return_value=True,
			), mock.patch(
				"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.classify_manufacture_outputs",
				return_value={
					CLASS_MAIN_FG: [],
					CLASS_MAIN_PRODUCT_REJECT: [doc.items[1]],
					CLASS_COMPONENT_SCRAP: [],
					CLASS_BULK_SCRAP: [],
					"CO_PRODUCT": [],
					"CO_PRODUCT_REJECT": [],
					"OTHER_OUTPUT": [],
				},
			):
				self.assertIsNone(get_iran_reject_only_produced_rate(sle))

	def test_ordinary_non_manufacture_returns_none(self):
		sle = SimpleNamespace(
			voucher_type="Stock Entry",
			voucher_no="MAT-STE-X",
			voucher_detail_no="d1",
			actual_qty=10.0,
			get=lambda k, d=None: getattr(sle, k, d),
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.frappe"
		) as frappe_mock:
			frappe_mock.local = SimpleNamespace()
			frappe_mock.db.get_value.return_value = SimpleNamespace(
				purpose="Material Transfer", company="E", docstatus=1
			)
			with mock.patch(
				"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.is_irr_company",
				return_value=True,
			):
				self.assertIsNone(get_iran_reject_only_produced_rate(sle))


class TestSabeSeedFromDocumentSle(unittest.TestCase):
	def tearDown(self):
		clear_reject_only_riv_cache()

	def test_should_seed_when_sle_valued_sabe_zero(self):
		sle = SimpleNamespace(
			voucher_type="Stock Entry",
			voucher_no="MAT-STE-TEST-RO",
			voucher_detail_no="rej1",
			actual_qty=200.0,
			incoming_rate=357596.0,
			stock_value_difference=71519200.0,
			get=lambda k, d=None: getattr(sle, k, d),
		)
		rows = [
			{
				"name": "sabe1",
				"qty": 200.0,
				"incoming_rate": 0.0,
				"outgoing_rate": 0.0,
				"stock_value_difference": 0.0,
			}
		]
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate._load_reject_only_context",
			return_value={"reject_names": {"rej1"}, "company": "E", "reject_by_name": {}},
		):
			self.assertTrue(should_seed_sabe_from_document_sle(sle, rows))

	def test_seed_writes_sle_economics_onto_sabe_row(self):
		sle = SimpleNamespace(
			actual_qty=200.0,
			incoming_rate=357596.0,
			stock_value_difference=71519200.0,
			get=lambda k, d=None: getattr(sle, k, d),
		)
		rows = [
			{
				"name": "sabe1",
				"qty": 200.0,
				"incoming_rate": 0.0,
				"outgoing_rate": 0.0,
				"stock_value_difference": 0.0,
			}
		]
		seed_sabe_rows_from_sle(sle, rows)
		self.assertEqual(rows[0]["incoming_rate"], 357596.0)
		self.assertEqual(rows[0]["stock_value_difference"], 71519200.0)

	@mock.patch(
		"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.is_irr_company",
		return_value=True,
	)
	@mock.patch(
		"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.get_company_currency",
		return_value="IRR",
	)
	@mock.patch(
		"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_currency",
		side_effect=lambda v, c: round(float(v)),
	)
	@mock.patch(
		"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.round_monetary_rate",
		side_effect=lambda v, c: round(float(v)),
	)
	def test_apply_integer_sabe_does_not_clobber_sed_synced_sle(self, *_mocks):
		"""Integration-shaped: SED-synced SLE must survive zero SABE integer policy."""
		sle = SimpleNamespace(
			company="E",
			voucher_type="Stock Entry",
			voucher_no="MAT-STE-TEST-RO",
			voucher_detail_no="rej1",
			actual_qty=200.0,
			incoming_rate=357596.0,
			stock_value_difference=71519200.0,
			stock_value=71519200.0,
			qty_after_transaction=200.0,
			valuation_rate=357596.0,
			serial_and_batch_bundle="bundle1",
			get=lambda k, d=None: getattr(sle, k, d),
		)
		zero_sabe = [
			SimpleNamespace(
				name="sabe1",
				qty=200.0,
				incoming_rate=0.0,
				outgoing_rate=0.0,
				stock_value_difference=0.0,
				batch_no="B1",
				get=lambda k, d=None, _r=None: None,
			)
		]
		# Make row.get work
		for r in zero_sabe:
			r.get = lambda k, d=None, row=r: getattr(row, k, d)

		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.sabe_integer_valuation.frappe"
		) as frappe_mock, mock.patch(
			"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.should_seed_sabe_from_document_sle",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.seed_sabe_rows_from_sle",
			side_effect=seed_sabe_rows_from_sle,
		):
			frappe_mock.get_all.return_value = zero_sabe
			frappe_mock.db.set_value = mock.Mock()
			out = apply_integer_sabe_policy_to_sle(sle, align_sle=True)

		self.assertEqual(flt(sle.incoming_rate), 357596.0)
		self.assertEqual(flt(sle.stock_value_difference), 71519200.0)
		self.assertTrue(out.get("sle_adjusted"))

	def test_component_scrap_path_does_not_seed(self):
		sle = SimpleNamespace(
			voucher_type="Stock Entry",
			voucher_no="MAT-STE-COMP",
			voucher_detail_no="comp1",
			actual_qty=10.0,
			incoming_rate=1000.0,
			stock_value_difference=10000.0,
			get=lambda k, d=None: getattr(sle, k, d),
		)
		rows = [{"qty": 10, "incoming_rate": 0, "outgoing_rate": 0, "stock_value_difference": 0}]
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate._load_reject_only_context",
			return_value=None,
		):
			self.assertFalse(should_seed_sabe_from_document_sle(sle, rows))


class TestRejectOnlySedSleLag(unittest.TestCase):
	def tearDown(self):
		clear_reject_only_riv_cache()

	def test_lag_identities_when_sle_still_zero(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate._load_reject_only_context",
			return_value={
				"company": "E",
				"reject_names": {"rej1"},
				"reject_by_name": {
					"rej1": SimpleNamespace(
						name="rej1",
						item_code="30100015",
						t_warehouse="Reject",
						amount=71519200.0,
						valuation_rate=357596.0,
						allow_zero_valuation_rate=0,
						get=lambda k, d=None: {
							"name": "rej1",
							"amount": 71519200.0,
							"valuation_rate": 357596.0,
							"allow_zero_valuation_rate": 0,
						}.get(k, d),
					)
				},
			},
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.frappe"
		) as frappe_mock:
			frappe_mock.db.get_value.return_value = SimpleNamespace(
				name="sle1",
				item_code="30100015",
				warehouse="Reject",
				incoming_rate=0.0,
				stock_value_difference=0.0,
				posting_date="2026-01-01",
				posting_time="10:00:00",
			)
			lags = reject_only_sed_sle_lag_identities("MAT-STE-TEST-RO")
		self.assertEqual(len(lags), 1)
		self.assertEqual(lags[0]["item_code"], "30100015")
		self.assertEqual(lags[0]["sle_incoming_rate"], 0.0)

	def test_no_lag_when_sle_already_priced(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate._load_reject_only_context",
			return_value={
				"company": "E",
				"reject_names": {"rej1"},
				"reject_by_name": {
					"rej1": SimpleNamespace(
						name="rej1",
						amount=71519200.0,
						valuation_rate=357596.0,
						allow_zero_valuation_rate=0,
						get=lambda k, d=None: {
							"amount": 71519200.0,
							"valuation_rate": 357596.0,
							"allow_zero_valuation_rate": 0,
						}.get(k, d),
					)
				},
			},
		), mock.patch(
			"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.frappe"
		) as frappe_mock:
			frappe_mock.db.get_value.return_value = SimpleNamespace(
				name="sle1",
				item_code="30100015",
				warehouse="Reject",
				incoming_rate=357596.0,
				stock_value_difference=71519200.0,
				posting_date="2026-01-01",
				posting_time="10:00:00",
			)
			self.assertEqual(reject_only_sed_sle_lag_identities("MAT-STE-TEST-RO"), [])


class TestRepeatedRivIdempotentRate(unittest.TestCase):
	"""Repeated rate selection with already-corrected SLE must stay stable."""

	def tearDown(self):
		clear_reject_only_riv_cache()

	def test_repeated_selection_keeps_sed_rate(self):
		doc = _reject_only_doc()
		sle = SimpleNamespace(
			voucher_type="Stock Entry",
			voucher_no=doc.name,
			voucher_detail_no="rej1",
			actual_qty=200.0,
			incoming_rate=357596.0,
			get=lambda k, d=None: getattr(sle, k, d),
		)
		with mock.patch(
			"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.frappe"
		) as frappe_mock:
			frappe_mock.local = SimpleNamespace()
			frappe_mock.db.get_value.return_value = SimpleNamespace(
				purpose="Manufacture", company="E", docstatus=1
			)
			frappe_mock.get_doc.return_value = doc
			with mock.patch(
				"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.is_irr_company",
				return_value=True,
			), mock.patch(
				"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate._has_reject_only",
				return_value=True,
			), mock.patch(
				"erpnext_extensions.iran_accounting.domain.reject_only_riv_rate.classify_manufacture_outputs",
				return_value={
					CLASS_MAIN_FG: [],
					CLASS_MAIN_PRODUCT_REJECT: [doc.items[1]],
					CLASS_COMPONENT_SCRAP: [],
					CLASS_BULK_SCRAP: [],
					"CO_PRODUCT": [],
					"CO_PRODUCT_REJECT": [],
					"OTHER_OUTPUT": [],
				},
			):
				r1 = get_iran_reject_only_produced_rate(sle)
				r2 = get_iran_reject_only_produced_rate(sle)
		self.assertEqual(r1, 357596.0)
		self.assertEqual(r2, 357596.0)


if __name__ == "__main__":
	unittest.main()
