"""
InventoryAlertService: fires exactly once when an item's current_stock
CROSSES from >= threshold to < threshold — never re-fires while it stays
below. Mirrors services/state_manager.py's _apply_transition()
"is_fault and not was_fault" pattern.

Does NOT decide whether to show a "low stock" badge -- the dashboard
(backend/api/inventory.py) always recomputes is_low live from
current_stock vs InventoryRules at render time, the same way
EquipmentCurrentState.health_state is read live rather than cached from a
FaultIncident. This module is only responsible for the one-time email.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AlertRecipient, InventoryItem, User
from services.inventory_rules import InventoryRules
from services.notification_service import NotificationService

logger = logging.getLogger(__name__)


def _low_stock_recipients(session: Session) -> list[str]:
    """Same audience shape as services/alert_engine.py's
    AlertEngine.get_recipients() for Critical severity (active admins +
    active AlertRecipient rows), inlined here rather than reused directly
    -- that method is signature-coupled to equipment_id/severity/
    AlertSubscription for equipment alerts specifically, and inventory
    items have no equipment_id to key off. One duplication site -- not
    worth a shared abstraction yet."""
    emails: set[str] = set()

    admins = session.execute(select(User).where(User.role == "admin", User.active.is_(True))).scalars().all()
    emails.update(a.email for a in admins)

    recipients = session.execute(select(AlertRecipient).where(AlertRecipient.active.is_(True))).scalars().all()
    emails.update(r.email for r in recipients)

    return sorted(emails)


def check_and_notify(
    session: Session,
    notification_service: NotificationService,
    rules: InventoryRules,
    item: InventoryItem,
    old_stock: int,
    new_stock: int,
) -> bool:
    """Returns True only if an email was actually sent. Call with
    old_stock/new_stock captured immediately before/after the mutation,
    in the same request that made it."""
    threshold = rules.threshold_for(item.key)
    old_below = old_stock < threshold
    new_below = new_stock < threshold

    if not (new_below and not old_below):
        # Covers: still below (no re-fire), still above, or went back
        # above (a restock/correction) -- none of these are a crossing.
        return False

    recipients = _low_stock_recipients(session)
    if not recipients:
        logger.warning("Low-stock crossing for %s but no recipients found — nothing sent", item.key)
        return False

    subject = f"Low Stock Alert — {item.name}"
    body = (
        f"Item:\n{item.name}\n\n"
        f"Current Stock:\n{new_stock} {item.unit}\n\n"
        f"Threshold:\n{threshold} {item.unit}\n\n"
        f"Action needed:\nRestock {item.name} soon."
    )
    sent = notification_service.send_email(recipients, subject, body)
    logger.info("Low-stock alert for %s: sent=%s recipients=%s", item.key, sent, recipients)
    return sent
