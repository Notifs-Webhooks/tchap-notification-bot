"""Notifier's authenticated HTTP API."""

from __future__ import annotations

import secrets
from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from notifier.models import (
    DeliveryResponse,
    DocumentNotificationRequest,
    HealthResponse,
    SubscriptionRequest,
    SubscriptionResponse,
)
from notifier.repository import ConversationRecord, DeliveryRecord
from notifier.service import (
    ConversationNotFoundError,
    DeliveryNotFoundError,
    NotifierService,
    SubscriptionRequiredError,
)


def subscription_response(record: ConversationRecord) -> SubscriptionResponse:
    return SubscriptionResponse(
        recipient=record.recipient,
        status=record.status,
        room_id=record.room_id,
    )


def delivery_response(record: DeliveryRecord) -> DeliveryResponse:
    return DeliveryResponse(
        delivery_id=record.delivery_id,
        idempotency_key=record.idempotency_key,
        recipient=record.recipient,
        status=record.status,
        room_id=record.room_id,
        event_id=record.event_id,
        error=record.error,
    )


def create_app(
    service: NotifierService,
    api_token: str,
    is_ready: Callable[[], bool],
) -> FastAPI:
    app = FastAPI(
        title="Tchap Notifier",
        version="0.1.0",
        description="Docs change notifications delivered through Tchap direct messages.",
    )
    bearer = HTTPBearer(auto_error=False)

    async def authenticate(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> None:
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not secrets.compare_digest(credentials.credentials, api_token)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid API token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/healthz", response_model=HealthResponse, tags=["system"])
    async def health() -> HealthResponse:
        ready = is_ready()
        return HealthResponse(
            status="ok" if ready else "starting",
            matrix_connected=ready,
        )

    @app.get("/readyz", response_model=HealthResponse, tags=["system"])
    async def readiness() -> HealthResponse:
        ready = is_ready()
        if not ready:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="The Matrix connection is not ready",
            )
        return HealthResponse(status="ok", matrix_connected=True)

    @app.post(
        "/v1/subscriptions",
        response_model=SubscriptionResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(authenticate)],
        tags=["subscriptions"],
    )
    async def subscribe(request: SubscriptionRequest) -> SubscriptionResponse:
        record = await service.subscribe(request.recipient)
        return subscription_response(record)

    @app.get(
        "/v1/subscriptions",
        response_model=SubscriptionResponse,
        dependencies=[Depends(authenticate)],
        tags=["subscriptions"],
    )
    async def get_subscription(
        recipient: str = Query(min_length=4, max_length=255),
    ) -> SubscriptionResponse:
        try:
            record = service.get_subscription(recipient)
        except ConversationNotFoundError as error:
            raise HTTPException(
                status_code=404, detail="Unknown subscription"
            ) from error
        return subscription_response(record)

    @app.delete(
        "/v1/subscriptions",
        response_model=SubscriptionResponse,
        dependencies=[Depends(authenticate)],
        tags=["subscriptions"],
    )
    async def unsubscribe(
        recipient: str = Query(min_length=4, max_length=255),
    ) -> SubscriptionResponse:
        try:
            record = await service.unsubscribe(recipient)
        except ConversationNotFoundError as error:
            raise HTTPException(
                status_code=404, detail="Unknown subscription"
            ) from error
        return subscription_response(record)

    @app.post(
        "/v1/notifications",
        response_model=DeliveryResponse,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(authenticate)],
        tags=["notifications"],
    )
    async def notify(request: DocumentNotificationRequest) -> DeliveryResponse:
        try:
            record = service.create_document_notification(request)
        except SubscriptionRequiredError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "The recipient must enable notifications and accept the "
                    "Tchap invitation first"
                ),
            ) from error
        return delivery_response(record)

    @app.get(
        "/v1/deliveries/{delivery_id}",
        response_model=DeliveryResponse,
        dependencies=[Depends(authenticate)],
        tags=["notifications"],
    )
    async def get_delivery(delivery_id: str) -> DeliveryResponse:
        try:
            record = service.get_delivery(delivery_id)
        except DeliveryNotFoundError as error:
            raise HTTPException(status_code=404, detail="Unknown delivery") from error
        return delivery_response(record)

    return app
