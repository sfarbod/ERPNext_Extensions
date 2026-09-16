import { test, expect } from "../src/fixtures/erpnext.fixture";
import { erpnextConfig } from "../src/fixtures/erpnext.fixture";
import { captureStep } from "../src/utils/screenshots";

/**
 * Dashboard ↔ Topic Grid consistency (v5.2.18).
 *
 * After Scan All, a topic with Dashboard KPI > 0 must never present a silent
 * empty grid. Either rows are loaded, or the UI shows an explicit
 * "Rows are not loaded yet / Click Scan" state.
 */

const TOPIC_KPI: { tab: string; kpi: string }[] = [
  { tab: "Posting Order", kpi: "Posting Order" },
  { tab: "Wrong Rate", kpi: "Wrong Rate" },
  { tab: "Zero / Lost Rate", kpi: "Zero Rate" },
  { tab: "SLE / Bin Integrity", kpi: "Broken Bin" },
  { tab: "GL Integrity", kpi: "Broken GL" },
  { tab: "Failed RIV", kpi: "Failed RIV" },
];

async function login(page, loginPage) {
  if (!process.env.FRAPPE_E2E_SID) {
    await loginPage.login(erpnextConfig.user, erpnextConfig.password);
  }
}

function kpiValue(page, label: string) {
  // Exact label match — "Wrong Rate" must not match "Wrong Rate READY".
  const re = new RegExp(`^${label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`);
  return page
    .locator(".hr-kpi")
    .filter({ has: page.locator(".hr-kpi-label", { hasText: re }) })
    .locator(".hr-kpi-value");
}

test.describe("5.2.18 Historical Repair Dashboard ↔ Grid consistency @release-blocking", () => {
  test.setTimeout(20 * 60_000);

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

  test("Scan All then each topic: KPI>0 never silent empty grid", async ({ page, loginPage }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(String(err)));
    page.on("response", (res) => {
      if (res.status() >= 500) errors.push(`HTTP ${res.status()} ${res.url()}`);
    });

    await login(page, loginPage);
    await page.goto("/app/historical-repair");
    await expect(page.locator(".hr-dashboard")).toBeVisible({ timeout: 60_000 });
    await expect(page.locator("button.hr-tab", { hasText: "Wrong Rate" })).toBeVisible({
      timeout: 30_000,
    });

    const companyInput = page.locator('.hr-toolbar [data-fieldname="company"] input, .hr-toolbar .frappe-control[data-fieldname="company"] input').first();
    if (await companyInput.count()) {
      await companyInput.fill("اسپاد فارمد دارو");
      await companyInput.press("Tab");
      await page.waitForTimeout(500);
    }
    await page.locator("button[data-action='scan-all']").click();

    // Wait for auto Scan All (async long job) — dashboard leaves "—" placeholders.
    await expect
      .poll(
        async () => {
          const txt = await page.locator(".hr-kpi").filter({ has: page.locator(".hr-kpi-label", { hasText: /^Integrity Score$/ }) }).locator(".hr-kpi-value").innerText();
          return txt.trim();
        },
        { timeout: 10 * 60_000, intervals: [2_000, 3_000, 5_000] }
      )
      .not.toBe("—");

    await captureStep(page, "hsr_dash_grid_after_scan_all");

    for (const { tab, kpi } of TOPIC_KPI) {
      const modal = page.locator(".modal.show");
      if (await modal.count()) {
        await page.keyboard.press("Escape");
        await expect(modal).toHaveCount(0, { timeout: 5_000 });
      }

      const kpiText = (await kpiValue(page, kpi).innerText()).trim();
      const kpiN = kpiText === "—" ? null : Number(kpiText.replace(/,/g, ""));

      await page.locator("button.hr-tab", { hasText: tab }).click();
      // Switching topics without cached rows must show explicit unload state
      // (auto-scan on Scan All only loads the previously active tab).
      const unload = page.locator("[data-role='rows-not-loaded']");
      const gridRows = page.locator(".hr-table tbody tr");
      const unloaded = await unload.isVisible().catch(() => false);
      const rowCount = await gridRows.count();

      if (kpiN != null && kpiN > 0 && rowCount === 0) {
        expect(unloaded, `${tab}: Dashboard ${kpi}=${kpiN} but empty grid without Load Rows state`).toBe(true);
        await expect(unload).toContainText(/Rows are not loaded yet/i);
        await expect(unload).toContainText(/Click Scan/i);
      }

      await page.locator("button[data-action='scan']").click();
      await expect(page.locator("pre[data-role='preview']")).toContainText(/Scan complete|Scan returned/i, {
        timeout: 180_000,
      });
      await expect(page.locator("[data-role='rows-not-loaded']")).toHaveCount(0);

      const afterRows = await page.locator(".hr-table tbody tr").count();
      if (kpiN != null && kpiN > 0) {
        // After topic Scan, either rows render OR an explicit filter-mismatch message.
        const empty = page.locator(".hr-empty");
        if (afterRows === 0) {
          await expect(empty).toBeVisible();
          const emptyText = await empty.innerText();
          expect(emptyText).toMatch(/Dashboard still reports|No anomalies|No rows match/i);
          expect(emptyText).not.toMatch(/^No anomalies in this topic\.?$/);
        }
      }

      await captureStep(page, `hsr_dash_grid_${tab.replace(/[^a-z0-9]+/gi, "_")}`);
    }

    const unexpected = errors.filter((e) => !e.includes("favicon") && !e.includes("socket.io"));
    expect(unexpected, unexpected.join("\n")).toEqual([]);
  });
});
