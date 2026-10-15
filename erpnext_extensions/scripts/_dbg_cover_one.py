import frappe
from erpnext_extensions.iran_accounting.historical_rebuild.processor import (
	find_covering_riv,
	verify_voucher_basic,
)


def run(campaign: str = "HRC-00001"):
	row = frappe.db.sql(
		"""
		select name, voucher_type, voucher_no, ordinal from `tabHistorical Rebuild Campaign Item`
		where campaign=%s and item_status='Pending' order by ordinal limit 1
		""",
		campaign,
		as_dict=True,
	)[0]
	ver = verify_voucher_basic(row.voucher_type, row.voucher_no)
	sles = frappe.db.sql(
		"""
		select item_code, warehouse, posting_date, posting_time from `tabStock Ledger Entry`
		where is_cancelled=0 and voucher_type=%s and voucher_no=%s
		limit 5
		""",
		(row.voucher_type, row.voucher_no),
		as_dict=True,
	)
	covers = []
	for s in sles:
		cov = find_covering_riv(s.item_code, s.warehouse, s.posting_date, s.posting_time)
		earliest_riv = frappe.db.sql(
			"""
			select name, status, posting_date, posting_time from `tabRepost Item Valuation`
			where docstatus=1 and based_on='Item and Warehouse'
			  and item_code=%s and warehouse=%s and status='Completed'
			order by posting_date asc, posting_time asc limit 1
			""",
			(s.item_code, s.warehouse),
			as_dict=True,
		)
		covers.append(
			{
				"item": s.item_code,
				"date": str(s.posting_date),
				"time": str(s.posting_time),
				"cover": cov,
				"earliest_completed": earliest_riv[0] if earliest_riv else None,
			}
		)
	return {"row": dict(row), "verify": ver, "sle_n": len(sles), "covers": covers}
