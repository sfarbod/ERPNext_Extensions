/**
 * v4.8.6 — Delete PM Request business action E2E (Administrator only; Actions menu).
 */
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import {
  benchExecute,
  documentExists,
  SITE,
} from "../../e2e/e2e_playwright_db.mjs";
import { runWithPlaywrightBrowser } from "./playwright_pm_harness.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "pm_request_delete_action_v486");
const TRACE = path.join(__dirname, "traces", "pm_request_delete_action_v486.zip");
const BASE = process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";

async function login(page, email, password) {
  await page.context().clearCookies();
  await page.goto(`${BASE}/login`, {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
  await page.waitForSelector("#login_email", { timeout: 120000 });
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

async function openActionsMenu(page) {
  const btn = page.locator(".actions-btn-group .btn, button:has-text('Actions')").first();
  if ((await btn.count()) && (await btn.isVisible())) {
    await btn.click();
    await page.waitForTimeout(400);
  }
}

async function isDeletePmRequestVisible(page) {
  await openActionsMenu(page);
  return page.evaluate(() => {
    const items = document.querySelectorAll(
      ".dropdown-menu.show .dropdown-item, .actions-btn-group .dropdown-menu .dropdown-item"
    );
    for (const el of items) {
      const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      if (/^Delete PM Request$/i.test(t)) {
        return true;
      }
    }
    return false;
  });
}

async function genericDeleteVisible(page) {
  return page.evaluate(() => {
    const link = document.querySelector('.menu-btn-group a[data-label="Delete"], a[data-label="Delete"]');
    if (!link) {
      return false;
    }
    const li = link.closest("li");
    return li ? li.offsetParent !== null && getComputedStyle(li).display !== "none" : true;
  });
}

async function main() {
  fs.mkdirSync(SCREEN, { recursive: true });
  const results = [];

  try {
    await runWithPlaywrightBrowser(
      async ({ page }) => {
        const acct = benchExecute(
          "erpnext_extensions.petty_management.e2e.pm_request_delete_action_v486_prep.prepare_delete_action_accountant_eligible"
        );
        await login(page, acct.user.email, acct.user.password);
        await openPmRequest(page, acct.pm_request);
        const visible = await isDeletePmRequestVisible(page);
        const genericDelete = await genericDeleteVisible(page);
        await shot(page, "01_accountant_delete_hidden");
        results.push({
          test: "accountant_delete_hidden",
          pass: visible === false && genericDelete === false,
          visible,
          genericDelete,
        });

        const blocked = benchExecute(
          "erpnext_extensions.petty_management.e2e.pm_request_delete_action_v486_prep.prepare_delete_action_hidden_pe_history"
        );
        await login(page, blocked.user.email, blocked.user.password);
        await openPmRequest(page, blocked.pm_request);
        const blockedVisible = await isDeletePmRequestVisible(page);
        await shot(page, "02_pe_history_delete_hidden");
        results.push({
          test: "accountant_delete_hidden_pe_history",
          pass: blockedVisible === false,
          blockedVisible,
        });

        const requester = benchExecute(
          "erpnext_extensions.petty_management.e2e.pm_request_delete_action_v486_prep.prepare_delete_action_requester_blocked"
        );
        const requesterFlags = benchExecute(
          "erpnext_extensions.petty_management.e2e.pm_request_delete_action_v486_prep.check_delete_action_flags_as_user",
          { pm_request: requester.pm_request, user: requester.user.email }
        );
        results.push({
          test: "requester_delete_hidden",
          pass:
            requesterFlags.may_execute === false &&
            requesterFlags.can_delete_pm_request === false,
          requesterFlags,
        });

        const admin = benchExecute(
          "erpnext_extensions.petty_management.e2e.pm_request_delete_action_v486_prep.prepare_delete_action_administrator"
        );
        const adminFlags = benchExecute(
          "erpnext_extensions.petty_management.e2e.pm_request_delete_action_v486_prep.check_delete_action_flags_as_user",
          { pm_request: admin.pm_request, user: "Administrator" }
        );
        results.push({
          test: "administrator_delete_visible",
          pass:
            adminFlags.may_execute === true && adminFlags.can_delete_pm_request === true,
          adminFlags,
        });
        const adminDelete = benchExecute(
          "erpnext_extensions.petty_management.e2e.pm_request_delete_action_v486_prep.execute_delete_pm_request_as_user",
          { pm_request: admin.pm_request, user: "Administrator" }
        );
        results.push({
          test: "administrator_delete_removed",
          pass: adminDelete.exists === false,
          adminDelete,
        });
      },
      {
        trace: true,
        tracePath: TRACE,
        context: {
          locale: "en-US",
          viewport: { width: 1600, height: 950 },
          extraHTTPHeaders: { "X-Frappe-Site-Name": SITE },
        },
      }
    );

    const failed = results.filter((r) => !r.pass);
    console.log(JSON.stringify({ ok: failed.length === 0, results }, null, 2));
    if (failed.length) {
      process.exitCode = 1;
    }
  } catch (err) {
    console.error(
      JSON.stringify(
        { ok: false, error: String(err && err.message ? err.message : err), results },
        null,
        2
      )
    );
    process.exitCode = 1;
  }
}

main();
