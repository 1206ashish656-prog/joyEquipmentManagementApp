"""
Unit tests for monitoring/lightweight_client.py. No real network call is
ever made — httpx.AsyncClient's transport is swapped for httpx.MockTransport,
so these verify the actual request/response handling logic (cookie
loading, redirect-to-login detection, pagination, error classification)
without hitting jwintell.com or needing a browser.
"""
from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from monitoring.config import load_settings
from monitoring.lightweight_client import LightweightTargetClient, _load_cookies
from monitoring.models import ExtractionError, TargetUnavailableError


def _settings(tmp_path, **overrides):
    base = load_settings()
    storage_state_path = tmp_path / "session.json"
    return replace(base, storage_state_path=storage_state_path, **overrides)


def _write_storage_state(path, cookies):
    path.write_text(json.dumps({"cookies": cookies, "origins": []}), encoding="utf-8")


# --- cookie loading ---

def test_load_cookies_from_missing_file_returns_empty(tmp_path):
    cookies = _load_cookies(tmp_path / "does_not_exist.json")
    assert len(cookies) == 0


def test_load_cookies_from_valid_storage_state(tmp_path):
    path = tmp_path / "session.json"
    _write_storage_state(path, [{"name": "PHPSESSID", "value": "abc123", "domain": "www.jwintell.com", "path": "/"}])
    cookies = _load_cookies(path)
    assert cookies.get("PHPSESSID", domain="www.jwintell.com") == "abc123"


def test_load_cookies_from_corrupt_file_returns_empty(tmp_path):
    path = tmp_path / "session.json"
    path.write_text("{ not valid json", encoding="utf-8")
    cookies = _load_cookies(path)
    assert len(cookies) == 0


def test_load_cookies_skips_malformed_entries(tmp_path):
    path = tmp_path / "session.json"
    _write_storage_state(path, [{"name": "onlyname"}])  # missing value/domain
    cookies = _load_cookies(path)  # must not raise
    assert len(cookies) == 0


# --- is_session_valid ---

@pytest.mark.asyncio
async def test_is_session_valid_true_when_not_redirected_to_login(tmp_path):
    settings = _settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>Console</html>")

    client = LightweightTargetClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await client.is_session_valid() is True


@pytest.mark.asyncio
async def test_is_session_valid_false_when_redirected_to_login(tmp_path):
    settings = _settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if "login" in str(request.url):
            return httpx.Response(200, text="login page")
        return httpx.Response(302, headers={"Location": "/NgsEmfuaOv.php/index/login"})

    client = LightweightTargetClient(settings)
    transport = httpx.MockTransport(handler)
    client._http = httpx.AsyncClient(transport=transport, follow_redirects=True)
    assert await client.is_session_valid() is False


@pytest.mark.asyncio
async def test_is_session_valid_raises_target_unavailable_on_connection_error(tmp_path):
    settings = _settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = LightweightTargetClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TargetUnavailableError):
        await client.is_session_valid()


# --- get_equipment_data ---

@pytest.mark.asyncio
async def test_get_equipment_data_maps_rows_to_canonical_fields(tmp_path):
    settings = _settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "total": 1,
            "rows": [{
                "id": 205, "sn": "153DF041EE7E279C", "name": "NEXUS",
                "device_type": {"name": "REFRESHA"}, "status_text": "Normal",
                "online_status_text": " Online", "fault_status_text": "Normal",
                "lack_status_text": "Normal", "ad_group": {"name": "Support V2"},
                "address": "", "shop_price": "120.00",
            }],
        })

    client = LightweightTargetClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rows = await client.get_equipment_data()

    assert len(rows) == 1
    assert rows[0]["equipment_id"] == "205"
    assert rows[0]["equipment_code"] == "153DF041EE7E279C"
    assert rows[0]["network_status"] == "Online"  # leading space stripped
    assert rows[0]["_raw"]["id"] == 205


@pytest.mark.asyncio
async def test_get_equipment_data_paginates_until_total_reached(tmp_path):
    settings = _settings(tmp_path)
    seen_offsets = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        limit = int(request.url.params["limit"])
        seen_offsets.append(offset)
        # Pretend there are 3 total records, served in pages of `limit`.
        remaining = max(0, 3 - offset)
        page_rows = [
            {"id": offset + i, "sn": f"CODE{offset+i}", "name": f"D{offset+i}",
             "status_text": "Normal", "online_status_text": "Online",
             "fault_status_text": "Normal", "lack_status_text": "Normal"}
            for i in range(min(limit, remaining))
        ]
        return httpx.Response(200, json={"total": 3, "rows": page_rows})

    client = LightweightTargetClient(settings)
    # Force small page size for this test by monkeypatching the constant
    # used inside get_equipment_data via the module-level import.
    import monitoring.lightweight_client as lc
    original_page_size = lc.DEVICE_LIST_API_PAGE_SIZE
    lc.DEVICE_LIST_API_PAGE_SIZE = 2
    try:
        client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        rows = await client.get_equipment_data()
    finally:
        lc.DEVICE_LIST_API_PAGE_SIZE = original_page_size

    assert len(rows) == 3
    assert seen_offsets == [0, 2]


@pytest.mark.asyncio
async def test_get_equipment_data_raises_extraction_error_on_bad_status(tmp_path):
    settings = _settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client = LightweightTargetClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ExtractionError):
        await client.get_equipment_data()


@pytest.mark.asyncio
async def test_get_equipment_data_raises_extraction_error_on_missing_rows_key(tmp_path):
    settings = _settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    client = LightweightTargetClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ExtractionError):
        await client.get_equipment_data()


@pytest.mark.asyncio
async def test_get_equipment_data_raises_target_unavailable_on_network_error(tmp_path):
    settings = _settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    client = LightweightTargetClient(settings)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(TargetUnavailableError):
        await client.get_equipment_data()


def test_reload_cookies_picks_up_new_file_contents(tmp_path):
    settings = _settings(tmp_path)
    client = LightweightTargetClient(settings)
    assert len(client._http.cookies) == 0

    _write_storage_state(settings.storage_state_path, [{"name": "s", "value": "v", "domain": "www.jwintell.com", "path": "/"}])
    client.reload_cookies()
    assert client._http.cookies.get("s", domain="www.jwintell.com") == "v"


def test_has_saved_session(tmp_path):
    settings = _settings(tmp_path)
    client = LightweightTargetClient(settings)
    assert client.has_saved_session() is False
    _write_storage_state(settings.storage_state_path, [])
    assert client.has_saved_session() is True
