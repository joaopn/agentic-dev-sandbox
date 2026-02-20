# Guide

- [After a Reboot](#after-a-reboot)
- [Recreate vs Destroy](#recreate-vs-destroy)
- [Image Profiles](#image-profiles)
- [`container/` Directory](#container-directory)
- [Git Remotes Inside the Container](#git-remotes-inside-the-container)
- [VS Code Remote-SSH](#vs-code-remote-ssh)
- [Reviewer](#reviewer)
- [Repo Watch](#repo-watch)
  - [Monitoring](#monitoring)
  - [Configuration](#configuration)
- [FAQ](#faq)

## After a Reboot

```bash
docker compose up -d           # Gitea, router
python sandbox.py start --all  # Agent containers
python sandbox.py review on    # Reviewer (if previously configured)
```

To fully tear down everything (all agent containers, volumes, networks, and infrastructure):

```bash
python sandbox.py unsetup
```

This removes all agent containers and their workspace volumes, stops and removes Gitea/router/review containers and their Docker volumes, cleans up per-project networks, and removes the generated `GITEA_ADMIN_TOKEN` from `.env`. 
Your other `.env` settings are preserved — run `sandbox setup` to start fresh.

## Recreate vs Destroy

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


## Image Profiles

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

## `container/` Directory

Any files placed in `container/` are copied into each agent's home directory volume at `/home/agent/`.
Use this to provide config files or custom instructions to every agent. For key-based SSH access,
place your public key at `container/.ssh/authorized_keys`.

By default it ships with:
- `CLAUDE.md` — baseline agent instructions (git workflow, remotes, verification)
- `repo-watch.sh` — agentic loop script (see [Repo Watch](#repo-watch))
- `repo-watch-prompt.md` — prompt template for repo-watch
- `agent-watch.sh` — real-time viewer for agent activity (see [Monitoring](#monitoring))

## Git Remotes Inside the Container

Each agent container has two git remotes:
- **`origin`** — the agent's fork on Gitea (read-write)
- **`upstream`** — the mirror of the GitHub repo (read-only)

After you merge the agent's work on GitHub and the mirror syncs, the agent must `git fetch upstream && git merge upstream/main` before starting new work. 
This is **not done automatically** — it is up to the agent (or the user) to sync. 
The default `CLAUDE.md` instructs Claude Code to do this before each task.

## VS Code Remote-SSH

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

## Reviewer

The review service runs as a bot that responds to slash commands in PR comments. Comment `/security` on any PR to trigger an automated security review. It runs in a separate container with its own LLM API key — the agent never interacts with it.

Each bot command gets a dedicated Gitea user (`bot-security`, etc.). Duplicate reviews on the same commit are skipped automatically. The bot is extensible — new commands can be added to the `COMMANDS` dict in `review/review-server.py`.

The reviewer is managed independently from the core infrastructure via three commands:

```bash
python sandbox.py review setup   # Interactive: configure provider, key, model → starts service
python sandbox.py review on      # Start service, connect to all projects, add webhooks
python sandbox.py review off     # Remove webhooks, disconnect, stop service
```

`review setup` prompts for provider, API key, and model, creates the `bot-security` Gitea user, writes the config to `.env`, builds the review container, and connects it to all existing projects. After that, `review on` and `review off` toggle it without re-prompting.

The reviewer is **not started by `sandbox setup`**. After a reboot, bring it back with `sandbox review on`.
`sandbox unsetup` and `docker compose down` tear it down along with everything else.

To view an existing review for a branch:

```bash
python sandbox.py review show <project> <branch>
```

### Supported providers

| Provider | API key required | Endpoint |
|---|---|---|
| `anthropic` | Yes | `https://api.anthropic.com`|
| `openai` | Yes | `https://api.openai.com`|
| `openrouter` | Yes | `https://openrouter.ai/api`|
| `local` | No | Must set `REVIEWER_ENDPOINT` |

Default endpoints are in `review/review-config.yaml`. Override with `REVIEWER_ENDPOINT` in `.env`.

**Customizing the review prompt:** Edit `review/review-config.yaml`. The prompt must contain a `{diff}` placeholder.
Rebuild the review container after changes: `sandbox review off && sandbox review setup`.

## Repo Watch

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

### Stopping

- **Ctrl+C** in the repo-watch window kills both the script and the running Claude process.
- To kill only the current task without stopping the loop, find the claude PID from another byobu window and `kill` it. The loop will continue to the next cycle.

### Retry behavior

If Claude Code errors out, the script retries up to `MAX_RETRIES` times (default: 3)
before skipping that issue. A new human comment resets the counter.

## FAQ

### GPU / CUDA support?

Install the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) on the host and pass `--gpus all` when creating the project. 
Create a custom profile with a CUDA base image if your workload needs one — the toolkit mounts the host driver automatically.

### Rootless Docker support?

If you already run [rootless Docker](https://docs.docker.com/engine/security/rootless/), the sandbox works as-is with no changes.
The added benefit is that a container escape lands as your unprivileged user rather than root, and granted capabilities (CHOWN,SETUID, etc.) are scoped to a user namespace that can't affect the real host.
This doesn't prevent the escape itself, but limits the blast radius.
Not required — regular Docker with the network isolation above is the intended baseline.
