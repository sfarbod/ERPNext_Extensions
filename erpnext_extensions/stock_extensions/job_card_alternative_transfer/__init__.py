# Copyright (c) 2026, ERPNext Extensions contributors
# License: MIT

"""Job Card material fulfillment with original + substitute aggregation (v5.7.52).

Work Order already credits Stock Entry Detail rows by
``original_item or item_code``. Job Card completeness checks historically
compared each Job Card Item row's ``transferred_qty`` to ``required_qty``
only, so a substitute transfer linked to a separate supply row left the
original demand row looking short.

This package provides one authoritative calculation and patches the Job Card
validation / shortage / Move-Materials paths to use it.
"""
