"""Subscription and notification-delivery business rules."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from notifier.models import (
    ConversationStatus,
    DeliveryStatus,
    DocumentChangeType,
    DocumentNotificationRequest,
)
from notifier.repository import (
    ConversationRecord,
    DeliveryRecord,
    NotificationRepository,
)

logger = logging.getLogger(__name__)


class MatrixGateway(Protocol):
    """Matrix operations required by Notifier."""

    @property
    def connected(self) -> bool: ...

    async def create_direct_room(self, recipient: str) -> str: ...

    async def send_notification(
        self,
        room_id: str,
        message: str,
        transaction_id: str,
    ) -> str: ...

    async def leave_room(self, room_id: str) -> None: ...

    def membership(self, room_id: str, recipient: str) -> str | None: ...


class SubscriptionRequiredError(RuntimeError):
    """The recipient has not enabled, or has revoked, the subscription."""


class ConversationNotFoundError(LookupError):
    """No subscription exists for this recipient."""


class DeliveryNotFoundError(LookupError):
    """No delivery matches this identifier."""


CHANGE_LABELS: dict[DocumentChangeType, tuple[str, str]] = {
    DocumentChangeType.CREATED: ("Document created", "created"),
    DocumentChangeType.UPDATED: ("Document updated", "updated"),
    DocumentChangeType.RENAMED: ("Document renamed", "renamed"),
    DocumentChangeType.COMMENTED: ("New comment", "commented on"),
    DocumentChangeType.SHARED: ("Document shared", "shared"),
    DocumentChangeType.DELETED: ("Document deleted", "deleted"),
    DocumentChangeType.RESTORED: ("Document restored", "restored"),
}


def render_document_notification(request: DocumentNotificationRequest) -> str:
    """Build the plain-text message displayed in Tchap."""

    title, verb = CHANGE_LABELS[request.change_type]
    lines = [
        f"📝 {title}",
        "",
        f'{request.actor_name} {verb} "{request.document_title}".',
    ]
    if request.document_url:
        lines.extend(("", f"Open document: {request.document_url}"))
    return "\n".join(lines)


class NotifierService:
    """Coordinate HTTP, SQLite, and the encrypted Matrix client."""

    def __init__(
        self,
        repository: NotificationRepository,
        matrix: MatrixGateway,
        *,
        dispatch_interval_seconds: float = 1.0,
        max_delivery_attempts: int = 5,
    ) -> None:
        self.repository = repository
        self.matrix = matrix
        self.dispatch_interval_seconds = dispatch_interval_seconds
        self.max_delivery_attempts = max_delivery_attempts
        self._subscription_lock = asyncio.Lock()
        self._dispatch_event = asyncio.Event()
        self._stopping = False

    async def subscribe(self, recipient: str) -> ConversationRecord:
        """Create an explicit invitation or return the existing subscription."""

        async with self._subscription_lock:
            existing = self.repository.get_conversation(recipient)
            if existing and existing.status in {
                ConversationStatus.PENDING,
                ConversationStatus.ACTIVE,
            }:
                return existing

            room_id = await self.matrix.create_direct_room(recipient)
            membership = self.matrix.membership(room_id, recipient)
            status = (
                ConversationStatus.ACTIVE
                if membership == "join"
                else ConversationStatus.PENDING
            )
            return self.repository.save_conversation(recipient, room_id, status)

    def get_subscription(self, recipient: str) -> ConversationRecord:
        conversation = self.repository.get_conversation(recipient)
        if conversation is None:
            raise ConversationNotFoundError(recipient)
        return conversation

    async def unsubscribe(self, recipient: str) -> ConversationRecord:
        """Disable the subscription and make the bot leave the room."""

        conversation = self.get_subscription(recipient)
        if conversation.status is not ConversationStatus.REVOKED:
            await self.matrix.leave_room(conversation.room_id)
        updated = self.repository.set_conversation_status(
            recipient,
            ConversationStatus.REVOKED,
        )
        assert updated is not None
        return updated

    def create_document_notification(
        self,
        request: DocumentNotificationRequest,
    ) -> DeliveryRecord:
        """Store an idempotent notification without waiting for Matrix."""

        duplicate = self.repository.get_delivery_by_idempotency_key(
            request.idempotency_key
        )
        if duplicate:
            return duplicate

        conversation = self.repository.get_conversation(request.recipient)
        if conversation is None or conversation.status in {
            ConversationStatus.REVOKED,
            ConversationStatus.ERROR,
        }:
            raise SubscriptionRequiredError(request.recipient)

        status = (
            DeliveryStatus.QUEUED
            if conversation.status is ConversationStatus.ACTIVE
            else DeliveryStatus.AWAITING_RECIPIENT
        )
        delivery, created = self.repository.create_delivery(
            idempotency_key=request.idempotency_key,
            recipient=request.recipient,
            room_id=conversation.room_id,
            message_body=render_document_notification(request),
            status=status,
        )
        if created and status is DeliveryStatus.QUEUED:
            self._dispatch_event.set()
        return delivery

    def get_delivery(self, delivery_id: str) -> DeliveryRecord:
        delivery = self.repository.get_delivery(delivery_id)
        if delivery is None:
            raise DeliveryNotFoundError(delivery_id)
        return delivery

    async def handle_membership(
        self,
        room_id: str,
        user_id: str,
        membership: str,
    ) -> None:
        """Handle the recipient accepting, declining, or leaving the room."""

        conversation = self.repository.get_conversation_by_room(room_id)
        if conversation is None or conversation.recipient != user_id:
            return

        if membership == "join":
            self.repository.set_conversation_status(
                user_id,
                ConversationStatus.ACTIVE,
            )
            self._dispatch_event.set()
        elif membership == "invite":
            self.repository.set_conversation_status(
                user_id,
                ConversationStatus.PENDING,
            )
        elif membership in {"leave", "ban"}:
            self.repository.set_conversation_status(
                user_id,
                ConversationStatus.REVOKED,
            )

    async def reconcile_memberships(self) -> None:
        """Reconcile SQLite with Matrix state after a restart."""

        for conversation in self.repository.list_conversations():
            membership = self.matrix.membership(
                conversation.room_id,
                conversation.recipient,
            )
            if membership:
                await self.handle_membership(
                    conversation.room_id,
                    conversation.recipient,
                    membership,
                )

    async def dispatch_once(self) -> int:
        """Send a batch of ready notifications and schedule retries."""

        sent = 0
        for delivery in self.repository.claim_due_deliveries():
            conversation = self.repository.get_conversation(delivery.recipient)
            if not conversation or conversation.status is not ConversationStatus.ACTIVE:
                if conversation and conversation.status is ConversationStatus.PENDING:
                    self.repository.set_delivery_status(
                        delivery.delivery_id,
                        DeliveryStatus.AWAITING_RECIPIENT,
                    )
                else:
                    self.repository.set_delivery_status(
                        delivery.delivery_id,
                        DeliveryStatus.RECIPIENT_DECLINED,
                        "The recipient does not have an active Tchap subscription",
                    )
                continue

            try:
                event_id = await self.matrix.send_notification(
                    delivery.room_id,
                    delivery.message_body,
                    delivery.delivery_id,
                )
            # One failed delivery must not interrupt the remaining deliveries.
            except Exception as error:
                logger.exception(
                    "Tchap delivery failed",
                    extra={"delivery_id": delivery.delivery_id},
                )
                self.repository.mark_delivery_retry(
                    delivery.delivery_id,
                    str(error),
                    self.max_delivery_attempts,
                )
            else:
                self.repository.mark_delivery_sent(delivery.delivery_id, event_id)
                sent += 1
        return sent

    async def run_dispatcher(self) -> None:
        """Delivery loop awakened by HTTP requests and Matrix events."""

        self.repository.reset_interrupted_deliveries()
        self._stopping = False
        while not self._stopping:
            await self.dispatch_once()
            self._dispatch_event.clear()
            try:
                await asyncio.wait_for(
                    self._dispatch_event.wait(),
                    timeout=self.dispatch_interval_seconds,
                )
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stopping = True
        self._dispatch_event.set()
