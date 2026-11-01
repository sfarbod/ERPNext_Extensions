// Copyright (c) 2026, ERPNext Extensions contributors
// License: MIT
//
// Recalculate Asset net_purchase_amount / purchase_amount when asset_quantity
// changes after a Purchase Receipt or Purchase Invoice is selected.
//
// Core get_values_from_purchase_doc fills remaining-row TOTALS (rate × remaining
// qty). Core has no asset_quantity handler, so editing qty left a stale total.
// This script stores the unit rate from the purchase-values response (including
// conversion_factor already baked into that total) and reapplies
// unit_rate × asset_quantity. It does not change the default qty returned by
// core on purchase-source selection.

frappe.provide("erpnext_extensions.asset_purchase_amount");

(function () {
	const NS = erpnext_extensions.asset_purchase_amount;
	const STATE_KEY = "__ee_asset_purchase_unit_rate_state";

	NS.unit_rate_from_purchase_values = function (net_purchase_amount, remaining_qty) {
		const rem = flt(remaining_qty);
		if (!(rem > 0)) {
			return null;
		}
		return flt(net_purchase_amount) / rem;
	};

	NS.amount_for_quantity = function (unit_rate, asset_quantity, precision) {
		const qty = flt(asset_quantity);
		const rate = flt(unit_rate);
		if (!(qty > 0) || !(rate > 0)) {
			return null;
		}
		if (precision == null || precision === "") {
			return flt(rate * qty);
		}
		return flt(rate * qty, precision);
	};

	NS.source_fingerprint = function (doc) {
		if (!doc) {
			return null;
		}
		const purchase_doc = doc.purchase_receipt || doc.purchase_invoice;
		if (!purchase_doc || !doc.item_code) {
			return null;
		}
		const doctype = doc.purchase_receipt ? "Purchase Receipt" : "Purchase Invoice";
		const row =
			(doc.purchase_receipt && doc.purchase_receipt_item) ||
			(doc.purchase_invoice && doc.purchase_invoice_item) ||
			"";
		return [doctype, purchase_doc, doc.item_code, row].join("::");
	};

	NS.clear_unit_rate = function (frm) {
		if (!frm) {
			return;
		}
		frm[STATE_KEY] = null;
	};

	NS.store_unit_rate = function (frm, unit_rate, meta) {
		if (!frm || !(flt(unit_rate) > 0) || !meta) {
			NS.clear_unit_rate(frm);
			return;
		}
		frm[STATE_KEY] = {
			unit_rate: flt(unit_rate),
			doctype: meta.doctype,
			purchase_doc: meta.purchase_doc,
			item_code: meta.item_code,
			row: meta.row || "",
			fingerprint: [meta.doctype, meta.purchase_doc, meta.item_code, meta.row || ""].join("::"),
		};
	};

	NS.get_cached_unit_rate = function (frm) {
		if (!frm || !frm[STATE_KEY]) {
			return null;
		}
		const state = frm[STATE_KEY];
		if (!(flt(state.unit_rate) > 0)) {
			return null;
		}
		const doctype = frm.doc.purchase_receipt
			? "Purchase Receipt"
			: frm.doc.purchase_invoice
				? "Purchase Invoice"
				: null;
		const purchase_doc = frm.doc.purchase_receipt || frm.doc.purchase_invoice;
		if (!doctype || doctype !== state.doctype || purchase_doc !== state.purchase_doc) {
			return null;
		}
		if (frm.doc.item_code !== state.item_code) {
			return null;
		}
		// Row may still be empty while core fills fields in order; reject only on mismatch.
		const doc_row = frm.doc.purchase_receipt_item || frm.doc.purchase_invoice_item || "";
		if (doc_row && state.row && doc_row !== state.row) {
			return null;
		}
		return flt(state.unit_rate);
	};

	function purchase_doctype(frm) {
		if (frm.doc.purchase_receipt) {
			return "Purchase Receipt";
		}
		if (frm.doc.purchase_invoice) {
			return "Purchase Invoice";
		}
		return null;
	}

	function has_purchase_source(frm) {
		return Boolean(purchase_doctype(frm) && frm.doc.item_code);
	}

	function amount_precision(frm) {
		return frm.fields_dict.net_purchase_amount
			? frm.fields_dict.net_purchase_amount.df.precision
			: null;
	}

	function apply_amount(frm, amount) {
		if (amount == null || !(flt(amount) >= 0)) {
			return;
		}
		const prec = amount_precision(frm);
		const rounded = prec != null && prec !== "" ? flt(amount, cint(prec)) : flt(amount);
		const same_net = flt(frm.doc.net_purchase_amount) === rounded;
		const same_purchase = flt(frm.doc.purchase_amount) === rounded;
		if (same_net && same_purchase) {
			return;
		}
		frm.__ee_applying_purchase_amount_from_qty = true;
		const tasks = [];
		if (!same_net) {
			tasks.push(frm.set_value("net_purchase_amount", rounded));
		}
		if (!same_purchase) {
			tasks.push(frm.set_value("purchase_amount", rounded));
		}
		Promise.all(tasks).finally(() => {
			frm.__ee_applying_purchase_amount_from_qty = false;
		});
	}

	function capture_unit_rate_from_response(frm, doctype, data) {
		const rem_qty = flt(data.asset_quantity);
		const unit = NS.unit_rate_from_purchase_values(data.net_purchase_amount, rem_qty);
		if (unit == null) {
			NS.clear_unit_rate(frm);
			return null;
		}
		NS.store_unit_rate(frm, unit, {
			doctype: doctype,
			purchase_doc: frm.doc.purchase_receipt || frm.doc.purchase_invoice,
			item_code: frm.doc.item_code,
			row: data.purchase_receipt_item || data.purchase_invoice_item || "",
		});
		return unit;
	}

	function fetch_unit_rate(frm) {
		const doctype = purchase_doctype(frm);
		if (!doctype) {
			return Promise.resolve(null);
		}
		return frappe
			.call({
				method: "erpnext.assets.doctype.asset.asset.get_values_from_purchase_doc",
				args: {
					purchase_doc_name: frm.doc.purchase_receipt || frm.doc.purchase_invoice,
					item_code: frm.doc.item_code,
					doctype: doctype,
					asset: frm.is_new() ? null : frm.doc.name,
				},
			})
			.then((r) => {
				if (!r || !r.message) {
					return null;
				}
				return capture_unit_rate_from_response(frm, doctype, r.message);
			})
			.catch(() => null);
	}

	NS.recalculate_amount_for_quantity = function (frm) {
		if (!frm || frm.__ee_applying_purchase_amount_from_qty) {
			return;
		}
		if (frm.doc.docstatus && cint(frm.doc.docstatus) > 0) {
			return;
		}
		if (!has_purchase_source(frm)) {
			return;
		}
		const qty = flt(frm.doc.asset_quantity);
		if (!(qty > 0)) {
			return;
		}

		const cached = NS.get_cached_unit_rate(frm);
		if (cached != null) {
			apply_amount(frm, NS.amount_for_quantity(cached, qty, amount_precision(frm)));
			return;
		}

		fetch_unit_rate(frm).then((unit) => {
			if (unit == null) {
				return;
			}
			// Re-read qty in case it changed while the RPC was in flight.
			const current_qty = flt(frm.doc.asset_quantity);
			if (!(current_qty > 0)) {
				return;
			}
			if (!has_purchase_source(frm)) {
				return;
			}
			apply_amount(frm, NS.amount_for_quantity(unit, current_qty, amount_precision(frm)));
		});
	};

	function wrap_set_values_from_purchase_doc(frm) {
		if (!frm.events || frm.events.__ee_purchase_values_qty_wrapped) {
			return;
		}
		const original = frm.events.set_values_from_purchase_doc;
		if (typeof original !== "function") {
			return;
		}
		frm.events.__ee_purchase_values_qty_wrapped = true;

		frm.events.set_values_from_purchase_doc = function (frm, doctype) {
			frappe.call({
				method: "erpnext.assets.doctype.asset.asset.get_values_from_purchase_doc",
				args: {
					purchase_doc_name: frm.doc.purchase_receipt || frm.doc.purchase_invoice,
					item_code: frm.doc.item_code,
					doctype: doctype,
					asset: frm.is_new() ? null : frm.doc.name,
				},
				callback: (r) => {
					if (!r.message) {
						NS.clear_unit_rate(frm);
						return;
					}
					const data = r.message;
					capture_unit_rate_from_response(frm, doctype, data);

					frm.set_value("company", data.company);
					frm.set_value("purchase_date", data.purchase_date);
					frm.set_value("net_purchase_amount", data.net_purchase_amount);
					frm.set_value("purchase_amount", data.net_purchase_amount);
					frm.set_value("asset_quantity", data.asset_quantity);
					frm.set_value("cost_center", data.cost_center);
					if (data.asset_location) {
						frm.set_value("location", data.asset_location);
					}
					if (doctype === "Purchase Receipt") {
						frm.set_value("purchase_receipt_item", data.purchase_receipt_item);
					} else {
						frm.set_value("purchase_invoice_item", data.purchase_invoice_item);
					}
				},
			});
		};
	}

	frappe.ui.form.on("Asset", {
		setup(frm) {
			wrap_set_values_from_purchase_doc(frm);
		},

		refresh(frm) {
			// Core doctype JS may register set_values_from_purchase_doc after app setup.
			wrap_set_values_from_purchase_doc(frm);
		},

		item_code(frm) {
			NS.clear_unit_rate(frm);
		},

		purchase_receipt(frm) {
			if (!frm.doc.purchase_receipt) {
				NS.clear_unit_rate(frm);
			}
		},

		purchase_invoice(frm) {
			if (!frm.doc.purchase_invoice) {
				NS.clear_unit_rate(frm);
			}
		},

		asset_quantity(frm) {
			NS.recalculate_amount_for_quantity(frm);
		},
	});
})();
