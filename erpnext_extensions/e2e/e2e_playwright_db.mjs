/**
 * Shared Playwright helpers — database is the source of truth for workflow/accounting E2E.
 *
 * Pattern: user action → server completes → poll DB → assert DB → optional UI match.
 */
import { execSync } from "child_process";

export const BENCH =
  process.env.FRAPPE_BENCH_ROOT || "/workspace/development/frappe-bench";
export const SITE = process.env.FRAPPE_E2E_SITE || "development.localhost";

const GET_STATE_METHOD =
  "erpnext_extensions.e2e.e2e_document_state.e2e_get_document_state";
const EXISTS_METHOD =
  "erpnext_extensions.e2e.e2e_document_state.e2e_document_exists";

const RETRYABLE_INFRA =
  /deadlock|QueryDeadlockError|1020|1213|Lock wait timeout|tabSeries|OperationalError/i;

function syncDelay(ms) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    /* spin */
  }
}

function benchExecuteOnce(method, kwargs, args) {
  let cmd = `cd ${BENCH} && bench --site ${SITE} execute '${method}'`;
  if (args != null) {
    cmd += ` --args '${JSON.stringify(args).replace(/'/g, "'\\''")}'`;
  }
  if (kwargs != null) {
    cmd += ` --kwargs '${JSON.stringify(kwargs).replace(/'/g, "'\\''")}'`;
  }
  let out;
  try {
    out = execSync(cmd, { encoding: "utf8", maxBuffer: 50 * 1024 * 1024 });
  } catch (e) {
    const blob = `${e.stdout || ""}\n${e.stderr || ""}\n${e.message || ""}`;
    throw new Error(
      `bench execute ${method} failed:\n${extractBenchExecuteError(blob)}`
    );
  }
  const lines = out.trim().split("\n").filter(Boolean);
  const last = lines[lines.length - 1];
  try {
    return JSON.parse(last);
  } catch {
    throw new Error(
      `bench execute ${method} did not return JSON. Last line: ${last}\n${extractBenchExecuteError(
        out
      )}`
    );
  }
}

function extractBenchExecuteError(blob) {
  if (!blob) {
    return "(no output)";
  }
  if (/Workflow approver cannot execute/i.test(blob)) {
    const match = blob.match(
      /Workflow approver cannot execute[\s\S]*?(?:Please correct[^\n]*|Finance reviewer required[^\n]*)/i
    );
    if (match) {
      return match[0].replace(/<[^>]+>/g, "").trim();
    }
  }
  const lines = blob.split("\n");
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    const line = lines[i];
    if (
      /ValidationError|PermissionError|WorkflowPermissionError|DoesNotExistError/.test(
        line
      )
    ) {
      return lines.slice(Math.max(0, i - 8), i + 1).join("\n");
    }
  }
  if (/NameError: name 'erpnext_extensions' is not defined/.test(blob)) {
    return (
      "Frappe bench execute eval fallback (underlying prep exception was swallowed). " +
      "Use benchExecutePrep() via e2e_runner.run_e2e_method.\n" +
      blob.slice(-2500)
    );
  }
  return blob.slice(-3000);
}

/** Call E2E prep callables without Frappe execute eval fallback on exceptions. */
export function benchExecutePrep(method, kwargs = null) {
  const envelope = benchExecute(
    "erpnext_extensions.e2e.e2e_runner.run_e2e_method",
    { method, kwargs: kwargs || {} }
  );
  if (!envelope?.ok) {
    throw new Error(
      `E2E prep ${method} failed (${envelope?.exc_type || "Error"}): ${
        envelope?.error || "unknown"
      }`
    );
  }
  return envelope.result;
}

export function benchExecute(
  method,
  kwargs = null,
  args = null,
  { retries = 3 } = {}
) {
  let lastErr;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      return benchExecuteOnce(method, kwargs, args);
    } catch (e) {
      lastErr = e;
      const blob = `${e.message || ""}\n${e.stdout || ""}\n${e.stderr || ""}`;
      if (!RETRYABLE_INFRA.test(blob) || attempt === retries) {
        throw e;
      }
      syncDelay(400 * (attempt + 1) + Math.floor(Math.random() * 300));
    }
  }
  throw lastErr;
}

export function getDocumentState(doctype, name, fields = null) {
  return benchExecute(GET_STATE_METHOD, {
    doctype,
    name,
    fields: fields || undefined,
  });
}

export function documentExists(doctype, name) {
  const res = benchExecute(EXISTS_METHOD, { doctype, name });
  return Boolean(res?.exists);
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function matchesExpected(state, expected) {
  if (!state?.exists) {
    return expected.exists === false;
  }
  for (const [key, value] of Object.entries(expected)) {
    if (key === "exists") {
      continue;
    }
    if (state[key] !== value) {
      return false;
    }
  }
  return true;
}

/**
 * Poll until document fields match ``expected`` (e.g. { workflow_state: "Draft", docstatus: 0 }).
 */
export async function waitDocumentState(
  doctype,
  name,
  expected,
  { timeoutMs = 90000, pollMs = 500, fields = null } = {}
) {
  const startedAt = Date.now();
  let last = null;
  while (Date.now() - startedAt < timeoutMs) {
    last = getDocumentState(doctype, name, fields);
    if (matchesExpected(last, expected)) {
      return {
        ok: true,
        state: last,
        elapsed_ms: Date.now() - startedAt,
        expected,
      };
    }
    await sleep(pollMs);
  }
  return {
    ok: false,
    state: last,
    elapsed_ms: Date.now() - startedAt,
    expected,
  };
}

export function waitWorkflowState(doctype, name, workflowState, opts = {}) {
  return waitDocumentState(
    doctype,
    name,
    { workflow_state: workflowState },
    opts
  );
}

export function waitDocstatus(doctype, name, docstatus, opts = {}) {
  return waitDocumentState(doctype, name, { docstatus }, opts);
}

export async function waitDocumentAbsent(
  doctype,
  name,
  { timeoutMs = 90000, pollMs = 500 } = {}
) {
  return waitDocumentState(
    doctype,
    name,
    { exists: false },
    { timeoutMs, pollMs, fields: ["name"] }
  );
}

/** Structured debug payload when a DB-first assertion fails. */
export function buildFailureDebug({
  test,
  doctype,
  name,
  expected,
  dbBefore = null,
  dbAfter = null,
  ui = null,
  serverResponse = null,
  waitMeta = null,
}) {
  return {
    test,
    timestamp: new Date().toISOString(),
    document: { doctype, name },
    expected,
    workflow_before: dbBefore?.workflow_state ?? null,
    workflow_after: dbAfter?.workflow_state ?? null,
    docstatus_before: dbBefore?.docstatus ?? null,
    docstatus_after: dbAfter?.docstatus ?? null,
    db_before: dbBefore,
    db_after: dbAfter,
    ui_state: ui,
    server_response: serverResponse,
    elapsed_wait_ms: waitMeta?.elapsed_ms ?? null,
  };
}

/**
 * Assert DB matches ``expected`` after optional wait. Returns { ok, db_after, debug }.
 */
export async function assertDbState({
  test,
  doctype,
  name,
  expected,
  dbBefore = null,
  ui = null,
  serverResponse = null,
  timeoutMs = 90000,
  fields = null,
}) {
  const waitMeta = await waitDocumentState(doctype, name, expected, {
    timeoutMs,
    fields,
  });
  const dbAfter = waitMeta.state;
  const ok = waitMeta.ok;
  return {
    ok,
    db_after: dbAfter,
    wait: waitMeta,
    debug: ok
      ? null
      : buildFailureDebug({
          test,
          doctype,
          name,
          expected,
          dbBefore,
          dbAfter,
          ui,
          serverResponse,
          waitMeta,
        }),
  };
}
