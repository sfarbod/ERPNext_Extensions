import { test, expect } from "../src/fixtures/erpnext.fixture";
import { erpnextConfig } from "../src/fixtures/erpnext.fixture";
import { captureStep } from "../src/utils/screenshots";

async function login(page, loginPage) {
  if (!process.env.FRAPPE_E2E_SID) {
    await loginPage.login(erpnextConfig.user, erpnextConfig.password);
  }
}

async function expectRepairPage(page) {
  await expect(page).toHaveURL(/historical-repair/, { timeout: 30_000 });
  await expect(page.locator(".hr-section-title")).toBeVisible({ timeout: 60_000 });
  await expect(page.locator("button[data-action='scan']")).toBeVisible();
  await expect(page.locator(".hr-dashboard")).toBeVisible();
}

test.describe("5.2.7 Historical Repair accessibility @release-blocking", () => {
  test.setTimeout(6 * 60_000);

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

  test("direct URL, sidebar, workspace, awesome bar, no console/API 500", async ({ page, loginPage }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(String(err)));
    page.on("response", (res) => {
      if (res.status() >= 500) errors.push(`HTTP ${res.status()} ${res.url()}`);
    });
    await login(page, loginPage);

    await page.goto("/app/historical-repair");
    await expectRepairPage(page);
    await captureStep(page, "hsr_nav_direct_url");

    await page.goto("/app/stock");
    const sidebar = page.locator(".body-sidebar");
    await expect(sidebar).toBeVisible({ timeout: 30_000 });
    const toolsSection = sidebar.locator('[item-name="Tools"]').first();
    await expect(toolsSection).toBeVisible({ timeout: 30_000 });
    const sideLink = sidebar.locator('[item-name="Historical Repair"] a').first();
    if (!(await sideLink.isVisible().catch(() => false))) {
      await toolsSection.click();
    }
    await expect(sideLink).toBeVisible({ timeout: 15_000 });
    await sideLink.click();
    await expectRepairPage(page);
    await captureStep(page, "hsr_nav_sidebar");

    await page.goto("/app/stock");
    const toolsCard = page.locator(".widget").filter({ hasText: "Tools" }).first();
    const shortcut = page.locator(".shortcut-widget-box, .widget.shortcut").filter({ hasText: "Historical Repair" }).first();
    const maintenance = page.locator(".widget").filter({ hasText: "Maintenance" }).first();
    if (await shortcut.isVisible().catch(() => false)) {
      await shortcut.click();
    } else if (await toolsCard.getByText("Historical Repair", { exact: true }).isVisible().catch(() => false)) {
      await toolsCard.getByText("Historical Repair", { exact: true }).click();
    } else {
      await maintenance.scrollIntoViewIfNeeded();
      await maintenance.getByText("Historical Repair", { exact: true }).click();
    }
    await expectRepairPage(page);
    await captureStep(page, "hsr_nav_workspace");

    await page.goto("/app");
    await page.keyboard.press("Control+k");
    const search = page.locator("#navbar-search");
    await expect(search).toBeVisible({ timeout: 15_000 });
    await search.fill("Historical Repair");
    const result = page.locator(".awesomplete li").filter({ hasText: /Historical Repair/i }).first();
    await expect(result).toBeVisible({ timeout: 15_000 });
    await result.click();
    await expectRepairPage(page);
    await captureStep(page, "hsr_nav_awesome_bar");

    const unexpected = errors.filter((e) => !e.includes("favicon") && !e.includes("socket.io"));
    expect(unexpected, unexpected.join("\n")).toEqual([]);
  });
});
