/**
 * v4.8.5 — Cancel PM Request business action E2E.
 */
import { chromium } from "/tmp/e2e-npm/node_modules/playwright/index.mjs";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import {
  benchExecute,
  getDocumentState,
  waitDocstatus,
  SITE,
} from "../../e2e/e2e_playwright_db.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "pm_request_cancel_action_v485");
const TRACE = path.join(__dirname, "traces", "pm_request_cancel_action_v485.zip");
const BASE = process.env.FRAPPE_E2E_BASE_URL || "http://127.0.0.1:8001";

async function login(page, email, password) {
  await page.goto(`${BASE}/login`, {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
  await page.fill("#login_email", email);
  await page.fill("#login_password", password);
  await page.click('button[type="submit"]');
  await page.waitForURL(/\/(app|desk)/, { timeout: 120000 });
}

async function shot(page, name) {
  fs.mkdirSync(SCREEN, { recursive: true });
  const p = path.join(SCREEN, `${name}.png`);
  await page.screenshot({ path: p, fullPage: true });
  return p;
}

async function openPmRequest(page, name) {
  await page.goto(`${BASE}/app/pm-request/${encodeURIComponent(name)}`, {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
  await page.waitForFunction(
    (n) =>
      window.cur_frm?.doc?.doctype === "PM Request" &&
      window.cur_frm.doc.name === n &&
      !window.cur_frm.is_loading,
    name,
    { timeout: 180000 }
  );
  await page.evaluate(async () => {
    if (window.cur_frm?.trigger) {
      await window.cur_frm.trigger("setup_pm_request_toolbar");
    }
  });
  await page.waitForTimeout(1500);
}

async function isCancelPmRequestVisible(page) {
  return page.evaluate(() => {
    const roots = [document.querySelector(".page-head"), document.body].filter(Boolean);
    for (const root of roots) {
      const els = root.querySelectorAll("button, a, .btn");
      for (const el of els) {
        const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
        if (/^Cancel PM Request$/i.test(t)) {
          return true;
        }
      }
    }
    return false;
  });
}

async function genericCancelVisible(page) {
  return page.evaluate(() => {
    const sec = document.querySelector(".page-actions .btn-secondary, .page-head .btn-secondary");
    if (!sec) return false;
    return /^Cancel$/i.test((sec.innerText || sec.textContent || "").trim());
  });
}

async function clickCancelPmRequest(page) {
  await page.getByRole("button", { name: /^Cancel PM Request$/i }).first().click({ timeout: 30000 });
  const modal = page.locator(".modal-dialog:visible").first();
  await modal.waitFor({ timeout: 60000 });
  await modal.locator("button.btn-primary").first().click();
}

async function main() {
  fs.mkdirSync(SCREEN, { recursive: true });
  const results = [];
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    locale: "en-US",
    viewport: { width: 1600, height: 950 },
    extraHTTPHeaders: { "X-Frappe-Site-Name": SITE },
  });
  await context.tracing.start({ screenshots: true, snapshots: true });
  const page = await context.newPage();

  try {
    const visible = benchExecute(
      "erpnext_extensions.petty_management.e2e.pm_request_cancel_action_v485_prep.prepare_cancel_action_visible"
    );
    await login(page, visible.user.email, visible.user.password);
    await openPmRequest(page, visible.pm_request);
    const beforeVisible = await isCancelPmRequestVisible(page);
    const genericBefore = await genericCancelVisible(page);
    await shot(page, "01_before_cancel_visible");
    results.push({
      test: "cancel_visible_unfunded",
      pass: beforeVisible === true && genericBefore === false,
      beforeVisible,
      genericBefore,
    });
    await clickCancelPmRequest(page);
    await waitDocstatus("PM Request", visible.pm_request, 2, { timeoutMs: 180000 });
    const after = getDocumentState("PM Request", visible.pm_request, [
      "docstatus",
      "status",
      "workflow_state",
    ]);
    await shot(page, "02_after_cancel_success");
    results.push({
      test: "cancel_success",
      pass: after.docstatus === 2 && after.status === "Cancelled",
      db: after,
    });

    const funded = benchExecute(
      "erpnext_extensions.petty_management.e2e.pm_request_cancel_action_v485_prep.prepare_cancel_action_hidden_funded"
    );
    await login(page, funded.user.email, funded.user.password);
    await openPmRequest(page, funded.pm_request);
    const fundedVisible = await isCancelPmRequestVisible(page);
    await shot(page, "03_funded_cancel_hidden");
    results.push({
      test: "cancel_hidden_funded",
      pass: fundedVisible === false,
      fundedVisible,
    });

    const closed = benchExecute(
      "erpnext_extensions.petty_management.e2e.pm_request_cancel_action_v485_prep.prepare_cancel_action_hidden_closed"
    );
    await login(page, closed.user.email, closed.user.password);
    await openPmRequest(page, closed.pm_request);
    const closedVisible = await isCancelPmRequestVisible(page);
    await shot(page, "04_closed_cancel_hidden");
    results.push({
      test: "cancel_hidden_closed",
      pass: closedVisible === false,
      closedVisible,
    });

    const draftClr = benchExecute(
      "erpnext_extensions.petty_management.e2e.pm_request_cancel_action_v485_prep.prepare_cancel_action_hidden_draft_clearance"
    );
    if (!draftClr.skipped) {
      await login(page, draftClr.user.email, draftClr.user.password);
      await openPmRequest(page, draftClr.pm_request);
      const draftClrVisible = await isCancelPmRequestVisible(page);
      await shot(page, "05_draft_clearance_cancel_hidden");
      results.push({
        test: "cancel_hidden_draft_clearance",
        pass: draftClrVisible === false,
        draftClrVisible,
      });
    } else {
      results.push({
        test: "cancel_hidden_draft_clearance",
        pass: true,
        skipped: true,
        skip_reason: draftClr.skip_reason,
      });
    }

    const failed = results.filter((r) => !r.pass);
    console.log(JSON.stringify({ ok: failed.length === 0, results }, null, 2));
    if (failed.length) {
      process.exitCode = 1;
    }
  } catch (err) {
    try {
      await shot(page, "99_failure");
    } catch {
      /* ignore */
    }
    console.error(
      JSON.stringify(
        { ok: false, error: String(err && err.message ? err.message : err), results },
        null,
        2
      )
    );
    process.exitCode = 1;
  } finally {
    try {
      await context.tracing.stop({ path: TRACE });
    } catch {
      /* ignore */
    }
    await browser.close();
  }
}

main();
