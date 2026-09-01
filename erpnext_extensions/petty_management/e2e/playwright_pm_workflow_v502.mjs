/**
 * v5.0.2 PM Request / PM Clearance workflow Return — Playwright + DB assertions.
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { benchExecutePrep } from "../../e2e/e2e_playwright_db.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "pm_workflow_v502");
const BASE = process.env.FRAPPE_E2E_BASE_URL || "http://127.0.0.1:8000";
const PREP = "erpnext_extensions.petty_management.e2e.pm_workflow_v502_prep";

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function prep(method, kwargs = null) {
  return benchExecutePrep(`${PREP}.${method}`, kwargs);
}

async function login(page, email, password) {
  await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded", timeout: 120000 });
  await page.waitForSelector("#login_email", { state: "visible", timeout: 120000 });
  await page.locator("#login_email").first().fill(email, { timeout: 60000 });
  await page.locator("#login_password, input[type='password']").first().fill(password, {
    timeout: 60000,
  });
  await page.locator('button[type="submit"]').first().click();
  await page.waitForURL(/\/(app|desk)/, { timeout: 120000 });
}

async function openDoc(page, route, name) {
  await page.goto(`${BASE}/app/${route}/${encodeURIComponent(name)}`, {
    waitUntil: "domcontentloaded",
    timeout: 180000,
  });
  await page.waitForFunction(
    (n) => window.cur_frm?.doc?.name === n && !window.cur_frm.is_loading,
    name,
    { timeout: 180000 }
  );
}

async function shot(page, name) {
  fs.mkdirSync(SCREEN, { recursive: true });
  const p = path.join(SCREEN, `${name}.png`);
  await page.screenshot({ path: p, fullPage: true });
  return p;
}

async function main() {
  const fixtures = prep("prepare_v502_fixtures");
  const results = { fixtures: { run_id: fixtures.run_id }, scenarios: {} };
  const browser = await chromium.launch({ headless: true });

  // --- PM Request: Invalid Manager (server-side fail-fast) ---
  const invalid = prep("attempt_invalid_manager_submit", {
    pm_request: fixtures.request_invalid_draft,
    holder_email: fixtures.users.holder.email,
  });
  results.scenarios.request_invalid_manager = invalid;
  assert(invalid.ok, `invalid manager submit should fail: ${invalid.error}`);
  assert(invalid.validation_message_clear, `unclear validation: ${invalid.error}`);
  assert(invalid.workflow_title === "Draft", `workflow should stay Draft: ${invalid.workflow_title}`);
  assert(
    invalid.open_todos_after === invalid.open_todos_before,
    "invalid submit must not create ToDo"
  );

  // --- PM Request: Manager Return (UI open + server Return) ---
  {
    const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 } });
    const page = await ctx.newPage();
    await login(page, fixtures.users.manager_good.email, fixtures.password);
    await openDoc(page, "pm-request", fixtures.request_pending_manager);
    await shot(page, "request_pending_manager_open");
    const canRead = await page.evaluate(() => !!window.cur_frm?.doc?.name);
    assert(canRead, "manager could not open PM Request");
    const serverActions = prep("workflow_actions", {
      doctype: "PM Request",
      name: fixtures.request_pending_manager,
      user: fixtures.users.manager_good.email,
    });
    assert(
      serverActions.includes("PM Return for Correction"),
      `Return missing on server: ${serverActions.join(", ")}`
    );
    await ctx.close();

    const applied = prep("apply_return_as_user", {
      doctype: "PM Request",
      name: fixtures.request_pending_manager,
      user: fixtures.users.manager_good.email,
    });
    const after = prep("get_request_snapshot", { pm_request: fixtures.request_pending_manager });
    results.scenarios.request_manager_return = { serverActions, applied, after, ui_open: canRead };
    assert(applied.workflow_title === "Draft", `expected Draft, got ${applied.workflow_title}`);
    assert(!after.manager_approver, "manager stamp should be cleared");
    assert(
      after.open_todos.some((t) => t.allocated_to === fixtures.users.holder.email),
      "requester ToDo missing"
    );
    assert(
      prep("has_return_timeline_marker", {
        doctype: "PM Request",
        name: fixtures.request_pending_manager,
      }),
      "Return timeline comment missing"
    );
  }

  // --- PM Clearance: Manager Return ---
  if (fixtures.clearance_pending_manager) {
    const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 } });
    const page = await ctx.newPage();
    await login(page, fixtures.users.manager_good.email, fixtures.password);
    await openDoc(page, "pm-clearance", fixtures.clearance_pending_manager);
    const canRead = await page.evaluate(() => !!window.cur_frm?.doc?.name);
    assert(canRead, "manager could not open PM Clearance");
    await ctx.close();

    prep("apply_return_as_user", {
      doctype: "PM Clearance",
      name: fixtures.clearance_pending_manager,
      user: fixtures.users.manager_good.email,
    });
    const after = prep("get_clearance_snapshot", {
      pm_clearance: fixtures.clearance_pending_manager,
    });
    results.scenarios.clearance_manager_return = { after, ui_open: canRead };
    assert(after.workflow_title === "Draft", `clearance Draft expected, got ${after.workflow_title}`);
    assert(
      after.open_todos.includes(fixtures.users.holder.email),
      "clearance requester ToDo missing after manager return"
    );
    assert(!after.manager_approver, "clearance manager stamp should be cleared");
  } else {
    results.scenarios.clearance_manager_return = {
      skipped: true,
      reason: fixtures.clearance_skipped,
    };
  }

  // --- PM Clearance: Finance Return ---
  if (fixtures.clearance_pending_finance) {
    const serverActions = prep("workflow_actions", {
      doctype: "PM Clearance",
      name: fixtures.clearance_pending_finance,
      user: fixtures.users.reviewer.email,
    });
    assert(
      serverActions.includes("PM Return for Correction"),
      `server Return missing: ${serverActions.join(", ")}`
    );
    const ctx = await browser.newContext({ viewport: { width: 1600, height: 950 } });
    const page = await ctx.newPage();
    await login(page, fixtures.users.reviewer.email, fixtures.password);
    await openDoc(page, "pm-clearance", fixtures.clearance_pending_finance);
    const canRead = await page.evaluate(() => !!window.cur_frm?.doc?.name);
    assert(canRead, "finance reviewer could not open PM Clearance");
    await ctx.close();

    prep("apply_return_as_user", {
      doctype: "PM Clearance",
      name: fixtures.clearance_pending_finance,
      user: fixtures.users.reviewer.email,
    });
    const after = prep("get_clearance_snapshot", {
      pm_clearance: fixtures.clearance_pending_finance,
    });
    results.scenarios.clearance_finance_return = { serverActions, after, ui_open: canRead };
    assert(after.workflow_title === "Draft", `finance return Draft expected, got ${after.workflow_title}`);
    assert(
      after.open_todos.includes(fixtures.users.holder.email),
      "finance return requester ToDo missing"
    );
    assert(!after.manager_approver, "finance return should clear manager stamp");
    assert(!after.finance_approver, "finance return should clear finance stamp");
  } else {
    results.scenarios.clearance_finance_return = {
      skipped: true,
      reason: fixtures.clearance_skipped,
    };
  }

  await browser.close();

  // --- Role drift (server-side) ---
  const drift = prep("run_role_drift_request", {
    pm_request: fixtures.request_pending_drift,
    manager_email: fixtures.users.manager_good.email,
  });
  results.scenarios.role_drift_request = drift;
  assert(drift.ok, `role drift should fail: ${drift.error}`);
  assert(drift.still_pending, "role drift must not mutate workflow");
  assert(drift.message_clear, `unclear drift message: ${drift.error}`);

  // --- Atomicity (server-side) ---
  const atomRequest = prep("run_atomicity_request", {
    pm_request: fixtures.request_pending_atomicity,
    manager_email: fixtures.users.manager_good.email,
  });
  results.scenarios.atomicity_request = atomRequest;
  assert(atomRequest.ok, atomRequest.error);
  assert(atomRequest.workflow_unchanged, "request workflow changed on assign failure");
  assert(atomRequest.docstatus_unchanged, "request docstatus changed on assign failure");
  assert(atomRequest.stamps_unchanged, "request stamps changed on assign failure");
  assert(atomRequest.todos_unchanged, "request ToDos changed on assign failure");
  assert(atomRequest.no_timeline, "request timeline written on assign failure");

  if (fixtures.clearance_pending_atomicity) {
    const atomClearance = prep("run_atomicity_clearance", {
      pm_clearance: fixtures.clearance_pending_atomicity,
      reviewer_email: fixtures.users.reviewer.email,
    });
    results.scenarios.atomicity_clearance = atomClearance;
    assert(atomClearance.ok, atomClearance.error);
    assert(atomClearance.workflow_unchanged, "clearance workflow changed on assign failure");
    assert(atomClearance.docstatus_unchanged, "clearance docstatus changed on assign failure");
    assert(atomClearance.stamps_unchanged, "clearance stamps changed on assign failure");
    assert(atomClearance.todos_unchanged, "clearance ToDos changed on assign failure");
    assert(atomClearance.no_timeline, "clearance timeline written on assign failure");
  } else {
    results.scenarios.atomicity_clearance = { skipped: true, reason: fixtures.clearance_skipped };
  }

  console.log(JSON.stringify({ ok: true, results }, null, 2));
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
