# Copyright (c) 2026, ERPNext Extensions contributors
"""Restricted Job Card secondary-type Apply (v5.5.33).

Run on the development clone whose Manufacture row is already By-Product:

  bench --site jc-manufacture-unlock.localhost execute \
    erpnext_extensions.iran_accounting.job_card_stock_rebuild.tests.test_secondary_type_scope_v5533.run_all
"""

from __future__ import annotations

import unittest
from unittest import mock

import frappe
from frappe.utils import flt

from erpnext_extensions import __version__ as APP_VERSION
from erpnext_extensions.iran_accounting.job_card_stock_rebuild import api, service, statuses as S
from erpnext_extensions.iran_accounting.job_card_stock_rebuild.service import (
	SCOPE_SECONDARY_TYPE_ONLY,
	_business_diffs,
	_job_card_business_snapshot,
	_normalize_apply_scope,
	_stock_business_snapshot,
	apply_rebuild,
	preview_rebuild,
)

JC = "PO-JOB07031"
ROW = "oa4knndmq6"
ITEM = "30500006"
SIBLING_ITEM = "13200187"
SE = "MAT-STE-2026-24883-1"
SE_ROW = "aelcfduknt"
ALLOWED_SITE = "jc-manufacture-unlock.localhost"


def _ok(name, cond, detail=""):
	return {"name": name, "pass": bool(cond), "detail": detail}


def _secondary_type(row_name=ROW):
	return frappe.db.get_value("Job Card Secondary Item", row_name, "secondary_item_type")


def _returnable_map():
	rows = frappe.get_all(
		"Job Card Item",
		filters={"parent": JC},
		fields=["name", "item_code", "custom_returnable_qty"],
		order_by="idx",
		limit_page_length=0,
	)
	return {row.name: (row.item_code, flt(row.custom_returnable_qty)) for row in rows}


def _set_secondary_type(new_type: str):
	doc = frappe.get_doc("Job Card", JC)
	target = next(row for row in doc.secondary_items if row.name == ROW)
	target.secondary_item_type = new_type
	doc.save()


def _approval_from(suggestion):
	return {
		"job_card": JC,
		"secondary_row": suggestion["secondary_row"],
		"current_type": suggestion["current_type"],
		"approved_type": suggestion["suggested_type"],
		"evidence_fingerprint": suggestion["evidence_fingerprint"],
	}


def _target_suggestion(preview):
	rows = (preview.get("secondary_type_suggestions") or {}).get("rows") or []
	return next((row for row in rows if row.get("secondary_row") == ROW), None)


class TestScopeContract(unittest.TestCase):
	def test_omitted_scope_stays_unrestricted(self):
		self.assertIsNone(_normalize_apply_scope(None))
		self.assertIsNone(_normalize_apply_scope(""))
		self.assertIsNone(_normalize_apply_scope("none"))

	def test_named_scope_is_exact(self):
		self.assertEqual(_normalize_apply_scope("SECONDARY_TYPE_ONLY"), SCOPE_SECONDARY_TYPE_ONLY)
		with self.assertRaises(frappe.ValidationError):
			_normalize_apply_scope("TRACKING_AND_TYPE")

	def test_unrestricted_apply_still_writes_tracking(self):
		plan = {
			"fingerprint": "fp",
			"writes": [{"type": "jc_item_field", "fieldname": "custom_returnable_qty", "write_required": True}],
			"blockers": [],
			"statuses": [],
			"warnings": [],
			"job_card": JC,
			"evidence_summary": {},
			"manufacture_readiness_status": None,
			"secondary_type_suggestions": {"rows": []},
		}
		approval = {
			"job_card": JC,
			"secondary_row": ROW,
			"current_type": "Scrap",
			"approved_type": "By-Product",
		}
		with (
			mock.patch.object(service, "_build_plan", return_value=plan),
			mock.patch.object(service, "build_evidence", return_value={"fingerprint": "fp"}),
			mock.patch.object(
				service,
				"validate_type_approvals",
				return_value={"ok": True, "normalized": [approval], "errors": []},
			),
			mock.patch.object(service, "_apply_writes", return_value=[{"type": "jc_item_field"}]) as writes,
			mock.patch.object(service, "apply_type_approvals", return_value=[]) as types,
			mock.patch.object(service, "_apply_secondary_types_via_document") as scoped,
			mock.patch.object(service, "_verify_tracking", return_value={"ok": True}),
			mock.patch.object(service, "simulate_normal_make_stock_entry", return_value={"ok": True}),
			mock.patch.object(service, "_snapshot_jc", return_value={}),
			mock.patch.object(service, "_write_audit", return_value="LOG"),
			mock.patch.object(frappe.db, "savepoint"),
			mock.patch.object(frappe.db, "sql"),
			mock.patch.object(frappe.db, "get_value", return_value="By-Product"),
		):
			result = apply_rebuild(
				JC,
				"fp",
				confirm=1,
				secondary_type_approvals=[approval],
			)
		self.assertTrue(result.get("mutated"), result.get("error"))
		writes.assert_called_once()
		types.assert_called_once()
		scoped.assert_not_called()

	def test_restricted_apply_skips_tracking_and_stock_entry_sync(self):
		plan = {
			"fingerprint": "fp",
			"writes": [{"type": "jc_item_field", "fieldname": "custom_returnable_qty", "write_required": True}],
			"blockers": [],
			"statuses": [],
			"warnings": [],
			"job_card": JC,
			"evidence_summary": {},
			"manufacture_readiness_status": None,
			"secondary_type_suggestions": {"rows": []},
		}
		approval = {
			"job_card": JC,
			"secondary_row": ROW,
			"current_type": "Scrap",
			"approved_type": "By-Product",
		}
		with (
			mock.patch.object(service, "_build_plan", return_value=plan),
			mock.patch.object(service, "build_evidence", return_value={"fingerprint": "fp"}),
			mock.patch.object(
				service,
				"validate_type_approvals",
				return_value={"ok": True, "normalized": [approval], "errors": []},
			),
			mock.patch.object(service, "_apply_writes") as writes,
			mock.patch.object(service, "apply_type_approvals") as types,
			mock.patch.object(
				service,
				"_apply_secondary_types_via_document",
				return_value=[{"type": "secondary_item_type"}],
			) as scoped,
			mock.patch.object(service, "simulate_normal_make_stock_entry", return_value={"ok": True}),
			mock.patch.object(service, "_snapshot_jc", return_value={}),
			mock.patch.object(service, "_write_audit", return_value="LOG"),
			mock.patch.object(frappe.db, "savepoint"),
			mock.patch.object(frappe.db, "sql"),
			mock.patch.object(frappe.db, "get_value", return_value="By-Product"),
		):
			result = apply_rebuild(
				JC,
				"fp",
				confirm=1,
				secondary_type_approvals=[approval],
				apply_scope=SCOPE_SECONDARY_TYPE_ONLY,
			)
		self.assertTrue(result.get("mutated"))
		self.assertEqual(result.get("verification", {}).get("tracking_writes_skipped"), 1)
		scoped.assert_called_once()
		writes.assert_not_called()
		types.assert_not_called()


def _precondition():
	if frappe.local.site == "erp.espadpharmed.com":
		return _ok("SITE", False, "refusing production")
	if frappe.local.site != ALLOWED_SITE:
		return _ok("SITE", False, f"integration runs only on {ALLOWED_SITE}, got {frappe.local.site}")
	if not frappe.db.exists("Job Card", JC):
		return _ok("SITE", False, "Job Card missing")
	row = frappe.db.get_value(
		"Stock Entry Detail",
		SE_ROW,
		["item_code", "secondary_item_type", "custom_output_class", "custom_output_equivalent_factor", "parent"],
		as_dict=1,
	)
	if not row or row.parent != SE or row.item_code != ITEM:
		return _ok("SITE", False, "target Stock Entry row missing")
	if (row.secondary_item_type or "") != "By-Product" or (row.custom_output_class or "") != "CO_PRODUCT":
		return _ok("SITE", False, "Stock Entry is not already By-Product / CO_PRODUCT")
	if abs(flt(row.custom_output_equivalent_factor) - 1) > 1e-9:
		return _ok("SITE", False, "equivalent factor is not 1")
	return None


def _integration():
	results = []
	original = _secondary_type()
	if original not in ("Scrap", "By-Product"):
		return [_ok("BASELINE", False, f"unexpected starting type {original}")]

	before_stock = _stock_business_snapshot(JC)
	returnable_before = _returnable_map()
	if original != "Scrap":
		before_jc = _job_card_business_snapshot(JC)
		_set_secondary_type("Scrap")
		stage_diffs = _business_diffs(before_jc, _job_card_business_snapshot(JC))
		allowed = [d for d in stage_diffs if d["path"] == f"secondary_items.{ROW}.secondary_item_type"]
		results.append(
			_ok(
				"STAGE_SCRAP_IS_TYPE_ONLY",
				len(stage_diffs) == 1 and allowed and allowed[0]["after"] == "Scrap",
				service._format_diffs(stage_diffs),
			)
		)
		if not results[-1]["pass"]:
			return results

	preview = preview_rebuild(JC)
	suggestion = _target_suggestion(preview)
	returnable_writes = [
		row
		for row in (preview.get("writes") or [])
		if row.get("fieldname") == "custom_returnable_qty" and row.get("write_required")
	]
	results.append(
		_ok(
			"TEST_1_PREVIEW",
			bool(suggestion)
			and suggestion.get("current_type") == "Scrap"
			and suggestion.get("suggested_type") == "By-Product"
			and suggestion.get("approval_enabled") is True
			and suggestion.get("item_code") == ITEM
			and suggestion.get("evidence_fingerprint"),
			{
				"fingerprint": preview.get("fingerprint"),
				"evidence_fingerprint": (suggestion or {}).get("evidence_fingerprint"),
				"returnable_writes": len(returnable_writes),
			},
		)
	)
	if not results[-1]["pass"]:
		return results

	approval = _approval_from(suggestion)
	stale = apply_rebuild(
		JC,
		"stale-fingerprint",
		confirm=1,
		secondary_type_approvals=[approval],
		apply_scope=SCOPE_SECONDARY_TYPE_ONLY,
	)
	results.append(
		_ok(
			"TEST_8_STALE_FINGERPRINT",
			stale.get("mutated") is False
			and stale.get("overall_status") == S.STALE_PREVIEW
			and _secondary_type() == "Scrap"
			and _returnable_map() == returnable_before,
			stale.get("error"),
		)
	)

	frappe.set_user("Guest")
	denied = False
	denied_detail = ""
	try:
		api.apply(
			JC,
			preview["fingerprint"],
			confirm=1,
			secondary_type_approvals=frappe.as_json([approval]),
			apply_scope=SCOPE_SECONDARY_TYPE_ONLY,
		)
		denied_detail = "Guest was not rejected"
	except frappe.PermissionError as exc:
		denied = True
		denied_detail = str(exc)
	finally:
		frappe.set_user("Administrator")
	results.append(
		_ok(
			"TEST_9_UNAUTHORIZED",
			denied and _secondary_type() == "Scrap",
			denied_detail,
		)
	)

	from erpnext.manufacturing.doctype.job_card.job_card import JobCard

	def _dirty_before_update(self):
		if self.name == JC:
			for row in self.secondary_items:
				if row.item_code == SIBLING_ITEM:
					row.custom_output_equivalent_factor = 9

	JobCard.before_update_after_submit = _dirty_before_update
	try:
		dirty = apply_rebuild(
			JC,
			preview["fingerprint"],
			confirm=1,
			secondary_type_approvals=[approval],
			apply_scope=SCOPE_SECONDARY_TYPE_ONLY,
		)
	finally:
		del JobCard.before_update_after_submit
	sibling = frappe.db.get_value(
		"Job Card Secondary Item",
		{"parent": JC, "item_code": SIBLING_ITEM},
		["name", "secondary_item_type", "custom_output_equivalent_factor"],
		as_dict=1,
	)
	results.append(
		_ok(
			"TEST_11_UNEXPECTED_MUTATION",
			dirty.get("mutated") is False
			and "Unexpected mutation" in (dirty.get("error") or "")
			and _secondary_type() == "Scrap"
			and sibling
			and sibling.secondary_item_type == "Scrap"
			and abs(flt(sibling.custom_output_equivalent_factor)) < 1e-9
			and _returnable_map() == returnable_before,
			dirty.get("error"),
		)
	)

	fresh = preview_rebuild(JC)
	fresh_suggestion = _target_suggestion(fresh)
	dry = service.dry_run_rebuild(
		JC,
		fingerprint=fresh["fingerprint"],
		secondary_type_approvals=[_approval_from(fresh_suggestion)],
		apply_scope=SCOPE_SECONDARY_TYPE_ONLY,
	)
	results.append(
		_ok(
			"DRY_RUN_DOES_NOT_PERSIST",
			dry.get("mutated") is False
			and dry.get("dry_run_status") == "DRY_RUN_PASS"
			and _secondary_type() == "Scrap"
			and _returnable_map() == returnable_before,
			dry.get("error") or dry.get("dry_run_status"),
		)
	)

	drift_row = next(iter(returnable_before))
	frappe.db.savepoint("returnable_drift")
	try:
		# Fixture only: create one tracking drift, then roll it back.
		# The scoped Apply itself must not write this field.
		frappe.db.set_value(
			"Job Card Item", drift_row, "custom_returnable_qty", 12345, update_modified=False
		)
		frappe.clear_document_cache("Job Card", JC)
		drift_preview = preview_rebuild(JC)
		drift_writes = [
			row
			for row in (drift_preview.get("writes") or [])
			if row.get("jc_item") == drift_row and row.get("fieldname") == "custom_returnable_qty"
		]
		drift_suggestion = _target_suggestion(drift_preview)
		drift_applied = apply_rebuild(
			JC,
			drift_preview["fingerprint"],
			confirm=1,
			secondary_type_approvals=[_approval_from(drift_suggestion)],
			apply_scope=SCOPE_SECONDARY_TYPE_ONLY,
		)
		drift_qty = flt(
			frappe.db.get_value("Job Card Item", drift_row, "custom_returnable_qty")
		)
		results.append(
			_ok(
				"TEST_3_TRACKING_WRITE_SKIPPED",
				bool(drift_writes)
				and drift_writes[0].get("write_required")
				and drift_applied.get("mutated") is True
				and abs(drift_qty - 12345) < 1e-6
				and _secondary_type() == "By-Product",
				{
					"writes": len(drift_preview.get("writes") or []),
					"proposed": (drift_writes or [{}])[0].get("derived"),
					"stored": drift_qty,
					"error": drift_applied.get("error"),
				},
			)
		)
	finally:
		frappe.db.rollback(save_point="returnable_drift")
	if _secondary_type() != "Scrap" or _returnable_map() != returnable_before:
		results.append(_ok("DRIFT_ROLLBACK", False, "returnable drift leaked"))
		return results

	live_preview = preview_rebuild(JC)
	live_suggestion = _target_suggestion(live_preview)
	live_approval = _approval_from(live_suggestion)
	jc_before = _job_card_business_snapshot(JC)
	applied = apply_rebuild(
		JC,
		live_preview["fingerprint"],
		confirm=1,
		secondary_type_approvals=[live_approval],
		apply_scope=SCOPE_SECONDARY_TYPE_ONLY,
	)
	jc_after = _job_card_business_snapshot(JC)
	diffs = _business_diffs(jc_before, jc_after)
	stock_diffs = _business_diffs(before_stock, _stock_business_snapshot(JC))
	results.append(
		_ok(
			"TEST_2_APPLY",
			applied.get("mutated") is True
			and applied.get("error") in (None, "")
			and _secondary_type() == "By-Product"
			and len(diffs) == 1
			and diffs[0]["path"] == f"secondary_items.{ROW}.secondary_item_type"
			and diffs[0]["before"] == "Scrap"
			and diffs[0]["after"] == "By-Product",
			applied.get("error") or service._format_diffs(diffs),
		)
	)
	results.append(
		_ok(
			"TEST_3_RETURNABLE_UNCHANGED",
			_returnable_map() == returnable_before and len(returnable_before) == 17,
			{"rows": len(returnable_before), "changed": _returnable_map() != returnable_before},
		)
	)
	sibling_after = frappe.db.get_value(
		"Job Card Secondary Item",
		{"parent": JC, "item_code": SIBLING_ITEM},
		["name", "secondary_item_type", "stock_qty", "custom_output_equivalent_factor"],
		as_dict=1,
	)
	results.append(
		_ok(
			"TEST_4_SIBLING",
			sibling_after
			and sibling_after.name == sibling.name
			and sibling_after.secondary_item_type == "Scrap"
			and abs(flt(sibling_after.custom_output_equivalent_factor)) < 1e-9,
			sibling_after,
		)
	)
	results.append(
		_ok(
			"TEST_5_6_7_STOCK_SLE_GL_RIV",
			not stock_diffs,
			service._format_diffs(stock_diffs) or "unchanged",
		)
	)

	repeat_preview = preview_rebuild(JC)
	repeat = apply_rebuild(
		JC,
		repeat_preview["fingerprint"],
		confirm=1,
		secondary_type_approvals=[live_approval],
		apply_scope=SCOPE_SECONDARY_TYPE_ONLY,
	)
	repeat_diffs = _business_diffs(jc_after, _job_card_business_snapshot(JC))
	results.append(
		_ok(
			"TEST_10_REPEAT",
			repeat.get("mutated") is False
			and _secondary_type() == "By-Product"
			and not repeat_diffs
			and _returnable_map() == returnable_before
			and not _business_diffs(before_stock, _stock_business_snapshot(JC)),
			repeat.get("error"),
		)
	)
	results.append(
		_ok(
			"AUTHORIZED_DIFF_ONLY",
			original == "By-Product" and _secondary_type() == original,
			f"start {original} end {_secondary_type()} version {APP_VERSION}",
		)
	)
	return results


def run_all():
	contract = unittest.TestResult()
	suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestScopeContract)
	suite.run(contract)
	results = [
		_ok(
			"TEST_12_UNRESTRICTED_CONTRACT",
			contract.wasSuccessful(),
			[] if contract.wasSuccessful() else [str(err[1]) for err in contract.failures + contract.errors],
		)
	]
	blocked = _precondition()
	if blocked:
		results.append(blocked)
		return {"version": APP_VERSION, "site": frappe.local.site, "results": results}
	frappe.db.savepoint("secondary_type_scope_test")
	try:
		results.extend(_integration())
		if all(row["pass"] for row in results):
			frappe.db.commit()
		else:
			frappe.db.rollback(save_point="secondary_type_scope_test")
	except Exception:
		frappe.db.rollback(save_point="secondary_type_scope_test")
		raise
	return {
		"version": APP_VERSION,
		"site": frappe.local.site,
		"results": results,
		"passed": sum(1 for row in results if row["pass"]),
		"failed": sum(1 for row in results if not row["pass"]),
	}
