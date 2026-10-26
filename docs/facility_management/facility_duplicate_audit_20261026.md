# Facility Duplicate Audit — Finance Review

**Date:** 2026-10-26
**Site (READ-ONLY scan):** `development.localhost`

## Important

- This report is **READ-ONLY**. No Facility, Journal Entry, repayment, schedule, or GL was modified.
- The agent does **not** decide deletions. Finance completes `Finance Decision` / `Finance Notes`.
- Suggested Owner is a heuristic only.

## Summary

| Metric | Value |
|---|---|
| Facilities scanned | 42 |
| EXACT_DUPLICATE groups | 3 |
| Facilities in exact groups | 7 |
| HIGH_CONFIDENCE_POTENTIAL groups | 6 |
| NEEDS_FINANCE_REVIEW groups | 1 |
| Potential groups (total) | 7 |
| Report rows | 21 |

## Legend

| Confidence | Meaning |
|---|---|
| EXACT_DUPLICATE | Same Company + Bank + normalized facility number |
| HIGH_CONFIDENCE_POTENTIAL | Strong number/JE match reinforced by contract signals |
| NEEDS_FINANCE_REVIEW | Suspicious but not conclusive — verify manually |

## Groups

### EXACT-0001 — EXACT_DUPLICATE

**Confidence:** `EXACT_DUPLICATE`

**Reason:** Exact match: company+bank+normalized facility_name

**Suggested owner (heuristic):** `FAC-2026-07367`

**Recommended action:** Review — likely keep Active; Draft may be duplicate

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07062` | Draft | `تسهیلات خرید دین بانک اقتصادنوین شماره 1604.220.7303670.1` | 487,756,164,383 | 22,243,835,617 | `ACC-JV-1405-00840` | Cancelled | 0 | 0 |
| `FAC-2026-07367` | Active | `تسهیلات خرید دین بانک اقتصادنوین شماره  1604.220.7303670.1` | 487,756,164,383 | 22,243,835,617 | `ACC-JV-1405-00921` | Cancelled | 0 | 0 |
### EXACT-0002 — EXACT_DUPLICATE

**Confidence:** `EXACT_DUPLICATE`

**Reason:** Exact match: company+bank+normalized facility_name

**Suggested owner (heuristic):** `FAC-2026-12794`

**Recommended action:** Review — likely keep Active; Draft may be duplicate

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-11961` | Draft | `تسهیلات حقوق نوین اقتصاد نوین شماره 1604.649.7303670.5` | 190,727,939,489 | 3,411,910,917 | `ACC-JV-1405-168717` | Cancelled | 0 | 0 |
| `FAC-2026-11962` | Draft | `تسهیلات حقوق نوین اقتصاد نوین شماره 1604.649.7303670.5` | 190,727,939,489 | 3,955,618,840 | `ACC-JV-1405-168717` | Cancelled | 0 | 0 |
| `FAC-2026-12794` | Active | `تسهیلات حقوق نوین اقتصاد نوین شماره 1604.649.7303670.5` | 190,727,939,489 | 3,411,910,917 | `ACC-JV-1405-169114` | Submitted | 1 | 194,139,850,406 |
### EXACT-0003 — EXACT_DUPLICATE

**Confidence:** `EXACT_DUPLICATE`

**Reason:** Exact match: company+bank+normalized facility_name

**Suggested owner (heuristic):** `FAC-2026-12853`

**Recommended action:** Review — likely keep Active; Draft may be duplicate

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-12852` | Draft | `تسهیلات خرید دین بانک خاورمیانه شماره 000004835-404-50-1001` | 295,793,205,479 | 814,136,988 | `—` | — | 0 | 0 |
| `FAC-2026-12853` | Active | `تسهیلات خرید دین بانک خاورمیانه شماره 000004835-404-50-1001` | 295,793,205,479 | 814,136,988 | `ACC-JV-1405-172080` | Submitted | 0 | 0 |
### HIGH-0004 — HIGH_CONFIDENCE_POTENTIAL

**Confidence:** `HIGH_CONFIDENCE_POTENTIAL`

**Reason:** Similar facility number (sim=0.94); Same principal amount; Same profit amount; Same bank account; Draft + Active pair

**Suggested owner (heuristic):** `FAC-2026-11810`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07443` | Draft | `تسهیلات خرید دین بانک اقتصاد نوین شماره 1604.220.7363670.3` | 432,264,132,834 | 39,541,161,518 | `ACC-JV-1405-00994` | Cancelled | 0 | 0 |
| `FAC-2026-11810` | Active | `تسهیلات خرید دین بانک اقتصادنوین شماره  1604.220.7303670.3` | 432,264,132,834 | 39,541,161,518 | `ACC-JV-1405-146350` | Cancelled | 0 | 0 |
### HIGH-0005 — HIGH_CONFIDENCE_POTENTIAL

**Confidence:** `HIGH_CONFIDENCE_POTENTIAL`

**Reason:** Similar facility number (sim=0.94); Same contract date; Same receive date; Same bank account

**Suggested owner (heuristic):** `FAC-2026-07294`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07293` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.2` | 102,000,000,000 | 1,072,096,774 | `ACC-JV-1405-00913` | Submitted | 1 | 103,072,096,774 |
| `FAC-2026-07294` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.3` | 88,000,000,000 | 924,946,237 | `ACC-JV-1405-00914` | Submitted | 1 | 88,924,946,237 |
### HIGH-0006 — HIGH_CONFIDENCE_POTENTIAL

**Confidence:** `HIGH_CONFIDENCE_POTENTIAL`

**Reason:** Similar facility number (sim=0.94); Same bank account; Draft + Active pair

**Suggested owner (heuristic):** `FAC-2026-06538`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-06538` | Active | `تسهیلات حقوق نوین بانک اقتصادنوین شماره 1604.649.7303670.2` | 167,500,000,000 | 3,210,416,667 | `ACC-JV-1405-00789` | Submitted | 1 | 170,296,169,355 |
| `FAC-2026-12837` | Draft | `تسهیلات حقوق نوین اقتصاد نوین شماره 1604.649.7303670.6` | 200,000,000,000 | 3,780,821,918 | `—` | — | 0 | 0 |
### HIGH-0007 — HIGH_CONFIDENCE_POTENTIAL

**Confidence:** `HIGH_CONFIDENCE_POTENTIAL`

**Reason:** Similar facility number (sim=0.94); Same bank account; Draft + Active pair

**Suggested owner (heuristic):** `FAC-2026-07063`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07063` | Active | `تسهیلات خرید دین بانک اقتصادنوین شماره  1604.220.7303670.2` | 1,387,520,547,945 | 112,479,452,055 | `ACC-JV-1405-00859` | Cancelled | 1 | 1,500,000,000,000 |
| `FAC-2026-12840` | Draft | `تسهیلات خرید دین بانک اقتصاد نوین شماره 1604.220.7303670.4` | 1,381,013,326,027 | 125,386,673,973 | `—` | — | 0 | 0 |
### HIGH-0008 — HIGH_CONFIDENCE_POTENTIAL

**Confidence:** `HIGH_CONFIDENCE_POTENTIAL`

**Reason:** Similar facility number (sim=0.94); Same bank account; Draft + Active pair

**Suggested owner (heuristic):** `FAC-2026-07295`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07295` | Active | `تسهیلات حقوق نوین اقتصاد نوین شماره 1604.649.7303670.3` | 223,743,454,977 | 4,150,080,213 | `ACC-JV-1405-147723` | Submitted | 1 | 227,893,535,190 |
| `FAC-2026-12837` | Draft | `تسهیلات حقوق نوین اقتصاد نوین شماره 1604.649.7303670.6` | 200,000,000,000 | 3,780,821,918 | `—` | — | 0 | 0 |
### HIGH-0009 — HIGH_CONFIDENCE_POTENTIAL

**Confidence:** `HIGH_CONFIDENCE_POTENTIAL`

**Reason:** Similar facility number (sim=0.94); Same bank account; Draft + Active pair

**Suggested owner (heuristic):** `FAC-2026-11810`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-11810` | Active | `تسهیلات خرید دین بانک اقتصادنوین شماره  1604.220.7303670.3` | 432,264,132,834 | 39,541,161,518 | `ACC-JV-1405-146350` | Cancelled | 0 | 0 |
| `FAC-2026-12840` | Draft | `تسهیلات خرید دین بانک اقتصاد نوین شماره 1604.220.7303670.4` | 1,381,013,326,027 | 125,386,673,973 | `—` | — | 0 | 0 |
### REVIEW-0010 — NEEDS_FINANCE_REVIEW

**Confidence:** `NEEDS_FINANCE_REVIEW`

**Reason:** Near-identical facility number (sim=0.95)

**Suggested owner (heuristic):** `FAC-2026-12211`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-06493` | Active | `تسهیلات مرابحه واردات کارآفرین شماره 3-43574719-2806-0132` | 294,000,000,000 | 37,901,261,535 | `—` | — | 5 | 138,292,190,000 |
| `FAC-2026-12211` | Active | `تسهیلات مرابحه واردات کارآفرین شماره 4-43574719-2806-0132` | 400,000,000,000 | 51,566,342,225 | `ACC-JV-1405-172098` | Submitted | 0 | 0 |

## Finance decision columns

Use the CSV companion file to fill `finance_decision` and `finance_notes`.

---

*Generated by `facility_duplicate_audit.run_duplicate_audit` — no deletions performed.*
