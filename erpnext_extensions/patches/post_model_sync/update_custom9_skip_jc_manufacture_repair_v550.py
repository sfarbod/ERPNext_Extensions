# Copyright (c) 2026, ERPNext Extensions contributors
"""Allow JC Manufacture Reconciliation to keep historical Manufacture posting.

Site Server Script "Custom 9 - Manufacture Posting After Transfers" bumps
Manufacture posting after the latest MTfM. Atomic historical repair must keep
the original Manufacture chronology, so skip when doc.flags.jc_manufacture_repair
is set.
"""

from __future__ import annotations

import frappe

SCRIPT_NAME = "Custom 9 - Manufacture Posting After Transfers"

NEW_SCRIPT = """
# Custom 9 | Stock Entry | Before Validate
# Keep the Manufacture strictly after the transfers it consumes.
# Skip when Stock Entry.flags.jc_manufacture_repair is set (JC rebuild 5.5.0).

skip_repair = False
try:
    skip_repair = bool(doc.flags.get("jc_manufacture_repair"))
except Exception:
    skip_repair = False

if skip_repair:
    pass
elif doc.purpose == "Manufacture" and doc.job_card:

    rows = frappe.db.sql('''
        select   se.name as name, se.posting_date as d, se.posting_time as t
        from     `tabStock Entry` se
        where    se.docstatus = 1
          and    se.job_card = %s
          and    se.purpose = 'Material Transfer for Manufacture'
        order by se.posting_date desc, se.posting_time desc, se.creation desc
        limit 1
    ''', (doc.job_card,), as_dict=True)

    if rows:
        latest = str(rows[0].d) + " " + str(rows[0].t)
        current = str(doc.posting_date) + " " + str(doc.posting_time or "00:00:00")

        if frappe.utils.get_datetime(current) <= frappe.utils.get_datetime(latest):
            moved = frappe.utils.add_to_date(latest, seconds=1, as_string=True)
            doc.set_posting_time = 1
            doc.posting_date = moved[0:10]
            doc.posting_time = moved[11:19]

            note = ("Posting moved to " + moved + " to follow material transfer "
                    + rows[0].name + " (Custom 9).")
            if doc.remarks:
                doc.remarks = doc.remarks + " | " + note
            else:
                doc.remarks = note
"""


def execute():
	if not frappe.db.exists("Server Script", SCRIPT_NAME):
		return
	doc = frappe.get_doc("Server Script", SCRIPT_NAME)
	if "jc_manufacture_repair" in (doc.script or ""):
		return
	doc.script = NEW_SCRIPT
	doc.save(ignore_permissions=True)
	frappe.clear_cache()
