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

`fetch-sandbox.py` is a standalone script you run from your **host machine** (not inside the container) to pull the agent's work into your real repository. It adds a `staging` git remote pointing at the local Gitea instance, fetches the requested branch, runs safety checks and an optional LLM security review, then merges.

```bash
python fetch-sandbox.py /path/to/your/repo agent/feature-branch
python fetch-sandbox.py /path/to/your/repo agent/feature-branch --base dev
python fetch-sandbox.py /path/to/your/repo agent/feature-branch --skip-review
```

Use `--base <branch>` to specify which branch the diff is computed against (for safety checks and security review). By default, the script auto-detects the base from `refs/remotes/origin/HEAD`, falling back to `main`.

The script performs these steps in order:

1. **Staging remote setup** — If the `staging` remote doesn't exist yet, prompts to add it (pointing at `http://localhost:<GITEA_PORT>/<agent-user>/<project>.git`).
2. **Fetch** — Runs `git fetch staging` to pull all refs from the agent's Gitea fork.
3. **Safety checks** — Scans the fetched ref for:
   - **Symlinks** — lists any symlinks and their targets.
   - **Auto-execute file modifications** — flags changes to files that run automatically (`.envrc`, `Makefile`, `package.json`, pre-commit hooks, `.gitmodules`, etc.).
4. **LLM security review** — If configured (via `fetch-sandbox.py setup`), computes the git diff and sends it to the configured LLM provider for security analysis. Results are displayed inline. If security issues are found, you're prompted before proceeding.
5. **Merge** — Applies the changes as unstaged modifications via `git merge --squash` + `git reset HEAD`.

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

## ◾ FAQ

### GPU / CUDA support?

Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) on the host and pass `--gpus all` when creating the project. 
Create a custom profile with a CUDA base image if your workload needs one — the toolkit mounts the host driver automatically.

### Rootless Docker support?

If you already run [rootless Docker](https://docs.docker.com/engine/security/rootless/), the sandbox works as-is with no changes.
The added benefit is that a container escape lands as your unprivileged user rather than root, and granted capabilities (CHOWN, SETUID, etc.) are scoped to a user namespace that can't affect the real host.
This doesn't prevent the escape itself, but limits the blast radius.
Not required — regular Docker with the network isolation above is the intended baseline.
