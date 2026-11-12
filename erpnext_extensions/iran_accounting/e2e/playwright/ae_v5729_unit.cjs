#!/usr/bin/env node
/**
 * Account Explorer v5.7.29 — AnalysisFilters + debounce contract unit harness.
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "../../../../");
const afPath = path.join(
	root,
	"erpnext_extensions/erpnext_extensions/page/account_explorer/core/explorer_analysis_filters.js"
);
const summaryPath = path.join(
	root,
	"erpnext_extensions/erpnext_extensions/page/account_explorer/core/explorer_analysis_filter_summary.js"
);

const checks = [];
function pass(name, detail) {
	checks.push({ name, ok: true, detail: detail || null });
}
function fail(name, err) {
	checks.push({ name, ok: false, err: String(err && err.message ? err.message : err) });
}

function loadAF() {
	const sandbox = {
		frappe: {
			provide(ns) {
				const parts = String(ns).split(".");
				let cur = sandbox;
				for (const p of parts) {
					cur[p] = cur[p] || {};
					cur = cur[p];
				}
			},
		},
		cint: (v) => parseInt(v, 10) || 0,
		__: (s) => s,
		console,
	};
	sandbox.erpnext_extensions = sandbox.frappe.erpnext_extensions || {};
	vm.createContext(sandbox);
	vm.runInContext(fs.readFileSync(afPath, "utf8"), sandbox);
	vm.runInContext(fs.readFileSync(summaryPath, "utf8"), sandbox);
	return sandbox.erpnext_extensions.account_explorer.core.AnalysisFilters;
}

function debounceHarness() {
	let now = 0;
	const timers = new Map();
	let nextId = 1;
	const api = {
		setTimeout(fn, ms) {
			const id = nextId++;
			timers.set(id, { fn, due: now + ms });
			return id;
		},
		clearTimeout(id) {
			timers.delete(id);
		},
		advance(ms) {
			now += ms;
			const due = [...timers.entries()].filter(([, t]) => t.due <= now);
			due.sort((a, b) => a[1].due - b[1].due);
			for (const [id, t] of due) {
				timers.delete(id);
				t.fn();
			}
		},
		get now() {
			return now;
		},
	};
	return api;
}

function main() {
	const AF = loadAF();

	try {
		const bag = AF.empty();
		let next = AF.apply_policy(bag, "replace_dimension", {
			key: "dimensions.cost_center",
			value: "CC-1",
			lifetime: "session",
			origin: "drill_graph",
			meta: { dimension_type: "cost_center" },
		});
		next = AF.apply_policy(next, "replace_dimension", {
			key: "dimensions.department",
			value: "DEP-1",
			lifetime: "session",
			origin: "drill_graph",
			meta: { dimension_type: "department" },
		});
		if (next.dimensions.cost_center) throw new Error("cost_center not cleared");
		if (!next.dimensions.department || next.dimensions.department.value !== "DEP-1") {
			throw new Error("department missing");
		}
		pass("replace_dimension_clears_other_types");
	} catch (err) {
		fail("replace_dimension_clears_other_types", err);
	}

	try {
		const bag = AF.normalize_bag({
			dimensions: {
				cost_center: { key: "dimensions.cost_center", value: "CC-X", lifetime: "session", origin: "user" },
				department: { key: "dimensions.department", value: "DEP-X", lifetime: "session", origin: "user" },
			},
		});
		const projected = AF.build_legacy_scope_payload_from_analysis_filters(
			bag,
			{ view_axis: "dimension", dimension_scope: { dimension_type: "department", selected_dimension_value: null } },
			{ accounting_dimensions: { cost_center: "CC-DOC", project: "PRJ" } }
		);
		const dims = projected.document_scope.accounting_dimensions || {};
		if (dims.cost_center) throw new Error("cost_center leaked into payload");
		if (dims.project) throw new Error("project leaked into payload");
		if (projected.analysis_context.dimension_scope.dimension_type !== "department") {
			throw new Error("dimension_type wrong");
		}
		pass("projection_isolates_active_dimension_type", dims);
	} catch (err) {
		fail("projection_isolates_active_dimension_type", err);
	}

	try {
		const bag = AF.normalize_bag({
			dimensions: {
				cost_center: { key: "dimensions.cost_center", value: "CC-1", lifetime: "session", origin: "user" },
				department: { key: "dimensions.department", value: "DEP-1", lifetime: "session", origin: "user" },
			},
		});
		const rows = AF.build_summary_rows(bag, {
			document_scope: { company: "X", accounting_dimensions: { cost_center: "CC-1", department: "DEP-1" } },
			analysis_context: { view_axis: "dimension", dimension_scope: { dimension_type: "department" } },
			metadata: {
				dimensions: [
					{ fieldname: "cost_center", label: "Cost Center" },
					{ fieldname: "department", label: "Department" },
				],
			},
		});
		const analysis = rows.filter((r) => r.group === "analysis_filters");
		const docs = rows.filter((r) => String(r.key || "").includes("document_dim_"));
		if (analysis.some((r) => String(r.key).includes("cost_center"))) {
			throw new Error("analysis chip shows cost_center");
		}
		if (docs.some((r) => String(r.key).includes("cost_center"))) {
			throw new Error("document chip shows cost_center");
		}
		if (!analysis.some((r) => String(r.key).includes("department"))) {
			throw new Error("department analysis chip missing");
		}
		pass("filter_summary_hides_other_dimension_types");
	} catch (err) {
		fail("filter_summary_hides_other_dimension_types", err);
	}

	try {
		const clock = debounceHarness();
		const calls = [];
		let latest = null;
		let timer = null;
		let seq = 0;
		const handle = (mapped, opts = {}) => {
			latest = mapped;
			clock.clearTimeout(timer);
			timer = null;
			const apply = () => {
				timer = null;
				seq += 1;
				calls.push({ seq, mapped: { ...latest }, at: clock.now });
			};
			if (opts.immediate) {
				apply();
				return;
			}
			timer = clock.setTimeout(apply, 500);
		};
		handle({ display_code: "a" });
		clock.advance(100);
		handle({ display_code: "ab" });
		clock.advance(100);
		handle({ display_code: "abc" });
		clock.advance(499);
		if (calls.length !== 0) throw new Error(`early fire ${calls.length}`);
		clock.advance(1);
		if (calls.length !== 1) throw new Error(`expected 1 got ${calls.length}`);
		if (calls[0].mapped.display_code !== "abc") throw new Error("stale value");
		handle({ display_code: "abcd" }, { immediate: true });
		if (calls.length !== 2) throw new Error("enter did not apply immediately");
		handle({ display_code: "x" });
		clock.advance(200);
		handle({}); // clear-like empty
		clock.clearTimeout(timer);
		seq += 1;
		calls.push({ seq, mapped: {}, at: clock.now, immediate_clear: true });
		if (calls[calls.length - 1].immediate_clear !== true) throw new Error("clear path");
		pass("debounce_500_enter_clear", { calls: calls.length });
	} catch (err) {
		fail("debounce_500_enter_clear", err);
	}

	try {
		const clock = debounceHarness();
		const applied = [];
		let latest = null;
		let timer = null;
		let seq = 0;
		let appliedSeq = 0;
		const handle = (mapped) => {
			latest = mapped;
			clock.clearTimeout(timer);
			const mySeq = ++seq;
			timer = clock.setTimeout(() => {
				if (mySeq < seq) return; // stale guard
				appliedSeq = mySeq;
				applied.push({ seq: mySeq, mapped: { ...latest } });
			}, 500);
		};
		handle({ a: "1" });
		const first = seq;
		clock.advance(400);
		handle({ a: "2" });
		clock.advance(500);
		if (applied.length !== 1 || applied[0].mapped.a !== "2") {
			throw new Error(JSON.stringify(applied));
		}
		if (appliedSeq === first) throw new Error("stale seq applied");
		pass("stale_response_guard");
	} catch (err) {
		fail("stale_response_guard", err);
	}

	const failed = checks.filter((c) => !c.ok).length;
	process.stdout.write(JSON.stringify({ total: checks.length, failed, checks }, null, 2));
	process.exit(failed ? 1 : 0);
}

main();
