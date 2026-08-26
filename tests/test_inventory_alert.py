"""Unit tests for services/inventory_alert.py: fires exactly once on a
below-threshold CROSSING, never re-fires while already below — mirrors
tests/test_alert_engine.py's FakeNotificationService pattern and
test_no_duplicate_alert_for_ongoing_fault's "one alert, not one per
poll" requirement."""
from __future__ import annotations

from db.models import AlertRecipient, InventoryItem, User
from services.inventory_alert import check_and_notify
from services.inventory_rules import InventoryRules


class FakeNotificationService:
    def __init__(self):
        self.sent: list[dict] = []

    def send_email(self, to, subject, body):
        self.sent.append({"to": sorted(to), "subject": subject, "body": body})
        return True


class FakeRules:
    def __init__(self, threshold: int):
        self._threshold = threshold

    def threshold_for(self, item_key: str) -> int:
        return self._threshold


def _item(db_session, key="dustbin_bags", name="Dustbin Bags", unit="packs", current_stock=10) -> InventoryItem:
    item = InventoryItem(key=key, name=name, unit=unit, pieces_per_carton=None, current_stock=current_stock)
    db_session.add(item)
    db_session.flush()
    return item


def _add_admin(db_session, email="admin@example.com") -> User:
    user = User(name="Admin", email=email, role="admin", active=True, password_hash="x")
    db_session.add(user)
    db_session.flush()
    return user


def test_crossing_below_threshold_fires_one_email(db_session):
    _add_admin(db_session)
    item = _item(db_session, current_stock=10)
    fake = FakeNotificationService()

    sent = check_and_notify(db_session, fake, FakeRules(threshold=5), item, old_stock=10, new_stock=3)

    assert sent is True
    assert len(fake.sent) == 1
    assert fake.sent[0]["to"] == ["admin@example.com"]
    assert "Dustbin Bags" in fake.sent[0]["subject"]
    assert "3 packs" in fake.sent[0]["body"]


def test_still_below_threshold_does_not_refire(db_session):
    """The core requirement: quiet unless something changed."""
    _add_admin(db_session)
    item = _item(db_session, current_stock=3)
    fake = FakeNotificationService()
    rules = FakeRules(threshold=5)

    sent1 = check_and_notify(db_session, fake, rules, item, old_stock=10, new_stock=3)
    sent2 = check_and_notify(db_session, fake, rules, item, old_stock=3, new_stock=2)  # already below, logs more usage

    assert sent1 is True
    assert sent2 is False
    assert len(fake.sent) == 1


def test_still_above_threshold_fires_nothing(db_session):
    _add_admin(db_session)
    item = _item(db_session, current_stock=20)
    fake = FakeNotificationService()

    sent = check_and_notify(db_session, fake, FakeRules(threshold=5), item, old_stock=25, new_stock=20)

    assert sent is False
    assert fake.sent == []


def test_restock_above_threshold_fires_nothing(db_session):
    _add_admin(db_session)
    item = _item(db_session, current_stock=50)
    fake = FakeNotificationService()

    sent = check_and_notify(db_session, fake, FakeRules(threshold=5), item, old_stock=2, new_stock=50)

    assert sent is False
    assert fake.sent == []


def test_no_recipients_returns_false_and_does_not_raise(db_session):
    item = _item(db_session, current_stock=3)
    fake = FakeNotificationService()

    sent = check_and_notify(db_session, fake, FakeRules(threshold=5), item, old_stock=10, new_stock=3)

    assert sent is False
    assert fake.sent == []


def test_inactive_admin_excluded(db_session):
    _add_admin(db_session)
    inactive = User(name="Gone", email="gone@example.com", role="admin", active=False, password_hash="x")
    db_session.add(inactive)
    item = _item(db_session, current_stock=3)
    fake = FakeNotificationService()

    check_and_notify(db_session, fake, FakeRules(threshold=5), item, old_stock=10, new_stock=3)

    assert fake.sent[0]["to"] == ["admin@example.com"]


def test_active_alert_recipient_also_notified_deduplicated(db_session):
    _add_admin(db_session, email="admin@example.com")
    db_session.add(AlertRecipient(email="ops-external@example.com", active=True))
    inactive_recipient = AlertRecipient(email="left@example.com", active=False)
    db_session.add(inactive_recipient)
    item = _item(db_session, current_stock=3)
    fake = FakeNotificationService()

    check_and_notify(db_session, fake, FakeRules(threshold=5), item, old_stock=10, new_stock=3)

    assert fake.sent[0]["to"] == ["admin@example.com", "ops-external@example.com"]


def test_real_inventory_rules_thresholds_used_by_default(db_session):
    """Sanity check the real InventoryRules integrates correctly (not
    just FakeRules above) -- uses the actual config/inventory_rules.yaml
    dustbin_bags threshold (5)."""
    _add_admin(db_session)
    item = _item(db_session, key="dustbin_bags", current_stock=10)
    fake = FakeNotificationService()
    rules = InventoryRules()

    sent = check_and_notify(db_session, fake, rules, item, old_stock=10, new_stock=4)

    assert sent is True
