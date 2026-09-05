/**
 * Playwright suite registry — tags drive runner (parallel vs serial).
 *
 * Tags: FAST | ISOLATED | SERIAL | ACCOUNTING | ROLLBACK | IMPORT | UI_ONLY
 */
import fs from "fs";
import path from "path";

export const SUITE_REGISTRY = [
  {
    script: "cheque_management/e2e/playwright_cheque_leaf_void.mjs",
    tags: ["ISOLATED", "SERIAL", "ACCOUNTING", "ROLLBACK"],
  },
  {
    script:
      "cheque_management/e2e/playwright_cheque_opening_import_delete_pdc.mjs",
    tags: ["ISOLATED", "SERIAL", "IMPORT", "ACCOUNTING"],
  },
  {
    script:
      "cheque_management/e2e/playwright_delete_imported_pdc_button_proof.mjs",
    tags: ["FAST", "ISOLATED", "IMPORT"],
  },
  {
    script:
      "cheque_management/e2e/playwright_delete_imported_pdc_dialog_scenarios.mjs",
    tags: ["FAST", "ISOLATED", "IMPORT"],
  },
  {
    script: "cheque_management/e2e/playwright_pdc_desk_list_filters.mjs",
    tags: ["FAST", "UI_ONLY"],
  },
  {
    script: "cheque_management/e2e/playwright_pdc_list_filters.mjs",
    tags: ["FAST", "UI_ONLY"],
  },
  {
    script: "cheque_management/e2e/playwright_pdc_workflow_rollback.mjs",
    tags: ["ISOLATED", "SERIAL", "ROLLBACK", "ACCOUNTING"],
  },
  {
    script: "facility_management/e2e/playwright_facility_defaults.mjs",
    tags: ["ISOLATED", "SERIAL", "UI_ONLY"],
  },
  {
    script: "facility_management/e2e/playwright_facility_dimension_links.mjs",
    tags: ["FAST", "UI_ONLY"],
  },
  {
    script: "facility_management/e2e/playwright_facility_je_preview.mjs",
    tags: ["ISOLATED", "SERIAL", "ACCOUNTING"],
  },
  {
    script:
      "facility_management/e2e/playwright_facility_receipt_je_dimensions.mjs",
    tags: ["ISOLATED", "SERIAL", "ACCOUNTING"],
  },
  {
    script:
      "facility_management/e2e/playwright_facility_repayment_draft_overrides.mjs",
    tags: ["ISOLATED", "SERIAL", "ACCOUNTING"],
  },
  {
    script: "facility_management/e2e/playwright_facility_repayment_je.mjs",
    tags: ["ISOLATED", "SERIAL", "ACCOUNTING"],
  },
  {
    script: "facility_management/e2e/playwright_facility_type_templates.mjs",
    tags: ["ISOLATED", "SERIAL", "ACCOUNTING"],
  },
  {
    script: "facility_management/e2e/playwright_facility_usability.mjs",
    tags: ["ISOLATED", "SERIAL", "UI_ONLY"],
  },
  {
    script: "petty_management/e2e/playwright_pm_multi_pe.mjs",
    tags: ["ISOLATED", "SERIAL", "ACCOUNTING"],
  },
  {
    script: "petty_management/e2e/playwright_pm_request_form_smoke.mjs",
    tags: ["FAST", "UI_ONLY"],
  },
  {
    script: "petty_management/e2e/playwright_pm_request_pe_list_e2e.mjs",
    tags: ["ISOLATED", "SERIAL", "ACCOUNTING"],
  },
  {
    script: "asset_usage_depreciation/e2e/playwright_asset_request.mjs",
    tags: ["ISOLATED", "SERIAL", "UI_ONLY"],
  },
  {
    script: "asset_usage_depreciation/e2e/playwright_asset_request_dimensions.mjs",
    tags: ["ISOLATED", "SERIAL", "ACCOUNTING", "UI_ONLY"],
  },
  {
    script:
      "iran_accounting/e2e/playwright_account_explorer_empty_classification.mjs",
    tags: ["ISOLATED", "SERIAL", "ACCOUNTING", "UI_ONLY"],
  },
  {
    script:
      "iran_accounting/e2e/playwright_account_explorer_hierarchy_filter.mjs",
    tags: ["ISOLATED", "SERIAL", "ACCOUNTING", "UI_ONLY"],
  },
  {
    script:
      "iran_accounting/e2e/playwright_account_explorer_jalali_dates.mjs",
    tags: ["ISOLATED", "SERIAL", "ACCOUNTING", "UI_ONLY"],
  },
  {
    script:
      "iran_accounting/e2e/playwright_account_explorer_inventory.mjs",
    tags: ["ISOLATED", "SERIAL", "ACCOUNTING", "UI_ONLY"],
  },
  {
    script:
      "iran_accounting/e2e/playwright_account_explorer_asymmetric_contract.mjs",
    tags: ["ISOLATED", "SERIAL", "ACCOUNTING", "UI_ONLY"],
  },
  {
    script:
      "iran_accounting/e2e/playwright_account_explorer_synthetic_rows.mjs",
    tags: ["ISOLATED", "SERIAL", "ACCOUNTING", "UI_ONLY"],
  },
];

export function registryByScript() {
  return new Map(SUITE_REGISTRY.map((e) => [e.script, e]));
}

export function discoverScripts(root) {
  const out = [];
  function walk(dir) {
    for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, ent.name);
      if (
        ent.isDirectory() &&
        ent.name !== "node_modules" &&
        ent.name !== "screenshots"
      ) {
        walk(p);
      } else if (
        ent.isFile() &&
        ent.name.startsWith("playwright") &&
        ent.name.endsWith(".mjs") &&
        ent.name !== "playwright_suites.mjs"
      ) {
        out.push(path.relative(root, p).replace(/\\/g, "/"));
      }
    }
  }
  walk(root);
  return out.sort();
}

export function tagsForScript(relPath) {
  return registryByScript().get(relPath)?.tags ?? ["ISOLATED"];
}
