# Campaign A — Asset Purchase Receipt Execution (Production)

**Site:** https://erp.espadpharmed.com  
**Company:** اسپاد فارمد دارو  
**App version:** erpnext_extensions `5.7.18` (required 5.7.18)  
**Generated:** 2026-11-09T20:58:43.722612+00:00  
**Final status:** `COMPLETED`  
**Counts:** {'RECONCILED': 7, 'RECONCILED_WITH_ASSET_EXCEPTION': 1}

## Runtime verification

| Check | Result |
|---|---|
| erpnext_extensions version | `5.7.18` PASS |
| Asset PR correction module | AVAILABLE |
| `fa_pr_gl_amount_difference` active | `True` |
| RIV execution path | `execute_job(..., is_async=False)` in web process (same process as patch bootstrap) |
| Separate RQ worker required for this campaign RIV | No |
| SSH/Bench worker probe | Timed out (port 22); not a blocker for this RIV path |
| Empirical patch proof | `MAT-PRE-2026-00747` FA debit **1,225,500,000** after RIV (amt_diff −64,500,000) |
| Open/stuck RIV on scope PRs | None |
| Backup verification | **Waived by user** |
| Denylist JE `ACC-JV-1405-169046` | Untouched (modified 2026-10-19 14:35:53.838699, total_debit 18,215,880,000) |

## Document results

| Purchase Receipt | Status | Target | RIV | FA debit after | GRNI credit after | Balanced |
|---|---|---|---|---:|---:|---|
| `MAT-PRE-2026-01907` | **RECONCILED** | 121013 | `qlv379euo1` | 6,630,000 | 6,630,000 | True |
| `MAT-PRE-2026-00953` | **RECONCILED** | 121013 | `tvcubv1s0n` | 1,500,000,000 | 6,247,000,000 | True |
| `MAT-PRE-2026-01311` | **RECONCILED_WITH_ASSET_EXCEPTION** | 121006 | `uceg805sb5` | 1,290,000,000 | 1,290,000,000 | True |
| `MAT-PRE-2026-00747` | **RECONCILED** | 121006 | `u46unout0l` | 1,225,500,000 | 1,225,500,000 | True |
| `MAT-PRE-2026-01413` | **RECONCILED** | 121003 | `tpuirqpe62` | 11,781,250,000 | 11,781,250,000 | True |
| `MAT-PRE-2026-00880` | **RECONCILED** | 121006 | `r7nb5olmha` | 158,000,000 | 158,000,000 | True |
| `MAT-PRE-2026-01241` | **RECONCILED** | 121003 | `raj6kdech2` | 1,030,000,000 | 1,030,000,000 | True |
| `MAT-PRE-2026-01266` | **RECONCILED** | 121006 | `tlmpufh927` | 1,160,000,000 | 1,160,000,000 | True |

### `MAT-PRE-2026-01907` — RECONCILED

- **Target:** 121013 — Computer (تجهیزات کامپیوتری)
- **RIV:** `qlv379euo1` status `Completed` accounting_only=`1` created `2026-11-10 00:16:11.134505`
- **Before Land debit (preflight):** 6630000.0
- **After Land debit:** 0
- **After target debit:** 6630000.0
- **After GRNI credit/debit:** 6630000.0 / 0.0
- **GL totals after:** debit 6630000.0 / credit 6630000.0 balanced=True
- **SLE count after:** 0 (Asset PR — SLE not expected)
- **Source FA expense accounts after:** ['121013 - تجهیزات کامپیوتری - E']
- **Before GL:**
```
{
  "121000 - زمین - E": {
    "debit": 6630000.0,
    "credit": 0.0,
    "n": 1
  },
  "211230 - معلق خرید دارایی ثابت - E": {
    "debit": 0.0,
    "credit": 6630000.0,
    "n": 1
  }
}
```
- **After GL:**
```
{
  "121013 - تجهیزات کامپیوتری - E": {
    "debit": 6630000.0,
    "credit": 0.0,
    "entries": [
      "229a9c9369"
    ]
  },
  "211230 - معلق خرید دارایی ثابت - E": {
    "debit": 0.0,
    "credit": 6630000.0,
    "entries": [
      "a85f5ed01e"
    ]
  }
}
```

### `MAT-PRE-2026-00953` — RECONCILED

- **Target:** 121013 — Computer (تجهیزات کامپیوتری)
- **RIV:** `tvcubv1s0n` status `Completed` accounting_only=`1` created `2026-11-10 00:21:48.412235`
- **Before Land debit (preflight):** 1500000000.0
- **After Land debit:** 0
- **After target debit:** 1500000000.0
- **After GRNI credit/debit:** 6247000000.0 / 0.0
- **GL totals after:** debit 6247000000.0 / credit 6247000000.0 balanced=True
- **SLE count after:** 0 (Asset PR — SLE not expected)
- **Source FA expense accounts after:** ['121006 - اثاثیه ومنصوبات - E', '121013 - تجهیزات کامپیوتری - E']
- **Before GL:**
```
{
  "121000 - زمین - E": {
    "debit": 1500000000.0,
    "credit": 0.0,
    "n": 1
  },
  "211230 - معلق خرید دارایی ثابت - E": {
    "debit": 0.0,
    "credit": 6247000000.0,
    "n": 1
  },
  "121006 - اثاثیه ومنصوبات - E": {
    "debit": 4747000000.0,
    "credit": 0.0,
    "n": 1
  }
}
```
- **After GL:**
```
{
  "211230 - معلق خرید دارایی ثابت - E": {
    "debit": 0.0,
    "credit": 6247000000.0,
    "entries": [
      "1f4377b685"
    ]
  },
  "121006 - اثاثیه ومنصوبات - E": {
    "debit": 4747000000.0,
    "credit": 0.0,
    "entries": [
      "45e0798b27"
    ]
  },
  "121013 - تجهیزات کامپیوتری - E": {
    "debit": 1500000000.0,
    "credit": 0.0,
    "entries": [
      "83831ba7e9"
    ]
  }
}
```

### `MAT-PRE-2026-01311` — RECONCILED_WITH_ASSET_EXCEPTION

- **Target:** 121006 — Furniture (اثاثیه ومنصوبات)
- **RIV:** `uceg805sb5` status `Completed` accounting_only=`1` created `2026-11-10 00:22:30.245839`
- **Before Land debit (preflight):** 1290000000.0
- **After Land debit:** 0
- **After target debit:** 1290000000.0
- **After GRNI credit/debit:** 1290000000.0 / 0.0
- **GL totals after:** debit 1290000000.0 / credit 1290000000.0 balanced=True
- **SLE count after:** 0 (Asset PR — SLE not expected)
- **Source FA expense accounts after:** ['121006 - اثاثیه ومنصوبات - E']
- **Before GL:**
```
{
  "121000 - زمین - E": {
    "debit": 1290000000.0,
    "credit": 0.0,
    "n": 1
  },
  "211230 - معلق خرید دارایی ثابت - E": {
    "debit": 0.0,
    "credit": 1290000000.0,
    "n": 1
  }
}
```
- **After GL:**
```
{
  "121006 - اثاثیه ومنصوبات - E": {
    "debit": 1290000000.0,
    "credit": 0.0,
    "entries": [
      "25f38a7845"
    ]
  },
  "211230 - معلق خرید دارایی ثابت - E": {
    "debit": 0.0,
    "credit": 1290000000.0,
    "entries": [
      "5e9fc809ff"
    ]
  }
}
```
- **Special:**
```
{
  "case": "E_submitted_cost_changed_with_posted_depr",
  "linked_asset_count": 10,
  "asset_unit_cost": 122550000.0,
  "pr_unit_implied": 129000000.0,
  "pr_gl_fa_debit_after": 1290000000.0,
  "historical_depreciation_preserved": true,
  "acquisition_cost_not_rewritten": true,
  "assets": [
    {
      "name": "3736",
      "purchase_amount": 122550000.0,
      "value_after_depreciation": 113489623.0,
      "status": "Partially Depreciated",
      "modified": "2026-10-22 00:02:37.564194"
    },
    {
      "name": "3737",
      "purchase_amount": 122550000.0,
      "value_after_depreciation": 113589355.0,
      "status": "Partially Depreciated",
      "modified": "2026-10-22 00:02:37.735684"
    },
    {
      "name": "3738",
      "purchase_amount": 122550000.0,
      "value_after_depreciation": 113589355.0,
      "status": "Partially Depreciated",
      "modified": "2026-10-22 00:02:37.905083"
    },
    {
      "name": "3739",
      "purchase_amount": 122550000.0,
      "value_after_depreciation": 113589355.0,
      "status": "Partially Depreciated",
      "modified": "2026-10-22 00:02:38.089389"
    },
    {
      "name": "3740",
      "purchase_amount": 122550000.0,
      "value_after_depreciation": 113589355.0,
      "status": "Partially Depreciated",
      "modified": "2026-10-22 00:02:38.257714"
    },
    {
      "name": "3741",
      "purchase_amount": 122550000.0,
      "value_after_depreciation": 113589355.0,
      "status": "Partially Depreciated",
      "modified": "2026-10-22 00:02:38.432246"
    },
    {
      "name": "3742",
      "purchase_amount": 122550000.0,
      "value_after_depreciation": 113589355.0,
      "status": "Partially Depreciated",
      "modified": "2026-10-22 00:02:38.602226"
    },
    {
      "name": "3743",
      "purchase_amount": 122550000.0,
      "value_after_depreciation": 113589355.0,
      "status": "Partially Depreciated",
      "modified": "2026-10-22 00:02:38.783597"
    },
    {
      "name": "3744",
      "purchase_amount": 122550000.0,
      "value_after_depreciation": 113589355.0,
      "status": "Partially Depreciated",
      "modified": "2026-10-22 00:02:38.975860"
    },
    {
      "name": "3745",
      "purchase_amount": 122550000.0,
      "value_after_depreciation": 113589355.0,
      "status": "Partially Depreciated",
      "modified": "2026-10-22 00:02:39.174055"
    }
  ],
  "exception_note": "Account/GL corrected Land→Furniture. Ten Assets remain at purchase_amount 122,550,000 IRR with posted depreciation; acquisition-cost rebuild not performed (Case E)."
}
```

### `MAT-PRE-2026-00747` — RECONCILED

- **Target:** 121006 — Furniture (اثاثیه ومنصوبات)
- **RIV:** `u46unout0l` status `Completed` accounting_only=`1` created `2026-11-10 00:22:03.832825`
- **Before Land debit (preflight):** 1290000000.0
- **After Land debit:** 0
- **After target debit:** 1225500000.0
- **After GRNI credit/debit:** 1225500000.0 / 0.0
- **GL totals after:** debit 1225500000.0 / credit 1225500000.0 balanced=True
- **SLE count after:** 0 (Asset PR — SLE not expected)
- **Source FA expense accounts after:** ['121006 - اثاثیه ومنصوبات - E']
- **Before GL:**
```
{
  "121000 - زمین - E": {
    "debit": 1290000000.0,
    "credit": 0.0,
    "n": 1
  },
  "211230 - معلق خرید دارایی ثابت - E": {
    "debit": 0.0,
    "credit": 1290000000.0,
    "n": 1
  }
}
```
- **After GL:**
```
{
  "121006 - اثاثیه ومنصوبات - E": {
    "debit": 1225500000.0,
    "credit": 0.0,
    "entries": [
      "1c01cd9c1b"
    ]
  },
  "211230 - معلق خرید دارایی ثابت - E": {
    "debit": 0.0,
    "credit": 1225500000.0,
    "entries": [
      "e22e5a153e"
    ]
  }
}
```
- **Special:**
```
{
  "pi_amount_difference": -64500000.0,
  "expected_fa_debit": 1225500000.0,
  "actual_fa_debit": 1225500000.0,
  "actual_grni_credit": 1225500000.0,
  "matched_expectation": true,
  "linked_pi": "ACC-PINV-2026-00331"
}
```

### `MAT-PRE-2026-01413` — RECONCILED

- **Target:** 121003 — Machinery (ماشین آلات و تجهیزات تولید)
- **RIV:** `tpuirqpe62` status `Completed` accounting_only=`1` created `2026-11-10 00:21:31.061424`
- **Before Land debit (preflight):** 11781250000.0
- **After Land debit:** 0
- **After target debit:** 11781250000.0
- **After GRNI credit/debit:** 11781250000.0 / 0.0
- **GL totals after:** debit 11781250000.0 / credit 11781250000.0 balanced=True
- **SLE count after:** 0 (Asset PR — SLE not expected)
- **Source FA expense accounts after:** ['121003 - ماشین آلات و تجهیزات تولید - E']
- **Before GL:**
```
{
  "121000 - زمین - E": {
    "debit": 11781250000.0,
    "credit": 0.0,
    "n": 1
  },
  "211230 - معلق خرید دارایی ثابت - E": {
    "debit": 0.0,
    "credit": 11781250000.0,
    "n": 1
  }
}
```
- **After GL:**
```
{
  "121003 - ماشین آلات و تجهیزات تولید - E": {
    "debit": 11781250000.0,
    "credit": 0.0,
    "entries": [
      "3ff3a30623"
    ]
  },
  "211230 - معلق خرید دارایی ثابت - E": {
    "debit": 0.0,
    "credit": 11781250000.0,
    "entries": [
      "5f53db82df"
    ]
  }
}
```

### `MAT-PRE-2026-00880` — RECONCILED

- **Target:** 121006 — Furniture (اثاثیه ومنصوبات)
- **RIV:** `r7nb5olmha` status `Completed` accounting_only=`1` created `2026-11-10 00:17:07.914798`
- **Before Land debit (preflight):** 158000000.0
- **After Land debit:** 0
- **After target debit:** 158000000.0
- **After GRNI credit/debit:** 158000000.0 / 0.0
- **GL totals after:** debit 158000000.0 / credit 158000000.0 balanced=True
- **SLE count after:** 0 (Asset PR — SLE not expected)
- **Source FA expense accounts after:** ['121006 - اثاثیه ومنصوبات - E']
- **Before GL:**
```
{
  "121000 - زمین - E": {
    "debit": 158000000.0,
    "credit": 0.0,
    "n": 1
  },
  "211230 - معلق خرید دارایی ثابت - E": {
    "debit": 0.0,
    "credit": 158000000.0,
    "n": 1
  }
}
```
- **After GL:**
```
{
  "121006 - اثاثیه ومنصوبات - E": {
    "debit": 158000000.0,
    "credit": 0.0,
    "entries": [
      "12f0d22892"
    ]
  },
  "211230 - معلق خرید دارایی ثابت - E": {
    "debit": 0.0,
    "credit": 158000000.0,
    "entries": [
      "b98d6e0d96"
    ]
  }
}
```

### `MAT-PRE-2026-01241` — RECONCILED

- **Target:** 121003 — Machinery (ماشین آلات و تجهیزات تولید)
- **RIV:** `raj6kdech2` status `Completed` accounting_only=`1` created `2026-11-10 00:17:17.121399`
- **Before Land debit (preflight):** 1030000000.0
- **After Land debit:** 0
- **After target debit:** 1030000000.0
- **After GRNI credit/debit:** 1030000000.0 / 0.0
- **GL totals after:** debit 1030000000.0 / credit 1030000000.0 balanced=True
- **SLE count after:** 0 (Asset PR — SLE not expected)
- **Source FA expense accounts after:** ['121003 - ماشین آلات و تجهیزات تولید - E']
- **Before GL:**
```
{
  "121000 - زمین - E": {
    "debit": 1030000000.0,
    "credit": 0.0,
    "n": 1
  },
  "211230 - معلق خرید دارایی ثابت - E": {
    "debit": 0.0,
    "credit": 1030000000.0,
    "n": 1
  }
}
```
- **After GL:**
```
{
  "211230 - معلق خرید دارایی ثابت - E": {
    "debit": 0.0,
    "credit": 1030000000.0,
    "entries": [
      "1f3a048f91"
    ]
  },
  "121003 - ماشین آلات و تجهیزات تولید - E": {
    "debit": 1030000000.0,
    "credit": 0.0,
    "entries": [
      "80affe203c"
    ]
  }
}
```

### `MAT-PRE-2026-01266` — RECONCILED

- **Target:** 121006 — Furniture (اثاثیه ومنصوبات)
- **RIV:** `tlmpufh927` status `Completed` accounting_only=`1` created `2026-11-10 00:21:17.422743`
- **Before Land debit (preflight):** 1160000000.0
- **After Land debit:** 0
- **After target debit:** 1160000000.0
- **After GRNI credit/debit:** 1160000000.0 / 0.0
- **GL totals after:** debit 1160000000.0 / credit 1160000000.0 balanced=True
- **SLE count after:** 0 (Asset PR — SLE not expected)
- **Source FA expense accounts after:** ['121006 - اثاثیه ومنصوبات - E']
- **Before GL:**
```
{
  "121000 - زمین - E": {
    "debit": 1160000000.0,
    "credit": 0.0,
    "n": 1
  },
  "211230 - معلق خرید دارایی ثابت - E": {
    "debit": 0.0,
    "credit": 1160000000.0,
    "n": 1
  }
}
```
- **After GL:**
```
{
  "121006 - اثاثیه ومنصوبات - E": {
    "debit": 1160000000.0,
    "credit": 0.0,
    "entries": [
      "14ff85bb74"
    ]
  },
  "211230 - معلق خرید دارایی ثابت - E": {
    "debit": 0.0,
    "credit": 1160000000.0,
    "entries": [
      "531969ef59"
    ]
  }
}
```

## Absolute restrictions

- backup_waived_by_user
- no_cohort_B
- no_512_fractional_STE
- no_three_exceptional_MTfM
- no_manual_JE
- no_arbitrary_stock_adjustment
- no_rewrite_submitted_historical_docs_beyond_PRI_expense_account
- no_automatic_depreciation_rebuild
- never_touch_ACC-JV-1405-169046
- extension_version_kept_5.7.18
- no_git_history_change

## Notes

- Corrections applied via apply_asset_purchase_receipt_correction (confirm=APPLY_ASSET_PR_CORRECTION, dry_run=0) with per-row account_overrides to approved targets.
- Accounting-only Transaction RIV created and executed synchronously per voucher (repost_only_accounting_ledgers=1).
- MAT-PRE-2026-00747 FA debit after RIV is 1,225,500,000 IRR (PI amt_diff -64,500,000 applied by fa_pr_gl_amount_difference) with matching GRNI credit — empirical proof patch active on RIV path.
- MAT-PRE-2026-01311: GL account corrected; Case E asset acquisition/depreciation NOT rebuilt; historical depr preserved → RECONCILED_WITH_ASSET_EXCEPTION.
- All eight vouchers have Completed RIV jobs and verified Production GL.

