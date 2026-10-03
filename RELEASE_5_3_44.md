# Copyright (c) 2026, ERPNext Extensions contributors
"""v5.3.44 — Historical Repair worker/RIV settlement orchestration (Dev only).

- WORKER_PREFLIGHT fail-closed gate (Redis + long-queue listeners + probe)
- RIV_SETTLEMENT_BARRIER tracks campaign-scoped RIV names; never SQL-Skips
- Frozen plan + L6 + Clean A/B require workers READY and queue quiescence
- Prior Clean B A/B drift classified as INFRASTRUCTURE_ORCHESTRATION_DRIFT
  (invalid for proof); must re-run fresh Clean A/B after this fix
"""
