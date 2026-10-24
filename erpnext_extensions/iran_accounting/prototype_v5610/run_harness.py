# Copyright (c) 2026 — superseded by e2e_v5610_physical_two_slice
"""Legacy prototype harness. Prefer:

bench --site development.localhost execute \\
  erpnext_extensions.iran_accounting.e2e_v5610_physical_two_slice.run_all
"""

from erpnext_extensions.iran_accounting.e2e_v5610_physical_two_slice import run_all

__all__ = ["run_all"]
