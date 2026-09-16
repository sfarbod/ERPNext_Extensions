# Copyright (c) 2026 — Playwright smoke for Warehouse Plan UI
import { test, expect } from "@playwright/test";

/**
 * Historical Repair — Warehouse Engine UI smoke (dev).
 * Requires site at 127.0.0.1:8000 and admin credentials via env.
 */
test.describe("Historical Repair Warehouse Plan", () => {
  test.skip(!process.env.HR_E2E, "Set HR_E2E=1 to run against local bench");

  test("Warehouse Plan button loads without API 500", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(String(e)));
    page.on("response", (r) => {
      if (r.url().includes("warehouse_plan_api") && r.status() >= 500) {
        errors.push(`API 500 ${r.url()}`);
      }
    });
    await page.goto("http://127.0.0.1:8000/app/historical-repair");
    await page.waitForLoadState("networkidle");
    const btn = page.getByRole("button", { name: /Warehouse Plan/i });
    await expect(btn).toBeVisible({ timeout: 30000 });
    expect(errors.filter((e) => /500|TypeError|ReferenceError/.test(e))).toEqual([]);
  });
});
