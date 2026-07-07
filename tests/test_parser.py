"""Parser dispatch: parse_args(...).func is cmd_* for every subcommand.

py_compile passes on runtime NameErrors; this is the net that actually
exercises the parser wiring after the core split.
"""

import pytest

import sandbox

CASES = [
    (["setup"], sandbox.cmd_setup),
    (["unsetup"], sandbox.cmd_unsetup),
    (["up"], sandbox.cmd_up),
    (["down"], sandbox.cmd_down),
    (["ci-watch", "setup"], sandbox.cmd_ci_watch),
    (["ci-watch", "start"], sandbox.cmd_ci_watch),
    (["ci-watch", "stop"], sandbox.cmd_ci_watch),
    (["webui", "start"], sandbox.cmd_webui),
    (["webui", "start", "--bind", "100.72.206.77"], sandbox.cmd_webui),
    (["webui", "stop"], sandbox.cmd_webui),
    (["webui", "status"], sandbox.cmd_webui),
    (["webui", "import"], sandbox.cmd_webui),
    (["webui", "import", "proj"], sandbox.cmd_webui),
    (["broker", "start"], sandbox.cmd_broker),
    (["broker", "stop"], sandbox.cmd_broker),
    (["broker", "status"], sandbox.cmd_broker),
    (["broker", "serve"], sandbox.cmd_broker),
    (["broker", "passwd"], sandbox.cmd_broker),
    (["create", "https://github.com/u/r"], sandbox.cmd_create),
    (["create", "https://github.com/u/r", "--branch", "dev", "--open-egress",
      "--memory", "8g", "--cpus", "2", "--gpus", "all", "--profile", "python",
      "--ssh-port", "2222", "--agent", "claude", "--docker"], sandbox.cmd_create),
    (["attach", "proj"], sandbox.cmd_attach),
    (["ssh"], sandbox.cmd_ssh),
    (["stop", "proj"], sandbox.cmd_stop),
    (["stop", "--all"], sandbox.cmd_stop),
    (["start", "proj"], sandbox.cmd_start),
    (["start", "--all"], sandbox.cmd_start),
    (["pause", "proj"], sandbox.cmd_pause),
    (["unpause", "proj"], sandbox.cmd_unpause),
    (["sync", "proj"], sandbox.cmd_sync),
    (["push-context", "proj"], sandbox.cmd_push_context),
    (["push-context", "proj", "--include", "plans/", "--dry-run"], sandbox.cmd_push_context),
    (["pull-context", "proj", "--overwrite"], sandbox.cmd_pull_context),
    (["set-branch", "proj", "dev"], sandbox.cmd_set_branch),
    (["recreate", "proj"], sandbox.cmd_recreate),
    (["status"], sandbox.cmd_status),
    (["destroy", "proj"], sandbox.cmd_destroy),
    (["port", "proj", "--list"], sandbox.cmd_port),
    (["port", "proj", "--open", "8080:80"], sandbox.cmd_port),
    (["port", "proj", "--close", "8080"], sandbox.cmd_port),
    (["port", "proj", "--stop"], sandbox.cmd_port),
    (["logs", "proj"], sandbox.cmd_logs),
]


@pytest.mark.parametrize("argv,func", CASES, ids=[" ".join(c[0]) for c in CASES])
def test_parser_dispatch(argv, func):
    parser = sandbox.build_parser()
    args = parser.parse_args(argv)
    assert args.func is func


def test_every_registered_command_is_covered():
    """If a new subcommand lands, force a dispatch case for it."""
    parser = sandbox.build_parser()
    sub = next(a for a in parser._actions
               if isinstance(a, type(parser._subparsers._group_actions[0])))
    registered = set(sub.choices)
    covered = {argv[0] for argv, _ in CASES}
    assert registered == covered
