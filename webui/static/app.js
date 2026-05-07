// Sandbox WebUI — SSH terminal aggregator.
// All persistent state lives in browser localStorage, encrypted with a
// PBKDF2-derived AES-GCM key. The decrypted vault and derived key live in
// JS memory only while unlocked; both are dropped on lock or refresh.

const VAULT_KEY = "sandbox-webui-vault";
const THEME_KEY = "sandbox-webui-theme";
const PBKDF2_ITERATIONS = 600000;
const PROBE_INTERVAL_MS = 15000;

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

const state = {
    derivedKey: null,   // CryptoKey | null
    salt: null,         // Uint8Array | null
    vault: null,        // { version, projects, settings } | null
    activeTab: null,    // string | null
    terminals: {},      // name -> { term, fitAddon, ws, container, project }
    probeTimer: null,
    theme: null,        // theme id; set by applyTheme on boot
    giteaUrl: null,     // string | null — populated from /config on dashboard render
};

// ---- utilities -------------------------------------------------------------

const $ = (sel) => document.querySelector(sel);
const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));
const ub64 = (s) => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

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
    const enc = await encryptVault(state.derivedKey, state.vault);
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
        if (pw1.value.length < 8) {
            errEl.textContent = "Password must be at least 8 characters.";
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
            state.vault = { version: 1, projects: [], settings: {} };
            await persistVault();
            renderDashboard();
        } catch (e) {
            errEl.textContent = "Setup failed: " + e.message;
        }
    };

    const card = el("div", { class: "card" }, [
        el("h2", {}, ["Set master password"]),
        el("p", {}, [
            "This password encrypts your saved agent credentials. There is no recovery — if you forget it, you'll need to re-add each project.",
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
            state.derivedKey = key;
            state.salt = salt;
            state.vault = vault;
            renderDashboard();
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

function renderDashboard() {
    clearBody();
    const tabbar = el("div", { class: "tabbar" });
    const giteaTab = makeGiteaTabEl();
    if (giteaTab) tabbar.appendChild(giteaTab);
    for (const p of state.vault.projects) {
        tabbar.appendChild(makeTabEl(p));
    }
    tabbar.appendChild(el("div", { class: "tab add", onclick: openAddProjectModal }, ["+ Add project"]));
    tabbar.appendChild(el("div", { class: "spacer" }));
    tabbar.appendChild(makeThemeSelector());
    tabbar.appendChild(el("button", { class: "lock-btn", onclick: lockVault }, ["Lock"]));

    const termArea = el("div", { class: "terminal-area", id: "terminal-area" });
    const welcomeText = state.vault.projects.length === 0
        ? "No projects yet. Click + Add project to register an agent."
        : "Click a tab to attach.";
    termArea.appendChild(el("div", { class: "welcome", id: "welcome" }, [welcomeText]));

    const dashboard = el("div", { class: "dashboard" }, [tabbar, termArea]);
    document.body.appendChild(el("div", { id: "app" }, [dashboard]));

    schedulePolling();
    refreshGiteaTab();
}

async function refreshGiteaTab() {
    try {
        const res = await fetch("/config");
        const data = await res.json();
        state.giteaUrl = data.gitea_url || null;
    } catch (_) {
        state.giteaUrl = null;
    }
    const existing = document.querySelector(".tab.gitea");
    if (state.giteaUrl) {
        if (!existing) {
            const tabbar = document.querySelector(".tabbar");
            if (tabbar) tabbar.insertBefore(makeGiteaTabEl(), tabbar.firstChild);
        }
    } else if (existing) {
        existing.remove();
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
        const t = state.terminals[state.activeTab];
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
    const t = state.terminals[state.activeTab];
    if (t && t.term) t.term.focus();
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

function makeGiteaTabEl() {
    if (!state.giteaUrl) return null;
    return el("div", {
        class: "tab gitea",
        title: `Open Gitea (${state.giteaUrl}) in a new tab`,
        onclick: openGitea,
    }, ["Gitea ↗"]);
}

function openGitea() {
    if (!state.giteaUrl) return;
    window.open(state.giteaUrl, "_blank", "noopener,noreferrer");
}

function makeTabEl(project) {
    const dot = el("span", { class: "status-dot" });
    const closeX = el("span", { class: "close-x", title: "Remove project" }, ["×"]);
    closeX.onclick = (ev) => {
        ev.stopPropagation();
        if (confirm(`Remove project "${project.name}"? (Agent and its container are not affected.)`)) {
            removeProject(project.name);
        }
    };
    const tab = el("div", {
        class: "tab",
        "data-name": project.name,
        onclick: () => activateTab(project.name),
    }, [dot, document.createTextNode(project.name), closeX]);
    return tab;
}

function schedulePolling() {
    if (state.probeTimer) clearInterval(state.probeTimer);
    const probeAll = () => {
        for (const p of state.vault.projects) probeProject(p);
    };
    probeAll();
    state.probeTimer = setInterval(probeAll, PROBE_INTERVAL_MS);
}

async function probeProject(project) {
    try {
        const url = `/probe?host=${encodeURIComponent(project.host)}&port=${project.port}`;
        const res = await fetch(url);
        const data = await res.json();
        const tab = document.querySelector(`[data-name="${CSS.escape(project.name)}"]`);
        if (!tab) return;
        tab.classList.toggle("up", !!data.up);
        tab.classList.toggle("down", !data.up);
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
    state.derivedKey = null;
    state.vault = null;
    state.salt = null;
    state.terminals = {};
    state.activeTab = null;
    state.giteaUrl = null;
    renderUnlock();
}

// ---- add / remove project --------------------------------------------------

function openAddProjectModal() {
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

    const backdrop = el("div", { class: "modal-backdrop" });
    const cancel = el("button", { class: "btn btn-secondary" }, ["Cancel"]);
    cancel.onclick = () => backdrop.remove();

    const save = el("button", { class: "btn" }, ["Add"]);
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
            renderDashboard();
        } catch (e) {
            errEl.textContent = "Save failed: " + e.message;
        }
    };

    const card = el("div", { class: "card" }, [
        el("h2", {}, ["Add project"]),
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
    backdrop.appendChild(card);
    document.body.appendChild(backdrop);
}

async function removeProject(name) {
    const t = state.terminals[name];
    if (t) {
        try { if (t.ws) t.ws.close(); } catch (_) {}
        try { if (t.term) t.term.dispose(); } catch (_) {}
        delete state.terminals[name];
    }
    state.vault.projects = state.vault.projects.filter((p) => p.name !== name);
    if (state.activeTab === name) state.activeTab = null;
    await persistVault();
    renderDashboard();
}

// ---- terminal & WS ---------------------------------------------------------

function activateTab(name) {
    for (const t of Object.values(state.terminals)) {
        if (t.container) t.container.classList.add("hidden");
    }
    const welcome = document.getElementById("welcome");
    if (welcome) welcome.style.display = "none";

    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    const tabEl = document.querySelector(`[data-name="${CSS.escape(name)}"]`);
    if (tabEl) tabEl.classList.add("active");

    state.activeTab = name;

    if (state.terminals[name]) {
        state.terminals[name].container.classList.remove("hidden");
        state.terminals[name].fitAddon.fit();
        state.terminals[name].term.focus();
        return;
    }

    const project = state.vault.projects.find((p) => p.name === name);
    if (!project) return;
    openTerminal(project);
}

function openTerminal(project) {
    const container = el("div", { class: "terminal-instance" });
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
    term.open(container);
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

    state.terminals[project.name] = { term, fitAddon, searchAddon, ws, container, project };

    ws.onopen = () => {
        ws.send(JSON.stringify({
            type: "connect",
            host: project.host,
            port: project.port,
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
            await handleControl(project, term, ws, ctrl);
        } else {
            term.write(new Uint8Array(ev.data));
        }
    };

    ws.onclose = () => {
        term.writeln("\r\n\x1b[90m[disconnected — click the tab again to reconnect]\x1b[0m");
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
        if (state.activeTab === project.name) fitAddon.fit();
    });
}

async function handleControl(project, term, ws, ctrl) {
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
            const t = state.terminals[project.name];
            if (t) {
                try { t.ws.close(); } catch (_) {}
                delete state.terminals[project.name];
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
    if (loadStored()) renderUnlock();
    else renderSetup();
});
