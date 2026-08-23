"""
Unit tests for services/notification_service.py. No real SMTP is ever
contacted — EmailNotificationChannel's use of smtplib.SMTP is mocked out.
"""
from __future__ import annotations

from dataclasses import replace
from unittest.mock import MagicMock, patch

from monitoring.config import load_settings
from services.notification_service import (
    ConsoleNotificationChannel,
    EmailNotificationChannel,
    NotificationService,
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
