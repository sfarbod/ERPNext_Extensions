/**
 * v4.8.6 — PM Request finalize E2E (Actions menu UX, admin-only delete, Connections tab).
 */
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";
import { benchExecute, SITE } from "../../e2e/e2e_playwright_db.mjs";
import { runWithPlaywrightBrowser } from "./playwright_pm_harness.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREEN = path.join(__dirname, "screenshots", "pm_request_finalize_v486");
const TRACE = path.join(__dirname, "traces", "pm_request_finalize_v486.zip");
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
      await window.cur_frm.trigger("refresh_connections");
    }
  });
  await page.waitForTimeout(1500);
}

async function openActionsMenu(page) {
  const selectors = [
    ".actions-btn-group .btn",
    ".page-actions .actions-btn-group button",
    'button:has-text("Actions")',
  ];
  for (const sel of selectors) {
    const btn = page.locator(sel).first();
    if ((await btn.count()) && (await btn.isVisible())) {
      await btn.click();
      await page.waitForTimeout(400);
      return;
    }
  }
}

async function isActionsMenuItemVisible(page, label) {
  await openActionsMenu(page);
  return page.evaluate((text) => {
    const items = document.querySelectorAll(
      ".dropdown-menu.show .dropdown-item, .actions-btn-group .dropdown-menu .dropdown-item, .dropdown-menu .dropdown-item"
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

async function isPrimaryToolbarButtonVisible(page, label) {
  return page.evaluate((text) => {
    const roots = [document.querySelector(".page-head"), document.body].filter(Boolean);
    for (const root of roots) {
      const els = root.querySelectorAll("button.btn, .btn");
      for (const el of els) {
        const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
        if (new RegExp(`^${text}$`, "i").test(t)) {
          if (el.closest(".actions-btn-group, .dropdown-menu")) {
            continue;
          }
          return el.offsetParent !== null;
        }
      }
    }
    return false;
  }, label);
}

async function clickActionsMenuItem(page, label) {
  await openActionsMenu(page);
  const item = page
    .locator(".dropdown-menu.show .dropdown-item, .actions-btn-group .dropdown-menu .dropdown-item")
    .filter({ hasText: new RegExp(`^${label}$`, "i") })
    .first();
  await item.click({ timeout: 30000 });
}

async function clickConfirmModal(page) {
  const modal = page.locator(".modal-dialog:visible").first();
  await modal.waitFor({ timeout: 60000 });
  await modal.locator("button.btn-primary").first().click();
}

async function openConnectionsTab(page) {
  const selectors = [
    '[data-fieldname="tab_connections"]',
    '.form-tabs-list .nav-link[data-fieldname="tab_connections"]',
    '.form-tabs-list .nav-link',
  ];
  for (const sel of selectors) {
    const tabs = page.locator(sel);
    const count = await tabs.count();
    for (let i = 0; i < count; i += 1) {
      const tab = tabs.nth(i);
      const text = ((await tab.innerText()) || "").trim();
      if (sel.includes("tab_connections") || /^Connections$/i.test(text)) {
        await tab.click();
        await page.waitForTimeout(1000);
        return true;
      }
    }
  }
  return false;
}

async function waitForConnectionsRender(page) {
  try {
    await page.waitForFunction(
      () => {
        const root = document.querySelector(".pm-request-connections");
        return root && (root.innerText || "").trim().length > 20;
      },
      { timeout: 60000 }
    );
    return true;
  } catch {
    return false;
  }
}

async function connectionsHasRows(page, heading) {
  return page.evaluate((sectionHeading) => {
    const root = document.querySelector(".pm-request-connections");
    if (!root) {
      return false;
    }
    const headings = [...root.querySelectorAll("h6")];
    const h = headings.find((el) =>
      (el.innerText || "").trim().toLowerCase().includes(sectionHeading.toLowerCase())
    );
    if (!h) {
      return false;
    }
    let table = h.nextElementSibling;
    while (table && table.tagName !== "TABLE") {
      table = table.nextElementSibling;
    }
    if (!table) {
      return false;
    }
    const rows = table.querySelectorAll("tbody tr");
    if (!rows.length) {
      return false;
    }
    const firstText = (rows[0].innerText || "").toLowerCase();
    return !firstText.includes("no payment entries") && !firstText.includes("no pm clearances");
  }, heading);
}

async function connectionsAudit(page) {
  return page.evaluate(() => {
    const root = document.querySelector(".pm-request-connections");
    if (!root) {
      return { ok: false, reason: "missing_root" };
    }
    const text = (root.innerText || "").toLowerCase();
    const sections = [
      "usage summary",
      "payment entries",
      "pm clearances",
      "journal entries",
    ];
    const missingSection = sections.find((s) => !text.includes(s));
    if (missingSection) {
      return { ok: false, reason: `missing_section:${missingSection}` };
    }
    const links = [...root.querySelectorAll("a[href]")];
    const badLinks = links.filter((a) => !(a.getAttribute("href") || "").startsWith("/app/"));
    return {
      ok: badLinks.length === 0,
      linkCount: links.length,
      badLinks: badLinks.map((a) => a.getAttribute("href")),
    };
  });
}

async function connectionsEmptyState(page) {
  return page.evaluate(() => {
    const root = document.querySelector(".pm-request-connections");
    if (!root) {
      return false;
    }
    const t = (root.innerText || "").toLowerCase();
    return (
      t.includes("no payment entries linked") &&
      t.includes("no pm clearances linked") &&
      t.includes("no journal entries linked")
    );
  });
}

async function main() {
  fs.mkdirSync(SCREEN, { recursive: true });
  const results = [];
  const consoleErrors = [];

  try {
    await runWithPlaywrightBrowser(
      async ({ page }) => {
        page.on("console", (msg) => {
          if (msg.type() !== "error") {
            return;
          }
          const text = msg.text() || "";
          if (
            /socket\.io|favicon|Failed to load resource|Unauthorized.*fetch failed|net::ERR/i.test(
              text
            )
          ) {
            return;
          }
          consoleErrors.push(text);
        });
        const cancelCtx = benchExecute(
          "erpnext_extensions.petty_management.e2e.pm_request_finalize_v486_prep.prepare_cancel_action_accountant_eligible"
        );
        await login(page, cancelCtx.user.email, cancelCtx.user.password);
        await openPmRequest(page, cancelCtx.pm_request);
        const cancelInActions = await isActionsMenuItemVisible(page, "Cancel PM Request");
        const deleteHiddenAcct = !(await isActionsMenuItemVisible(page, "Delete PM Request"));
        await shot(page, "01_accountant_actions_before_cancel");
        results.push({
          test: "accountant_cancel_in_actions_not_delete",
          pass: cancelInActions && deleteHiddenAcct,
          cancelInActions,
          deleteHiddenAcct,
        });
        await clickActionsMenuItem(page, "Cancel PM Request");
        await clickConfirmModal(page);
        await page.waitForTimeout(2500);
        await openPmRequest(page, cancelCtx.pm_request);
        const cancelled = (await page.evaluate(() => window.cur_frm?.doc?.docstatus)) === 2;
        results.push({ test: "accountant_cancel_works", pass: cancelled, cancelled });

        const requesterCtx = benchExecute(
          "erpnext_extensions.petty_management.e2e.pm_request_finalize_v486_prep.prepare_requester_no_cancel_delete"
        );
        await login(page, requesterCtx.user.email, requesterCtx.user.password);
        await openPmRequest(page, requesterCtx.submitted_pm_request);
        const reqFlags = benchExecute(
          "erpnext_extensions.petty_management.e2e.pm_request_finalize_v486_prep.check_action_flags_as_user",
          {
            pm_request: requesterCtx.submitted_pm_request,
            user: requesterCtx.user.email,
          }
        );
        const cancelHidden = !(await isActionsMenuItemVisible(page, "Cancel PM Request"));
        const deleteHiddenUser = !(await isActionsMenuItemVisible(page, "Delete PM Request"));
        results.push({
          test: "requester_no_cancel_delete",
          pass:
            cancelHidden &&
            deleteHiddenUser &&
            !reqFlags.can_cancel_pm_request &&
            !reqFlags.can_delete_pm_request,
          reqFlags,
          cancelHidden,
          deleteHiddenUser,
        });

        const connCtx = benchExecute(
          "erpnext_extensions.petty_management.e2e.pm_request_finalize_v486_prep.prepare_connections_fixture"
        );
        await login(page, connCtx.user.email, connCtx.user.password);
        await openPmRequest(page, connCtx.pm_request);
        const tabVisible = await openConnectionsTab(page);
        const rendered = await waitForConnectionsRender(page);
        const peRows = rendered ? await connectionsHasRows(page, "Payment Entries") : false;
        const clRows = rendered ? await connectionsHasRows(page, "PM Clearances") : false;
        const audit = rendered ? await connectionsAudit(page) : { ok: false };
        await shot(page, "03_connections_tab_populated");
        results.push({
          test: "connections_tab_pe_and_clearance",
          pass:
            rendered &&
            peRows &&
            clRows &&
            audit.ok &&
            connCtx.expected_pe_count >= 1 &&
            connCtx.expected_clearance_count >= 1,
          tabVisible,
          rendered,
          peRows,
          clRows,
          audit,
          connCtx,
        });

        const emptyCtx = benchExecute(
          "erpnext_extensions.petty_management.e2e.pm_request_admin_delete_ui_v486_prep.prepare_connections_empty_fixture"
        );
        await login(page, emptyCtx.administrator.email, emptyCtx.administrator.password);
        await openPmRequest(page, emptyCtx.pm_request);
        await openConnectionsTab(page);
        const emptyRendered = await waitForConnectionsRender(page);
        const emptyOk = emptyRendered ? await connectionsEmptyState(page) : false;
        await shot(page, "04_connections_tab_empty");
        results.push({
          test: "connections_empty_state",
          pass: emptyRendered && emptyOk,
          emptyRendered,
          emptyOk,
        });

        results.push({
          test: "connections_no_console_errors",
          pass: consoleErrors.length === 0,
          consoleErrors,
        });

        await login(page, cancelCtx.user.email, cancelCtx.user.password);
        await openPmRequest(page, connCtx.pm_request);
        const createPeToolbar = await isPrimaryToolbarButtonVisible(page, "Create Payment Entry");
        results.push({
          test: "create_pe_on_toolbar",
          pass: createPeToolbar,
          createPeToolbar,
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
