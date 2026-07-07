"""Pin the ci-watch.py import contract.

ci-watch.py does `from sandbox import (...)` for exactly these nine names.
After the core split, most of them live in cli/sandboxcore.py and reach
ci-watch only through sandbox.py's explicit import block — deleting an
"unused-looking" import there breaks ci-watch at startup. This test is the
tripwire.
"""

import sandbox

# Keep in lockstep with the import block at ci-watch.py:29.
CI_WATCH_IMPORTS = [
    "SCRIPT_DIR",
    "Config",
    "die",
    "gen_password",
    "gitea_api",
    "gitea_api_ok",
    "http_basic_auth_request",
    "load_config",
    "update_env_key",
]


def test_ci_watch_names_resolve_on_sandbox():
    for name in CI_WATCH_IMPORTS:
        assert hasattr(sandbox, name), f"ci-watch.py imports sandbox.{name}"


def test_ci_watch_import_list_matches_source():
    """Parse ci-watch.py's actual import block so the lockstep can't drift."""
    import ast
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    tree = ast.parse((repo_root / "ci-watch.py").read_text())
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "sandbox"
        for alias in node.names
    ]
    assert sorted(imported) == sorted(CI_WATCH_IMPORTS)
