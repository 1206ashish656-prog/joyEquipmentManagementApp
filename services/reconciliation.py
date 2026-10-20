"""
Machine server <-> PayU PGW reconciliation (admin-only, /reconciliation)
-- a separate endpoint from Cost Management/Order Summary, per explicit
request. Compares OrderPaymentRecord (individual machine orders,
db/models.py) against PayU's own transaction records
(services/payu_client.py) for a chosen date range, via
services/reconciliation.py's pure matching logic.

Matching key: OrderPaymentRecord.order_code <-> PayUTransaction.txnid
(CORRECTED 2026-10-20, confirmed live against a real production PayU
MID -- see ../payu-transactions-experiment, a standalone read-only
experiment folder outside this repo that first proved the Merchant API
credentials/hash worked end-to-end). The original assumption here was
OrderPaymentRecord.out_trade_no <-> txnid (see db/models.py's
OrderPaymentRecord docstring before this date), based on the target's
raw order data exposing PayU-shaped integration fields under that name
-- plausible, but never actually verified against a live PayU account,
since PAYU_MERCHANT_KEY/SALT were never configured until now. Once they
were, matching on out_trade_no produced ZERO matches across 721 real
UPI orders against 743 real PayU transactions for the same 3-day
window, while order_code matched all 721 -- and out_trade_no instead
matches PayU's own internal transaction id (what PayU calls "id" in
this API's response, surfaced as PayUTransaction.mihpayid). So
out_trade_no is a real, useful PayU correlation field, just not the
txnid one.

Only pay_type == "UPI" orders are PayU-eligible on the machine side --
the only other value ever seen, "Self-check repair", is a free/
maintenance category PayU never sees, so it's excluded from matching
entirely rather than showing up as a false "missing" row.

build_reconciliation() is pure matching logic (no I/O) so it's directly
unit-testable against fixed inputs -- backend/api/reconciliation.py
does the actual DB query + PayU API call and passes the results in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from db.models import OrderPaymentRecord
from services.payu_client import PayUTransaction

UPI_PAY_TYPE = "UPI"
MACHINE_PAID_STATUS = "Have paid"  # OrderPaymentRecord.payment_status meaning "machine recorded this as paid"
PAYU_SUCCESS_STATUSES = {"success", "captured"}

MATCHED = "Matched"
AMOUNT_MISMATCH = "Amount Mismatch"
MISSING_IN_PAYU = "Missing in PayU"
MISSING_ON_MACHINE = "Missing on Machine Server"


@dataclass
class ReconciliationRow:
    match_status: str
    match_key: str  # OrderPaymentRecord.order_code == PayUTransaction.txnid
    order_code: str | None = None
    device_app: str | None = None
    order_date: str | None = None
    machine_amount: Decimal | None = None
    machine_payment_status: str | None = None
    payu_amount: Decimal | None = None
    payu_status: str | None = None
    payu_mihpayid: str | None = None


@dataclass
class ReconciliationReport:
    start: str
    end: str
    rows: list[ReconciliationRow] = field(default_factory=list)

    @property
    def matched_count(self) -> int:
        return sum(1 for r in self.rows if r.match_status == MATCHED)

    @property
    def mismatch_count(self) -> int:
        return sum(1 for r in self.rows if r.match_status == AMOUNT_MISMATCH)

    @property
    def missing_in_payu_count(self) -> int:
        return sum(1 for r in self.rows if r.match_status == MISSING_IN_PAYU)

    @property
    def missing_on_machine_count(self) -> int:
        return sum(1 for r in self.rows if r.match_status == MISSING_ON_MACHINE)

    @property
    def has_problems(self) -> bool:
        return self.mismatch_count or self.missing_in_payu_count or self.missing_on_machine_count


def build_reconciliation(
    machine_orders: list[OrderPaymentRecord],
    payu_transactions: list[PayUTransaction],
    start: str,
    end: str,
) -> ReconciliationReport:
    payu_by_txnid = {t.txnid: t for t in payu_transactions if t.txnid}
    all_order_codes = {o.order_code for o in machine_orders if o.order_code}

    rows: list[ReconciliationRow] = []

    for order in machine_orders:
        if order.pay_type != UPI_PAY_TYPE:
            continue

        key = order.order_code or ""
        payu_txn = payu_by_txnid.get(key) if key else None

        if payu_txn is not None:
            status = MATCHED if payu_txn.amount == order.order_money else AMOUNT_MISMATCH
            rows.append(ReconciliationRow(
                match_status=status, match_key=key, order_code=order.order_code,
                device_app=order.device_app, order_date=order.order_date,
                machine_amount=order.order_money, machine_payment_status=order.payment_status,
                payu_amount=payu_txn.amount, payu_status=payu_txn.status, payu_mihpayid=payu_txn.mihpayid,
            ))
        elif order.payment_status == MACHINE_PAID_STATUS:
            # The machine believes money was collected but PayU has no
            # record for this reference at all -- the most serious kind
            # of mismatch, worth surfacing even without a key.
            rows.append(ReconciliationRow(
                match_status=MISSING_IN_PAYU, match_key=key, order_code=order.order_code,
                device_app=order.device_app, order_date=order.order_date,
                machine_amount=order.order_money, machine_payment_status=order.payment_status,
            ))
        # else: no payment was ever recorded on the machine side either
        # (a genuinely abandoned/failed checkout) -- not a reconciliation
        # problem, so it's not included as a row at all.

    for txn in payu_transactions:
        if txn.status not in PAYU_SUCCESS_STATUSES:
            continue  # a failed/pending PayU attempt with no matching order isn't a reconciliation problem
        if txn.txnid and txn.txnid not in all_order_codes:
            rows.append(ReconciliationRow(
                match_status=MISSING_ON_MACHINE, match_key=txn.txnid,
                payu_amount=txn.amount, payu_status=txn.status, payu_mihpayid=txn.mihpayid,
            ))

    return ReconciliationReport(start=start, end=end, rows=rows)
