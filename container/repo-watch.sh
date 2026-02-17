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
# Prerequisites:
#   - Claude Code installed and authenticated (`claude` must work)
#   - Environment variables set by entrypoint: GITEA_URL, GITEA_TOKEN,
#     GITEA_USER, REPO_NAME
#
# This script blocks the terminal. Use byobu F2 to open a new window.

set -euo pipefail

POLL_INTERVAL="${POLL_INTERVAL:-30}"
MAX_RETRIES="${MAX_RETRIES:-3}"
REPO_DIR="${HOME}/${REPO_NAME}"
API="${GITEA_URL}/api/v1"
REPO_PATH="${GITEA_USER}/${REPO_NAME}"
PROMPT_FILE="${HOME}/repo-watch-prompt.md"
FAIL_DIR="${HOME}/.repo-watch-failures"

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

    log "invoking claude for ${item_type} #${number}..."
    local rc=0
    local claude_output
    claude_output=$( (cd "$REPO_DIR" && claude -p "$(cat "$task_file")" --output-format json) ) || rc=$?

    rm -f "$task_file"

    # Parse and display result + stats
    if [[ -n "$claude_output" ]]; then
        echo "$claude_output" | jq -r '
            .result,
            "",
            "\u001b[30;47m Time: \(.duration_ms/1000)s | In: \(.usage.input_tokens) | Out: \(.usage.output_tokens) | Cached: \(.usage.cache_read_input_tokens) | Cost: $\(.total_cost_usd) \u001b[0m"
        ' 2>/dev/null || echo "$claude_output"
    fi

    if [[ "$rc" -ne 0 ]]; then
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

mkdir -p "$FAIL_DIR"

log "repo-watch: monitoring ${REPO_PATH} (poll every ${POLL_INTERVAL}s)"
log "repo-watch: press Ctrl+C to stop"

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

        # Determine who spoke last
        if [[ "$num_comments" -eq 0 ]]; then
            last_author="$author"
        else
            last_author=$(echo "$comments_json" | jq -r '.[-1].user.login')
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
