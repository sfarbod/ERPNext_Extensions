# Gap Analysis — Credit Control Allocation UI Completion

**Inspected:** `development.localhost` after commit `51f879c`  
**Version:** 5.8.5 (unchanged)

| Requirement | File / Codebase | Live Desk DB | Gap |
|---|---|---|---|
| A. Workspace menu for Credit Allocation | Present in `costing/workspace/costing/costing.json` | **Missing** from Workspace `Costing` links | JSON not synced into DB Workspace |
| A. Sidebar | `costing/sidebar/costing/costing.json` | No Control Credit entry | Missing |
| B. Labor/Overhead pool list | Parent List View exists (one row/month) | No Labor vs Overhead pool rows | Need Script Report unfolding persisted parent fields |
| C. Batch × credit pool details | `batch_rows` / `product_rows` child tables (istable) | Not list-browsable like Batch Expense | Need Script Report over persisted children |
| D. PA navigation | Four fields present | No link to credit pool / batch details | Add form buttons / report filters |
| Reports module | No Costing Script Reports | — | Create two reports |
| Allocation engine | Intact | 6 Calculated docs, 318 batch / 344 product rows | **Do not change** |

**Reuse:** Parent totals + child tables already store pool and allocation amounts. Reports/Workspace only.
