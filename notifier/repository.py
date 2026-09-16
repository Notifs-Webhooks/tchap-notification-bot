"""SQLite storage for subscriptions and deliveries."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from notifier.models import ConversationStatus, DeliveryStatus


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ConversationRecord:
    recipient: str
    room_id: str
    status: ConversationStatus
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DeliveryRecord:
    delivery_id: str
    idempotency_key: str
    recipient: str
    room_id: str
    message_body: str
    status: DeliveryStatus
    event_id: str | None
    attempts: int
    next_attempt_at: str
    error: str | None
    created_at: str
    updated_at: str


class NotificationRepository:
    """Small synchronous repository protected for light concurrent use."""

    def __init__(self, database_path: Path | str) -> None:
        path = Path(database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    recipient TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    recipient TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    message_body TEXT NOT NULL,
                    status TEXT NOT NULL,
                    event_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(recipient) REFERENCES conversations(recipient)
                );

                CREATE INDEX IF NOT EXISTS deliveries_dispatch_idx
                    ON deliveries(status, next_attempt_at);
                """
            )

    @staticmethod
    def _conversation(row: sqlite3.Row | None) -> ConversationRecord | None:
        if row is None:
            return None
        return ConversationRecord(
            recipient=row["recipient"],
            room_id=row["room_id"],
            status=ConversationStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _delivery(row: sqlite3.Row | None) -> DeliveryRecord | None:
        if row is None:
            return None
        return DeliveryRecord(
            delivery_id=row["delivery_id"],
            idempotency_key=row["idempotency_key"],
            recipient=row["recipient"],
            room_id=row["room_id"],
            message_body=row["message_body"],
            status=DeliveryStatus(row["status"]),
            event_id=row["event_id"],
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def get_conversation(self, recipient: str) -> ConversationRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM conversations WHERE recipient = ?", (recipient,)
            ).fetchone()
        return self._conversation(row)

    def get_conversation_by_room(self, room_id: str) -> ConversationRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM conversations WHERE room_id = ?", (room_id,)
            ).fetchone()
        return self._conversation(row)

    def list_conversations(self) -> list[ConversationRecord]:
        with self._lock:
            rows = self._connection.execute("SELECT * FROM conversations").fetchall()
        return [record for row in rows if (record := self._conversation(row))]

    def save_conversation(
        self,
        recipient: str,
        room_id: str,
        status: ConversationStatus,
    ) -> ConversationRecord:
        now = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO conversations(recipient, room_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(recipient) DO UPDATE SET
                    room_id = excluded.room_id,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (recipient, room_id, status.value, now, now),
            )
        record = self.get_conversation(recipient)
        assert record is not None
        return record

    def set_conversation_status(
        self,
        recipient: str,
        status: ConversationStatus,
    ) -> ConversationRecord | None:
        now = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE conversations SET status = ?, updated_at = ? WHERE recipient = ?",
                (status.value, now, recipient),
            )

            if status is ConversationStatus.ACTIVE:
                self._connection.execute(
                    """
                    UPDATE deliveries SET status = ?, next_attempt_at = ?, updated_at = ?, error = NULL
                    WHERE recipient = ? AND status = ?
                    """,
                    (
                        DeliveryStatus.QUEUED.value,
                        now,
                        now,
                        recipient,
                        DeliveryStatus.AWAITING_RECIPIENT.value,
                    ),
                )
            elif status is ConversationStatus.REVOKED:
                self._connection.execute(
                    """
                    UPDATE deliveries SET status = ?, updated_at = ?, error = ?
                    WHERE recipient = ? AND status IN (?, ?, ?)
                    """,
                    (
                        DeliveryStatus.RECIPIENT_DECLINED.value,
                        now,
                        "The recipient declined the invitation or left the Tchap room",
                        recipient,
                        DeliveryStatus.AWAITING_RECIPIENT.value,
                        DeliveryStatus.QUEUED.value,
                        DeliveryStatus.SENDING.value,
                    ),
                )
        return self.get_conversation(recipient)

    def get_delivery(self, delivery_id: str) -> DeliveryRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
        return self._delivery(row)

    def get_delivery_by_idempotency_key(self, key: str) -> DeliveryRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM deliveries WHERE idempotency_key = ?", (key,)
            ).fetchone()
        return self._delivery(row)

    def create_delivery(
        self,
        *,
        idempotency_key: str,
        recipient: str,
        room_id: str,
        message_body: str,
        status: DeliveryStatus,
    ) -> tuple[DeliveryRecord, bool]:
        existing = self.get_delivery_by_idempotency_key(idempotency_key)
        if existing:
            return existing, False

        now = utc_now()
        delivery_id = str(uuid4())
        try:
            with self._lock, self._connection:
                self._connection.execute(
                    """
                    INSERT INTO deliveries(
                        delivery_id, idempotency_key, recipient, room_id, message_body,
                        status, next_attempt_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        delivery_id,
                        idempotency_key,
                        recipient,
                        room_id,
                        message_body,
                        status.value,
                        now,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError:
            raced = self.get_delivery_by_idempotency_key(idempotency_key)
            if raced is None:
                raise
            return raced, False

        record = self.get_delivery(delivery_id)
        assert record is not None
        return record, True

    def claim_due_deliveries(self, limit: int = 50) -> list[DeliveryRecord]:
        now = utc_now()
        claimed: list[DeliveryRecord] = []
        with self._lock, self._connection:
            rows = self._connection.execute(
                """
                SELECT delivery_id FROM deliveries
                WHERE status = ? AND next_attempt_at <= ?
                ORDER BY created_at
                LIMIT ?
                """,
                (DeliveryStatus.QUEUED.value, now, limit),
            ).fetchall()
            for row in rows:
                cursor = self._connection.execute(
                    """
                    UPDATE deliveries SET status = ?, updated_at = ?
                    WHERE delivery_id = ? AND status = ?
                    """,
                    (
                        DeliveryStatus.SENDING.value,
                        now,
                        row["delivery_id"],
                        DeliveryStatus.QUEUED.value,
                    ),
                )
                if cursor.rowcount:
                    delivery_row = self._connection.execute(
                        "SELECT * FROM deliveries WHERE delivery_id = ?",
                        (row["delivery_id"],),
                    ).fetchone()
                    record = self._delivery(delivery_row)
                    if record:
                        claimed.append(record)
        return claimed

    def mark_delivery_sent(self, delivery_id: str, event_id: str) -> None:
        now = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE deliveries SET status = ?, event_id = ?, error = NULL, updated_at = ?
                WHERE delivery_id = ?
                """,
                (DeliveryStatus.SENT.value, event_id, now, delivery_id),
            )

    def set_delivery_status(
        self,
        delivery_id: str,
        status: DeliveryStatus,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE deliveries SET status = ?, error = ?, updated_at = ?
                WHERE delivery_id = ?
                """,
                (status.value, error, now, delivery_id),
            )

    def mark_delivery_retry(
        self,
        delivery_id: str,
        error: str,
        max_attempts: int,
    ) -> None:
        delivery = self.get_delivery(delivery_id)
        if delivery is None:
            return
        attempts = delivery.attempts + 1
        now = datetime.now(UTC)
        terminal = attempts >= max_attempts
        status = DeliveryStatus.FAILED if terminal else DeliveryStatus.QUEUED
        retry_delay = min(300, 5 * (2 ** max(0, attempts - 1)))
        next_attempt = now if terminal else now + timedelta(seconds=retry_delay)
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE deliveries
                SET status = ?, attempts = ?, next_attempt_at = ?, error = ?, updated_at = ?
                WHERE delivery_id = ?
                """,
                (
                    status.value,
                    attempts,
                    next_attempt.isoformat(),
                    error[:2000],
                    now.isoformat(),
                    delivery_id,
                ),
            )

    def reset_interrupted_deliveries(self) -> None:
        now = utc_now()
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE deliveries SET status = ?, next_attempt_at = ?, updated_at = ?
                WHERE status = ?
                """,
                (
                    DeliveryStatus.QUEUED.value,
                    now,
                    now,
                    DeliveryStatus.SENDING.value,
                ),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()
