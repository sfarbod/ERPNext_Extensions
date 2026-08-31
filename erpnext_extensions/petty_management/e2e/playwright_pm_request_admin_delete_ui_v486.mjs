/**
 * v4.8.6 — Administrator Desk UI delete E2E (browser-only verification; no API fallback).
 */
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { benchExecute, SITE } from "../../e2e/e2e_playwright_db.mjs";
import { runWithPlaywrightBrowser } from "./playwright_pm_harness.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "pm_request_admin_delete_ui_v486");
const TRACE = path.join(__dirname, "traces", "pm_request_admin_delete_ui_v486.zip");
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
  await page.waitForURL(/\/(app|desk)/, { timeout: 120000, waitUntil: "domcontentloaded" });
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
  await page.waitForTimeout(1200);
}

async function openActionsMenu(page) {
  const btn = page.locator(".actions-btn-group .btn, button:has-text('Actions')").first();
  if ((await btn.count()) && (await btn.isVisible())) {
    await btn.click();
    await page.waitForTimeout(400);
  }
}

async function isActionsMenuItemVisible(page, label) {
  await openActionsMenu(page);
  return page.evaluate((text) => {
    const items = document.querySelectorAll(
      ".dropdown-menu.show .dropdown-item, .actions-btn-group .dropdown-menu .dropdown-item"
    );
    for (const el of items) {
      const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      if (new RegExp(`^${text}$`, "i").test(t)) {
        const li = el.closest("li");
        if (li && (li.offsetParent === null || getComputedStyle(li).display === "none")) {
          continue;
        }
        return true;
      }
    }
    return false;
  }, label);
}

async function clickActionsMenuItem(page, label) {
  await openActionsMenu(page);
  const clicked = await page.evaluate((text) => {
    const items = document.querySelectorAll(
      ".dropdown-menu.show .dropdown-item, .actions-btn-group .dropdown-menu .dropdown-item"
    );
    for (const el of items) {
      const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      if (new RegExp(`^${text}$`, "i").test(t)) {
        el.click();
        return true;
      }
    }
    return false;
  }, label);
  if (!clicked) {
    await openActionsMenu(page);
    await page
      .locator(".dropdown-menu.show .dropdown-item, .actions-btn-group .dropdown-menu .dropdown-item")
      .filter({ hasText: new RegExp(`^${label}$`, "i") })
      .first()
      .click({ timeout: 30000 });
  }
}

async function clickConfirmModal(page) {
  const modal = page.locator(".modal-dialog:visible").first();
  await modal.waitFor({ timeout: 60000 });
  await modal.locator("button.btn-primary").first().click();
}

async function waitForDeleteAction(page, expectVisible, timeoutMs = 90000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const visible = await isActionsMenuItemVisible(page, "Delete PM Request");
    if (visible === expectVisible) {
      return visible;
    }
    await page.evaluate(async () => {
      if (window.cur_frm?.trigger) {
        await window.cur_frm.trigger("setup_pm_request_toolbar");
      }
    });
    await page.waitForTimeout(1500);
  }
  return isActionsMenuItemVisible(page, "Delete PM Request");
}

async function waitForCancelAction(page, expectVisible, timeoutMs = 90000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const visible = await isActionsMenuItemVisible(page, "Cancel PM Request");
    if (visible === expectVisible) {
      return visible;
    }
    await page.evaluate(async () => {
      if (window.cur_frm?.trigger) {
        await window.cur_frm.trigger("setup_pm_request_toolbar");
      }
    });
    await page.waitForTimeout(1500);
  }
  return isActionsMenuItemVisible(page, "Cancel PM Request");
}

async function searchListShowsNoResult(page, name) {
  await page.goto(`${BASE}/app/pm-request/view/list`, {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
  await page.waitForTimeout(2500);
  return page.evaluate((docName) => {
    const body = document.body.innerText || "";
    if (!body.includes(docName)) {
      return true;
    }
    const rows = document.querySelectorAll(".list-row-container, .list-row, [data-name]");
    for (const row of rows) {
      const dn = row.getAttribute("data-name") || row.innerText || "";
      if (dn.includes(docName)) {
        return false;
      }
    }
    return !body.includes(docName);
  }, name);
}

async function openUrlShowsNotFound(page, name) {
  await page.goto(`${BASE}/app/pm-request/${encodeURIComponent(name)}`, {
    waitUntil: "domcontentloaded",
    timeout: 120000,
  });
  await page.waitForTimeout(2500);
  return page.evaluate(() => {
    const body = (document.body.innerText || "").toLowerCase();
    if (/not found|does not exist|invalid|missing|cannot find|no permission/i.test(body)) {
      return true;
    }
    if (window.cur_frm?.doc?.name) {
      return false;
    }
    return !document.querySelector(".form-layout");
  });
}

async function main() {
  fs.mkdirSync(SCREEN, { recursive: true });
  const results = [];
  const screenshots = [];

  try {
    await runWithPlaywrightBrowser(
      async ({ page }) => {
        const ctx = benchExecute(
          "erpnext_extensions.petty_management.e2e.pm_request_admin_delete_ui_v486_prep.prepare_admin_ui_delete_submitted_request"
        );
        const { pm_request: reqName, administrator: admin } = ctx;

        await login(page, admin.email, admin.password);
        await openPmRequest(page, reqName);

        const cancelBefore = await isActionsMenuItemVisible(page, "Cancel PM Request");
        const deleteBefore = await isActionsMenuItemVisible(page, "Delete PM Request");
        screenshots.push(await shot(page, "01_submitted_before_cancel"));
        results.push({
          test: "submitted_cancel_visible_delete_hidden",
          pass: cancelBefore && !deleteBefore,
          cancelBefore,
          deleteBefore,
        });

        await clickActionsMenuItem(page, "Cancel PM Request");
        await clickConfirmModal(page);
        await page.waitForFunction(
          (ds) => window.cur_frm?.doc?.docstatus === ds,
          2,
          { timeout: 120000 }
        );
        await page.waitForTimeout(2000);
        await openPmRequest(page, reqName);

        const cancelAfter = await waitForCancelAction(page, false);
        const deleteAfter = await waitForDeleteAction(page, true);
        screenshots.push(await shot(page, "02_cancelled_before_delete"));
        results.push({
          test: "cancelled_delete_visible_cancel_hidden",
          pass: deleteAfter && !cancelAfter,
          cancelAfter,
          deleteAfter,
        });

        await clickActionsMenuItem(page, "Delete PM Request");
        await clickConfirmModal(page);
        await page.waitForURL(/\/(app|desk)\/pm-request/, {
          timeout: 120000,
          waitUntil: "domcontentloaded",
        });
        await page.waitForTimeout(2000);
        screenshots.push(await shot(page, "03_after_delete_list"));

        const notInList = await searchListShowsNoResult(page, reqName);
        screenshots.push(await shot(page, "04_list_search_no_result"));
        results.push({ test: "list_search_no_result", pass: notInList, notInList });

        const notFound = await openUrlShowsNotFound(page, reqName);
        screenshots.push(await shot(page, "05_old_url_not_found"));
        results.push({ test: "old_url_not_found", pass: notFound, notFound });
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
    console.log(
      JSON.stringify({ ok: failed.length === 0, results, screenshots }, null, 2)
    );
    if (failed.length) {
      process.exitCode = 1;
    }
  } catch (err) {
    console.error(
      JSON.stringify(
        {
          ok: false,
          error: String(err && err.message ? err.message : err),
          results,
          screenshots,
        },
        null,
        2
      )
    );
    process.exitCode = 1;
  }
}

main();
