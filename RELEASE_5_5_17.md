# erpnext_extensions 5.5.17

## Stock Entry Dimension Repair — Desk page deployability

### Summary

- Ensure the standard Desk Page `stock-entry-dimension-repair` is created/synced on migrate
  (filesystem Page JSON + idempotent navigation patch that imports the Page if missing).
- Improve the Desk UI sections (Scan / Preview / Dry Run / Apply) with clearer GL tables,
  mixed-row highlighting, item names, and Apply confirmation text.
- Harden `make_app_page` against invalid container locales (`en-US@posix` / Intl.Locale).
- Add page/navigation automated tests (P1–P12) and a Playwright smoke script.

### Access

- Route: `/app/stock-entry-dimension-repair` (Frappe v16 may redirect to `/desk/...`)
- Navigation: Stock / Manufacturing workspaces under **Repair Tools**; Production Control sidebar
- Roles: System Manager, Accounts Manager, Administrator

### Non-goals / unchanged

- Guard + Repair APIs unchanged in contract
- No real Apply executed as part of this release verification on canary 40149
