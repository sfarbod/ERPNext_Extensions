from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from erpnext_extensions import approved_decimal_precision as mod


class TestApprovedMonetaryDecimalPrecision(unittest.TestCase):
	def test_approved_allowlists_exact(self):
		self.assertEqual(
			mod.APPROVED_FIELDS_BY_DOCTYPE["Facility"],
			(
				"principal_amount",
				"profit_amount",
				"total_liability_amount",
				"opening_paid_principal_amount",
				"opening_paid_profit_amount",
				"opening_paid_penalty_amount",
				"received_amount",
				"paid_principal_amount",
				"paid_profit_amount",
				"paid_penalty_amount",
				"remaining_principal_amount",
				"remaining_profit_amount",
				"remaining_total_amount",
			),
		)
		self.assertEqual(
			mod.APPROVED_FIELDS_BY_DOCTYPE["Facility Repayment"],
			("principal_amount", "profit_amount", "penalty_amount", "total_payment_amount"),
		)
		self.assertEqual(len(mod.approved_field_targets()), 17)

	def test_decide_decimal_action_matrix(self):
		self.assertEqual(mod.decide_decimal_action(None), mod.SKIP_MISSING_COLUMN)
		self.assertEqual(
			mod.decide_decimal_action({"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 30, "NUMERIC_SCALE": 9}),
			mod.SKIP_ALREADY_CORRECT,
		)
		self.assertEqual(
			mod.decide_decimal_action({"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 38, "NUMERIC_SCALE": 9}),
			mod.SKIP_ALREADY_WIDER,
		)
		self.assertEqual(
			mod.decide_decimal_action({"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 24, "NUMERIC_SCALE": 9}),
			mod.ALTER_TO_DECIMAL_30_9,
		)
		self.assertEqual(
			mod.decide_decimal_action({"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 21, "NUMERIC_SCALE": 6}),
			mod.SKIP_UNEXPECTED_SCALE,
		)
		self.assertEqual(
			mod.decide_decimal_action({"DATA_TYPE": "double", "NUMERIC_PRECISION": None, "NUMERIC_SCALE": None}),
			mod.SKIP_UNEXPECTED_TYPE,
		)

	def test_desired_metadata_length_preserves_wider_decimal(self):
		self.assertEqual(mod.desired_metadata_length(None), 30)
		self.assertEqual(
			mod.desired_metadata_length({"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 38, "NUMERIC_SCALE": 9}),
			38,
		)
		self.assertEqual(
			mod.desired_metadata_length({"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 21, "NUMERIC_SCALE": 9}),
			30,
		)

	@patch("erpnext_extensions.approved_decimal_precision.frappe.clear_cache")
	@patch("erpnext_extensions.approved_decimal_precision.make_property_setter")
	@patch("erpnext_extensions.approved_decimal_precision.frappe.db.set_value")
	@patch("erpnext_extensions.approved_decimal_precision.frappe.db.get_value")
	def test_property_setter_create_update_skip(self, mock_get_value, mock_set_value, mock_make_ps, mock_clear_cache):
		logger = MagicMock()

		mock_get_value.side_effect = [None]
		action, value = mod.ensure_length_property_setter("Facility", "principal_amount", 30, logger)
		self.assertEqual((action, value), ("CREATE_METADATA_LENGTH", 30))
		mock_make_ps.assert_called_once()

		mock_make_ps.reset_mock()
		mock_get_value.side_effect = ["Facility-principal_amount-length", "21"]
		action, value = mod.ensure_length_property_setter("Facility", "principal_amount", 30, logger)
		self.assertEqual((action, value), ("UPDATE_METADATA_LENGTH", 30))
		mock_set_value.assert_called_once()
		mock_clear_cache.assert_called()

		mock_set_value.reset_mock()
		mock_get_value.side_effect = ["Facility-principal_amount-length", "38"]
		action, value = mod.ensure_length_property_setter("Facility", "principal_amount", 30, logger)
		self.assertEqual((action, value), ("SKIP_METADATA_ALREADY_SET", 38))
		mock_set_value.assert_not_called()

	def test_verify_and_set_metadata_isolates_errors(self):
		logger = MagicMock()
		targets = (
			mod.ApprovedFieldTarget("Facility", "principal_amount"),
			mod.ApprovedFieldTarget("Facility", "received_amount"),
		)

		with (
			patch.object(mod, "approved_field_targets", return_value=targets),
			patch("erpnext_extensions.approved_decimal_precision.frappe.db.exists", return_value=True),
			patch("erpnext_extensions.approved_decimal_precision.frappe.get_meta") as mock_meta,
			patch.object(mod, "read_column_schema", return_value={"DATA_TYPE": "decimal", "NUMERIC_PRECISION": 21, "NUMERIC_SCALE": 9}),
			patch.object(mod, "ensure_length_property_setter", side_effect=[Exception("boom"), ("CREATE_METADATA_LENGTH", 30)]),
			patch("erpnext_extensions.approved_decimal_precision.frappe.get_traceback", return_value="traceback"),
		):
			mock_meta.return_value.get_field.side_effect = [
				type("DF", (), {"fieldtype": "Currency"})(),
				type("DF", (), {"fieldtype": "Currency"})(),
			]
			rows = mod.verify_and_set_metadata(logger)

		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[0]["status"], "error")
		self.assertEqual(rows[1]["metadata_action"], "CREATE_METADATA_LENGTH")
