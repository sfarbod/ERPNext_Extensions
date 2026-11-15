# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Cheque Opening Import — workflow_state acceptance, normalization, and graph paths."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from erpnext_extensions.cheque_management.doctype.cheque_opening_import.cheque_opening_import import (
	_normalize_row_link_fields,
	_resolve_bank_account_name,
	_resolve_drawer_bank_name,
	_validate_row_preview,
	find_workflow_path,
)
from erpnext_extensions.cheque_management.pdc_workflow_state_machine import (
	CHEQUE_DIRECTION_PAYABLE,
	CHEQUE_DIRECTION_RECEIVABLE,
	WORKFLOW_ASSIGNED_DEBT_PURCHASE,
	WORKFLOW_BOUNCED,
	WORKFLOW_DRAFT,
	WORKFLOW_REGISTERED,
	WORKFLOW_SENT_TO_BANK,
	normalize_workflow_state_value,
)
from erpnext_extensions.cheque_management.pdc_workflow_to_cheque_status import (
	CHEQUE_STATUS_DEBT_PURCHASE_IN_COLLECTION,
	CHEQUE_STATUS_BOUNCED,
	CHEQUE_STATUS_IN_CLEARING,
	CHEQUE_STATUS_IN_HAND,
	map_workflow_state_to_cheque_status,
)


def _row(**extra) -> dict:
	base = {
		"cheque_direction": CHEQUE_DIRECTION_RECEIVABLE,
		"company": "C",
		"bank_account": "BA-1",
		"cheque_number": "1001",
		"cheque_due_date": "2026-06-20",
		"cheque_amount": "1000",
		"party_type": "Customer",
		"party": "CUST-1",
		"drawer_bank_name": "Bank X",
		"workflow_state": WORKFLOW_REGISTERED,
	}
	base.update(extra)
	return base


class TestNormalizeWorkflowStateValue(unittest.TestCase):
	def test_blank_is_draft(self):
		self.assertEqual(normalize_workflow_state_value(None), WORKFLOW_DRAFT)
		self.assertEqual(normalize_workflow_state_value(""), WORKFLOW_DRAFT)
		self.assertEqual(normalize_workflow_state_value("  "), WORKFLOW_DRAFT)

	def test_case_insensitive_debt_purchase(self):
		self.assertEqual(
			normalize_workflow_state_value("assigned to bank for debt purchase"),
			WORKFLOW_ASSIGNED_DEBT_PURCHASE,
		)
		self.assertEqual(
			normalize_workflow_state_value("ASSIGNED TO BANK FOR DEBT PURCHASE"),
			WORKFLOW_ASSIGNED_DEBT_PURCHASE,
		)

	def test_whitespace_collapse(self):
		self.assertEqual(
			normalize_workflow_state_value("  Assigned   to Bank  for Debt Purchase  "),
			WORKFLOW_ASSIGNED_DEBT_PURCHASE,
		)
		self.assertEqual(
			normalize_workflow_state_value("sent  to   bank"),
			WORKFLOW_SENT_TO_BANK,
		)

	def test_unknown_preserved_after_collapse(self):
		self.assertEqual(normalize_workflow_state_value("Not A Real State"), "Not A Real State")
		self.assertEqual(normalize_workflow_state_value("  weird   state  "), "weird state")


class TestOpeningImportWorkflowPaths(unittest.TestCase):
	def test_path_to_assigned_debt_purchase(self):
		path = find_workflow_path(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE)
		self.assertEqual(
			path,
			[WORKFLOW_DRAFT, WORKFLOW_REGISTERED, WORKFLOW_ASSIGNED_DEBT_PURCHASE],
		)

	def test_path_to_assigned_debt_purchase_casefold_input(self):
		path = find_workflow_path(
			CHEQUE_DIRECTION_RECEIVABLE, "assigned to bank for debt purchase"
		)
		self.assertEqual(path[-1], WORKFLOW_ASSIGNED_DEBT_PURCHASE)

	def test_path_to_bounced(self):
		path = find_workflow_path(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_BOUNCED)
		self.assertEqual(path[0], WORKFLOW_DRAFT)
		self.assertEqual(path[-1], WORKFLOW_BOUNCED)
		# Shortest path may go via Assigned DP (bounce-from-DP edge) or via Sent to Bank.
		self.assertTrue(
			WORKFLOW_SENT_TO_BANK in path or WORKFLOW_ASSIGNED_DEBT_PURCHASE in path,
			path,
		)

	def test_path_to_registered(self):
		self.assertEqual(
			find_workflow_path(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_REGISTERED),
			[WORKFLOW_DRAFT, WORKFLOW_REGISTERED],
		)

	def test_path_to_sent_to_bank(self):
		self.assertEqual(
			find_workflow_path(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_SENT_TO_BANK),
			[WORKFLOW_DRAFT, WORKFLOW_REGISTERED, WORKFLOW_SENT_TO_BANK],
		)

	def test_invalid_state_rejected(self):
		with self.assertRaises(Exception) as ctx:
			find_workflow_path(CHEQUE_DIRECTION_RECEIVABLE, "Totally Invalid State")
		self.assertIn("Invalid workflow_state", str(ctx.exception))

	def test_payable_cannot_reach_assigned_dp(self):
		with self.assertRaises(Exception) as ctx:
			find_workflow_path(CHEQUE_DIRECTION_PAYABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE)
		self.assertIn("No allowed workflow path", str(ctx.exception))

	def test_debt_purchase_settled_unreachable_from_draft(self):
		with self.assertRaises(Exception) as ctx:
			find_workflow_path(CHEQUE_DIRECTION_RECEIVABLE, "Debt Purchase Settled")
		self.assertIn("No allowed workflow path", str(ctx.exception))


class TestOpeningImportLinkResolution(unittest.TestCase):
	def test_resolve_bank_account_by_canonical_name(self):
		canonical = "بانک خاورمیانه مهستان 100511300000000593  - بانک خاورمیانه"
		mod = "erpnext_extensions.cheque_management.doctype.cheque_opening_import.cheque_opening_import"
		with patch(f"{mod}.frappe") as mock_frappe:
			mock_frappe.db.exists.return_value = True
			self.assertEqual(_resolve_bank_account_name(canonical, "C"), canonical)
			mock_frappe.db.sql.assert_not_called()

	def test_resolve_bank_account_by_account_name(self):
		excel = " بانک خاورمیانه مهستان 100511300000000593 "
		canonical = "بانک خاورمیانه مهستان 100511300000000593  - بانک خاورمیانه"
		mod = "erpnext_extensions.cheque_management.doctype.cheque_opening_import.cheque_opening_import"
		with patch(f"{mod}.frappe") as mock_frappe:
			mock_frappe.db.exists.return_value = False
			mock_frappe.db.sql.return_value = [[canonical]]
			self.assertEqual(_resolve_bank_account_name(excel, "C"), canonical)

	def test_resolve_bank_account_collapses_internal_spaces(self):
		# Production COI-2026-13437: Excel used a single space before "-" while Link name has two.
		excel = "بانک خاورمیانه مهستان 100511300000000593 - بانک خاورمیانه"
		canonical = "بانک خاورمیانه مهستان 100511300000000593  - بانک خاورمیانه"
		mod = "erpnext_extensions.cheque_management.doctype.cheque_opening_import.cheque_opening_import"
		with patch(f"{mod}.frappe") as mock_frappe:
			mock_frappe.db.exists.return_value = False
			mock_frappe.db.sql.return_value = [[canonical]]
			self.assertEqual(_resolve_bank_account_name(excel, "C"), canonical)

	def test_resolve_bank_account_ambiguous_raises(self):
		mod = "erpnext_extensions.cheque_management.doctype.cheque_opening_import.cheque_opening_import"
		with patch(f"{mod}.frappe") as mock_frappe:
			mock_frappe.db.exists.return_value = False
			mock_frappe.db.sql.return_value = [["BA-A"], ["BA-B"]]

			def _throw(msg, title=None):
				raise Exception(str(msg))

			mock_frappe.throw.side_effect = _throw
			mock_frappe._ = lambda s: s
			with self.assertRaises(Exception) as ctx:
				_resolve_bank_account_name("ambiguous", "C")
			self.assertIn("Ambiguous Bank Account", str(ctx.exception))

	def test_resolve_drawer_bank_prefixed(self):
		mod = "erpnext_extensions.cheque_management.doctype.cheque_opening_import.cheque_opening_import"
		with patch(f"{mod}.frappe") as mock_frappe:
			mock_frappe.db.exists.side_effect = lambda dt, name: name == "بانک خاورمیانه"
			self.assertEqual(_resolve_drawer_bank_name("خاورمیانه"), "بانک خاورمیانه")

	def test_resolve_drawer_bank_exact(self):
		mod = "erpnext_extensions.cheque_management.doctype.cheque_opening_import.cheque_opening_import"
		with patch(f"{mod}.frappe") as mock_frappe:
			mock_frappe.db.exists.return_value = True
			self.assertEqual(_resolve_drawer_bank_name("بانک خاورمیانه"), "بانک خاورمیانه")

	def test_resolve_drawer_bank_ambiguous_raises(self):
		mod = "erpnext_extensions.cheque_management.doctype.cheque_opening_import.cheque_opening_import"
		with patch(f"{mod}.frappe") as mock_frappe:
			mock_frappe.db.exists.return_value = False
			mock_frappe.db.sql.return_value = [["Bank A"], ["Bank B"]]

			def _throw(msg, title=None):
				raise Exception(str(msg))

			mock_frappe.throw.side_effect = _throw
			mock_frappe._ = lambda s: s
			with self.assertRaises(Exception) as ctx:
				_resolve_drawer_bank_name("Bank")
			self.assertIn("Ambiguous Drawer Bank", str(ctx.exception))

	def test_normalize_row_mutates_links(self):
		row = {
			"company": "C",
			"bank_account": " BA-RAW ",
			"drawer_bank_name": "خاورمیانه",
		}
		mod = "erpnext_extensions.cheque_management.doctype.cheque_opening_import.cheque_opening_import"
		with (
			patch(f"{mod}._resolve_bank_account_name", return_value="BA-CANON"),
			patch(f"{mod}._resolve_drawer_bank_name", return_value="بانک خاورمیانه"),
		):
			err = _normalize_row_link_fields(row)
		self.assertIsNone(err)
		self.assertEqual(row["bank_account"], "BA-CANON")
		self.assertEqual(row["drawer_bank_name"], "بانک خاورمیانه")

	def test_preview_and_execute_share_link_normalize(self):
		"""Preview validation and import_row both call _normalize_row_link_fields."""
		row = _row(
			workflow_state="Assigned to Bank for Debt Purchase",
			bank_account=" BA-RAW ",
			drawer_bank_name="خاورمیانه",
			debt_purchase_assignment_date="2026-04-01",
		)
		mod = "erpnext_extensions.cheque_management.doctype.cheque_opening_import.cheque_opening_import"
		with (
			patch(f"{mod}._resolve_bank_account_name", return_value="BA-CANON") as ba,
			patch(f"{mod}._resolve_drawer_bank_name", return_value="بانک خاورمیانه") as dr,
		):
			err = _validate_row_preview(row, 2)
			self.assertIsNone(err)
			self.assertEqual(row["bank_account"], "BA-CANON")
			self.assertEqual(row["drawer_bank_name"], "بانک خاورمیانه")
			ba.assert_called()
			dr.assert_called()
			# Second normalize (Execute path) is idempotent on already-canonical values.
			err2 = _normalize_row_link_fields(row)
			self.assertIsNone(err2)
			self.assertEqual(row["bank_account"], "BA-CANON")
			self.assertEqual(row["drawer_bank_name"], "بانک خاورمیانه")


class TestOpeningImportPreviewValidation(unittest.TestCase):
	def _validate(self, row, row_no=2):
		# Preview now resolves Links; stub resolvers so unit tests stay DB-free.
		with (
			patch(
				"erpnext_extensions.cheque_management.doctype.cheque_opening_import.cheque_opening_import._resolve_bank_account_name",
				side_effect=lambda raw, company=None: (raw or "").strip() or None,
			),
			patch(
				"erpnext_extensions.cheque_management.doctype.cheque_opening_import.cheque_opening_import._resolve_drawer_bank_name",
				side_effect=lambda raw: (raw or "").strip() or None,
			),
		):
			return _validate_row_preview(row, row_no)

	def test_registered_ok(self):
		self.assertIsNone(self._validate(_row(workflow_state="Registered")))

	def test_sent_to_bank_ok(self):
		self.assertIsNone(self._validate(_row(workflow_state="Sent to Bank")))

	def test_bounced_ok(self):
		self.assertIsNone(
			self._validate(_row(workflow_state="Bounced", bounced_date="2026-04-06"))
		)

	def test_assigned_dp_requires_assignment_date(self):
		err = self._validate(_row(workflow_state="Assigned to Bank for Debt Purchase"))
		self.assertIsNotNone(err)
		self.assertIn("debt_purchase_assignment_date", err or "")

	def test_assigned_dp_ok_with_assignment_date(self):
		self.assertIsNone(
			self._validate(
				_row(
					workflow_state="Assigned to Bank for Debt Purchase",
					debt_purchase_assignment_date="2026-04-01",
				)
			)
		)

	def test_assigned_dp_lowercase_ok_with_assignment_date(self):
		self.assertIsNone(
			self._validate(
				_row(
					workflow_state="assigned to bank for debt purchase",
					debt_purchase_assignment_date="2026-04-01",
				)
			)
		)

	def test_registered_ok_without_assignment_date(self):
		self.assertIsNone(self._validate(_row(workflow_state="Registered")))

	def test_assigned_dp_requires_bank(self):
		err = self._validate(
			_row(workflow_state="Assigned to Bank for Debt Purchase", bank_account="")
		)
		self.assertIsNotNone(err)
		self.assertIn("bank_account", err or "")

	def test_invalid_workflow_rejected(self):
		err = self._validate(_row(workflow_state="NotAState"))
		self.assertIsNotNone(err)
		self.assertIn("Invalid workflow_state", err or "")

	def test_unresolvable_bank_rejected(self):
		with (
			patch(
				"erpnext_extensions.cheque_management.doctype.cheque_opening_import.cheque_opening_import._resolve_bank_account_name",
				return_value=None,
			),
			patch(
				"erpnext_extensions.cheque_management.doctype.cheque_opening_import.cheque_opening_import._resolve_drawer_bank_name",
				return_value="بانک خاورمیانه",
			),
		):
			err = _validate_row_preview(
				_row(workflow_state="Assigned to Bank for Debt Purchase", bank_account="MISSING"),
				2,
			)
		self.assertIsNotNone(err)
		self.assertIn("Bank Account not found", err or "")


class TestChequeStatusMappingForImportTargets(unittest.TestCase):
	def test_status_for_import_targets(self):
		self.assertEqual(
			map_workflow_state_to_cheque_status(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_REGISTERED
			),
			CHEQUE_STATUS_IN_HAND,
		)
		self.assertEqual(
			map_workflow_state_to_cheque_status(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_SENT_TO_BANK
			),
			CHEQUE_STATUS_IN_CLEARING,
		)
		self.assertEqual(
			map_workflow_state_to_cheque_status(CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_BOUNCED),
			CHEQUE_STATUS_BOUNCED,
		)
		self.assertEqual(
			map_workflow_state_to_cheque_status(
				CHEQUE_DIRECTION_RECEIVABLE, WORKFLOW_ASSIGNED_DEBT_PURCHASE
			),
			CHEQUE_STATUS_DEBT_PURCHASE_IN_COLLECTION,
		)


if __name__ == "__main__":
	unittest.main()
