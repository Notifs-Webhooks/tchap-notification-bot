from __future__ import annotations

from collections.abc import Iterator

import pytest

from notifier.repository import NotificationRepository
from notifier.service import NotifierService


class FakeMatrixGateway:
    def __init__(self) -> None:
        self.connected = True
        self.rooms: dict[str, dict[str, str]] = {}
        self.sent: list[tuple[str, str, str]] = []
        self.left: list[str] = []
        self.created = 0
        self.send_error: Exception | None = None

    async def create_direct_room(self, recipient: str) -> str:
        self.created += 1
        room_id = f"!room-{self.created}:localhost"
        self.rooms[room_id] = {recipient: "invite"}
        return room_id

    async def send_notification(
        self,
        room_id: str,
        message: str,
        transaction_id: str,
    ) -> str:
        if self.send_error:
            raise self.send_error
        self.sent.append((room_id, message, transaction_id))
        return f"$event-{transaction_id}"

    async def leave_room(self, room_id: str) -> None:
        self.left.append(room_id)

    def membership(self, room_id: str, recipient: str) -> str | None:
        return self.rooms.get(room_id, {}).get(recipient)


@pytest.fixture()
def repository(tmp_path) -> Iterator[NotificationRepository]:
    repo = NotificationRepository(tmp_path / "notifier.sqlite3")
    yield repo
    repo.close()


@pytest.fixture()
def matrix() -> FakeMatrixGateway:
    return FakeMatrixGateway()


@pytest.fixture()
def service(repository, matrix) -> NotifierService:
    return NotifierService(
        repository,
        matrix,
        dispatch_interval_seconds=0.01,
        max_delivery_attempts=3,
    )
