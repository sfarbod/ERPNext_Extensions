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
  const btn = page.locator(".actions-btn-group .dropdown-toggle, .actions-btn-group .btn").first();
  if ((await btn.count()) && (await btn.isVisible())) {
    await btn.click();
    await page.waitForTimeout(400);
  }
}

async function countActionsMenus(page) {
  return page.evaluate(() => {
    const inner = Array.from(
      document.querySelectorAll(".inner-group-button .dropdown-toggle")
    ).filter((el) => (el.textContent || "").trim() === "Actions").length;
    const std = Array.from(document.querySelectorAll(".actions-btn-group")).filter(
      (el) => el.offsetParent !== null
    ).length;
    return { inner, std };
  });
}

async function formTabLabels(page) {
  return page.evaluate(() =>
    [...document.querySelectorAll(".form-tabs-list .nav-link")].map((el) =>
      (el.innerText || "").trim()
    )
  );
}

async function isActionsMenuItemVisible(page, label) {
  await openActionsMenu(page);
  return page.evaluate((text) => {
    const items = document.querySelectorAll(
      ".actions-btn-group .dropdown-menu.show .dropdown-item, .actions-btn-group .dropdown-menu .dropdown-item"
    );
    for (const el of items) {
      const t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      if (new RegExp(`^${text}$`, "i").test(t)) {
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
  const clicked = await page.evaluate((text) => {
    const items = document.querySelectorAll(
      ".actions-btn-group .dropdown-menu.show .dropdown-item, .actions-btn-group .dropdown-menu .dropdown-item"
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
    await page
      .locator(".actions-btn-group .dropdown-menu .dropdown-item")
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

async function connectionsHasRows(page, cardTitle) {
  return page.evaluate((title) => {
    const root = document.querySelector(".pm-request-connections");
    if (!root) {
      return false;
    }
    const cards = [...root.querySelectorAll(".pm-conn-card")];
    const card = cards.find((c) =>
      (c.innerText || "").toLowerCase().includes(title.toLowerCase())
    );
    if (!card) {
      return false;
    }
    const badge = card.querySelector(".badge");
    const count = badge ? parseInt(badge.innerText, 10) : 0;
    return count > 0 && card.querySelector("tbody tr") !== null;
  }, cardTitle);
}

async function connectionsTabIsTraceabilityOnly(page) {
  return page.evaluate(() => {
    const active = document.querySelector(".form-tabs-list .nav-link.active");
    if (!active || !/connections/i.test(active.innerText || "")) {
      return false;
    }
    if (!document.querySelector(".pm-request-connections")) {
      return false;
    }
    const blocked = new Set(["details", "remark", "journal_entry", "payment_entry"]);
    const controls = document.querySelectorAll(".frappe-control[data-fieldname]");
    for (const el of controls) {
      const fn = el.getAttribute("data-fieldname");
      if (!blocked.has(fn)) {
        continue;
      }
      const hidden =
        el.classList.contains("hide-control") ||
        el.offsetParent === null ||
        getComputedStyle(el).display === "none";
      if (!hidden) {
        return false;
      }
    }
    return true;
  });
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
      t.includes("no payment entries") &&
      t.includes("no pm clearances") &&
      t.includes("no journal entries")
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
        const actionsMenus = await countActionsMenus(page);
        const tabs = await formTabLabels(page);
        results.push({
          test: "single_actions_menu_and_tab_order",
          pass:
            actionsMenus.inner === 0 &&
            actionsMenus.std >= 1 &&
            tabs[0] === "Details" &&
            tabs.includes("Connections") &&
            tabs.filter((t) => t === "Details").length === 1,
          actionsMenus,
          tabs,
        });
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
        const traceOnly = tabVisible ? await connectionsTabIsTraceabilityOnly(page) : false;
        await shot(page, "03_connections_tab_populated");
        results.push({
          test: "connections_tab_pe_and_clearance",
          pass:
            rendered &&
            peRows &&
            clRows &&
            audit.ok &&
            traceOnly &&
            connCtx.expected_pe_count >= 1 &&
            connCtx.expected_clearance_count >= 1,
          tabVisible,
          rendered,
          peRows,
          clRows,
          audit,
          traceOnly,
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
        const emptyTraceOnly = await connectionsTabIsTraceabilityOnly(page);
        await shot(page, "04_connections_tab_empty");
        results.push({
          test: "connections_empty_state",
          pass: emptyRendered && emptyOk && emptyTraceOnly,
          emptyRendered,
          emptyOk,
          emptyTraceOnly,
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
