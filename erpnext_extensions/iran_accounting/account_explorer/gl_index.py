# Copyright (c) 2026, Farbod Siyahpoosh and contributors
"""GL Entry index helpers for Account Explorer company-wide aggregates."""

from __future__ import annotations

import frappe
from frappe.utils.caching import request_cache

# Covering range index for company-wide Account Levels / opening aggregates.
# Created by patch ae_gl_company_range_index_v5621.
AE_GL_COMPANY_RANGE_INDEX = "ae_gl_co_iscancel_posting_acct"
_FALLBACK_INDEX = "company"


@request_cache
def resolve_gl_company_range_index() -> str:
	"""Return the best FORCE INDEX name available on this site."""
	rows = frappe.db.sql(
		"""
		SELECT 1
		FROM information_schema.statistics
		WHERE table_schema = DATABASE()
			AND table_name = 'tabGL Entry'
			AND index_name = %s
		LIMIT 1
		""",
		(AE_GL_COMPANY_RANGE_INDEX,),
	)
	return AE_GL_COMPANY_RANGE_INDEX if rows else _FALLBACK_INDEX


def force_gl_company_range_index(query):
	"""Apply the measured company-range covering index when present."""
	return query.force_index(resolve_gl_company_range_index())
