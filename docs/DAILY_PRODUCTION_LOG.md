# Daily Production Log — Workflow (v4.7.2 – v4.8.1)

**Module:** `erpnext_extensions/daily_production/` · **DocType:** `Daily Production Log` (`DPL-YYYY-#####`)
**Branch:** `feature/daily-production-log` · **Target:** ERPNext v16 with *Track Semi Finished Goods* Work Orders

## Summary

One **Daily Production Log = one production cycle of one operation**: the operator records
*Work Order + operation + qty + operator + from/to time* and presses **Run**. The runner then
does, in a single transaction, everything an operator otherwise clicks through by hand on the
desk for the v16 semi-finished-goods chain:

```
validate → Job Card (reuse / create) → Material Transfer for Manufacture → time log(s)
        → Complete Job (cycle qty = produced qty, pending 0) → submit card → Manufacture entry
```

Every step calls the **same ERPNext server function the desk button calls** — nothing
manufacturing-related is re-implemented. Any exception rolls the whole run back and leaves the
log in status **Failed** with the traceback; nothing half-posted is left behind.

Rule the whole design enforces (learned from the semi-finished-goods test): **Qty to
Manufacture in this Cycle == Completed Qty, Pending Qty == 0.** A Pending Qty left on a card
locks the remainder of the operation (core `validate_job_card_qty` ignores it) and the only
recovery is cancelling the Stock Entry, the transfer and the card.

## Files

| Path | Role |
| --- | --- |
| `daily_production/runner.py` | `run(name)` — the whole cycle; read `_execute` top to bottom |
| `daily_production/doctype/daily_production_log/` | DocType JSON, controller (input rules, immutability), form JS (filters, Run button) |
| `daily_production/doctype/daily_production_log_document/` | Child table: audit trail of created / reused documents |
| `daily_production/job_card_hooks.py` | Job Card `after_insert` / `validate` guards (apply to **all** cards, not only log-driven ones) |
| `daily_production/e2e/run_test_plan.py` | REST-driven end-to-end test plan against staging |
| `patches/pre_model_sync/add_daily_production_module_def.py` | v4.7.3 — registers the *Daily Production* Module Def before DocType sync |
| `patches/post_model_sync/add_daily_production_custom_fields.py` | v4.7.2 — `Batch.custom_is_placeholder_lot` |
| `hooks.py` → `doc_events["Job Card"]` | wires the two Job Card guards |

## 1. Operator workflow (Desk)

1. **New Daily Production Log.** Pick the **Work Order** — the picker only offers submitted
   Work Orders that are not *Completed / Stopped / Closed / Cancelled*.
2. Pick the **Operation** — filtered to the operations of that Work Order. The form binds the
   log to the Work Order **operation row** (`operation_row_id` = row `idx`), not just the
   operation name, because that is what *Track Semi Finished Goods* uses to attach materials
   to Job Cards. If the same operation appears twice on a Work Order, `Operation Row ID` must
   be set explicitly.
3. Enter **Qty**, **Operator** (Employee), **From** and **To**.
   *From* defaults to the operator's last *Done* cycle's *To* (the usual "next shift" case).
4. **Department / Cost Center** are fetched from the Employee (`department`,
   `payroll_cost_center`) and filled in on save if left empty. Department is mandatory on every
   Stock Entry row on this site, so the runner stamps them on every transfer/manufacture row
   that has none.
5. **Output Batch** (optional) — filtered to the operation's Finished Good item. If empty and
   the Finished Good is batch-tracked, the runner generates a **placeholder lot** (§ 2.7).
6. **Save → Run.** The primary **Run** button is shown on saved logs that are not *Done* or
   *Running*; a dirty form is saved first. The desk freezes with *"Running production cycle…"*
   and reloads when the call returns.
7. Result: status **Done** with links to the Job Card, Material Transfer and Manufacture Entry
   plus the *Created Documents* table — or **Failed** with the traceback in *Error Log* and
   the last line shown as a red headline on the form.

### Status lifecycle

```
Draft ──Run──▶ Running ──ok──▶ Done       (immutable; delete blocked)
                  │
                  └──error──▶ Failed ──edit inputs, Run again──▶ Running …
```

- **Done** logs cannot be edited by hand (`_block_edit_after_done`; only the runner updates them
  via `flags.from_runner`) and cannot be deleted once they link any document.
- **Failed** logs keep their inputs; fix the input (qty, operator, times…) and press **Run**.
- **Running** logs are refused by a second `run()`; a *Done* log is a no-op (§ 2.9).

## 2. What **Run** does (server side, one transaction)

`erpnext_extensions.daily_production.runner.run(name)` — whitelisted, requires *write* on the
log. Steps below mirror the numbered blocks in `_execute`.

### 2.1 Claim the log

`status` is read `for_update` (row lock). *Done* → return no-op; *Running* → throw; otherwise the
log is set to **Running** and committed, so no two requests can both start it. Then a file
lock `daily_production_<work_order>_<row>` (timeout 2 s) serialises runs of the same
Work Order + operation; a second caller fails with *"Another Daily Production Log for Work
Order … is running right now."*

### 2.2 Pre-flight validation — nothing is created if any of these fail

| # | Check | Error |
| --- | --- | --- |
| 1 | Work Order is submitted and not Completed/Stopped/Closed/Cancelled | *Work Order X is …; nothing can be produced against it* |
| 1 | Work Order has **Track Semi Finished Goods** on | *does not track semi finished goods* |
| 1 | The operation row exists and has a **Finished Good** | *Operation … has no Finished Good* |
| 2 | `qty > 0`, `to_time > from_time`, Employee is *Active* | |
| 3 | **Pending qty of the operation ≥ qty** where `pending = WO qty − completed − process loss − qty claimed by other open draft cards of this operation` | *Only P of Q is still pending for operation …* |
| 4 | **Previous operation's output is available** in this operation's source warehouse — for batch/serial-tracked items only lots **this Work Order** produced count, even when that is zero (`get_previous_operation_output_sn_batch`); plain `Bin.actual_qty` for untracked items | *Only A of ITEM (output of the previous operation) is available in WH; this cycle needs N* |
| 5 | The operator has **no running timer** (a `time_logs` row without *To*) on any draft Job Card. Rows of the card's *Employee* multi-select live in the same child table and are ignored (v4.8.1) | *Employee … still has a running timer on Job Card …* |
| 6 | No other log for the same Work Order + operation row is *Running* | |

Check 4 enforces the "downstream card qty" rule up front instead of failing inside the
transfer mapping.

### 2.3 Job Card — reuse or create

- `_find_cards` lists draft, non-corrective Job Cards of this operation row (oldest first).
  A card **nobody touched** — no time logs, `transferred_qty = 0`, no Stock Entry against it —
  is **reused**; every other draft card counts as *claimed* quantity in check 3.
- Reused card with a different `for_quantity` is rescaled exactly as editing *Qty To
  Manufacture* on the desk does: items cleared and rebuilt via `get_required_items()`.
  Audit action: `reused (qty set to N)`.
- Otherwise `make_job_card` (the Work Order → *Create Job Card* dialog, items scaled to qty)
  creates one card. If the call produces more than one card the qty exceeds the operation's
  **batch size** → the run fails with *Split the cycle*.
- Every card gets `semi_fg_bom = WO.bom_no` (if empty) and `custom_batch_number =
  WO.custom_fg_batch_no` (if the field exists and is empty).

### 2.4 Material Transfer for Manufacture (skipped when the operation has *Skip Material Transfer*)

`make_stock_entry` from the Job Card (the same mapper as the desk button), `fg_completed_qty =
qty`, then every row is rebuilt:

- `naming_series` is reset so the Stock Entry uses its own series, not the Job Card's.
- Rows that already carry a batch / serial-and-batch bundle (previous-operation output —
  lots this Work Order produced) are kept as they are.
- A **semi-finished** input row *without* a lot is a hard failure: the runner never picks
  another Work Order's lot of a semi-finished good by FEFO.
- For every other material, `_pick_source` tries the **card's source warehouse first**, then
  the **Work Order item's (BOM) source warehouse** — packaging materials live in a different
  store than the formulation chemicals, while core `get_required_items` stamps the operation's
  warehouse on every row (v4.7.4 fix). The first warehouse that can cover the full row qty wins;
  otherwise the run fails naming both warehouses and what each holds.
- **Batch-tracked** materials are split into one row per batch, **earliest expiry first**
  (`use_serial_batch_fields = 1`, explicit `batch_no`). Untracked materials use `Bin.actual_qty`.
- `department` / `cost_center` default from the log.

The entry is inserted and submitted.

### 2.5 Time logs

`start_timer` + `complete_job_card(qty, for_quantity=qty, pending_qty=0, process_loss_qty=0)`
— exactly the desk timer sequence. Operations **with sub-operations** need one closed log per
sub-operation (v16 takes the minimum completed qty across them), so the From–To window is split
into equal consecutive slots, one per sub-operation.

### 2.6 Post-check and submit

The card must now read `for_quantity == total_completed_qty == qty` and `pending_qty == 0`;
anything else fails the run. The card is submitted.

### 2.7 Manufacture entry

`make_stock_entry_for_semi_fg_item(auto_submit=False)` (the desk *Make Stock Entry → No*),
then:

- `expense_account = "621301 - تعدیلات موجودی کالا - E"` on every row — the site's
  Before-Submit validator («ولیدیت-تولید-دیفرنس تعدیلات سند منیفکچر») requires it; this is what
  the desk button «اعمال حساب تعدیلات» does. Constant: `MANUFACTURE_DIFFERENCE_ACCOUNT`.
- The finished-item row must equal the cycle qty.
- If the Finished Good is batch-tracked: `batch_no = log.output_batch_no` or a **placeholder
  lot**:
  - `lot_no = <GMP no>-Lnn` — GMP no = card `custom_batch_number` → WO `custom_fg_batch_no` →
    WO name; `nn` = number of *Done* logs for this WO + operation row + 1 (the cycle number).
  - Batch id follows the site's batch-script convention `<counter>-<item>-<lot_no>` (counter =
    highest numeric prefix in use + 1) so the duplicate validator and reports keep working;
    `custom_batch_no = lot_no`,
    `custom_is_placeholder_lot = 1`, manufacturing date = posting date, expiry from the item's
    shelf life, reference = the Work Order.
  - An existing Batch with the same `custom_batch_no` is reused.
  - The `-Lnn` suffix is a **placeholder until planning defines the sub-lot rule.**

Saved and submitted.

### 2.8 Close the log

On success the log gets `status = Done`, the three document links, `run_on`,
`duration_seconds`, and one *Created Documents* row per document with its action
(`created` / `reused` / `reused (qty set to N)`); committed. The call returns
`{status, job_card, transfer_stock_entry, manufacture_stock_entry, duration_seconds, message}`.

### 2.9 Failure, idempotency, concurrency

- **Any exception** → `frappe.db.rollback()`, log set to **Failed** with the full traceback in
  `error_log` and the elapsed time, committed, then re-thrown to the desk as
  *"Cycle failed and was rolled back: <last line>"*. No Job Card, Stock Entry or Batch survives.
- **Idempotent:** `run()` on a *Done* log returns *"already Done — nothing to do"* and creates
  nothing; a *Failed* log can be re-run after editing.
- **Concurrent** runs of the same Work Order + operation: exactly one *Done*, the other *Failed*
  with nothing created (row lock + file lock).

## 3. Job Card guards (`job_card_hooks.py`, all Job Cards)

| Hook | Rule | Why |
| --- | --- | --- |
| `after_insert` | Set `semi_fg_bom = WO.bom_no` on any card of a *Track Semi Finished Goods* Work Order that lacks it | Cards created at Work Order submit have no `semi_fg_bom`, cards from the *Create Job Card* dialog get the parent BOM; their Manufacture entries then carry different `bom_no` and `get_consumed_operating_cost` (filters on `bom_no`) stops seeing earlier lots → **operating cost allocated twice** |
| `validate` (draft cards only) | Refuse `pending_qty > 0` when `total_completed_qty > 0` | The Complete-Job dialog values would strand the remaining quantity (see the rule in *Summary*); message tells the user to re-open *Complete Job* and set cycle qty = completed qty |

## 4. Schema

### Daily Production Log (`DPL-{YYYY}-{#####}`, track changes on)

| Section | Field | Type | Notes |
| --- | --- | --- | --- |
| Cycle | `work_order` | Link Work Order | required |
| | `production_item` | Link Item | fetched, read-only |
| | `operation` | Link Operation | required, filtered to the WO's operations |
| | `operation_row_id` | Int | read-only, auto = WO operation row `idx` |
| | `qty` | Float | required, > 0 |
| | `employee` / `employee_name` | Link Employee | required (*Operator*) |
| | `from_time` / `to_time` | Datetime | required, `to > from` |
| Accounting Dimensions & Batch | `department`, `cost_center` | Link | fetched from Employee; fallback on save |
| | `output_batch_no` | Link Batch | optional, filtered to the operation's Finished Good |
| Run | `status` | Select | Draft / Running / Done / Failed (read-only) |
| | `job_card`, `transfer_stock_entry`, `manufacture_stock_entry` | Link | read-only, set by the runner |
| | `run_on`, `duration_seconds` | Datetime / Float | read-only |
| Error | `error_log` | Long Text | traceback of the last failed run |
| Created Documents | `created_documents` | Table | child `Daily Production Log Document` (`document_type`, `document_name`, `action`) |

**Permissions:** System Manager and Manufacturing Manager — full; Manufacturing User — read /
write / create (no delete).

### Custom field

`Batch.custom_is_placeholder_lot` (Check, read-only, after `custom_batch_no`) — lot number was
generated by Daily Production Log with a placeholder `-Lnn` suffix.

## 5. Deployment

```bash
bench --site <site> migrate     # runs both patches, syncs the DocTypes
bench --site <site> clear-cache
```

Patches are idempotent. Version history on this branch:

| Version | Change |
| --- | --- |
| 4.7.2 | Feature: DocTypes, runner, Job Card guards, Batch custom field, e2e test plan |
| 4.7.3 | `bench migrate` does not create Module Defs for a module added to an already-installed app and may serve a cached module list, so `daily_production/` was skipped by `sync_for` on staging (app at 4.7.2, patch applied, no DocTypes). A **pre-model-sync** patch now creates the *Daily Production* Module Def and rebuilds the module map |
| 4.7.4 | Transfer falls back to the Work Order item's (BOM) source warehouse when the card's is short — first staging run failed op 1 with *Not enough batched stock of 13100023 in the raw-material store* (packaging in another store). Test plan retries transient TLS errors and creates the FG batch idempotently for `--work-order` reruns |
| 4.8.0 | (develop) ERPNext 16.33 / Frappe 16.32 compatibility — not Daily Production specific |
| 4.8.1 | Operator-timer guard ignores the Job Card *Employee* multi-select rows (false "running timer" on any draft card listing the operator); tracked previous-operation inputs count only this Work Order's lots even when zero; placeholder-lot counter = highest prefix in use + 1. Test plan: free test days chosen automatically (`--start-day`), Material Transfer for the manual card, `DPL_MAX_SECONDS`. See `RELEASE_4_8_1.md` |

## 6. End-to-end test plan (staging)

```bash
ERP_URL=https://erpstage… ERP_API_KEY=… ERP_API_SECRET=… \
  [DPL_EMP1=HR-EMP-0537 DPL_EMP2=HR-EMP-0538] [DPL_MAX_SECONDS=10] \
  python -m erpnext_extensions.daily_production.e2e.run_test_plan \
      [--bom BOM-20100067-017] [--work-order WO-…] [--start-day 2026-09-01]
```

Runs the whole **Alcarisa-28** batch (`BOM-20100067-017`, 2756 units, company اسپاد فارمد دارو,
`FG - Test - E` / `WIP Filling - Test - E`) purely through Daily Production Logs and exits
non-zero if any expectation fails. The five test days are picked automatically as the day
after the operators' last time log (core refuses overlapping employee time logs), so the plan
can be re-run on a site that still holds earlier runs; `--work-order` reuses a submitted Work
Order nothing was produced on yet. Covered:

- **Failure cases (nothing may be created):** qty > pending; `to_time < from_time` rejected at
  save; downstream operation asking for more than the previous operation produced (600 with 0,
  then with 500 inspected); operator with a running timer on another card; the losing side of a
  concurrent run.
- **Happy path:** op 1 as two cycles (1378 + 1378), op 2 full batch, op 3 in three lots
  (500 / 1500 / 756) with lot 2 opened via the WO dialog and a manual timer between shifts, ops
  4–5 per lot, ops 4 and 5 running while op 3 lot 2 is still open.
- **Concurrency:** two logs for the same WO + op started in parallel → one *Done*, one *Failed*
  that created nothing; the failed one is re-run after fixing its inputs.
- **Idempotency:** `run()` twice on a *Done* log → no-op, no duplicate Job Card.
- **Timing:** every cycle under `DPL_MAX_SECONDS` (default 10 s).
- **Final report:** Work Order *Completed*, produced 2756; **12 Job Cards** (2+1+3+3+3), every
  card `for_quantity == total_completed_qty`, `semi_fg_bom` on all cards; per-operation planned
  vs actual minutes/cost with **booked-to-stock operating cost == Work Order actual cost** (delta
  < 1); valuation chain of every Manufacture entry with its lot.

**Last staging result (2026-08-27, erpstage, app 4.7.4):** `MFG-WO-2026-00441` — Completed,
2756 produced, 12 cards, every functional expectation passed; only op 5 (packaging, 21
materials) exceeded the 10 s budget at 11–12.5 s per cycle.

## 7. Known limitations / open points

- The placeholder sub-lot suffix `-Lnn` stands in until planning defines the real sub-lot rule;
  placeholder lots are flagged via `custom_is_placeholder_lot`.
- `MANUFACTURE_DIFFERENCE_ACCOUNT` is a site-specific constant in `runner.py`.
- A cycle larger than the operation's batch size is refused (*Split the cycle*) rather than
  split automatically.
- Semi-finished inputs are never sourced from another Work Order's lots by design — a missing
  lot of the previous operation's output fails the run.
- Operations with *Skip Material Transfer* produce no transfer entry; their Manufacture entry
  consumes directly.
- **Data finding (staging, mirrors prod):** BOM-20100067-017 sources the packaging items
  `13200412` and `13200317` from the *primary* packaging store while stock is held in the
  *secondary* store. The runner fails closed (card warehouse → BOM warehouse only). Correct
  either the BOM source warehouse or the stock location before packaging cycles run on prod.
- Packaging cycles with ~20 materials take 11–12.5 s on staging (FEFO pick per material).
