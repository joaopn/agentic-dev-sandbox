"""broker_auth — credential + session-token logic for the broker daemon.

Stdlib-only (the broker runs in the host CLI's stdlib+PyYAML Python). Kept
separate from broker.py's socket/dispatch concerns so the security-critical
pieces — password hashing, the in-daemon token store, the audit sink — are
small and unit-testable on their own.

Three things live here:

  * **The single operator secret** — `sandbox.py broker passwd` sets it; it is
    hashed with stdlib scrypt into a versioned 0600 file. Verification is
    constant-time; a missing file fails closed (no password set ⇒ no login).
  * **The session-token store** — issued on login, held only in the daemon's
    memory (never on disk), keyed `token → (principal, expires_at)` with an
    absolute TTL. A restart flushes every token by construction.
  * **A minimal append-only audit sink** — one JSON line per auth / write
    event (timestamp + principal + verb + outcome), so a compromised or
    runaway caller leaves a trail. Injected into dispatch so dispatch itself
    stays pure (tests pass no sink).

This module does NOT import broker.py (broker imports it — importing back
would cycle), so it computes the repo-local state path independently.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

# Same repo-local host-side tree as the broker socket + webport registry
# (<repo>/.broker/, gitignored). Computed here, not imported from broker.py,
# to avoid an import cycle. MIRROR PAIR: must equal sandboxcore.BROKER_DIR —
# pinned by a pytest; drift puts the password hash / audit log outside the
# broker tree.
BROKER_DIR = Path(__file__).resolve().parent.parent / ".broker"
PASSWD_FILE = BROKER_DIR / "broker-passwd.json"
AUDIT_FILE = BROKER_DIR / "broker-audit.log"

# The single-admin identity baked now. The token is principal-bearing from day
# one (the seam for a later per-user authz step); today every valid token maps
# to this principal and the authz check-point answers "operator → all".
DEFAULT_PRINCIPAL = "operator"

# --- scrypt parameters --------------------------------------------------------
# Memory used ≈ 128 * N * r bytes. N=2**14, r=8 ⇒ ~16 MiB per verify: hard
# enough to make offline cracking of a leaked hash expensive, cheap enough to
# verify in well under 100 ms, and under OpenSSL's default 32 MiB `maxmem` (so
# no maxmem override is needed). Halving N halves the cracking cost for no
# latency win; a 10× N (~128 MiB) pushes verify past ~0.5 s and risks maxmem
# errors. p=1: no parallelism needed for an interactive single-secret login.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32          # 256-bit derived key
SALT_BYTES = 16            # 128-bit salt, fresh per set_password
PASSWD_FORMAT_VERSION = 1

# --- login-proof derivation (unified login, split derivations) ----------------
# The browser derives TWO independent values from the one master password: the
# vault key (PBKDF2, random per-vault salt, never transmitted) and this LOGIN
# PROOF — the only thing that transits browser → webui → broker. The salt here
# is a fixed, public, versioned domain-separation constant (the account-
# identifier salt pattern password managers use, degenerate single-account
# case; a multi-user step upgrades it to constant + username). It is not a
# secret — it only has to DIFFER from the vault derivation's salt so neither
# the webui nor the broker can compute the vault key from a relayed proof.
# The stored record still gets its own fresh random scrypt salt on top
# (set_password below), so a stolen passwd file stays per-record salted.
#
# MIRROR-PAIR LOCKSTEP: LOGIN_PROOF_SALT / LOGIN_PROOF_ITERATIONS must equal
# LOGIN_PROOF_SALT_STR / LOGIN_PROOF_ITERATIONS in webui/static/app.js. The
# webui cannot import this module; the bash acceptance harness + the pinned
# pytest vector keep the two implementations honest. 600k = the vault
# derivation's PBKDF2-SHA256 tier — the two sides MUST stay equal or every
# login fails.
LOGIN_PROOF_SALT = b"ads-broker-login-v1"
LOGIN_PROOF_ITERATIONS = 600_000
LOGIN_PROOF_DKLEN = 32     # 256-bit proof; canonical wire form is base64 of it

# One password gates BOTH the webui vault and the broker, so the two entry
# points must agree on the minimum or an operator can set a broker password
# the vault UI would refuse at create. 8 mirrors the vault-create gate in
# webui/static/app.js — change them together.
MIN_PASSWORD_LENGTH = 8

# --- session token ------------------------------------------------------------
# token_urlsafe(32) ⇒ 256 bits of entropy. TTL is one work day: log in once,
# reversible verbs only; a leaked token expires within the day and a broker
# restart flushes all tokens.
TOKEN_BYTES = 32
SESSION_TTL_SECONDS = 8 * 60 * 60


# ===========================================================================
# Password store
# ===========================================================================


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def _scrypt(password: str, salt: bytes, *, n: int, r: int, p: int,
            dklen: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt,
                          n=n, r=r, p=p, dklen=dklen)


def derive_login_proof(password: str) -> str:
    """The client-side login derivation: base64 (standard alphabet, padded) of
    PBKDF2-HMAC-SHA256(password, LOGIN_PROOF_SALT). This string — never the
    raw master password — is what clients send at login/step-up and what
    `sandbox.py broker passwd` stores (scrypt-hashed). Mirror of app.js's
    deriveLoginProof; the two must produce identical strings."""
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), LOGIN_PROOF_SALT,
        LOGIN_PROOF_ITERATIONS, dklen=LOGIN_PROOF_DKLEN)
    return _b64(digest)


def set_password(password: str, *, path: Path = PASSWD_FILE) -> None:
    """Hash `password` with a fresh salt and write the versioned 0600 record.
    Under unified login the stored secret is the LOGIN PROOF (derive_login_proof
    of the master password), never the raw password — this stays a generic
    string hasher; callers derive first. Raises ValueError on an empty password
    (an empty secret = no auth)."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                     dklen=SCRYPT_DKLEN)
    record = {
        "v": PASSWD_FORMAT_VERSION,
        "kdf": "scrypt",
        "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "dklen": SCRYPT_DKLEN,
        "salt": _b64(salt),
        "hash": _b64(digest),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        path.parent.chmod(0o700)
    # Write 0600 race-free via a restrictive umask around the create.
    old = os.umask(0o177)
    try:
        path.write_text(json.dumps(record))
    finally:
        os.umask(old)
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def password_is_set(*, path: Path = PASSWD_FILE) -> bool:
    return path.exists()


def verify_password(password: str, *, path: Path = PASSWD_FILE) -> bool:
    """Constant-time verify against the stored record. Fail closed: a missing
    or unreadable/garbled record returns False (never authenticates). Under
    unified login the verified string is the login proof, so a record hashed
    over a raw password simply stops verifying — the remedy is re-running
    `sandbox.py broker passwd`."""
    try:
        record = json.loads(path.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return False
    try:
        salt = _unb64(record["salt"])
        expected = _unb64(record["hash"])
        digest = _scrypt(password, salt,
                         n=int(record["n"]), r=int(record["r"]),
                         p=int(record["p"]), dklen=int(record["dklen"]))
    except (KeyError, ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


# ===========================================================================
# Session-token store (in-daemon, never persisted)
# ===========================================================================


class TokenStore:
    """token → (principal, expires_at), absolute TTL, GC-on-lookup. `now` is
    injectable purely so expiry is testable without sleeping."""

    def __init__(self, ttl: float = SESSION_TTL_SECONDS, now=time.time):
        self._ttl = ttl
        self._now = now
        self._tokens: dict[str, tuple[str, float]] = {}

    def issue(self, principal: str) -> tuple[str, float]:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        expires_at = self._now() + self._ttl
        self._tokens[token] = (principal, expires_at)
        return token, expires_at

    def principal_for(self, token) -> str | None:
        """Return the principal for a live token, else None. Expired tokens are
        dropped on the way out."""
        if not isinstance(token, str):
            return None
        entry = self._tokens.get(token)
        if entry is None:
            return None
        principal, expires_at = entry
        if self._now() >= expires_at:
            self._tokens.pop(token, None)
            return None
        return principal

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)


# ===========================================================================
# Audit sink (minimal, append-only)
# ===========================================================================


def audit_event(principal, verb, outcome, *, path: Path = AUDIT_FILE) -> None:
    """Append one JSON line: {ts, principal, verb, outcome}. Best-effort — an
    audit-write failure must never break request handling."""
    line = json.dumps({
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "principal": principal,
        "verb": verb,
        "outcome": outcome,
    })
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        old = os.umask(0o177)
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        finally:
            os.umask(old)
    except OSError:
        pass
