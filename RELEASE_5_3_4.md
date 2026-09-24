# Release 5.3.4 — Zero Valuation Provenance + Leftover-MA Repair

Package version: **5.3.4**.

Zero rate is not itself corruption. This release answers:

> Is the **current** stock economically zero-valued based on its complete
> valuation provenance?

It adds two complementary paths:

1. **Historical Repair** for leftover Moving Average after an authorized
   zero-value inbound on still-valued stock.
2. A provenance-aware `lost_outgoing_rate_zero` runtime guard that no longer
   treats a fully depleted older lot as proof that the current lot must carry
   that old rate.

Production is not part of this cut.

---

## Provenance classes

| Class | Meaning | Outgoing zero | Auto-repair |
|---|---|---|---|
| `ZP_PROVEN_LEGITIMATE_ZERO` | Current qty > 0 and stock value = 0 because stock arrived only from legitimate zero inbound after the previous valued position was fully depleted (`qty → 0` and `value → 0`) | Allowed | No |
| `ZP_VALUED_STOCK_ZERO_STAMP` | qty > 0 and stock value > 0 but SLE valuation / outgoing rate is zero | Blocked | Leftover-MA if the contract is proven |
| `ZP_ZERO_INBOUND_ON_VALUED_POSITION` | Zero inbound while previous qty > 0 and previous stock value > 0. Receipt value stays 0; warehouse MA must remain `value / qty` | Blocked until MA is reconstructed | Leftover-MA if proven |
| `ZP_DEPLETED_OLD_VALUE_HISTORY` | Secondary marker: an older nonzero lot existed and was fully depleted before the current zero lot | Not evidence against the new lot | No |
| `ZP_UNPROVEN_ZERO` | Opening-state, incomplete, or otherwise ambiguous | Blocked | MANUAL |

An older depleted rate such as `241,875,000` is **never** copied onto a later
zero-valued lot.

`allow_zero_valuation_rate` on an **outgoing** row is not a bypass.

---

## Leftover-MA repair contract

Class: `LEFTOVER_MA_REPAIR` / `READY_LEFTOVER_MA`.

A candidate is READY only when all of the following are proven:

- quantity history is deterministic
- stock value immediately before the zero inbound is known
- the zero inbound is document-authorized (`allow_zero_valuation_rate`)
- no unknown valuation-affecting gap interrupts the chain
- downstream outgoing valuation can be reconstructed deterministically
- replay creates no historical negative qty, I1, I4, or negative Bin
- the zero inbound itself is **not** given an invented rate
  (`incoming_rate = 0`, `SVD = 0`, leftover value unchanged)
- warehouse MA after the inbound is `previous_stock_value / (previous_qty + inbound_qty)`
- second apply is idempotent (`economic_writes = 0`)

Otherwise: `MANUAL_LEFTOVER_MA`. Opening-state / unauthorized zero inbound
never auto-repairs.

Generic `replay_series` must not be used for this class: it would invent
receipt value by applying leftover MA as the incoming rate.

---

## Runtime guard

**Old:** `lost_outgoing_rate_zero` treated any historical nonzero rate,
including a fully depleted lot, as evidence that current outgoing zero is
corruption.

**New:** before blocking, classify current provenance.

- Allow when the current lot is `ZP_PROVEN_LEGITIMATE_ZERO`.
- Block when qty > 0 and stock value > 0 but outgoing / effective rate is 0.
- If history cannot prove legitimate zero, keep the safety block.

---

## Development canaries (restored baseline)

Site: `development.localhost` only.
Backup: `20260923_162203-erp_espadpharmed_com-database.sql.gz`.

### 16100226 — legitimate zero after depletion

Old valued lot `8 @ 241,875,000` reached `qty = 0` / `value = 0` on
`MAT-STE-2026-24101`. `MAT-STE-2026-14493` then received `+2 @ 0` onto an
empty warehouse. No leftover-MA repair. Guard allows outgoing zero.

### 16100066 — leftover-MA poison

Before `14493`: `qty = 522`, `stock_value = 2,647,827,252`, MA `5,072,466`.
Receipt `+7 @ 0` (authorized). Value correctly stayed `2,647,827,252`, but
valuation rate was stamped 0. Repair reconstructed MA
`2,647,827,252 / 529 ≈ 5,005,344.52` and replayed downstream issues.
Receipt incoming value remained 0.

### MAT-STE-2026-37599

Submitted normally after the two canaries. `16100226` issued at 0.
`16100066` issued at the reconstructed MA. GL balanced. `allow_zero` was
not set on 37599.

Database-wide leftover-MA: 50 candidates, 7 READY+EXACT applied
(1 canary + 6 scrap/reject), 43 MANUAL (unauthorized zero inbound).
Safety gates did not increase.

---

## Tests

`erpnext_extensions.iran_accounting.tests.test_zero_valuation_provenance_v534`

Covers depleted-then-free receipt, leftover-MA replay, old-rate-not-evidence,
qty+value+zero-stamp block, opening-state MANUAL, idempotency, safety flags,
planner READY/AMBIGUOUS, stamp eligibility, outgoing `allow_zero` not
bypassing a valued lot, honest worker-queue detection (empty queues / fresh
heartbeat is not available), and the RIV hook ignoring non-Completed docs.

v5.3.3 Historical Repair regressions remain green.

---

## Deployment

| Step | Required |
|------|----------|
| `bench migrate` | No new schema |
| Patch / schema | No |
| Asset build | No |
| Cache clear | Recommended |
| Restart | Recommended |

Development / test only in this cut. **Do not deploy to Production** without
separate explicit approval.

`erpnext_extensions.__version__` = **5.3.4**

---

## Corrective addendum — actual Stock Ledger postcondition

A leftover-MA repair is **not** Completed unless the same `tabStock Ledger Entry`
fields the standard ERPNext Stock Ledger report reads satisfy:

- Incoming Rate of the authorized zero inbound remains `0`
- Balance stock value of that inbound is unchanged
- **Avg Rate (`valuation_rate`) is the leftover MA**, not zero
- downstream Outgoing Rate (`stock_value_difference / actual_qty`) is nonzero
  while leftover value remains
- Bin matches the last SLE

If expected MA is nonzero but that report source still shows zero, status is
`FAILED_POSTCONDITION`. Persist prefers ERPNext `update_entries_after`
(Moving Average replay from the zero inbound forward).

A Completed leftover-MA repair also requires:

- Desk `query_report.run(..., ignore_prepared_report=True)` Avg Rate matches
  direct `stock_ledger.execute` (Stock Ledger is a Prepared Report)
- any created RIV is `Completed`, not `Queued` / `Failed`
- **wait for that RIV to finish** before marking the repair Completed
- `valuation_rate` on the zero inbound SLE is leftover value / qty even if
  vanilla RIV leaves that field at 0 while `stock_value` stays leftover
- after a later official RIV, the same leftover MA is restored (hook
  `on_repost_item_valuation_update`) and only **downstream** SLEs are replayed
  so the zero inbound is not given an invented incoming rate

Mismatch is `FAILED_POSTCONDITION: REPORT_LEDGER_MISMATCH`. A failed official
RIV is `FAILED_RIV`. Matching Completed Stock Ledger Prepared Reports are
deleted after a successful persist so Desk cannot serve a pre-repair snapshot.

A heartbeat alone is not a listener. `WORKER_UNAVAILABLE` stays visible if
the `long` queue cannot consume jobs (empty subscription, state `?`, stale
heartbeat, and no recent dequeue). Scan All / Stock Ledger Generate / RIV
must go `Queued → Started → Completed` on a real worker. Completing a job
in-process is not acceptance.

Development `bench start` Procfile must run:

- `web` (`bench serve`)
- `schedule`
- `worker` (`short,default,long`)
- `worker_long` (`long`)

so a normal Development restart keeps a long-queue listener.
