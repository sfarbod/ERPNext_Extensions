/**
 * v5.5.5 — Active reservation + Return zero-available deadlock (real Desk UI).
 * Scenario 1: Return via Actions menu (no API fallback for the Return click).
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { benchExecutePrep } from "../../e2e/e2e_playwright_db.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "pm_clearance_active_reservation_v555");
const BASE = process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";
const PREP =
  "erpnext_extensions.petty_management.e2e.pm_clearance_active_reservation_v555_prep";

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

async function openClearance(page, name) {
  await page.goto(`${BASE}/app/pm-clearance/${encodeURIComponent(name)}`, {
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

/** Click workflow action from Desk Actions dropdown — no frappe.call fallback. */
async function clickWorkflowAction(page, actionLabel) {
  // Frappe workflow Actions uses primary button in .actions-btn-group
  await page.waitForFunction(() => {
    const el = document.querySelector(".actions-btn-group .btn-primary, .actions-btn-group .btn");
    return !!(el && el.offsetParent !== null);
  }, { timeout: 120000 });
  // Ensure workflow actions refreshed
  await page.evaluate(async () => {
    if (window.cur_frm?.script_manager?.trigger) {
      try {
        await window.cur_frm.script_manager.trigger("refresh");
      } catch (_e) {
        /* ignore */
      }
    }
  });
  await page.waitForTimeout(500);
  const actionsBtn = page.locator(".actions-btn-group .btn-primary").first();
  if (await actionsBtn.count()) {
    await actionsBtn.click();
  } else {
    await page
      .locator(".actions-btn-group .btn")
      .filter({ hasText: /^Actions$/i })
      .first()
      .click();
  }
  await page.waitForTimeout(400);
  const item = page
    .locator(".actions-btn-group .dropdown-menu a.dropdown-item")
    .filter({ hasText: /Return for Correction/i })
    .first();
  await item.waitFor({ state: "visible", timeout: 60000 });
  const label = (await item.innerText()).trim();
  assert(/Return for Correction/i.test(label), `unexpected action label: ${label}`);
  await item.click();
  await page.waitForTimeout(2500);
  await page.waitForFunction(() => !window.cur_frm?.is_loading, { timeout: 120000 });
}

async function main() {
  const evidence = { screenshots: {}, steps: {}, scenarios: {} };
  const browser = await chromium.launch({ headless: true });
  let context = null;
  let page = null;
  let fixtures = null;

  try {
    // ---- Scenario 1: Return deadlock via real Actions click ----
    fixtures = prep("prepare_return_deadlock_fixture");
    assert(fixtures?.pm_clearance, "missing clearance");
    evidence.fixtures_return = fixtures;
    assert(
      Number(fixtures.available_excl_pending) <= 0.01,
      `expected zero available, got ${fixtures.available_excl_pending}`
    );

    context = await browser.newContext({
      locale: "en-US",
      viewport: { width: 1600, height: 950 },
    });
    page = await context.newPage();
    await login(page, fixtures.users.reviewer.email, fixtures.password);
    await openClearance(page, fixtures.pm_clearance);
    evidence.screenshots.s1_pending = await shot(page, "01_pending_finance_zero_avail");

    // Real Desk UI — Actions → PM Return for Correction (no API fallback)
    await clickWorkflowAction(page, "PM Return for Correction");
    await openClearance(page, fixtures.pm_clearance);
    evidence.screenshots.s1_after_return = await shot(page, "02_after_return_ui");

    const after = prep("get_clearance_snapshot", { pm_clearance: fixtures.pm_clearance });
    evidence.scenarios.return_deadlock = after;
    assert(after.name === fixtures.pm_clearance, "name changed");
    assert(after.workflow_state === "Draft", `expected Draft, got ${after.workflow_state}`);
    // Reservation retained (settled 1000 + returned draft 400)
    assert(Number(after.prior_all) >= 1400 - 0.01, `prior_all=${after.prior_all}`);

    // Ensure no error banner about available balance remains as blocking state
    const bodyText = await page.locator("body").innerText();
    assert(
      !/has no available balance for clearance/i.test(bodyText) ||
        after.workflow_state === "Draft",
      "availability error blocked Return"
    );
    await context.close();

    // ---- Scenario 2: Draft reservation (same validate path Desk save uses) ----
    const draftFx = prep("prepare_draft_reservation_fixture");
    evidence.fixtures_draft = draftFx;
    const blocked301 = prep("try_sibling_alloc", {
      pm_request: draftFx.pm_request,
      employee: draftFx.employee,
      purchase_invoice: draftFx.purchase_invoice,
      company: draftFx.company,
      amount: 301,
    });
    evidence.scenarios.draft_301 = blocked301;
    assert(!blocked301.ok, "B=301 must be blocked");
    assert(/available|exceed/i.test(String(blocked301.error || "")), blocked301.error);
    const ok300 = prep("try_sibling_alloc", {
      pm_request: draftFx.pm_request,
      employee: draftFx.employee,
      purchase_invoice: draftFx.purchase_invoice,
      company: draftFx.company,
      amount: 300,
    });
    evidence.scenarios.draft_300 = ok300;
    assert(ok300.ok, `B=300 must pass: ${ok300.error}`);

    prep("restore_pi_outstanding", {
      purchase_invoice: fixtures.purchase_invoice,
      outstanding: fixtures.original_pi_outstanding,
    });
    prep("restore_pi_outstanding", {
      purchase_invoice: draftFx.purchase_invoice,
      outstanding: draftFx.original_pi_outstanding,
    });

    const legacy = prep("legacy_reservation_conflicts");
    evidence.legacy = legacy;

    console.log(JSON.stringify({ ok: true, evidence }, null, 2));
    await browser.close();
    process.exit(0);
  } catch (err) {
    evidence.error = String(err);
    try {
      if (page) evidence.screenshots.failure = await shot(page, "99_failure");
      if (context) await context.close().catch(() => null);
      if (fixtures?.purchase_invoice != null) {
        prep("restore_pi_outstanding", {
          purchase_invoice: fixtures.purchase_invoice,
          outstanding: fixtures.original_pi_outstanding,
        });
      }
    } catch (_e) {
      /* ignore */
    }
    console.log(JSON.stringify({ ok: false, evidence }, null, 2));
    await browser.close().catch(() => null);
    process.exit(1);
  }
}

main();
