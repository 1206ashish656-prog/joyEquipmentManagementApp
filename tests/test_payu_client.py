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
