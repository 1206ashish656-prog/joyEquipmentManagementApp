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


def _to_decimal(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal("0")


def _map_transaction(row: dict) -> PayUTransaction:
    return PayUTransaction(
        txnid=str(row.get("txnid") or "").strip(),
        mihpayid=str(row.get("mihpayid") or row.get("mihpayupid") or "").strip(),
        amount=_to_decimal(row.get("amount")),
        status=str(row.get("status") or "").strip().lower(),
        mode=str(row.get("mode") or "").strip(),
        addedon=str(row.get("addedon") or "").strip(),
        bank_ref_no=str(row.get("bank_ref_no") or "").strip(),
        raw=row,
    )


class PayUClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._http = httpx.AsyncClient(timeout=30.0)

    def _hash(self, command: str, var1: str) -> str:
        raw = f"{self.settings.payu_merchant_key}|{command}|{var1}|{self.settings.payu_merchant_salt}"
        return hashlib.sha512(raw.encode("utf-8")).hexdigest()

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

    async def close(self) -> None:
        await self._http.aclose()
