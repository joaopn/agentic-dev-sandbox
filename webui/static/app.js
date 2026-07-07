// Sandbox WebUI — SSH terminal aggregator + broker-driven project management.
//
// All persistent state lives in browser localStorage, encrypted with a
// PBKDF2-derived AES-GCM key. The decrypted vault and derived key live in
// JS memory only while unlocked; both are dropped on lock or refresh.
//
// Management (create/start/stop/sync/destroy) rides the webui's broker relay
// (/broker/*). The browser never sees a broker token — it holds an opaque
// session cookie minted by /broker/login; one master password unlocks the
// vault AND derives the broker login proof (unified login).

const VAULT_KEY = "sandbox-webui-vault";
const THEME_KEY = "sandbox-webui-theme";
const RAIL_PINNED_KEY = "sandbox-webui-rail-pinned";
const RAIL_WIDTH_KEY = "sandbox-webui-rail-width";
const RAIL_WIDTH_DEFAULT = 200;
const RAIL_WIDTH_MIN = 80;
const RAIL_WIDTH_MAX = 480;
const PBKDF2_ITERATIONS = 600000;

// MIRROR-PAIR LOCKSTEP: LOGIN_PROOF_SALT_STR / LOGIN_PROOF_ITERATIONS must
// equal LOGIN_PROOF_SALT / LOGIN_PROOF_ITERATIONS in cli/broker_auth.py.
// The wire form is padded standard-alphabet base64 of 32 PBKDF2 bytes;
// drift means every login fails. Pinned by tests/test_webui_locksteps.py
// and the pinned vector in tests/test_broker_auth.py.
const LOGIN_PROOF_SALT_STR = "ads-broker-login-v1";
const LOGIN_PROOF_ITERATIONS = 600000;

// Vault-create password floor. LOCKSTEP with cli/broker_auth.py
// MIN_PASSWORD_LENGTH (unified master password: the same secret must satisfy
// both the vault floor here and `sandbox.py broker passwd`'s check).
const MIN_PASSWORD_LENGTH = 8;

// Sidebar cadence: TCP probes of vault-backed rows + the broker list re-sync.
const PROBE_INTERVAL_MS = 15000;

// Poll cadence for the op view-log tail. Milestones are coarse (2–8 per op
// over a lifecycle of seconds to minutes), so sub-second polling is plenty
// live without hammering the webui; at 2x (1.2s) progress feels laggy, at
// half (300ms) it's needless load for a single operator driving one op.
const OP_POLL_INTERVAL_MS = 600;

// The create-dialog payload allowlist. LOCKSTEP with cli/broker.py
// CREATE_WEBUI_FIELDS — the broker silently drops unknown fields, so a field
// missing there is silently lost, and a field missing here is unreachable
// from the browser. The dialog builds its POST body by iterating this list.
const CREATE_FIELDS = ["github_url", "branch", "egress", "memory", "cpus",
                       "profile", "agent", "docker"];

// The "Add port tab" payload fields (project rides in the URL path).
// LOCKSTEP with cli/broker.py WEBPORT_ADD_FIELDS.
const WEBPORT_FIELDS = ["port", "label"];

// ---- themes -----------------------------------------------------------------
//
// Each theme provides:
//   xterm: an ITheme passed to the xterm.js terminal
//   css:   CSS variables applied to document.documentElement
// Page chrome and terminal stay in the same family — never invert.

const THEMES = {
    dark: {
        label: "Dark",
        xterm: {
            background: "#000000", foreground: "#e0e0e0",
            cursor: "#e0e0e0", cursorAccent: "#000000",
            selectionBackground: "rgba(255,255,255,0.25)",
            black: "#000000", red: "#cc0403", green: "#19cb00", yellow: "#cecb00",
            blue: "#0d73cc", magenta: "#cb1ed1", cyan: "#0dcdcd", white: "#dddddd",
            brightBlack: "#767676", brightRed: "#f2201f", brightGreen: "#23fd00",
            brightYellow: "#fffd00", brightBlue: "#1a8fff", brightMagenta: "#fd28ff",
            brightCyan: "#14ffff", brightWhite: "#ffffff",
        },
        css: {
            "--bg-base": "#1e1e1e", "--bg-card": "#2a2a2a", "--bg-active": "#1e1e1e",
            "--bg-input": "#1a1a1a", "--bg-input-focus-border": "#6c9",
            "--fg-base": "#e0e0e0", "--fg-muted": "#aaa", "--fg-faint": "#777",
            "--fg-accent": "#6c9",
            "--border": "#444", "--border-strong": "#555",
            "--btn-bg": "#4a7c4e", "--btn-bg-hover": "#5a8c5e",
            "--btn-secondary-bg": "#444", "--btn-secondary-bg-hover": "#555",
            "--btn-danger-bg": "#7c3a3a", "--btn-danger-bg-hover": "#8c4a4a",
            "--error-fg": "#e88",
            "--status-up": "#6c6", "--status-down": "#555", "--status-error": "#e66",
            "--terminal-bg": "#000", "--terminal-fg": "#e0e0e0",
        },
    },
    light: {
        label: "Light",
        xterm: {
            background: "#ffffff", foreground: "#2a2a2a",
            cursor: "#2a2a2a", cursorAccent: "#ffffff",
            selectionBackground: "rgba(0,0,0,0.18)",
            black: "#2a2a2a", red: "#c91b00", green: "#00c200", yellow: "#c7c400",
            blue: "#0225c7", magenta: "#ca30c7", cyan: "#00c5c7", white: "#c7c7c7",
            brightBlack: "#676767", brightRed: "#ff6e67", brightGreen: "#5ffa68",
            brightYellow: "#fffc67", brightBlue: "#6871ff", brightMagenta: "#ff77ff",
            brightCyan: "#60fdff", brightWhite: "#ffffff",
        },
        css: {
            "--bg-base": "#fafafa", "--bg-card": "#ececec", "--bg-active": "#ffffff",
            "--bg-input": "#ffffff", "--bg-input-focus-border": "#3a8a3a",
            "--fg-base": "#1f1f1f", "--fg-muted": "#555", "--fg-faint": "#888",
            "--fg-accent": "#3a8a3a",
            "--border": "#d0d0d0", "--border-strong": "#bbb",
            "--btn-bg": "#3a8a3a", "--btn-bg-hover": "#4a9a4a",
            "--btn-secondary-bg": "#d0d0d0", "--btn-secondary-bg-hover": "#bbb",
            "--btn-danger-bg": "#b03a3a", "--btn-danger-bg-hover": "#c04a4a",
            "--error-fg": "#a33",
            "--status-up": "#3a8a3a", "--status-down": "#aaa", "--status-error": "#c04040",
            "--terminal-bg": "#ffffff", "--terminal-fg": "#2a2a2a",
        },
    },
    "solarized-dark": {
        label: "Solarized Dark",
        xterm: {
            background: "#002b36", foreground: "#839496",
            cursor: "#93a1a1", cursorAccent: "#002b36",
            selectionBackground: "rgba(147,161,161,0.25)",
            black: "#073642", red: "#dc322f", green: "#859900", yellow: "#b58900",
            blue: "#268bd2", magenta: "#d33682", cyan: "#2aa198", white: "#eee8d5",
            brightBlack: "#002b36", brightRed: "#cb4b16", brightGreen: "#586e75",
            brightYellow: "#657b83", brightBlue: "#839496", brightMagenta: "#6c71c4",
            brightCyan: "#93a1a1", brightWhite: "#fdf6e3",
        },
        css: {
            "--bg-base": "#002b36", "--bg-card": "#073642", "--bg-active": "#002b36",
            "--bg-input": "#001f27", "--bg-input-focus-border": "#268bd2",
            "--fg-base": "#93a1a1", "--fg-muted": "#839496", "--fg-faint": "#657b83",
            "--fg-accent": "#2aa198",
            "--border": "#0a4452", "--border-strong": "#0e5a6f",
            "--btn-bg": "#268bd2", "--btn-bg-hover": "#3a9be0",
            "--btn-secondary-bg": "#0a4452", "--btn-secondary-bg-hover": "#0e5a6f",
            "--btn-danger-bg": "#dc322f", "--btn-danger-bg-hover": "#ec4240",
            "--error-fg": "#dc322f",
            "--status-up": "#859900", "--status-down": "#586e75", "--status-error": "#dc322f",
            "--terminal-bg": "#002b36", "--terminal-fg": "#839496",
        },
    },
    dracula: {
        label: "Dracula",
        xterm: {
            background: "#282a36", foreground: "#f8f8f2",
            cursor: "#f8f8f2", cursorAccent: "#282a36",
            selectionBackground: "rgba(68,71,90,0.7)",
            black: "#21222c", red: "#ff5555", green: "#50fa7b", yellow: "#f1fa8c",
            blue: "#bd93f9", magenta: "#ff79c6", cyan: "#8be9fd", white: "#f8f8f2",
            brightBlack: "#6272a4", brightRed: "#ff6e6e", brightGreen: "#69ff94",
            brightYellow: "#ffffa5", brightBlue: "#d6acff", brightMagenta: "#ff92df",
            brightCyan: "#a4ffff", brightWhite: "#ffffff",
        },
        css: {
            "--bg-base": "#282a36", "--bg-card": "#343746", "--bg-active": "#282a36",
            "--bg-input": "#21222c", "--bg-input-focus-border": "#bd93f9",
            "--fg-base": "#f8f8f2", "--fg-muted": "#bdbdc8", "--fg-faint": "#6272a4",
            "--fg-accent": "#bd93f9",
            "--border": "#44475a", "--border-strong": "#5c5f74",
            "--btn-bg": "#50fa7b", "--btn-bg-hover": "#69ff94",
            "--btn-secondary-bg": "#44475a", "--btn-secondary-bg-hover": "#5c5f74",
            "--btn-danger-bg": "#ff5555", "--btn-danger-bg-hover": "#ff6e6e",
            "--error-fg": "#ff5555",
            "--status-up": "#50fa7b", "--status-down": "#6272a4", "--status-error": "#ff5555",
            "--terminal-bg": "#282a36", "--terminal-fg": "#f8f8f2",
        },
    },
    nord: {
        label: "Nord",
        xterm: {
            background: "#2e3440", foreground: "#d8dee9",
            cursor: "#d8dee9", cursorAccent: "#2e3440",
            selectionBackground: "rgba(76,86,106,0.7)",
            black: "#3b4252", red: "#bf616a", green: "#a3be8c", yellow: "#ebcb8b",
            blue: "#81a1c1", magenta: "#b48ead", cyan: "#88c0d0", white: "#e5e9f0",
            brightBlack: "#4c566a", brightRed: "#bf616a", brightGreen: "#a3be8c",
            brightYellow: "#ebcb8b", brightBlue: "#81a1c1", brightMagenta: "#b48ead",
            brightCyan: "#8fbcbb", brightWhite: "#eceff4",
        },
        css: {
            "--bg-base": "#2e3440", "--bg-card": "#3b4252", "--bg-active": "#2e3440",
            "--bg-input": "#272c36", "--bg-input-focus-border": "#88c0d0",
            "--fg-base": "#d8dee9", "--fg-muted": "#a8b2c1", "--fg-faint": "#7884a0",
            "--fg-accent": "#88c0d0",
            "--border": "#434c5e", "--border-strong": "#4c566a",
            "--btn-bg": "#5e81ac", "--btn-bg-hover": "#7592b8",
            "--btn-secondary-bg": "#434c5e", "--btn-secondary-bg-hover": "#4c566a",
            "--btn-danger-bg": "#bf616a", "--btn-danger-bg-hover": "#cf717a",
            "--error-fg": "#bf616a",
            "--status-up": "#a3be8c", "--status-down": "#4c566a", "--status-error": "#bf616a",
            "--terminal-bg": "#2e3440", "--terminal-fg": "#d8dee9",
        },
    },
};

const DEFAULT_THEME = "dark";

function loadStoredTheme() {
    const id = localStorage.getItem(THEME_KEY);
    return THEMES[id] ? id : DEFAULT_THEME;
}

function applyTheme(id) {
    const theme = THEMES[id] || THEMES[DEFAULT_THEME];
    for (const [k, v] of Object.entries(theme.css)) {
        document.documentElement.style.setProperty(k, v);
    }
    for (const t of Object.values(state.terminals)) {
        if (t.term) t.term.options.theme = theme.xterm;
    }
    state.theme = id;
    localStorage.setItem(THEME_KEY, id);
}

function currentXtermTheme() {
    return THEMES[state.theme || DEFAULT_THEME].xterm;
}

// ---- state -------------------------------------------------------------------

const state = {
    derivedKey: null,        // CryptoKey | null
    salt: null,              // Uint8Array | null
    loginProof: null,        // string | null — broker login derivation; memory-only
                             // sibling of derivedKey (set at setup / post-decrypt
                             // unlock, cleared on lock, NEVER persisted)
    vault: null,             // { version, projects, settings } | null
    brokerProjects: [],      // last /broker/projects rows: [{project, state, ssh_port}]
    mgmtLocked: false,       // auto-login failed / session expired — show Connect row
    mgmtWasLive: false,      // a management session existed this unlock (for expiry detection)
    activeProject: null,     // string | null
    activeService: null,     // string | null
    projectServices: {},     // { [projectName]: [ {id, label, kind, ...} ] }
    projectLastService: {},  // { [projectName]: serviceId } — session-only landing memory
    terminals: {},           // "${project}:${service}" -> { term, fitAddon, ws, container, project, service }
    probeTimer: null,
    theme: null,             // theme id; set by applyTheme on boot
    giteaUrl: null,          // string | null — populated from /config on dashboard render
    railPinned: false,       // persisted: keep rail in flex flow (push layout)
    railExpanded: false,     // in-memory: rail visible (overlay when unpinned)
    railWidth: RAIL_WIDTH_DEFAULT,  // persisted: rail width in px
};

// ---- utilities -------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));
const ub64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
const tkey = (project, service) => `${project}:${service}`;

function el(tag, attrs = {}, children = []) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === "class") e.className = v;
        else if (k === "onclick") e.onclick = v;
        else if (k === "oninput") e.oninput = v;
        else if (k === "onkeydown") e.onkeydown = v;
        else e.setAttribute(k, v);
    }
    for (const c of children) {
        if (c == null) continue;
        e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return e;
}

function clearBody() {
    closeGearMenu();
    document.body.innerHTML = "";
}

// ---- crypto ----------------------------------------------------------------

async function deriveKey(password, salt) {
    const enc = new TextEncoder();
    const baseKey = await crypto.subtle.importKey(
        "raw", enc.encode(password), "PBKDF2", false, ["deriveKey"],
    );
    return crypto.subtle.deriveKey(
        { name: "PBKDF2", salt, iterations: PBKDF2_ITERATIONS, hash: "SHA-256" },
        baseKey,
        { name: "AES-GCM", length: 256 },
        false,
        ["encrypt", "decrypt"],
    );
}

// The login proof: base64 of 256 PBKDF2 bits over the fixed public salt.
// Canonical wire form — must match cli/broker_auth.py::derive_login_proof
// byte for byte (standard-alphabet padded base64). Needs its own importKey:
// the vault key's import above grants only ["deriveKey"].
async function deriveLoginProof(password) {
    const enc = new TextEncoder();
    const baseKey = await crypto.subtle.importKey(
        "raw", enc.encode(password), "PBKDF2", false, ["deriveBits"],
    );
    const bits = await crypto.subtle.deriveBits(
        { name: "PBKDF2", salt: enc.encode(LOGIN_PROOF_SALT_STR),
          iterations: LOGIN_PROOF_ITERATIONS, hash: "SHA-256" },
        baseKey, 256,
    );
    return b64(bits);
}

async function encryptVault(key, vault) {
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const enc = new TextEncoder();
    const ciphertext = await crypto.subtle.encrypt(
        { name: "AES-GCM", iv }, key, enc.encode(JSON.stringify(vault)),
    );
    return { iv: b64(iv), ciphertext: b64(ciphertext) };
}

async function decryptVault(key, ivB64, ctB64) {
    const dec = new TextDecoder();
    const plaintext = await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: ub64(ivB64) }, key, ub64(ctB64),
    );
    return JSON.parse(dec.decode(plaintext));
}

// ---- vault persistence -----------------------------------------------------

function loadStored() {
    const raw = localStorage.getItem(VAULT_KEY);
    return raw ? JSON.parse(raw) : null;
}

function saveStored(stored) {
    localStorage.setItem(VAULT_KEY, JSON.stringify(stored));
}

async function persistVault() {
    // JIT-attached projects (broker keyring) are transient: their SSH creds are
    // held in browser memory only and must NEVER reach the encrypted blob.
    // Strip them here — the single persist choke point — so the guarantee holds
    // regardless of what triggered the save.
    const persistable = {
        ...state.vault,
        projects: state.vault.projects.filter((p) => !p._jit),
    };
    const enc = await encryptVault(state.derivedKey, persistable);
    saveStored({ salt: b64(state.salt), ...enc });
}

// ---- screens ---------------------------------------------------------------

function renderSetup() {
    clearBody();
    const pw1 = el("input", { type: "password", autocomplete: "new-password" });
    const pw2 = el("input", { type: "password", autocomplete: "new-password" });
    const errEl = el("div", { class: "error" });

    const submit = el("button", { class: "btn" }, ["Create vault"]);
    submit.onclick = async () => {
        if (pw1.value.length < MIN_PASSWORD_LENGTH) {
            errEl.textContent = `Password must be at least ${MIN_PASSWORD_LENGTH} characters.`;
            return;
        }
        if (pw1.value !== pw2.value) {
            errEl.textContent = "Passwords do not match.";
            return;
        }
        try {
            const salt = crypto.getRandomValues(new Uint8Array(16));
            state.derivedKey = await deriveKey(pw1.value, salt);
            state.salt = salt;
            state.loginProof = await deriveLoginProof(pw1.value);
            state.vault = { version: 1, projects: [], settings: {} };
            await persistVault();
            await renderDashboard();
        } catch (e) {
            errEl.textContent = "Setup failed: " + e.message;
        }
    };

    const card = el("div", { class: "card" }, [
        el("h2", {}, ["Set master password"]),
        el("p", {}, [
            "One password for everything: it encrypts your saved agent credentials and logs you into project management. There is no recovery — if you forget it, you'll need to re-add each project.",
        ]),
        el("p", { class: "hint" }, [
            "For management, set the same password on the host with ",
            el("code", {}, ["python sandbox.py broker passwd"]),
            " (at least 8 characters).",
        ]),
        el("div", { class: "field" }, [el("label", {}, ["Master password"]), pw1]),
        el("div", { class: "field" }, [el("label", {}, ["Confirm password"]), pw2]),
        el("div", { class: "btn-row" }, [submit]),
        errEl,
    ]);
    document.body.appendChild(el("div", { id: "app" }, [
        el("div", { class: "center-screen" }, [card]),
    ]));
    setTimeout(() => pw1.focus(), 50);
}

function renderUnlock() {
    clearBody();
    const pw = el("input", { type: "password", autocomplete: "current-password" });
    const errEl = el("div", { class: "error" });

    const submit = el("button", { class: "btn" }, ["Unlock"]);
    submit.onclick = async () => {
        try {
            const stored = loadStored();
            const salt = ub64(stored.salt);
            const key = await deriveKey(pw.value, salt);
            const vault = await decryptVault(key, stored.iv, stored.ciphertext);
            // Derive the broker login proof only AFTER decrypt succeeds: a
            // wrong password must never leave a proof behind that a later
            // auto-login would burn against the broker's rate limiter.
            state.loginProof = await deriveLoginProof(pw.value);
            state.derivedKey = key;
            state.salt = salt;
            state.vault = vault;
            await renderDashboard();
        } catch (e) {
            errEl.textContent = "Wrong password.";
        }
    };
    pw.onkeydown = (e) => { if (e.key === "Enter") submit.click(); };

    const card = el("div", { class: "card" }, [
        el("h2", {}, ["Unlock vault"]),
        el("div", { class: "field" }, [el("label", {}, ["Master password"]), pw]),
        el("div", { class: "btn-row" }, [submit]),
        errEl,
    ]);
    document.body.appendChild(el("div", { id: "app" }, [
        el("div", { class: "center-screen" }, [card]),
    ]));
    setTimeout(() => pw.focus(), 50);
}

// ---- dashboard --------------------------------------------------------------

async function fetchGiteaUrl() {
    try {
        const res = await fetch("/config");
        const data = await res.json();
        state.giteaUrl = data.gitea_url || null;
    } catch (_) {
        state.giteaUrl = null;
    }
}

// Per-project tab catalog: /services/<p> returns {project, services: [spec…]}
// (an ordered LIST — ADS's shape, not RS's map). Cached per project; callers
// bust the cache (delete) before a fetch when they need fresh probe results.
async function fetchProjectServices(projectName) {
    if (state.projectServices[projectName]) return state.projectServices[projectName];
    try {
        const res = await fetch(`/services/${encodeURIComponent(projectName)}`);
        const body = await res.json();
        state.projectServices[projectName] = Array.isArray(body.services) ? body.services : [];
    } catch (e) {
        state.projectServices[projectName] = [];
    }
    return state.projectServices[projectName];
}

async function renderDashboard() {
    clearBody();
    await fetchGiteaUrl();

    const rail = makeProjectRail();
    const tabStrip = el("div", { class: "service-tabs", id: "service-tabs" }, [
        makeProjectsTab(),
    ]);
    const termArea = el("div", { class: "terminal-area", id: "terminal-area" });
    const welcomeText = state.vault.projects.length === 0
        ? "No projects yet. Open the Projects rail and click + New project."
        : "Open the Projects rail and click a project to attach.";
    termArea.appendChild(el("div", { class: "welcome", id: "welcome" }, [welcomeText]));

    const main = el("div", { class: "main-area" }, [tabStrip, termArea]);
    const dashboard = el("div", { class: "dashboard" }, [rail, main]);
    document.body.appendChild(el("div", { id: "app" }, [dashboard]));

    applyRailState();
    schedulePolling();

    if (state.activeProject) {
        await activateProject(state.activeProject);
    }

    // Unified login: one best-effort broker login with the unlock-derived
    // proof, awaited BEFORE the sidebar sync — a non-blocking sync would race
    // the session mint and silently no-op on first unlock. A down broker or a
    // password mismatch is tolerated (management stays opt-in; the Connect
    // row in the rail surfaces the login/mismatch card on demand).
    const ok = await tryBrokerLogin();
    state.mgmtLocked = !ok;
    state.mgmtWasLive = ok;
    if (ok) await syncSidebarFromBroker();
    refreshProjectRail();
}

// One POST /broker/login with the in-memory proof. Exactly one attempt per
// call — callers fire it once per unlock / Connect click / explicit Retry,
// never in a loop, so it cannot trip the webui's global login rate limiter.
// Returns true on a live management session, false otherwise.
async function tryBrokerLogin() {
    if (!state.loginProof) return false;
    try {
        const res = await fetch("/broker/login", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ proof: state.loginProof }),
        });
        return res.status === 200;
    } catch (e) {
        return false;   // broker/webui unreachable — management is opt-in
    }
}

// ---- rail expand / pin -----------------------------------------------------

function loadRailPinned() {
    return localStorage.getItem(RAIL_PINNED_KEY) === "1";
}

function loadRailWidth() {
    const v = parseInt(localStorage.getItem(RAIL_WIDTH_KEY) || "", 10);
    if (!isFinite(v)) return RAIL_WIDTH_DEFAULT;
    return Math.max(RAIL_WIDTH_MIN, Math.min(RAIL_WIDTH_MAX, v));
}

function applyRailWidth(px) {
    document.documentElement.style.setProperty("--rail-width", `${px}px`);
}

// Splitter on the rail's right edge — pointer-capture pattern so the drag
// survives moving the cursor across xterm canvases.
function installRailSplitterDrag(splitter) {
    let dragging = false;
    let pointerId = null;
    const onMove = (ev) => {
        if (!dragging) return;
        const rail = splitter.parentElement;
        if (!rail) return;
        const rect = rail.getBoundingClientRect();
        let w = ev.clientX - rect.left;
        w = Math.max(RAIL_WIDTH_MIN, Math.min(RAIL_WIDTH_MAX, w));
        applyRailWidth(w);
        state.railWidth = w;
    };
    const onUp = () => {
        if (!dragging) return;
        dragging = false;
        splitter.classList.remove("dragging");
        try { if (pointerId != null) splitter.releasePointerCapture(pointerId); } catch (_) {}
        pointerId = null;
        splitter.removeEventListener("pointermove", onMove);
        splitter.removeEventListener("pointerup", onUp);
        splitter.removeEventListener("pointercancel", onUp);
        document.body.style.userSelect = "";
        localStorage.setItem(RAIL_WIDTH_KEY, String(Math.round(state.railWidth)));
        // Pinned rail shifts the terminal area's width — refit xterms.
        setTimeout(() => {
            for (const t of Object.values(state.terminals)) {
                if (t.fitAddon) { try { t.fitAddon.fit(); } catch (_) {} }
            }
        }, 0);
    };
    splitter.onpointerdown = (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        dragging = true;
        pointerId = ev.pointerId;
        splitter.classList.add("dragging");
        try { splitter.setPointerCapture(ev.pointerId); } catch (_) {}
        splitter.addEventListener("pointermove", onMove);
        splitter.addEventListener("pointerup", onUp);
        splitter.addEventListener("pointercancel", onUp);
        document.body.style.userSelect = "none";
    };
}

function makeProjectsTab() {
    const expanded = state.railPinned || state.railExpanded;
    const chev = el("span", {
        class: "projects-chevron",
        id: "projects-chevron",
    }, [expanded ? "◀" : "▶"]);
    const tab = el("div", {
        class: "tab projects-tab",
        title: expanded ? "Hide projects" : "Show projects",
    }, [chev, el("span", {}, ["Projects"])]);
    tab.onclick = (ev) => { ev.stopPropagation(); toggleRailExpanded(); };
    return tab;
}

function makePinButton() {
    const btn = el("button", { class: "pin-btn", title: "Pin sidebar" });
    btn.innerHTML = '<svg viewBox="0 0 16 16" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M9.828.722a.5.5 0 0 1 .354.146l4.95 4.95a.5.5 0 0 1 0 .707c-.48.48-1.072.588-1.503.588-.177 0-.335-.018-.46-.039l-3.134 3.134a5.927 5.927 0 0 1 .16 1.013c.046.702-.032 1.687-.72 2.375a.5.5 0 0 1-.707 0l-2.829-2.828-3.182 3.182c-.195.195-1.219.902-1.414.707-.195-.195.512-1.22.707-1.414l3.182-3.182-2.828-2.829a.5.5 0 0 1 0-.707c.688-.688 1.673-.767 2.375-.72a5.922 5.922 0 0 1 1.013.16l3.134-3.133a2.772 2.772 0 0 1-.04-.461c0-.43.108-1.022.589-1.503A.5.5 0 0 1 9.828.722z"/></svg>';
    btn.onclick = (ev) => { ev.stopPropagation(); togglePinned(); };
    return btn;
}

function applyRailState() {
    const dashboard = document.querySelector(".dashboard");
    if (!dashboard) return;
    const expanded = state.railPinned || state.railExpanded;
    dashboard.classList.toggle("pinned", state.railPinned);
    dashboard.classList.toggle("expanded", expanded);

    const pinBtn = dashboard.querySelector(".rail-header .pin-btn");
    if (pinBtn) {
        pinBtn.classList.toggle("pinned", state.railPinned);
        pinBtn.title = state.railPinned ? "Unpin sidebar" : "Pin sidebar";
    }
    const chevron = document.getElementById("projects-chevron");
    if (chevron) chevron.textContent = expanded ? "◀" : "▶";
    const projectsTab = dashboard.querySelector(".projects-tab");
    if (projectsTab) projectsTab.title = expanded ? "Hide projects" : "Show projects";

    // Layout shift only happens when pinned toggles; refit the active terminal.
    const t = state.activeProject && state.activeService
        ? state.terminals[tkey(state.activeProject, state.activeService)] : null;
    if (t && t.fitAddon) {
        setTimeout(() => { try { t.fitAddon.fit(); } catch (_) {} }, 0);
    }
}

function toggleRailExpanded() {
    // Tab handle is the universal show/hide control. If pinned, collapsing
    // also unpins — keeping pinned-but-collapsed is incoherent.
    if (state.railPinned) {
        state.railPinned = false;
        localStorage.setItem(RAIL_PINNED_KEY, "0");
        state.railExpanded = false;
    } else {
        state.railExpanded = !state.railExpanded;
    }
    applyRailState();
}

// Auto-collapse the floating rail on any interaction outside it. Pinned rail
// is excluded — pinning is the explicit "keep it open" affordance.
function installRailOutsideClickHandlers() {
    document.addEventListener("pointerdown", (ev) => {
        if (!state.railExpanded || state.railPinned) return;
        // Modal in front owns the interaction; don't collapse behind it.
        if (document.querySelector(".modal-backdrop")) return;
        const path = ev.composedPath ? ev.composedPath() : [];
        for (const node of path) {
            if (!node || !node.classList) continue;
            // Click inside the rail itself — let inner handlers run.
            if (node.classList.contains("project-rail")) return;
            // The gear menu is a body child (floats outside the rail) but is
            // logically part of it — interacting with it must not collapse
            // the rail. Same for the gear that opens it.
            if (node.classList.contains("project-gear-menu")) return;
            if (node.classList.contains("project-config-btn")) return;
            // Click on the Projects tab — its own onclick toggles the rail.
            // Letting our outside handler also fire here would double-toggle.
            if (node.classList.contains("projects-tab")) return;
        }
        state.railExpanded = false;
        applyRailState();
    });
}

function togglePinned() {
    state.railPinned = !state.railPinned;
    localStorage.setItem(RAIL_PINNED_KEY, state.railPinned ? "1" : "0");
    // Pinning auto-expands; unpinning auto-collapses so the user recovers
    // horizontal space in a single click.
    state.railExpanded = state.railPinned;
    applyRailState();
}

// ---- project rail ----------------------------------------------------------

// Rebuild the sidebar rail in place after a management op changes the project
// set (create adds a row, destroy removes one). Targeted — NOT renderDashboard,
// which clearBody()s and would tear down any open service tabs.
function refreshProjectRail() {
    const old = document.querySelector(".project-rail");
    if (!old) return;
    old.replaceWith(makeProjectRail());
    applyRailState();
    schedulePolling();
    if (state.activeProject) {
        const row = document.querySelector(
            `.project[data-name="${CSS.escape(state.activeProject)}"]`);
        if (row) row.classList.add("active");
    }
}

function makeProjectRail() {
    const rail = el("aside", { class: "project-rail" });
    const splitter = el("div", { class: "rail-splitter", title: "Drag to resize" });
    installRailSplitterDrag(splitter);
    rail.appendChild(splitter);
    const header = el("div", { class: "rail-header" }, [
        el("span", {}, ["Projects"]),
        makePinButton(),
    ]);
    rail.appendChild(header);

    // Fixed Gitea launcher pinned on top (new-tab launcher; the embedded
    // iframe tab is Stage 4). Rendered only when the server exposes a URL.
    if (state.giteaUrl) {
        const row = el("div", {
            class: "gitea-row",
            title: `Open Gitea (${state.giteaUrl}) in a new tab`,
        }, ["Gitea ↗"]);
        row.onclick = () => window.open(state.giteaUrl, "_blank", "noopener,noreferrer");
        rail.appendChild(row);
    }

    // Auto-login failed or the session expired: a Connect row that opens the
    // login/mismatch card. Broker-only rows freeze at last-known state until
    // re-login; vault-backed rows stay live via /probe.
    if (state.mgmtLocked && state.loginProof) {
        const row = el("div", { class: "mgmt-connect-row", title: "Log into project management" },
                       ["Management locked — Connect"]);
        row.onclick = () => openMgmtLoginModal();
        rail.appendChild(row);
    }

    // One row per project: the vault's entries (persisted bookmarks + _jit
    // rows) first, then broker-listed projects with no vault entry (stopped
    // projects, or running ones whose JIT attach hasn't landed yet).
    const seen = new Set();
    for (const p of state.vault.projects) {
        seen.add(p.name);
        rail.appendChild(makeProjectRow(p.name, p));
    }
    for (const b of state.brokerProjects) {
        if (seen.has(b.project)) continue;
        rail.appendChild(makeProjectRow(b.project, null));
    }

    const addBtn = el("button", { class: "add-project" }, ["+ New project"]);
    addBtn.onclick = () => openNewProjectDialog("create");
    rail.appendChild(addBtn);

    rail.appendChild(el("div", { class: "rail-spacer" }));
    const footer = el("div", { class: "rail-footer" }, [
        makeThemeSelector(),
        el("button", { class: "lock-btn", onclick: lockVault }, ["Lock vault"]),
    ]);
    rail.appendChild(footer);
    return rail;
}

function makeThemeSelector() {
    const sel = document.createElement("select");
    sel.className = "theme-select";
    sel.title = "Theme";
    for (const [id, t] of Object.entries(THEMES)) {
        const opt = document.createElement("option");
        opt.value = id;
        opt.textContent = t.label;
        if (id === state.theme) opt.selected = true;
        sel.appendChild(opt);
    }
    sel.onchange = () => applyTheme(sel.value);
    return sel;
}

function brokerRowFor(name) {
    return state.brokerProjects.find((p) => p.project === name) || null;
}

function makeProjectRow(name, vaultProject) {
    const dot = el("span", { class: "status-dot" });
    const nameEl = el("span", { class: "name" }, [name]);
    const gear = el("span", {
        class: "project-config-btn",
        title: "Project actions",
    }, ["⚙"]);
    gear.onclick = (ev) => {
        ev.stopPropagation();
        openGearMenu(name, gear);
    };
    const head = el("div", { class: "project-head" }, [dot, nameEl, gear]);
    const row = el("div", {
        class: "project",
        "data-name": name,
        onclick: () => activateProject(name),
    }, [head]);
    // Broker-only rows (no vault coords to probe) get their dot from the last
    // broker list sync; vault-backed rows are colored by probeProject.
    if (!vaultProject) {
        const b = brokerRowFor(name);
        if (b) row.classList.add(b.state === "running" ? "up" : "down");
    }
    return row;
}

// ---- gear menu (per-project lifecycle actions) ------------------------------

let gearMenuEl = null;

function closeGearMenu() {
    if (gearMenuEl) { gearMenuEl.remove(); gearMenuEl = null; }
}

function openGearMenu(name, anchor) {
    if (gearMenuEl) { closeGearMenu(); }
    const menu = el("div", { class: "project-gear-menu" });
    const item = (label, fn, danger) => {
        const it = el("div", { class: danger ? "gear-item danger" : "gear-item" }, [label]);
        it.onclick = (ev) => { ev.stopPropagation(); closeGearMenu(); fn(); };
        menu.appendChild(it);
    };
    // Contextual power entry when the broker state is known; both otherwise
    // (the broker validates — a wrong verb fails with a clear message).
    const b = brokerRowFor(name);
    if (!b || b.state !== "running") item("Start", () => mgmtLifecycle(name, "start"));
    if (!b || b.state === "running") item("Stop", () => mgmtLifecycle(name, "stop"));
    item("Sync", () => mgmtLifecycle(name, "sync"));
    item("Port tabs…", () => openWebportDialog(name));
    item("Destroy…", () => mgmtDestroyDialog(name), true);
    // A vault bookmark the broker doesn't list (manual import) can only be
    // cleaned up client-side — same behavior the old close-x provided.
    const vaultEntry = state.vault.projects.find((p) => p.name === name);
    if (vaultEntry && !vaultEntry._jit && !b) {
        item("Remove bookmark", () => removeBookmark(name));
    }

    document.body.appendChild(menu);
    const r = anchor.getBoundingClientRect();
    menu.style.left = `${Math.min(r.left, window.innerWidth - menu.offsetWidth - 8)}px`;
    menu.style.top = `${r.bottom + 4}px`;
    gearMenuEl = menu;
    // Dismiss on the next pointerdown outside the menu.
    const dismiss = (ev) => {
        const path = ev.composedPath ? ev.composedPath() : [];
        for (const node of path) {
            if (node && node.classList && node.classList.contains("project-gear-menu")) return;
        }
        closeGearMenu();
        document.removeEventListener("pointerdown", dismiss, true);
    };
    setTimeout(() => document.addEventListener("pointerdown", dismiss, true), 0);
}

async function removeBookmark(name) {
    if (!confirm(`Remove "${name}" from the sidebar? (The project itself is not affected.)`)) return;
    teardownProjectState(name);
    state.vault.projects = state.vault.projects.filter((p) => p.name !== name);
    await persistVault();
    refreshProjectRail();
    if (!state.activeProject) showWelcome();
}

// ---- broker sync -----------------------------------------------------------

// Fetch a project's SSH coordinates from the broker (JIT keyring) and add or
// refresh its sidebar entry as a transient (_jit) bookmark. Best-effort:
// returns true on success. NOTE the ADS field name: AttachInfo carries
// `project`, not RS's `name` (§9 field-rename lockstep, read side).
async function attachIntoVault(name) {
    let res;
    try {
        res = await fetch(`/broker/project/${encodeURIComponent(name)}/attach`,
            { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    } catch (e) { return false; }
    if (!res.ok) return false;
    let body; try { body = await res.json(); } catch (e) { return false; }
    if (!body.ok || !body.result) return false;
    const info = body.result;   // {project, host, port, username, password}
    const existing = state.vault.projects.find((p) => p.name === info.project);
    if (existing) {
        existing.host = info.host; existing.port = info.port;
        existing.username = info.username; existing.password = info.password;
    } else {
        state.vault.projects.push({
            name: info.project, host: info.host, port: info.port,
            username: info.username, password: info.password, _jit: true,
        });
    }
    return true;
}

// Merge the broker's project list into the sidebar: remember every row (the
// rail shows stopped projects too — that's what Start acts on) and JIT-attach
// creds for running projects missing from the vault. _jit rows are transient
// (creds in memory only, never persisted), so a reload drops them; this
// re-adds them from the authoritative broker list. Needs a management session
// — silently no-ops when logged out.
async function syncSidebarFromBroker(prefetched) {
    let list = prefetched;
    if (!Array.isArray(list)) {
        let res;
        try { res = await fetch("/broker/projects"); } catch (e) { return; }
        if (!res.ok) return;                     // 401/403/503 → no session
        let body; try { body = await res.json(); } catch (e) { return; }
        if (!body.ok || !Array.isArray(body.result)) return;
        list = body.result;
    }
    state.brokerProjects = list;
    const running = list.filter((p) => p.state === "running");
    let added = 0;
    await Promise.all(running.map(async (p) => {
        if (state.vault.projects.some((v) => v.name === p.project)) return;
        if (await attachIntoVault(p.project)) added++;
    }));
    if (added) refreshProjectRail();
}

// Periodic broker list re-sync (rail rows + dots for broker-only rows).
// A 401 after a previously live session means the session expired — surface
// the Connect row instead of failing fully silently (broker-only dots freeze
// at last-known state; vault-backed dots stay live via /probe).
async function refreshBrokerList() {
    let res;
    try { res = await fetch("/broker/projects"); } catch (e) { return; }
    if (res.status === 401) {
        if (state.mgmtWasLive && !state.mgmtLocked) {
            state.mgmtLocked = true;
            refreshProjectRail();
        }
        return;
    }
    if (!res.ok) return;
    let body; try { body = await res.json(); } catch (e) { return; }
    if (!body.ok || !Array.isArray(body.result)) return;
    const sig = (l) => l.map((p) => `${p.project}:${p.state}`).sort().join(",");
    const changed = sig(body.result) !== sig(state.brokerProjects);
    state.brokerProjects = body.result;
    if (changed) refreshProjectRail();
}

function schedulePolling() {
    if (state.probeTimer) clearInterval(state.probeTimer);
    const tick = () => {
        for (const p of state.vault.projects) probeProject(p);
        refreshBrokerList();
    };
    tick();
    state.probeTimer = setInterval(tick, PROBE_INTERVAL_MS);
}

// Vault-backed rows are probed at their STORED coordinates — JIT rows carry
// sandbox-agent-<p>:22 (stamped by attach), imported rows whatever the user
// saved. Never hardcode the container DNS name here: an imported row's
// coords are the only ones that are right for it.
async function probeProject(project) {
    try {
        const url = `/probe?host=${encodeURIComponent(project.host)}&port=${project.port}`;
        const res = await fetch(url);
        const data = await res.json();
        const row = document.querySelector(`.project[data-name="${CSS.escape(project.name)}"]`);
        if (!row) return;
        row.classList.toggle("up", !!data.up);
        row.classList.toggle("down", !data.up);
    } catch (_) {
        // ignore probe errors
    }
}

function lockVault() {
    if (state.probeTimer) { clearInterval(state.probeTimer); state.probeTimer = null; }
    for (const t of Object.values(state.terminals)) {
        try { if (t.ws) t.ws.close(); } catch (_) {}
        try { if (t.term) t.term.dispose(); } catch (_) {}
    }
    // Locked vault = logged out of management too: revoke the webui session +
    // broker token (fire-and-forget — a down broker must not block locking).
    // A refresh/tab-close drops JS memory without running this, so that path
    // keeps today's behavior: the session cookie just ages out on its TTL.
    try {
        fetch("/broker/logout", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: "{}",
        }).catch(() => {});
    } catch (e) { /* best-effort */ }
    state.derivedKey = null;
    state.loginProof = null;
    state.vault = null;
    state.salt = null;
    state.brokerProjects = [];
    state.mgmtLocked = false;
    state.mgmtWasLive = false;
    state.terminals = {};
    state.activeProject = null;
    state.activeService = null;
    state.projectServices = {};
    state.projectLastService = {};
    state.giteaUrl = null;
    state.railExpanded = state.railPinned;
    renderUnlock();
}

// ---- management login (modal) ------------------------------------------------

function mgmtCard(view, children) {
    view.innerHTML = "";
    view.appendChild(el("div", { class: "card mgmt-card" }, children));
}

function mgmtCloseBtn(view) {
    const btn = el("button", { class: "btn btn-secondary" }, ["Close"]);
    btn.onclick = () => {
        const backdrop = view.closest(".modal-backdrop");
        if (backdrop) backdrop.remove();
    };
    return btn;
}

// Unified login: no password form — the proof derived from the master
// password at unlock is auto-submitted, ONE attempt per call (a call happens
// per Connect click and per explicit Retry, never in a loop, so the global
// login rate limiter can't be tripped). A 401 here means the broker's stored
// secret was set to a DIFFERENT password than the vault's — surfaced as a
// mismatch card; the fix is host-side. `onSuccess` runs after a 200.
async function renderMgmtLogin(view, onSuccess) {
    mgmtCard(view, [el("div", { class: "mgmt-loading" }, ["Connecting to management…"])]);
    let res = null;
    if (state.loginProof) {
        try {
            res = await fetch("/broker/login", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ proof: state.loginProof }),
            });
        } catch (e) { return renderMgmtUnavailable(view); }
    }
    if (res && res.status === 200) return onSuccess();
    if (res && res.status === 403) return renderMgmtRejected(view);
    if (res && res.status === 503) return renderMgmtUnavailable(view);
    // 401 (password mismatch), 429 (rate-limited), or a missing proof:
    // explain + a manual Retry (each click = one limiter-visible attempt).
    const retry = el("button", { class: "btn" }, ["Retry"]);
    retry.onclick = () => renderMgmtLogin(view, onSuccess);
    let msg;
    if (res && res.status === 429) {
        const ra = res.headers.get("Retry-After");
        msg = el("p", {}, [`Too many login attempts. Wait ${ra || "a moment"}s, then retry.`]);
    } else {
        msg = el("p", {}, [
            "Your master password doesn't match the broker's operator password. ",
            "On the host, run ",
            el("code", {}, ["python sandbox.py broker passwd"]),
            " and enter your vault (master) password — then retry.",
        ]);
    }
    mgmtCard(view, [
        el("h2", {}, ["Management login failed"]),
        msg,
        el("div", { class: "btn-row" }, [mgmtCloseBtn(view), retry]),
    ]);
}

function renderMgmtUnavailable(view) {
    mgmtCard(view, [
        el("h2", {}, ["Management unavailable"]),
        el("p", {}, ["The broker isn't reachable. Start it on the host:"]),
        el("pre", {}, ["python sandbox.py broker start"]),
        el("div", { class: "hint" }, [
            "Management is opt-in; the rest of the webui is unaffected.",
        ]),
        el("div", { class: "btn-row" }, [mgmtCloseBtn(view)]),
    ]);
}

function renderMgmtRejected(view) {
    mgmtCard(view, [
        el("h2", {}, ["Broker rejected the webui"]),
        el("p", {}, [
            "The broker rejected the webui's identity. The webui must run as ",
            "the same user as the broker (uid match). The broker log names the ",
            "mismatch.",
        ]),
        el("div", { class: "btn-row" }, [mgmtCloseBtn(view)]),
    ]);
}

// The login/mismatch card as a modal over the dashboard. On success: close,
// re-sync the sidebar, then run the interrupted action (if any).
function openMgmtLoginModal(next) {
    if (document.querySelector(".modal-backdrop")) return;
    const backdrop = el("div", { class: "modal-backdrop" });
    const view = el("div", { class: "mgmt-modal-view" });
    backdrop.appendChild(view);
    document.body.appendChild(backdrop);
    renderMgmtLogin(view, async () => {
        backdrop.remove();
        state.mgmtLocked = false;
        state.mgmtWasLive = true;
        await syncSidebarFromBroker();
        refreshProjectRail();
        if (next) next();
    });
}

function openMgmtCardModal(renderer) {
    if (document.querySelector(".modal-backdrop")) return;
    const backdrop = el("div", { class: "modal-backdrop" });
    const view = el("div", { class: "mgmt-modal-view" });
    backdrop.appendChild(view);
    document.body.appendChild(backdrop);
    renderer(view);
}

// An auth/availability status the caller should defer to (open the right
// modal), or null if the response carries a real verb result to handle.
// `retry` re-runs the interrupted action after a successful re-login.
function mgmtStatusRedirect(status, retry) {
    if (status === 401) return () => openMgmtLoginModal(retry);
    if (status === 403) return () => openMgmtCardModal(renderMgmtRejected);
    if (status === 503) return () => openMgmtCardModal(renderMgmtUnavailable);
    return null;
}

// Human text for a failed relay reply. Handles both the broker envelope
// ({ok:false, error:{kind, message}}) and the webui's own plain-string
// errors ({error: "invalid project name"}).
function mgmtErrText(body) {
    const e = body && body.error;
    if (typeof e === "string") return e;
    return (e && (e.message || e.kind)) || "unknown error";
}

// ---- two-phase op box: (confirm →) live progress tail -----------------------
// One floating box for every lifecycle verb. create/destroy confirm first
// (phase 1); start/stop/sync fire directly into phase 2. Phase 2 tails the
// broker's view log live to a terminal milestone, then shows the result +
// a Close button. The verb's own execution outcome — incl. destroy's step-up
// password re-verification, which the broker runs async — surfaces in
// phase 2 keyed on the error KIND, never on message text.

function opSleep(ms) { return new Promise((r) => setTimeout(r, ms)); }

// Expected milestone checklist per verb — rendered UP FRONT (all rows pending)
// so the operator sees what's still to come, each row flipping to a green ✓ as
// its milestone lands. KEYS ARE LOCKSTEP with cli/sandboxcore.py PROGRESS_KEYS
// (pinned by tests/test_webui_locksteps.py). The terminal "done" record is NOT
// a row — it's the foot button that enables on completion. A milestone not
// pre-listed appends already-checked as it arrives, so a missing optional
// stage never leaves a stuck pending row.
const OP_CHECKLISTS = {
    create: [
        { key: "validate", label: "checking prerequisites" },
        { key: "gitea", label: "creating Gitea user and mirror" },
        { key: "build-image", label: "building agent image" },
        { key: "network", label: "creating project network" },
        { key: "create-container", label: "creating agent container" },
        { key: "route", label: "wiring network route and firewall" },
        { key: "agent-install", label: "installing agent CLI" },
        { key: "ready", label: "finalizing" },
    ],
    destroy: [
        { key: "validate", label: "locating project" },
        { key: "forwarder", label: "stopping port forwarder" },
        { key: "remove-container", label: "removing container, volume and workspace" },
        { key: "cleanup", label: "removing project network" },
        { key: "gitea", label: "deleting Gitea user and mirror" },
    ],
    start: [
        { key: "validate", label: "checking project" },
        { key: "start", label: "starting container" },
        { key: "route", label: "re-injecting route" },
    ],
    stop: [
        { key: "validate", label: "checking project" },
        { key: "stop", label: "stopping container" },
    ],
    sync: [
        { key: "validate", label: "checking project" },
        { key: "mirror", label: "syncing Gitea mirror" },
        { key: "pull", label: "pulling into workspace" },
    ],
};

// Human message for a failed op, from its structured result envelope.
// Keyed on the error KIND (never message text — the broker's step-up message
// is free to change without breaking this mapping).
function mgmtOpFailMsg(result) {
    const err = result && result.error;
    const kind = err && err.kind;
    if (kind === "step_up_required") return "Wrong password.";
    if (kind === "broker_unavailable") return "Broker unreachable.";
    if (err && err.message) return err.message;
    return kind || "operation failed";
}

// Phase 2: swap `card` to the expected-stage CHECKLIST and tail op `opId` to
// completion. Status (GET /broker/op/<id>) is the source of truth for
// completion — it covers the no-log failure paths (broker_unavailable,
// step-up reject, internal) where the broker never wrote a view file, and it
// SERVES-THEN-EVICTS: the first terminal read is authoritative; a later
// "unknown" after a seen log terminal means done (webui restarted mid-op).
async function mgmtTailOp(backdrop, card, opId, title, verb, onDone) {
    const checklist = OP_CHECKLISTS[verb] || [];
    const listEl = el("div", { class: "op-checklist" });
    const items = {};   // stepKey → { row, icon }
    const addRow = (key, label) => {
        const icon = el("span", { class: "op-check-icon" }, ["○"]);
        const row = el("div", { class: "op-check pending" },
                       [icon, el("span", { class: "op-check-label" }, [label])]);
        listEl.appendChild(row);
        items[key] = { row, icon };
        return items[key];
    };
    for (const it of checklist) addRow(it.key, it.label);
    const markDone = (key) => {
        const ref = items[key] || addRow(key, key);
        ref.row.classList.remove("pending");
        ref.row.classList.add("ok");
        ref.icon.textContent = "✓";
    };

    const failEl = el("div", { class: "op-fail" });
    // "Done" is the foot button, DISABLED until the op reaches a terminal
    // state, so the box can't be dismissed mid-op. The relay's background-op
    // catch-all guarantees the status reaches a terminal value within the op
    // timeout, so it always enables in-session.
    const doneBtn = el("button", { class: "btn", disabled: "" }, ["Working…"]);
    doneBtn.onclick = () => {
        if (doneBtn.disabled) return;
        backdrop.remove();
    };
    card.innerHTML = "";
    card.appendChild(el("h2", {}, [title]));
    card.appendChild(listEl);
    card.appendChild(failEl);
    card.appendChild(el("div", { class: "btn-row" }, [doneBtn]));

    let from = 0, done = false, result = null, ok = false;
    let sawTerminal = false, terminalOk = false;
    const drainLog = async () => {
        const r = await fetch(`/broker/op/${encodeURIComponent(opId)}/log?from=${from}`);
        const redirect = mgmtStatusRedirect(r.status);
        if (redirect) return redirect;          // truthy → caller dismisses + redirects
        const b = await r.json();
        if (b.started !== false && b.data) {
            from = b.next;
            for (const line of b.data.split("\n")) {
                if (!line.trim()) continue;
                let rec; try { rec = JSON.parse(line); } catch (e) { continue; }
                if (rec.status === "done") { sawTerminal = true; terminalOk = true; }
                else if (rec.status === "failed") { sawTerminal = true; terminalOk = false; }
                else if (rec.step) markDone(rec.step);
            }
        }
        return null;
    };

    while (!done) {
        // 1. Drain new view-log bytes, flipping each landed stage to ✓.
        try {
            const redirect = await drainLog();
            if (redirect) { backdrop.remove(); return redirect(); }
        } catch (e) { /* transient; the status poll below decides completion */ }
        // 2. Status — authoritative for completion (serve-then-evict).
        try {
            const r = await fetch(`/broker/op/${encodeURIComponent(opId)}`);
            const redirect = mgmtStatusRedirect(r.status);
            if (redirect) { backdrop.remove(); return redirect(); }
            const sb = await r.json();
            if (sb.state === "ok" || sb.state === "failed") {
                result = sb.result || null;
                ok = sb.state === "ok";
                done = true;
                break;
            }
            // state "unknown" → OP_RUNS entry gone (webui restarted mid-op, or
            // this poll raced the eviction); fall back to a log terminal if we
            // saw one, else keep polling for the file to appear.
            if (sb.state === "unknown" && sawTerminal) {
                ok = terminalOk; done = true; break;
            }
        } catch (e) { /* transient */ }
        await opSleep(OP_POLL_INTERVAL_MS);
    }
    // Final drain: the terminal milestone may have landed between this tick's
    // /log and /status fetches, so the trailing stage row is settled.
    try { await drainLog(); } catch (e) { /* best-effort */ }
    if (!ok) failEl.textContent = "Failed — " + mgmtOpFailMsg(result);
    doneBtn.textContent = "Done";
    doneBtn.disabled = false;
    if (onDone) { try { await onDone(ok, result); } catch (e) { /* best-effort */ } }
}

// Build the phase-1 confirm card; on confirm, fire `cfg.request()`, then hand
// the returned op_id to mgmtTailOp for phase 2. Used by create + destroy;
// start/stop/sync direct-fire through mgmtLifecycle below.
function mgmtConfirmThenTail(cfg) {
    // A dialog is already open — a fast double-click would otherwise stack
    // two backdrops.
    if (document.querySelector(".modal-backdrop")) return null;
    const backdrop = el("div", { class: "modal-backdrop" });
    const errEl = el("div", { class: "error" });
    const cancel = el("button", { class: "btn btn-secondary" }, ["Cancel"]);
    cancel.onclick = () => backdrop.remove();
    const go = el("button", { class: cfg.danger ? "btn btn-danger" : "btn" },
                 [cfg.confirmLabel]);
    const card = el("div", { class: cfg.cardClass ? "card " + cfg.cardClass : "card" }, [
        el("h2", {}, [cfg.title]),
        ...cfg.body,
        el("div", { class: "btn-row" }, [cancel, go]),
        errEl,
    ]);
    go.onclick = async () => {
        errEl.textContent = "";
        const verr = cfg.validate ? cfg.validate() : null;
        if (verr) { errEl.textContent = verr; return; }
        go.disabled = true; cancel.disabled = true;
        const orig = cfg.confirmLabel; go.textContent = "…";
        let res;
        try { res = await cfg.request(); }
        catch (e) {
            go.disabled = false; cancel.disabled = false; go.textContent = orig;
            errEl.textContent = "Broker unreachable."; return;
        }
        const redirect = mgmtStatusRedirect(res.status, cfg.retry);
        if (redirect) { backdrop.remove(); return redirect(); }
        let body; try { body = await res.json(); } catch (e) { body = {}; }
        // NOTE: the relay's op-start reply is a bare {op_id} — no `ok` field
        // (unlike the synchronous relays, which return the broker envelope).
        if (!body.op_id) {
            go.disabled = false; cancel.disabled = false; go.textContent = orig;
            errEl.textContent = "Failed: " + mgmtErrText(body); return;
        }
        await mgmtTailOp(backdrop, card, body.op_id,
                         cfg.tailTitle || cfg.title, cfg.verb, cfg.onDone);
    };
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
    if (cfg.focus) setTimeout(() => cfg.focus(), 50);
    return backdrop;
}

// Direct-fire lifecycle verbs (start/stop/sync): no confirm phase — the gear
// click fires the POST and the progress box opens straight into the tail.
function mgmtLifecycle(name, verb) {
    if (document.querySelector(".modal-backdrop")) return;
    const titles = { start: `Starting ${name}`, stop: `Stopping ${name}`,
                     sync: `Syncing ${name}` };
    const title = titles[verb] || `${verb} ${name}`;
    const backdrop = el("div", { class: "modal-backdrop" });
    const card = el("div", { class: "card" }, [
        el("h2", {}, [title]),
        el("div", { class: "mgmt-loading" }, ["Starting…"]),
    ]);
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
    (async () => {
        let res;
        try {
            res = await fetch(`/broker/project/${encodeURIComponent(name)}/${verb}`,
                { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
        } catch (e) {
            card.innerHTML = "";
            card.appendChild(el("h2", {}, [title]));
            card.appendChild(el("p", { class: "error" }, ["Broker unreachable."]));
            card.appendChild(el("div", { class: "btn-row" }, [mgmtCloseBtn(card)]));
            return;
        }
        const redirect = mgmtStatusRedirect(res.status, () => mgmtLifecycle(name, verb));
        if (redirect) { backdrop.remove(); return redirect(); }
        let body; try { body = await res.json(); } catch (e) { body = {}; }
        if (!body.op_id) {
            card.innerHTML = "";
            card.appendChild(el("h2", {}, [title]));
            card.appendChild(el("p", { class: "error" }, ["Failed: " + mgmtErrText(body)]));
            card.appendChild(el("div", { class: "btn-row" }, [mgmtCloseBtn(card)]));
            return;
        }
        await mgmtTailOp(backdrop, card, body.op_id, title, verb, async (ok) => {
            if (!ok) return;
            delete state.projectLastService[name];
            if (verb === "stop") {
                // The container is gone from under any open terminal — tear the
                // dead sessions down so the tab reopens cleanly after a start.
                teardownProjectState(name);
            }
            if (verb === "start") {
                await attachIntoVault(name);   // fresh JIT creds for the fresh container
            }
            await refreshBrokerList();
            refreshProjectRail();
        });
    })();
}

// ---- destroy (type-name confirm + step-up re-auth) --------------------------

function mgmtDestroyDialog(name) {
    const nameI = el("input", { type: "text", placeholder: name, autocomplete: "off" });
    const pwI = el("input", { type: "password", autocomplete: "current-password" });
    mgmtConfirmThenTail({
        title: "Destroy project",
        tailTitle: `Destroying ${name}`,
        verb: "destroy",
        confirmLabel: "Destroy",
        danger: true,
        retry: () => mgmtDestroyDialog(name),
        body: [
            el("p", {}, [
                `This permanently deletes "${name}" — its container, workspace, ` +
                "volume, network, and Gitea user. This cannot be undone.",
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Type the project name to confirm"]), nameI,
            ]),
            el("div", { class: "field" }, [
                el("label", {}, ["Re-enter your master password"]), pwI,
            ]),
        ],
        validate: () => {
            if (nameI.value.trim() !== name) return "Type the project name exactly to confirm.";
            if (!pwI.value) return "Re-enter your master password.";
            return null;
        },
        // Step-up: the retyped password is derived CLIENT-SIDE (unified login —
        // the raw password never transits) and the proof rides the request; the
        // broker re-verifies it async, so a wrong password surfaces as a FAILED
        // op in phase 2 ("Failed — Wrong password."), not an inline phase-1
        // error. Retype (vs reusing state.loginProof) is deliberate: it keeps
        // step-up meaningful against a stolen webui session cookie.
        request: async () => fetch(`/broker/project/${encodeURIComponent(name)}/destroy`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ proof: await deriveLoginProof(pwI.value) }),
        }),
        onDone: async (ok) => {
            if (!ok) return;
            // Tear down the gone project's open terminals/websockets (no dead
            // reconnect spam), drop it from the sidebar + persisted vault, then
            // refresh the rail behind the box so the row disappears.
            teardownProjectState(name);
            state.vault.projects = state.vault.projects.filter((p) => p.name !== name);
            try { await persistVault(); } catch (e) { /* best-effort */ }
            await refreshBrokerList();
            refreshProjectRail();
        },
        focus: () => nameI.focus(),
    });
}

// ---- "+ New project" dialog: create (broker) / import (vault) ---------------

// Radio-semantics card group: exactly one of `options` selected at a time
// (or none, when `allowNone` handled by the caller via a "None" option).
// Returns { wrap, get } where get() is the selected value ("" for None).
function makeRadioCards(options, selected) {
    let current = selected;
    const cards = options.map((opt) => {
        const card = el("div", {
            class: "box-opt-card" + (opt.value === current ? " selected" : ""),
        }, [el("span", { class: "box-opt-name" }, [opt.label])]);
        card.onclick = () => {
            current = opt.value;
            for (const c of cards) c.el.classList.toggle("selected", c.value === current);
        };
        return { el: card, value: opt.value };
    });
    const wrap = el("div", { class: "box-opt-cards" }, cards.map((c) => c.el));
    return { wrap, get: () => current };
}

function openNewProjectDialog(mode) {
    if (document.querySelector(".modal-backdrop")) return;
    const backdrop = el("div", { class: "modal-backdrop" });
    const card = el("div", { class: "card create-card" });
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
    renderNewProjectCard(backdrop, card, mode || "create");
}

function renderNewProjectCard(backdrop, card, mode) {
    card.innerHTML = "";
    card.appendChild(el("h2", {}, ["New project"]));

    // Mode switch: broker create vs vault import. Import is vault-only (no
    // fetch before submit), so it works with the broker fully stopped.
    const modeCards = makeRadioCards([
        { value: "create", label: "Create from GitHub" },
        { value: "import", label: "Import existing" },
    ], mode);
    for (const c of modeCards.wrap.children) {
        const prev = c.onclick;
        c.onclick = (ev) => {
            prev.call(c, ev);
            renderNewProjectCard(backdrop, card, modeCards.get());
        };
    }
    card.appendChild(el("div", { class: "field" }, [modeCards.wrap]));

    if (mode === "create") renderCreateSection(backdrop, card);
    else renderImportSection(backdrop, card);
}

function renderCreateSection(backdrop, card) {
    const section = el("div", { class: "create-section" }, [
        el("div", { class: "mgmt-loading" }, ["Loading catalog…"]),
    ]);
    card.appendChild(section);

    (async () => {
        let res;
        try { res = await fetch("/broker/catalog"); } catch (e) { res = null; }
        if (!res || res.status === 503 || res === null) {
            section.innerHTML = "";
            section.appendChild(el("p", {}, ["The broker isn't reachable. Start it on the host:"]));
            section.appendChild(el("pre", {}, ["python sandbox.py broker start"]));
            section.appendChild(el("div", { class: "btn-row" }, [mgmtCloseBtn(section)]));
            return;
        }
        if (res.status === 401) {
            section.innerHTML = "";
            section.appendChild(el("p", {}, ["Creating a project needs a management login."]));
            const connect = el("button", { class: "btn" }, ["Connect"]);
            connect.onclick = () => {
                backdrop.remove();
                openMgmtLoginModal(() => openNewProjectDialog("create"));
            };
            section.appendChild(el("div", { class: "btn-row" }, [mgmtCloseBtn(section), connect]));
            return;
        }
        if (res.status === 403) {
            backdrop.remove();
            return openMgmtCardModal(renderMgmtRejected);
        }
        let body; try { body = await res.json(); } catch (e) { body = {}; }
        if (!body.ok || !body.result) {
            section.innerHTML = "";
            section.appendChild(el("p", { class: "error" }, ["Catalog failed: " + mgmtErrText(body)]));
            section.appendChild(el("div", { class: "btn-row" }, [mgmtCloseBtn(section)]));
            return;
        }
        renderCreateForm(backdrop, card, section, body.result);
    })();
}

function renderCreateForm(backdrop, card, section, catalog) {
    const profiles = Array.isArray(catalog.profiles) ? catalog.profiles : [];
    const agents = Array.isArray(catalog.agents) ? catalog.agents : [];

    const urlI = el("input", { type: "text", autocomplete: "off",
                               placeholder: "https://github.com/user/repo" });
    const branchI = el("input", { type: "text", autocomplete: "off",
                                  placeholder: "default branch" });
    const memI = el("input", { type: "text", autocomplete: "off", placeholder: "e.g. 8g" });
    const cpusI = el("input", { type: "text", autocomplete: "off", placeholder: "e.g. 4" });

    // Egress radio (default locked — the sandbox's whole point).
    const egress = makeRadioCards([
        { value: "locked", label: "locked (80/443 only)" },
        { value: "open", label: "open" },
    ], "locked");

    // Profile cards fed by /broker/catalog — radio semantics. Default to
    // "python" when offered (the core's default image), else no selection
    // (field omitted → core default applies).
    const profileDefault = profiles.includes("python") ? "python" : "";
    const profile = makeRadioCards(
        profiles.map((p) => ({ value: p, label: p })), profileDefault);

    // Agent cards — radio + an explicit "None" card: ADS's `agent` is a
    // single enum (one agent per project), unlike RS's multi-select set.
    const agent = makeRadioCards(
        [{ value: "", label: "None" }].concat(agents.map((a) => ({ value: a, label: a }))),
        "");

    // DinD checkbox — inline label (NOT inside a .field, whose input/label
    // rules assume block text inputs and bleed onto checkboxes).
    const dockerCb = el("input", { type: "checkbox" });
    const dockerLabel = el("label", { class: "mgmt-check" },
                           [dockerCb, " Docker-in-Docker (sysbox runtime)"]);

    const errEl = el("div", { class: "error" });
    const cancel = el("button", { class: "btn btn-secondary" }, ["Cancel"]);
    cancel.onclick = () => backdrop.remove();
    const go = el("button", { class: "btn" }, ["Create"]);

    section.innerHTML = "";
    section.appendChild(el("div", { class: "field" }, [el("label", {}, ["GitHub URL"]), urlI]));
    section.appendChild(el("div", { class: "field" }, [el("label", {}, ["Branch (optional)"]), branchI]));
    section.appendChild(el("div", { class: "field" }, [el("label", {}, ["Egress"]), egress.wrap]));
    section.appendChild(el("div", { class: "box-opt-group" }, [
        el("div", { class: "box-opt-caption" }, ["Image profile"]),
        profile.wrap,
    ]));
    section.appendChild(el("div", { class: "box-opt-group" }, [
        el("div", { class: "box-opt-caption" }, ["Agent"]),
        agent.wrap,
    ]));
    section.appendChild(el("div", { class: "field-row" }, [
        el("div", { class: "field" }, [el("label", {}, ["Memory (optional)"]), memI]),
        el("div", { class: "field" }, [el("label", {}, ["CPUs (optional)"]), cpusI]),
    ]));
    section.appendChild(dockerLabel);
    section.appendChild(el("div", { class: "hint" }, [
        "Creating builds the agent image and installs the agent CLI — a cold build can take several minutes.",
    ]));
    section.appendChild(el("div", { class: "btn-row" }, [cancel, go]));
    section.appendChild(errEl);
    setTimeout(() => urlI.focus(), 50);

    go.onclick = async () => {
        errEl.textContent = "";
        const url = urlI.value.trim();
        if (!url) { errEl.textContent = "GitHub URL is required."; return; }
        if (!/^https?:\/\//.test(url)) {
            errEl.textContent = "The URL must start with http:// or https://.";
            return;
        }
        // Build the payload by iterating CREATE_FIELDS (the allowlist pin):
        // only non-empty values ride, so the core's defaults apply unshadowed.
        const values = {
            github_url: url,
            branch: branchI.value.trim(),
            egress: egress.get(),
            memory: memI.value.trim(),
            cpus: cpusI.value.trim(),
            profile: profile.get(),
            agent: agent.get(),
            docker: dockerCb.checked,
        };
        const payload = {};
        for (const f of CREATE_FIELDS) {
            const v = values[f];
            if (v === "" || v == null || v === false) continue;
            payload[f] = v;
        }
        go.disabled = true; cancel.disabled = true; go.textContent = "…";
        let res;
        try {
            res = await fetch("/broker/project", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
        } catch (e) {
            go.disabled = false; cancel.disabled = false; go.textContent = "Create";
            errEl.textContent = "Broker unreachable."; return;
        }
        const redirect = mgmtStatusRedirect(res.status, () => openNewProjectDialog("create"));
        if (redirect) { backdrop.remove(); return redirect(); }
        let body; try { body = await res.json(); } catch (e) { body = {}; }
        if (!body.op_id) {
            go.disabled = false; cancel.disabled = false; go.textContent = "Create";
            errEl.textContent = "Failed: " + mgmtErrText(body); return;
        }
        await mgmtTailOp(backdrop, card, body.op_id, "Creating project", "create",
            async (ok, result) => {
                if (!ok) return;
                // Surface the new project in the sidebar: JIT-fetch its creds
                // (never persist the CreateResult's ssh_password) + refresh.
                const project = result && result.result && result.result.project;
                if (project) await attachIntoVault(project);
                await refreshBrokerList();
                refreshProjectRail();
            });
    };
}

// Import mode: a vault-only bookmark (manual fallback for when the broker is
// down, or for non-broker SSH targets). No fetch before submit.
function renderImportSection(backdrop, card) {
    const importTa = el("textarea", { placeholder: "Paste sandbox.py import string (optional)" });
    const nameI = el("input", { type: "text" });
    const hostI = el("input", { type: "text", value: "host.docker.internal" });
    const portI = el("input", { type: "number", min: "1", max: "65535" });
    const userI = el("input", { type: "text", value: "agent" });
    const passI = el("input", { type: "password", autocomplete: "new-password" });
    const errEl = el("div", { class: "error" });

    importTa.oninput = () => {
        const s = importTa.value.trim();
        if (!s) return;
        try {
            const decoded = JSON.parse(atob(s));
            if (decoded.name) nameI.value = decoded.name;
            if (decoded.host) hostI.value = decoded.host;
            if (decoded.port) portI.value = decoded.port;
            if (decoded.username) userI.value = decoded.username;
            if (decoded.password) passI.value = decoded.password;
        } catch (_) { /* ignore non-import-string content */ }
    };

    const cancel = el("button", { class: "btn btn-secondary" }, ["Cancel"]);
    cancel.onclick = () => backdrop.remove();
    const save = el("button", { class: "btn" }, ["Import"]);
    save.onclick = async () => {
        const name = nameI.value.trim();
        const host = hostI.value.trim();
        const port = parseInt(portI.value, 10);
        const username = userI.value.trim() || "agent";
        const password = passI.value;
        if (!name || !host || !port || !password) {
            errEl.textContent = "Name, host, port, and password are required.";
            return;
        }
        if (state.vault.projects.some((p) => p.name === name)) {
            errEl.textContent = "A project with that name already exists.";
            return;
        }
        state.vault.projects.push({ name, host, port, username, password });
        try {
            await persistVault();
            backdrop.remove();
            refreshProjectRail();
        } catch (e) {
            errEl.textContent = "Save failed: " + e.message;
        }
    };

    const section = el("div", { class: "import-section" }, [
        el("div", { class: "field" }, [
            el("label", {}, ["Import string (optional)"]),
            importTa,
            el("div", { class: "hint" }, ["Paste the base64 string from sandbox.py create output to auto-fill the fields."]),
        ]),
        el("div", { class: "field" }, [el("label", {}, ["Project name"]), nameI]),
        el("div", { class: "field" }, [el("label", {}, ["Host"]), hostI]),
        el("div", { class: "field" }, [el("label", {}, ["SSH port"]), portI]),
        el("div", { class: "field" }, [el("label", {}, ["Username"]), userI]),
        el("div", { class: "field" }, [el("label", {}, ["Password"]), passI]),
        el("div", { class: "btn-row" }, [cancel, save]),
        errEl,
    ]);
    card.appendChild(section);
    setTimeout(() => nameI.focus(), 50);
}

// ---- port tabs (broker-owned webports registry) ------------------------------

// Registry management only in this stage: registered ports become embedded
// tabs once the origin-port proxy stamps origin_url on them (Stage 4).
function openWebportDialog(name) {
    if (document.querySelector(".modal-backdrop")) return;
    const backdrop = el("div", { class: "modal-backdrop" });
    const card = el("div", { class: "card" });
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);

    const render = async () => {
        card.innerHTML = "";
        card.appendChild(el("h2", {}, [`Port tabs — ${name}`]));
        card.appendChild(el("p", { class: "hint" }, [
            "Registered ports appear as embedded tabs once the HTTP-tab proxy ships; ",
            "the registry is broker-owned (agents can't add or retarget tabs).",
        ]));
        const listEl = el("div", { class: "webport-list" }, [
            el("div", { class: "mgmt-loading" }, ["Loading…"]),
        ]);
        card.appendChild(listEl);

        const portI = el("input", { type: "number", min: "1024", max: "65535",
                                    placeholder: "port (1024–65535)" });
        const labelI = el("input", { type: "text", placeholder: "label" });
        const errEl = el("div", { class: "error" });
        const addBtn = el("button", { class: "btn btn-small" }, ["Add"]);
        addBtn.onclick = async () => {
            errEl.textContent = "";
            // Payload fields ride from WEBPORT_FIELDS (allowlist pin).
            const values = { port: parseInt(portI.value, 10), label: labelI.value.trim() };
            const payload = {};
            for (const f of WEBPORT_FIELDS) payload[f] = values[f];
            let res;
            try {
                res = await fetch(`/broker/project/${encodeURIComponent(name)}/webport`, {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
            } catch (e) { errEl.textContent = "Broker unreachable."; return; }
            const redirect = mgmtStatusRedirect(res.status, () => openWebportDialog(name));
            if (redirect) { backdrop.remove(); return redirect(); }
            let body; try { body = await res.json(); } catch (e) { body = {}; }
            if (!body.ok) { errEl.textContent = mgmtErrText(body); return; }
            delete state.projectServices[name];
            render();
        };
        card.appendChild(el("div", { class: "field-row webport-add" }, [
            el("div", { class: "field" }, [el("label", {}, ["Port"]), portI]),
            el("div", { class: "field" }, [el("label", {}, ["Label"]), labelI]),
            addBtn,
        ]));
        card.appendChild(errEl);
        const close = el("button", { class: "btn btn-secondary" }, ["Close"]);
        close.onclick = () => backdrop.remove();
        card.appendChild(el("div", { class: "btn-row" }, [close]));

        // Load the current registry rows.
        let res;
        try { res = await fetch(`/broker/project/${encodeURIComponent(name)}/webports`); }
        catch (e) { listEl.textContent = "Broker unreachable."; return; }
        const redirect = mgmtStatusRedirect(res.status, () => openWebportDialog(name));
        if (redirect) { backdrop.remove(); return redirect(); }
        let body; try { body = await res.json(); } catch (e) { body = {}; }
        listEl.innerHTML = "";
        if (!body.ok) {
            listEl.appendChild(el("div", { class: "error" }, [mgmtErrText(body)]));
            return;
        }
        const rows = Array.isArray(body.result) ? body.result : [];
        if (rows.length === 0) {
            listEl.appendChild(el("div", { class: "hint" }, ["No port tabs registered."]));
            return;
        }
        for (const row of rows) {
            const rm = el("button", { class: "btn btn-small btn-secondary" }, ["Remove"]);
            rm.onclick = async () => {
                let r2;
                try {
                    r2 = await fetch(`/broker/project/${encodeURIComponent(name)}/webport-remove`, {
                        method: "POST", headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ port: row.port }),
                    });
                } catch (e) { errEl.textContent = "Broker unreachable."; return; }
                const rd = mgmtStatusRedirect(r2.status, () => openWebportDialog(name));
                if (rd) { backdrop.remove(); return rd(); }
                let b2; try { b2 = await r2.json(); } catch (e) { b2 = {}; }
                if (!b2.ok) { errEl.textContent = mgmtErrText(b2); return; }
                delete state.projectServices[name];
                render();
            };
            listEl.appendChild(el("div", { class: "webport-row" }, [
                el("span", { class: "webport-port" }, [String(row.port)]),
                el("span", { class: "webport-label" }, [row.label || ""]),
                rm,
            ]));
        }
    };
    render();
}

// ---- project activation + service tabs --------------------------------------

// Render-free teardown of a project's in-page state — close its terminals +
// websockets (so a destroyed/stopped container stops drawing reconnect
// attempts), drop its cached services, and clear it as active if it was.
function teardownProjectState(name) {
    for (const k of Object.keys(state.terminals)) {
        if (k.startsWith(`${name}:`)) {
            const t = state.terminals[k];
            try { if (t.ws) t.ws.close(); } catch (_) {}
            try { if (t.term) t.term.dispose(); } catch (_) {}
            try { if (t.container) t.container.remove(); } catch (_) {}
            delete state.terminals[k];
        }
    }
    delete state.projectServices[name];
    delete state.projectLastService[name];
    if (state.activeProject === name) {
        state.activeProject = null;
        state.activeService = null;
    }
}

async function activateProject(name) {
    document.querySelectorAll(".project-rail .project").forEach((r) => r.classList.remove("active"));
    const row = document.querySelector(`.project[data-name="${CSS.escape(name)}"]`);
    if (row) row.classList.add("active");

    // Unpinned + expanded means "I just opened the rail to switch projects" —
    // collapse it again now that the switch is done. Pinned rail stays put.
    if (!state.railPinned && state.railExpanded) {
        state.railExpanded = false;
        applyRailState();
    }

    state.activeProject = name;

    // A broker-listed row with no vault entry: JIT-attach if it's running,
    // else there is nothing to connect to — say so.
    let project = state.vault.projects.find((p) => p.name === name);
    if (!project) {
        const b = brokerRowFor(name);
        if (b && b.state === "running") {
            if (await attachIntoVault(name)) {
                refreshProjectRail();
                project = state.vault.projects.find((p) => p.name === name);
            }
        }
    }
    if (!project) {
        state.activeService = null;
        renderServiceTabs(name, []);
        showWelcome(`"${name}" is not running — use its gear menu to start it.`);
        return;
    }

    // Bust the per-project service cache on every activation so a service that
    // came up AFTER the first activation surfaces on re-select.
    delete state.projectServices[name];
    const services = await fetchProjectServices(name);

    renderServiceTabs(name, services);
    const visible = renderableServices(services);
    if (visible.length === 0) {
        state.activeService = null;
        showWelcome("No services available for this project.");
        return;
    }
    let next = state.projectLastService[name];
    if (!next || !visible.some((s) => s.id === next)) {
        next = visible[0].id;
    }
    activateService(next);
}

// A service renders as a tab when the frontend can actually open it: ssh
// always; http only once the origin-port proxy has stamped origin_url on it
// (Stage 4) — until then http entries (editor, webports) simply don't show.
function renderableServices(services) {
    return (services || []).filter((s) =>
        s.kind === "ssh" || (s.kind === "http" && s.origin_url));
}

// Hand-authored inline SVG glyphs (CSP-clean: inline markup, currentColor,
// no external refs). Built via innerHTML — the makePinButton() precedent —
// because el() routes through createElement, which makes an inert
// HTML-namespace <svg> that does not render.
const TAB_ICON_SVG = {
    editor: '<svg viewBox="0 0 16 16" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M5.5 3.4 1 8l4.5 4.6L7 11.1 3.9 8 7 4.9zM10.5 3.4 9 4.9 12.1 8 9 11.1l1.5 1.5L15 8z"/></svg>',
    terminal: '<svg viewBox="0 0 16 16" fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M2 2.9 3.4 1.5 9.9 8l-6.5 6.5L2 13.1 7.1 8z"/><path d="M8 12h6v2H8z"/></svg>',
    generic: '<svg viewBox="0 0 16 16" fill="currentColor" fill-rule="evenodd" xmlns="http://www.w3.org/2000/svg"><path d="M1.5 3h13v10h-13V3zm1.5 1.5v7h10v-7H3z"/></svg>',
};

function iconSvg(name) {
    const span = el("span", { class: "tab-icon" });
    span.innerHTML = TAB_ICON_SVG[name] || TAB_ICON_SVG.generic;
    return span;
}

function iconOf(svc) {
    if (svc.id === "editor") return "editor";
    return svc.kind === "ssh" ? "terminal" : "generic";
}

function renderServiceTabs(projectName, services) {
    const strip = document.getElementById("service-tabs");
    if (!strip) return;
    strip.innerHTML = "";
    strip.appendChild(makeProjectsTab());
    // Active-project label — a bordered chip (distinct from the service tabs).
    strip.appendChild(el("div", { class: "active-project" }, [projectName]));
    strip.appendChild(el("div", { class: "tab-group-divider" }));
    const visible = renderableServices(services);
    if (visible.length === 0) {
        strip.appendChild(el("div", { class: "empty" }, [
            "No services available for this project.",
        ]));
        return;
    }
    // Partition into Visual (iframe surfaces — Stage 4) then CLI (terminals),
    // preserving catalog order within each group. A thin vertical rule
    // separates the groups, omitted when either is empty.
    const visual = visible.filter((s) => s.kind === "http");
    const cli = visible.filter((s) => s.kind !== "http");
    const makeTab = (svc) => el("div", {
        class: "tab",
        "data-service": svc.id,
        onclick: () => activateService(svc.id),
    }, [iconSvg(iconOf(svc)), el("span", {}, [svc.label || svc.id])]);
    for (const svc of visual) strip.appendChild(makeTab(svc));
    if (visual.length > 0 && cli.length > 0) {
        strip.appendChild(el("div", { class: "tab-group-divider" }));
    }
    for (const svc of cli) strip.appendChild(makeTab(svc));
}

function activateService(serviceId) {
    if (!state.activeProject) return;

    const project = state.vault.projects.find((p) => p.name === state.activeProject);
    if (!project) return;
    const services = state.projectServices[state.activeProject] || [];
    const svc = services.find((s) => s.id === serviceId);
    if (!svc) return;

    document.querySelectorAll(".service-tabs .tab").forEach((t) => t.classList.remove("active"));
    const tabEl = document.querySelector(`.service-tabs .tab[data-service="${CSS.escape(serviceId)}"]`);
    if (tabEl) tabEl.classList.add("active");
    state.activeService = serviceId;
    state.projectLastService[state.activeProject] = serviceId;

    // Hide everything except the active terminal.
    const activeKey = tkey(state.activeProject, serviceId);
    for (const [k, t] of Object.entries(state.terminals)) {
        if (!t.container) continue;
        if (k === activeKey) t.container.classList.remove("hidden");
        else t.container.classList.add("hidden");
    }
    const welcome = document.getElementById("welcome");
    if (welcome) welcome.style.display = "none";

    const existing = state.terminals[activeKey];
    if (existing && !existing.disconnected) {
        if (existing.container) existing.container.classList.remove("hidden");
        if (existing.fitAddon) existing.fitAddon.fit();
        if (existing.term) existing.term.focus();
        return;
    }
    if (existing && existing.disconnected) {
        // Tear down the dead terminal so the open path below creates a fresh
        // one. Scroll buffer is lost on reconnect — acceptable.
        try { if (existing.ws) existing.ws.close(); } catch (_) {}
        try { if (existing.term) existing.term.dispose(); } catch (_) {}
        try { if (existing.container) existing.container.remove(); } catch (_) {}
        delete state.terminals[activeKey];
    }

    if (svc.kind === "ssh") {
        openSshTerminal(project, serviceId, svc);
    } else {
        // http tabs become openable in Stage 4 (origin-port proxy); a
        // renderable http entry can't exist before then, so this is a guard.
        const parent = document.getElementById("terminal-area");
        parent.appendChild(el("div", { class: "welcome" }, [
            `Service kind "${svc.kind}" is not supported yet.`,
        ]));
    }
}

function showWelcome(msg) {
    for (const t of Object.values(state.terminals)) {
        if (t.container) t.container.classList.add("hidden");
    }
    const welcome = document.getElementById("welcome");
    if (welcome) {
        welcome.style.display = "";
        if (msg) welcome.textContent = msg;
    }
}

// ---- search bar ------------------------------------------------------------

let searchBarEl = null;
let searchInputEl = null;

function ensureSearchBar() {
    if (searchBarEl) return searchBarEl;
    const input = el("input", { type: "text", placeholder: "Search…", spellcheck: "false" });
    const prev = el("button", { class: "search-btn", title: "Previous (Shift+Enter)" }, ["↑"]);
    const next = el("button", { class: "search-btn", title: "Next (Enter)" }, ["↓"]);
    const close = el("button", { class: "search-btn", title: "Close (Esc)" }, ["×"]);

    const bar = el("div", { class: "search-bar hidden" }, [input, prev, next, close]);

    const find = (forward) => {
        const t = activeTerminal();
        if (!t || !t.searchAddon || !input.value) return;
        const opts = { regex: false, wholeWord: false, caseSensitive: false };
        if (forward) t.searchAddon.findNext(input.value, opts);
        else t.searchAddon.findPrevious(input.value, opts);
    };
    input.oninput = () => find(true);
    input.onkeydown = (e) => {
        if (e.key === "Enter") { find(!e.shiftKey); e.preventDefault(); }
        else if (e.key === "Escape") { closeSearchBar(); e.preventDefault(); }
    };
    next.onclick = () => find(true);
    prev.onclick = () => find(false);
    close.onclick = closeSearchBar;

    searchBarEl = bar;
    searchInputEl = input;
    return bar;
}

function activeTerminal() {
    if (!state.activeProject || !state.activeService) return null;
    return state.terminals[tkey(state.activeProject, state.activeService)] || null;
}

function openSearchBar() {
    const bar = ensureSearchBar();
    const termArea = document.getElementById("terminal-area");
    if (termArea && bar.parentElement !== termArea) termArea.appendChild(bar);
    bar.classList.remove("hidden");
    searchInputEl.focus();
    searchInputEl.select();
}

function closeSearchBar() {
    if (searchBarEl) searchBarEl.classList.add("hidden");
    const t = activeTerminal();
    if (t && t.term) t.term.focus();
}

// ---- ssh terminal & WS -------------------------------------------------------

function openSshTerminal(project, serviceId, svc) {
    const container = el("div", { class: "terminal-instance" });
    // Inset wrapper: gives the visual breathing room WITHOUT putting padding
    // on the element xterm-fit measures. See style.css comment on
    // .terminal-pad for the fit-addon quirk this works around.
    const pad = el("div", { class: "terminal-pad" });
    container.appendChild(pad);
    document.getElementById("terminal-area").appendChild(container);

    const term = new Terminal({
        cursorBlink: true,
        fontFamily: "ui-monospace, Menlo, Consolas, monospace",
        fontSize: 13,
        theme: currentXtermTheme(),
        scrollback: 5000,
    });
    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.loadAddon(new WebLinksAddon.WebLinksAddon(
        (event, uri) => window.open(uri, "_blank", "noopener,noreferrer"),
    ));
    const searchAddon = new SearchAddon.SearchAddon();
    term.loadAddon(searchAddon);
    term.open(pad);
    try {
        const webgl = new WebglAddon.WebglAddon();
        webgl.onContextLoss(() => webgl.dispose());
        term.loadAddon(webgl);
    } catch (_) {
        // WebGL unavailable; xterm falls back to canvas/DOM renderer.
    }
    fitAddon.fit();
    term.focus();
    term.attachCustomKeyEventHandler((ev) => {
        if (ev.type === "keydown" && ev.ctrlKey && !ev.altKey && !ev.metaKey && !ev.shiftKey
            && (ev.key === "f" || ev.key === "F")) {
            ev.preventDefault();
            openSearchBar();
            return false;
        }
        return true;
    });
    term.onSelectionChange(() => {
        const sel = term.getSelection();
        if (sel) navigator.clipboard.writeText(sel).catch(() => {});
    });
    // OSC 52 — tmux/byobu (with set-clipboard on) emits this after every copy,
    // which lets users copy from inside mouse mode without bypassing it.
    term.parser.registerOscHandler(52, (data) => {
        const semi = data.indexOf(";");
        if (semi < 0) return false;
        const payload = data.slice(semi + 1);
        if (payload === "?") return true; // query — silently ignored for security
        try {
            const text = atob(payload);
            if (text) navigator.clipboard.writeText(text).catch(() => {});
            return true;
        } catch (_) {
            return false;
        }
    });

    const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${wsProto}//${location.host}/tab`);
    ws.binaryType = "arraybuffer";

    const key = tkey(project.name, serviceId);
    state.terminals[key] = {
        term, fitAddon, searchAddon, ws, container, project, service: serviceId,
    };

    ws.onopen = () => {
        ws.send(JSON.stringify({
            type: "connect",
            host: project.host,
            port: project.port || (svc && svc.default_port) || 22,
            username: project.username || "agent",
            password: project.password,
            fingerprint: project.host_key_fingerprint || null,
            rows: term.rows,
            cols: term.cols,
        }));
    };

    ws.onmessage = async (ev) => {
        if (typeof ev.data === "string") {
            let ctrl;
            try { ctrl = JSON.parse(ev.data); } catch (_) { return; }
            await handleControl(project, serviceId, term, ws, ctrl);
        } else {
            term.write(new Uint8Array(ev.data));
        }
    };

    ws.onclose = () => {
        term.writeln("\r\n\x1b[90m[disconnected — click the tab again to reconnect]\x1b[0m");
        const k = tkey(project.name, serviceId);
        // Mark for teardown on the next activateService(serviceId) — the
        // fast-path early-return would otherwise just re-show the stale,
        // disconnected terminal without reopening the WS.
        if (state.terminals[k]) state.terminals[k].disconnected = true;
    };

    term.onData((d) => {
        if (ws.readyState === WebSocket.OPEN) {
            ws.send(new TextEncoder().encode(d));
        }
    });

    term.onResize(({ rows, cols }) => {
        if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "resize", rows, cols }));
        }
    });

    window.addEventListener("resize", () => {
        if (state.activeProject === project.name && state.activeService === serviceId) {
            fitAddon.fit();
        }
    });
}

async function handleControl(project, serviceId, term, ws, ctrl) {
    if (ctrl.type === "connected") {
        if (!project.host_key_fingerprint) {
            project.host_key_fingerprint = ctrl.fingerprint;
            await persistVault();
            term.writeln(`\r\n\x1b[90m[connected — host key recorded: ${ctrl.fingerprint}]\x1b[0m`);
        } else {
            term.writeln(`\r\n\x1b[90m[connected]\x1b[0m`);
        }
    } else if (ctrl.type === "fingerprint_mismatch") {
        const accept = confirm(
            `Host key for "${project.name}" has CHANGED.\n\n` +
            `Stored: ${project.host_key_fingerprint}\n` +
            `Actual: ${ctrl.actual}\n\n` +
            `Accept the new key?\n\n` +
            `Click OK only if you intentionally recreated the agent — otherwise this could be a man-in-the-middle.`,
        );
        if (accept) {
            project.host_key_fingerprint = ctrl.actual;
            await persistVault();
            term.writeln("\r\n\x1b[33m[host key updated; click the tab to reconnect]\x1b[0m");
            const k = tkey(project.name, serviceId);
            const t = state.terminals[k];
            if (t) {
                try { t.ws.close(); } catch (_) {}
                delete state.terminals[k];
            }
        } else {
            term.writeln("\r\n\x1b[31m[host key mismatch — connection rejected]\x1b[0m");
        }
    } else if (ctrl.type === "auth_failed") {
        term.writeln("\r\n\x1b[31m[auth failed — check the saved password]\x1b[0m");
    } else if (ctrl.type === "error") {
        term.writeln(`\r\n\x1b[31m[error: ${ctrl.msg}]\x1b[0m`);
    }
}

// ---- bootstrap -------------------------------------------------------------

window.addEventListener("DOMContentLoaded", () => {
    applyTheme(loadStoredTheme());
    state.railPinned = loadRailPinned();
    state.railExpanded = state.railPinned;
    state.railWidth = loadRailWidth();
    applyRailWidth(state.railWidth);
    installRailOutsideClickHandlers();
    if (loadStored()) renderUnlock();
    else renderSetup();
});
