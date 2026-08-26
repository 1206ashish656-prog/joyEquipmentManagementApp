"""
NotificationService: delivery only. Composing WHO to notify and WHEN
(dedup, escalation, recovery) is AlertEngine's job (services/alert_engine.py)
— this module just knows how to send one message to a list of addresses.

Channel is pluggable (project requirement #18: "architecture should allow
future channels: Email, SMS, Teams, Slack, push"). Only EmailNotification
Channel is implemented; add new channels by implementing NotificationChannel
and registering them in NotificationService, without touching AlertEngine.

Per requirement #17: SMTP credentials come only from Settings (env),
and are never included in any log line here.
"""
from __future__ import annotations

import logging
import smtplib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage

from monitoring.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class Notification:
    to: list[str]
    subject: str
    body: str
    # Optional HTML alternative (e.g. services/fault_digest.py's tabular
    # critical-faults report) -- when set, EmailNotificationChannel sends
    # a proper multipart/alternative message (plain text body stays as
    # the fallback for clients that don't render HTML); when None,
    # behavior is unchanged from before this field existed.
    html_body: str | None = None


class NotificationChannel(ABC):
    @abstractmethod
    def send(self, notification: Notification) -> bool:
        """Returns True on success. Must never raise for a routine delivery
        failure — callers treat a False return as "not sent" and move on;
        an alert-delivery outage must not crash the monitoring worker."""


class ConsoleNotificationChannel(NotificationChannel):
    """Dev/fallback channel used when SMTP isn't configured (see
    Settings.smtp_configured) — logs what WOULD be sent instead of
    actually sending, so alerting logic is exercisable without real SMTP
    credentials (mirrors how the DB layer works against SQLite in tests)."""

    def send(self, notification: Notification) -> bool:
        logger.info(
            "[console-channel — no SMTP configured] To: %s | Subject: %s\n%s%s",
            ", ".join(notification.to), notification.subject, notification.body,
            "\n[+ an HTML alternative body, not shown here]" if notification.html_body else "",
        )
        return True


class EmailNotificationChannel(NotificationChannel):
    def __init__(self, settings: Settings):
        self._host = settings.smtp_host
        self._port = settings.smtp_port
        self._username = settings.smtp_username
        self._password = settings.smtp_password
        self._from_email = settings.smtp_from_email or settings.smtp_username
        self._use_tls = settings.smtp_use_tls

    def send(self, notification: Notification) -> bool:
        if not notification.to:
            return False

        msg = EmailMessage()
        msg["Subject"] = notification.subject
        msg["From"] = self._from_email
        msg["To"] = ", ".join(notification.to)
        msg.set_content(notification.body)
        if notification.html_body:
            # multipart/alternative: the plain-text set_content() above
            # stays as the fallback for a client that can't render HTML;
            # most clients show this HTML part instead.
            msg.add_alternative(notification.html_body, subtype="html")

        try:
            with smtplib.SMTP(self._host, self._port, timeout=15) as smtp:
                if self._use_tls:
                    smtp.starttls()
                if self._username:
                    smtp.login(self._username, self._password)
                smtp.send_message(msg)
            logger.info("Email sent to %s: %s", ", ".join(notification.to), notification.subject)
            return True
        except Exception:
            # Never log str(e) here without checking first — smtplib
            # exceptions can occasionally echo back server responses that
            # include the username; logging only the exception TYPE keeps
            # this safely credential-free (requirement #17).
            logger.exception("Failed to send email (see traceback above; credentials never logged)")
            return False


class NotificationService:
    def __init__(self, settings: Settings):
        self._channel: NotificationChannel = (
            EmailNotificationChannel(settings) if settings.smtp_configured else ConsoleNotificationChannel()
        )

    def send_email(self, to: list[str], subject: str, body: str, html_body: str | None = None) -> bool:
        if not to:
            logger.warning("send_email called with no recipients — subject=%r", subject)
            return False
        return self._channel.send(Notification(to=to, subject=subject, body=body, html_body=html_body))
