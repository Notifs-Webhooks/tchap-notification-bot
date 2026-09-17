from __future__ import annotations

from datetime import datetime

import pytest

from notifier.models import (
    ConversationStatus,
    DeliveryStatus,
    DocumentChangeType,
    DocumentNotificationRequest,
)
from notifier.service import SubscriptionRequiredError, render_document_notification

RECIPIENT = "@alice:localhost"


def notification(key: str = "doc-42-v1") -> DocumentNotificationRequest:
    return DocumentNotificationRequest(
        recipient=RECIPIENT,
        idempotency_key=key,
        actor_name="John Smith",
        document_id="42",
        document_title="Budget 2027",
        document_url="https://docs.example.test/docs/42",
        change_type=DocumentChangeType.UPDATED,
    )


def test_renders_document_digest_summary() -> None:
    digest = notification().model_copy(
        update={
            "actor_name": "Alice and Bob",
            "change_count": 3,
            "period_start": datetime.fromisoformat("2026-09-17T10:00:00+00:00"),
            "period_end": datetime.fromisoformat("2026-09-17T11:00:00+00:00"),
        }
    )

    assert render_document_notification(digest) == (
        "📝 Document update summary\n\n"
        'Alice and Bob made 3 saved updates to "Budget 2027".\n'
        "Period: 2026-09-17T10:00+00:00 to 2026-09-17T11:00+00:00\n\n"
        "Open document: https://docs.example.test/docs/42"
    )


@pytest.mark.asyncio()
async def test_waits_for_consent_then_sends(service, repository, matrix) -> None:
    conversation = await service.subscribe(RECIPIENT)
    assert conversation.status is ConversationStatus.PENDING

    delivery = service.create_document_notification(notification())
    assert delivery.status is DeliveryStatus.AWAITING_RECIPIENT
    assert not matrix.sent

    matrix.rooms[conversation.room_id][RECIPIENT] = "join"
    await service.handle_membership(conversation.room_id, RECIPIENT, "join")
    assert service.get_subscription(RECIPIENT).status is ConversationStatus.ACTIVE

    assert await service.dispatch_once() == 1
    delivered = service.get_delivery(delivery.delivery_id)
    assert delivered.status is DeliveryStatus.SENT
    assert delivered.event_id == f"$event-{delivery.delivery_id}"
    assert 'John Smith updated "Budget 2027".' in matrix.sent[0][1]
    assert "https://docs.example.test/docs/42" in matrix.sent[0][1]


@pytest.mark.asyncio()
async def test_idempotency_key_never_creates_two_deliveries(service) -> None:
    conversation = await service.subscribe(RECIPIENT)
    await service.handle_membership(conversation.room_id, RECIPIENT, "join")

    first = service.create_document_notification(notification())
    second = service.create_document_notification(notification())

    assert first.delivery_id == second.delivery_id
    assert await service.dispatch_once() == 1
    assert await service.dispatch_once() == 0


@pytest.mark.asyncio()
async def test_declining_invitation_cancels_pending_delivery(service) -> None:
    conversation = await service.subscribe(RECIPIENT)
    delivery = service.create_document_notification(notification())

    await service.handle_membership(conversation.room_id, RECIPIENT, "leave")

    assert service.get_subscription(RECIPIENT).status is ConversationStatus.REVOKED
    assert (
        service.get_delivery(delivery.delivery_id).status
        is DeliveryStatus.RECIPIENT_DECLINED
    )
    with pytest.raises(SubscriptionRequiredError):
        service.create_document_notification(notification("doc-42-v2"))


@pytest.mark.asyncio()
async def test_explicit_resubscription_creates_a_new_room(service, matrix) -> None:
    first = await service.subscribe(RECIPIENT)
    await service.handle_membership(first.room_id, RECIPIENT, "leave")

    second = await service.subscribe(RECIPIENT)

    assert second.status is ConversationStatus.PENDING
    assert second.room_id != first.room_id
    assert matrix.created == 2


@pytest.mark.asyncio()
async def test_delivery_failure_is_recorded(service, matrix) -> None:
    conversation = await service.subscribe(RECIPIENT)
    await service.handle_membership(conversation.room_id, RECIPIENT, "join")
    delivery = service.create_document_notification(notification())
    service.max_delivery_attempts = 1
    matrix.send_error = RuntimeError("Matrix unavailable")

    assert await service.dispatch_once() == 0

    failed = service.get_delivery(delivery.delivery_id)
    assert failed.status is DeliveryStatus.FAILED
    assert failed.attempts == 1
    assert failed.error == "Matrix unavailable"


@pytest.mark.asyncio()
async def test_unsubscribe_leaves_room_and_revokes_subscription(
    service,
    matrix,
) -> None:
    conversation = await service.subscribe(RECIPIENT)

    revoked = await service.unsubscribe(RECIPIENT)

    assert revoked.status is ConversationStatus.REVOKED
    assert matrix.left == [conversation.room_id]


def test_notification_requires_prior_subscription(service) -> None:
    with pytest.raises(SubscriptionRequiredError):
        service.create_document_notification(notification())
