# Petty Management — QA environment prerequisites

Non-functional QA notes for PM Request cancel (v4.8.5) and the full PM regression suite.

## Buying Settings

Cancel/clearance unit tests and Playwright prep scripts **temporarily set and restore**:

| Field | Test value | Reason |
|-------|------------|--------|
| `po_required` | `No` | Allows Purchase Invoice fixtures without Purchase Order |

Purchase Invoice fixtures also set `remarks` (site may mark the field mandatory).

Tests must **not** depend on the site's persisted Buying Settings.

## DocPerm assumptions

Cancel QA asserts **DocType JSON** Role Permissions (`tabDocPerm` on the DocType document), **not** effective permissions from site `Custom DocPerm`.

| Role | JSON `cancel` | JSON `delete` | Notes |
|------|---------------|---------------|-------|
| Petty Management Accountant | 1 | 0 | v4.8.5 business cancel is DocPerm-independent at runtime |
| Petty Management User | 0 | 0 | Requester must not generic-cancel |

Site `Custom DocPerm` overrides (e.g. Accountant `cancel=0`) are **not** release blockers and are ignored by cancel QA tests.

## Playwright prerequisites

| Item | Requirement |
|------|-------------|
| Bench | `FRAPPE_BENCH_ROOT` (default `/workspace/development/frappe-bench`) |
| Site | `FRAPPE_E2E_SITE` (default `development.localhost`) |
| Base URL | `FRAPPE_E2E_BASE_URL` — must match the running `bench serve` port (e.g. `http://development.localhost:8000`) |
| Node deps | Playwright installed at `/tmp/e2e-npm/node_modules/playwright` |
| Browsers | `PLAYWRIGHT_BROWSERS_PATH` (default `/home/frappe/.cache/ms-playwright`) |
| Bench serve | Port matching `FRAPPE_E2E_BASE_URL` must be running |

Run the pack:

```bash
cd erpnext_extensions/petty_management/e2e
bash run_pm_playwright_regression.sh /tmp/pm_playwright_regression.txt
```

Cancel action E2E (also included in the pack):

```bash
node playwright_pm_request_cancel_action_v485.mjs
```

## ulimit / EMFILE

The Playwright regression runner performs an **EMFILE preflight** before starting.

| Setting | Recommendation |
|---------|----------------|
| `ulimit -n` | ≥ 4096 (`PM_PLAYWRIGHT_MIN_ULIMIT`) |
| Per-script ulimit | `PM_PLAYWRIGHT_ULIMIT` (default 8192) in runner subshell |

If preflight fails with `EMFILE`, restart the dev container / bench processes before re-running QA. This is an **infrastructure** issue, not a product defect.

## Unit / integration — PM Request cancel

```bash
bench --site development.localhost run-tests \
  --module erpnext_extensions.petty_management.tests.test_pm_request_cancel_action_v485 \
  --skip-before-tests

bench --site development.localhost run-tests \
  --module erpnext_extensions.petty_management.tests.test_pm_request_cancel_delete \
  --skip-before-tests
```

## Unit / integration — PM Request delete (v4.8.6)

Delete is **Administrator only**. PM Accountant may cancel but must not delete (action hidden + API denied).

```bash
bench --site development.localhost run-tests \
  --module erpnext_extensions.petty_management.tests.test_pm_request_delete_action_v486 \
  --skip-before-tests

bench --site development.localhost run-tests \
  --module erpnext_extensions.petty_management.tests.test_pm_request_connections_v486 \
  --skip-before-tests
```

Finalize Playwright (Actions menu + Connections tab):

```bash
node playwright_pm_request_finalize_v486.mjs
```

Full PM module regression:

```bash
cd erpnext_extensions/petty_management/smoke
bash run_pm_regression.sh /tmp/pm_regression_results.txt
```
