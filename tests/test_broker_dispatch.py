"""dispatch() gate order, envelope kinds, op-log gate-before-sink, verb-table
invariants, and the op-log primitives — all pure (no socket, no docker):
dispatch takes injected verb tables, token stores, audit and oplog sinks."""

import io
import json

import pytest

from cli import broker, broker_auth
from cli import sandboxcore as core


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _ok_verb(args, progress=None):
    return {"ran": True, "args": args}


def _tokens_with_session():
    store = broker_auth.TokenStore()
    token, _ = store.issue("operator")
    return store, token


class _FakeOpLog:
    """Duck-typed _OpLog: records milestones + teed text in memory."""

    def __init__(self):
        self.events = []
        self.full = io.StringIO()
        self.closed = False
        outer = self

        class _P:
            def step(self, key, msg=""):
                outer.events.append(("step", key, msg))

            def done(self, msg=""):
                outer.events.append(("done", "done", msg))

            def fail(self, msg=""):
                outer.events.append(("failed", "failed", msg))

        self.progress = _P()

    def close(self):
        self.closed = True


class _OpLogSpy:
    def __init__(self):
        self.calls = []
        self.op = _FakeOpLog()

    def __call__(self, op_id, verb, args):
        self.calls.append((op_id, verb, args))
        return self.op


# ─── Gate order ───────────────────────────────────────────────────────────────


def test_open_verbs_run_without_token():
    reply = broker.dispatch("list", {}, None, None, verbs={"list": _ok_verb})
    assert reply["ok"] is True


def test_gated_verb_without_token_unauthorized():
    reply = broker.dispatch("stop", {"project": "p"}, None,
                            broker_auth.TokenStore(), verbs={"stop": _ok_verb})
    assert reply["ok"] is False
    assert reply["error"]["kind"] == "unauthorized"


def test_gated_verb_with_bad_token_unauthorized():
    tokens, _ = _tokens_with_session()
    reply = broker.dispatch("stop", {"project": "p"}, "not-a-real-token",
                            tokens, verbs={"stop": _ok_verb})
    assert reply["error"]["kind"] == "unauthorized"


def test_destroy_without_token_is_plain_unauthorized():
    """Token gate BEFORE step-up: an unauthenticated caller must never learn
    the step-up gate exists."""
    reply = broker.dispatch("destroy", {"project": "p", "proof": "x"}, None,
                            broker_auth.TokenStore(),
                            verbs={"destroy": _ok_verb})
    assert reply["error"]["kind"] == "unauthorized"


def test_destroy_with_token_but_no_proof_step_up_required(monkeypatch):
    monkeypatch.setattr(broker_auth, "verify_password",
                        lambda pw, **kw: False)
    tokens, token = _tokens_with_session()
    reply = broker.dispatch("destroy", {"project": "p"}, token, tokens,
                            verbs={"destroy": _ok_verb})
    assert reply["error"]["kind"] == "step_up_required"


def test_destroy_with_token_and_proof_runs(monkeypatch):
    monkeypatch.setattr(broker_auth, "verify_password",
                        lambda pw, **kw: pw == "good-proof")
    tokens, token = _tokens_with_session()
    reply = broker.dispatch("destroy", {"project": "p", "proof": "good-proof"},
                            token, tokens, verbs={"destroy": _ok_verb})
    assert reply["ok"] is True


def test_unknown_verb():
    reply = broker.dispatch("nope", {}, None, None, verbs={"list": _ok_verb})
    assert reply["error"]["kind"] == "unknown_verb"


def test_non_dict_args_bad_request():
    reply = broker.dispatch("list", "not-a-dict", None, None,
                            verbs={"list": _ok_verb})
    assert reply["error"]["kind"] == "bad_request"


# ─── Envelope kinds (the three channels + the stray-exception backstop) ──────


def test_validation_error_envelope():
    def verb(args, progress=None):
        raise core.ValidationError("bad input value")
    reply = broker.dispatch("list", {}, None, None, verbs={"list": verb})
    assert reply["error"]["kind"] == "validation"
    assert reply["error"]["message"] == "bad input value"


def test_harness_error_envelope_uses_client_detail():
    def verb(args, progress=None):
        raise core.HarnessError("gitea-step", "scrubbed detail for the client")
    reply = broker.dispatch("list", {}, None, None, verbs={"list": verb})
    assert reply["error"]["kind"] == "failed"
    assert reply["error"]["message"] == "scrubbed detail for the client"


def test_die_envelope_trims_real_prefix():
    """Drives the REAL core.die() so the 'Error: ' prefix ↔ dispatch trim
    mirror pair is pinned by execution — if either side changes, this fails."""
    def verb(args, progress=None):
        core.die("boom")
    reply = broker.dispatch("list", {}, None, None, verbs={"list": verb})
    assert reply["error"]["kind"] == "failed"
    assert reply["error"]["message"] == "boom"


def test_stray_exception_mapped_not_escaped(capsys):
    """The blanket backstop deferred from the core split: a non-channel
    exception must produce a coarse envelope (no str(e)), never escape
    dispatch (which would truncate the client reply)."""
    def verb(args, progress=None):
        raise RuntimeError("secret-bearing detail must not leak")
    reply = broker.dispatch("list", {}, None, None, verbs={"list": verb})
    assert reply["error"]["kind"] == "failed"
    assert reply["error"]["message"] == "internal error (RuntimeError); see broker.log"
    assert "secret-bearing" not in reply["error"]["message"]
    # Traceback lands on the (restored) stderr = broker.log in the daemon.
    assert "RuntimeError" in capsys.readouterr().err


# ─── Auth path ────────────────────────────────────────────────────────────────


def test_login_success_issues_token(monkeypatch):
    monkeypatch.setattr(broker_auth, "verify_password",
                        lambda pw, **kw: pw == "the-proof")
    tokens = broker_auth.TokenStore()
    reply = broker.dispatch("login", {"proof": "the-proof"}, None, tokens)
    assert reply["ok"] is True
    assert reply["result"]["principal"] == "operator"
    assert tokens.principal_for(reply["result"]["token"]) == "operator"


def test_login_bad_proof(monkeypatch):
    monkeypatch.setattr(broker_auth, "verify_password", lambda pw, **kw: False)
    reply = broker.dispatch("login", {"proof": "wrong"}, None,
                            broker_auth.TokenStore())
    assert reply["error"]["kind"] == "auth"


def test_login_non_string_proof(monkeypatch):
    monkeypatch.setattr(broker_auth, "verify_password",
                        lambda pw, **kw: True)   # must not even be consulted path-wise
    reply = broker.dispatch("login", {"proof": 42}, None,
                            broker_auth.TokenStore())
    assert reply["error"]["kind"] == "auth"


def test_logout_revokes_presented_token():
    tokens, token = _tokens_with_session()
    reply = broker.dispatch("logout", {}, token, tokens)
    assert reply["result"]["logged_out"] is True
    assert tokens.principal_for(token) is None
    # Idempotent on an already-revoked/unknown token.
    reply = broker.dispatch("logout", {}, token, tokens)
    assert reply["result"]["logged_out"] is True


# ─── Audit ────────────────────────────────────────────────────────────────────


def test_audit_outcomes(monkeypatch):
    monkeypatch.setattr(broker_auth, "verify_password",
                        lambda pw, **kw: pw == "ok-proof")
    events = []
    audit = lambda principal, verb, outcome: events.append(
        (principal, verb, outcome))
    tokens = broker_auth.TokenStore()

    broker.dispatch("list", {}, None, tokens,
                    verbs={"list": _ok_verb, "stop": _ok_verb}, audit=audit)
    assert events == []                            # open reads not audited

    broker.dispatch("stop", {}, None, tokens, verbs={"stop": _ok_verb},
                    audit=audit)
    assert events[-1] == (None, "stop", "unauthorized")

    reply = broker.dispatch("login", {"proof": "ok-proof"}, None, tokens,
                            audit=audit)
    assert events[-1] == ("operator", "login", "ok")
    token = reply["result"]["token"]

    broker.dispatch("stop", {}, token, tokens, verbs={"stop": _ok_verb},
                    audit=audit)
    assert events[-1] == ("operator", "stop", "ok")

    broker.dispatch("login", {"proof": "bad"}, None, tokens, audit=audit)
    assert events[-1] == (None, "login", "auth_fail")

    broker.dispatch("logout", {}, token, tokens, audit=audit)
    assert events[-1] == ("operator", "logout", "logout")


# ─── Op-log: gate-before-sink + dispatcher-owned terminals ───────────────────


def test_oplog_not_created_for_unauthorized_caller():
    spy = _OpLogSpy()
    broker.dispatch("stop", {"project": "p"}, None, broker_auth.TokenStore(),
                    op_id="p-stop-1", verbs={"stop": _ok_verb}, oplog=spy)
    assert spy.calls == []


def test_oplog_not_created_on_step_up_rejection(monkeypatch):
    monkeypatch.setattr(broker_auth, "verify_password", lambda pw, **kw: False)
    tokens, token = _tokens_with_session()
    spy = _OpLogSpy()
    broker.dispatch("destroy", {"project": "p"}, token, tokens,
                    op_id="p-destroy-1", verbs={"destroy": _ok_verb}, oplog=spy)
    assert spy.calls == []


def test_oplog_success_gets_dispatcher_done():
    tokens, token = _tokens_with_session()
    spy = _OpLogSpy()
    reply = broker.dispatch("stop", {"project": "p"}, token, tokens,
                            op_id="p-stop-1", verbs={"stop": _ok_verb},
                            oplog=spy)
    assert reply["ok"] is True
    assert spy.calls == [("p-stop-1", "stop", {"project": "p"})]
    assert spy.op.events[-1] == ("done", "done", "")
    assert spy.op.closed


def test_oplog_failure_gets_dispatcher_fail():
    def verb(args, progress=None):
        raise core.ValidationError("nope")
    tokens, token = _tokens_with_session()
    spy = _OpLogSpy()
    broker.dispatch("stop", {"project": "p"}, token, tokens,
                    op_id="p-stop-1", verbs={"stop": verb}, oplog=spy)
    assert spy.op.events[-1] == ("failed", "failed", "nope")
    assert spy.op.closed


def test_oplog_malformed_op_id_bad_request_no_file():
    tokens, token = _tokens_with_session()
    reply = broker.dispatch("stop", {"project": "p"}, token, tokens,
                            op_id="../evil", verbs={"stop": _ok_verb},
                            oplog=broker.make_oplog)
    assert reply["error"]["kind"] == "bad_request"


def test_oplog_only_for_progress_verbs():
    tokens, token = _tokens_with_session()
    spy = _OpLogSpy()
    broker.dispatch("webport_add", {"project": "p"}, token, tokens,
                    op_id="p-webport-1", verbs={"webport_add": _ok_verb},
                    oplog=spy)
    assert spy.calls == []                        # not in PROGRESS_VERBS


def test_verb_stdout_tees_to_full_log():
    def verb(args, progress=None):
        print("chatty step output")
        return {}
    tokens, token = _tokens_with_session()
    spy = _OpLogSpy()
    broker.dispatch("stop", {"project": "p"}, token, tokens,
                    op_id="p-stop-1", verbs={"stop": verb}, oplog=spy)
    assert "chatty step output" in spy.op.full.getvalue()


# ─── _OP_ID_RE matrix ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("op_id", [
    "a", "proj-stop-1751900000-ab12", "A.b_c-1", "0leading-digit",
])
def test_op_id_accepts(op_id):
    assert broker._OP_ID_RE.match(op_id)


@pytest.mark.parametrize("op_id", [
    "", ".hidden", "..", "a/b", "/abs", "-dash", "_score", "a b", "../up",
    "a\nb",
])
def test_op_id_rejects(op_id):
    assert not broker._OP_ID_RE.match(op_id)


# ─── Real op-log files (make_oplog / _Progress / _Tee) ────────────────────────


@pytest.fixture
def oplog_dirs(scratch_dir, monkeypatch):
    view = scratch_dir / "run" / "oplogs"
    full = scratch_dir / "oplogs-full"
    monkeypatch.setattr(broker, "BROKER_OPLOG_DIR", view)
    monkeypatch.setattr(broker, "BROKER_FULLLOG_DIR", full)
    return view, full


def test_make_oplog_writes_both_files(oplog_dirs):
    view, full = oplog_dirs
    op = broker.make_oplog("p-stop-1", "stop", {"project": "p"})
    op.progress.step("validate")
    op.progress.step("stop", "stopping container")
    op.progress.done()
    op.full.write("raw verb output\n")
    op.close()
    lines = [json.loads(line) for line in
             (view / "p-stop-1.view.log").read_text().splitlines()]
    assert [(r["status"], r["step"]) for r in lines] == \
        [("step", "validate"), ("step", "stop"), ("done", "done")]
    assert all(r["project"] == "p" and r["action"] == "stop" for r in lines)
    assert (full / "p-stop-1.full.log").read_text() == "raw verb output\n"


def test_make_oplog_rejects_bad_op_id_without_files(oplog_dirs):
    view, full = oplog_dirs
    with pytest.raises(ValueError):
        broker.make_oplog("../evil", "stop", {"project": "p"})
    assert not view.exists() and not full.exists()


@pytest.mark.parametrize("verb,args,label", [
    ("create", {"github_url": "https://github.com/u/repo.git"}, "repo"),
    ("create", {}, None),
    ("stop", {"project": "p1"}, "p1"),
])
def test_make_oplog_project_label(oplog_dirs, verb, args, label):
    op = broker.make_oplog("op-1", verb, args)
    op.close()
    assert op.progress.project == label


def test_tee_fans_out_and_survives_flush_errors():
    a, b = io.StringIO(), io.StringIO()

    class NoFlush:
        def write(self, s):
            return len(s)

        def flush(self):
            raise OSError("flush fails")

    tee = broker._Tee(a, b, NoFlush())
    tee.write("hello")
    tee.flush()                                    # must not raise
    assert a.getvalue() == b.getvalue() == "hello"


# ─── Verb-table invariants (plan §5 pins) ────────────────────────────────────


def test_verb_tables_shape():
    assert set(broker.VERBS) == {
        "list", "status", "catalog", "attach", "start", "stop", "sync",
        "create", "destroy", "webport_add", "webport_remove", "webport_list"}
    assert broker.OPEN_VERBS == frozenset({"list", "status"})
    assert broker.STEP_UP_VERBS == frozenset({"destroy"})
    assert broker.AUTH_VERBS == frozenset({"login", "logout"})
    assert broker.PROGRESS_VERBS == frozenset(
        {"create", "destroy", "start", "stop", "sync"})
    assert broker.OPEN_VERBS <= set(broker.VERBS)
    assert broker.STEP_UP_VERBS <= set(broker.VERBS)
    assert broker.PROGRESS_VERBS <= set(broker.VERBS)
    assert not (broker.AUTH_VERBS & set(broker.VERBS))


def test_create_field_allowlist_exact():
    """Deny-by-default boundary: exactly the 8 in-container fields; the
    host-shaped ssh_port/gpus must never appear here."""
    assert broker.CREATE_WEBUI_FIELDS == frozenset({
        "github_url", "branch", "egress", "memory", "cpus", "profile",
        "agent", "docker"})
    assert broker.WEBPORT_ADD_FIELDS == frozenset({"project", "port", "label"})
    assert broker.WEBPORT_TARGET_FIELDS == frozenset({"project", "port"})


def test_progress_verbs_match_core_keys():
    """The broker's op-log verbs are exactly the verbs the core emits
    milestone keys for (the webui checklist lockstep anchor)."""
    assert broker.PROGRESS_VERBS == frozenset(core.PROGRESS_KEYS)


# ─── catalog verb ─────────────────────────────────────────────────────────────


def test_list_profiles_is_the_single_enumeration():
    """python/cuda in, helper scripts (.sh) out, sorted — the one home the
    catalog verb and both error paths read."""
    profiles = core.list_profiles()
    assert "python" in profiles and "cuda" in profiles
    assert not any(p.endswith(".sh") for p in profiles)
    assert profiles == sorted(profiles)


def test_catalog_is_token_gated():
    reply = broker.dispatch("catalog", {}, None, broker_auth.TokenStore())
    assert reply["error"]["kind"] == "unauthorized"


def test_catalog_contents():
    """Filesystem enums only (no docker): profiles from agent/Dockerfile.*,
    agents from INSTALLERS — never list_agents(), which would advertise the
    installer-less opencode that create rejects."""
    tokens, token = _tokens_with_session()
    reply = broker.dispatch("catalog", {}, token, tokens)
    assert reply["ok"] is True
    result = reply["result"]
    assert "python" in result["profiles"] and "cuda" in result["profiles"]
    assert set(result["agents"]) == set(core.INSTALLERS)
    assert "claude" in result["agents"] and "goose" in result["agents"]
    assert "opencode" not in result["agents"]
    assert result["profiles"] == sorted(result["profiles"])
