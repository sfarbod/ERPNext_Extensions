# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.6.1: sequential session keeps HRC-00001 status/progress in sync."""

from __future__ import annotations

import unittest
from unittest import mock

from erpnext_extensions.iran_accounting.historical_rebuild import sequential_rebuild as sr
from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	STATUS_FAILED,
	STATUS_PAUSED,
	STATUS_RUNNING,
)


class TestSequentialCampaignStatus(unittest.TestCase):
	def test_campaign_remarks_with_session_replaces_old(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.frappe.db.get_value",
			return_value="SEQ_SESSION=old\nkeep-me",
		):
			out = sr._campaign_remarks_with_session("HRC-00001", "20261018160151")
		self.assertTrue(out.startswith("SEQ_SESSION=20261018160151"))
		self.assertIn("keep-me", out)
		self.assertNotIn("SEQ_SESSION=old", out)

	def test_sync_sets_running_and_session_remark(self):
		calls = []

		def capture_set_value(doctype, name, fields, update_modified=True):
			calls.append(dict(fields))

		with mock.patch.object(sr, "_campaign_remarks_with_session", return_value="SEQ_SESSION=20261018160151"), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.frappe.db.exists",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.frappe.db.get_value",
			return_value=None,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.frappe.db.set_value",
			side_effect=capture_set_value,
		):
			sr.sync_campaign_execution_state(
				"HRC-00001",
				status=STATUS_RUNNING,
				session_id="20261018160151",
				current_riv="riv1",
				current_item="13200244",
				current_warehouse="WH",
				touch_started=True,
			)

		self.assertTrue(calls)
		fields = calls[0]
		self.assertEqual(fields["status"], STATUS_RUNNING)
		self.assertEqual(fields["remarks"], "SEQ_SESSION=20261018160151")
		self.assertEqual(fields["current_voucher_no"], "riv1")
		self.assertEqual(fields["last_voucher_type"], "13200244")

	def test_fail_fast_sets_failed_status(self):
		calls = []

		def capture_set_value(doctype, name, fields, update_modified=True):
			calls.append(dict(fields))

		with mock.patch.object(sr, "_campaign_remarks_with_session", return_value="SEQ_SESSION=20261018160151"), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.frappe.db.exists",
			return_value=True,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.frappe.db.get_value",
			return_value=None,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.frappe.db.set_value",
			side_effect=capture_set_value,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign._append_log"
		) as log:
			sr.sync_campaign_execution_state(
				"HRC-00001",
				status=STATUS_FAILED,
				session_id="20261018160151",
				failure={
					"riv": "btua40o4u4",
					"item_code": "13200244",
					"warehouse": "WH",
					"error": "I3",
				},
			)
			self.assertTrue(log.called)

		self.assertEqual(calls[0]["status"], STATUS_FAILED)

	def test_duplicate_executor_lock(self):
		cache = mock.Mock()
		cache.get_value.return_value = "OTHER_SESSION"
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.frappe.cache",
			return_value=cache,
		):
			with self.assertRaises(Exception):
				sr._acquire_executor_lock("HRC-00001", "NEW_SESSION")

	def test_parse_campaign_session_id(self):
		with mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.frappe.db.get_value",
			return_value="SEQ_SESSION=20261018160151\nnote",
		):
			self.assertEqual(sr.parse_campaign_session_id("HRC-00001"), "20261018160151")

	def test_status_api_includes_sequential_block(self):
		from erpnext_extensions.iran_accounting.historical_rebuild import campaign as camp

		fake_doc = mock.Mock()
		fake_doc.name = "HRC-00001"
		fake_doc.status = STATUS_PAUSED
		fake_doc.execution_logs = []
		fake_doc.current_voucher_type = None
		fake_doc.current_voucher_no = None
		fake_doc.last_voucher_type = "13200244"
		fake_doc.last_voucher_no = "WH"
		fake_doc.last_ordinal = 0
		fake_doc.last_activity = None
		fake_doc.started_at = None
		fake_doc.completed_at = None
		fake_doc.manifest_checksum = "x"
		fake_doc.reload = mock.Mock()

		sess = {
			"session_id": "20261018160151",
			"campaign": "HRC-00001",
			"status": "Failed",
			"riv_completed": ["a"] * 289,
			"riv_failed": ["btua40o4u4"],
			"riv_created": ["a"] * 290,
			"identities_done": ["x"] * 289,
			"started_at": "2026-10-18 16:01:51",
			"last_failure": {
				"riv": "btua40o4u4",
				"item_code": "13200244",
				"error": "I3",
			},
		}

		with mock.patch.object(camp, "_require_campaign", return_value=fake_doc), mock.patch.object(
			camp, "_refresh_counters", return_value={"total": 15968, "pending": 15968}
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.get_all",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.db.sql",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.db.count",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.get_seq_session",
			return_value=sess,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.parse_campaign_session_id",
			return_value="20261018160151",
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.chronological_identities",
			return_value=[mock.Mock()] * 4517,
		):
			out = camp.status("HRC-00001")

		self.assertEqual(out["active_session_id"], "20261018160151")
		self.assertEqual(out["session_status"], "Failed")
		self.assertEqual(out["sequential"]["session_riv_completed"], 289)
		self.assertEqual(out["session_last_failure"]["riv"], "btua40o4u4")
		self.assertIn("Active Session:", out["text"])
		self.assertIn("counters", out)
		self.assertIn("campaign", out)
		self.assertIn("status", out)

	def test_old_session_does_not_contaminate(self):
		from erpnext_extensions.iran_accounting.historical_rebuild import campaign as camp

		fake_doc = mock.Mock()
		fake_doc.name = "HRC-00001"
		fake_doc.status = STATUS_PAUSED
		fake_doc.execution_logs = []
		fake_doc.current_voucher_type = None
		fake_doc.current_voucher_no = None
		fake_doc.last_voucher_type = None
		fake_doc.last_voucher_no = None
		fake_doc.last_ordinal = 0
		fake_doc.last_activity = None
		fake_doc.started_at = None
		fake_doc.completed_at = None
		fake_doc.manifest_checksum = "x"
		fake_doc.reload = mock.Mock()

		foreign = {
			"session_id": "OTHER",
			"campaign": "HRC-OTHER",
			"riv_completed": ["x"] * 999,
			"riv_failed": [],
			"riv_created": [],
			"identities_done": [],
			"status": "Running",
		}
		with mock.patch.object(camp, "_require_campaign", return_value=fake_doc), mock.patch.object(
			camp, "_refresh_counters", return_value={"total": 10}
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.get_all",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.db.sql",
			return_value=[],
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.campaign.frappe.db.count",
			return_value=0,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.get_seq_session",
			return_value=foreign,
		), mock.patch(
			"erpnext_extensions.iran_accounting.historical_rebuild.sequential_rebuild.parse_campaign_session_id",
			return_value=None,
		):
			out = camp.status("HRC-00001")

		self.assertIsNone(out["session_status"])
		self.assertEqual(out["sequential"]["session_riv_completed"], 0)


if __name__ == "__main__":
	unittest.main()
