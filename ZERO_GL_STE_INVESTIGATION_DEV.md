# Zero-GL Stock Entry Investigation — Development (Read-Only)

**Site:** `development.localhost`  
**Version:** `erpnext_extensions` **5.7.18** (unchanged)  
**Mode:** Read-only — no RIV, repost, JE, cancel/amend/submit, or git commit  
**Denylist:** `ACC-JV-1405-169046` untouched  

**Evidence**
- Full inventory (1,387 rows): `ZERO_GL_STE_INVESTIGATION_DEV.json`
- Deep dives: `ZERO_GL_STE_DEEP_DIVES_DEV.json`

---

## Executive verdict

| Bucket | Count | Meaning |
|---|---:|---|
| **Expected zero GL** | **1,384** | Same inventory-account netting, zero-value movements, or intentional Repack/Manufacture net-zero |
| **Unexpected / defect** | **3** | Zero document values + valued SLE + Iran balanced-transfer GL path emits `[]` while core would post |
| Submitted STE with zero GL | **1,387** | Company: اسپاد فارمد دارو; perpetual inventory **enabled** |

**Cohort B:** may proceed only for Class A fractional-rate identities **after** excluding or separately gating the 3 unexpected MTfM vouchers and accepting that **Repack `MAT-STE-2026-29903` zero GL is expected** (fractional IR is a separate issue). Not a blanket “1,387 missing GL” crisis.

---

## 1. Population

All submitted Stock Entries with **no active** `GL Entry` (`is_cancelled=0`):

| Purpose | Count |
|---|---:|
| Material Transfer | 932 |
| Repack | 345 |
| Manufacture | 35 |
| Material Issue | 28 |
| Material Transfer for Manufacture | 28 |
| Material Receipt | 19 |
| **Total** | **1,387** |

- None lack SLE entirely (`NO_SLE` = 0).  
- None lack warehouse accounts.  
- None have only cancelled GL leftovers.

---

## 2. Classification by purpose and root cause

| Classification | Count | Purposes | Root cause |
|---|---:|---|---|
| `EXPECTED_SAME_ACCOUNT_TRANSFER_NET_ZERO` | 935 | Material Transfer (928), MTfM (7) | From/to warehouses share **one** inventory account; SLE SVD nets ~0; perpetual GL nets to nothing |
| `EXPECTED_SAME_ACCOUNT_REPACK_NET_ZERO` | 345 | Repack | Same WH/account; in value = out value; extension drops Dr=Cr merged inventory row (`drop_net_zero_merged_gl_rows` / normalized Repack GL) |
| `EXPECTED_ZERO_VALUE_MOVEMENT` | 72 | Issue 28, Receipt 19, MTfM 18, Manufacture 5, Transfer 2 | Header/SLE stock value ~0 → no inventory GL amount |
| `LIKELY_MANUFACTURE_SAME_ACCOUNT_NET_ZERO` | 30 | Manufacture | Single inventory account + ~zero value difference; live `get_gl_entries()` empty |
| `EXPECTED_PAIRED_SAME_ACCOUNT_NET_ZERO` | 2 | Material Transfer | Multi-account transfer but **each pair** stays inside one account (RM→RM, packaging→packaging); overall SVD=0 |
| `UNEXPECTED_ZERO_DOC_VALUE_TRANSFER_SLE_VALUED_NO_GL` | **3** | MTfM | See §4 |

Document-level inventory with per-row fields (purpose, dates, warehouses, accounts, SLE counts, classification) is in the JSON `records[]` array (1,387 objects).

---

## 3. Expected zero GL (1,384)

### Accounting basis (core + config)

- Company has **`enable_perpetual_inventory = 1`**. Zero GL is therefore **not** “perpetual off.”  
- Core `StockController.get_gl_entries` posts inventory legs from SLE `stock_value_difference` by warehouse account. When source and target use the **same** account, debit and credit cancel under `process_gl_map` / merge → **no residual GL rows**.  
- Quarantine→Approved transfers for the same item class commonly share one account (e.g. both packaging warehouses → `111620`).

### Extension behavior that correctly yields empty GL

- **Repack / Manufacture** IRR path: `iran_stock_entry_get_gl_entries` → `build_normalized_manufacture_gl_entries` → `drop_net_zero_merged_gl_rows` removes inventory rows where debit=credit (see `test_repack_net_zero_gl_v5715.py`).  
- **Zero-value movements:** no amount to post.

### Lists

- **Expected:** all records in JSON where `classification` starts with `EXPECTED_` or `LIKELY_MANUFACTURE_SAME_ACCOUNT_NET_ZERO` (1,384 names).  
- Regenerable filter: `classification in expected_classifications` in the JSON header.

---

## 4. Unexpected missing GL (3)

| Stock Entry | Purpose | Distinct WH accounts | Doc in/out value | SLE SVD sum | Live wrapped `get_gl_entries` | Core `get_gl_entries` (unwrapped) |
|---|---|---|---:|---:|---|---|
| `MAT-STE-2026-29942` | MTfM | Packaging `111620` → WIP `111701` | 0 / 0 | 947,744,691 | `[]` (0) | Dr WIP 947,744,691 / Cr Stock Adjustment `621301` |
| `MAT-STE-2026-29943` | MTfM | same pattern | 0 / 0 | 139,242,111 | `[]` (0) | Dr WIP 139,242,111 / Cr Stock Adjustment |
| `MAT-STE-2026-29944` | MTfM | same pattern | 0 / 0 | 68,872,442 | `[]` (0) | Dr WIP 68,872,442 / Cr Stock Adjustment |

Replay used `monkey_patches.apply_monkey_patches()` then `StockEntry.get_gl_entries` vs `_iran_original_stock_entry_get_gl_entries`. Live `GL Entry` count = 0 for all three. Bare console **without** hooks can call unpatched core and appear to “have” GL maps — that is not the runtime accounting path.

### Mechanism (reproducible)

1. SED rows have **rate/amount = 0** (zero valuation transfer / pending rate).  
2. Header `total_incoming_value = total_outgoing_value = 0`, `value_difference = 0`.  
3. Extension `_should_force_balanced_transfer_gl` returns **True** for MTfM when in=out and value_diff=0 (`zero_value_transfer.py`).  
4. `build_iran_balanced_transfer_gl` returns **`[]`** (confirmed count 0 for all three).  
5. SLE shows **source SVD = 0**, **destination SVD = full value** (asymmetric valuation).  
6. Core `_iran_original_stock_entry_get_gl_entries` posts **Dr WIP / Cr Stock Adjustment** for the destination value (2 rows each).

**Category:** unexpected relative to economic substance (value moved into WIP).  
**Cause:** interaction of **historical zero-rate transfer data** + **Iran balanced-transfer GL gate** — not “missing warehouse account,” not cancelled GL, not perpetual off.  
**Not** assumed to be a generic Repack bug.

---

## 5. Special case — `MAT-STE-2026-29903` (Repack)

| Field | Evidence |
|---|---|
| Purpose | Repack |
| Warehouses | Same: Quarantine SFG |
| Inventory account | Single: `111605 - موجودی نیمه ساخته - E` |
| Out / in value | 1,087,591,494.88 / 1,087,591,494.88 (`value_difference = 0`) |
| Items | Same item `30200048` qty 659 out and 659 in (finished) |
| SLE | 2 rows: −659 SVD −1,087,591,494.88; +659 SVD +1,087,591,494.88 |
| Inward `incoming_rate` | **1,650,366.45657…** (fractional — Cohort B Class A) |
| Outward `outgoing_rate` | 1,399,938 (integer; rate ≠ inward) |
| Active GL | **none** |
| Live `get_gl_entries()` | **`[]`** |

### Expected vs defect

- **Zero GL: expected.** Same warehouse/account, equal in/out value → inventory Dr/Cr cancel; extension intentionally drops net-zero Repack GL. This matches ERPNext perpetual logic for same-account stock movements and local Repack net-zero tests.  
- **Do not** treat 29903 as “SLE without required GL.”  
- **Separate issue:** fractional inward IR and outbound/inbound rate divergence → **Cohort B / Class A valuation**, not GL omission.

---

## 6. Core vs extension responsibilities

| Layer | Role in zero-GL outcomes |
|---|---|
| ERPNext core | Same-account transfer/repack nets to empty GL; SLE drives amounts when posting |
| Iran `zero_value_transfer` | Forces balanced-transfer GL builder when header in=out & value_diff=0 — **empty when doc values are 0 even if SLE later valued** |
| Iran manufacture/repack normalize | Drops merged Dr=Cr inventory rows (expected empty GL) |
| Configuration | Shared inventory accounts across quarantine/approved WH amplify expected net-zero |

---

## 7. Proposed remediation (do **not** execute now)

| Category | Strategy |
|---|---|
| Expected same-account transfer / paired / zero-value / manufacture net-zero (1,384) | **No GL backfill.** Document as expected. Optional ops note in Chart of Accounts / WH account design |
| Expected Repack net-zero including `29903` | **No GL backfill.** Handle fractional rates only under Cohort B Class A rules |
| Unexpected MTfM `29942`–`29944` | Separate mini-cohort: rebuild document valuation from SLE or re-run accounting with a path that does not short-circuit on 0/0 headers; compare Iran vs core GL; regression test; **do not** use manual JE/SR to hide |
| Cohort B fractional | Proceed only with allowlist that treats zero-GL expected Repacks as valuation-only; **exclude or specially gate** the 3 unexpected MTfM until GL strategy approved |

---

## 8. Required regression tests

1. Same-account Material Transfer → `get_gl_entries()` empty; SLE present.  
2. Same-account Repack balanced values → empty GL (exists: `test_repack_net_zero_gl_v5715`).  
3. **MTfM with zero SED rates but nonzero destination SLE SVD** → must not silently return `[]` without an explicit, tested policy (today’s unexpected behavior).  
4. Paired multi-account transfer (RM+packaging quarantine→approved) → empty GL when each account nets.  
5. Material Issue/Receipt with zero SVD → empty GL.  
6. Cohort B gate: `29903` eligible for rate fix without requiring GL insert.  
7. Denylist JE unchanged under any future STE remediation harness.

---

## 9. Cohort B recommendation

**Conditional proceed (valuation-only), not blocked by the 1,384 expected zero-GL STE.**

| Question | Answer |
|---|---|
| Does zero GL on 1,387 STE block Cohort B? | **No** for the 1,384 expected cases |
| Is `MAT-STE-2026-29903` a GL blocker? | **No** — zero GL expected; keep as Class A fractional IR candidate |
| Hard blockers before Cohort B execute | (1) Explicit authorization; (2) triage/exclude **`29942`–`29944`** from blind RIV; (3) stuck RIV queue hygiene; (4) dry-run refresh with zero-GL classification attached |

**Do not** invent GL for expected net-zero transfers/repacks as part of Cohort B.

---

## 10. Explicit non-actions

No remediation executed · No RIV/repost · No JE/SR · No document mutation · No Production · No version change · No git commit/push.
