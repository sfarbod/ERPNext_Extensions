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
    }
  });
  await page.waitForTimeout(1000);
}

async function openConnectionsTabAndLoadCounts(page, options = {}) {
  const { requirePeCount = true } = options;
  const opened = await openConnectionsTab(page);
  if (!opened) {
    return false;
  }
  await page.evaluate(() => {
    const dashboard = window.cur_frm?.dashboard;
    if (dashboard) {
      dashboard._fetched_counts = false;
      dashboard.set_open_count();
    }
  });
  try {
    await page.waitForFunction(
      (needPe) => {
        const root = document.querySelector(".form-dashboard .form-links");
        if (!root || root.querySelectorAll(".document-link").length < 3) {
          return false;
        }
        if (!needPe) {
          return true;
        }
        const pe = root.querySelector('.document-link[data-doctype="Payment Entry"] .count');
        return pe && !pe.classList.contains("hidden");
      },
      requirePeCount,
      { timeout: 90000 }
    );
    return true;
  } catch {
    return false;
  }
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

async function waitForConnectionsRender(page, options = {}) {
  return openConnectionsTabAndLoadCounts(page, options);
}

async function connectionsRoot(page) {
  return page.locator(".form-dashboard .form-links");
}

async function detailsIsDefaultTab(page) {
  return page.evaluate(() => {
    const active = document.querySelector(".form-tabs-list .nav-link.active");
    return active && /^Details$/i.test((active.innerText || "").trim());
  });
}

async function connectionsUsesNativeDashboard(page) {
  return page.evaluate(() => {
    const dashboard = document.querySelector(".form-dashboard");
    const links = document.querySelector(".form-dashboard .form-links");
    if (!dashboard || !links) {
      return false;
    }
    return (
      !document.querySelector(".pm-request-connections") &&
      links.querySelectorAll(".form-link-title").length >= 3 &&
      links.querySelectorAll(".document-link").length >= 3 &&
      links.querySelectorAll("table.table").length === 0
    );
  });
}

async function connectionsCounts(page) {
  return page.evaluate(() => {
    const root = document.querySelector(".form-dashboard .form-links");
    if (!root) {
      return null;
    }
    const out = {};
    root.querySelectorAll(".document-link").forEach((el) => {
      const doctype = el.getAttribute("data-doctype") || "";
      const countEl = el.querySelector(".count");
      const countText = (countEl?.innerText || "").trim();
      out[doctype] = {
        count: countText ? parseInt(countText, 10) || 0 : 0,
        countVisible: countEl && !countEl.classList.contains("hidden"),
        linkDisabled: Boolean(el.querySelector(".badge-link")?.hasAttribute("disabled")),
        names: (el.getAttribute("data-names") || "").split(",").filter(Boolean),
      };
    });
    return out;
  });
}

async function connectionsHasNoUsageSummary(page) {
  return page.evaluate(() => {
    const root = document.querySelector(".form-dashboard .form-links");
    if (!root) {
      return false;
    }
    const text = (root.innerText || "").toLowerCase();
    return !/usage summary/i.test(text);
  });
}

async function connectionsTabIsTraceabilityOnly(page) {
  return page.evaluate(() => {
    const active = document.querySelector(".form-tabs-list .nav-link.active");
    if (!active || !/connections/i.test(active.innerText || "")) {
      return false;
    }
    if (!document.querySelector(".form-dashboard .form-links")) {
      return false;
    }
    if (document.querySelector(".pm-request-connections")) {
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
    const root = document.querySelector(".form-dashboard .form-links");
    if (!root) {
      return { ok: false, reason: "missing_root" };
    }
    const text = (root.innerText || "").toLowerCase();
    const required = ["funding", "settlement", "accounting", "payment entry", "pm clearance", "journal entry"];
    const forbidden = ["usage summary", "pm-request-connections"];
    const missing = required.find((s) => !text.includes(s));
    if (missing) {
      return { ok: false, reason: `missing:${missing}` };
    }
    if (document.querySelector(".pm-request-connections")) {
      return { ok: false, reason: "custom_renderer" };
    }
    if (forbidden.some((s) => text.includes(s))) {
      return { ok: false, reason: "forbidden_text" };
    }
    return { ok: true };
  });
}

async function connectionsEmptyState(page) {
  return page.evaluate(() => {
    const root = document.querySelector(".form-dashboard .form-links");
    if (!root) {
      return false;
    }
    const rows = [...root.querySelectorAll(".document-link")];
    if (rows.length < 3) {
      return false;
    }
    return rows.every((el) => {
      const countEl = el.querySelector(".count");
      const countHidden = !countEl || countEl.classList.contains("hidden");
      const linkDisabled = Boolean(el.querySelector(".badge-link")?.hasAttribute("disabled"));
      return countHidden && linkDisabled;
    });
  });
}

async function clickConnectionNav(page, doctype) {
  await page
    .locator(`.form-dashboard .document-link[data-doctype="${doctype}"] .badge-link`)
    .click();
  await page.waitForTimeout(2000);
}

async function listFilterMatches(page, expectedNames) {
  await page.waitForTimeout(1000);
  return page.evaluate((names) => {
    const url = new URL(window.location.href);
    const nameParam = url.searchParams.get("name");
    if (!nameParam) {
      return { ok: false, reason: "no_name_param", href: url.href };
    }
    let filter;
    try {
      filter = JSON.parse(nameParam);
    } catch {
      return { ok: false, reason: "parse_failed", nameParam };
    }
    if (!Array.isArray(filter) || filter[0] !== "in") {
      return { ok: false, reason: "bad_filter", filter };
    }
    const raw = filter[1];
    const values = Array.isArray(raw)
      ? raw
      : String(raw || "")
          .split(",")
          .map((v) => v.trim())
          .filter(Boolean);
    const sorted = [...values].sort();
    const exp = [...names].sort();
    return {
      ok: JSON.stringify(sorted) === JSON.stringify(exp),
      values: sorted,
      expected: exp,
      href: url.href,
    };
  }, expectedNames);
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
        const defaultDetails = await detailsIsDefaultTab(page);
        await shot(page, "01_details_default");
        results.push({
          test: "details_is_default_tab",
          pass: defaultDetails,
          defaultDetails,
        });

        await openActionsMenu(page);
        await shot(page, "04_actions_menu");

        const tabVisible = await openConnectionsTab(page);
        const rendered = await openConnectionsTabAndLoadCounts(page);
        const nativeDashboard = rendered ? await connectionsUsesNativeDashboard(page) : false;
        const counts = rendered ? await connectionsCounts(page) : null;
        const audit = rendered ? await connectionsAudit(page) : { ok: false };
        const traceOnly = tabVisible ? await connectionsTabIsTraceabilityOnly(page) : false;
        const noUsageSummary = rendered ? await connectionsHasNoUsageSummary(page) : false;
        await shot(page, "02_connections_populated");

        await clickConnectionNav(page, "Payment Entry");
        const listPeNav = /\/List/i.test(page.url()) || /payment-entry.*list/i.test(page.url());
        const singlePeList = listPeNav
          ? await listFilterMatches(page, [connCtx.payment_entry])
          : { ok: false };
        await openPmRequest(page, connCtx.pm_request);
        await openConnectionsTabAndLoadCounts(page);

        results.push({
          test: "connections_tab_pe_and_clearance",
          pass:
            rendered &&
            nativeDashboard &&
            audit.ok &&
            traceOnly &&
            noUsageSummary &&
            counts?.["Payment Entry"]?.count === connCtx.expected_pe_count &&
            counts?.["PM Clearance"]?.count === connCtx.expected_clearance_count &&
            counts?.["Payment Entry"]?.countVisible === true &&
            listPeNav &&
            singlePeList.ok &&
            connCtx.expected_pe_count >= 1 &&
            connCtx.expected_clearance_count >= 1,
          tabVisible,
          rendered,
          nativeDashboard,
          counts,
          audit,
          traceOnly,
          noUsageSummary,
          listPeNav,
          singlePeList,
          connCtx,
        });

        benchExecute(
          "erpnext_extensions.petty_management.e2e.pm_request_finalize_v486_prep.add_second_payment_entry_for_connections",
          { pm_request: connCtx.pm_request }
        );
        await openPmRequest(page, connCtx.pm_request);
        await openConnectionsTabAndLoadCounts(page);
        const multiCounts = await connectionsCounts(page);
        const expectedPeNames = multiCounts?.["Payment Entry"]?.names || [];
        await clickConnectionNav(page, "Payment Entry");
        const multiListNav = /\/List/i.test(page.url()) || /payment-entry.*list/i.test(page.url());
        const multiPeList = multiListNav
          ? await listFilterMatches(page, expectedPeNames)
          : { ok: false };
        results.push({
          test: "connections_multi_pe_opens_list",
          pass:
            (multiCounts?.["Payment Entry"]?.count || 0) >= 2 &&
            multiListNav &&
            multiPeList.ok &&
            expectedPeNames.length >= 2,
          multiCounts,
          multiListNav,
          multiPeList,
          expectedPeNames,
          url: page.url(),
        });

        const emptyCtx = benchExecute(
          "erpnext_extensions.petty_management.e2e.pm_request_admin_delete_ui_v486_prep.prepare_connections_empty_fixture"
        );
        await login(page, emptyCtx.administrator.email, emptyCtx.administrator.password);
        await openPmRequest(page, emptyCtx.pm_request);
        const emptyRendered = await openConnectionsTabAndLoadCounts(page, { requirePeCount: false });
        const emptyOk = emptyRendered ? await connectionsEmptyState(page) : false;
        const emptyTraceOnly = await connectionsTabIsTraceabilityOnly(page);
        const emptyNoSummary = emptyRendered ? await connectionsHasNoUsageSummary(page) : false;
        await shot(page, "03_connections_empty");
        results.push({
          test: "connections_empty_state",
          pass: emptyRendered && emptyOk && emptyTraceOnly && emptyNoSummary,
          emptyRendered,
          emptyOk,
          emptyTraceOnly,
          emptyNoSummary,
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
