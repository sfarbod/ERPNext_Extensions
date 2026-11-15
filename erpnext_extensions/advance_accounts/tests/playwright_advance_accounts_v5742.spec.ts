/**
 * Playwright UI smoke for Advance Account Type + PE checkbox (v5.7.42).
 * Run from iran_accounting e2e package (shared Playwright install):
 *   npx playwright test ../../advance_accounts/tests/playwright_advance_accounts_v5742.spec.ts
 */
import { test, expect } from "@playwright/test";
import { execSync } from "child_process";

const BASE = process.env.FRAPPE_E2E_BASE_URL || "http://development.localhost:8000";
const SITE = process.env.FRAPPE_SITE || "development.localhost";
const BENCH = process.env.FRAPPE_BENCH_ROOT || "/workspace/development/frappe-bench";

function benchExecute(method: string, kwargs: Record<string, unknown> = {}) {
  const kw = JSON.stringify(kwargs);
  const out = execSync(
    `cd ${BENCH} && bench --site ${SITE} execute ${method} --kwargs '${kw.replace(/'/g, "'\\''")}'`,
    { encoding: "utf8", maxBuffer: 10_000_000 }
  );
  const line = out.trim().split("\n").filter(Boolean).pop() || "";
  try {
    return JSON.parse(line);
  } catch {
    return line;
  }
}

function mintSid(): string {
  const out = execSync(
    `cd ${BENCH} && bench --site ${SITE} execute frappe.auth.get_logged_user`,
    { encoding: "utf8" }
  );
  // Prefer explicit sid mint via frappe.sessions
  const sidOut = execSync(
    `cd ${BENCH} && bench --site ${SITE} execute erpnext_extensions.advance_accounts.tests.e2e_playwright_helpers.mint_sid`,
    { encoding: "utf8" }
  );
  const last = sidOut.trim().split("\n").filter(Boolean).pop() || "";
  try {
    const parsed = JSON.parse(last);
    return parsed.sid || parsed;
  } catch {
    return last.replace(/"/g, "");
  }
}

test.describe("Advance Accounts v5.7.42 UI", () => {
  test("Account field + PE read-only advance checkbox", async ({ page, context }) => {
    const prep = benchExecute(
      "erpnext_extensions.advance_accounts.tests.e2e_playwright_helpers.prepare_ui_fixture"
    ) as {
      account: string;
      pe_receive_advance?: string;
      pe_receive_normal?: string;
    };

    const sid = mintSid();
    const domain = new URL(BASE).hostname;
    await context.addCookies([
      { name: "sid", value: sid, domain, path: "/" },
      { name: "system_user", value: "yes", domain, path: "/" },
      { name: "full_name", value: "Administrator", domain, path: "/" },
      { name: "user_id", value: "Administrator", domain, path: "/" },
    ]);

    // Account form shows Advance Account Type
    await page.goto(`${BASE}/app/account/${encodeURIComponent(prep.account)}`);
    await page.waitForSelector('div[data-fieldname="advance_account_type"]', { timeout: 30000 });
    const advType = page.locator('div[data-fieldname="advance_account_type"] select, div[data-fieldname="advance_account_type"] .control-value, div[data-fieldname="advance_account_type"] input');
    await expect(page.locator('div[data-fieldname="advance_account_type"]')).toBeVisible();

    // Frappe read-only Check uses disabled-selected / disabled-deselected classes
    // rather than a reliable native input.checked property.
    if (prep.pe_receive_advance) {
      await page.goto(`${BASE}/app/payment-entry/${prep.pe_receive_advance}`);
      await page.waitForSelector('div[data-fieldname="book_advance_payments_in_separate_party_account"]', {
        timeout: 30000,
      });
      const wrap = page.locator('div[data-fieldname="book_advance_payments_in_separate_party_account"]');
      await expect(wrap.locator("input[type=checkbox]")).toBeDisabled();
      const on = await page.evaluate(() => {
        // @ts-expect-error desk global
        return Number(cur_frm?.doc?.book_advance_payments_in_separate_party_account || 0);
      });
      expect(on).toBe(1);
    }

    if (prep.pe_receive_normal) {
      await page.goto(`${BASE}/app/payment-entry/${prep.pe_receive_normal}`);
      await page.waitForSelector('div[data-fieldname="book_advance_payments_in_separate_party_account"]', {
        timeout: 30000,
      });
      const wrap = page.locator('div[data-fieldname="book_advance_payments_in_separate_party_account"]');
      await expect(wrap.locator("input[type=checkbox]")).toBeDisabled();
      const on = await page.evaluate(() => {
        // @ts-expect-error desk global
        return Number(cur_frm?.doc?.book_advance_payments_in_separate_party_account || 0);
      });
      expect(on).toBe(0);
    }
  });
});
