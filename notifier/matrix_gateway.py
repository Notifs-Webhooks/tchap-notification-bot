"""Adapter between Notifier and matrix-nio/Tchap Bot."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from matrix_bot.auth import AuthLogin, Credentials
from matrix_bot.client import MatrixClient
from matrix_bot.config import bot_lib_config
from nio import (
    ErrorResponse,
    Event,
    MatrixRoom,
    RoomCreateResponse,
    RoomMemberEvent,
    RoomPreset,
    RoomSendResponse,
    RoomVisibility,
    SyncResponse,
)

from notifier.settings import NotifierSettings

logger = logging.getLogger(__name__)
MembershipHandler = Callable[[str, str, str], Awaitable[None]]


class MatrixOperationError(RuntimeError):
    """A Matrix operation returned a protocol error."""


class TchapMatrixGateway:
    """Long-running Matrix client with persistent cryptographic storage."""

    def __init__(self, settings: NotifierSettings) -> None:
        bot_lib_config.store_path = settings.matrix_store_path
        bot_lib_config.session_path = settings.matrix_session_path
        bot_lib_config.encryption_enabled = True
        bot_lib_config.ignore_unverified_devices = True

        credentials = Credentials(
            homeserver=settings.homeserver,
            username=settings.bot_username,
            password=settings.bot_password.get_secret_value(),
            session_stored_file_path=settings.matrix_session_path,
        )
        self.client = MatrixClient(AuthLogin(credentials))
        self.settings = settings
        self._membership_handler: MembershipHandler | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self, membership_handler: MembershipHandler) -> None:
        """Authenticate the device, load its keys, and perform the first sync."""

        self._membership_handler = membership_handler
        self.client.add_event_callback(self._on_membership, RoomMemberEvent)
        await self.client.automatic_login()

        display_name_response = await self.client.set_displayname(
            self.settings.bot_display_name
        )
        if isinstance(display_name_response, ErrorResponse):
            logger.warning("Could not update the bot display name")

        sync_response = await self.client.sync(timeout=0, full_state=True)
        if not isinstance(sync_response, SyncResponse):
            raise MatrixOperationError(f"Initial Matrix sync failed: {sync_response}")
        self._connected = True
        logger.info(
            "Notifier connected to Matrix",
            extra={
                "homeserver": self.client.homeserver,
                "user_id": self.client.user_id,
                "device_id": self.client.device_id,
            },
        )

    async def _on_membership(
        self,
        room: MatrixRoom,
        event: Event,
    ) -> None:
        if self._membership_handler and isinstance(event, RoomMemberEvent):
            await self._membership_handler(
                room.room_id,
                event.state_key,
                event.membership,
            )

    async def sync_forever(self) -> None:
        try:
            await self.client.sync_forever(timeout=60_000, full_state=False)
        finally:
            self._connected = False

    async def create_direct_room(self, recipient: str) -> str:
        """Create an encrypted direct room and invite the recipient."""

        response = await self.client.room_create(
            visibility=RoomVisibility.private,
            name=self.settings.room_name,
            topic=self.settings.room_topic,
            federate=True,
            is_direct=True,
            preset=RoomPreset.trusted_private_chat,
            invite=[recipient],
            initial_state=[
                {
                    "type": "m.room.encryption",
                    "state_key": "",
                    "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                },
                {
                    "type": "m.room.history_visibility",
                    "state_key": "",
                    "content": {"history_visibility": "invited"},
                },
                {
                    "type": "m.room.guest_access",
                    "state_key": "",
                    "content": {"guest_access": "forbidden"},
                },
            ],
        )
        if not isinstance(response, RoomCreateResponse):
            raise MatrixOperationError(f"Could not create the room: {response}")
        return response.room_id

    def membership(self, room_id: str, recipient: str) -> str | None:
        room = self.client.rooms.get(room_id)
        if room is None:
            return None
        if recipient in room.invited_users:
            return "invite"
        if recipient in room.users:
            return "join"
        return "leave"

    async def send_notification(
        self,
        room_id: str,
        message: str,
        transaction_id: str,
    ) -> str:
        """Send encrypted text with an idempotent transaction ID."""

        response = await self.client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": message},
            tx_id=transaction_id,
            ignore_unverified_devices=True,
        )
        if not isinstance(response, RoomSendResponse):
            raise MatrixOperationError(f"Could not send the message: {response}")
        return response.event_id

    async def leave_room(self, room_id: str) -> None:
        response = await self.client.room_leave(room_id)
        if isinstance(response, ErrorResponse):
            raise MatrixOperationError(f"Could not leave the room: {response}")

    async def close(self) -> None:
        self._connected = False
        await self.client.close()


async def cancel_task(task: asyncio.Task[object]) -> None:
    """Cancel and await a task without hiding its cancellation."""

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
