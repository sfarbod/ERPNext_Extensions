import { test, expect } from "../src/fixtures/erpnext.fixture";
import { erpnextConfig } from "../src/fixtures/erpnext.fixture";
import { captureStep } from "../src/utils/screenshots";

async function login(page, loginPage) {
  if (!process.env.FRAPPE_E2E_SID) {
    await loginPage.login(erpnextConfig.user, erpnextConfig.password);
  }
}

async function openHistoricalRepair(page) {
  await page.goto("/app/historical-repair");
  await expect(page).toHaveURL(/historical-repair/, { timeout: 30_000 });
  await expect(page.locator(".hr-section-title")).toBeVisible({ timeout: 60_000 });
  await expect(page.locator("button[data-action='scan']")).toBeVisible();
}

test.describe("5.2.13 Historical Repair I4 + filters @release-blocking", () => {
  test.setTimeout(6 * 60_000);

  test.beforeEach(async ({ page }) => {
    const sid = process.env.FRAPPE_E2E_SID;
    if (sid) {
      const base = process.env.FRAPPE_E2E_BASE_URL || "http://127.0.0.1:8000";
      let domain = "127.0.0.1";
      try {
        domain = new URL(base).hostname;
      } catch {
        /* keep default */
      }
      await page.context().addCookies([
        { name: "sid", value: sid, domain, path: "/" },
        { name: "system_user", value: "yes", domain, path: "/" },
        { name: "full_name", value: "Administrator", domain, path: "/" },
      ]);
    }
  });

  test("filters, I4 button, wizard, and SLE scan args", async ({ page, loginPage }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(String(err)));
    page.on("response", (res) => {
      if (res.status() >= 500) errors.push(`HTTP ${res.status()} ${res.url()}`);
    });
    await login(page, loginPage);
    await openHistoricalRepair(page);

    await expect(page.locator(".hr-wizard")).toBeVisible();
    await expect(page.locator("button[data-action='clear-filters']")).toBeVisible();
    await expect(page.locator("button[data-action='root-explorer']")).toBeVisible();
    await expect(page.locator("button[data-action='identity-health']")).toBeVisible();
    await expect(page.locator("button[data-action='master-plan']")).toBeVisible();
    await expect(page.locator("[data-role='voucher-quick']")).toBeVisible();

    await page.locator(".hr-tab[data-topic='sle']").click();
    await expect(page.locator("button[data-action='repair-i4']")).toBeVisible();

    const requestPromise = page.waitForRequest(
      (req) => req.url().includes("scan_sle_bin_api") && req.method() === "POST",
      { timeout: 60_000 }
    );
    await page.locator("button[data-action='scan']").click();
    const req = await requestPromise;
    const post = req.postData() || "";
    expect(post.includes("company") || post.includes("cmd")).toBeTruthy();
    await captureStep(page, "hsr_i4_sle_scan");

    await page.locator("button[data-action='clear-filters']").click();
    await expect(page.locator(".hr-kpi").filter({ hasText: "I4 Leftover" })).toBeVisible({ timeout: 60_000 });

    expect(errors.filter((e) => !/favicon/i.test(e))).toEqual([]);
  });

  test("operator path: voucher filter → scan → dry run → integrity for repaired 25791", async ({
    page,
    loginPage,
  }) => {
    const errors: string[] = [];
    const api500: string[] = [];
    page.on("pageerror", (err) => errors.push(String(err)));
    page.on("response", (res) => {
      if (res.status() >= 500) api500.push(`HTTP ${res.status()} ${res.url()}`);
    });
    await login(page, loginPage);
    await openHistoricalRepair(page);

    await page.locator(".hr-tab[data-topic='sle']").click();
    await expect(page.locator("button[data-action='repair-i4']")).toBeVisible();
    await expect(page.locator(".hr-wizard")).toBeVisible();

    // Voucher quick search / filter for already-repaired Patient Zero (idempotency path)
    const voucher = "MAT-STE-2026-25791";
    const quick = page.locator("[data-role='voucher-quick']");
    await quick.fill(voucher);
    await quick.press("Enter");

    // Wait for scan to settle
    await page.waitForTimeout(3000);
    await expect(page.locator(".hr-preview").first()).toBeVisible({ timeout: 90_000 });
    await expect(page.locator(".hr-table-wrap").first()).toBeVisible();
    await captureStep(page, "hsr_i4_25791_after_scan");

    // Dry Run should not 500 even when no READY_I4 rows remain
    await page.locator("button[data-action='dry-run']").click();
    await page.waitForTimeout(2500);
    await captureStep(page, "hsr_i4_25791_dry_run");

    await page.locator("button[data-action='integrity']").click();
    await page.waitForTimeout(2500);
    await captureStep(page, "hsr_i4_25791_integrity");

    // Backup warning copy exists on repair button title / confirm path
    const repairI4 = page.locator("button[data-action='repair-i4']");
    await expect(repairI4).toBeVisible();
    const title = (await repairI4.getAttribute("title")) || "";
    expect(title.length).toBeGreaterThan(0);

    // Progress / wizard present
    await expect(page.locator(".hr-wizard-step").first()).toBeVisible();

    expect(api500).toEqual([]);
    expect(errors.filter((e) => !/favicon/i.test(e))).toEqual([]);
  });
});
