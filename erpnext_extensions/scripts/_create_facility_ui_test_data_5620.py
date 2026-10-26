#!/usr/bin/env python3
"""Create realistic Facility UI test data for v5.6.20 on development.localhost.

Authorized test-data only. Does not touch Production.
"""

from __future__ import annotations

import json
import unittest

import frappe
from frappe.utils import add_days, random_string, today

from erpnext_extensions.facility_management.doctype.facility.facility import create_receipt_journal_entry
from erpnext_extensions.facility_management.facility_e2e_context import (
	apply_facility_test_accounts,
	ensure_bank_master,
)
from erpnext_extensions.facility_management.facility_settings_doc import (
	get_facility_settings_doc,
	resolve_repayment_cost_center,
)
from erpnext_extensions.facility_management.tests.test_facility_debt_purchase_e2e import (
	DebtPurchaseDbFixtureMixin,
	_ensure_dp_facility_type,
	_pick_company,
)


# Avoid digit/dot loan-number tokens (facility identity uniqueness).
TAG = "UI TEST FAC"


def _ensure_normal_facility_type() -> str:
	name = "UI Test Normal Type"
	if frappe.db.exists("Facility Type", name):
		frappe.db.set_value("Facility Type", name, "is_debt_purchase", 0, update_modified=False)
		return name
	doc = frappe.new_doc("Facility Type")
	doc.facility_type_name = name
	doc.is_debt_purchase = 0
	doc.insert(ignore_permissions=True)
	return doc.name


def _new_facility(*, company: str, facility_type: str, name_suffix: str, principal: float, profit: float):
	settings = get_facility_settings_doc(company)
	bank = frappe.db.get_value("Bank", {}, "name", order_by="creation asc") or ensure_bank_master()
	cc = resolve_repayment_cost_center(facility=None, settings=settings)
	fac = frappe.new_doc("Facility")
	# Unique letter-heavy name; no shared 4+ digit identity tokens across runs.
	uniq = random_string(10)
	fac.facility_name = f"{TAG} {name_suffix} {uniq}"
	fac.company = company
	fac.facility_type = facility_type
	fac.bank = bank
	fac.contract_date = today()
	fac.receive_date = today()
	fac.maturity_date = add_days(today(), 90)
	fac.principal_amount = principal
	fac.profit_amount = profit
	fac.installment_count = 3
	fac.remarks = f"{TAG} | {name_suffix} | {uniq}"
	apply_facility_test_accounts(fac)
	if cc and not fac.cost_center:
		fac.cost_center = cc
	# Simple 3-row schedule (presentation totals only)
	p_share = round(principal / 3)
	pr_share = round(profit / 3)
	remain_p = principal
	remain_pr = profit
	for i in range(3):
		if i == 2:
			row_p, row_pr = remain_p, remain_pr
		else:
			row_p, row_pr = p_share, pr_share
			remain_p -= p_share
			remain_pr -= pr_share
		fac.append(
			"repayment_schedule",
			{
				"due_date": add_days(today(), 30 * (i + 1)),
				"principal_amount": row_p,
				"profit_amount": row_pr,
			},
		)
	return fac


def _mixin_pdcs(mixin: DebtPurchaseDbFixtureMixin, amounts: list[float]) -> list[str]:
	return [mixin._new_assigned_pdc(amount=a) for a in amounts]


def _cleanup_prior_ui_tests():
	"""Remove prior Draft UI-test Facilities (and their allocations) to allow re-runs."""
	prior = frappe.get_all(
		"Facility",
		filters={"facility_name": ["like", f"{TAG}%"], "status": "Draft"},
		pluck="name",
	)
	extra = frappe.get_all(
		"Facility",
		filters={"facility_name": ["like", "UI TEST%"], "status": "Draft"},
		pluck="name",
	)
	for name in sorted(set(prior + extra)):
		try:
			frappe.delete_doc("Facility", name, force=1, ignore_permissions=True)
		except Exception as e:
			print(f"SKIP_DELETE {name}: {e}")
	frappe.db.commit()


def run():
	frappe.set_user("Administrator")
	_cleanup_prior_ui_tests()
	company = _pick_company()
	dp_type = _ensure_dp_facility_type()
	normal_type = _ensure_normal_facility_type()

	# Reuse mixin for PDC creation helpers (needs TestCase for assert*).
	class _H(DebtPurchaseDbFixtureMixin, unittest.TestCase):
		pass

	_H.setUpClass()
	h = _H()
	h.company = company
	h.ft = dp_type
	h.customer = _H.customer
	h.bank_account = _H.bank_account
	h.settings = _H.settings
	h.dpic = _H.dpic

	results = []

	# --- Test 1: Draft DP with exact allocation (3 PDCs) ---
	principal, profit = 9_000_000, 900_000
	amounts = [3_300_000, 3_300_000, 3_300_000]  # sum = 9,900,000
	assert sum(amounts) == principal + profit
	fac1 = _new_facility(
		company=company,
		facility_type=dp_type,
		name_suffix="Draft Exact",
		principal=principal,
		profit=profit,
	)
	pdcs1 = _mixin_pdcs(h, amounts)
	for p in pdcs1:
		fac1.append("debt_purchase_pdcs", {"post_dated_cheque": p})
	fac1.insert(ignore_permissions=True)
	frappe.db.commit()
	results.append(
		{
			"test": 1,
			"name": fac1.name,
			"type": dp_type,
			"status": fac1.status,
			"pdc_count": len(fac1.debt_purchase_pdcs),
			"pdc_total": sum(amounts),
			"schedule_rows": len(fac1.repayment_schedule),
			"is_debt_purchase": int(fac1.is_debt_purchase or 0),
			"note": "Draft DP exact allocation",
		}
	)

	# --- Test 2: Active DP via receipt JE workflow ---
	principal2, profit2 = 6_000_000, 600_000
	amounts2 = [2_200_000, 2_200_000, 2_200_000]
	fac2 = _new_facility(
		company=company,
		facility_type=dp_type,
		name_suffix="Active Via Receipt",
		principal=principal2,
		profit=profit2,
	)
	pdcs2 = _mixin_pdcs(h, amounts2)
	for p in pdcs2:
		fac2.append("debt_purchase_pdcs", {"post_dated_cheque": p})
	fac2.insert(ignore_permissions=True)
	frappe.db.commit()
	try:
		create_receipt_journal_entry(fac2.name)
		frappe.db.commit()
		fac2.reload()
	except Exception as e:
		frappe.db.rollback()
		fac2.reload()
		# Fallback: opening-facility activation path if receipt JE blocked
		fac2.is_opening_facility = 1
		fac2.status = "Active"
		fac2.save(ignore_permissions=True)
		frappe.db.commit()
		fac2.reload()
		results.append({"test": 2, "fallback": str(e)[:200]})
	results.append(
		{
			"test": 2,
			"name": fac2.name,
			"type": dp_type,
			"status": fac2.status,
			"pdc_count": len(fac2.debt_purchase_pdcs or []),
			"pdc_total": sum(amounts2),
			"schedule_rows": len(fac2.repayment_schedule or []),
			"is_debt_purchase": int(fac2.is_debt_purchase or 0),
			"receipt_je": fac2.receipt_journal_entry,
			"note": "Active DP",
		}
	)

	# --- Test 3: Normal (non-DP) Facility ---
	fac3 = _new_facility(
		company=company,
		facility_type=normal_type,
		name_suffix="Normal Non-DP",
		principal=5_000_000,
		profit=500_000,
	)
	fac3.insert(ignore_permissions=True)
	frappe.db.commit()
	results.append(
		{
			"test": 3,
			"name": fac3.name,
			"type": normal_type,
			"status": fac3.status,
			"pdc_count": len(fac3.debt_purchase_pdcs or []),
			"pdc_total": 0,
			"schedule_rows": len(fac3.repayment_schedule or []),
			"is_debt_purchase": int(fac3.is_debt_purchase or 0),
			"note": "Normal non-DP; PDC table must be hidden",
		}
	)

	# --- Test 4: Incomplete Draft allocation ---
	principal4, profit4 = 8_000_000, 800_000
	amounts4 = [1_000_000, 1_000_000]  # sum 2M != 8.8M
	fac4 = _new_facility(
		company=company,
		facility_type=dp_type,
		name_suffix="Draft Incomplete",
		principal=principal4,
		profit=profit4,
	)
	pdcs4 = _mixin_pdcs(h, amounts4)
	for p in pdcs4:
		fac4.append("debt_purchase_pdcs", {"post_dated_cheque": p})
	fac4.insert(ignore_permissions=True)
	frappe.db.commit()
	activation_blocked = False
	activation_msg = ""
	try:
		fac4.status = "Active"
		fac4.is_opening_facility = 1
		fac4.save(ignore_permissions=True)
		frappe.db.commit()
	except Exception as e:
		activation_blocked = True
		activation_msg = str(e)
		frappe.db.rollback()
		fac4.reload()
	results.append(
		{
			"test": 4,
			"name": fac4.name,
			"type": dp_type,
			"status": fac4.status,
			"pdc_count": len(fac4.debt_purchase_pdcs or []),
			"pdc_total": sum(amounts4),
			"facility_total": principal4 + profit4,
			"schedule_rows": len(fac4.repayment_schedule or []),
			"is_debt_purchase": int(fac4.is_debt_purchase or 0),
			"activation_blocked": activation_blocked,
			"activation_msg": activation_msg[:300],
			"note": "Incomplete allocation; activation must be blocked",
		}
	)

	# --- Test 5: PDC uniqueness (reuse a PDC from Test 1) ---
	fac5 = _new_facility(
		company=company,
		facility_type=dp_type,
		name_suffix="Uniqueness Probe",
		principal=3_300_000,
		profit=0,
	)
	fac5.insert(ignore_permissions=True)
	frappe.db.commit()
	dup_rejected = False
	dup_msg = ""
	try:
		fac5.append("debt_purchase_pdcs", {"post_dated_cheque": pdcs1[0]})
		fac5.save(ignore_permissions=True)
		frappe.db.commit()
	except Exception as e:
		dup_rejected = True
		dup_msg = str(e)
		frappe.db.rollback()
		fac5.reload()
	# Keep fac5 as empty draft for UI inspection; uniqueness is verified server-side
	results.append(
		{
			"test": 5,
			"name": fac5.name,
			"type": dp_type,
			"status": fac5.status,
			"pdc_count": len(fac5.debt_purchase_pdcs or []),
			"pdc_total": 0,
			"schedule_rows": len(fac5.repayment_schedule or []),
			"is_debt_purchase": int(fac5.is_debt_purchase or 0),
			"duplicate_rejected": dup_rejected,
			"duplicate_msg": dup_msg[:300],
			"conflict_pdc": pdcs1[0],
			"conflict_facility": fac1.name,
			"note": "Uniqueness: allocating PDC already on Test1 must fail",
		}
	)

	print(json.dumps({"company": company, "dp_type": dp_type, "normal_type": normal_type, "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
	run()
