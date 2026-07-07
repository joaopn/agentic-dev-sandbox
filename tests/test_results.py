"""Construct every result dataclass and run the CLI formatters over them.

The formatters must render from result fields alone (no docker queries, no
filesystem walks) — that is the seam the broker relies on. These tests pin
the key lines of the output, including the secrets-only-in-create-summary
rule.
"""

import base64
import json

from cli.sandboxcore import (
    ActionResult,
    AgentStatus,
    AttachInfo,
    Config,
    CreateResult,
    DestroyResult,
    InfraStatus,
    ProjectSummary,
    StatusResult,
    SyncResult,
    Webport,
    WebportAddResult,
    WebportRemoveResult,
)
import sandbox


def make_cfg() -> Config:
    cfg = Config()
    cfg.gitea_port = "3000"
    cfg.gitea_admin_password = "adminpw"
    cfg.projects_dir = "/data/projects"
    return cfg


def make_create_result(**overrides) -> CreateResult:
    kw = dict(project="myrepo", container="sandbox-agent-myrepo",
              gitea_user="agent-myrepo", image="sandbox-agent-python:latest",
              profile="python", agent="claude", ssh_port=2222,
              ssh_password="sekret", egress="locked", base_branch="",
              docker=False)
    kw.update(overrides)
    return CreateResult(**kw)


# ─── Result dataclass construction ────────────────────────────────────────────


def test_construct_every_result_dataclass():
    make_create_result()
    DestroyResult(project="p", removed_container=True, removed_volume=True,
                  removed_workspace=False, removed_gitea_user=True,
                  removed_mirror=True)
    ActionResult(project="p", container="sandbox-agent-p", action="stop", ok=True)
    SyncResult(project="p", mirrored=True, pulled=False)
    ProjectSummary(project="p", state="running", ssh_port="2222")
    InfraStatus(name="sandbox-gitea", state="running")
    AgentStatus(project="p", state="running", ssh_port="2222",
                port_bind="127.0.0.1", port_mappings=[(8080, 8080)])
    StatusResult(infra=[], ci_test_lines=[], agents=[], projects_dir="",
                 project_dirs=[])
    AttachInfo(project="p", host="sandbox-agent-p", port=22,
               username="agent", password="sekret")
    Webport(port=8080, label="Jupyter")
    WebportAddResult(project="p", port=8080, label="Jupyter")
    WebportRemoveResult(project="p", port=8080)


def test_project_summary_has_no_password_field():
    # list/status are OPEN verbs — their results must never carry credentials.
    assert "password" not in ProjectSummary.__dataclass_fields__
    assert not any("password" in f for f in StatusResult.__dataclass_fields__)


# ─── format_create_summary ────────────────────────────────────────────────────


def test_create_summary_key_lines():
    cfg = make_cfg()
    out = sandbox.format_create_summary(make_create_result(), cfg)
    assert out.startswith("\n=== Sandbox ready: myrepo ===")
    assert "ssh agent@localhost -p 2222  (password: sekret)" in out
    assert "Gitea:     http://localhost:3000/agent-myrepo/myrepo" in out
    assert "Gitea login: sandbox-admin / adminpw" in out
    assert "Egress:    locked (80/443/DNS only)" in out
    assert "Base branch" not in out  # empty base_branch ⇒ no line


def test_create_summary_docker_and_branch_labels():
    cfg = make_cfg()
    out = sandbox.format_create_summary(
        make_create_result(docker=True, base_branch="dev", egress="open"), cfg)
    assert "=== Sandbox ready: myrepo === (Docker-in-Docker)" in out
    assert "\nBase branch: dev" in out
    assert "Egress:    open (all ports)" in out


def test_create_summary_import_string_decodes():
    cfg = make_cfg()
    out = sandbox.format_create_summary(make_create_result(), cfg)
    import_line = [l for l in out.splitlines() if l.startswith("  ")][0]
    decoded = json.loads(base64.b64decode(import_line.strip()))
    assert decoded == {"name": "myrepo", "host": "sandbox-agent-myrepo",
                       "port": 22, "username": "agent", "password": "sekret"}


# ─── format_status ────────────────────────────────────────────────────────────


def make_status_result(**overrides) -> StatusResult:
    kw = dict(
        infra=[InfraStatus(name="sandbox-gitea", state="running"),
               InfraStatus(name="sandbox-router", state="running")],
        ci_test_lines=["sandbox-ci-test-1\tUp 2 minutes\tpr-7"],
        agents=[AgentStatus(project="myrepo", state="running", ssh_port="2222",
                            port_bind="127.0.0.1", port_mappings=[(8080, 80)]),
                AgentStatus(project="other", state="exited", ssh_port="-",
                            port_bind="", port_mappings=[])],
        projects_dir="/data/projects",
        project_dirs=["myrepo", "other"],
    )
    kw.update(overrides)
    return StatusResult(**kw)


def test_status_full_render():
    cfg = make_cfg()
    out = sandbox.format_status(make_status_result(), cfg, ci_pid=None)
    assert out.startswith("=== Sandbox Status ===\n\n── Infrastructure ──")
    assert "  sandbox-gitea        running" in out
    assert "  Gitea login: sandbox-admin / adminpw" in out
    assert "── CI Watch ──" in out
    assert "  Status:    not configured" in out
    assert "── Active CI Tests ──" in out
    assert "sandbox-ci-test-1" in out and "pr-7" in out
    assert "── Agent Containers ──" in out
    # Column layout: PROJECT padded to longest name + 4
    assert "  PROJECT     STATE       SSH PORT    PORTS" in out
    assert "  myrepo      running     2222        127.0.0.1 -> 8080" in out
    assert "  other       exited      -           -" in out
    assert "── Projects Directory ──\n  /data/projects" in out
    assert "    myrepo/" in out and "    other/" in out


def test_status_empty_agents_and_default_volumes():
    cfg = make_cfg()
    cfg.gitea_admin_password = ""
    out = sandbox.format_status(
        make_status_result(agents=[], ci_test_lines=[], projects_dir="",
                           project_dirs=[]),
        cfg, ci_pid=None)
    assert "  (no agent containers)" in out
    assert "── Active CI Tests ──" not in out
    assert "Gitea login" not in out
    assert "── Projects Directory ──\n  (standard Docker volumes)" in out


def test_status_ci_watch_running_section():
    cfg = make_cfg()
    out = sandbox.format_status(make_status_result(), cfg, ci_pid=4242)
    assert "  Status:    running (PID 4242)" in out
    assert "  Poll:      every 5s" in out
    assert "  Commands:  /test-pr, /test-pr-bug" in out


def test_status_ci_watch_configured_not_running():
    cfg = make_cfg()
    cfg.ci_watch_enabled = True
    out = sandbox.format_status(make_status_result(), cfg, ci_pid=None)
    assert "  Status:    configured but not running" in out
