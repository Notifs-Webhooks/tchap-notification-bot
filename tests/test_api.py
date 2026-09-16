from __future__ import annotations

from fastapi.testclient import TestClient

from notifier.api import create_app

TOKEN = "test-token-with-more-than-thirty-two-characters"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
RECIPIENT = "@alice:localhost"


def payload() -> dict[str, str]:
    return {
        "recipient": RECIPIENT,
        "idempotency_key": "document-42-version-7",
        "actor_name": "Alice Martin",
        "document_id": "42",
        "document_title": "Budget 2027",
        "document_url": "https://docs.example.test/docs/42",
        "change_type": "updated",
    }


def test_routes_are_authenticated_and_subscription_is_explicit(service) -> None:
    client = TestClient(create_app(service, TOKEN, lambda: True))

    assert (
        client.post("/v1/subscriptions", json={"recipient": RECIPIENT}).status_code
        == 401
    )

    missing_subscription = client.post(
        "/v1/notifications",
        headers=AUTH,
        json=payload(),
    )
    assert missing_subscription.status_code == 409

    subscription = client.post(
        "/v1/subscriptions",
        headers=AUTH,
        json={"recipient": RECIPIENT},
    )
    assert subscription.status_code == 202
    assert subscription.json()["status"] == "pending"

    delivery = client.post("/v1/notifications", headers=AUTH, json=payload())
    assert delivery.status_code == 202
    assert delivery.json()["status"] == "awaiting_recipient"

    readback = client.get(
        f"/v1/deliveries/{delivery.json()['delivery_id']}",
        headers=AUTH,
    )
    assert readback.status_code == 200
    assert readback.json()["idempotency_key"] == "document-42-version-7"


def test_health_does_not_require_authentication(service) -> None:
    client = TestClient(create_app(service, TOKEN, lambda: False))

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json() == {"status": "starting", "matrix_connected": False}
    assert client.get("/readyz").status_code == 503


def test_rejects_invalid_matrix_id_and_unknown_fields(service) -> None:
    client = TestClient(create_app(service, TOKEN, lambda: True))

    response = client.post(
        "/v1/subscriptions",
        headers=AUTH,
        json={"recipient": "alice@example.test", "unexpected": True},
    )
    assert response.status_code == 422
