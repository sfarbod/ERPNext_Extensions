import { test, expect } from "../src/fixtures/erpnext.fixture";
import { erpnextConfig } from "../src/fixtures/erpnext.fixture";
import { captureStep } from "../src/utils/screenshots";

/**
 * KPI sub-bucket semantic exactness (v5.2.18).
 * Wrong Rate MANUAL must never show RATE_REPAIR_COMPLETE rows.
 */

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
      });
    });
  }
}

async function waitScanAll(page) {
  await expect
    .poll(
      async () => {
        const txt = await kpiValue(page, "Integrity Score").innerText();
        return txt.trim();
      },
      { timeout: 10 * 60_000, intervals: [2_000, 3_000, 5_000] }
    )
    .not.toBe("—");
}

async function collectPlannerStatuses(page): Promise<string[]> {
  // Planner Status column — last badge cell often holds status; scrape preview + table text.
  const cells = page.locator(".hr-table tbody tr");
  const n = await cells.count();
  const statuses: string[] = [];
  for (let i = 0; i < Math.min(n, 200); i++) {
    const text = await cells.nth(i).innerText();
    const m = text.match(
      /(READY_WRONG_RATE|WAITING_PATIENT_ZERO|WAITING_RATE_DEPENDENCY|WAITING_RATE_REPAIR|RATE_MANUAL|RATE_AMBIGUOUS|RATE_REPAIR_COMPLETE|MANUAL|AMBIGUOUS|READY_I4|WAITING_I4|SAFE_TO_RETRY|READY\b)/
    );
    if (m) statuses.push(m[1]);
  }
  return statuses;
}

test.describe("5.2.18 KPI bucket exactness @release-blocking", () => {
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

  test("Wrong Rate READY/WAITING/MANUAL buckets never mix COMPLETE", async ({ page, loginPage }) => {
    if (!process.env.FRAPPE_E2E_SID) {
      await loginPage.login(erpnextConfig.user, erpnextConfig.password);
    }
    await page.goto("/app/historical-repair");
    await expect(page.locator(".hr-dashboard")).toBeVisible({ timeout: 60_000 });

    const companyInput = page
      .locator('.hr-toolbar [data-fieldname="company"] input, .hr-toolbar .frappe-control[data-fieldname="company"] input')
      .first();
    if (await companyInput.count()) {
      await companyInput.fill("اسپاد فارمد دارو");
      await companyInput.press("Tab");
      await page.waitForTimeout(400);
    }
    await page.locator("button[data-action='scan-all']").click();
    await waitScanAll(page);
    await captureStep(page, "kpi_bucket_after_scan_all");

    const cards = ["Wrong Rate READY", "Wrong Rate WAITING", "Wrong Rate MANUAL"] as const;
    const allowed: Record<string, RegExp> = {
      "Wrong Rate READY": /^READY_WRONG_RATE$/,
      "Wrong Rate WAITING": /^WAITING_/,
      "Wrong Rate MANUAL": /^(RATE_MANUAL|RATE_AMBIGUOUS|MANUAL|AMBIGUOUS|RATE_POISONED_OPENING|RATE_WAREHOUSE_ESCALATION)$/,
    };

    for (const card of cards) {
      await waitFreezeGone(page);
      await page
        .locator(".hr-kpi")
        .filter({ has: page.locator(".hr-kpi-label", { hasText: new RegExp(`^${card}$`) }) })
        .click();
      await expect(page.locator("[data-role='kpi-filter']")).toBeVisible({ timeout: 30_000 });
      await expect(page.locator("[data-role='kpi-filter']")).toContainText(card);
      await expect(page.locator("pre[data-role='preview']")).toContainText(/KPI filter|Scan complete|Scan returned/i, {
        timeout: 180_000,
      });
      await waitFreezeGone(page);

      // Prefer server-reported count from preview when present
      const preview = await page.locator("pre[data-role='preview']").innerText();
      expect(preview).toMatch(/bucket=/i);
      expect(preview.toLowerCase()).not.toContain("invariant fail");

      const statuses = await collectPlannerStatuses(page);
      for (const st of statuses) {
        expect(st, `${card} leaked status ${st}`).toMatch(allowed[card]);
        expect(st).not.toBe("RATE_REPAIR_COMPLETE");
      }
      if (card === "Wrong Rate MANUAL") {
        expect(statuses.filter((s) => s === "RATE_REPAIR_COMPLETE")).toEqual([]);
      }
      await captureStep(page, `kpi_bucket_${card.replace(/\s+/g, "_")}`);
    }
  });
});
