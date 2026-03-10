#!/usr/bin/env bash
# agent-watch.sh — Real-time viewer for repo-watch Goose activity.
#
# When run without arguments, watches for agent activity continuously.
# If no task is running, waits until one starts. After a task finishes,
# waits for the next one.
#
# When a log file is given, replays that specific session and exits.
#
# Usage:
#   ./agent-watch.sh           # watch live (waits if idle)
#   ./agent-watch.sh <file>    # replay a specific log file

set -euo pipefail

LOG_DIR="${HOME}/.repo-watch-logs"
CURRENT_LOG_LINK="${LOG_DIR}/current.log"

one_shot=false
if [[ -n "${1:-}" ]]; then
    if [[ ! -f "$1" ]]; then
        echo "Log file not found: $1"
        exit 1
    fi
    one_shot=true
fi

# Terminal setup
clear
rows=$(tput lines)
cleanup() {
    printf '\033[r'
    clear
    tput cnorm 2>/dev/null || true
}
trap cleanup EXIT

tput civis
printf '\033[1;%dr' $((rows - 1))
printf '\033[1;1H'

start_epoch=$(date +%s)

print_status() {
    local now elapsed mins secs
    now=$(date +%s)
    elapsed=$(( now - start_epoch ))
    mins=$(( elapsed / 60 ))
    secs=$(( elapsed % 60 ))

    printf '\0337'
    printf '\033[%d;1H\033[K\033[30;47m %02d:%02d | Goose running \033[0m' \
        "$rows" "$mins" "$secs"
    printf '\0338'
}

print_idle() {
    printf '\0337'
    printf '\033[%d;1H\033[K\033[30;47m Idle \033[0m' "$rows"
    printf '\0338'
}

# Process a single log file until it stops growing or the symlink changes.
watch_log() {
    local log_file="$1"
    start_epoch=$(stat -c %Y "$log_file" 2>/dev/null || stat -f %m "$log_file" 2>/dev/null)

    echo "=== agent-watch: $(basename "$log_file") ==="
    echo ""
    print_status

    # Goose outputs plain text — just tail it with periodic status updates
    tail -n +1 -f "$log_file" &
    local tail_pid=$!

    # In one-shot mode, wait for tail to finish naturally (e.g. piped file)
    if [[ "$one_shot" == "true" ]]; then
        wait "$tail_pid" 2>/dev/null || true
        echo ""
        echo "=== Done ==="
        return 0
    fi

    # In continuous mode, watch until the symlink changes or disappears
    while true; do
        sleep 2
        print_status

        # Check if the current log link still points to our file
        if [[ ! -L "$CURRENT_LOG_LINK" ]]; then
            # Log link removed — task finished
            break
        fi

        local current_target
        current_target=$(readlink -f "$CURRENT_LOG_LINK" 2>/dev/null || true)
        if [[ "$current_target" != "$log_file" ]]; then
            # Symlink changed to a new file — task finished
            break
        fi
    done

    kill "$tail_pid" 2>/dev/null || true
    wait "$tail_pid" 2>/dev/null || true

    echo ""
    echo "=== Done ==="
    print_status
}

# One-shot mode: replay a specific file and exit
if [[ "$one_shot" == "true" ]]; then
    watch_log "$1"
    exit 0
fi

# Continuous mode: watch sessions as they appear
while true; do
    if [[ ! -L "$CURRENT_LOG_LINK" ]]; then
        echo "Waiting for agent activity..."
        print_idle
        while [[ ! -L "$CURRENT_LOG_LINK" ]]; do
            sleep 1
        done
    fi

    log_file=$(readlink -f "$CURRENT_LOG_LINK")
    watch_log "$log_file"

    # Wait for the symlink to disappear (task cleanup) before looking for the next one
    while [[ -L "$CURRENT_LOG_LINK" ]] && [[ "$(readlink -f "$CURRENT_LOG_LINK")" == "$log_file" ]]; do
        sleep 1
    done
done
