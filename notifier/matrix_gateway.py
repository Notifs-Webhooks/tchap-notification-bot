"""Adapter between Notifier and matrix-nio/Tchap Bot."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from io import BytesIO
from pathlib import Path
from typing import TypedDict, cast

from matrix_bot.auth import AuthLogin, Credentials
from matrix_bot.client import MatrixClient
from matrix_bot.config import bot_lib_config
from nio import (
    ErrorResponse,
    Event,
    MatrixRoom,
    ProfileGetAvatarResponse,
    RoomCreateResponse,
    RoomMemberEvent,
    RoomPreset,
    RoomSendResponse,
    RoomVisibility,
    SyncResponse,
    UploadResponse,
)

from notifier.settings import NotifierSettings

logger = logging.getLogger(__name__)
MembershipHandler = Callable[[str, str, str], Awaitable[None]]


class AvatarState(TypedDict):
    """Persistent link between the bundled image and its Matrix media URI."""

    sha256: str
    content_uri: str


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
        self._avatar_content_uri: str | None = None
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

        await self._ensure_avatar()

        sync_response = await self.client.sync(timeout=0, full_state=True)
        if not isinstance(sync_response, SyncResponse):
            raise MatrixOperationError(f"Initial Matrix sync failed: {sync_response}")
        await self._ensure_existing_room_avatars()
        self._connected = True
        logger.info(
            "Notifier connected to Matrix",
            extra={
                "homeserver": self.client.homeserver,
                "user_id": self.client.user_id,
                "device_id": self.client.device_id,
            },
        )

    @property
    def _avatar_state_path(self) -> Path:
        return self.settings.matrix_store_path / "notifier-avatar.json"

    def _load_avatar_state(self) -> AvatarState | None:
        try:
            raw_state: object = json.loads(
                self._avatar_state_path.read_text(encoding="utf-8")
            )
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

        if not isinstance(raw_state, dict):
            return None
        state = cast("dict[str, object]", raw_state)
        sha256 = state.get("sha256")
        content_uri = state.get("content_uri")
        if not isinstance(sha256, str) or not isinstance(content_uri, str):
            return None
        if not content_uri.startswith("mxc://"):
            return None
        return {"sha256": sha256, "content_uri": content_uri}

    def _save_avatar_state(self, state: AvatarState) -> None:
        state_path = self._avatar_state_path
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = state_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(state, sort_keys=True),
            encoding="utf-8",
        )
        temporary_path.replace(state_path)

    async def _ensure_avatar(self) -> None:
        avatar_path = self.settings.bot_avatar_path
        try:
            avatar_data = avatar_path.read_bytes()
        except OSError as error:
            raise MatrixOperationError(
                f"Could not read the bot avatar at {avatar_path}: {error}"
            ) from error

        avatar_hash = hashlib.sha256(avatar_data).hexdigest()
        avatar_state = self._load_avatar_state()
        content_uri: str

        if avatar_state is not None and avatar_state["sha256"] == avatar_hash:
            content_uri = avatar_state["content_uri"]
        else:
            upload_response, _ = await self.client.upload(
                BytesIO(avatar_data),
                content_type="image/png",
                filename=avatar_path.name,
                filesize=len(avatar_data),
            )
            if not isinstance(upload_response, UploadResponse):
                raise MatrixOperationError(
                    f"Could not upload the bot avatar: {upload_response}"
                )
            content_uri = upload_response.content_uri
            self._save_avatar_state({"sha256": avatar_hash, "content_uri": content_uri})

        self._avatar_content_uri = content_uri
        avatar_response = await self.client.get_avatar()
        if (
            isinstance(avatar_response, ProfileGetAvatarResponse)
            and avatar_response.avatar_url == content_uri
        ):
            logger.info("Notifier avatar is already up to date")
            return

        set_avatar_response = await self.client.set_avatar(content_uri)
        if isinstance(set_avatar_response, ErrorResponse):
            raise MatrixOperationError(
                f"Could not update the bot avatar: {set_avatar_response}"
            )
        logger.info("Notifier avatar updated")

    async def _ensure_existing_room_avatars(self) -> None:
        content_uri = self._avatar_content_uri
        if content_uri is None:
            raise MatrixOperationError("The bot avatar has not been initialized")

        for room in self.client.rooms.values():
            if (
                room.name != self.settings.room_name
                or room.room_avatar_url == content_uri
            ):
                continue
            response = await self.client.room_put_state(
                room.room_id,
                "m.room.avatar",
                {"url": content_uri},
            )
            if isinstance(response, ErrorResponse):
                logger.warning(
                    "Could not update the conversation avatar",
                    extra={"room_id": room.room_id},
                )
                continue
            room.room_avatar_url = content_uri
            logger.info(
                "Conversation avatar updated",
                extra={"room_id": room.room_id},
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

        if self._avatar_content_uri is None:
            raise MatrixOperationError("The bot avatar has not been initialized")

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
                {
                    "type": "m.room.avatar",
                    "state_key": "",
                    "content": {"url": self._avatar_content_uri},
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
