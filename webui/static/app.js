// Sandbox WebUI — SSH terminal aggregator.
// All persistent state lives in browser localStorage, encrypted with a
// PBKDF2-derived AES-GCM key. The decrypted vault and derived key live in
// JS memory only while unlocked; both are dropped on lock or refresh.

const VAULT_KEY = "sandbox-webui-vault";
const PBKDF2_ITERATIONS = 600000;
const PROBE_INTERVAL_MS = 15000;

const state = {
    derivedKey: null,   // CryptoKey | null
    salt: null,         // Uint8Array | null
    vault: null,        // { version, projects, settings } | null
    activeTab: null,    // string | null
    terminals: {},      // name -> { term, fitAddon, ws, container, project }
    probeTimer: null,
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
    for (const p of state.vault.projects) {
        tabbar.appendChild(makeTabEl(p));
    }
    tabbar.appendChild(el("div", { class: "tab add", onclick: openAddProjectModal }, ["+ Add project"]));
    tabbar.appendChild(el("div", { class: "spacer" }));
    tabbar.appendChild(el("button", { class: "lock-btn", onclick: lockVault }, ["Lock"]));

    const termArea = el("div", { class: "terminal-area", id: "terminal-area" });
    const welcomeText = state.vault.projects.length === 0
        ? "No projects yet. Click + Add project to register an agent."
        : "Click a tab to attach.";
    termArea.appendChild(el("div", { class: "welcome", id: "welcome" }, [welcomeText]));

    const dashboard = el("div", { class: "dashboard" }, [tabbar, termArea]);
    document.body.appendChild(el("div", { id: "app" }, [dashboard]));

    schedulePolling();
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
        theme: { background: "#000", foreground: "#e0e0e0" },
    });
    const fitAddon = new FitAddon.FitAddon();
    term.loadAddon(fitAddon);
    term.open(container);
    fitAddon.fit();
    term.focus();

    const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${wsProto}//${location.host}/tab`);
    ws.binaryType = "arraybuffer";

    state.terminals[project.name] = { term, fitAddon, ws, container, project };

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
    if (loadStored()) renderUnlock();
    else renderSetup();
});
