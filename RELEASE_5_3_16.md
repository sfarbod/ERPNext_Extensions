# RELEASE 5.3.16

- Stock Valuation residual GL destination remains Company.stock_adjustment_account
  (621301), never generic Round Off.
- Transfer EXACT provenance: Stock Reconciliation batch rate at source warehouse
  (and RECO-corroborated previous_healthy) promotes lost-rate transfers to EXACT.
- Warehouse-scoped batch inward SABB; NULL voucher_no no longer excluded by SQL.
- Wrong-rate apply stamps all SE details + SLE + SABB for voucher/item before RIV
  so multi-row transfers (28696) survive dual SRC/TGT repost.
