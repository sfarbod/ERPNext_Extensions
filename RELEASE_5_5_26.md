# erpnext_extensions 5.5.26

## Same-item manufacture output family and acquisition rate ownership

5.5.25 is the previous local release. This patch does not change FIX A,
FIX B, or FIX B2, and it does not weaken I3 or the multi-FG fail-closed
guard.

### Same-item output family

A manufacture may have several finished-good rows of one item, including
a retain sample, plus one product-reject row of that same item in a
rejected warehouse. Those rows share one ROUND_HALF_UP material rate.
The difference between the material pool and the composed integer amounts
is the existing IRR value difference. Different-item multi-FG plus a
product reject, a finished good in a rejected warehouse, and an unproven
warehouse stay fail-closed.

MAT-STE-2026-35260-1 stays MAIN_FG / MAIN_FG / MAIN_PRODUCT_REJECT at
rate 808,717. Incoming 890,400,612, outgoing 890,400,434, value
difference 178, GL balanced.

### Authoritative acquisition rate

A positive Purchase Receipt or Purchase Invoice incoming movement keeps
the document valuation rate. The previous warehouse average is not an
acquisition rate. Only a canonically linked invoice rate already written
onto the receipt row is used. Moving average then carries the warehouse
stock value forward, so a stale integer rate cannot replace that value
and explode a later balance.

MAT-PRE-2026-01815 incoming rate 46,000,000 and movement 230,000,000.
MAT-PRE-2026-00498 incoming rate 2,350,000 and movement 47,000,000.
I3 still blocks a genuine positive incoming with a negative movement.

The 16700019 magnitude explosion during repost is the same stale-rate
mechanism. After this carry, MAT-STE-2026-32910-1, MAT-STE-2026-40584,
and MAT-SLE-2026-303475 stay at normal magnitudes. No separate arithmetic
patch was added. Rebuild is not part of RIV.

### Production

Not deployed. Not pushed.
