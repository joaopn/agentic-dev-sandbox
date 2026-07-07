"""broker_auth unit vectors: login-proof derivation (the app.js mirror pair),
scrypt password store (fail-closed), TokenStore, audit sink, and the
BROKER_DIR lockstep with the core."""

import base64
import json
import stat

import pytest

from cli import broker_auth, sandboxcore


# ─── Login proof (mirror pair with webui/static/app.js) ──────────────────────


def test_login_proof_pinned_vector():
    """Known-answer vector for the client-side derivation. The webui's
    deriveLoginProof (Stage 3) must produce this exact string for this exact
    password, or every login fails — this literal is the host-side anchor of
    that mirror pair."""
    assert (broker_auth.derive_login_proof("stage2-pinned-vector")
            == "uwaugvGTPvYtj9udyrTs2MjL3Wgq09H7UW2rmUfBNuw=")


def test_login_proof_wire_form():
    proof = broker_auth.derive_login_proof("stage2-pinned-vector")
    # Canonical wire form: PADDED standard-alphabet base64 of 32 PBKDF2 bytes.
    assert proof.endswith("=")
    assert len(base64.b64decode(proof)) == broker_auth.LOGIN_PROOF_DKLEN == 32


def test_login_proof_mirror_constants():
    """The app.js side hardcodes these; drift ⇒ every login fails."""
    assert broker_auth.LOGIN_PROOF_SALT == b"ads-broker-login-v1"
    assert broker_auth.LOGIN_PROOF_ITERATIONS == 600_000
    assert broker_auth.MIN_PASSWORD_LENGTH == 8


def test_broker_dir_lockstep():
    """broker_auth computes .broker/ independently (import-cycle avoidance);
    it must equal the core's or passwd/audit land outside the broker tree."""
    assert broker_auth.BROKER_DIR == sandboxcore.BROKER_DIR


# ─── Password store ───────────────────────────────────────────────────────────


def test_set_verify_roundtrip(scratch_dir):
    path = scratch_dir / "passwd.json"
    broker_auth.set_password("some-stored-proof", path=path)
    assert broker_auth.password_is_set(path=path)
    assert broker_auth.verify_password("some-stored-proof", path=path)
    assert not broker_auth.verify_password("wrong", path=path)


def test_set_password_file_is_0600_and_versioned(scratch_dir):
    path = scratch_dir / "passwd.json"
    broker_auth.set_password("s3cret-proof", path=path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    record = json.loads(path.read_text())
    assert record["v"] == broker_auth.PASSWD_FORMAT_VERSION
    assert record["kdf"] == "scrypt"
    assert set(record) >= {"n", "r", "p", "dklen", "salt", "hash"}


def test_set_password_rejects_empty(scratch_dir):
    with pytest.raises(ValueError):
        broker_auth.set_password("", path=scratch_dir / "passwd.json")


def test_verify_fails_closed_missing_file(scratch_dir):
    assert not broker_auth.verify_password("anything",
                                           path=scratch_dir / "absent.json")


def test_verify_fails_closed_garbled_record(scratch_dir):
    path = scratch_dir / "passwd.json"
    path.write_text("{not json")
    assert not broker_auth.verify_password("anything", path=path)
    path.write_text(json.dumps({"v": 1, "kdf": "scrypt"}))  # missing fields
    assert not broker_auth.verify_password("anything", path=path)


# ─── TokenStore ───────────────────────────────────────────────────────────────


def test_token_issue_and_lookup():
    store = broker_auth.TokenStore()
    token, expires_at = store.issue("operator")
    assert store.principal_for(token) == "operator"
    assert expires_at > 0


def test_token_expiry_via_injected_now():
    clock = [1000.0]
    store = broker_auth.TokenStore(ttl=60, now=lambda: clock[0])
    token, expires_at = store.issue("operator")
    assert expires_at == 1060.0
    clock[0] = 1059.9
    assert store.principal_for(token) == "operator"
    clock[0] = 1060.0
    assert store.principal_for(token) is None      # dropped on the way out
    clock[0] = 1000.0
    assert store.principal_for(token) is None      # GC'd, not resurrected


def test_token_revoke_and_bad_inputs():
    store = broker_auth.TokenStore()
    token, _ = store.issue("operator")
    store.revoke(token)
    assert store.principal_for(token) is None
    store.revoke(token)                            # idempotent
    assert store.principal_for(None) is None
    assert store.principal_for(12345) is None
    assert store.principal_for("unknown") is None


# ─── Audit sink ───────────────────────────────────────────────────────────────


def test_audit_event_appends_jsonl(scratch_dir):
    path = scratch_dir / "audit.log"
    broker_auth.audit_event("operator", "stop", "ok", path=path)
    broker_auth.audit_event(None, "login", "auth_fail", path=path)
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert (first["principal"], first["verb"], first["outcome"]) == \
        ("operator", "stop", "ok")
    assert "ts" in first
    assert json.loads(lines[1])["principal"] is None


def test_audit_event_best_effort_on_unwritable_path(scratch_dir):
    blocker = scratch_dir / "file"
    blocker.write_text("")
    # Parent "directory" is a regular file → OSError inside; must not raise.
    broker_auth.audit_event("operator", "stop", "ok",
                            path=blocker / "audit.log")
