# RELEASE 5.3.11 — Safe out-of-scope SLE field restore

## Bug
Soft-skip correctly returned False for unrelated FG I3 during 28696 RIV, but
`restore_out_of_scope_sle_ledger_state` called `sle.set(...)` on a ledger row
dict where `.set` is None → `TypeError: 'NoneType' object is not callable`,
which ERPNext surfaced as RIV Failed (masking the soft-skip success).

## Fix
Assign via type-level Document.set when callable; otherwise `sle[field]=val`.
