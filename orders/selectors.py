"""
Confirmed target endpoints/fields for Order Management -> Order Information.
Discovered live on 2026-08-24 (see docs/target_application_integration_spec.md
for the write-up) the same way Device Information was in Phase 1: this is a
second FastAdmin bootstrap-table list, structurally identical to the device
list API already used by monitoring/.

  GET /NgsEmfuaOv.php/order/order/index
      ?addtabs=1&sort=id&order=desc&offset=<N>&limit=<N>
      &filter={"createtime":"<start-date> - <end-date>"}
      &op={"createtime":"RANGE"}
  Header: X-Requested-With: XMLHttpRequest

CONFIRMED: the createtime RANGE filter's day boundaries are UTC+8 (China
Standard Time), not UTC, not IST, not the querying machine's local time —
verified live across three consecutive days (each day's first/last order,
converted to UTC+8, lands within a few minutes of that day's 00:00:00/
23:59:59). Getting this wrong would silently misalign every daily summary.

That UTC+8 fact is a property of the TARGET's own filter, not of this
app's reporting — per explicit request (2026-08-24), this app's
canonical "which day" for an order is now IST (orders/mapping.py's
IST_TZ), 2.5 hours behind UTC+8. orders/client.py's fetch_day() queries
across two of the target's UTC+8 days to cover one full IST day, then
filters by each order's own IST-computed date — see its module
docstring for the reconciliation.
"""
from __future__ import annotations

ORDER_LIST_API_PATH = "/NgsEmfuaOv.php/order/order/index"
ORDER_LIST_PAGE_SIZE = 100

# The FastAdmin date-range filter this endpoint expects for `filter`/`op`.
CREATETIME_FILTER_FIELD = "createtime"
CREATETIME_FILTER_OP = "RANGE"

# Confirmed raw JSON field -> canonical OrderRecord field. `device.name` is
# a dotted path into the nested `device` object (see orders/mapping.py).
FIELD_MAP: dict[str, str] = {
    "id": "order_id",
    "order_code": "order_code",
    "device_id": "device_id",
    "device.name": "device_app",
    "order_money": "order_money",
    "pay_state_text": "payment_status",
    "delivery_state_text": "delivery_status",
    "order_state_text": "order_status",
    "pay_type_text": "pay_type",
    "goods_name": "goods_name",
    "cup_num": "cup_num",
    "orange_num": "orange_num",
    "orange_weight": "orange_weight",
    "createtime": "createtime",
}

# The filter this feature was explicitly asked to apply when computing the
# summary (confirmed with the user 2026-08-24): only orders that were both
# fully completed AND successfully delivered count toward the numbers.
SUMMARY_FILTER = {
    "order_status": "Completed",
    "delivery_status": "Success",
}
