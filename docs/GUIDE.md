# Guide

[◾ After a Reboot](#-after-a-reboot)
[◾ Recreate vs Destroy](#-recreate-vs-destroy)
[◾ Image Profiles](#-image-profiles)
[◾ Docker-in-Docker](#-docker-in-docker)
[◾ `container/` Directory](#-container-directory)
[◾ Git Remotes Inside the Container](#-git-remotes-inside-the-container)
[◾ VS Code Remote-SSH](#-vs-code-remote-ssh)
[◾ Fetch Sandbox](#-fetch-sandbox)
[◾ Reviewer](#-reviewer)
[◾ Repo Watch](#-repo-watch)
[◾ CI Watch](#-ci-watch)
[◾ FAQ](#-faq)

## ◾ After a Reboot

```bash
docker compose up -d           # Gitea, router
python sandbox.py start --all  # Agent containers
```

To fully tear down everything (all agent containers, volumes, networks, and infrastructure):

```bash
python sandbox.py unsetup
```

This removes all agent containers and their workspace volumes, stops and removes Gitea/router containers and their Docker volumes, cleans up per-project networks, and removes the generated `GITEA_ADMIN_TOKEN` from `.env`. 
Your other `.env` settings are preserved — run `sandbox setup` to start fresh.

## ◾ Recreate vs Destroy

Both commands require confirmation before proceeding.

`recreate` gives the agent a clean workspace while keeping its Gitea identity — the agent's fork retains all branches and PRs. Use it to reset a stuck agent or switch profiles without losing git history on Gitea.

`destroy` fully removes the project, including the Gitea mirror. To re-create the project you need to import from GitHub again.

|  | `recreate` | `destroy` |
|---|---|---|
| Agent container | Replaced | Removed |
| Workspace volume | Deleted and recreated | Deleted |
| `container/` files (CLAUDE.md, etc.) | Re-copied | Deleted |
| Gitea agent user and repo | Preserved | Deleted |
| Gitea mirror (sandbox-admin) | Preserved | Deleted |


## ◾ Image Profiles

Profiles let you pick different base environments for agent containers. Each profile is a Dockerfile in `agent/` named `Dockerfile.<profile>`.

| Profile | Base image | Includes |
|---|---|---|
| `python` | `continuumio/miniconda3` | conda, nano |
| `cuda` | `nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04` | CUDA 12.8, PyTorch, conda, nano |

```bash
python sandbox.py create https://github.com/you/myproject --profile python
```

To add a custom profile, create `agent/Dockerfile.myprofile`. Copy an existing Dockerfile as a starting point. The entrypoint is always `agent/entrypoint.sh`.

**Structural requirements:**

- Non-root `agent` user with UID 1000 and passwordless sudo
- `/home/agent` as the working directory
- Home directory contents stashed to `/etc/agent-skel` (the entrypoint copies them into
  the workspace volume on first run)

**Required packages:**

| Package | Needed by |
|---|---|
| `git` | entrypoint (clone, remotes), all git workflows |
| `openssh-server` | entrypoint (sshd), VS Code Remote-SSH |
| `byobu` | entrypoint (terminal session) |
| `sudo` | entrypoint (user setup) |
| `curl` | repo-watch (Gitea API) |
| `jq` | repo-watch (JSON parsing) |

**Recommended:**

| Package | Why |
|---|---|
| `locales` | UTF-8 support — without it, some tools misbehave on non-ASCII content |
| `nano` | Lightweight editor for quick in-container edits |
| `iputils-ping` | Useful for debugging network isolation |
| `btop` | Useful for a global view of the container activity |

## ◾ Docker-in-Docker

The `--docker` flag gives the agent a full Docker environment inside its container, so it can build images, run `docker compose`, and orchestrate containers. This uses [Sysbox](https://github.com/nestybox/sysbox), a container runtime that enables Docker-in-Docker without `--privileged` or socket mounting.

```bash
python sandbox.py create https://github.com/you/myproject --profile python --docker
```

**Requirement:** Sysbox must be installed on the host. The CLI will error if it's missing. See [Sysbox installation](https://github.com/nestybox/sysbox#installation).

`--docker` is orthogonal to `--profile` — any profile can use it. Docker is installed at container creation time (not baked into the image), so existing profiles work without modification. On `stop`/`start`, the Docker daemon restarts automatically.

**What changes with `--docker`:**
- Container runs with `--runtime=sysbox-runc` instead of the default runtime
- PID limit is raised from 512 to 2048 (inner containers need headroom)
- Docker CE is installed inside the container at creation time (~30-60s)
- [crun](https://github.com/containers/crun) is used as the inner OCI runtime instead of runc (see below)
- The `agent` user is added to the `docker` group for rootless access
- `barrier-check.sh` will flag the Docker socket and extra capabilities — these are expected in DinD mode (see [Security: Docker-in-Docker](SECURITY.md#-docker-in-docker-security))

**What stays the same:** Network isolation, Gitea setup, router egress filtering — all unchanged.

#### Why crun instead of runc

The inner Docker daemon uses [crun](https://github.com/containers/crun) as its OCI runtime. runc >= 1.3.3 added procfs mount validation ([CVE-2025-52881](https://github.com/opencontainers/runc/security/advisories/GHSA-jfvp-7x6p-h2pv)) that false-positives on Sysbox's FUSE-emulated `/proc/sys`, preventing any inner container from starting. crun implements the same OCI spec (namespaces, cgroups, seccomp, capabilities) without this validation issue. Since Sysbox is the outer security boundary, the inner OCI runtime choice has no effect on host isolation. See [sysbox#973](https://github.com/nestybox/sysbox/issues/973) for upstream tracking.

## ◾ `container/` Directory

Any files placed in `container/` are copied into each agent's home directory volume at `/home/agent/`.
Use this to provide config files or custom instructions to every agent. For key-based SSH access,
place your public key at `container/.ssh/authorized_keys`.

By default it ships with:
- `CLAUDE.md` — baseline agent instructions (git workflow, remotes, verification)
- `repo-watch.sh` — agentic loop script (see [Repo Watch](#repo-watch))
- `repo-watch-prompt.md` — prompt template for repo-watch
- `agent-watch.sh` — real-time viewer for agent activity (see [Monitoring](#monitoring))

## ◾ Git Remotes Inside the Container

Each agent container has two git remotes:
- **`origin`** — the agent's fork on Gitea (read-write)
- **`upstream`** — the mirror of the GitHub repo (read-only)

After you merge the agent's work on GitHub and the mirror syncs, the agent must fetch and merge from both `origin` and `upstream` remotes before starting work.
This is **not done automatically** — it is up to the agent (or the user) to sync. 
The default `CLAUDE.md` instructs Claude Code to do this before each task.

## ◾ VS Code Remote-SSH

Connect to agent containers via VS Code Remote-SSH for full IDE access:

```bash
sandbox ssh   # shows port + password for all containers
ssh agent@localhost -p <ssh-port>
```

Once connected, run `byobu attach` in the VS Code terminal to connect to the agent session. 
Displaying the password is not a security issue, as anyone with docker permissions can already connect directly to the agent container.

**Important**: Verify these VS Code settings are disabled before connecting:
- `remote.SSH.enableAgentForwarding` — must be off (forwards host SSH keys)
- Git credential forwarding — must not be configured

## ◾ Fetch Sandbox

`fetch-sandbox.py` is a standalone script you run from your **host machine** (not inside the container) to pull the agent's work into your real repository. It fetches the requested branch directly by URL from the local Gitea instance, runs safety checks and an optional LLM security review, then merges. No git remote is added to your repo.

```bash
python fetch-sandbox.py /path/to/your/repo agent/feature-branch
python fetch-sandbox.py /path/to/your/repo agent/feature-branch --base dev
python fetch-sandbox.py /path/to/your/repo agent/feature-branch --skip-review
python fetch-sandbox.py /path/to/your/repo agent/feature-branch --remote staging
```

Use `--base <branch>` to specify which branch the diff is computed against (for safety checks and security review). By default, the script auto-detects the base from `refs/remotes/origin/HEAD`, falling back to `main`.

Use `--remote <name>` to fetch from a pre-configured git remote instead of by URL. This is optional — by default the script fetches directly and leaves no trace in your git config.

The script performs these steps in order:

1. **Fetch** — Fetches the requested branch by URL into a temporary ref (`refs/sandbox-fetch/<branch>`). The ref is cleaned up automatically after the operation.
2. **Safety checks** — Scans the fetched ref for:
   - **Symlinks** — lists any symlinks and their targets.
   - **Auto-execute file modifications** — flags changes to files that run automatically (`.envrc`, `Makefile`, `package.json`, pre-commit hooks, `.gitmodules`, etc.).
3. **LLM security review** — If configured (via `fetch-sandbox.py setup`), computes the git diff and sends it to the configured LLM provider for security analysis. Results are displayed inline. If security issues are found, you're prompted before proceeding.
4. **Merge** — Applies the changes as unstaged modifications via `git merge --squash` + `git reset HEAD`.

Requires `GITEA_ADMIN_TOKEN` in `.env` (set by `sandbox setup`).

## ◾ Reviewer

Security reviews run automatically at fetch time. When `fetch-sandbox.py` fetches a branch, it computes the git diff and sends it to a configured LLM for security analysis. Results are displayed in the terminal. If security issues are found, you're prompted before merging.

The API key stays on the host — it never enters any container.

The reviewer is configured once:

```bash
python fetch-sandbox.py setup   # Interactive: configure provider, key, model
```

`setup` prompts for provider, API key, and model, verifies credentials with a health check, and writes the config to `.env`. No containers, webhooks, or bot users are involved.

To skip the review for a specific fetch:
```bash
python fetch-sandbox.py /path/to/repo branch --skip-review
```

### Supported providers

| Provider | API key required | Endpoint |
|---|---|---|
| `anthropic` | Yes | `https://api.anthropic.com`|
| `openai` | Yes | `https://api.openai.com`|
| `openrouter` | Yes | `https://openrouter.ai/api`|
| `local` | No | Must set `REVIEWER_ENDPOINT` |

Default endpoints are built into `fetch-sandbox.py`. Override with `REVIEWER_ENDPOINT` in `.env`.

**Customizing the review prompt:** Edit `review-config.yaml` in the project root. The prompt must contain a `{diff}` placeholder. Changes take effect on the next `fetch-sandbox.py` run — no rebuild needed.

## ◾ Repo Watch

`repo-watch.sh` is a bash script that turns the agent into an autonomous developer you interact with through Gitea issues. 
It polls the Gitea API, detects new activity, and invokes Claude Code to handle it.

### How it works

The script checks open issues assigned to the agent every `POLL_INTERVAL` seconds
(default: 30). For each issue:

- If the last comment is by the agent — skip (waiting for human input).
- If the last comment is by a human (or the issue is new) — invoke Claude Code with
  the full conversation context.

It also monitors open PRs authored by the agent for line-level review comments that don't show up as issue comments.

One issue is processed per cycle. The script blocks the terminal — use byobu F2 for a new window.

### Usage

```bash
./repo-watch.sh                        # poll every 30s (default)
POLL_INTERVAL=60 ./repo-watch.sh       # poll every 60s
```

### Customizing the prompt

Edit `~/repo-watch-prompt.md` inside the container. This file contains the agent's behavioral instructions and Gitea API examples. 
The script appends the issue context to the end of this file before invoking Claude Code.

### Labels

On first run, the script creates these labels (idempotent):
- `in-progress` — agent is working on it
- `needs-review` — agent opened a PR
- `done` — merged and closed

### Monitoring

Each Claude Code invocation is streamed to a JSONL log file in `~/.repo-watch-logs/`.
A symlink at `~/.repo-watch-logs/current.jsonl` points to the active log during execution.

**Live status** (open a new byobu window with F2):
```bash
./agent-watch.sh               # real-time: elapsed time, tokens, cost, tool calls
```

**From the host** (if using `PROJECTS_DIR` bind mounts):
```bash
tail -f container_volumes/<project>/.repo-watch-logs/current.jsonl
```

Log files persist after completion and can be attached to issue comments.

### Slash commands

Users can prefix issue bodies or comments with slash commands to control agent behavior. Commands are defined in `~/issue-commands.json` inside the container (shipped from `container/issue-commands.json`).

| Command | Effect |
|---|---|
| `/plan` | Produce a structured plan without writing code |
| `/review` | Review the open PR and post findings |
| `/explain <topic>` | Explain a file, concept, or codebase area |
| `/test` | Run the test suite and report results |
| `/search <topic>` | Research a topic using web search, no code changes |
| `/security` | Security audit for vulnerabilities in code |
| `/fix` | Diagnose and fix a specific bug or error |
| `/refactor` | Improve code quality without changing behavior |
| `/deps` | Audit dependencies for vulnerabilities and outdated packages |

Each command can specify a `task_prefix` (prepended to the prompt) and `flags` (passed to the agent binary, e.g. `--disallowedTools`). To add or modify commands, edit `container/issue-commands.json`.

There are also **CI commands** (`/test-pr`, `/test-pr-bug`) that trigger external verification — see [CI Watch](#-ci-watch).

### Configuration

| Variable | Effect | Default |
|---|---|---|
| `POLL_INTERVAL` | Seconds between polling cycles | `30` |
| `MAX_RETRIES` | Consecutive failures before skipping an issue | `3` |
| `REPO_WATCH_MAX_TURNS` | Max agentic iterations per invocation | unlimited |
| `REPO_WATCH_MAX_BUDGET_USD` | Cost ceiling per invocation (USD) | unlimited |
| `REPO_WATCH_TIMEOUT` | Wall-clock limit (e.g., `10m`, `1h`) | unlimited |

Set as environment variables when launching repo-watch:
```bash
POLL_INTERVAL=60 REPO_WATCH_MAX_TURNS=50 REPO_WATCH_TIMEOUT=15m ./repo-watch.sh
```

## ◾ CI Watch

External PR verification that neither the agent nor a compromised container can tamper with. Post a command on a PR, the host runs the tests in a sandboxed container (no network, no tokens), and posts results back as `sandbox-admin`.

### Why

The SWE agent lies. It claims tests pass without running them, writes vacuous tests, and submits broken code. Internal hooks (git hooks, Claude hooks) run inside the container and can be bypassed. CI watch runs on the host — the agent can't modify, disable, or fake the results.

### How it works

```
Agent or human posts PR comment:  /test-pr-bug tests/repro_42.py agent/fix-login
                                              │
                                              ▼
                             sandbox ci-watch polls Gitea API, finds command
                                              │
                                              ▼
                             Host clones repo to temp dir (using admin token)
                                              │
                                              ▼
                            docker run --rm --network=none -v /tmp/ci-xyz:/repo:ro
                              ├─ No network, no tokens, read-only repo
                              ├─ Run test per command type (see below)
                              └─ Host captures output, posts results as sandbox-admin
                                              │
                                              ▼
                            repo-watch detects new PR comment on next poll
                              ├─ Passed → agent proceeds
                              └─ Failed → agent fixes and re-triggers
```

### Commands

Commands are defined in `ci-commands.json` and can be customized. The default commands are:

#### `/test-pr-bug <test-file> <branch>`

Verifies a bug fix using the "time-travel" method:

1. Clone repo, checkout `<branch>`, record commit SHA
2. Save `<test-file>` from `<branch>`
3. Checkout base branch, run the test — **must exit non-zero** (bug exists on base)
4. Checkout `<branch>`, run the test — **must exit 0** (fix works)

The test file is uploaded as a downloadable Gitea attachment on the result comment for human review.

```
/test-pr-bug tests/repro_42.py agent/fix-login
```

#### `/test-pr "<test-command>" <branch>`

Runs an explicit test command on a branch:

1. Clone repo, checkout `<branch>`, record commit SHA
2. Run `<test-command>` — **must exit 0**

```
/test-pr "pytest tests/" agent/add-auth
/test-pr "python -m unittest discover" agent/refactor-db
```

#### Custom commands

Add custom CI commands by editing `ci-commands.json`. For example, to add a `/lint` command:

```json
"/lint": {
  "description": "Run linter on the PR branch",
  "type": "fixed-command",
  "command": "ruff check .",
  "usage": "/lint <branch>",
  "args": { "branch": {"pattern": "^[a-zA-Z0-9][a-zA-Z0-9/_.-]*$"} },
  "timeout": 120, "memory": "1g"
}
```

### Result format

Results are posted as a PR comment by `sandbox-admin`:

```
## CI: tests/repro_42.py

**Tested commit:** `abc1234`
**Status:** PASS

| Step | Result |
|------|--------|
| Repro fails on base (`main`) | pass (exit 1) |
| Repro passes on PR (`agent/fix`) | pass (exit 0) |
```

On failure, the full test output is included (truncated to 50 lines). If the PR HEAD has moved since the test ran, a stale warning is shown. The full log is attached as a downloadable file.

### Setup

```bash
sandbox ci-watch setup    # Configure (prompted during 'sandbox setup')
sandbox up                # Start infrastructure + CI watch
sandbox ci-watch stop     # Stop CI watch
sandbox status            # Check CI watch status + active tests
```

CI watch is a background Python process managed by `sandbox.py`. It starts with `sandbox up` and stops with `sandbox down`.

### Configuration

| Variable | Effect | Default |
|---|---|---|
| `CI_WATCH_ENABLED` | Enable CI watch background process | `false` |
| `CI_WATCH_POLL_INTERVAL` | How often to poll Gitea for commands (seconds) | `5` |

Set in `.env`. CI watch reads `GITEA_ADMIN_TOKEN` from `.env` on the host — this token never enters any container.

### Clean room verification

Verification runs in a disposable container with no shared state with the agent. This follows CI/CD industry practice (GitHub Actions, GitLab CI):

- `docker run --rm` with the project's agent image (same base tools: conda, node, git, etc.)
- **Pre-cloned by host** — the host process clones the repo to a temp directory and bind-mounts it
- **No shared volume** — the agent's `/home/agent` volume is never mounted
- **Network: none** — the test container has no network access at all
- **No tokens** — no Gitea token, no Docker socket, no credentials of any kind
- **Resource limits:** 2 GB memory, 2 CPUs, 10-minute timeout
- **cap-drop ALL** — minimal Linux capabilities

If the test needs dependencies, they must already be in the agent image (the container has no network to install them).

### Security model

**No Docker socket in containers.** Unlike the previous webhook handler, CI watch runs entirely on the host. No container has Docker socket access.

**No tokens in containers.** The admin token stays on the host. The repo is pre-cloned by the host and bind-mounted — the test container never sees any credentials. Even `git remote -v` shows a local path, not a URL with a token.

**No network in test containers.** Test containers run with `--network=none`. A malicious test cannot download malware, exfiltrate data, or phone home.

**Unforgeable results.** Results are posted as `sandbox-admin` using the admin token. The agent can only post as `agent-{project}`. The comment author is the trust signal.

**SHA pinning.** CI watch records the tested commit SHA. If the PR HEAD has moved since (e.g., the agent force-pushed), the result is marked stale.

**Input validation.** Branch names must match `^[a-zA-Z0-9][a-zA-Z0-9/_.-]*$`. Test file paths must match `^[a-zA-Z0-9][a-zA-Z0-9/_.-]*\.(py|sh)$` with no `..`. Both must exist in the repo (verified via Gitea API before container spin-up). No `shell=True` in subprocess calls.

**Rate limiting.** Max 10 CI runs per PR per hour (configurable in `ci-commands.json`).

**Rate limiting.** Max 10 verification runs per PR per hour.

### Agent instructions

The agent is *instructed* to trigger verification (via `CLAUDE.md`) but not forced. The design is "instruct, don't force" — if the agent skips verification, the missing checkmark is visible to the human on the PR. The agent can also be told to trigger verification via issue comments.

The agent's `CLAUDE.md` tells it to:
- Write `tests/repro_<issue_number>.py` for bug fixes (exit non-zero when bug exists, exit 0 when fixed)
- Post `/test-pr-bug` or `/test-pr` on the PR after pushing
- Read verification results and fix failures (up to two attempts before asking the maintainer)

### Limitations

- **Vacuous tests.** The agent writes the test, so it can write one that always passes. The test file is attached for human review — this is a mitigation, not a prevention.
- **Agent skips verification.** If the agent doesn't post the command, verification doesn't run. The missing result is visible to the human.
- **Sequential execution.** CI watch processes one verification at a time. Concurrent test requests queue up in the polling loop.

## ◾ FAQ

### GPU / CUDA support?

Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) on the host and pass `--gpus all` when creating the project. 
Create a custom profile with a CUDA base image if your workload needs one — the toolkit mounts the host driver automatically.

### Rootless Docker support?

If you already run [rootless Docker](https://docs.docker.com/engine/security/rootless/), the sandbox works as-is with no changes.
The added benefit is that a container escape lands as your unprivileged user rather than root, and granted capabilities (CHOWN, SETUID, etc.) are scoped to a user namespace that can't affect the real host.
This doesn't prevent the escape itself, but limits the blast radius.
Not required — regular Docker with the network isolation above is the intended baseline.
