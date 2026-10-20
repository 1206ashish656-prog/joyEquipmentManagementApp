"""
PayUClient: calls PayU's "Get Transaction Details" API to fetch every
transaction in a date range, for services/reconciliation.py. Confirmed
live against PayU's own developer docs (docs.payu.in) before writing
this:

    POST https://info.payu.in/merchant/postservice.php?form=2   (production)
    POST https://test.payu.in/merchant/postservice.php?form=2   (test/sandbox)
    command=get_Transaction_Details
    key=<merchant key>
    var1=<start date, YYYY-MM-DD>
    var2=<end date, YYYY-MM-DD>
    hash=sha512(f"{key}|get_Transaction_Details|{var1}|{salt}")

Deliberately independent of monitoring/lightweight_client.py's target-
application client -- this talks to PayU, not jwintell.com, and has
nothing to do with that saved session.

Credentials come from Settings (monitoring/config.py), sourced from
.env -- never hardcoded (same rule as the target application's own
credentials).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

from monitoring.config import Settings
from monitoring.models import ExtractionError, TargetUnavailableError

logger = logging.getLogger(__name__)

GET_TRANSACTION_DETAILS_COMMAND = "get_Transaction_Details"
# "Get All Refunds for a Transaction IDs" (docs.payu.in/reference/
# get_all_refunds_from_transaction_ids_api) -- one transaction per call,
# hash = sha512(key|command|var1|salt). var1 CORRECTED 2026-10-20 after
# a real refunded transaction (confirmed refunded on PayU's own
# dashboard) came back "No Refunds Found" when queried by mihpayid, the
# ID this command's docs page's prose seemed to describe. Tested
# directly against the live account: var1=mihpayid -> "No Refunds
# Found"; var1=txnid (PayUTransaction.txnid, == OrderPaymentRecord.
# order_code) -> the real refund, correctly. So var1 here is the
# merchant's own txnid/order_code, NOT mihpayid -- the opposite of
# get_Transaction_Details's own "id" field convention (see
# _map_transaction's docstring) and of what the docs page's wording
# suggested. Yet another case (like services/reconciliation.py's
# out_trade_no/order_code mixup) where this account's live behavior
# doesn't match the generic docs -- don't trust the prose over a live
# test again.
GET_ALL_REFUNDS_COMMAND = "getAllRefundsFromTxnIds"
# "Refund Transaction API" (docs.payu.in/reference/refund_transaction_api)
# -- the actual refund-TRIGGER command (as opposed to the read-only
# lookups above). var1=mihpayid, var2=merchant-generated token (<=23
# chars, also enforced as an idempotency key ON PAYU'S OWN SIDE -- a
# reused token comes back {"status": 0, "msg": "token already used or
# request pending"} per PayU's own docs example), var3=refund amount.
# Hash formula was NOT documented by PayU (their own docs page admits
# this) -- confirmed live 2026-10-20 against the TEST MID (a real
# sandbox transaction, TEST087B9B9ACFF34BC8/mihpayid 20000078392, had no
# refund beforehand -- confirmed via getAllRefundsFromTxnIds -- then a
# live trigger call with hash=sha512(key|command|var1|salt) (var1 ONLY,
# same shape as every other command in this file -- var2/var3 are sent
# but NOT hashed) returned {"status": 1, "msg": "Refund Request Queued",
# "request_id": "900000000000423571", ...}, and the refund subsequently
# appeared via getAllRefundsFromTxnIds with that same request_id).
# Candidates that included var2/var3 in the hash, or put salt in a
# different position, all came back {"status": 0, "msg": "Invalid
# Hash."} first.
CANCEL_REFUND_COMMAND = "cancel_refund_transaction"


class PayUNotConfiguredError(Exception):
    """Raised when PAYU_MERCHANT_KEY/PAYU_MERCHANT_SALT aren't set --
    distinct from a network/API failure, so the UI can show "add your
    PayU credentials to .env" rather than a confusing error."""


@dataclass
class PayUTransaction:
    txnid: str  # the merchant's own reference -- matches OrderPaymentRecord.out_trade_no
    mihpayid: str  # PayU's own internal transaction id
    amount: Decimal
    status: str  # e.g. "success", "failure", "pending"
    mode: str  # e.g. "UPI", "CC"
    addedon: str  # PayU's own timestamp string, preserved as-is
    bank_ref_no: str
    raw: dict


@dataclass
class PayURefund:
    """One refund/cancel action PayU has on record against a transaction
    (there can be more than one -- partial refunds).

    refund_token is what PayU's OWN dashboard displays as "Refund ID"
    on a transaction's detail page (confirmed 2026-10-20 by matching a
    real refunded transaction's dashboard screenshot against this API's
    response for it: dashboard showed "Refund ID: 31386182688_506445f139",
    which is exactly this response's RefundToken field) -- use this one
    for anything shown to a human as "the refund id".

    request_id is a DIFFERENT PayU-assigned id (the one
    cancel_refund_transaction itself returns, and what check_action_status
    takes as input to poll a refund's progress) -- present on the same
    row but not what the dashboard calls "Refund ID". Kept since it may
    be useful for a future check_action_status integration, but do not
    confuse it with refund_token."""
    refund_token: str
    request_id: str
    amount: Decimal
    status: str  # e.g. "success", "queued", "in_progress", "failure"
    created_at: str  # PayU's own "RefundCreationDate" string, preserved as-is
    bank_ref_no: str
    raw: dict


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


def _map_transaction(row: dict) -> PayUTransaction:
    return PayUTransaction(
        txnid=str(row.get("txnid") or "").strip(),
        # Confirmed live against a real production MID (2026-10-20, see
        # ../payu-transactions-experiment): get_Transaction_Details's own
        # row shape uses "id" for PayU's internal transaction id, NOT
        # "mihpayid"/"mihpayupid" (those are the field names PayU's
        # webhook/redirect payloads use, which this list API doesn't
        # share) -- every real row had "id" populated and neither of the
        # other two keys present at all. Kept as fallbacks in case a
        # different account/response variant does send one of them.
        mihpayid=str(row.get("mihpayid") or row.get("mihpayupid") or row.get("id") or "").strip(),
        # Confirmed live 2026-10-20: for some DBQR/QR-callback-settled
        # rows (status "captured", field9 "...Completed Using
        # Callback"), "amount" comes back "0.00" while "transaction_fee"
        # holds the real collected amount -- this affected real
        # production orders (324721, 323879) and would also have been
        # silently causing false AMOUNT_MISMATCH rows in
        # services/reconciliation.py for anything matched this way.
        # Falls back to transaction_fee only when amount itself is zero,
        # so a genuine zero-amount transaction (if one ever exists)
        # isn't overridden by a stale transaction_fee.
        amount=_to_decimal(row.get("amount")) or _to_decimal(row.get("transaction_fee")),
        status=str(row.get("status") or "").strip().lower(),
        mode=str(row.get("mode") or "").strip(),
        addedon=str(row.get("addedon") or "").strip(),
        bank_ref_no=str(row.get("bank_ref_no") or "").strip(),
        raw=row,
    )


def _map_refund(row: dict) -> PayURefund:
    return PayURefund(
        refund_token=str(row.get("RefundToken") or "").strip(),
        request_id=str(row.get("RequestID") or "").strip(),
        amount=_to_decimal(row.get("Amount")),
        status=str(row.get("Status") or "").strip().lower(),
        created_at=str(row.get("RefundCreationDate") or "").strip(),
        bank_ref_no=str(row.get("bank_ref_no") or "").strip(),
        raw=row,
    )


@dataclass
class PayURefundInitiationResult:
    """The direct response from calling cancel_refund_transaction --
    distinct from PayURefund (which comes from the separate
    getAllRefundsFromTxnIds lookup). success=False covers both a clean
    PayU-side rejection (e.g. invalid amount, already refunded) and a
    duplicate token -- either way, PayU received and processed the
    request, so this is still a definitive response, not a transport
    failure (those raise instead -- see initiate_refund())."""
    success: bool
    request_id: str | None  # PayU's own id for tracking this specific refund action, if given
    message: str
    mihpayid: str
    raw: dict


class PayUClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._http = httpx.AsyncClient(timeout=30.0)

    def _hash(self, command: str, *vars_: str) -> str:
        parts = [self.settings.payu_merchant_key, command, *vars_, self.settings.payu_merchant_salt]
        return hashlib.sha512("|".join(parts).encode("utf-8")).hexdigest()

    async def get_transaction_details(self, start_date: str, end_date: str) -> list[PayUTransaction]:
        """start_date/end_date: 'YYYY-MM-DD'. Per PayU's own docs, the
        hash is computed over var1 (the start date) only -- var2 is sent
        unsigned. Returns every transaction PayU has recorded in the
        range, regardless of status (success/failure/pending) -- the
        caller (services/reconciliation.py) decides what counts as a
        problem."""
        if not self.settings.payu_configured:
            raise PayUNotConfiguredError(
                "PAYU_MERCHANT_KEY/PAYU_MERCHANT_SALT are not set in .env — "
                "add them from https://payu.in/business/transactions before running reconciliation."
            )

        url = f"{self.settings.payu_base_url}/merchant/postservice.php?form=2"
        payload = {
            "key": self.settings.payu_merchant_key,
            "command": GET_TRANSACTION_DETAILS_COMMAND,
            "var1": start_date,
            "var2": end_date,
            "hash": self._hash(GET_TRANSACTION_DETAILS_COMMAND, start_date),
        }

        try:
            resp = await self._http.post(url, data=payload)
        except httpx.HTTPError as e:
            raise TargetUnavailableError(f"Could not reach PayU: {e}") from e

        if resp.status_code != 200:
            raise ExtractionError(f"PayU API returned HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError as e:
            raise ExtractionError(f"PayU API did not return valid JSON: {e}") from e

        # Confirmed live against PayU's own docs sample response: the key
        # is "Transaction_details" (capital T) and it's a JSON ARRAY, not
        # a dict keyed by txnid -- e.g.
        # {"status": 1, "msg": "...", "Transaction_details": [{...}, {...}]}
        # An empty array is a legitimate "no transactions this period"
        # response, not an error. Accepting the lowercase key too as a
        # defensive fallback -- APIs occasionally drift from their own
        # docs on casing, and this is cheap insurance against that
        # specific failure mode without masking any other schema change.
        details = None
        if isinstance(data, dict):
            details = data.get("Transaction_details", data.get("transaction_details"))

        if not isinstance(details, list):
            # PayU's own error shape: {"status": 0, "msg": "..."} with no
            # details key at all -- surfaced distinctly rather than a
            # generic schema-mismatch message.
            if isinstance(data, dict) and str(data.get("status")) == "0":
                raise ExtractionError(f"PayU API error: {data.get('msg') or data}")
            raise ExtractionError(
                f"PayU API response missing 'Transaction_details' array — schema may have changed. "
                f"Keys seen: {list(data.keys()) if isinstance(data, dict) else type(data)}"
            )

        return [_map_transaction(row) for row in details if isinstance(row, dict)]

    async def get_refunds_for_transaction(self, txnid: str) -> list[PayURefund]:
        """All refund/cancel actions PayU has on record against ONE
        transaction, identified by `txnid` -- the MERCHANT's own
        reference (PayUTransaction.txnid, == OrderPaymentRecord.order_code),
        NOT PayU's internal mihpayid (see GET_ALL_REFUNDS_COMMAND's
        module-level comment for how that was confirmed). Used to show a
        genuine refund id on /refund-check, verified straight from PayU
        rather than inferred from our own refund_check table's "an admin
        clicked Refund" intent flag. One call per transaction (PayU's
        API takes a single var1, not a batch) -- confirmed live
        2026-10-20."""
        if not self.settings.payu_configured:
            raise PayUNotConfiguredError(
                "PAYU_MERCHANT_KEY/PAYU_MERCHANT_SALT are not set in .env — "
                "add them from https://payu.in/business/transactions before checking refund status."
            )

        url = f"{self.settings.payu_base_url}/merchant/postservice.php?form=2"
        payload = {
            "key": self.settings.payu_merchant_key,
            "command": GET_ALL_REFUNDS_COMMAND,
            "var1": txnid,
            "hash": self._hash(GET_ALL_REFUNDS_COMMAND, txnid),
        }

        try:
            resp = await self._http.post(url, data=payload)
        except httpx.HTTPError as e:
            raise TargetUnavailableError(f"Could not reach PayU: {e}") from e

        if resp.status_code != 200:
            raise ExtractionError(f"PayU API returned HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError as e:
            raise ExtractionError(f"PayU API did not return valid JSON: {e}") from e

        if not isinstance(data, dict):
            raise ExtractionError(f"PayU API response was not a JSON object: {type(data)}")

        # Confirmed live 2026-10-20: "No Refunds Found for the
        # transaction." (status 1, no "Refund Details" key at all) is
        # the normal, expected response for a transaction with no
        # refund -- not an error. A genuine API error is status 0.
        details = data.get("Refund Details")
        if details is None:
            if str(data.get("status")) == "0":
                raise ExtractionError(f"PayU API error: {data.get('msg') or data}")
            return []

        # Keyed by PayuID -> list of refund rows; var1 names exactly one
        # id, so flatten every list present rather than assuming the key
        # echoes var1 verbatim.
        rows: list[dict] = []
        if isinstance(details, dict):
            for group in details.values():
                if isinstance(group, list):
                    rows.extend(r for r in group if isinstance(r, dict))
        return [_map_refund(row) for row in rows]

    async def initiate_refund(self, mihpayid: str, token: str, amount: Decimal) -> PayURefundInitiationResult:
        """Calls cancel_refund_transaction -- fires a REAL refund against
        whichever PayU account `self.settings` is configured for. This
        client has no built-in safety check of its own; which account
        actually gets called lives entirely in which Settings the caller
        constructs it with (see backend/api/refund_check.py's
        _handle_refund_trigger(), which resolves production credentials
        for a real order and test/sandbox credentials only for a dormant
        TEST_DEVICE_APP dummy order -- activated against production per
        explicit instruction, 2026-10-20, after being proven against
        TEST/sandbox credentials first).

        Hash formula confirmed live 2026-10-20 against the TEST MID (see
        CANCEL_REFUND_COMMAND's module-level comment for the exact
        before/after verification) -- sha512(key|command|var1|salt),
        var1 (mihpayid) ONLY, the same shape as every other command in
        this file. var2 (token) and var3 (amount) are sent as request
        params but NOT part of the hash.

        Does NOT raise on a clean PayU-side rejection (status 0) -- that
        means PayU received and processed the request (e.g. "already
        refunded", "token already used"), which the caller needs to
        store, not treat as a crash. Only transport/schema failures
        raise, same convention as the other methods here."""
        if not self.settings.payu_configured:
            raise PayUNotConfiguredError(
                "PAYU_MERCHANT_KEY/PAYU_MERCHANT_SALT are not set — this client must be constructed "
                "from test/sandbox settings before calling initiate_refund()."
            )

        var1, var2, var3 = mihpayid, token, f"{amount:.2f}"
        url = f"{self.settings.payu_base_url}/merchant/postservice.php?form=2"
        payload = {
            "key": self.settings.payu_merchant_key,
            "command": CANCEL_REFUND_COMMAND,
            "var1": var1,
            "var2": var2,
            "var3": var3,
            "hash": self._hash(CANCEL_REFUND_COMMAND, var1),
        }

        try:
            resp = await self._http.post(url, data=payload)
        except httpx.HTTPError as e:
            raise TargetUnavailableError(f"Could not reach PayU: {e}") from e

        if resp.status_code != 200:
            raise ExtractionError(f"PayU API returned HTTP {resp.status_code}")

        try:
            data = resp.json()
        except ValueError as e:
            raise ExtractionError(f"PayU API did not return valid JSON: {e}") from e

        if not isinstance(data, dict):
            raise ExtractionError(f"PayU API response was not a JSON object: {type(data)}")

        return PayURefundInitiationResult(
            success=str(data.get("status")) == "1",
            request_id=str(data.get("request_id")).strip() if data.get("request_id") else None,
            message=str(data.get("msg") or "").strip(),
            mihpayid=str(data.get("mihpayid") or mihpayid).strip(),
            raw=data,
        )

    async def close(self) -> None:
        await self._http.aclose()
