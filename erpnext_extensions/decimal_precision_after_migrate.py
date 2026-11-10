# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT
"""Central DECIMAL(30,9) after_migrate coordinator.

Runs selling, stock-repost, purchasing/asset, facility, and manufacturing
healers so migrate never succeeds while registered monetary columns have
drifted back to DECIMAL(21,9) (or other under-capacity types).
"""

from __future__ import annotations

import frappe


def after_migrate() -> None:
	logger = frappe.logger("erpnext_extensions.decimal_precision_after_migrate")
	logger.info("Starting decimal precision after_migrate coordinator")

	from erpnext_extensions.selling_documents_decimal_precision_v507 import (
		repair_selling_documents_decimal_schema,
	)
	from erpnext_extensions.stock_repost_decimal_precision_v519 import (
		repair_stock_repost_decimal_schema,
	)
	from erpnext_extensions.purchasing_decimal_precision_v524 import (
		repair_purchasing_decimal_schema,
	)
	from erpnext_extensions.facility_management.facility_precision import (
		repair_facility_decimal_schema,
	)
	from erpnext_extensions.manufacturing_decimal_precision_v5721 import (
		repair_manufacturing_decimal_schema,
	)

	selling = repair_selling_documents_decimal_schema(run_completeness_guard=False)
	stock = repair_stock_repost_decimal_schema(run_completeness_guard=False)
	purchasing = repair_purchasing_decimal_schema(run_completeness_guard=False)
	facility = repair_facility_decimal_schema()
	manufacturing = repair_manufacturing_decimal_schema(run_completeness_guard=False)
	logger.info(
		"Decimal precision after_migrate done: selling_repaired=%s stock_repaired=%s "
		"purchasing_repaired=%s facility_repaired=%s manufacturing_repaired=%s",
		len(selling.get("repaired") or []),
		len(stock.get("repaired") or []),
		len(purchasing.get("repaired") or []),
		len(facility.get("repaired") or []),
		len(manufacturing.get("repaired") or []),
	)
