# Copyright (c) 2026, ERPNext Extensions contributors
"""Controlled source valuation reconstruction using existing Iran Accounting APIs."""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt

from erpnext_extensions.iran_accounting.historical_rebuild.constants import (
	SOURCE_BLOCKED,
	SOURCE_DONE,
	SOURCE_FAILED,
	SOURCE_NOT_REQUIRED,
	SOURCE_REQUIRED,
)


def diagnose_source_needs(voucher_type: str, voucher_no: str) -> dict[str, Any]:
	"""Decide whether existing Iran source reconstruction is needed before Core RIV."""
	out = {
		"required": False,
		"status": SOURCE_NOT_REQUIRED,
		"detail": "",
		"gaps": [],
	}
	if voucher_type != "Stock Entry":
		return out
	if not frappe.db.exists("Stock Entry", voucher_no):
		out["status"] = SOURCE_BLOCKED
		out["detail"] = "Stock Entry missing"
		return out

	# Reuse valuation_rebuild gap diagnosis for this voucher.
	try:
		from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
			diagnose_sle_rate_gaps,
		)

		gaps = diagnose_sle_rate_gaps(voucher_no) or []
	except Exception as e:
		out["status"] = SOURCE_BLOCKED
		out["detail"] = f"diagnose failed: {e}"
		return out

	stale = [g for g in gaps if g.get("flags")]
	if not stale:
		# Also check SED amount vs |SLE SVD| for native-outgoing style drift.
		drift = _sed_sle_amount_drift(voucher_no)
		if drift:
			out["required"] = True
			out["status"] = SOURCE_REQUIRED
			out["detail"] = f"SED/SLE amount drift on {len(drift)} row(s)"
			out["gaps"] = drift
		return out

	out["required"] = True
	out["status"] = SOURCE_REQUIRED
	out["detail"] = f"stale SLE/SABB rate gaps={len(stale)}"
	out["gaps"] = stale[:20]
	return out


def _sed_sle_amount_drift(voucher_no: str) -> list[dict]:
	rows = frappe.db.sql(
		"""
		select sed.name as sed_name, sed.item_code, sed.amount,
		       coalesce(sum(abs(sle.stock_value_difference)), 0) as sle_abs
		from `tabStock Entry Detail` sed
		left join `tabStock Ledger Entry` sle
		  on sle.voucher_type='Stock Entry'
		 and sle.voucher_no=%s
		 and sle.voucher_detail_no=sed.name
		 and sle.is_cancelled=0
		where sed.parent=%s
		group by sed.name
		having abs(coalesce(sed.amount,0) - coalesce(sum(abs(sle.stock_value_difference)),0)) > 0.5
		""",
		(voucher_no, voucher_no),
		as_dict=True,
	)
	return rows or []


def apply_source_reconstruction(voucher_type: str, voucher_no: str) -> dict[str, Any]:
	"""Run existing Iran reconstruction paths. Does not invent formulas."""
	diag = diagnose_source_needs(voucher_type, voucher_no)
	if not diag.get("required"):
		return {"status": SOURCE_NOT_REQUIRED, "detail": diag.get("detail") or "no gaps"}

	if voucher_type != "Stock Entry":
		return {
			"status": SOURCE_BLOCKED,
			"detail": f"No Iran source rebuild path for {voucher_type}; Core RIV only",
		}

	actions: list[str] = []
	try:
		from erpnext_extensions.iran_accounting.historical_stock.valuation_rebuild import (
			sync_sle_from_stock_entry_detail,
			sync_sabb_from_sle,
			write_sle_transaction_rates,
		)

		# Align SLE rates from healthy SED when SED is authoritative; then SABB.
		# For native-outgoing, prefer SLE→SED via stock_entry_sync after Core RIV.
		# Here we heal stale rate fields using existing helpers before RIV walk.
		changed = write_sle_transaction_rates(voucher_no) or []
		actions.append(f"write_sle_transaction_rates={len(changed)}")
		sabb = sync_sabb_from_sle(voucher_no) or []
		actions.append(f"sync_sabb_from_sle={len(sabb)}")

		# Manufacture reconstructable roots via existing reconstruct API when purpose matches.
		purpose = frappe.db.get_value("Stock Entry", voucher_no, "purpose")
		if purpose == "Manufacture":
			try:
				from erpnext_extensions.iran_accounting.historical_stock.manufacture import (
					preview_manufacture_voucher,
				)

				prev = preview_manufacture_voucher(voucher_no) or {}
				if prev.get("status") == "RECONSTRUCTABLE" or prev.get("needs_repair"):
					from erpnext_extensions.iran_accounting.historical_stock.reconstruct import (
						repair_manufacture_selected,
					)

					repair_manufacture_selected([{"voucher": voucher_no}], dry_run=False)
					actions.append("repair_manufacture_selected")
			except Exception as e:
				# Do not invent; record and continue to Core RIV.
				actions.append(f"manufacture_skip:{e}")

		# Refresh native outgoing SED from live SLE if already posted.
		try:
			from erpnext_extensions.iran_accounting.domain.stock_entry_sync import (
				refresh_native_outgoing_sed_from_db,
			)

			doc = frappe.get_doc("Stock Entry", voucher_no)
			refresh_native_outgoing_sed_from_db(doc)
			for row in doc.get("items") or []:
				if row.get("name"):
					row.db_update()
			actions.append("refresh_native_outgoing_sed_from_db")
		except Exception as e:
			actions.append(f"native_sed_refresh_skip:{e}")

		frappe.db.commit()
		return {"status": SOURCE_DONE, "detail": "; ".join(actions), "actions": actions}
	except Exception as e:
		frappe.db.rollback()
		return {"status": SOURCE_FAILED, "detail": str(e), "actions": actions}
