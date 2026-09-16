import { test, expect } from "../src/fixtures/erpnext.fixture";
import { erpnextConfig } from "../src/fixtures/erpnext.fixture";
import { captureStep } from "../src/utils/screenshots";

const TABS = [
  "Posting Order",
  "Wrong Rate",
  "Zero / Lost Rate",
  "Manufacture Valuation",
  "SLE / Bin Integrity",
  "GL Integrity",
  "Failed RIV",
];

test.describe("5.2.7 Historical Stock Integrity tabs @release-blocking", () => {
  test.setTimeout(12 * 60_000);

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

  test("all tabs scan dry-run preview integrity no API 500", async ({ page, loginPage }) => {
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
    await captureStep(page, "hsr_01_posting_order");

    for (const label of TABS) {
      const modal = page.locator(".modal.show");
      if (await modal.count()) {
        await page.keyboard.press("Escape");
        await expect(modal).toHaveCount(0, { timeout: 5_000 });
      }
      const tab = page.locator("button.hr-tab", { hasText: label });
      await expect(tab).toBeVisible({ timeout: 30_000 });
      await tab.click();
      await expect(page.locator("button[data-action='scan']")).toBeVisible();
      await expect(page.locator("button[data-action='dry-run']")).toBeVisible();
      await expect(page.locator("button[data-action='repair']")).toBeVisible();
      await expect(page.locator("button[data-action='repost']")).toBeVisible();
      await expect(page.locator("button[data-action='integrity']")).toBeVisible();
      await page.locator("button[data-action='scan']").click();
      const preview = page.locator("pre[data-role='preview']");
      await expect(preview).toContainText("Scan complete", {
        timeout: 180_000,
      });
      await page.locator("button[data-action='dry-run']").click();
      await expect(preview).toBeVisible({ timeout: 180_000 });
      const text = await preview.innerText();
      expect(text.toLowerCase()).not.toContain("internal server error");
      expect(text).not.toContain("Traceback");
      await page.locator("button[data-action='integrity']").click();
      await captureStep(page, `hsr_tab_${label.replace(/[^a-z0-9]+/gi, "_")}`);
    }

    const unexpected = errors.filter((e) => !e.includes("favicon") && !e.includes("socket.io"));
    expect(unexpected, unexpected.join("\n")).toEqual([]);
  });
});
