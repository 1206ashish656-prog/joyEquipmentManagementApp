"""
Unit tests for services/payu_client.py. No real network call is ever
made -- httpx.AsyncClient's transport is swapped for httpx.MockTransport
(same convention as tests/test_lightweight_client.py) -- so these verify
request construction (URL, hash, params) and response parsing against
PayU's own documented shapes without hitting payu.in.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from monitoring.config import load_settings
from monitoring.models import ExtractionError, TargetUnavailableError
from services.payu_client import PayUClient, PayUNotConfiguredError


def _settings(**overrides):
    base = load_settings()
    defaults = {"payu_merchant_key": "testkey", "payu_merchant_salt": "testsalt", "payu_env": "test"}
    defaults.update(overrides)
    return replace(base, **defaults)


# --- configuration ---

@pytest.mark.asyncio
async def test_not_configured_raises_before_any_request():
    settings = _settings(payu_merchant_key="", payu_merchant_salt="")
    client = PayUClient(settings)
    with pytest.raises(PayUNotConfiguredError):
        await client.get_transaction_details("2026-08-01", "2026-08-31")
    await client.close()


# --- request construction ---

@pytest.mark.asyncio
async def test_uses_test_url_when_env_is_test():
    settings = _settings(payu_env="test")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"status": 1, "Transaction_details": []})

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await client.get_transaction_details("2026-08-01", "2026-08-31")

    assert seen["url"].startswith("https://test.payu.in/merchant/postservice.php")


@pytest.mark.asyncio
async def test_uses_production_url_when_env_is_production():
    settings = _settings(payu_env="production")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"status": 1, "Transaction_details": []})

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await client.get_transaction_details("2026-08-01", "2026-08-31")

    assert seen["url"].startswith("https://info.payu.in/merchant/postservice.php")


@pytest.mark.asyncio
async def test_hash_matches_documented_formula():
    """PayU's own docs: sha512(key|command|var1|salt) -- hashed over the
    START date only, var2 sent unsigned."""
    settings = _settings(payu_merchant_key="mykey", payu_merchant_salt="mysalt")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        seen["body"] = dict(p.split("=") for p in body.split("&"))
        return httpx.Response(200, json={"status": 1, "Transaction_details": []})

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await client.get_transaction_details("2026-08-01", "2026-08-31")

    expected = hashlib.sha512("mykey|get_Transaction_Details|2026-08-01|mysalt".encode()).hexdigest()
    assert seen["body"]["hash"] == expected
    assert seen["body"]["key"] == "mykey"
    assert seen["body"]["var1"] == "2026-08-01"
    assert seen["body"]["var2"] == "2026-08-31"
    assert seen["body"]["command"] == "get_Transaction_Details"


# --- response parsing ---

@pytest.mark.asyncio
async def test_parses_transaction_details_array():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": 1, "msg": "Transaction Fetched Successfully",
            "Transaction_details": [
                {"txnid": "30388839606", "mihpayid": "4034...", "amount": "120.00", "status": "captured",
                 "mode": "UPI", "addedon": "2026-08-29 23:56", "bank_ref_no": "REF123"},
                {"txnid": "30388779356", "mihpayid": "4035...", "amount": "120.00", "status": "failed",
                 "mode": "UPI", "addedon": "2026-08-29 23:52", "bank_ref_no": ""},
            ],
        })

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    txns = await client.get_transaction_details("2026-08-01", "2026-08-31")

    assert len(txns) == 2
    assert txns[0].txnid == "30388839606"
    assert txns[0].amount == 120
    assert txns[0].status == "captured"
    assert txns[1].status == "failed"


@pytest.mark.asyncio
async def test_amount_falls_back_to_transaction_fee_when_zero():
    """Confirmed live 2026-10-20 against real production orders (324721,
    323879): some DBQR/QR-callback-settled rows report "amount": "0.00"
    while "transaction_fee" holds the real collected amount -- without
    this fallback, these would show as a false amount mismatch anywhere
    PayUTransaction.amount is compared against the machine's own
    order_money (services/reconciliation.py)."""
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": 1, "Transaction_details": [
                {"txnid": "1", "id": "100", "amount": "0.00", "transaction_fee": "135.00", "status": "captured"},
            ],
        })

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    txns = await client.get_transaction_details("2026-08-01", "2026-08-31")

    assert txns[0].amount == Decimal("135.00")


@pytest.mark.asyncio
async def test_amount_not_overridden_when_genuinely_nonzero():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": 1, "Transaction_details": [
                {"txnid": "1", "id": "100", "amount": "50.00", "transaction_fee": "135.00", "status": "captured"},
            ],
        })

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    txns = await client.get_transaction_details("2026-08-01", "2026-08-31")

    assert txns[0].amount == Decimal("50.00")  # amount wins when it's genuinely non-zero


@pytest.mark.asyncio
async def test_mihpayid_falls_back_to_id_field():
    """Confirmed live against a real production MID (2026-10-20): this
    API's own rows carry PayU's internal transaction id under "id", not
    "mihpayid"/"mihpayupid" -- those are webhook/redirect field names
    this list API doesn't use at all."""
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": 1,
            "Transaction_details": [
                {"txnid": "2026102010054555", "id": "31393916735", "amount": "135.00", "status": "dropped"},
            ],
        })

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    txns = await client.get_transaction_details("2026-08-01", "2026-08-31")

    assert txns[0].mihpayid == "31393916735"


@pytest.mark.asyncio
async def test_mihpayid_field_still_preferred_over_id_when_present():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": 1,
            "Transaction_details": [
                {"txnid": "1", "mihpayid": "explicit-mihpayid", "id": "fallback-id", "amount": "50.00", "status": "captured"},
            ],
        })

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    txns = await client.get_transaction_details("2026-08-01", "2026-08-31")

    assert txns[0].mihpayid == "explicit-mihpayid"


@pytest.mark.asyncio
async def test_empty_transaction_details_is_not_an_error():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 1, "Transaction_details": []})

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    txns = await client.get_transaction_details("2026-08-01", "2026-08-31")

    assert txns == []


@pytest.mark.asyncio
async def test_accepts_lowercase_key_as_fallback():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 1, "transaction_details": [{"txnid": "1", "amount": "50.00", "status": "success"}]})

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    txns = await client.get_transaction_details("2026-08-01", "2026-08-31")

    assert len(txns) == 1
    assert txns[0].txnid == "1"


@pytest.mark.asyncio
async def test_payu_error_shape_raises_extraction_error_with_message():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 0, "msg": "Invalid hash"})

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ExtractionError, match="Invalid hash"):
        await client.get_transaction_details("2026-08-01", "2026-08-31")


@pytest.mark.asyncio
async def test_unexpected_shape_raises_extraction_error():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ExtractionError):
        await client.get_transaction_details("2026-08-01", "2026-08-31")


@pytest.mark.asyncio
async def test_bad_http_status_raises_extraction_error():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ExtractionError):
        await client.get_transaction_details("2026-08-01", "2026-08-31")


@pytest.mark.asyncio
async def test_network_error_raises_target_unavailable():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TargetUnavailableError):
        await client.get_transaction_details("2026-08-01", "2026-08-31")


@pytest.mark.asyncio
async def test_non_json_response_raises_extraction_error():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ExtractionError):
        await client.get_transaction_details("2026-08-01", "2026-08-31")


# --- get_refunds_for_transaction (getAllRefundsFromTxnIds) ---

@pytest.mark.asyncio
async def test_refunds_not_configured_raises_before_any_request():
    settings = _settings(payu_merchant_key="", payu_merchant_salt="")
    client = PayUClient(settings)
    with pytest.raises(PayUNotConfiguredError):
        await client.get_refunds_for_transaction("2026102010050545")
    await client.close()


@pytest.mark.asyncio
async def test_refunds_hash_matches_documented_formula():
    """Confirmed live 2026-10-20 against a real production MID: hash =
    sha512(key|command|var1|salt), var1 = the merchant's own txnid --
    NOT PayU's internal mihpayid (a real refunded transaction came back
    "No Refunds Found" when queried by mihpayid; querying by its txnid
    found the refund correctly)."""
    settings = _settings(payu_merchant_key="mykey", payu_merchant_salt="mysalt")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        seen["body"] = dict(p.split("=") for p in body.split("&"))
        return httpx.Response(200, json={"status": 1, "msg": "No Refunds Found for the transaction."})

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await client.get_refunds_for_transaction("2026102010050545")

    expected = hashlib.sha512("mykey|getAllRefundsFromTxnIds|2026102010050545|mysalt".encode()).hexdigest()
    assert seen["body"]["hash"] == expected
    assert seen["body"]["command"] == "getAllRefundsFromTxnIds"
    assert seen["body"]["var1"] == "2026102010050545"


@pytest.mark.asyncio
async def test_no_refunds_found_is_not_an_error():
    """Confirmed live 2026-10-20: this is PayU's normal response shape
    for a transaction with no refund -- not an error, and not even a
    "Refund Details" key at all."""
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 1, "msg": "No Refunds Found for the transaction."})

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    refunds = await client.get_refunds_for_transaction("2026102010050545")

    assert refunds == []


@pytest.mark.asyncio
async def test_parses_refund_details_keyed_by_payu_id():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": 1, "msg": "Refunds fetched successfully.",
            "Refund Details": {
                "31386182688": [
                    {
                        "PayuID": "31386182688", "RequestID": "23277266487",
                        "RefundToken": "31386182688_506445f139", "Amount": "135.00",
                        "Status": "success", "RefundCreationDate": "2026-10-19 22:51:47",
                        "bank_ref_no": "100203042039",
                    },
                ],
            },
        })

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    refunds = await client.get_refunds_for_transaction("2026102010050545")

    assert len(refunds) == 1
    # refund_token is what PayU's own dashboard calls "Refund ID" --
    # confirmed by matching a real refunded transaction's dashboard
    # screenshot against this exact API response for it (2026-10-20).
    assert refunds[0].refund_token == "31386182688_506445f139"
    assert refunds[0].request_id == "23277266487"  # a different PayU id -- NOT what the dashboard shows
    assert refunds[0].status == "success"
    assert refunds[0].amount == 135
    assert refunds[0].bank_ref_no == "100203042039"


@pytest.mark.asyncio
async def test_refunds_error_shape_raises_extraction_error():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": 0, "msg": "Invalid hash"})

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ExtractionError, match="Invalid hash"):
        await client.get_refunds_for_transaction("31386182688")


@pytest.mark.asyncio
async def test_refunds_network_error_raises_target_unavailable():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TargetUnavailableError):
        await client.get_refunds_for_transaction("31386182688")


# --- initiate_refund (cancel_refund_transaction) ---

@pytest.mark.asyncio
async def test_initiate_refund_not_configured_raises_before_any_request():
    settings = _settings(payu_merchant_key="", payu_merchant_salt="")
    client = PayUClient(settings)
    with pytest.raises(PayUNotConfiguredError):
        await client.initiate_refund(mihpayid="20000078392", token="RF1", amount=Decimal("1.00"))
    await client.close()


@pytest.mark.asyncio
async def test_initiate_refund_hash_covers_var1_only():
    """Confirmed live 2026-10-20 against the real TEST MID: hash =
    sha512(key|command|var1|salt) -- var1 (mihpayid) ONLY. Candidates
    that included var2/var3 in the hash all came back "Invalid Hash."
    first -- see CANCEL_REFUND_COMMAND's module comment for the full
    verification trail."""
    settings = _settings(payu_merchant_key="mykey", payu_merchant_salt="mysalt")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        seen["body"] = dict(p.split("=") for p in body.split("&"))
        return httpx.Response(200, json={"status": 1, "msg": "Refund Request Queued", "request_id": "R1", "mihpayid": "20000078392"})

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await client.initiate_refund(mihpayid="20000078392", token="RF1", amount=Decimal("1.00"))

    expected = hashlib.sha512("mykey|cancel_refund_transaction|20000078392|mysalt".encode()).hexdigest()
    assert seen["body"]["hash"] == expected
    assert seen["body"]["command"] == "cancel_refund_transaction"
    assert seen["body"]["var1"] == "20000078392"
    assert seen["body"]["var2"] == "RF1"
    assert seen["body"]["var3"] == "1.00"


@pytest.mark.asyncio
async def test_initiate_refund_formats_amount_with_two_decimals():
    settings = _settings()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode()
        seen["body"] = dict(p.split("=") for p in body.split("&"))
        return httpx.Response(200, json={"status": 1, "msg": "Refund Request Queued"})

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await client.initiate_refund(mihpayid="1", token="RF1", amount=Decimal("135"))

    assert seen["body"]["var3"] == "135.00"


@pytest.mark.asyncio
async def test_initiate_refund_success_parsed():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": 1, "msg": "Refund Request Queued",
            "request_id": "900000000000423571", "mihpayid": 20000078392, "error_code": 102,
        })

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await client.initiate_refund(mihpayid="20000078392", token="RF1", amount=Decimal("1.00"))

    assert result.success is True
    assert result.request_id == "900000000000423571"
    assert result.mihpayid == "20000078392"  # coerced to str even though PayU sent it as an int
    assert result.message == "Refund Request Queued"


@pytest.mark.asyncio
async def test_initiate_refund_rejection_does_not_raise():
    """A clean PayU-side rejection is a definitive response, not a
    transport failure -- the caller (backend/api/refund_check.py) needs
    to store it and lock idempotency, not catch an exception."""
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "status": 0, "msg": "token already used or request pending", "mihpayid": "403993715521937565",
        })

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await client.initiate_refund(mihpayid="403993715521937565", token="RF1", amount=Decimal("1.00"))

    assert result.success is False
    assert result.message == "token already used or request pending"
    assert result.request_id is None


@pytest.mark.asyncio
async def test_initiate_refund_network_error_raises_target_unavailable():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TargetUnavailableError):
        await client.initiate_refund(mihpayid="1", token="RF1", amount=Decimal("1.00"))


@pytest.mark.asyncio
async def test_initiate_refund_non_json_response_raises_extraction_error():
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    client = PayUClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ExtractionError):
        await client.initiate_refund(mihpayid="1", token="RF1", amount=Decimal("1.00"))
