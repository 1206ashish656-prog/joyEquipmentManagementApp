"""
Unit tests for services/alert_engine.py: recipient resolution (spec
section 19) and that the right message goes out for the right event.
Uses a FakeNotificationService instead of any real channel — these tests
are about WHO gets notified and WHEN, not about email delivery mechanics
(see test_notification_service.py for that).
"""
from __future__ import annotations

from dataclasses import replace

from db.models import AlertSubscription, User
from monitoring.config import load_settings
from services.alert_engine import AlertEngine
from services.health_engine import HealthEngine
from services.state_manager import StateManager
from tests.conftest import make_record


class FakeNotificationService:
    def __init__(self):
        self.sent: list[dict] = []

    def send_email(self, to, subject, body):
        self.sent.append({"to": sorted(to), "subject": subject, "body": body})
        return True


def _engine(fake_service):
    settings = replace(load_settings(), smtp_host="")
    return AlertEngine(settings, fake_service)


def _add_user(db_session, email, role="user", active=True) -> User:
    user = User(name=email.split("@")[0], email=email, role=role, active=active)
    db_session.add(user)
    db_session.flush()
    return user


def test_admin_always_notified_on_critical(db_session):
    _add_user(db_session, "admin@example.com", role="admin")
    fake = FakeNotificationService()
    alert_engine = _engine(fake)
    sm = StateManager(HealthEngine())

    result = sm.process_observation(db_session, make_record(fault_type="Motor Fault"))
    alert_engine.notify(db_session, result)

    assert len(fake.sent) == 1
    assert fake.sent[0]["to"] == ["admin@example.com"]
    assert "Motor Fault" in fake.sent[0]["body"]
    assert "205" in fake.sent[0]["subject"]  # default equipment_id from make_record


def test_admin_not_notified_for_warning_without_subscription(db_session):
    """Admins are guaranteed Critical alerts (requirement #19); a Warning
    (material shortage) needs an explicit subscription."""
    _add_user(db_session, "admin@example.com", role="admin")
    fake = FakeNotificationService()
    alert_engine = _engine(fake)
    sm = StateManager(HealthEngine())

    result = sm.process_observation(db_session, make_record(material_shortage_status="Low"))
    alert_engine.notify(db_session, result)

    assert fake.sent == []


def test_all_machines_subscription_receives_alert(db_session):
    user = _add_user(db_session, "ops@example.com")
    db_session.add(AlertSubscription(user_id=user.id, equipment_id=None, severity=None, notification_type="email", enabled=True))
    db_session.flush()

    fake = FakeNotificationService()
    alert_engine = _engine(fake)
    sm = StateManager(HealthEngine())

    result = sm.process_observation(db_session, make_record(fault_type="Motor Fault"))
    alert_engine.notify(db_session, result)

    assert fake.sent[0]["to"] == ["ops@example.com"]


def test_equipment_specific_subscription_only_fires_for_that_equipment(db_session):
    sm = StateManager(HealthEngine())
    fake = FakeNotificationService()
    alert_engine = _engine(fake)

    # Subscribe to equipment "205" specifically.
    user = _add_user(db_session, "ops-a@example.com")
    result_205 = sm.process_observation(db_session, make_record(equipment_id="205", fault_type="Motor Fault"))
    db_session.add(
        AlertSubscription(
            user_id=user.id, equipment_id=result_205.equipment.id, severity=None,
            notification_type="email", enabled=True,
        )
    )
    db_session.flush()

    # A different, unrelated equipment faults — should NOT notify ops-a.
    fake.sent.clear()
    result_206 = sm.process_observation(
        db_session, make_record(equipment_id="206", equipment_code="OTHERCODE", name="OTHER", fault_type="Motor Fault")
    )
    alert_engine.notify(db_session, result_206)
    assert fake.sent == []


def test_disabled_subscription_does_not_fire(db_session):
    user = _add_user(db_session, "ops@example.com")
    db_session.add(AlertSubscription(user_id=user.id, equipment_id=None, severity=None, notification_type="email", enabled=False))
    db_session.flush()

    fake = FakeNotificationService()
    alert_engine = _engine(fake)
    sm = StateManager(HealthEngine())

    result = sm.process_observation(db_session, make_record(fault_type="Motor Fault"))
    alert_engine.notify(db_session, result)

    assert fake.sent == []


def test_severity_filtered_subscription(db_session):
    user = _add_user(db_session, "ops@example.com")
    db_session.add(AlertSubscription(user_id=user.id, equipment_id=None, severity="Warning", notification_type="email", enabled=True))
    db_session.flush()

    fake = FakeNotificationService()
    alert_engine = _engine(fake)
    sm = StateManager(HealthEngine())

    # Critical fault should NOT reach a Warning-only subscriber.
    result = sm.process_observation(db_session, make_record(fault_type="Motor Fault"))
    alert_engine.notify(db_session, result)
    assert fake.sent == []

    # A Warning-severity event (material shortage) SHOULD reach them.
    fake.sent.clear()
    result2 = sm.process_observation(db_session, make_record(fault_type="Normal", material_shortage_status="Low"))
    alert_engine.notify(db_session, result2)
    assert fake.sent[0]["to"] == ["ops@example.com"]


def test_recipients_deduplicated_across_matching_subscriptions(db_session):
    user = _add_user(db_session, "ops@example.com", role="admin")
    db_session.add(AlertSubscription(user_id=user.id, equipment_id=None, severity=None, notification_type="email", enabled=True))
    db_session.flush()

    fake = FakeNotificationService()
    alert_engine = _engine(fake)
    sm = StateManager(HealthEngine())

    # This user qualifies BOTH as an admin (Critical) and via their
    # all-machines subscription — must only get one email.
    result = sm.process_observation(db_session, make_record(fault_type="Motor Fault"))
    alert_engine.notify(db_session, result)

    assert len(fake.sent) == 1
    assert fake.sent[0]["to"] == ["ops@example.com"]


def test_no_duplicate_alert_for_ongoing_fault(db_session):
    _add_user(db_session, "admin@example.com", role="admin")
    fake = FakeNotificationService()
    alert_engine = _engine(fake)
    sm = StateManager(HealthEngine())

    for _ in range(4):
        result = sm.process_observation(db_session, make_record(fault_type="Motor Fault"))
        alert_engine.notify(db_session, result)

    assert len(fake.sent) == 1, "spec requirement: one alert, not one per poll"


def test_recovery_notification_sent_when_enabled(db_session):
    _add_user(db_session, "admin@example.com", role="admin")
    fake = FakeNotificationService()
    alert_engine = _engine(fake)
    sm = StateManager(HealthEngine())

    r1 = sm.process_observation(db_session, make_record(fault_type="Motor Fault"))
    alert_engine.notify(db_session, r1)
    fake.sent.clear()

    r2 = sm.process_observation(db_session, make_record())
    alert_engine.notify(db_session, r2)

    assert len(fake.sent) == 1
    assert "Recovered" in fake.sent[0]["subject"]


def test_recovery_notification_suppressed_when_disabled(db_session):
    _add_user(db_session, "admin@example.com", role="admin")
    fake = FakeNotificationService()
    settings = replace(load_settings(), smtp_host="", send_recovery_notifications=False)
    alert_engine = AlertEngine(settings, fake)
    sm = StateManager(HealthEngine())

    r1 = sm.process_observation(db_session, make_record(fault_type="Motor Fault"))
    alert_engine.notify(db_session, r1)
    fake.sent.clear()

    r2 = sm.process_observation(db_session, make_record())
    alert_engine.notify(db_session, r2)

    assert fake.sent == []


def test_incident_marked_notification_sent_on_open(db_session):
    _add_user(db_session, "admin@example.com", role="admin")
    fake = FakeNotificationService()
    alert_engine = _engine(fake)
    sm = StateManager(HealthEngine())

    result = sm.process_observation(db_session, make_record(fault_type="Motor Fault"))
    assert result.incident_opened.notification_sent is False
    alert_engine.notify(db_session, result)
    assert result.incident_opened.notification_sent is True


def test_no_recipients_does_not_crash(db_session):
    """No admins, no subscriptions — must degrade gracefully, not raise."""
    fake = FakeNotificationService()
    alert_engine = _engine(fake)
    sm = StateManager(HealthEngine())

    result = sm.process_observation(db_session, make_record(fault_type="Motor Fault"))
    alert_engine.notify(db_session, result)  # should not raise
    assert fake.sent == []
