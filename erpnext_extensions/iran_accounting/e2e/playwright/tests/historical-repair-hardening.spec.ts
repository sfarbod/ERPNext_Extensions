import { test, expect } from "../src/fixtures/erpnext.fixture";
import { erpnextConfig } from "../src/fixtures/erpnext.fixture";
import { captureStep } from "../src/utils/screenshots";

test.describe("5.2.7 Historical Repair hardening UX @release-blocking", () => {
  test.setTimeout(8 * 60_000);

  test.beforeEach(async ({ page }) => {
    const sid = process.env.FRAPPE_E2E_SID;
    if (sid) {
      await page.context().addCookies([
        { name: "sid", value: sid, domain: "development.localhost", path: "/" },
        { name: "system_user", value: "yes", domain: "development.localhost", path: "/" },
        { name: "full_name", value: "Administrator", domain: "development.localhost", path: "/" },
      ]);
    }
  });

  test("dashboard KPIs, expected-rate columns, reconstruction preview", async ({ page, loginPage }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(String(err)));
    page.on("response", (res) => {
      if (res.status() >= 500) errors.push(`HTTP ${res.status()} ${res.url()}`);
    });
    if (!process.env.FRAPPE_E2E_SID) {
      await loginPage.login(erpnextConfig.user, erpnextConfig.password);
    }

    await page.goto("/app/historical-repair");
    await expect(page.locator(".hr-section-title")).toContainText("Production Posting Order", {
      timeout: 60_000,
    });
    await expect(page.locator("button[data-action='scan-all']")).toBeVisible();
    await expect(page.locator("button[data-action='columns']")).toBeVisible();
    await page.locator("button[data-action='scan-all']").click();
    await expect(page.locator("pre[data-role='preview']")).toContainText("Integrity Score", {
      timeout: 180_000,
    });
    await expect(page.locator(".hr-kpi-label").filter({ hasText: "Integrity Score" })).toBeVisible();
    await expect(page.locator(".hr-kpi-label").filter({ hasText: "Wrong Rate" })).toBeVisible();
    await captureStep(page, "hsr_hardening_dashboard");

    await page.locator("button.hr-tab", { hasText: "Zero / Lost Rate" }).click();
    await page.locator("button[data-action='scan']").click();
    await expect(page.locator("pre[data-role='preview']")).toContainText("Scan complete", {
      timeout: 180_000,
    });
    await expect(page.locator("table.hr-table thead")).toContainText("Expected Basic Rate");
    await expect(page.locator("table.hr-table thead")).toContainText("Rate Source");
    await expect(page.locator("table.hr-table thead")).toContainText("Repair Required");
    const firstData = page.locator("table.hr-table tbody tr").first();
    if (await firstData.count()) {
      await firstData.click();
      await expect(page.locator("[data-role='reconstruction']")).toContainText(/Current|Expected|Confidence/i, {
        timeout: 60_000,
      });
    }
    await captureStep(page, "hsr_hardening_expected_rates");

    const unexpected = errors.filter((e) => !e.includes("favicon") && !e.includes("socket.io"));
    expect(unexpected, unexpected.join("\n")).toEqual([]);
  });
});
