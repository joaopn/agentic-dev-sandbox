#!/usr/bin/env bash
# agent-watch.sh — Real-time viewer for repo-watch agent activity.
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
CURRENT_LOG_LINK="${LOG_DIR}/current.jsonl"

one_shot=false
if [[ -n "${1:-}" ]]; then
    if [[ ! -f "$1" ]]; then
        echo "Log file not found: $1"
        exit 1
    fi
    one_shot=true
fi

# Stats
total_in=0
total_out=0
total_cached=0
cost="N/A"
turns=0
start_epoch=$(date +%s)

# Terminal setup
clear
rows=$(tput lines)
cleanup() {
    printf '\033[%d;1H\n' "$rows"
    printf '\033[r'
    tput cnorm 2>/dev/null || true
}
trap cleanup EXIT

tput civis
printf '\033[1;%dr' $((rows - 1))
printf '\033[1;1H'

print_status() {
    local now elapsed mins secs
    now=$(date +%s)
    elapsed=$(( now - start_epoch ))
    mins=$(( elapsed / 60 ))
    secs=$(( elapsed % 60 ))

    local in_str="N/A" out_str="N/A" cached_str="N/A"
    [[ "$total_in" -gt 0 ]] && in_str="$total_in"
    [[ "$total_out" -gt 0 ]] && out_str="$total_out"
    [[ "$total_cached" -gt 0 ]] && cached_str="$total_cached"

    printf '\0337'
    printf '\033[%d;1H\033[K\033[30;47m %02d:%02d | In: %s | Out: %s | Cache: %s | Cost: $%s | Turns: %d \033[0m' \
        "$rows" "$mins" "$secs" "$in_str" "$out_str" "$cached_str" "$cost" "$turns"
    printf '\0338'
}

print_idle() {
    printf '\0337'
    printf '\033[%d;1H\033[K\033[30;47m Idle \033[0m' "$rows"
    printf '\0338'
}

reset_stats() {
    total_in=0
    total_out=0
    total_cached=0
    cost="N/A"
    turns=0
}

# Process a single log file until the result event.
watch_log() {
    local log_file="$1"
    start_epoch=$(stat -c %Y "$log_file" 2>/dev/null || stat -f %m "$log_file" 2>/dev/null)
    reset_stats

    echo "=== agent-watch: $(basename "$log_file") ==="
    echo ""
    print_status

    while IFS= read -r line; do
        [[ -z "$line" || "${line:0:1}" != "{" ]] && continue
        type=$(echo "$line" | jq -r '.type // empty' 2>/dev/null) || continue

        case "$type" in
            assistant)
                turns=$(( turns + 1 ))
                local_in=$(echo "$line" | jq -r '.message.usage.input_tokens // 0' 2>/dev/null) || local_in=0
                local_out=$(echo "$line" | jq -r '.message.usage.output_tokens // 0' 2>/dev/null) || local_out=0
                local_cached=$(echo "$line" | jq -r '.message.usage.cache_read_input_tokens // 0' 2>/dev/null) || local_cached=0
                total_in=$(( total_in + local_in ))
                total_out=$(( total_out + local_out ))
                total_cached=$(( total_cached + local_cached ))

                tools=$(echo "$line" | jq -r '.message.content[]? | select(.type == "tool_use") | .name' 2>/dev/null) || true
                if [[ -n "$tools" ]]; then
                    while IFS= read -r tool_name; do
                        echo "  [tool] ${tool_name}"
                    done <<< "$tools"
                fi

                text=$(echo "$line" | jq -r '.message.content[]? | select(.type == "text") | .text' 2>/dev/null) || true
                if [[ -n "$text" ]]; then
                    echo "$text"
                fi

                print_status
                ;;

            result)
                total_in=$(echo "$line" | jq -r '.usage.input_tokens // 0' 2>/dev/null) || true
                total_out=$(echo "$line" | jq -r '.usage.output_tokens // 0' 2>/dev/null) || true
                total_cached=$(echo "$line" | jq -r '.usage.cache_read_input_tokens // 0' 2>/dev/null) || true
                cost=$(echo "$line" | jq -r '.total_cost_usd // "N/A"' 2>/dev/null) || true
                duration=$(echo "$line" | jq -r '.duration_ms // 0' 2>/dev/null) || duration=0

                print_status
                echo ""
                echo "=== Done ==="
                printf '\033[30;47m Time: %ss | In: %s | Out: %s | Cache: %s | Cost: $%s \033[0m\n' \
                    "$(( duration / 1000 ))" "$total_in" "$total_out" "$total_cached" "$cost"
                echo ""
                return 0
                ;;
        esac
    done < <(tail -n +1 -f "$log_file")
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
