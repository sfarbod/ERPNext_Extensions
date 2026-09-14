# Copyright (c) 2026, Farbod Siyahpoosh and contributors
# For license information, please see license.txt

"""v5.2.10: CASE B — JE cancel/delete must not cancel PM Clearance."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from erpnext_extensions.petty_management.services.clearance_action_policy import (
	LIFECYCLE_APPROVED,
	find_active_settlement_je,
	heal_inactive_settlement_reference,
)


class TestHealInactiveSettlementReferenceV5210(unittest.TestCase):
	def test_cancelled_je_link_cleared_without_cancelling_clearance(self):
		doc = SimpleNamespace(
			name="CLR-1",
			docstatus=1,
			journal_entry="JE-CXL",
			status="Pending Journal Entry Submission",
			workflow_state="Approved",
		)
		with (
			patch(
				"erpnext_extensions.petty_management.services.clearance_action_policy.journal_entry_docstatus",
				return_value=2,
			),
			patch("frappe.db.set_value") as set_value,
			patch("frappe.get_all", return_value=[]),
			patch(
				"erpnext_extensions.petty_management.services.clearance_action_policy.sync_clearance_lifecycle",
				return_value=LIFECYCLE_APPROVED,
			) as sync,
		):
			changed = heal_inactive_settlement_reference(doc, persist=True)

		self.assertTrue(changed)
		self.assertIsNone(doc.journal_entry)
		self.assertEqual(doc.docstatus, 1)
		set_value.assert_any_call(
			"PM Clearance", "CLR-1", {"journal_entry": None}, update_modified=False
		)
		sync.assert_called()

	def test_active_je_not_cleared(self):
		doc = SimpleNamespace(name="CLR-1", docstatus=1, journal_entry="JE-OK", status="Settled")
		with patch(
			"erpnext_extensions.petty_management.services.clearance_action_policy.journal_entry_docstatus",
			return_value=1,
		):
			self.assertFalse(heal_inactive_settlement_reference(doc, persist=True))
		self.assertEqual(doc.journal_entry, "JE-OK")

	def test_cancelled_clearance_not_revived(self):
		doc = SimpleNamespace(name="CLR-1", docstatus=2, journal_entry="JE-X", status="Cancelled")
		self.assertFalse(heal_inactive_settlement_reference(doc, persist=True))


class TestFindActiveSettlementJeV5210(unittest.TestCase):
	def test_field_active_preferred(self):
		doc = SimpleNamespace(name="CLR-1", journal_entry="JE-1")
		with patch(
			"erpnext_extensions.petty_management.services.clearance_action_policy.journal_entry_docstatus",
			return_value=0,
		):
			self.assertEqual(find_active_settlement_je(doc), "JE-1")

	def test_cancelled_field_falls_through_to_custom(self):
		doc = SimpleNamespace(name="CLR-1", journal_entry="JE-OLD")
		meta = MagicMock()
		meta.has_field.return_value = True
		with (
			patch(
				"erpnext_extensions.petty_management.services.clearance_action_policy.journal_entry_docstatus",
				return_value=2,
			),
			patch("frappe.get_meta", return_value=meta),
			patch("frappe.get_all", return_value=["JE-NEW"]),
		):
			self.assertEqual(find_active_settlement_je(doc), "JE-NEW")


class TestUnlinkNeverCancelsClearanceV5210(unittest.TestCase):
	def test_unlink_syncs_but_skips_docstatus_2(self):
		from erpnext_extensions.petty_management import journal_entry_hooks as jeh

		meta = MagicMock()
		meta.has_field.return_value = True

		def _get_value(dt, nm, field=None, **kw):
			if dt == "PM Clearance" and field == "docstatus":
				return 2
			if dt == "Journal Entry" and field == "custom_pm_clearance":
				return "CLR-CXL"
			if kw.get("as_dict"):
				return {
					"holder": None,
					"employee": None,
					"company": None,
					"docstatus": 2,
					"workflow_state": "Approved",
					"status": "Cancelled",
				}
			return None

		with (
			patch("frappe.get_meta", return_value=meta),
			patch("frappe.db.exists", return_value=True),
			patch("frappe.get_all", side_effect=[[], ["CLR-CXL"]]),
			patch("frappe.db.get_value", side_effect=_get_value),
			patch("frappe.db.set_value") as set_value,
			patch("frappe.get_doc") as get_doc,
			patch(
				"erpnext_extensions.petty_management.services.clearance_action_policy.sync_clearance_lifecycle"
			) as sync,
			patch("erpnext_extensions.petty_management.petty_audit.log_event"),
			patch.object(jeh, "_notify_pm_requests_for_journal_entry"),
		):
			jeh.unlink_settlement_je_from_clearances("JE-1", event="pm_journal_entry_cancelled")

		get_doc.assert_not_called()
		sync.assert_not_called()
		set_value.assert_any_call(
			"PM Clearance", "CLR-CXL", {"journal_entry": None}, update_modified=False
		)


if __name__ == "__main__":
	unittest.main()
