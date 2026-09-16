#!/usr/bin/env bash
# Script to start/stop the Tchap notification bot

set -euo pipefail

# Configuration
PID_FILE="/tmp/tchap-notifier.pid"
LOG_FILE="/tmp/tchap-notifier.log"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $*"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $*"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $*"
}

is_running() {
    if [[ -f "$PID_FILE" ]]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

start_bot() {
    if is_running; then
        log_warn "Bot is already running (PID: $(cat "$PID_FILE"))"
        return 0
    fi

    log_info "Starting Tchap notification bot..."
    cd "$PROJECT_DIR"

    # Check if config.toml exists
    if [[ ! -f "config.toml" ]]; then
        log_error "config.toml not found. Please copy config.example.toml to config.toml and configure it."
        exit 1
    fi

    # Start the bot in background
    nohup poetry run notifier >> "$LOG_FILE" 2>&1 &
    local pid=$!

    # Save PID
    echo "$pid" > "$PID_FILE"

    # Wait a bit and check if process is still alive
    sleep 2
    if kill -0 "$pid" 2>/dev/null; then
        log_info "Bot started successfully (PID: $pid)"
        log_info "Logs: $LOG_FILE"
    else
        log_error "Bot failed to start. Check logs: $LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
}

stop_bot() {
    if ! is_running; then
        log_warn "Bot is not running"
        return 0
    fi

    local pid
    pid=$(cat "$PID_FILE")
    log_info "Stopping bot (PID: $pid)..."

    # Send SIGTERM for graceful shutdown
    kill "$pid" 2>/dev/null

    # Wait for process to stop
    local count=0
    while kill -0 "$pid" 2>/dev/null && [[ $count -lt 30 ]]; do
        sleep 1
        ((count++))
    done

    if kill -0 "$pid" 2>/dev/null; then
        log_warn "Bot didn't stop gracefully, forcing kill..."
        kill -9 "$pid" 2>/dev/null
        sleep 1
    fi

    rm -f "$PID_FILE"
    log_info "Bot stopped"
}

status_bot() {
    if is_running; then
        log_info "Bot is running (PID: $(cat "$PID_FILE"))"
        return 0
    else
        log_info "Bot is not running"
        return 1
    fi
}

show_logs() {
    if [[ -f "$LOG_FILE" ]]; then
        tail -f "$LOG_FILE"
    else
        log_warn "Log file not found: $LOG_FILE"
    fi
}

usage() {
    cat <<EOF
Usage: $0 {start|stop|restart|status|logs}

Commands:
  start    Start the bot in background
  stop     Stop the bot
  restart  Restart the bot
  status   Show bot status
  logs     Follow bot logs

Examples:
  $0 start
  $0 stop
  $0 restart
  $0 status
  $0 logs
EOF
}

main() {
    case "${1:-}" in
        start)
            start_bot
            ;;
        stop)
            stop_bot
            ;;
        restart)
            stop_bot
            start_bot
            ;;
        status)
            status_bot
            ;;
        logs)
            show_logs
            ;;
        *)
            usage
            exit 1
            ;;
    esac
}

main "$@"