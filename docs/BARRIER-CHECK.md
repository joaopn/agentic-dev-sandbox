# Barrier Check

[◾ Overview](#-overview)
[◾ What it checks](#-what-it-checks)
[◾ Why not active exploitation or agentic testing?](#-why-not-active-exploitation-or-agentic-testing)

---

## ◾ Overview

`barrier-check.sh` is a passive, read-only security posture checker. It verifies that every barrier in the [threat model](SECURITY.md) exists and is correctly configured. All checks are procfs reads, file existence tests, or connection attempts that timeout — safe to run anywhere.

It can be run on the host, where it likely detects the unmitigated attack surface (Docker socket, open egress, no seccomp). Running on the container should show all barrier active.

```bash
# On the host (expect failures — that's the point)
bash barrier-check.sh

# Inside a sandbox container (auto-detects, runs full suite)
bash barrier-check.sh
```

Container detection is automatic (`/.dockerenv`). Inside a container, additional checks run: Deepce, LinPEAS, Gitea access controls, PID limits, mount types, expected users and environment. Output goes to `barrier-check-results/` next to the script.

Results
- [python profile](img/barrier-check-python.png)
- [cuda profile](img/barrier-check-cuda.png)

Results can change as base images get updated (in particular, LinPEAS CVEs).


## ◾ What it checks

**Universal checks** (host and container):

| Category | Checks |
|---|---|
| Capabilities | Dangerous caps absent from CapEff (SYS_ADMIN, NET_ADMIN, SYS_PTRACE, SYS_MODULE, SYS_RAWIO) |
| Isolation | Seccomp filtering, user namespace creation blocked, PID 1 is not host init |
| Filesystem | No Docker/containerd socket, no DOCKER_HOST env |
| Credentials | No SSH private keys, cloud creds (AWS/GCP/Azure), Docker/npm/PyPI auth, K8s tokens, GPG keys, gh CLI, leaked git credentials |
| Network | RFC1918 unreachable, Docker bridge unreachable, metadata service unreachable, port filtering active |

**Container-only checks** (auto-detected):

| Category | Checks |
|---|---|
| [Deepce](https://github.com/stealthcopter/deepce) | Docker socket, privileged mode, dangerous capabilities (cross-referenced against CapBnd), Docker group, known CVEs |
| [LinPEAS](https://github.com/peass-ng/PEASS-ng/tree/master/linPEAS) | Container escape vectors, SUID/SGID binaries, writable sensitive files, CVE matches with status classification |
| Sandbox config | PID limit (512), only `agent` user, home is Docker volume, no host bind mounts, routing via router container, Gitea admin API blocked, only own repos visible, Docker API unreachable, no unexpected secret env vars |

## ◾ Why not active exploitation or agentic testing?

**Active exploitation** ([CDK](https://github.com/cdk-team/CDK), [BOtB](https://github.com/brompwnie/botb)) attempts real escapes: cgroup release agent abuse, device mounting, Docker socket exploitation. Assuming barriers will contain the exploit is circular reasoning — if we trust them enough to run the tool, we don't need it; if we don't, the tool is dangerous. Safe active testing requires a disposable VM with the full stack deployed inside, an external observer, and teardown after each run.

**Agentic testing** (`claude -p "escape this container"`) has additional problems: verification requires external observers, adversarial prompts written by the barrier authors check only what the authors know to check, and LLM behavior varies by model version. Infrastructure barriers are deterministic; agent behavior is not.
