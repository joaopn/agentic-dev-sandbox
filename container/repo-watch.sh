#!/usr/bin/env bash
# repo-watch.sh — Polls Gitea issues/PRs and invokes Claude Code to handle them.
#
# Watches for open issues and PRs assigned to this agent on its Gitea fork.
# When a human posts, comments, or leaves a review, invokes Claude Code with
# the full conversation context. Claude decides what to do: respond, code,
# open a PR, merge, etc.
#
# Usage:
#   ./repo-watch.sh              # poll every 30s (default)
#   POLL_INTERVAL=60 ./repo-watch.sh   # poll every 60s
#
# Safety limits (env vars, all optional):
#   REPO_WATCH_MAX_TURNS     — max agentic iterations per invocation
#   REPO_WATCH_MAX_BUDGET_USD — cost ceiling per invocation
#   REPO_WATCH_TIMEOUT       — wall-clock limit (e.g. "10m", "1h")
#
# Prerequisites:
#   - Claude Code installed and authenticated (`claude` must work)
#   - Environment variables set by entrypoint: GITEA_URL, GITEA_TOKEN,
#     GITEA_USER, REPO_NAME
#
# This script blocks the terminal. Use byobu F2 to open a new window.
# Run ./agent-watch.sh in another window for real-time agent activity.

set -euo pipefail

POLL_INTERVAL="${POLL_INTERVAL:-30}"
MAX_RETRIES="${MAX_RETRIES:-3}"
REPO_DIR="${HOME}/${REPO_NAME}"
API="${GITEA_URL}/api/v1"
REPO_PATH="${GITEA_USER}/${REPO_NAME}"
PROMPT_FILE="${HOME}/repo-watch-prompt.md"
FAIL_DIR="${HOME}/.repo-watch-failures"
LOG_DIR="${HOME}/.repo-watch-logs"
CURRENT_LOG_LINK="${LOG_DIR}/current.jsonl"

# Safety limits (empty = unlimited)
REPO_WATCH_MAX_TURNS="${REPO_WATCH_MAX_TURNS:-}"
REPO_WATCH_MAX_BUDGET_USD="${REPO_WATCH_MAX_BUDGET_USD:-}"
REPO_WATCH_TIMEOUT="${REPO_WATCH_TIMEOUT:-}"

# Last log file path (set by invoke_agent, used for post-processing)
LAST_LOG_FILE=""

# ── Helpers ──────────────────────────────────────────────────────────────────

gitea() {
    local method="$1" path="$2"
    shift 2
    local data="${1:-}"
    local args=(-s -H "Authorization: token ${GITEA_TOKEN}" -H "Content-Type: application/json" -X "$method")
    [[ -n "$data" ]] && args+=(-d "$data")
    curl "${args[@]}" "${API}${path}"
}

log() {
    echo "[$(date '+%H:%M:%S')] $*"
}

# Build prompt and invoke claude for a given issue/PR.
# Args: $1=number $2=title $3=body $4=author $5=labels $6=type ("issue" or "pr")
#       $7=formatted_comments $8=open_prs $9=extra_context
invoke_agent() {
    local number="$1" title="$2" body="$3" author="$4" labels="$5" item_type="$6"
    local formatted_comments="$7" open_prs="$8" extra_context="${9:-}"

    local git_branch
    git_branch=$(git -C "$REPO_DIR" branch --show-current 2>/dev/null || echo "detached")

    local task_file
    task_file=$(mktemp)

    cat "$PROMPT_FILE" > "$task_file"
    cat >> "$task_file" << CONTEXT

---

## Current task

### ${item_type} #${number}: ${title}

**Author:** ${author}
**Labels:** ${labels}

${body}

### Comments

${formatted_comments:-_No comments yet._}
${extra_context}
### Related pull requests

${open_prs:-_None._}

### Git state

- Current branch: ${git_branch}
- Repo directory: ${REPO_DIR}
CONTEXT

    local log_file="${LOG_DIR}/${item_type,,}-${number}-$(date '+%Y%m%d-%H%M%S').jsonl"
    LAST_LOG_FILE="$log_file"
    ln -sfn "$log_file" "$CURRENT_LOG_LINK"

    log "invoking claude for ${item_type} #${number}..."
    log "log: ${log_file}"

    # Build claude args
    local claude_args=(-p "$(cat "$task_file")" --output-format stream-json --verbose)
    [[ -n "$REPO_WATCH_MAX_TURNS" ]] && claude_args+=(--max-turns "$REPO_WATCH_MAX_TURNS")
    [[ -n "$REPO_WATCH_MAX_BUDGET_USD" ]] && claude_args+=(--max-budget-usd "$REPO_WATCH_MAX_BUDGET_USD")

    local rc=0
    if [[ -n "$REPO_WATCH_TIMEOUT" ]]; then
        (cd "$REPO_DIR" && timeout --signal=TERM "$REPO_WATCH_TIMEOUT" claude "${claude_args[@]}") \
            > "$log_file" 2>&1 || rc=$?
    else
        (cd "$REPO_DIR" && claude "${claude_args[@]}") > "$log_file" 2>&1 || rc=$?
    fi

    rm -f "$task_file" "$CURRENT_LOG_LINK"

    # Parse result + stats from the JSONL log
    if [[ -f "$log_file" ]]; then
        jq -r 'select(.type == "result") |
            .result,
            "",
            "\u001b[30;47m Time: \(.duration_ms/1000)s | In: \(.usage.input_tokens // "N/A") | Out: \(.usage.output_tokens // "N/A") | Cached: \(.usage.cache_read_input_tokens // "N/A") | Cost: $\(.total_cost_usd // "N/A") \u001b[0m"
        ' "$log_file" 2>/dev/null | tail -4
    fi

    if [[ "$rc" -eq 124 ]]; then
        log "claude timed out after ${REPO_WATCH_TIMEOUT} for ${item_type} #${number}"
        return 0  # timeout is deliberate, not a retry-worthy failure
    elif [[ "$rc" -ne 0 ]]; then
        log "claude exited with error (code ${rc}) for ${item_type} #${number}"
        # Track failure count
        mkdir -p "$FAIL_DIR"
        local fail_file="${FAIL_DIR}/${number}"
        local fail_count=0
        [[ -f "$fail_file" ]] && fail_count=$(cat "$fail_file")
        echo $((fail_count + 1)) > "$fail_file"
        return 1
    else
        # Clear failure count on success
        rm -f "${FAIL_DIR}/${number}" 2>/dev/null
        return 0
    fi
}

# Check if an item has exceeded retry limit.
# Resets when a new human comment appears (tracked by comment count in fail file).
check_retries() {
    local number="$1" current_comments="$2"
    local fail_file="${FAIL_DIR}/${number}"
    [[ ! -f "$fail_file" ]] && return 0  # no failures, proceed
    local fail_count
    fail_count=$(cat "$fail_file")
    if [[ "$fail_count" -ge "$MAX_RETRIES" ]]; then
        return 1  # skip, too many failures
    fi
    return 0
}

# Format issue/PR comments into readable text.
format_comments() {
    local comments_json="$1"
    local num_comments
    num_comments=$(echo "$comments_json" | jq 'length')
    local formatted=""

    for j in $(seq 0 $((num_comments - 1))); do
        local c_author c_body c_date
        c_author=$(echo "$comments_json" | jq -r ".[$j].user.login")
        c_body=$(echo "$comments_json" | jq -r ".[$j].body")
        c_date=$(echo "$comments_json" | jq -r ".[$j].created_at")
        formatted+="**${c_author}** (${c_date}):
${c_body}

---
"
    done

    echo "$formatted"
}

# Parse a JSONL log into a readable markdown file.
# Extracts thinking (blockquoted), text responses, tool indicators, and summary.
# Args: $1=log_file  Prints: path to the parsed file
parse_log() {
    local log_file="$1"
    local basename
    basename=$(basename "$log_file" .jsonl)
    local parsed_file="$(dirname "$log_file")/agent_log_${basename}.md"
    > "$parsed_file"

    while IFS= read -r line; do
        [[ -z "$line" || "${line:0:1}" != "{" ]] && continue
        type=$(echo "$line" | jq -r '.type // empty' 2>/dev/null) || continue

        case "$type" in
            assistant)
                # Thinking blocks (blockquoted)
                thinking=$(echo "$line" | jq -r '.message.content[]? | select(.type == "thinking") | .thinking' 2>/dev/null) || true
                if [[ -n "$thinking" ]]; then
                    echo "$thinking" | sed 's/^/> /' >> "$parsed_file"
                    echo "" >> "$parsed_file"
                fi
                # Tool name indicators
                tools=$(echo "$line" | jq -r '.message.content[]? | select(.type == "tool_use") | .name' 2>/dev/null) || true
                if [[ -n "$tools" ]]; then
                    while IFS= read -r tool_name; do
                        echo "  [tool] ${tool_name}" >> "$parsed_file"
                    done <<< "$tools"
                fi
                # Text responses
                text=$(echo "$line" | jq -r '.message.content[]? | select(.type == "text") | .text' 2>/dev/null) || true
                if [[ -n "$text" ]]; then
                    echo "$text" >> "$parsed_file"
                fi
                ;;
            result)
                local duration total_in total_out total_cached cost
                duration=$(echo "$line" | jq -r '.duration_ms // 0' 2>/dev/null) || duration=0
                total_in=$(echo "$line" | jq -r '.usage.input_tokens // 0' 2>/dev/null) || true
                total_out=$(echo "$line" | jq -r '.usage.output_tokens // 0' 2>/dev/null) || true
                total_cached=$(echo "$line" | jq -r '.usage.cache_read_input_tokens // 0' 2>/dev/null) || true
                cost=$(echo "$line" | jq -r '.total_cost_usd // "N/A"' 2>/dev/null) || true
                # Clean up float precision (e.g. 0.15171925000000003 → 0.151719)
                if [[ "$cost" != "N/A" ]]; then
                    cost=$(printf '%.6f' "$cost")
                fi
                echo "" >> "$parsed_file"
                echo "---" >> "$parsed_file"
                printf 'Time: %ss | In: %s | Out: %s | Cache: %s | Cost: $%s\n' \
                    "$(( duration / 1000 ))" "$total_in" "$total_out" "$total_cached" "$cost" >> "$parsed_file"
                ;;
        esac
    done < "$log_file"

    echo "$parsed_file"
}

# Attach the parsed log to the agent's last comment on an issue/PR.
# Uploads the file as an attachment, then patches the comment body with a
# relative markdown link so the download URL works regardless of ROOT_URL.
# Args: $1=issue_number $2=file_path
attach_log_to_last_comment() {
    local number="$1" parsed_file="$2"
    [[ ! -f "$parsed_file" || ! -s "$parsed_file" ]] && return 0

    # Find the agent's last comment
    local last_comment comment_id
    last_comment=$(gitea GET "/repos/${REPO_PATH}/issues/${number}/comments" \
        | jq "[.[] | select(.user.login == \"${GITEA_USER}\")] | last")
    comment_id=$(echo "$last_comment" | jq -r '.id // empty')

    if [[ -z "$comment_id" ]]; then
        log "no agent comment found for #${number}, skipping log attachment"
        return 0
    fi

    # Upload the attachment and extract the UUID
    local attach_response attach_uuid attach_name
    attach_response=$(curl -s -X POST \
        -H "Authorization: token ${GITEA_TOKEN}" \
        -F "attachment=@${parsed_file}" \
        "${API}/repos/${REPO_PATH}/issues/comments/${comment_id}/assets")
    attach_uuid=$(echo "$attach_response" | jq -r '.uuid // empty')
    attach_name=$(echo "$attach_response" | jq -r '.name // empty')

    if [[ -z "$attach_uuid" ]]; then
        log "failed to upload attachment for #${number}"
        return 0
    fi

    # Patch the comment to add a relative markdown link (bypasses broken ROOT_URL)
    # Keep body in JSON-land the entire time to avoid shell mangling of newlines
    local updated_body
    updated_body=$(echo "$last_comment" | jq \
        --arg name "$attach_name" \
        --arg uuid "$attach_uuid" \
        '{"body": (.body + "\n\n[" + $name + "](/attachments/" + $uuid + ")")}')

    gitea PATCH "/repos/${REPO_PATH}/issues/comments/${comment_id}" "$updated_body" >/dev/null

    log "attached activity log to comment #${comment_id} on #${number}"
}

# ── Preflight checks ────────────────────────────────────────────────────────

for var in GITEA_URL GITEA_TOKEN GITEA_USER REPO_NAME; do
    if [[ -z "${!var:-}" ]]; then
        echo "Error: ${var} is not set. Are you inside an agent container?" >&2
        exit 1
    fi
done

if ! command -v claude &>/dev/null; then
    echo "Error: claude is not installed. Run 'claude' first to install and authenticate." >&2
    exit 1
fi

if [[ ! -f "$PROMPT_FILE" ]]; then
    echo "Error: ${PROMPT_FILE} not found." >&2
    exit 1
fi

if [[ ! -d "${REPO_DIR}/.git" ]]; then
    echo "Error: ${REPO_DIR} is not a git repository." >&2
    exit 1
fi

# ── Bootstrap labels (idempotent) ────────────────────────────────────────────

existing_labels=$(gitea GET "/repos/${REPO_PATH}/labels" | jq -r '.[].name')

for entry in "in-progress:#0075ca" "needs-review:#e4e669" "done:#0e8a16"; do
    name="${entry%%:*}"
    color="${entry#*:}"
    if ! echo "$existing_labels" | grep -qx "$name"; then
        gitea POST "/repos/${REPO_PATH}/labels" "{\"name\":\"${name}\",\"color\":\"${color}\"}" >/dev/null
        log "created label '${name}'"
    fi
done

# ── Main loop ────────────────────────────────────────────────────────────────

mkdir -p "$FAIL_DIR" "$LOG_DIR"

log "repo-watch: monitoring ${REPO_PATH} (poll every ${POLL_INTERVAL}s)"
log "repo-watch: logs → ${LOG_DIR}"
[[ -n "$REPO_WATCH_MAX_TURNS" ]] && log "repo-watch: max turns: ${REPO_WATCH_MAX_TURNS}"
[[ -n "$REPO_WATCH_MAX_BUDGET_USD" ]] && log "repo-watch: max budget: \$${REPO_WATCH_MAX_BUDGET_USD}"
[[ -n "$REPO_WATCH_TIMEOUT" ]] && log "repo-watch: timeout: ${REPO_WATCH_TIMEOUT}"
log "repo-watch: press Ctrl+C to stop. Run ./agent-watch.sh for live status."

was_idle=false

while true; do
    handled=false

    # ── Pass 1: Issues and PR conversations ──────────────────────────────
    # No type filter — returns both issues and PRs. Triggers on who-spoke-last.

    items=$(gitea GET "/repos/${REPO_PATH}/issues?state=open&assignee=${GITEA_USER}&sort=oldest")
    count=$(echo "$items" | jq 'length')

    for i in $(seq 0 $((count - 1))); do
        number=$(echo "$items" | jq -r ".[$i].number")
        title=$(echo "$items" | jq -r ".[$i].title")
        body=$(echo "$items" | jq -r ".[$i].body")
        author=$(echo "$items" | jq -r ".[$i].user.login")
        labels=$(echo "$items" | jq -r "[.[$i].labels[].name] | join(\", \")")
        is_pr=$(echo "$items" | jq -r ".[$i].pull_request // empty")

        local_type="Issue"
        [[ -n "$is_pr" ]] && local_type="PR"

        # Fetch comments
        comments_json=$(gitea GET "/repos/${REPO_PATH}/issues/${number}/comments")
        num_comments=$(echo "$comments_json" | jq 'length')

        # Determine who spoke last (skip bot commands and automated responses)
        if [[ "$num_comments" -eq 0 ]]; then
            last_author="$author"
        else
            last_author=$(echo "$comments_json" | jq -r '
                [.[] | select(
                    (.body | test("<!-- automated-security-review -->") | not)
                    and (.body | test("^/") | not)
                )]
                | if length > 0 then .[-1].user.login else empty end')
            [[ -z "$last_author" ]] && last_author="$author"
        fi

        # Skip if agent spoke last (waiting for human)
        if [[ "$last_author" == "$GITEA_USER" ]]; then
            continue
        fi

        # Skip if too many consecutive failures
        if ! check_retries "$number" "$num_comments"; then
            continue
        fi

        log "${local_type} #${number} needs attention: ${title}"

        formatted_comments=$(format_comments "$comments_json")

        # Fetch only PRs related to this issue (body mentions #N)
        related_prs=$(gitea GET "/repos/${REPO_PATH}/pulls?state=open" \
            | jq -r --arg issue "#${number}" \
            '.[] | select(.body | test($issue + "\\b")) | "- #\(.number) \(.title) (branch: \(.head.ref))"')

        invoke_agent "$number" "$title" "$body" "$author" "$labels" "$local_type" \
            "$formatted_comments" "$related_prs" || true

        # Parse and attach activity log
        if [[ -n "$LAST_LOG_FILE" && -f "$LAST_LOG_FILE" ]]; then
            parsed_file=$(parse_log "$LAST_LOG_FILE")
            attach_log_to_last_comment "$number" "$parsed_file"
        fi

        log "done with ${local_type} #${number}"
        handled=true
        break  # one item per cycle
    done

    # ── Pass 2: PR reviews (line-level feedback) ─────────────────────────
    # Only runs if Pass 1 didn't handle anything.
    # Checks open PRs by the agent for reviews submitted after the agent's
    # last conversation comment.

    if [[ "$handled" == "false" ]]; then
        prs=$(gitea GET "/repos/${REPO_PATH}/pulls?state=open")
        pr_count=$(echo "$prs" | jq 'length')

        for i in $(seq 0 $((pr_count - 1))); do
            pr_number=$(echo "$prs" | jq -r ".[$i].number")
            pr_title=$(echo "$prs" | jq -r ".[$i].title")
            pr_body=$(echo "$prs" | jq -r ".[$i].body")
            pr_author=$(echo "$prs" | jq -r ".[$i].user.login")
            pr_labels=$(echo "$prs" | jq -r "[.[$i].labels[].name] | join(\", \")")

            # Only check PRs authored by this agent
            if [[ "$pr_author" != "$GITEA_USER" ]]; then
                continue
            fi

            # Skip if too many consecutive failures
            if ! check_retries "$pr_number" "0"; then
                continue
            fi

            # Get the agent's last conversation comment timestamp
            comments_json=$(gitea GET "/repos/${REPO_PATH}/issues/${pr_number}/comments")
            agent_last_comment=$(echo "$comments_json" | jq -r \
                "[.[] | select(.user.login == \"${GITEA_USER}\")] | last | .created_at // empty")

            # Fetch reviews from non-agent users
            reviews=$(gitea GET "/repos/${REPO_PATH}/pulls/${pr_number}/reviews")
            new_reviews="false"

            if [[ -n "$agent_last_comment" ]]; then
                # Check if any non-agent review is newer than agent's last comment
                new_reviews=$(echo "$reviews" | jq -r \
                    --arg cutoff "$agent_last_comment" \
                    --arg agent "$GITEA_USER" \
                    '[.[] | select(.user.login != $agent and .submitted_at > $cutoff)] | length > 0')
            else
                # Agent never commented — any non-agent review is new
                new_reviews=$(echo "$reviews" | jq -r \
                    --arg agent "$GITEA_USER" \
                    '[.[] | select(.user.login != $agent)] | length > 0')
            fi

            if [[ "$new_reviews" != "true" ]]; then
                continue
            fi

            log "PR #${pr_number} has new reviews: ${pr_title}"

            formatted_comments=$(format_comments "$comments_json")

            # Format review comments as extra context
            review_context=$(echo "$reviews" | jq -r \
                --arg agent "$GITEA_USER" \
                '.[] | select(.user.login != $agent) | "**Review by \(.user.login)** (\(.submitted_at)) — \(.state):\n\(.body // "_No summary._")\n---"')

            related_prs=$(gitea GET "/repos/${REPO_PATH}/pulls?state=open" \
                | jq -r --arg issue "#${pr_number}" \
                '.[] | select(.body | test($issue + "\\b")) | "- #\(.number) \(.title) (branch: \(.head.ref))"')

            extra="### PR reviews

${review_context}

"

            invoke_agent "$pr_number" "$pr_title" "$pr_body" "$pr_author" "$pr_labels" "PR" \
                "$formatted_comments" "$related_prs" "$extra" || true

            # Parse and attach activity log
            if [[ -n "$LAST_LOG_FILE" && -f "$LAST_LOG_FILE" ]]; then
                parsed_file=$(parse_log "$LAST_LOG_FILE")
                attach_log_to_last_comment "$pr_number" "$parsed_file"
            fi

            log "done with PR #${pr_number}"
            handled=true
            break  # one item per cycle
        done
    fi

    if [[ "$handled" == "false" ]]; then
        if [[ "$was_idle" == "false" ]]; then
            log "no pending items, monitoring every ${POLL_INTERVAL}s"
            was_idle=true
        fi
    else
        was_idle=false
    fi

    sleep "$POLL_INTERVAL"
done
