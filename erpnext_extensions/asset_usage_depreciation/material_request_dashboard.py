# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Additive Material Request dashboard link back to Asset Request.

``custom_asset_request`` lives on Material Request and points to Asset Request.
That is an *internal* link on the MR document — not an external reverse query.

Do NOT map Asset Request via ``non_standard_fieldnames`` to
``custom_asset_request``: ``get_open_count`` would then run

	SELECT … FROM `tabAsset Request` WHERE `custom_asset_request` = <MR name>

which raises OperationalError 1054 because Asset Request has no such column.
"""

from __future__ import annotations

import frappe
from frappe import _


def get_data(data=None):
	data = frappe._dict(data or {})

	# Correct direction: read Material Request.custom_asset_request → Asset Request.
	if not data.get("internal_links"):
		data.internal_links = {}
	data.internal_links["Asset Request"] = "custom_asset_request"

	# Remove the invalid reverse mapping if an older override left it on ``data``.
	ns = data.get("non_standard_fieldnames")
	if ns and ns.get("Asset Request") == "custom_asset_request":
		ns.pop("Asset Request", None)

	if not data.get("transactions"):
		data.transactions = []

	label = _("Assets")
	for group in data.transactions:
		if _(group.get("label") or "") == label:
			items = group.setdefault("items", [])
			if "Asset Request" not in items:
				items.append("Asset Request")
			return data
	data.transactions.append({"label": label, "items": ["Asset Request"]})
	return data
