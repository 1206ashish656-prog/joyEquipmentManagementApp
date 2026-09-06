"""
Unit tests for services/notification_service.py. No real SMTP is ever
contacted — EmailNotificationChannel's use of smtplib.SMTP is mocked out.
"""
from __future__ import annotations

import socket
from dataclasses import replace
from unittest.mock import MagicMock, patch

import httpx

from monitoring.config import load_settings
from services.notification_service import (
    ConsoleNotificationChannel,
    EmailNotificationChannel,
    Notification,
    NotificationService,
    ResendEmailChannel,
)


def _base_settings():
    return load_settings()


def test_console_channel_used_when_smtp_not_configured(caplog):
    settings = replace(_base_settings(), smtp_host="")
    service = NotificationService(settings)
    assert isinstance(service._channel, ConsoleNotificationChannel)

    with caplog.at_level("INFO"):
        result = service.send_email(["a@example.com"], "Subject", "Body")
    assert result is True
    assert "Subject" in caplog.text


def test_email_channel_used_when_smtp_configured():
    settings = replace(
        _base_settings(),
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="bot@example.com",
        smtp_password="supersecret",
        smtp_from_email="bot@example.com",
        smtp_use_tls=True,
    )
    service = NotificationService(settings)
    assert isinstance(service._channel, EmailNotificationChannel)


def test_email_channel_sends_via_smtp_with_tls_and_login():
    settings = replace(
        _base_settings(),
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_username="bot@example.com",
        smtp_password="supersecret",
        smtp_from_email="bot@example.com",
        smtp_use_tls=True,
    )
    channel = EmailNotificationChannel(settings)

    mock_smtp_instance = MagicMock()
    mock_smtp_ctx = MagicMock()
    mock_smtp_ctx.__enter__.return_value = mock_smtp_instance
    mock_smtp_ctx.__exit__.return_value = False

    with patch("services.notification_service.smtplib.SMTP", return_value=mock_smtp_ctx) as mock_smtp_cls:
        from services.notification_service import Notification

        ok = channel.send(Notification(to=["ops@example.com"], subject="Test", body="Body text"))

    assert ok is True
    mock_smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=15)
    mock_smtp_instance.starttls.assert_called_once()
    mock_smtp_instance.login.assert_called_once_with("bot@example.com", "supersecret")
    mock_smtp_instance.send_message.assert_called_once()


def test_email_channel_returns_false_on_smtp_failure_without_raising():
    settings = replace(_base_settings(), smtp_host="smtp.example.com", smtp_from_email="bot@example.com")
    channel = EmailNotificationChannel(settings)

    with patch("services.notification_service.smtplib.SMTP", side_effect=OSError("connection refused")):
        from services.notification_service import Notification

        ok = channel.send(Notification(to=["ops@example.com"], subject="Test", body="Body"))

    assert ok is False


def test_email_channel_forces_ipv4_only_dns_resolution(monkeypatch):
    """Regression test for a live production incident (2026-09-02):
    Railway's containers have no IPv6 egress, but smtp.gmail.com
    resolves to an IPv6 address first, so smtplib's real connect() got
    OSError: [Errno 101] Network is unreachable and a Critical alert
    (equipment gone OFFLINE) silently never reached anyone. The fix
    forces IPv4-only resolution for just the connection attempt — this
    simulates smtplib's internal DNS call (which the mocked-out
    smtplib.SMTP below never actually performs) to verify the forced
    family, and that the patch is restored afterward rather than left
    in place process-wide."""
    seen_families = []

    def fake_getaddrinfo(host, port, family=0, *args, **kwargs):
        seen_families.append(family)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    settings = replace(_base_settings(), smtp_host="smtp.gmail.com", smtp_from_email="bot@example.com")
    channel = EmailNotificationChannel(settings)

    mock_smtp_instance = MagicMock()
    mock_smtp_ctx = MagicMock()
    mock_smtp_ctx.__enter__.return_value = mock_smtp_instance
    mock_smtp_ctx.__exit__.return_value = False

    def smtp_side_effect(host, port, timeout=15):
        # Simulate what the real smtplib.SMTP.connect() does internally —
        # call socket.getaddrinfo() — since the class itself is mocked out.
        socket.getaddrinfo(host, port)
        return mock_smtp_ctx

    with patch("services.notification_service.smtplib.SMTP", side_effect=smtp_side_effect):
        from services.notification_service import Notification

        ok = channel.send(Notification(to=["ops@example.com"], subject="Test", body="Body"))

    assert ok is True
    assert seen_families == [socket.AF_INET]  # forced to IPv4, never left as AF_UNSPEC (0)
    assert socket.getaddrinfo is fake_getaddrinfo  # restored to what it was before send(), not left patched


def test_email_channel_restores_dns_resolution_even_on_failure(monkeypatch):
    """The IPv4-only patch above must be restored via `finally` even when
    the send itself fails — otherwise one failed send would leave DNS
    resolution force-IPv4 for the rest of the process."""
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)  # a distinct bound reference to compare against
    before = socket.getaddrinfo

    settings = replace(_base_settings(), smtp_host="smtp.gmail.com", smtp_from_email="bot@example.com")
    channel = EmailNotificationChannel(settings)

    with patch("services.notification_service.smtplib.SMTP", side_effect=OSError("Network is unreachable")):
        from services.notification_service import Notification

        ok = channel.send(Notification(to=["ops@example.com"], subject="Test", body="Body"))

    assert ok is False
    assert socket.getaddrinfo is before


def test_password_never_appears_in_logs(caplog):
    """Requirement #17: passwords must never appear in logs."""
    settings = replace(
        _base_settings(),
        smtp_host="smtp.example.com",
        smtp_username="bot@example.com",
        smtp_password="supersecret-password-xyz",
        smtp_from_email="bot@example.com",
    )
    channel = EmailNotificationChannel(settings)

    with patch("services.notification_service.smtplib.SMTP", side_effect=OSError("boom")):
        from services.notification_service import Notification

        with caplog.at_level("INFO"):
            channel.send(Notification(to=["ops@example.com"], subject="Test", body="Body"))

    assert "supersecret-password-xyz" not in caplog.text


def test_send_email_with_no_recipients_returns_false():
    service = NotificationService(replace(_base_settings(), smtp_host=""))
    assert service.send_email([], "Subject", "Body") is False


def test_email_channel_sends_multipart_when_html_body_given():
    """services/fault_digest.py's tabular reports need a real HTML part,
    not just a plain-text body with literal <table> tags in it."""
    settings = replace(
        _base_settings(),
        smtp_host="smtp.example.com", smtp_port=587, smtp_username="bot@example.com",
        smtp_password="supersecret", smtp_from_email="bot@example.com", smtp_use_tls=True,
    )
    channel = EmailNotificationChannel(settings)

    mock_smtp_instance = MagicMock()
    mock_smtp_ctx = MagicMock()
    mock_smtp_ctx.__enter__.return_value = mock_smtp_instance
    mock_smtp_ctx.__exit__.return_value = False

    with patch("services.notification_service.smtplib.SMTP", return_value=mock_smtp_ctx):
        from services.notification_service import Notification

        ok = channel.send(Notification(
            to=["ops@example.com"], subject="Critical Faults Report",
            body="plain text fallback", html_body="<table><tr><td>NEXUS</td></tr></table>",
        ))

    assert ok is True
    sent_msg = mock_smtp_instance.send_message.call_args[0][0]
    assert sent_msg.is_multipart()
    html_part = next(part for part in sent_msg.walk() if part.get_content_type() == "text/html")
    assert "<table>" in html_part.get_content()


def test_email_channel_without_html_body_is_not_multipart():
    """No regression for the existing single-incident alerts, which
    never pass html_body."""
    settings = replace(
        _base_settings(),
        smtp_host="smtp.example.com", smtp_from_email="bot@example.com",
    )
    channel = EmailNotificationChannel(settings)

    mock_smtp_instance = MagicMock()
    mock_smtp_ctx = MagicMock()
    mock_smtp_ctx.__enter__.return_value = mock_smtp_instance
    mock_smtp_ctx.__exit__.return_value = False

    with patch("services.notification_service.smtplib.SMTP", return_value=mock_smtp_ctx):
        from services.notification_service import Notification

        channel.send(Notification(to=["ops@example.com"], subject="Test", body="Body text"))

    sent_msg = mock_smtp_instance.send_message.call_args[0][0]
    assert sent_msg.is_multipart() is False


def _resend_settings(**overrides):
    defaults = {"resend_api_key": "re_test_key", "resend_from_email": "alerts@example.com"}
    defaults.update(overrides)
    return replace(_base_settings(), **defaults)


def test_resend_channel_selected_over_smtp_when_both_configured():
    """Production incident (2026-09-06): confirmed live that Railway
    blocks all outbound SMTP ports entirely, so Resend (HTTPS) must win
    whenever both are configured — SMTP staying configured too (e.g. a
    leftover local .env copied into production) must never silently win
    back over the channel that actually works there."""
    settings = replace(_resend_settings(), smtp_host="smtp.example.com", smtp_from_email="bot@example.com")
    service = NotificationService(settings)
    assert isinstance(service._channel, ResendEmailChannel)


def test_smtp_channel_selected_when_resend_not_configured():
    """No regression: local dev (no RESEND_API_KEY) keeps using SMTP
    exactly as before."""
    settings = replace(_base_settings(), smtp_host="smtp.example.com", smtp_from_email="bot@example.com")
    service = NotificationService(settings)
    assert isinstance(service._channel, EmailNotificationChannel)


def test_resend_channel_sends_via_https_post():
    channel = ResendEmailChannel(_resend_settings())

    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None

    with patch("services.notification_service.httpx.post", return_value=mock_response) as mock_post:
        ok = channel.send(Notification(to=["ops@example.com"], subject="Test", body="Body text"))

    assert ok is True
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.resend.com/emails"
    assert kwargs["json"]["from"] == "alerts@example.com"
    assert kwargs["json"]["to"] == ["ops@example.com"]
    assert kwargs["json"]["subject"] == "Test"
    assert kwargs["json"]["text"] == "Body text"
    assert "html" not in kwargs["json"]
    assert kwargs["headers"]["Authorization"] == "Bearer re_test_key"


def test_resend_channel_includes_html_body_when_given():
    channel = ResendEmailChannel(_resend_settings())
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None

    with patch("services.notification_service.httpx.post", return_value=mock_response) as mock_post:
        channel.send(Notification(to=["ops@example.com"], subject="Test", body="Body text", html_body="<b>hi</b>"))

    assert mock_post.call_args.kwargs["json"]["html"] == "<b>hi</b>"


def test_resend_channel_returns_false_on_http_error_without_raising():
    channel = ResendEmailChannel(_resend_settings())

    with patch("services.notification_service.httpx.post", side_effect=httpx.ConnectTimeout("timed out")):
        ok = channel.send(Notification(to=["ops@example.com"], subject="Test", body="Body"))

    assert ok is False


def test_resend_channel_returns_false_on_4xx_response():
    channel = ResendEmailChannel(_resend_settings())
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "bad request", request=MagicMock(), response=MagicMock(status_code=422)
    )

    with patch("services.notification_service.httpx.post", return_value=mock_response):
        ok = channel.send(Notification(to=["ops@example.com"], subject="Test", body="Body"))

    assert ok is False


def test_resend_api_key_never_appears_in_logs(caplog):
    """Requirement #17: the API key must never appear in logs, same rule
    already enforced for SMTP passwords."""
    channel = ResendEmailChannel(_resend_settings(resend_api_key="re_super_secret_key_xyz"))

    with patch("services.notification_service.httpx.post", side_effect=httpx.ConnectTimeout("timed out")):
        with caplog.at_level("ERROR"):
            channel.send(Notification(to=["ops@example.com"], subject="Test", body="Body"))

    assert "re_super_secret_key_xyz" not in caplog.text
