# ERPNext Extensions v5.2.22

**Version:** `5.2.22`  
**Previous:** `5.2.21`  
**App:** `erpnext_extensions`  
**Focus:** Historical Stock Integrity & Repair — the I1 apply path becomes fail-closed.

v5.2.21 made `READY_I1` rows repairable. Applying one on a production tenant then exposed that the apply path could **commit a Stock-Entry-only half-repair**: it rewrote the Stock Entry rows, silently failed to touch the SLEs, never rebuilt GL, captured no snapshot, and reported success.

This release makes that outcome impossible.

---

## What went wrong

A single `READY_I1` voucher was applied. Afterwards:

| Layer | Result |
|---|---|
| Stock Entry rows | rewritten (FG restored to a positive rate) |
| Stock Ledger Entries | **untouched** — still poisoned |
| GL | **untouched** — still the pre-repair magnitude |
| Bin | matched the poisoned SLEs |

The Stock Entry and the stock ledger were left disagreeing, and the run reported
`sql_updates_executed: 13`, `negative_incoming_cleared: True`.

### Five defects, all fixed here

1. **The replay's refusal was ignored.** `replay_from_patient_zero` scans the identity and returns `{"ok": False, "status": "VALUATION_POISON_DEPENDENCY"}` *without writing* when it meets a hard poison. The apply appended that result to a list and never checked the flag.
2. **The wrong SLE writer was used.** `write_sle_transaction_rates` derives its rate from the SLE's *existing* `stock_value_difference`, so it can never carry a corrected Stock Entry amount onto the ledger. It wrote nothing.
3. **No snapshots.** `rollback_run` reported `full_rollback_possible: False, count: 0` — the repair could not be undone from the audit log.
4. **No GL rebuild.** `affected_gl` and the "Selective GL" preview chain promised a rebuild the apply never performed.
5. **The post-write check tested the wrong column.** It asserted `incoming_rate >= 0` and passed, while the finished good still carried `valuation_rate = -5,824,757,089`. A false pass is worse than no check.

---

## What's New

### Fail-closed apply

- `_replay_blockers(item, warehouse, from_dt)` evaluates the replay's own refusal predicate **before any write**. `apply_i1_voucher` re-runs it immediately before writing, since the scan that produced the row may be stale, and raises rather than writing a partial repair.
- `_write_inbound_sle_from_se` carries corrected amounts onto the inbound SLE legs, matched **1:1 through `voucher_detail_no`**. An item that is both issued and returned in the same voucher keeps its outgoing leg untouched.
- `valuation_rate` is deliberately **not** stamped from the row rate — it is the warehouse moving average after the movement, and belongs to the replay.
- The replay's `ok` flag is honoured; a refusal aborts the voucher.
- GL is rebuilt **last**, via `rebuild_gl_for_voucher`, and a refusal aborts.
- `capture_identity_snapshot` runs before each voucher and is stored as `snapshot_before`, so `rollback_run` works.

### Ordering matters

ERPNext builds the Stock Entry GL map from **SLE `stock_value_difference`**, not from the Stock Entry header. Rebuilding GL before the SLE legs are corrected would post the poisoned magnitude. Two endpoints disagreed on the same voucher for exactly this reason:

| Source | Expected GL |
|---|---|
| `classify_stock_entry_gl` (SE header) | 1,537,220,904 |
| `rebuild_gl_for_voucher` (`se.get_gl_entries`, SLE-derived) | 4,148,765,917,806 |

The apply now writes SLE first, then rebuilds GL.

### Classification reflects repairability

`classify_i1_voucher` now downgrades `READY_I1` → `WAITING_I1` when any touched identity cannot replay, carrying `replay_blockers` and naming the blocking voucher. A solvable pool is not sufficient — the chain must also be rewritable. **This alone would have prevented the incident**, since the voucher in question has identities poisoned by other vouchers.

The blocker check runs only for otherwise-READY candidates, so scan cost is unchanged for `WAITING`/`MANUAL` rows.

### Verification across every layer

`_verify_repair` replaces `_verify_no_negative_incoming` and checks `incoming_rate`, **`valuation_rate`**, inbound SVD sign, Bin against the last SLE for every touched identity, and GL balance. Any failure raises and rolls back the savepoint.

---

## Expected effect

`READY_I1` will **drop** on tenants whose identities are cross-poisoned — that is the point. Vouchers that were "ready" but unwritable are now honestly `WAITING_I1` with a named blocker, instead of being applied into a half state.

---

## Testing

New `test_i1_apply_hardening_v5222.py`:

- replay blockers detected for hard poisons; healthy chains produce none
- apply refuses a `WAITING_I1` voucher
- apply refuses when pre-flight finds a blocker, asserting **`set_value` is never called**
- `_verify_repair` catches a negative `valuation_rate` while `incoming_rate` is positive — the exact false pass
- negative inbound SVD and unbalanced GL are caught; a clean voucher passes

```bash
bench --site <site> run-tests --app erpnext_extensions \
  --module erpnext_extensions.iran_accounting.tests.test_i1_apply_hardening_v5222
```

---

## Known Limitations

The apply path is **still not production-proven**; `promotion_status` remains `NOT_PROVEN`. Every defect fixed in 5.2.21 and 5.2.22 was found by running against real data, not by the test suite. Run the tests and prove on a restored copy before production use.

A voucher left half-repaired by 5.2.20/5.2.21 is **not** healed by upgrading. Its Stock Entry is corrected while its SLE/GL/Bin are not; it needs separate remediation.

---

## Upgrade Notes

Pure Python — no JS, no schema, no patches.

```bash
cd ~/frappe-bench/apps/erpnext_extensions && git pull
cd ~/frappe-bench && bench restart
```

---

## Version metadata

| Location | Value |
|----------|-------|
| `erpnext_extensions/__init__.py` | `__version__ = "5.2.22"` |
| Master Repair Plan / Campaign Wizard / Assisted Master Plan payloads | `"version": "5.2.22"` |

---

## Publish checklist (operator)

- [ ] Working tree clean after this preparation commit  
- [ ] Tests run on a restored copy  
- [ ] Apply proven on a restored copy before production use  
- [ ] Push / tag / publish only after explicit approval  

**This document is release preparation only. Do not push, tag, or publish until the operator authorizes it.**
