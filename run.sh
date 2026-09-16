#!/usr/bin/env bash

set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$PROJECT_DIR/config.toml"
SERVICE_NAME="notifier"
READY_URL="${NOTIFIER_READY_URL:-http://127.0.0.1:8085/readyz}"
START_TIMEOUT="${NOTIFIER_START_TIMEOUT:-60}"
LOG_TAIL="${NOTIFIER_LOG_TAIL:-100}"

declare -a COMPOSE_CMD=()

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
    RED=$'\033[0;31m'
    GREEN=$'\033[0;32m'
    YELLOW=$'\033[1;33m'
    RESET=$'\033[0m'
else
    RED=""
    GREEN=""
    YELLOW=""
    RESET=""
fi

log_info() {
    printf '%s[INFO]%s %s\n' "$GREEN" "$RESET" "$*"
}

log_warn() {
    printf '%s[WARN]%s %s\n' "$YELLOW" "$RESET" "$*" >&2
}

log_error() {
    printf '%s[ERROR]%s %s\n' "$RED" "$RESET" "$*" >&2
}

resolve_compose() {
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        COMPOSE_CMD=(docker compose)
        return
    fi

    if command -v docker-compose >/dev/null 2>&1; then
        COMPOSE_CMD=(docker-compose)
        return
    fi

    log_error "Docker Compose was not found. Install Docker with the Compose plugin first."
    exit 1
}

compose() {
    (
        cd "$PROJECT_DIR"
        "${COMPOSE_CMD[@]}" "$@"
    )
}

require_config() {
    if [[ -f "$CONFIG_FILE" ]]; then
        return
    fi

    log_error "Missing $CONFIG_FILE"
    log_error "Copy config.example.toml to config.toml and configure it first."
    exit 1
}

validate_settings() {
    if [[ ! "$START_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
        log_error "NOTIFIER_START_TIMEOUT must be a positive integer."
        exit 1
    fi

    if [[ ! "$LOG_TAIL" =~ ^[1-9][0-9]*$ ]]; then
        log_error "NOTIFIER_LOG_TAIL must be a positive integer."
        exit 1
    fi
}

service_container_id() {
    compose ps --quiet "$SERVICE_NAME"
}

is_running() {
    [[ -n "$(service_container_id)" ]]
}

show_recent_logs() {
    compose logs --no-color --tail="$LOG_TAIL" "$SERVICE_NAME" >&2 || true
}

wait_until_ready() {
    if ! command -v curl >/dev/null 2>&1; then
        log_warn "curl was not found; skipping the readiness check."
        return 0
    fi

    local elapsed=0
    while ((elapsed < START_TIMEOUT)); do
        if curl --fail --silent --max-time 2 "$READY_URL" >/dev/null; then
            return 0
        fi

        if ! is_running; then
            log_error "Notifier stopped before becoming ready."
            show_recent_logs
            return 1
        fi

        sleep 1
        ((elapsed += 1))
    done

    log_error "Notifier did not become ready within ${START_TIMEOUT}s."
    log_error "Readiness endpoint: $READY_URL"
    show_recent_logs
    return 1
}

start_bot() {
    require_config

    if is_running; then
        log_warn "Notifier is already running."
    else
        log_info "Building and starting Notifier..."
        compose up --detach --build "$SERVICE_NAME"
    fi

    log_info "Waiting for the Matrix connection and HTTP API..."
    wait_until_ready
    log_info "Notifier is ready at $READY_URL"
}

stop_bot() {
    if is_running; then
        log_info "Stopping Notifier..."
    else
        log_warn "Notifier is not running; cleaning up any stopped containers."
    fi

    compose down
    log_info "Notifier stopped. The persistent Docker volume was preserved."
}

restart_bot() {
    stop_bot
    start_bot
}

status_bot() {
    if ! is_running; then
        log_warn "Notifier is not running."
        compose ps --all "$SERVICE_NAME"
        return 1
    fi

    compose ps "$SERVICE_NAME"
    if command -v curl >/dev/null 2>&1 && curl --fail --silent --max-time 2 "$READY_URL" >/dev/null; then
        log_info "Notifier is running and ready."
    else
        log_warn "Notifier is running but its readiness endpoint is unavailable: $READY_URL"
    fi
}

show_logs() {
    if ! is_running; then
        log_warn "Notifier is not running. Showing logs from the latest container, if available."
    fi
    compose logs --follow --tail="$LOG_TAIL" "$SERVICE_NAME"
}

usage() {
    printf '%s\n' \
        "Usage: $0 {start|stop|restart|status|logs}" \
        "" \
        "Commands:" \
        "  start    Build and start Notifier in the background" \
        "  stop     Stop Notifier without deleting its persistent volume" \
        "  restart  Recreate and restart Notifier" \
        "  status   Show the container and readiness status" \
        "  logs     Follow Notifier logs" \
        "" \
        "Optional environment variables:" \
        "  NOTIFIER_READY_URL       Readiness URL (default: http://127.0.0.1:8085/readyz)" \
        "  NOTIFIER_START_TIMEOUT   Startup timeout in seconds (default: 60)" \
        "  NOTIFIER_LOG_TAIL        Initial number of log lines (default: 100)" \
        "  NO_COLOR                 Disable colored output"
}

main() {
    case "${1:-}" in
        -h | --help | help)
            usage
            return 0
            ;;
        start | stop | restart | status | logs)
            resolve_compose
            validate_settings
            ;;
        *)
            usage >&2
            return 2
            ;;
    esac

    case "$1" in
        start)
            start_bot
            ;;
        stop)
            stop_bot
            ;;
        restart)
            restart_bot
            ;;
        status)
            status_bot
            ;;
        logs)
            show_logs
            ;;
    esac
}

main "$@"
