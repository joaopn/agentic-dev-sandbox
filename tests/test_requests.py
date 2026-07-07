"""from_kwargs validation matrices for every request dataclass.

Valid fixtures come from the repo itself: profile "python" is
agent/Dockerfile.python, agent "claude" is container/claude/ with an
installer. "opencode" has a container/ dir but no installer, so it must be
rejected pre-flight.
"""

import pytest

from cli.sandboxcore import (
    AttachRequest,
    CreateRequest,
    DestroyRequest,
    StartRequest,
    StopRequest,
    SyncRequest,
    ValidationError,
    WebportAddRequest,
    WebportListRequest,
    WebportRemoveRequest,
)


# ─── CreateRequest ────────────────────────────────────────────────────────────


def test_create_minimal_valid():
    req = CreateRequest.from_kwargs(github_url="https://github.com/user/myrepo")
    assert req.project == "myrepo"
    assert req.github_url == "https://github.com/user/myrepo"
    assert req.egress == ""
    assert req.ssh_port == 0
    assert req.docker is False


def test_create_strips_dot_git_suffix():
    req = CreateRequest.from_kwargs(github_url="https://github.com/user/myrepo.git")
    assert req.project == "myrepo"


def test_create_dotted_repo_name_allowed():
    req = CreateRequest.from_kwargs(github_url="https://github.com/vercel/next.js")
    assert req.project == "next.js"


def test_create_full_valid():
    req = CreateRequest.from_kwargs(
        github_url="https://github.com/user/myrepo", branch="dev",
        egress="open", memory="8g", cpus="1.5", gpus="all",
        profile="python", ssh_port=2222, agent="claude", docker=True)
    assert req.branch == "dev"
    assert req.egress == "open"
    assert req.memory == "8g"
    assert req.cpus == "1.5"
    assert req.profile == "python"
    assert req.ssh_port == 2222
    assert req.agent == "claude"
    assert req.docker is True


@pytest.mark.parametrize("url", [
    "", "github.com/user/repo", "ftp://github.com/user/repo",
    "git@github.com:user/repo.git", "https:///norepo", "https://",
])
def test_create_rejects_bad_url(url):
    with pytest.raises(ValidationError):
        CreateRequest.from_kwargs(github_url=url)


@pytest.mark.parametrize("url", [
    "https://github.com/user/.hidden",
    "https://github.com/user/-leading-dash",
    "https://github.com/user/has%20space",
])
def test_create_rejects_bad_derived_name(url):
    with pytest.raises(ValidationError):
        CreateRequest.from_kwargs(github_url=url)


def test_create_rejects_bad_egress():
    with pytest.raises(ValidationError):
        CreateRequest.from_kwargs(
            github_url="https://github.com/u/r", egress="yes")


@pytest.mark.parametrize("egress", ["", "locked", "open"])
def test_create_accepts_egress_enum(egress):
    req = CreateRequest.from_kwargs(github_url="https://github.com/u/r", egress=egress)
    assert req.egress == egress


@pytest.mark.parametrize("memory", ["512m", "8g", "1024", "2G", "1.5g"])
def test_create_accepts_docker_memory_formats(memory):
    assert CreateRequest.from_kwargs(
        github_url="https://github.com/u/r", memory=memory).memory == memory


@pytest.mark.parametrize("memory", ["8gb", "abc", "-1g", "g8", "8 g"])
def test_create_rejects_bad_memory(memory):
    with pytest.raises(ValidationError):
        CreateRequest.from_kwargs(github_url="https://github.com/u/r", memory=memory)


@pytest.mark.parametrize("cpus", ["1", "1.5", "0.5"])
def test_create_accepts_cpus(cpus):
    assert CreateRequest.from_kwargs(
        github_url="https://github.com/u/r", cpus=cpus).cpus == cpus


@pytest.mark.parametrize("cpus", ["x", "-1", "0"])
def test_create_rejects_bad_cpus(cpus):
    with pytest.raises(ValidationError):
        CreateRequest.from_kwargs(github_url="https://github.com/u/r", cpus=cpus)


@pytest.mark.parametrize("branch", ["main", "dev", "feature/x", "v1.2.3"])
def test_create_accepts_branch(branch):
    assert CreateRequest.from_kwargs(
        github_url="https://github.com/u/r", branch=branch).branch == branch


@pytest.mark.parametrize("branch", ["has space", "bad~ref", "a:b", "-flag", "a?b", "a*b"])
def test_create_rejects_bad_branch(branch):
    with pytest.raises(ValidationError):
        CreateRequest.from_kwargs(github_url="https://github.com/u/r", branch=branch)


@pytest.mark.parametrize("ssh_port", [-1, 70000, "2222", 1.5])
def test_create_rejects_bad_ssh_port(ssh_port):
    with pytest.raises(ValidationError):
        CreateRequest.from_kwargs(
            github_url="https://github.com/u/r", ssh_port=ssh_port)


def test_create_rejects_unknown_profile():
    with pytest.raises(ValidationError, match="Unknown profile"):
        CreateRequest.from_kwargs(
            github_url="https://github.com/u/r", profile="nope")


def test_create_rejects_unknown_agent():
    with pytest.raises(ValidationError, match="Unknown agent"):
        CreateRequest.from_kwargs(github_url="https://github.com/u/r", agent="nope")


def test_create_rejects_agent_without_installer():
    # container/opencode/ exists but INSTALLERS has no entry for it
    with pytest.raises(ValidationError, match="No installer"):
        CreateRequest.from_kwargs(
            github_url="https://github.com/u/r", agent="opencode")


# ─── {project}-only requests ──────────────────────────────────────────────────


@pytest.mark.parametrize("cls", [
    DestroyRequest, StartRequest, StopRequest, SyncRequest, AttachRequest,
    WebportListRequest,
])
def test_project_requests_accept_valid_name(cls):
    assert cls.from_kwargs(project="my-repo_1.x").project == "my-repo_1.x"


@pytest.mark.parametrize("cls", [
    DestroyRequest, StartRequest, StopRequest, SyncRequest, AttachRequest,
    WebportListRequest,
])
@pytest.mark.parametrize("project", ["", None, "-leading", ".hidden", "has space", "a/b"])
def test_project_requests_reject_bad_name(cls, project):
    with pytest.raises(ValidationError):
        cls.from_kwargs(project=project)


# ─── Webport requests ─────────────────────────────────────────────────────────


def test_webport_add_valid():
    req = WebportAddRequest.from_kwargs(project="proj", port=8080, label="Jupyter Lab")
    assert (req.project, req.port, req.label) == ("proj", 8080, "Jupyter Lab")


@pytest.mark.parametrize("port", [80, 1023, 65536, 0, -1, "8080", True])
def test_webport_rejects_bad_port(port):
    with pytest.raises(ValidationError):
        WebportAddRequest.from_kwargs(project="proj", port=port, label="ok")


@pytest.mark.parametrize("label", [
    "", None, "<script>", "a" * 33, " leading-space", "-leading-dash", "tab\tlabel",
])
def test_webport_rejects_bad_label(label):
    with pytest.raises(ValidationError):
        WebportAddRequest.from_kwargs(project="proj", port=8080, label=label)


def test_webport_label_max_length_boundary():
    WebportAddRequest.from_kwargs(project="proj", port=8080, label="a" * 32)
    with pytest.raises(ValidationError):
        WebportAddRequest.from_kwargs(project="proj", port=8080, label="a" * 33)


def test_webport_remove_valid():
    req = WebportRemoveRequest.from_kwargs(project="proj", port=1024)
    assert (req.project, req.port) == ("proj", 1024)
