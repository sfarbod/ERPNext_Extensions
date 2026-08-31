/**
 * v4.8.3 PM Clearance Return for Correction + Remarks — Playwright Desk acceptance.
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { benchExecute } from "../../e2e/e2e_playwright_db.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "pm_clearance_return_remarks_v483");
const BASE =
  process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8001";
const PREP =
  "erpnext_extensions.petty_management.e2e.pm_clearance_return_remarks_v483_prep";

function bench(method, kwargs = null) {
  return benchExecute(method, kwargs);
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

async function login(page, email, password) {
  await page.goto(`${BASE}/login`, {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
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

async function openActionsMenu(page) {
  const btn = page.locator(".actions-btn-group .btn-primary").first();
  await btn.waitFor({ state: "visible", timeout: 90000 });
  await btn.click();
  await page.waitForTimeout(300);
}

async function openActionsMenuIfPresent(page) {
  const btn = page.locator(".actions-btn-group .btn-primary").first();
  if (!(await btn.count())) {
    return false;
  }
  try {
    await btn.waitFor({ state: "visible", timeout: 15000 });
  } catch (_e) {
    return false;
  }
  await btn.click();
  await page.waitForTimeout(300);
  return true;
}

async function actionMenuLabels(page) {
  return page.evaluate(() =>
    Array.from(
      document.querySelectorAll(".actions-btn-group .dropdown-menu a.dropdown-item")
    ).map((a) => (a.textContent || "").trim())
  );
}

async function remarkFieldReadOnly(page) {
  return page.evaluate(() => {
    const df = window.cur_frm?.fields_dict?.remark;
    return !!(df && (df.df?.read_only || df.disp_status === "Read Only"));
  });
}

async function setRemark(page, text) {
  return page.evaluate(async (value) => {
    await window.cur_frm.set_value("remark", value);
    await window.cur_frm.save();
    return window.cur_frm.doc.remark;
  }, text);
}

async function main() {
  const prep = bench(`${PREP}.prepare_v483_fixtures`);
  assert(prep?.pending_manager, "prep failed");

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    locale: "en-US",
    viewport: { width: 1600, height: 950 },
  });
  const page = await context.newPage();

  // Pending Manager — manager sees Return
  await login(page, prep.users.manager.email, prep.users.manager.password);
  await openClearance(page, prep.pending_manager);
  await openActionsMenu(page);
  let menu = await actionMenuLabels(page);
  assert(
    menu.some((t) => /Return for Correction/i.test(t)),
    `Pending Manager missing Return: ${menu.join(", ")}`
  );
  assert(
    !menu.some((t) => /^PM Reject$/i.test(t)),
    `Pending Manager must not show PM Reject: ${menu.join(", ")}`
  );
  await shot(page, "pending_manager_actions");

  const remarkPending = await remarkFieldReadOnly(page);
  assert(!remarkPending, "remark must be editable while pending");
  const savedRemark = await setRemark(page, "v483 pending remark");
  assert(savedRemark === "v483 pending remark", "remark save failed while pending");

  // Pending Finance Review — reviewer sees Return
  await context.close();
  const reviewerContext = await browser.newContext({
    locale: "en-US",
    viewport: { width: 1600, height: 950 },
  });
  const reviewerPage = await reviewerContext.newPage();
  await login(reviewerPage, prep.users.reviewer.email, prep.users.reviewer.password);
  await openClearance(reviewerPage, prep.pending_finance);
  assert(
    prep.server_actions?.pending_finance?.includes("PM Return for Correction"),
    `Server: Return missing on Pending Finance Review: ${JSON.stringify(prep.server_actions?.pending_finance)}`
  );
  await openActionsMenu(reviewerPage);
  menu = await actionMenuLabels(reviewerPage);
  assert(
    menu.some((t) => /Return for Correction/i.test(t)),
    `Pending Finance Review missing Return: ${menu.join(", ")}`
  );
  assert(
    menu.some((t) => /PM Finance Approve/i.test(t)),
    `Finance Approve missing: ${menu.join(", ")}`
  );
  await shot(reviewerPage, "pending_finance_actions");

  // Approved — no Return, remark read-only
  await openClearance(reviewerPage, prep.approved);
  if (await openActionsMenuIfPresent(reviewerPage)) {
    menu = await actionMenuLabels(reviewerPage);
    assert(
      !menu.some((t) => /Return for Correction/i.test(t)),
      `Approved must not show Return: ${menu.join(", ")}`
    );
  }
  const remarkApproved = await remarkFieldReadOnly(reviewerPage);
  assert(remarkApproved, "remark must be read-only after approve");
  await shot(reviewerPage, "approved_remark_readonly");

  // List view — remark column + filter search
  await reviewerPage.goto(`${BASE}/app/pm-clearance`, {
    waitUntil: "domcontentloaded",
    timeout: 180000,
  });
  await reviewerPage.waitForTimeout(2500);
  const listText = await reviewerPage.evaluate(() => document.body.innerText || "");
  assert(listText.includes(prep.remark_marker), "remark not visible in list");
  await shot(reviewerPage, "list_remark_column");

  await reviewerContext.close();
  await context.close();
  await browser.close();
  console.log("playwright_pm_clearance_return_remarks_v483 OK");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
