"""
Reconciliation between the machine server's own order records
(OrderPaymentRecord, db/models.py) and PayU's transaction records
(services/payu_client.py) for a chosen date range -- "a separate
endpoint for reconciliation of machine server and payu pgw," per
explicit request.

Matching key: OrderPaymentRecord.out_trade_no <-> PayUTransaction.txnid.
Confirmed with the account owner that PayU's own "external order id"
(what PayU calls txnid) is the exact same reference the machine server
calls out_trade_no -- see db/models.py's OrderPaymentRecord docstring
for how that was established (the target's own per-order data exposes
PayU-shaped integration fields pointing at a PayU-compatible gateway).

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
    out_trade_no: str
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
    all_out_trade_nos = {o.out_trade_no for o in machine_orders if o.out_trade_no}

    rows: list[ReconciliationRow] = []

    for order in machine_orders:
        if order.pay_type != UPI_PAY_TYPE:
            continue

        key = order.out_trade_no or ""
        payu_txn = payu_by_txnid.get(key) if key else None

        if payu_txn is not None:
            status = MATCHED if payu_txn.amount == order.order_money else AMOUNT_MISMATCH
            rows.append(ReconciliationRow(
                match_status=status, out_trade_no=key, order_code=order.order_code,
                device_app=order.device_app, order_date=order.order_date,
                machine_amount=order.order_money, machine_payment_status=order.payment_status,
                payu_amount=payu_txn.amount, payu_status=payu_txn.status, payu_mihpayid=payu_txn.mihpayid,
            ))
        elif order.payment_status == MACHINE_PAID_STATUS:
            # The machine believes money was collected but PayU has no
            # record for this reference at all -- the most serious kind
            # of mismatch, worth surfacing even without a key.
            rows.append(ReconciliationRow(
                match_status=MISSING_IN_PAYU, out_trade_no=key, order_code=order.order_code,
                device_app=order.device_app, order_date=order.order_date,
                machine_amount=order.order_money, machine_payment_status=order.payment_status,
            ))
        # else: no payment was ever recorded on the machine side either
        # (a genuinely abandoned/failed checkout) -- not a reconciliation
        # problem, so it's not included as a row at all.

    for txn in payu_transactions:
        if txn.status not in PAYU_SUCCESS_STATUSES:
            continue  # a failed/pending PayU attempt with no matching order isn't a reconciliation problem
        if txn.txnid and txn.txnid not in all_out_trade_nos:
            rows.append(ReconciliationRow(
                match_status=MISSING_ON_MACHINE, out_trade_no=txn.txnid,
                payu_amount=txn.amount, payu_status=txn.status, payu_mihpayid=txn.mihpayid,
            ))

    return ReconciliationReport(start=start, end=end, rows=rows)
