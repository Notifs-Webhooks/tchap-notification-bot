from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from nio import ProfileGetAvatarResponse, ProfileSetAvatarResponse, RoomCreateResponse
from nio.responses import UploadResponse

from notifier.matrix_gateway import TchapMatrixGateway


class FakeAvatarClient:
    def __init__(self) -> None:
        self.current_avatar: str | None = None
        self.uploads = 0
        self.avatar_updates: list[str] = []
        self.rooms: dict[str, SimpleNamespace] = {}
        self.room_state_updates: list[tuple[str, str, dict[str, str]]] = []
        self.room_create_arguments: dict[str, Any] | None = None

    async def upload(
        self,
        data_provider: BytesIO,
        **kwargs: object,
    ) -> tuple[UploadResponse, None]:
        assert kwargs["content_type"] == "image/png"
        assert data_provider.read() == b"avatar data"
        self.uploads += 1
        return UploadResponse("mxc://localhost/notifier-icon"), None

    async def get_avatar(self) -> ProfileGetAvatarResponse:
        return ProfileGetAvatarResponse(self.current_avatar)

    async def set_avatar(self, content_uri: str) -> ProfileSetAvatarResponse:
        self.current_avatar = content_uri
        self.avatar_updates.append(content_uri)
        return ProfileSetAvatarResponse()

    async def room_put_state(
        self,
        room_id: str,
        event_type: str,
        content: dict[str, str],
    ) -> object:
        self.room_state_updates.append((room_id, event_type, content))
        return object()

    async def room_create(self, **kwargs: Any) -> RoomCreateResponse:
        self.room_create_arguments = kwargs
        return RoomCreateResponse("!new-room:localhost")


def gateway(tmp_path: Path) -> tuple[TchapMatrixGateway, FakeAvatarClient]:
    avatar_path = tmp_path / "icon.png"
    avatar_path.write_bytes(b"avatar data")
    settings = SimpleNamespace(
        bot_avatar_path=avatar_path,
        matrix_store_path=tmp_path / "store",
        room_name="Docs notifications",
        room_topic="Document updates",
    )
    client = FakeAvatarClient()
    matrix_gateway = object.__new__(TchapMatrixGateway)
    matrix_gateway.settings = settings
    matrix_gateway.client = client
    matrix_gateway._avatar_content_uri = None
    return matrix_gateway, client


@pytest.mark.asyncio()
async def test_avatar_is_uploaded_once_and_restored_from_cache(tmp_path: Path) -> None:
    matrix_gateway, client = gateway(tmp_path)

    await matrix_gateway._ensure_avatar()
    client.current_avatar = None
    await matrix_gateway._ensure_avatar()

    assert client.uploads == 1
    assert client.avatar_updates == [
        "mxc://localhost/notifier-icon",
        "mxc://localhost/notifier-icon",
    ]


@pytest.mark.asyncio()
async def test_avatar_is_applied_to_existing_and_new_rooms(tmp_path: Path) -> None:
    matrix_gateway, client = gateway(tmp_path)
    await matrix_gateway._ensure_avatar()
    client.rooms = {
        "!notifier:localhost": SimpleNamespace(
            room_id="!notifier:localhost",
            name="Docs notifications",
            room_avatar_url=None,
        ),
        "!unrelated:localhost": SimpleNamespace(
            room_id="!unrelated:localhost",
            name="Another room",
            room_avatar_url=None,
        ),
    }

    await matrix_gateway._ensure_existing_room_avatars()
    await matrix_gateway.create_direct_room("@alice:localhost")

    assert client.room_state_updates == [
        (
            "!notifier:localhost",
            "m.room.avatar",
            {"url": "mxc://localhost/notifier-icon"},
        )
    ]
    assert client.room_create_arguments is not None
    initial_state = client.room_create_arguments["initial_state"]
    assert {
        "type": "m.room.avatar",
        "state_key": "",
        "content": {"url": "mxc://localhost/notifier-icon"},
    } in initial_state
