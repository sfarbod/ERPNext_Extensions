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
| Facilities scanned | 38 |
| Exact duplicate groups | 3 |
| Facilities in exact groups | 7 |
| Potential duplicate groups | 25 |
| Facilities in potential groups | 50 |
| Report rows | 57 |

## Legend

| Similarity | Meaning |
|---|---|
| Exact | Same Company + Bank + normalized facility number |
| High / Medium / Low | Multi-signal near-duplicate ranking |

## Groups

### EXACT-0001 — Exact

**Reason:** Exact match: company+bank+normalized facility_name

**Suggested owner (heuristic):** `FAC-2026-07367`

**Recommended action:** Review — likely keep Active; Draft may be duplicate

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07062` | Draft | `تسهیلات خرید دین بانک اقتصادنوین شماره 1604.220.7303670.1` | 487,756,164,383 | 22,243,835,617 | `ACC-JV-1405-00840` | Cancelled | 0 | 0 |
| `FAC-2026-07367` | Active | `تسهیلات خرید دین بانک اقتصادنوین شماره  1604.220.7303670.1` | 487,756,164,383 | 22,243,835,617 | `ACC-JV-1405-00921` | Cancelled | 0 | 0 |
### EXACT-0002 — Exact

**Reason:** Exact match: company+bank+normalized facility_name

**Suggested owner (heuristic):** `FAC-2026-12794`

**Recommended action:** Review — likely keep Active; Draft may be duplicate

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-11961` | Draft | `تسهیلات حقوق نوین اقتصاد نوین شماره 1604.649.7303670.5` | 190,727,939,489 | 3,411,910,917 | `ACC-JV-1405-168717` | Cancelled | 0 | 0 |
| `FAC-2026-11962` | Draft | `تسهیلات حقوق نوین اقتصاد نوین شماره 1604.649.7303670.5` | 190,727,939,489 | 3,955,618,840 | `ACC-JV-1405-168717` | Cancelled | 0 | 0 |
| `FAC-2026-12794` | Active | `تسهیلات حقوق نوین اقتصاد نوین شماره 1604.649.7303670.5` | 190,727,939,489 | 3,411,910,917 | `ACC-JV-1405-169114` | Submitted | 1 | 194,139,850,406 |
### EXACT-0003 — Exact

**Reason:** Exact match: company+bank+normalized facility_name

**Suggested owner (heuristic):** `FAC-2026-12853`

**Recommended action:** Review — likely keep Active; Draft may be duplicate

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-12852` | Draft | `تسهیلات خرید دین بانک خاورمیانه شماره 000004835-404-50-1001` | 295,793,205,479 | 814,136,988 | `—` | — | 0 | 0 |
| `FAC-2026-12853` | Active | `تسهیلات خرید دین بانک خاورمیانه شماره 000004835-404-50-1001` | 295,793,205,479 | 814,136,988 | `ACC-JV-1405-172080` | Submitted | 0 | 0 |
### POTENTIAL-0004 — High

**Reason:** Near-identical facility number (sim=0.94); Same principal amount; Same profit amount; Same bank account; Draft + Active pair

**Suggested owner (heuristic):** `FAC-2026-11810`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07443` | Draft | `تسهیلات خرید دین بانک اقتصاد نوین شماره 1604.220.7363670.3` | 432,264,132,834 | 39,541,161,518 | `ACC-JV-1405-00994` | Cancelled | 0 | 0 |
| `FAC-2026-11810` | Active | `تسهیلات خرید دین بانک اقتصادنوین شماره  1604.220.7303670.3` | 432,264,132,834 | 39,541,161,518 | `ACC-JV-1405-146350` | Cancelled | 0 | 0 |
### POTENTIAL-0005 — Medium

**Reason:** Near-identical facility number (sim=0.94); Same contract date; Same receive date; Same bank account

**Suggested owner (heuristic):** `FAC-2026-07294`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07293` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.2` | 102,000,000,000 | 1,072,096,774 | `ACC-JV-1405-00913` | Submitted | 1 | 103,072,096,774 |
| `FAC-2026-07294` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.3` | 88,000,000,000 | 924,946,237 | `ACC-JV-1405-00914` | Submitted | 1 | 88,924,946,237 |
### POTENTIAL-0006 — Medium

**Reason:** Near-identical facility number (sim=0.94); Same bank account; Draft + Active pair

**Suggested owner (heuristic):** `FAC-2026-06538`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-06538` | Active | `تسهیلات حقوق نوین بانک اقتصادنوین شماره 1604.649.7303670.2` | 167,500,000,000 | 3,210,416,667 | `ACC-JV-1405-00789` | Submitted | 1 | 170,296,169,355 |
| `FAC-2026-12837` | Draft | `تسهیلات حقوق نوین اقتصاد نوین شماره 1604.649.7303670.6` | 200,000,000,000 | 3,780,821,918 | `—` | — | 0 | 0 |
### POTENTIAL-0007 — Medium

**Reason:** Near-identical facility number (sim=0.94); Same bank account; Draft + Active pair

**Suggested owner (heuristic):** `FAC-2026-07063`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07063` | Active | `تسهیلات خرید دین بانک اقتصادنوین شماره  1604.220.7303670.2` | 1,387,520,547,945 | 112,479,452,055 | `ACC-JV-1405-00859` | Cancelled | 1 | 1,500,000,000,000 |
| `FAC-2026-12840` | Draft | `تسهیلات خرید دین بانک اقتصاد نوین شماره 1604.220.7303670.4` | 1,381,013,326,027 | 125,386,673,973 | `—` | — | 0 | 0 |
### POTENTIAL-0008 — Medium

**Reason:** Near-identical facility number (sim=0.94); Same bank account; Draft + Active pair

**Suggested owner (heuristic):** `FAC-2026-07295`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07295` | Active | `تسهیلات حقوق نوین اقتصاد نوین شماره 1604.649.7303670.3` | 223,743,454,977 | 4,150,080,213 | `ACC-JV-1405-147723` | Submitted | 1 | 227,893,535,190 |
| `FAC-2026-12837` | Draft | `تسهیلات حقوق نوین اقتصاد نوین شماره 1604.649.7303670.6` | 200,000,000,000 | 3,780,821,918 | `—` | — | 0 | 0 |
### POTENTIAL-0009 — Medium

**Reason:** Near-identical facility number (sim=0.94); Same bank account; Draft + Active pair

**Suggested owner (heuristic):** `FAC-2026-11810`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-11810` | Active | `تسهیلات خرید دین بانک اقتصادنوین شماره  1604.220.7303670.3` | 432,264,132,834 | 39,541,161,518 | `ACC-JV-1405-146350` | Cancelled | 0 | 0 |
| `FAC-2026-12840` | Draft | `تسهیلات خرید دین بانک اقتصاد نوین شماره 1604.220.7303670.4` | 1,381,013,326,027 | 125,386,673,973 | `—` | — | 0 | 0 |
### POTENTIAL-0010 — Low

**Reason:** Possible typo in facility number digits (sim=0.85); Same contract date; Same receive date; Same bank account

**Suggested owner (heuristic):** `FAC-2026-06503`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-06501` | Active | `تسهیلات فروش اقساطی توسعه صادرات شماره 6300005760009` | 294,954,063,593 | 88,445,648,691 | `—` | — | 6 | 83,592,001,132 |
| `FAC-2026-06503` | Active | `تسهیلات فروش اقساطی توسعه صادرات شماره 6300005761007` | 480,783,587,898 | 211,159,168,590 | `—` | — | 6 | 161,486,744,769 |
### POTENTIAL-0011 — Low

**Reason:** Near-identical facility number (sim=0.94); Same bank account

**Suggested owner (heuristic):** `FAC-2026-07295`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-06538` | Active | `تسهیلات حقوق نوین بانک اقتصادنوین شماره 1604.649.7303670.2` | 167,500,000,000 | 3,210,416,667 | `ACC-JV-1405-00789` | Submitted | 1 | 170,296,169,355 |
| `FAC-2026-07295` | Active | `تسهیلات حقوق نوین اقتصاد نوین شماره 1604.649.7303670.3` | 223,743,454,977 | 4,150,080,213 | `ACC-JV-1405-147723` | Submitted | 1 | 227,893,535,190 |
### POTENTIAL-0012 — Low

**Reason:** Near-identical facility number (sim=0.94); Same bank account

**Suggested owner (heuristic):** `FAC-2026-06538`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-06538` | Active | `تسهیلات حقوق نوین بانک اقتصادنوین شماره 1604.649.7303670.2` | 167,500,000,000 | 3,210,416,667 | `ACC-JV-1405-00789` | Submitted | 1 | 170,296,169,355 |
| `FAC-2026-11825` | Closed | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.649.7303670.4` | 226,386,533,434 | 4,059,134,887 | `ACC-JV-1405-147297` | Submitted | 1 | 230,445,668,321 |
### POTENTIAL-0013 — Low

**Reason:** Similar facility number (sim=0.89); Same bank account; Draft + Active pair

**Suggested owner (heuristic):** `FAC-2026-07063`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07063` | Active | `تسهیلات خرید دین بانک اقتصادنوین شماره  1604.220.7303670.2` | 1,387,520,547,945 | 112,479,452,055 | `ACC-JV-1405-00859` | Cancelled | 1 | 1,500,000,000,000 |
| `FAC-2026-07443` | Draft | `تسهیلات خرید دین بانک اقتصاد نوین شماره 1604.220.7363670.3` | 432,264,132,834 | 39,541,161,518 | `ACC-JV-1405-00994` | Cancelled | 0 | 0 |
### POTENTIAL-0014 — Low

**Reason:** Near-identical facility number (sim=0.94); Same bank account

**Suggested owner (heuristic):** `FAC-2026-07063`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07063` | Active | `تسهیلات خرید دین بانک اقتصادنوین شماره  1604.220.7303670.2` | 1,387,520,547,945 | 112,479,452,055 | `ACC-JV-1405-00859` | Cancelled | 1 | 1,500,000,000,000 |
| `FAC-2026-11810` | Active | `تسهیلات خرید دین بانک اقتصادنوین شماره  1604.220.7303670.3` | 432,264,132,834 | 39,541,161,518 | `ACC-JV-1405-146350` | Cancelled | 0 | 0 |
### POTENTIAL-0015 — Low

**Reason:** Near-identical facility number (sim=0.94); Same bank account

**Suggested owner (heuristic):** `FAC-2026-11968`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07293` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.2` | 102,000,000,000 | 1,072,096,774 | `ACC-JV-1405-00913` | Submitted | 1 | 103,072,096,774 |
| `FAC-2026-11968` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.4` | 57,000,000,000 | 538,767,124 | `ACC-JV-1405-169088` | Submitted | 1 | 57,538,767,124 |
### POTENTIAL-0016 — Low

**Reason:** Near-identical facility number (sim=0.94); Same bank account

**Suggested owner (heuristic):** `FAC-2026-07293`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07293` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.2` | 102,000,000,000 | 1,072,096,774 | `ACC-JV-1405-00913` | Submitted | 1 | 103,072,096,774 |
| `FAC-2026-12133` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.6` | 511,897,620,000 | 9,676,968,707 | `ACC-JV-1405-168994` | Cancelled | 0 | 0 |
### POTENTIAL-0017 — Low

**Reason:** Near-identical facility number (sim=0.94); Same bank account

**Suggested owner (heuristic):** `FAC-2026-07293`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07293` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.2` | 102,000,000,000 | 1,072,096,774 | `ACC-JV-1405-00913` | Submitted | 1 | 103,072,096,774 |
| `FAC-2026-12209` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.5` | 228,800,000,000 | 4,325,260,274 | `ACC-JV-1405-169113` | Submitted | 0 | 0 |
### POTENTIAL-0018 — Low

**Reason:** Near-identical facility number (sim=0.94); Same bank account

**Suggested owner (heuristic):** `FAC-2026-11968`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07294` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.3` | 88,000,000,000 | 924,946,237 | `ACC-JV-1405-00914` | Submitted | 1 | 88,924,946,237 |
| `FAC-2026-11968` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.4` | 57,000,000,000 | 538,767,124 | `ACC-JV-1405-169088` | Submitted | 1 | 57,538,767,124 |
### POTENTIAL-0019 — Low

**Reason:** Near-identical facility number (sim=0.94); Same bank account

**Suggested owner (heuristic):** `FAC-2026-07294`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07294` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.3` | 88,000,000,000 | 924,946,237 | `ACC-JV-1405-00914` | Submitted | 1 | 88,924,946,237 |
| `FAC-2026-12133` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.6` | 511,897,620,000 | 9,676,968,707 | `ACC-JV-1405-168994` | Cancelled | 0 | 0 |
### POTENTIAL-0020 — Low

**Reason:** Near-identical facility number (sim=0.94); Same bank account

**Suggested owner (heuristic):** `FAC-2026-07294`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07294` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.3` | 88,000,000,000 | 924,946,237 | `ACC-JV-1405-00914` | Submitted | 1 | 88,924,946,237 |
| `FAC-2026-12209` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.5` | 228,800,000,000 | 4,325,260,274 | `ACC-JV-1405-169113` | Submitted | 0 | 0 |
### POTENTIAL-0021 — Low

**Reason:** Near-identical facility number (sim=0.94); Same bank account

**Suggested owner (heuristic):** `FAC-2026-07295`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-07295` | Active | `تسهیلات حقوق نوین اقتصاد نوین شماره 1604.649.7303670.3` | 223,743,454,977 | 4,150,080,213 | `ACC-JV-1405-147723` | Submitted | 1 | 227,893,535,190 |
| `FAC-2026-11825` | Closed | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.649.7303670.4` | 226,386,533,434 | 4,059,134,887 | `ACC-JV-1405-147297` | Submitted | 1 | 230,445,668,321 |
### POTENTIAL-0022 — Low

**Reason:** Near-identical facility number (sim=0.94); Same bank account

**Suggested owner (heuristic):** `FAC-2026-11825`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-11825` | Closed | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.649.7303670.4` | 226,386,533,434 | 4,059,134,887 | `ACC-JV-1405-147297` | Submitted | 1 | 230,445,668,321 |
| `FAC-2026-12837` | Draft | `تسهیلات حقوق نوین اقتصاد نوین شماره 1604.649.7303670.6` | 200,000,000,000 | 3,780,821,918 | `—` | — | 0 | 0 |
### POTENTIAL-0023 — Low

**Reason:** Near-identical facility number (sim=0.94); Same bank account

**Suggested owner (heuristic):** `FAC-2026-11968`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-11968` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.4` | 57,000,000,000 | 538,767,124 | `ACC-JV-1405-169088` | Submitted | 1 | 57,538,767,124 |
| `FAC-2026-12133` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.6` | 511,897,620,000 | 9,676,968,707 | `ACC-JV-1405-168994` | Cancelled | 0 | 0 |
### POTENTIAL-0024 — Low

**Reason:** Near-identical facility number (sim=0.94); Same bank account

**Suggested owner (heuristic):** `FAC-2026-11968`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-11968` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.4` | 57,000,000,000 | 538,767,124 | `ACC-JV-1405-169088` | Submitted | 1 | 57,538,767,124 |
| `FAC-2026-12209` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.5` | 228,800,000,000 | 4,325,260,274 | `ACC-JV-1405-169113` | Submitted | 0 | 0 |
### POTENTIAL-0025 — Low

**Reason:** Similar facility number (sim=0.89); Same bank account; Draft + Active pair

**Suggested owner (heuristic):** `FAC-2026-11968`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-11968` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.4` | 57,000,000,000 | 538,767,124 | `ACC-JV-1405-169088` | Submitted | 1 | 57,538,767,124 |
| `FAC-2026-12840` | Draft | `تسهیلات خرید دین بانک اقتصاد نوین شماره 1604.220.7303670.4` | 1,381,013,326,027 | 125,386,673,973 | `—` | — | 0 | 0 |
### POTENTIAL-0026 — Low

**Reason:** Near-identical facility number (sim=0.94); Same bank account

**Suggested owner (heuristic):** `FAC-2026-12209`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-12133` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.6` | 511,897,620,000 | 9,676,968,707 | `ACC-JV-1405-168994` | Cancelled | 0 | 0 |
| `FAC-2026-12209` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.5` | 228,800,000,000 | 4,325,260,274 | `ACC-JV-1405-169113` | Submitted | 0 | 0 |
### POTENTIAL-0027 — Low

**Reason:** Similar facility number (sim=0.89); Same bank account; Draft + Active pair

**Suggested owner (heuristic):** `FAC-2026-12133`

**Recommended action:** Potential duplicate — verify manually

| Facility | Status | Number | Principal | Profit | Receipt JE | JE Status | Repayments | Paid |
|---|---|---|---|---|---|---|---|---|
| `FAC-2026-12133` | Active | `تسهیلات مرابحه واردات اقتصاد نوین شماره1604.608.7303670.6` | 511,897,620,000 | 9,676,968,707 | `ACC-JV-1405-168994` | Cancelled | 0 | 0 |
| `FAC-2026-12837` | Draft | `تسهیلات حقوق نوین اقتصاد نوین شماره 1604.649.7303670.6` | 200,000,000,000 | 3,780,821,918 | `—` | — | 0 | 0 |
### POTENTIAL-0028 — Low

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
