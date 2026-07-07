"""Static per-project tab catalog for the webui.

Each entry describes one tab the frontend can open for a project. The
`command` string and the upstream names it implies (`sandbox-agent-<p>`,
user `agent`, byobu session `main`) are a names lockstep with the agent
containers — drift means dead terminal tabs.

Webport tabs (user-registered ports from the broker's webports.json) are
synthesized in server.py from the registry, not listed here.
"""

# service_id -> spec. Insertion order is display order.
SERVICES = {
    "terminal": {
        "label": "Terminal",
        "kind": "ssh",             # rendered as an xterm.js tab over /tab
        "always_on": True,         # no probe — sshd runs whenever the agent does
        "renderer": "xterm.js",
        "default_port": 22,
        "command": ("byobu attach -t main 2>/dev/null"
                    " || byobu new-session -s main -c /home/agent -- bash"),
    },
    "editor": {
        "label": "Editor",
        "kind": "http",            # iframe tab — functional once the origin-port
        "always_on": False,        # proxy stamps origin_url on it (Stage 4);
        "renderer": "iframe",      # probe-gated until then it simply never shows
        "default_port": 8443,
        "upstream_path": "/",
    },
}


def resolve(service_id: str) -> dict | None:
    return SERVICES.get(service_id)


def get(service_id: str) -> dict | None:
    return SERVICES.get(service_id)
