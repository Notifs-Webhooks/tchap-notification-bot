# Notifier

Notifier is an HTTP service that turns changes made in Docs into end-to-end
encrypted Tchap direct messages.

It is built on `tchap-bot`, the library used by Tchap bots, and `matrix-nio`.
The Notifier account is a regular Tchap account: it cannot force a user to join
a conversation.

## How consent works

Enabling notifications is voluntary and happens in two stages:

1. the Docs backend calls `POST /v1/subscriptions` after the user clicks the
   future "Enable Tchap notifications" button;
2. Notifier creates an encrypted private room and invites the user;
3. the subscription remains `pending` until the user accepts the invitation in
   Tchap;
4. the Matrix membership transition from `invite` to `join` activates the
   subscription;
5. pending notifications are then delivered to the room;
6. declining the invitation or leaving the room revokes the subscription and
   cancels notifications that have not been sent yet.

Notifier never automatically reinvites a user who declined the invitation or
left the room. A new explicit `POST /v1/subscriptions` call, resulting from a
new user action, is required.

A notification looks like this:

```text
📝 Document updated

Alice Martin updated "Budget 2027".

Open document: https://docs.example.test/docs/42
```

Supported change types are `created`, `updated`, `renamed`, `commented`,
`shared`, `deleted`, and `restored`.

## Architecture

The process runs three components in the same asynchronous event loop:

- a FastAPI HTTP API on port `8085`;
- a long-running Matrix synchronization loop that receives membership changes
  and manages encryption;
- a dispatcher that sends ready notifications and retries temporary failures.

SQLite stores conversations, deliveries, and idempotency keys. The
`matrix-nio` store holds the device's cryptographic identity. The database,
cryptographic store, session file, and avatar media cache must all be
persisted. On startup, Notifier uploads the bundled `icon.png` to Matrix and
sets it as the account avatar. The cached Matrix media URI prevents duplicate
uploads on subsequent restarts; replacing `icon.png` automatically triggers a
new upload. The same image is assigned to every notification conversation,
including existing rooms discovered at startup.

## HTTP API

Every `/v1/*` endpoint requires:

```http
Authorization: Bearer <api_token>
```

The token is a server-side secret. It must never be embedded in the Docs
frontend JavaScript. The future UI button will call the Docs backend, which
will then call Notifier.

Interactive OpenAPI documentation is available at `/docs` while the service is
running.

### Enable notifications

```bash
curl -X POST http://127.0.0.1:8085/v1/subscriptions \
  -H "Authorization: Bearer $NOTIFIER_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"recipient":"@bob:localhost"}'
```

Response before the invitation is accepted in Tchap:

```json
{
  "recipient": "@bob:localhost",
  "status": "pending",
  "room_id": "!abc:localhost"
}
```

`POST /v1/subscriptions` is idempotent while the subscription is `pending` or
`active`. After revocation, it creates a new invitation.

### Read a subscription

```bash
curl --get http://127.0.0.1:8085/v1/subscriptions \
  -H "Authorization: Bearer $NOTIFIER_API_TOKEN" \
  --data-urlencode 'recipient=@bob:localhost'
```

Possible states:

- `pending`: invitation sent but not yet accepted;
- `active`: the user joined the room and messages may be sent;
- `revoked`: invitation declined, room left, or subscription disabled;
- `error`: permanent conversation failure.

### Disable notifications

```bash
curl -X DELETE 'http://127.0.0.1:8085/v1/subscriptions?recipient=%40bob%3Alocalhost' \
  -H "Authorization: Bearer $NOTIFIER_API_TOKEN"
```

The bot leaves the room and cancels notifications that have not been sent.

### Send a document notification

A subscription must already exist. Without prior activation, the API returns
`409 Conflict` and does not create an invitation.

```bash
curl -X POST http://127.0.0.1:8085/v1/notifications \
  -H "Authorization: Bearer $NOTIFIER_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "recipient": "@bob:localhost",
    "idempotency_key": "document-42-version-7-bob",
    "actor_name": "Alice Martin",
    "document_id": "42",
    "document_title": "Budget 2027",
    "document_url": "https://docs.example.test/docs/42",
    "change_type": "updated",
    "occurred_at": "2026-09-16T10:30:00Z"
  }'
```

The HTTP response is `202 Accepted`. If the invitation is still pending, the
delivery status is `awaiting_recipient`. If the subscription is active, it is
`queued` and then becomes `sent` after Matrix accepts the message.

`idempotency_key` must identify one unique logical delivery. It should normally
include the Docs event identifier and the recipient identifier. Reusing the
same key returns the existing delivery without creating a duplicate message.

### Read a delivery

```bash
curl http://127.0.0.1:8085/v1/deliveries/DELIVERY_ID \
  -H "Authorization: Bearer $NOTIFIER_API_TOKEN"
```

Possible states are `awaiting_recipient`, `queued`, `sending`, `sent`,
`recipient_declined`, and `failed`.

### Health checks

```bash
curl http://127.0.0.1:8085/healthz
curl http://127.0.0.1:8085/readyz
```

`/readyz` returns `200` only while the Matrix session is connected.

## Local setup with the tchap-web-notifs Synapse server

### 1. Start Tchap and Matrix

From the neighboring `tchap-web-notifs` repository:

```bash
TCHAP_PUBLIC_HOST=127.0.0.1 ./run-tchap.sh
```

Synapse then listens on `http://127.0.0.1:8008`. Bob is available with the
Matrix ID `@bob:localhost`.

### 2. Create the local Notifier account

This command only needs to be run once:

```bash
docker exec -it tchap-matrix-local \
  register_new_matrix_user \
  -u notifier \
  -p 'NotifierLocal2026!' \
  --no-admin \
  --exists-ok \
  -c /data/homeserver.yaml \
  http://localhost:8008
```

### 3. Configure the service

```bash
cp config.example.toml config.toml
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Copy the generated secret into `api_token`. The local Docker configuration
should contain at least:

```toml
homeserver = "http://host.docker.internal:8008"
bot_username = "@notifier:localhost"
bot_password = "NotifierLocal2026!"
api_token = "THE_GENERATED_SECRET"
```

Git ignores `config.toml`, tokens, and cryptographic keys. The provided Compose
file stores the database, session, and cryptographic store in the named
`notifier_data` Docker volume.

### 4. Start Notifier

```bash
bash ./run.sh start
```

Then check readiness:

```bash
bash ./run.sh status
```

The launcher detects either `docker compose` or the legacy `docker-compose`
command. It also provides `stop`, `restart`, and `logs` commands. You can still
run `docker compose up --build` directly if preferred.

Use the API examples with `@bob:localhost`, then sign in to Tchap as Bob and
accept the invitation.

`bash ./run.sh stop` stops the service without deleting its Matrix identity.
Avoid `docker compose down -v`: it also deletes the E2EE store, session, and
delivery database.

## Development without Docker

Requirements: Python 3.11 or 3.12, Poetry, and the system dependencies required
by `matrix-nio[e2e]`.

```bash
poetry install
cp config.example.toml config.toml
```

For execution outside Docker, adjust the homeserver and storage paths:

```toml
homeserver = "http://127.0.0.1:8008"
database_path = "./data/notifier.sqlite3"
matrix_store_path = "./data/store"
matrix_session_path = "./data/session.txt"
```

Then run:

```bash
poetry run notifier
```

Run tests and quality checks with:

```bash
poetry run pytest
poetry run ruff check .
poetry run basedpyright
```

## Tchap deployment

The Notifier account must be created manually through Tchap with a working,
authorized email address. Tchap does not currently provide service accounts, so
the account remains subject to periodic renewal by email.

In production:

- use the Notifier account's homeserver, not the recipient's homeserver;
- pass complete Matrix IDs to the API, for example
  `@first.last:agent.dinum.tchap.gouv.fr`;
- keep `/data` on a backed-up persistent volume;
- inject `bot_password` and `api_token` from a secret manager;
- expose the API only to the Docs backend, preferably on a private network;
- terminate TLS at the reverse proxy;
- monitor `/readyz` and deliveries in the `failed` state;
- never delete `store/` or `session.txt` without a cryptographic-device
  rotation procedure.

`NOTIFIER_*` environment variables override `config.toml`, including
`NOTIFIER_API_TOKEN`, `NOTIFIER_BOT_PASSWORD`, and `NOTIFIER_HOMESERVER`.
`NOTIFIER_CONFIG` selects a different TOML configuration file. Set
`NOTIFIER_BOT_AVATAR_PATH` when deploying a different PNG outside the Docker
image.

## Guarantees and limitations

- The HTTP API and Matrix both use idempotent identifiers to limit duplicates
  during retries.
- Messages are encrypted in E2EE Matrix rooms.
- A user must accept the invitation before the first delivery.
- Leaving the room revokes permission to send messages.
- Notifier does not resolve email addresses into Matrix IDs.
- The database contains the body of pending messages and must be protected as
  sensitive application data.
