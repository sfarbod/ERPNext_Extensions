import { test, expect } from "../src/fixtures/erpnext.fixture";
import { erpnextConfig } from "../src/fixtures/erpnext.fixture";
import { captureStep } from "../src/utils/screenshots";

/**
 * Final release gate — per-topic Scan All → Scan → KPI/Grid → Dry Run → refresh.
 * Fail on silent empty grids or Wrong/Zero cross-routing.
 */

const TOPICS: { tab: string; kpi: string | null }[] = [
  { tab: "Posting Order", kpi: "Posting Order" },
  { tab: "Wrong Rate", kpi: "Wrong Rate" },
  { tab: "Zero / Lost Rate", kpi: "Zero Rate" },
  { tab: "Manufacture Valuation", kpi: null },
  { tab: "SLE / Bin Integrity", kpi: "Broken Bin" },
  { tab: "GL Integrity", kpi: "Broken GL" },
  { tab: "Failed RIV", kpi: "Failed RIV" },
];

function kpiValue(page, label: string) {
  const re = new RegExp(`^${label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`);
  return page
    .locator(".hr-kpi")
    .filter({ has: page.locator(".hr-kpi-label", { hasText: re }) })
    .locator(".hr-kpi-value");
}

async function waitFreezeGone(page) {
  const freeze = page.locator("#freeze");
  try {
    await expect(freeze).toHaveCount(0, { timeout: 5_000 });
  } catch {
    await expect(freeze).toBeHidden({ timeout: 180_000 }).catch(async () => {
      await page.evaluate(() => {
        const el = document.getElementById("freeze");
        if (el) el.remove();
        document.body.classList.remove("modal-open");
      });
    });
  }
  const modal = page.locator(".modal.show");
  if (await modal.count()) {
    await page.keyboard.press("Escape");
    await expect(modal).toHaveCount(0, { timeout: 10_000 }).catch(() => undefined);
  }
}

async function login(page, loginPage) {
  if (!process.env.FRAPPE_E2E_SID) {
    await loginPage.login(erpnextConfig.user, erpnextConfig.password);
  }
}

async function waitScanAll(page) {
  await expect
    .poll(
      async () => {
        const txt = await page
          .locator(".hr-kpi")
          .filter({ has: page.locator(".hr-kpi-label", { hasText: /^Integrity Score$/ }) })
          .locator(".hr-kpi-value")
          .innerText();
        return txt.trim();
      },
      { timeout: 10 * 60_000, intervals: [2_000, 3_000, 5_000] }
    )
    .not.toBe("—");
}

test.describe("5.2.18 Historical Repair final release gate @release-blocking", () => {
  test.setTimeout(25 * 60_000);

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

  test("every topic: Scan All → Scan → KPI/grid contract → Dry Run → refresh", async ({
    page,
    loginPage,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (err) => errors.push(String(err)));
    page.on("response", (res) => {
      if (res.status() >= 500) errors.push(`HTTP ${res.status()} ${res.url()}`);
    });

    await login(page, loginPage);
    await page.goto("/app/historical-repair");
    await expect(page.locator(".hr-dashboard")).toBeVisible({ timeout: 60_000 });
    await expect(page.locator("button.hr-tab", { hasText: "Wrong Rate" })).toBeVisible();
    await expect(page.locator("button[data-action='warehouse-plan']")).toBeVisible();

    // Ensure company is set so Scan All / topic scans share one snapshot.
    const companyInput = page.locator('.hr-toolbar [data-fieldname="company"] input, .hr-toolbar .frappe-control[data-fieldname="company"] input').first();
    if (await companyInput.count()) {
      await companyInput.fill("اسپاد فارمد دارو");
      await companyInput.press("Tab");
      await page.waitForTimeout(500);
    }
    // Explicit Scan All after company is known
    await page.locator("button[data-action='scan-all']").click();

    await waitScanAll(page);
    await captureStep(page, "gate_after_scan_all");

    for (const { tab, kpi } of TOPICS) {
      await waitFreezeGone(page);

      let kpiN: number | null = null;
      if (kpi) {
        const kpiText = (await kpiValue(page, kpi).innerText()).trim();
        kpiN = kpiText === "—" ? null : Number(kpiText.replace(/,/g, ""));
      }

      await page.locator("button.hr-tab", { hasText: tab }).click();
      await waitFreezeGone(page);
      const unload = page.locator("[data-role='rows-not-loaded']");
      const rowCount = await page.locator(".hr-table tbody tr").count();
      if (kpiN != null && kpiN > 0 && rowCount === 0) {
        await expect(unload).toBeVisible();
        await expect(unload).toContainText(/Rows are not loaded yet/i);
        await expect(unload).toContainText(/Click Scan/i);
        const body = await unload.innerText();
        expect(body).not.toMatch(/^No anomalies in this topic\.?$/);
      }

      await page.locator("button[data-action='scan']").click();
      await expect(page.locator("pre[data-role='preview']")).toContainText(/Scan complete|Scan returned/i, {
        timeout: 180_000,
      });
      await waitFreezeGone(page);
      await expect(page.locator("[data-role='rows-not-loaded']")).toHaveCount(0);

      // Select at most one row before Dry Run (Wrong Rate refuses full-scan dry-run).
      const firstCb = page.locator(".hr-table tbody tr input[type=checkbox]").first();
      if (await firstCb.count()) {
        await firstCb.check({ force: true }).catch(() => undefined);
      }

      page.once("dialog", (d) => d.accept().catch(() => undefined));
      await page.locator("button[data-action='dry-run']").click();
      await waitFreezeGone(page);
      await expect(page.locator("pre[data-role='preview']")).toBeVisible({ timeout: 180_000 });
      const preview = await page.locator("pre[data-role='preview']").innerText();
      expect(preview.toLowerCase()).not.toContain("internal server error");
      expect(preview).not.toContain("Traceback");

      // Wrong Rate must never advertise Zero Rate dry-run payload as its scan method
      if (tab === "Wrong Rate") {
        expect(preview).not.toMatch(/scan_zero_rates/i);
      }
      if (tab === "Zero / Lost Rate") {
        expect(preview).not.toMatch(/scan_wrong_rates/i);
      }

      await captureStep(page, `gate_topic_${tab.replace(/[^a-z0-9]+/gi, "_")}`);
    }

    // Refresh must not throw; warehouse plan button still present
    await page.reload();
    await expect(page.locator(".hr-dashboard")).toBeVisible({ timeout: 60_000 });
    await expect(page.locator("button[data-action='warehouse-plan']")).toBeVisible();
    await waitScanAll(page);

    // Warehouse Plan must not throw ReferenceError (handler exists)
    await page.locator("button.hr-tab", { hasText: "Posting Order" }).click();
    await page.locator("button[data-action='scan']").click();
    await expect(page.locator("pre[data-role='preview']")).toContainText(/Scan complete|Scan returned/i, {
      timeout: 180_000,
    });
    page.once("dialog", (d) => d.dismiss().catch(() => undefined));
    await page.locator("button[data-action='warehouse-plan']").click();
    await page.waitForTimeout(1500);
    const pageErrors = errors.filter((e) => /show_warehouse_plan|is not a function|ReferenceError/i.test(e));
    expect(pageErrors, pageErrors.join("\n")).toEqual([]);

    const unexpected = errors.filter((e) => !e.includes("favicon") && !e.includes("socket.io"));
    expect(unexpected, unexpected.join("\n")).toEqual([]);
  });
});
