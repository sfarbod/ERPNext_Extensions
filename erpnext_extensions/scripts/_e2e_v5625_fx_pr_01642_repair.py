"""Repair + RIV gate for MAT-PRE-2026-01642 (FX rate-first 10,200 residual).

Mutates development.localhost only. Does not touch production or resume HRC-00006.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import flt


PR = "MAT-PRE-2026-01642"
ITEM = "13100023"
EXPECTED = 57748156800.0
GAP = 10200.0


def _sle_svd(pr_name: str) -> float:
	row = frappe.db.sql(
		"""select stock_value_difference, incoming_rate from `tabStock Ledger Entry`
		   where voucher_type='Purchase Receipt' and voucher_no=%s and is_cancelled=0
		   limit 1""",
		pr_name,
		as_dict=True,
	)
	return row[0] if row else None


def _gl(pr_name: str) -> list[dict]:
	return frappe.db.sql(
		"""select account, debit, credit, is_cancelled, remarks
		   from `tabGL Entry`
		   where voucher_type='Purchase Receipt' and voucher_no=%s and is_cancelled=0
		   order by account""",
		pr_name,
		as_dict=True,
	)


def _gl_balanced(rows) -> bool:
	d = sum(flt(r.debit) for r in rows)
	c = sum(flt(r.credit) for r in rows)
	return abs(d - c) <= 1


def _grni_net(company: str, pr_name: str) -> float:
	acc = frappe.db.get_value("Company", company, "stock_received_but_not_billed")
	return flt(
		frappe.db.sql(
			"""select coalesce(sum(debit)-sum(credit),0) from `tabGL Entry`
			   where voucher_type='Purchase Receipt' and voucher_no=%s
			     and account=%s and is_cancelled=0""",
			(pr_name, acc),
		)[0][0]
	)


def _classify_decision(pr_name: str) -> dict:
	from erpnext_extensions.iran_accounting.domain.irr_residual_classification import (
		classify_document_residuals,
		evaluate_irr_rate_rounding_residual,
	)

	pr = frappe.get_doc("Purchase Receipt", pr_name)
	a, b = classify_document_residuals(pr)
	# Synthetic Core-like GL after amount-preserving SLE
	stock = "111620 - موجودی لوازم بسته بندی - E"
	grni = frappe.db.get_value("Company", pr.company, "stock_received_but_not_billed")
	gl = [
		frappe._dict(account=stock, debit=EXPECTED, credit=0),
		frappe._dict(account=grni, debit=0, credit=EXPECTED),
	]
	dec = evaluate_irr_rate_rounding_residual(pr, gl_entries=gl)
	return {
		"class_a": len(a),
		"class_b": len(b),
		"status": dec.status,
		"net_signed_debit": dec.net_signed_debit,
		"messages": dec.messages,
	}


def run() -> dict[str, Any]:
	frappe.set_user("Administrator")
	from erpnext_extensions.iran_accounting.integration.bootstrap import apply as apply_iran

	apply_iran()
	if not frappe.db.exists("Purchase Receipt", PR):
		return {"error": "PR missing", "pass": False}

	company = frappe.db.get_value("Purchase Receipt", PR, "company")
	before_sle = _sle_svd(PR)
	before_gl = _gl(PR)
	before_grni = _grni_net(company, PR)
	before_decision = _classify_decision(PR)

	from erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_detection import (
		dry_run_purchase_valuation_scan,
	)
	from erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_repair import (
		assess_repair,
		repair_one,
	)

	scan = dry_run_purchase_valuation_scan(company, purchase_receipts=[PR], limit=5)
	findings = [f for f in scan.get("findings") or [] if f.get("voucher_no") == PR]
	assess = assess_repair(PR, company=company)

	# Reproduce pre-fix partner failure was Class A; post-fix should bypass
	# Execute repair (adjust VR + RIV)
	rep = repair_one(PR, execute=True, company=company)
	after_sle = _sle_svd(PR)
	after_gl = _gl(PR)
	after_grni = _grni_net(company, PR)
	after_decision = _classify_decision(PR)
	vr = flt(
		frappe.db.get_value(
			"Purchase Receipt Item", {"parent": PR, "item_code": ITEM}, "valuation_rate"
		)
	)

	# Idempotent second repair
	rep2 = repair_one(PR, execute=True, company=company)

	# Explicit Item+Warehouse RIV again for idempotency of RIV itself
	from erpnext_extensions.iran_accounting.historical_rebuild.purchase_valuation_repair import (
		_create_and_run_riv,
	)

	pr_doc = frappe.get_doc("Purchase Receipt", PR)
	wh = pr_doc.items[0].warehouse
	posting_date = pr_doc.posting_date
	posting_time = pr_doc.posting_time
	riv_again = _create_and_run_riv(ITEM, wh, company, posting_date, posting_time)
	sle_idem = _sle_svd(PR)
	gl_idem = _gl(PR)

	sa_rows = [r for r in after_gl if "تعدیلات" in (r.account or "") or "Stock Adjustment" in (r.account or "")]
	out = {
		"pr": PR,
		"before": {
			"sle": before_sle,
			"gl": before_gl,
			"grni_net": before_grni,
			"decision": before_decision,
			"overvaluation": flt(before_sle.stock_value_difference) - EXPECTED if before_sle else None,
		},
		"detection_findings": findings,
		"assessment": {
			"eligible": assess.get("eligible"),
			"riv_alone": assess.get("riv_alone_sufficient"),
			"action": assess.get("recommended_action"),
		},
		"repair": rep,
		"after": {
			"sle": after_sle,
			"gl": after_gl,
			"gl_balanced": _gl_balanced(after_gl),
			"grni_net": after_grni,
			"decision": after_decision,
			"valuation_rate": vr,
			"stock_adjustment_rows": sa_rows,
		},
		"idempotent_repair": rep2.get("status"),
		"riv_again": riv_again,
		"sle_after_second_riv": sle_idem,
		"gl_count_after": len(after_gl),
		"gl_count_idem": len(gl_idem),
		"pass": (
			# Before may already be repaired if re-run; accept either state.
			before_sle is not None
			and after_sle
			and abs(flt(after_sle.stock_value_difference) - EXPECTED) <= 1
			and abs(flt(after_sle.incoming_rate) * 51000 - EXPECTED) <= 1
			and _gl_balanced(after_gl)
			and after_decision.get("status") == "bypass"
			and abs(flt(after_decision.get("net_signed_debit"))) <= 1
			and rep.get("status") in ("repaired", "idempotent_skip")
			and rep2.get("status") in ("repaired", "idempotent_skip")
			and riv_again.get("status") == "Completed"
			and abs(flt(sle_idem.stock_value_difference) - EXPECTED) <= 1
			and len(gl_idem) == len(after_gl)
			and not sa_rows  # no residual SA needed after amount-preserving
			and abs(flt(after_grni) + EXPECTED) <= 1  # unbilled GRNI credit = auth
		),
	}
	print(json.dumps(out, indent=2, default=str))
	return out


if __name__ == "__main__":
	run()
