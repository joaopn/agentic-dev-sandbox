#!/usr/bin/env bash
# barrier-check.sh — Passive security posture checker for agentic-dev-sandbox.
# Safe to run anywhere (host, container, VM). All checks are read-only.
#
# Container-specific checks (Deepce, LinPEAS, Gitea, PID limits) run
# automatically when a container is detected (/.dockerenv).
#
# Usage:
#   bash barrier-check.sh                  # run all checks (auto-detects container)
#   bash barrier-check.sh --no-color       # disable colored output
#   bash barrier-check.sh --help           # show usage

set -uo pipefail

# ── Constants ────────────────────────────────────────────────────────────────
VERSION="0.1.0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULTS_DIR="$SCRIPT_DIR/barrier-check-results"
TOOLS_DIR="$RESULTS_DIR/.tools"

# Pinned versions of external tools (for container checks)
LINPEAS_URL="https://github.com/peass-ng/PEASS-ng/releases/download/20260212-43b28429/linpeas.sh"
LINPEAS_SHA256="e001347d8238c1d8e8ff9ec601fea0074ecedc4260c4859d0aa57e919429135f"

DEEPCE_URL="https://raw.githubusercontent.com/stealthcopter/deepce/420b1d1ddb636f6bd277a105f580cd09b03517cc/deepce.sh"
DEEPCE_SHA256="2c8301c3e8d12c01f6ad39e9513f5a7011ccaaee7eee1f40951a38b756d3bf98"

# Capabilities bitmask positions
declare -A DANGEROUS_CAPS=(
    [SYS_ADMIN]=21
    [NET_ADMIN]=12
    [SYS_PTRACE]=19
    [SYS_MODULE]=16
    [SYS_RAWIO]=17
    [DAC_READ_SEARCH]=2
)

# Expected capabilities (for reference -- the script checks dangerous caps are absent)
# CHOWN=0 DAC_OVERRIDE=1 FOWNER=3 FSETID=4 KILL=5 SETGID=6 SETUID=7 NET_RAW=13 AUDIT_WRITE=29

# ── Color ────────────────────────────────────────────────────────────────────
USE_COLOR=true

setup_colors() {
    if $USE_COLOR && [ -t 1 ]; then
        RED=$'\033[0;31m'
        GREEN=$'\033[0;32m'
        YELLOW=$'\033[0;33m'
        CYAN=$'\033[0;36m'
        BOLD=$'\033[1m'
        DIM=$'\033[2m'
        RESET=$'\033[0m'
    else
        RED='' GREEN='' YELLOW='' CYAN='' BOLD='' DIM='' RESET=''
    fi
}

# ── Output helpers ───────────────────────────────────────────────────────────
pass()  { printf '  %-38s %s[PASS]%s  %s\n' "$1" "$GREEN" "$RESET" "$2"; }
fail()  { printf '  %-38s %s[FAIL]%s  %s\n' "$1" "$RED" "$RESET" "$2"; }
info()  { printf '  %-38s %s[INFO]%s  %s\n' "$1" "$CYAN" "$RESET" "$2"; }
skip()  { printf '  %-38s %s[SKIP]%s  %s\n' "$1" "$DIM" "$RESET" "$2"; }
warn()  { printf '  %-38s %s[WARN]%s  %s\n' "$1" "$YELLOW" "$RESET" "$2"; }
warn_msg() { printf '%s  Warning: %s%s\n' "$YELLOW" "$1" "$RESET"; }
hdr()   { printf '\n%s%s%s\n' "$BOLD" "$1" "$RESET"; }

# ── Argument parsing ─────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
    case "$1" in
        --no-color)   USE_COLOR=false ;;
        --help|-h)
            echo "Usage: bash barrier-check.sh [--no-color] [--help]"
            echo ""
            echo "Passive security posture checker. Safe to run anywhere."
            echo "Container-specific checks (Deepce, LinPEAS, Gitea, PID limits)"
            echo "run automatically when a container is detected."
            echo ""
            echo "  --no-color   Disable colored output"
            echo "  --help       Show this help"
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

setup_colors

# ── Root guard ────────────────────────────────────────────────────────────────
if [ "$(id -u)" -eq 0 ]; then
    printf '%sError: do not run this script as root.%s\n' "$RED" "$RESET"
    echo "Run as a regular user to test the actual security posture."
    exit 1
fi

# ── Setup ────────────────────────────────────────────────────────────────────
mkdir -p "$RESULTS_DIR" "$TOOLS_DIR"

# Container detection (standard Docker marker)
IN_CONTAINER=false
if [ -f /.dockerenv ]; then
    IN_CONTAINER=true
fi

# Detect egress mode (used later by network checks)
EGRESS_MODE="unknown"

# ── Banner ───────────────────────────────────────────────────────────────────
printf '%s' "$BOLD"
echo "════════════════════════════════════════════════════"
echo " SANDBOX BARRIER TEST  v${VERSION}"
printf ' Hostname: %s' "$(hostname)"
if $IN_CONTAINER; then
    printf ' | Container: detected'
else
    printf ' | Container: not detected'
fi
echo ""
printf ' Date: %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "════════════════════════════════════════════════════"
printf '%s\n' "$RESET"

# ── Logging ──────────────────────────────────────────────────────────────────
LOG="$RESULTS_DIR/barrier-check.log"
: > "$LOG"

log() { echo "[$(date -u '+%H:%M:%S')] $*" >> "$LOG"; }

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1: CUSTOM CHECKS
# ══════════════════════════════════════════════════════════════════════════════

# ── 1a. Capabilities & Isolation ─────────────────────────────────────────────
hdr "CAPABILITIES & ISOLATION"

# Decode capability bitmasks
cap_hex=""
cap_bnd_hex=""
if [ -r /proc/self/status ]; then
    cap_hex=$(grep -i '^CapEff:' /proc/self/status 2>/dev/null | awk '{print $2}')
    cap_bnd_hex=$(grep -i '^CapBnd:' /proc/self/status 2>/dev/null | awk '{print $2}')
fi

if [ -n "$cap_hex" ]; then
    cap_dec=$((16#$cap_hex))
    log "CapEff hex=$cap_hex dec=$cap_dec"

    # Check each dangerous capability
    for cap_name in "${!DANGEROUS_CAPS[@]}"; do
        bit=${DANGEROUS_CAPS[$cap_name]}
        if (( cap_dec & (1 << bit) )); then
            fail "No $cap_name" "PRESENT (bit $bit)"
            log "FAIL: $cap_name is present"
        else
            pass "No $cap_name" ""
            log "PASS: $cap_name absent"
        fi
    done
else
    skip "Capability check" "Cannot read /proc/self/status"
    log "SKIP: Cannot read CapEff"
fi

# Seccomp
seccomp_mode=""
if [ -r /proc/self/status ]; then
    seccomp_mode=$(grep -i '^Seccomp:' /proc/self/status 2>/dev/null | awk '{print $2}')
fi
case "$seccomp_mode" in
    2) pass "Seccomp filtering" "mode 2" ;;
    1) info "Seccomp strict" "mode 1" ;;
    0) fail "Seccomp filtering" "DISABLED (mode 0)" ;;
    *) skip "Seccomp filtering" "cannot determine" ;;
esac
log "Seccomp mode=$seccomp_mode"

# User namespace creation
if unshare --user echo test >/dev/null 2>&1; then
    fail "User namespace blocked" "unshare --user SUCCEEDED"
    log "FAIL: user namespace creation possible"
else
    pass "User namespace blocked" ""
    log "PASS: user namespace creation blocked"
fi

# PID 1 check
pid1_cmd=""
if [ -r /proc/1/cmdline ]; then
    pid1_cmd=$(tr '\0' ' ' < /proc/1/cmdline 2>/dev/null | head -c 200)
fi
if echo "$pid1_cmd" | grep -qiE '(systemd|/sbin/init)'; then
    fail "PID 1 is not host init" "PID 1: $pid1_cmd"
else
    pass "PID 1 is not host init" "$(echo "$pid1_cmd" | head -c 60)"
fi
log "PID 1: $pid1_cmd"

# Device access (always true for non-root — root execution is blocked above)
if [ -r /dev/mem ]; then
    fail "Device access blocked" "/dev/mem is readable"
else
    pass "Device access blocked" "/dev/mem not readable"
fi

# Sysrq (always true for non-root — root execution is blocked above)
if [ -w /proc/sysrq-trigger ]; then
    fail "Sysrq blocked" "/proc/sysrq-trigger is writable"
else
    pass "Sysrq blocked" ""
fi

# ── 1b. Filesystem ──────────────────────────────────────────────────────────
hdr "FILESYSTEM"

# Docker socket
if [ -e /var/run/docker.sock ]; then
    fail "No Docker socket" "FOUND at /var/run/docker.sock"
else
    pass "No Docker socket" ""
fi

# Containerd socket
if [ -e /run/containerd/containerd.sock ]; then
    fail "No containerd socket" "FOUND"
else
    pass "No containerd socket" ""
fi

# DOCKER_HOST env
if [ -n "${DOCKER_HOST:-}" ]; then
    fail "No DOCKER_HOST env" "set to $DOCKER_HOST"
else
    pass "No DOCKER_HOST env" ""
fi

# ── 1c. Credentials ─────────────────────────────────────────────────────────
hdr "CREDENTIALS"

check_path_absent() {
    local label="$1" path="$2"
    if [ -e "$path" ]; then
        fail "$label" "FOUND: $path"
        log "FAIL: $label found at $path"
    else
        pass "$label" ""
        log "PASS: $label not found"
    fi
}

# Git credentials (Gitea credentials are expected inside the container)
git_cred_found=false
if [ -e "$HOME/.git-credentials" ]; then
    if $IN_CONTAINER && [ -n "${GITEA_URL:-}" ]; then
        # In container with Gitea: only flag credentials for non-Gitea URLs.
        # .git-credentials format: http://user:token@host:port
        # GITEA_URL format: http://host:port — extract host:port to match.
        gitea_host=$(echo "$GITEA_URL" | sed 's|.*://||;s|/.*||')
        non_gitea=$(grep -v "$gitea_host" "$HOME/.git-credentials" 2>/dev/null \
            | grep -v '^$' || true)
        if [ -n "$non_gitea" ]; then git_cred_found=true; fi
    else
        git_cred_found=true
    fi
fi
if ! $IN_CONTAINER && git config --global credential.helper >/dev/null 2>&1; then
    helper=$(git config --global credential.helper 2>/dev/null)
    if [ -n "$helper" ]; then
        git_cred_found=true
    fi
fi
if $git_cred_found; then
    fail "No git credentials" "found credential helper or .git-credentials"
else
    if $IN_CONTAINER && [ -n "${GITEA_URL:-}" ]; then
        pass "No git credentials" "Gitea-only (expected)"
    else
        pass "No git credentials" ""
    fi
fi

# SSH private keys (detect by content, not naming convention)
ssh_keys=$(grep -rlm 1 'PRIVATE KEY' "$HOME"/.ssh/ 2>/dev/null || true)
if [ -n "$ssh_keys" ]; then
    key_count=$(echo "$ssh_keys" | wc -l)
    fail "No SSH private keys" "$key_count key(s) found"
else
    pass "No SSH private keys" ""
fi

# Cloud credentials
check_path_absent "No AWS credentials" "$HOME/.aws/credentials"
check_path_absent "No GCP credentials" "$HOME/.config/gcloud"
check_path_absent "No Azure credentials" "$HOME/.azure"

# Container registry
check_path_absent "No Docker registry auth" "$HOME/.docker/config.json"

# Package auth
npm_auth=false
if [ -f "$HOME/.npmrc" ] && grep -q '_authToken' "$HOME/.npmrc" 2>/dev/null; then
    npm_auth=true
fi
pypi_auth=false
if [ -f "$HOME/.pypirc" ] && grep -q 'password' "$HOME/.pypirc" 2>/dev/null; then
    pypi_auth=true
fi
if $npm_auth || $pypi_auth; then
    fail "No package registry auth" "found in .npmrc or .pypirc"
else
    pass "No package registry auth" ""
fi

# K8s tokens
check_path_absent "No K8s service account" "/var/run/secrets/kubernetes.io"

# GPG keys
gpg_keys=false
if [ -d "$HOME/.gnupg/private-keys-v1.d" ]; then
    gpg_count=$(find "$HOME/.gnupg/private-keys-v1.d/" -maxdepth 1 -type f 2>/dev/null | wc -l)
    if [ "$gpg_count" -gt 0 ]; then gpg_keys=true; fi
fi
if $gpg_keys; then
    fail "No GPG private keys" "$gpg_count key(s) found"
else
    pass "No GPG private keys" ""
fi

# gh CLI
check_path_absent "No gh CLI auth" "$HOME/.config/gh/hosts.yml"

# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2: NETWORK BARRIER CHECKS
# ══════════════════════════════════════════════════════════════════════════════
hdr "NETWORK"

net_check() {
    local label="$1" target="$2" expect_fail="$3"
    local result
    if curl --connect-timeout 3 -so /dev/null "$target" 2>/dev/null; then
        result="reachable"
    else
        result="unreachable"
    fi

    if [ "$expect_fail" = "true" ]; then
        if [ "$result" = "unreachable" ]; then
            pass "$label" "unreachable"
        else
            fail "$label" "REACHABLE"
        fi
    else
        if [ "$result" = "reachable" ]; then
            pass "$label" "reachable"
        else
            fail "$label" "UNREACHABLE"
        fi
    fi
    log "Network: $label -> $result (expect_fail=$expect_fail)"
}

# RFC1918 / LAN
net_check "RFC1918 blocked (10.0.0.1)"     "http://10.0.0.1"      true
net_check "RFC1918 blocked (172.16.0.1)"    "http://172.16.0.1"    true
net_check "RFC1918 blocked (192.168.1.1)"   "http://192.168.1.1"   true
net_check "Docker bridge (172.17.0.1)"      "http://172.17.0.1"    true
net_check "Metadata (169.254.169.254)"      "http://169.254.169.254" true

# Baseline connectivity
net_check "Baseline HTTP (example.com:80)"  "http://example.com"   false
net_check "Baseline HTTPS (example.com)"    "https://example.com"  false

# Port filtering (detect egress mode)
if curl --connect-timeout 3 -so /dev/null "http://example.com:8080" 2>/dev/null; then
    EGRESS_MODE="open"
    info "Port filtering (example.com:8080)" "reachable (open-egress mode)"
    info "Port filtering (example.com:22)"   "not tested (open-egress mode)"
else
    EGRESS_MODE="locked"
    pass "Port filtering (example.com:8080)" "blocked (locked mode)"
    # Also check SSH port
    if curl --connect-timeout 3 -so /dev/null "http://example.com:22" 2>/dev/null; then
        fail "Port filtering (example.com:22)" "REACHABLE in locked mode"
    else
        pass "Port filtering (example.com:22)" "blocked (locked mode)"
    fi
fi
log "Egress mode: $EGRESS_MODE"

# ══════════════════════════════════════════════════════════════════════════════
# CONTAINER-ONLY CHECKS (auto-detected via /.dockerenv)
# ══════════════════════════════════════════════════════════════════════════════

download_tool() {
    local name="$1" url="$2" expected_sha="$3"
    local dest="$TOOLS_DIR/$name"

    if [ -f "$dest" ]; then
        local existing_sha
        existing_sha=$(sha256sum "$dest" | awk '{print $1}')
        if [ "$existing_sha" = "$expected_sha" ]; then
            log "Tool $name already downloaded with correct checksum"
            return 0
        fi
    fi

    log "Downloading $name from $url"
    if ! curl -fsSL --connect-timeout 10 --max-time 120 -o "$dest" "$url" 2>/dev/null; then
        warn_msg "Failed to download $name"
        log "FAIL: download $name"
        return 1
    fi

    local actual_sha
    actual_sha=$(sha256sum "$dest" | awk '{print $1}')
    if [ "$actual_sha" != "$expected_sha" ]; then
        warn_msg "Checksum mismatch for $name (expected: ${expected_sha:0:16}..., got: ${actual_sha:0:16}...)"
        rm -f "$dest"
        log "FAIL: checksum mismatch for $name"
        return 1
    fi

    log "Downloaded $name, checksum verified"
    return 0
}

if $IN_CONTAINER; then

# ── Deepce ───────────────────────────────────────────────────────────────────
hdr "DEEPCE"

if download_tool "deepce.sh" "$DEEPCE_URL" "$DEEPCE_SHA256"; then
    chmod +x "$TOOLS_DIR/deepce.sh"
    log "Running Deepce"
    timeout 300 bash "$TOOLS_DIR/deepce.sh" --no-colors --no-network \
        > "$RESULTS_DIR/deepce.txt" 2>&1 || true

    # Parse Deepce output
    deepce_out="$RESULTS_DIR/deepce.txt"

    parse_deepce() {
        local label="$1" pattern="$2" expect="$3"
        local line
        line=$(grep -i "$pattern" "$deepce_out" 2>/dev/null | head -1)
        if [ -z "$line" ]; then
            skip "$label" "not found in output"
            return
        fi
        if echo "$line" | grep -qi "yes"; then
            if [ "$expect" = "no" ]; then
                fail "$label" "YES"
            else
                pass "$label" "yes"
            fi
        elif echo "$line" | grep -qi "no"; then
            if [ "$expect" = "no" ]; then
                pass "$label" "no"
            else
                fail "$label" "NO"
            fi
        else
            info "$label" "$(echo "$line" | sed 's/.*\.\.\.*//;s/^[[:space:]]*//')"
        fi
    }

    parse_deepce "Docker Socket"           "docker sock"          "no"
    parse_deepce "Privileged Mode"         "privileged mode"      "no"

    # Dangerous capabilities: when Deepce says "Yes", verify which of its
    # dangerous caps are present using CapBnd (bounding set — what Deepce checks).
    # CapEff is empty for non-root users; CapBnd shows what could be acquired.
    # Deepce's dangerous list: sys_admin(21) sys_ptrace(19) sys_module(16)
    # sys_rawio(17) dac_read_search(2) dac_override(1) mknod(27).
    # dac_override is ignored: intentionally granted by sandbox.
    deepce_cap_line=$(grep -i "dangerous cap" "$deepce_out" 2>/dev/null | head -1)
    if [ -z "$deepce_cap_line" ]; then
        skip "Dangerous Capabilities" "not found in output"
    elif echo "$deepce_cap_line" | grep -qi "no"; then
        pass "Dangerous Capabilities" "none"
    elif [ -z "$cap_bnd_hex" ]; then
        fail "Dangerous Capabilities" "YES (cannot verify — no CapBnd)"
    else
        cap_bnd_dec=$((16#$cap_bnd_hex))
        unexpected="" ignored=""
        for entry in "sys_admin:21" "sys_ptrace:19" "sys_module:16" \
                     "sys_rawio:17" "dac_read_search:2" "dac_override:1" "mknod:27"; do
            name="${entry%%:*}"
            bit="${entry##*:}"
            if (( cap_bnd_dec & (1 << bit) )); then
                if [ "$name" = "dac_override" ]; then
                    ignored="${ignored:+$ignored, }$name"
                else
                    unexpected="${unexpected:+$unexpected, }$name"
                fi
            fi
        done
        if [ -n "$unexpected" ]; then
            fail "Dangerous Capabilities" "$unexpected"
        else
            pass "Dangerous Capabilities" "none unexpected"
        fi
        # Always show which caps were detected but ignored
        if [ -n "$ignored" ]; then
            info "  dac_override present" "expected (sandbox grants DAC_OVERRIDE)"
        fi
    fi

    # Docker group: check Deepce's Groups line directly
    deepce_groups=$(grep -i '^\[+\] Groups' "$deepce_out" 2>/dev/null | head -1)
    if [ -z "$deepce_groups" ]; then
        skip "Docker Group" "not found in output"
    elif echo "$deepce_groups" | grep -qi 'docker'; then
        fail "Docker Group" "user is in docker group"
    else
        pass "Docker Group" "not in docker group"
    fi

    # Check for CVEs
    cve_hits=$(grep -ci "CVE-" "$deepce_out" 2>/dev/null) || cve_hits=0
    if [ "$cve_hits" -gt 0 ]; then
        cve_list=$(grep -oi "CVE-[0-9]\{4\}-[0-9]*" "$deepce_out" 2>/dev/null | sort -u | tr '\n' ', ' | sed 's/,$//')
        fail "Known CVEs" "$cve_list"
    else
        pass "Known CVEs" "none detected"
    fi
else
    skip "Deepce" "download failed"
fi

# ── LinPEAS ──────────────────────────────────────────────────────────────────
hdr "LINPEAS"

if download_tool "linpeas.sh" "$LINPEAS_URL" "$LINPEAS_SHA256"; then
    chmod +x "$TOOLS_DIR/linpeas.sh"
    log "Running LinPEAS (this may take a few minutes)"
    printf '  %sRunning LinPEAS (may take 1-3 minutes)...%s\r' "$DIM" "$RESET"
    timeout 300 bash "$TOOLS_DIR/linpeas.sh" -N -q -s \
        > "$RESULTS_DIR/linpeas.txt" 2>&1 || true
    printf "  %-60s\r" ""  # clear progress line

    linpeas_out="$RESULTS_DIR/linpeas.txt"

    # ── Container escape vectors ──
    # Parse LinPEAS's actual structured checks (═╣ lines with Yes/No results).
    # Ignore section headers, URLs, and documentation text.
    docker_sock_mount=$(grep -i 'Docker sock mounted' "$linpeas_out" 2>/dev/null | grep -ci 'yes') || docker_sock_mount=0
    release_agent=$(grep -i 'release_agent breakout' "$linpeas_out" 2>/dev/null | grep -ci 'yes') || release_agent=0
    core_pattern=$(grep -i 'core_pattern breakout' "$linpeas_out" 2>/dev/null | grep -ci 'yes') || core_pattern=0
    binfmt_misc=$(grep -i 'binfmt_misc breakout' "$linpeas_out" 2>/dev/null | grep -ci 'yes') || binfmt_misc=0
    uevent_helper=$(grep -i 'uevent_helper breakout' "$linpeas_out" 2>/dev/null | grep -ci 'yes') || uevent_helper=0
    escape_total=$((docker_sock_mount + release_agent + core_pattern + binfmt_misc + uevent_helper))
    if [ "$escape_total" -gt 0 ]; then
        fail "Container escape vectors" "$escape_total breakout(s) returned Yes"
    else
        pass "Container escape vectors" "all breakout checks negative"
    fi
    # Expected: nsenter/unshare present but harmless without capabilities
    escape_tools=$(grep -i 'Container escape tools' "$linpeas_out" 2>/dev/null \
        | grep -oiE '/usr/[^ ]+' | sort -u | tr '\n' ', ' | sed 's/,$//' || true)
    if [ -n "$escape_tools" ]; then
        info "  escape tools present" "$escape_tools (needs caps to use)"
    fi

    # ── Docker group ──
    linpeas_docker_group=$(grep -i 'Am I inside Docker group' "$linpeas_out" 2>/dev/null | head -1)
    if [ -n "$linpeas_docker_group" ]; then
        if echo "$linpeas_docker_group" | grep -qi 'no'; then
            pass "Docker group" "no"
        else
            fail "Docker group" "YES"
        fi
    fi

    # ── SUID/SGID binaries ──
    # Direct filesystem check — deterministic, no text parsing needed.
    suid_bins=$(find / -perm /6000 -type f 2>/dev/null \
        | grep -vE '(linpeas|barrier-check|\.tools)' | sort || true)
    suid_count=0
    if [ -n "$suid_bins" ]; then
        suid_count=$(echo "$suid_bins" | wc -l)
    fi
    if [ "$suid_count" -gt 0 ]; then
        echo "$suid_bins" > "$RESULTS_DIR/suid-sgid.txt"
        info "SUID/SGID binaries" "$suid_count found (see barrier-check-results/suid-sgid.txt)"
    else
        pass "SUID/SGID binaries" "none found"
    fi

    # ── Writable sensitive files ──
    # Check LinPEAS structured output lines for writable paths
    writable_hits=$(grep -ciE '(writable|world.writ).*(/etc/passwd|/etc/shadow|/etc/cron|/etc/sudoers)' \
        "$linpeas_out" 2>/dev/null) || writable_hits=0
    if [ "$writable_hits" -gt 0 ]; then
        fail "Writable sensitive files" "$writable_hits found"
    else
        pass "Writable sensitive files" "none"
    fi

    # ── CVE matches ──
    # Opt-out: collect all CVE IDs, then remove ones LinPEAS explicitly cleared.
    # For uncleared CVEs, extract their status (vulnerable / unknown / may be).
    all_cve_ids=$(grep -oiE 'CVE-[0-9]{4}-[0-9]+' "$linpeas_out" 2>/dev/null | sort -u || true)
    cleared_cves=""
    # cve_status: associative array mapping CVE ID → status string
    declare -A cve_status
    for cve_id in $all_cve_ids; do
        cve_lines=$(grep -i "$cve_id" "$linpeas_out" 2>/dev/null || true)
        # Cleared: explicit "Not Found"
        if echo "$cve_lines" | grep -qiE 'not found'; then
            cleared_cves="${cleared_cves}${cve_id}|"
            continue
        fi
        # Cleared: "Vulnerable to CVE-XXXX .... No" (trailing whitespace tolerant)
        if echo "$cve_lines" | grep -qiE "vulnerable.*${cve_id}.*No[[:space:]]*$"; then
            cleared_cves="${cleared_cves}${cve_id}|"
            continue
        fi
        # Not cleared — extract status for reporting
        if echo "$cve_lines" | grep -qiE 'may be vulnerable'; then
            cve_status[$cve_id]="may be vulnerable"
        elif echo "$cve_lines" | grep -qiE 'unknown'; then
            cve_status[$cve_id]="unknown"
        elif echo "$cve_lines" | grep -qiE "vulnerable.*Yes"; then
            cve_status[$cve_id]="vulnerable"
        else
            cve_status[$cve_id]="referenced"
        fi
    done
    # Build list of uncleared CVEs with their status
    real_cves=""
    for cve_id in $all_cve_ids; do
        if [ -n "$cleared_cves" ] && echo "$cve_id" | grep -qE "^(${cleared_cves%|})$"; then
            continue
        fi
        status="${cve_status[$cve_id]:-referenced}"
        real_cves="${real_cves:+$real_cves, }${cve_id} (${status})"
    done
    if [ -n "$real_cves" ]; then
        # FAIL only if any CVE is confirmed vulnerable; WARN if all are uncertain
        has_confirmed=false
        for cve_id in $all_cve_ids; do
            s="${cve_status[$cve_id]:-}"
            if [ "$s" = "vulnerable" ]; then
                has_confirmed=true
                break
            fi
        done
        if $has_confirmed; then
            fail "CVE matches" "$real_cves"
        else
            warn "CVE matches" "$real_cves"
        fi
        info "  (kernel-level)" "shared host kernel — see threat model"
    else
        pass "CVE matches" "none"
    fi

    # ── Expected by design ──
    # Passwordless sudo: LinPEAS flags this as PE vector, but it's by design
    if grep -qi 'NOPASSWD\|Passwordless' "$linpeas_out" 2>/dev/null; then
        info "  passwordless sudo" "expected (agent needs sudo for tasks)"
    fi
    # SSH_PASSWORD in env
    if grep -qi 'SSH_PASSWORD' "$linpeas_out" 2>/dev/null; then
        info "  SSH_PASSWORD in env" "expected (set by entrypoint)"
    fi
    # GITEA_TOKEN in env
    if grep -qi 'GITEA_TOKEN' "$linpeas_out" 2>/dev/null; then
        info "  GITEA_TOKEN in env" "expected (agent needs Gitea access)"
    fi
else
    skip "LinPEAS" "download failed"
fi

# ── Container-specific checks ────────────────────────────────────────────────
hdr "CONTAINER (sandbox-specific)"

# PID limit
pids_max=""
for cg_path in /sys/fs/cgroup/pids/pids.max /sys/fs/cgroup/pids.max; do
    if [ -r "$cg_path" ]; then
        pids_max=$(cat "$cg_path" 2>/dev/null)
        break
    fi
done
if [ -n "$pids_max" ]; then
    if [ "$pids_max" = "512" ]; then
        pass "PID limit" "512"
    elif [ "$pids_max" = "max" ]; then
        fail "PID limit" "unlimited"
    else
        info "PID limit" "$pids_max (expected 512)"
    fi
else
    skip "PID limit" "cannot read cgroup pids.max"
fi

# Only agent user
home_dirs=""
for d in /home/*/; do
    [ -d "$d" ] && home_dirs="${home_dirs}$(basename "$d")"$'\n'
done
home_dirs=$(echo "$home_dirs" | grep -v '^$' || true)
home_count=$(echo "$home_dirs" | grep -c . 2>/dev/null || echo "0")
if [ "$home_count" -eq 1 ] && echo "$home_dirs" | grep -q '^agent$'; then
    pass "Only agent user" ""
elif [ "$home_count" -eq 0 ]; then
    info "Only agent user" "no home dirs found"
else
    fail "Only agent user" "found: $home_dirs"
fi

# Home is Docker volume
if grep -q '/home/agent' /proc/self/mountinfo 2>/dev/null; then
    mount_type=$(grep '/home/agent' /proc/self/mountinfo 2>/dev/null | head -1)
    if echo "$mount_type" | grep -q 'overlay\|volume'; then
        pass "Home is Docker volume" ""
    else
        info "Home mount" "$(echo "$mount_type" | awk '{print $9, $10}')"
    fi
else
    info "Home is Docker volume" "cannot determine (not in mountinfo)"
fi

# No host bind mounts
bind_mounts=$(grep 'bind' /proc/self/mountinfo 2>/dev/null | grep -v '/etc/resolv\|/etc/hostname\|/etc/hosts' || true)
if [ -n "$bind_mounts" ]; then
    bind_count=$(echo "$bind_mounts" | wc -l)
    fail "No host bind mounts" "$bind_count unexpected bind mount(s)"
    log "Bind mounts: $bind_mounts"
else
    pass "No host bind mounts" ""
fi

# Default route via router
default_route=$(ip route 2>/dev/null | grep '^default' | head -1)
if [ -n "$default_route" ]; then
    pass "Default route via router" "$default_route"
else
    info "Default route" "no default route (fail-closed?)"
fi

# Only expected interfaces
ifaces=$(ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | sort)
unexpected=$(echo "$ifaces" | grep -vE '^(lo|eth0)$' || true)
if [ -n "$unexpected" ]; then
    info "Expected interfaces only" "extra: $unexpected"
else
    pass "Expected interfaces only" "lo, eth0"
fi

# Gitea admin API
if [ -n "${GITEA_URL:-}" ] && [ -n "${GITEA_TOKEN:-}" ]; then
    gitea_admin=$(curl -s --connect-timeout 3 \
        -H "Authorization: token $GITEA_TOKEN" \
        "$GITEA_URL/api/v1/admin/users" 2>/dev/null)
    if echo "$gitea_admin" | grep -q '"message"'; then
        pass "Gitea admin API blocked" "$(echo "$gitea_admin" | grep -o '"message":"[^"]*"' | head -1)"
    elif echo "$gitea_admin" | grep -q '"login"'; then
        fail "Gitea admin API blocked" "ADMIN ACCESS GRANTED"
    else
        info "Gitea admin API" "unexpected response"
    fi
else
    skip "Gitea admin API" "GITEA_URL or GITEA_TOKEN not set"
fi

# Own repos only
if [ -n "${GITEA_URL:-}" ] && [ -n "${GITEA_TOKEN:-}" ]; then
    repos=$(curl -s --connect-timeout 3 \
        -H "Authorization: token $GITEA_TOKEN" \
        "$GITEA_URL/api/v1/repos/search?limit=50" 2>/dev/null)
    repo_count=$(echo "$repos" | grep -co '"full_name"' 2>/dev/null || echo "0")
    if [ "$repo_count" -le 2 ]; then
        pass "Own repos only" "$repo_count repo(s) visible"
    else
        info "Own repos only" "$repo_count repo(s) visible (review if expected)"
    fi
else
    skip "Own repos only" "GITEA_URL or GITEA_TOKEN not set"
fi

# Docker API
if curl -s --connect-timeout 2 "http://localhost:2375/version" >/dev/null 2>&1; then
    fail "Docker API blocked" "REACHABLE on localhost:2375"
else
    pass "Docker API blocked" ""
fi

# Expected env vars only
secret_env=$(env | grep -iE '(key|secret|token|password)' 2>/dev/null \
    | grep -viE '(GITEA_TOKEN|SSH_PASSWORD|TERM|COLORTERM|LS_COLORS|GPG_KEY|PYTHON_SHA256|no_proxy)' \
    || true)
if [ -n "$secret_env" ]; then
    secret_count=$(echo "$secret_env" | wc -l)
    fail "Expected env vars only" "$secret_count unexpected secret-like var(s)"
    log "Unexpected env vars: $secret_env"
else
    pass "Expected env vars only" ""
fi

fi # end IN_CONTAINER

# ══════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
hdr "KNOWN LIMITATIONS (by design)"
printf '  %s- HTTPS exfiltration to public endpoints: possible%s\n' "$DIM" "$RESET"
printf '  %s- DNS exfiltration: possible%s\n' "$DIM" "$RESET"
printf '  %s- ICMP: allowed%s\n' "$DIM" "$RESET"
printf '  %s- Container-scoped persistence: possible%s\n' "$DIM" "$RESET"

echo ""
printf '%sEgress mode: %s%s\n' "$BOLD" "$EGRESS_MODE" "$RESET"
printf '%sFull output: %s/%s\n' "$DIM" "$RESULTS_DIR" "$RESET"
echo ""
log "Completed at $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
