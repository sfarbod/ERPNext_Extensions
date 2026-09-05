frappe.provide("erpnext_extensions.account_explorer.core");

/**
 * Configurable Drill Graph — nodes, intents, edges, policies (ADR-3B-004).
 */

const AE_DRILL_INTENTS = new Set(["filter", "navigate", "detail", "open", "compare"]);
const AE_DRILL_POLICIES = new Set([
	"append_filter",
	"replace_filter",
	"replace_dimension",
	"keep_filters",
	"clear_drill_filters",
	"consume_temporary",
]);

function ae_drill_edge_key(edge) {
	return [edge.from_node, edge.intent, edge.edge_type || "", edge.target || "", edge.policy || ""].join("|");
}

erpnext_extensions.account_explorer.core.ExplorerDrillGraph = class ExplorerDrillGraph {
	constructor() {
		this._nodes = new Map();
		this._edges = [];
		this._edge_keys = new Set();
		this._default_intents = new Map();
	}

	register_node(node) {
		if (!node || !node.id) {
			return { ok: false, reason: "missing_id" };
		}
		if (this._nodes.has(node.id)) {
			return { ok: false, reason: "duplicate_node", id: node.id };
		}
		this._nodes.set(node.id, { ...node });
		if (node.default_intent && AE_DRILL_INTENTS.has(node.default_intent)) {
			this._default_intents.set(node.id, node.default_intent);
		}
		return { ok: true, id: node.id };
	}

	register_edge(edge) {
		if (!edge || !edge.from_node || !edge.intent) {
			return { ok: false, reason: "invalid_edge" };
		}
		if (!AE_DRILL_INTENTS.has(edge.intent)) {
			return { ok: false, reason: "unknown_intent", intent: edge.intent };
		}
		const policy = edge.policy || "keep_filters";
		if (!AE_DRILL_POLICIES.has(policy)) {
			return { ok: false, reason: "invalid_policy", policy };
		}
		const normalized = {
			from_node: edge.from_node,
			intent: edge.intent,
			edge_type: edge.edge_type || "navigate",
			target: edge.target ?? null,
			policy,
			filter_key: edge.filter_key || null,
			lifetime: edge.lifetime || "session",
			meta: edge.meta || null,
		};
		const key = ae_drill_edge_key(normalized);
		if (this._edge_keys.has(key)) {
			return { ok: false, reason: "duplicate_edge", key };
		}
		this._edge_keys.add(key);
		this._edges.push(normalized);
		return { ok: true, key };
	}

	get_default_intent(node_id) {
		return this._default_intents.get(node_id) || "navigate";
	}

	list_edges(node_id, intent = null) {
		return this._edges.filter((edge) => {
			if (edge.from_node !== node_id) {
				return false;
			}
			if (intent && edge.intent !== intent) {
				return false;
			}
			return true;
		});
	}

	list_intents(node_id) {
		return [...new Set(this.list_edges(node_id).map((edge) => edge.intent))];
	}

	has_node(node_id) {
		return this._nodes.has(node_id);
	}

	/**
	 * Resolve intent into actionable steps. Does not mutate Store.
	 */
	resolve(node_id, intent, row = null, context = {}) {
		const node = this._nodes.get(node_id) || { id: node_id, unknown: !this._nodes.has(node_id) };
		const resolved_intent = intent || this.get_default_intent(node_id);
		if (intent && !AE_DRILL_INTENTS.has(intent)) {
			return {
				node,
				intent: resolved_intent,
				edges: [],
				actions: [],
				error: "unknown_intent",
				row,
				context,
			};
		}
		const edges = this.list_edges(node_id, resolved_intent);
		const actions = edges.map((edge) => ({
			edge_type: edge.edge_type,
			target: edge.target,
			policy: edge.policy,
			filter_key: edge.filter_key,
			lifetime: edge.lifetime,
			row,
			context,
			meta: edge.meta,
		}));
		return {
			node,
			intent: resolved_intent,
			edges,
			actions,
			row,
			context,
			error: edges.length ? null : "no_matching_edge",
		};
	}

	/**
	 * Pure row → node classification (no controller mutation).
	 */
	static classify_row(row, { axis = "account_level", detail_mode = "summary", levels = [] } = {}) {
		if (detail_mode === "grouped_gl") {
			return "GLDetail";
		}
		if (axis === "voucher") {
			return "Voucher";
		}
		if (axis === "party") {
			return "PartyValue";
		}
		if (axis === "unified_party") {
			return "UnifiedPartyValue";
		}
		if (axis === "dimension") {
			return "DimensionValue";
		}
		if (axis === "currency") {
			return "CurrencyValue";
		}
		if (axis === "item_group") {
			return "ItemGroupValue";
		}
		if (axis === "item") {
			return "ItemValue";
		}
		if (axis === "inventory_account") {
			return "InventoryAccountValue";
		}
		if (axis === "account_level") {
			const sorted = [...(levels || [])].sort((a, b) => a.sequence - b.sequence);
			const current = row?.level_sequence;
			const next = sorted.find((lvl) => lvl.enabled && lvl.sequence > current);
			if (!next && row?.selected_account && !row?.is_virtual_group) {
				return "SubsidiaryLedger";
			}
			if (current != null && sorted.length) {
				const index = sorted.findIndex((lvl) => lvl.sequence === current);
				if (index <= 0) {
					return "AccountGroup";
				}
				if (index === 1) {
					return "GeneralLedger";
				}
			}
			return next ? "GeneralLedger" : "SubsidiaryLedger";
		}
		return "AccountGroup";
	}

	static create_default() {
		const graph = new erpnext_extensions.account_explorer.core.ExplorerDrillGraph();

		[
			// Account hierarchy: default = apply session Analysis Filter (presentation level unchanged).
			{ id: "AccountGroup", default_intent: "filter" },
			{ id: "GeneralLedger", default_intent: "filter" },
			{ id: "SubsidiaryLedger", default_intent: "filter" },
			{ id: "DimensionValue", default_intent: "filter" },
			{ id: "CurrencyValue", default_intent: "filter" },
			{ id: "PartyValue", default_intent: "filter" },
			{ id: "UnifiedPartyValue", default_intent: "navigate" },
			{ id: "Voucher", default_intent: "detail" },
			{ id: "GLDetail", default_intent: "open" },
			{ id: "SourceDocument", default_intent: "open" },
			{ id: "ItemGroupValue", default_intent: "navigate" },
			{ id: "ItemValue", default_intent: "filter" },
			{ id: "InventoryAccountValue", default_intent: "navigate" },
		].forEach((node) => graph.register_node(node));

		[
			// v4.6.3: filter intent scopes the account only; keep current Account Levels pill.
			// advance_level is navigate-only so Analyze on Group 11 stays at Group (one row),
			// while explicit navigate / second click still reveals children (1110, 1112, …).
			{
				from_node: "AccountGroup",
				intent: "filter",
				edge_type: "apply_filter",
				target: null,
				policy: "replace_filter",
				filter_key: "account",
				lifetime: "session",
			},
			{
				from_node: "AccountGroup",
				intent: "navigate",
				edge_type: "advance_level",
				target: "GeneralLedger",
				policy: "keep_filters",
			},
			{
				from_node: "GeneralLedger",
				intent: "filter",
				edge_type: "apply_filter",
				target: null,
				policy: "replace_filter",
				filter_key: "account",
				lifetime: "session",
			},
			{
				from_node: "GeneralLedger",
				intent: "navigate",
				edge_type: "advance_level",
				target: "SubsidiaryLedger",
				policy: "keep_filters",
			},
			{
				from_node: "SubsidiaryLedger",
				intent: "filter",
				edge_type: "apply_filter",
				target: null,
				policy: "replace_filter",
				filter_key: "account",
				lifetime: "session",
			},
			{
				from_node: "SubsidiaryLedger",
				intent: "navigate",
				edge_type: "change_axis",
				target: "Voucher",
				policy: "append_filter",
				filter_key: "account",
				lifetime: "session",
			},
			{
				from_node: "Voucher",
				intent: "detail",
				edge_type: "open_detail",
				target: "GLDetail",
				policy: "keep_filters",
			},
			{
				from_node: "Voucher",
				intent: "filter",
				edge_type: "apply_filter",
				target: null,
				policy: "replace_filter",
				filter_key: "voucher",
				lifetime: "session",
			},
			{
				from_node: "GLDetail",
				intent: "open",
				edge_type: "open_source",
				target: "SourceDocument",
				policy: "keep_filters",
			},
			{
				from_node: "DimensionValue",
				intent: "filter",
				edge_type: "apply_filter",
				target: null,
				policy: "replace_dimension",
				filter_key: "dimensions",
				lifetime: "session",
			},
			{
				from_node: "DimensionValue",
				intent: "navigate",
				edge_type: "change_axis",
				target: "Voucher",
				policy: "append_filter",
				lifetime: "session",
			},
			{
				from_node: "CurrencyValue",
				intent: "filter",
				edge_type: "apply_filter",
				target: null,
				policy: "replace_filter",
				filter_key: "currency",
				lifetime: "session",
			},
			{
				from_node: "CurrencyValue",
				intent: "navigate",
				edge_type: "change_axis",
				target: "Voucher",
				policy: "append_filter",
				filter_key: "currency",
				lifetime: "session",
			},
			{
				from_node: "PartyValue",
				intent: "filter",
				edge_type: "apply_filter",
				target: null,
				policy: "replace_filter",
				filter_key: "party",
				lifetime: "session",
			},
			{
				from_node: "PartyValue",
				intent: "navigate",
				edge_type: "change_axis",
				target: "Voucher",
				policy: "append_filter",
				filter_key: "party",
				lifetime: "session",
			},
			{
				from_node: "UnifiedPartyValue",
				intent: "navigate",
				edge_type: "change_axis",
				target: "Voucher",
				policy: "append_filter",
				filter_key: "unified_party",
				lifetime: "session",
			},
			{
				from_node: "DimensionValue",
				intent: "compare",
				edge_type: "apply_filter",
				target: null,
				policy: "append_filter",
				lifetime: "temporary",
				meta: { integration_edge: 1 },
			},
			{
				from_node: "ItemGroupValue",
				intent: "filter",
				edge_type: "apply_filter",
				target: null,
				policy: "replace_filter",
				filter_key: "item_group",
				lifetime: "session",
			},
			{
				from_node: "ItemGroupValue",
				intent: "navigate",
				edge_type: "apply_filter",
				target: null,
				policy: "replace_filter",
				filter_key: "item_group",
				lifetime: "session",
			},
			{
				from_node: "ItemGroupValue",
				intent: "navigate",
				edge_type: "change_axis",
				target: "item",
				policy: "keep_filters",
			},
			{
				from_node: "ItemValue",
				intent: "filter",
				edge_type: "apply_filter",
				target: null,
				policy: "replace_filter",
				filter_key: "item",
				lifetime: "session",
			},
			{
				from_node: "InventoryAccountValue",
				intent: "filter",
				edge_type: "apply_filter",
				target: null,
				policy: "replace_filter",
				filter_key: "inventory_account",
				lifetime: "session",
			},
			{
				from_node: "InventoryAccountValue",
				intent: "navigate",
				edge_type: "open_stock_ledger",
				target: null,
				policy: "keep_filters",
			},
			{
				from_node: "InventoryAccountValue",
				intent: "detail",
				edge_type: "open_general_ledger",
				target: null,
				policy: "keep_filters",
			},
		].forEach((edge) => graph.register_edge(edge));

		return graph;
	}
};
