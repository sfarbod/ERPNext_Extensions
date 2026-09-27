# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Doc events for Asset Depreciation Schedule — Iran whole-number gate."""

from __future__ import annotations

from erpnext_extensions.asset_usage_depreciation.services.ads_amount_normalize import normalize_ads_document


def before_validate(doc, method=None):
	"""Last monetary persistence gate before ADS validate/submit."""
	if getattr(doc, "flags", None) and doc.flags.get("skip_iran_ads_normalize"):
		return
	# Draft generation and before_submit share validate; normalize whenever schedule exists.
	if cint_docstatus(doc) > 1:
		return
	normalize_ads_document(doc)


def before_submit(doc, method=None):
	if getattr(doc, "flags", None) and doc.flags.get("skip_iran_ads_normalize"):
		return
	normalize_ads_document(doc)


def cint_docstatus(doc) -> int:
	try:
		return int(doc.docstatus or 0)
	except (TypeError, ValueError):
		return 0
