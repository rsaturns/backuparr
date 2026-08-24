"use strict";

let APP_META = [];
let RUN_POLL_TIMER = null;
let SETTINGS_SNAPSHOT = null;

async function apiFetch(url, opts) {
  const res = await fetch(url, opts);
  let body = null;
  try {
    body = await res.json();
  } catch (e) {
    body = null;
  }
  if (!res.ok) {
    const message = (body && (body.error || body.message)) || `HTTP ${res.status}`;
    throw new Error(message);
  }
  return body;
}

function toast(message, kind) {
  const root = document.getElementById("toast-root");
  const el = document.createElement("div");
  el.className = `toast ${kind || ""}`;
  el.textContent = message;
  root.appendChild(el);
  setTimeout(() => el.remove(), 5000);
}

function fmtBytes(n) {
  if (n == null) return "-";
  const units = ["B", "KB", "MB", "GB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtTime(iso) {
  if (!iso) return "-";
  try {
    return new Date(iso).toLocaleString();
  } catch (e) {
    return iso;
  }
}

// ---------------------------------------------------------------- tabs ----
function initTabs() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const current = document.querySelector(".tab-btn.active");
      const leavingDirtySettings = current && current.dataset.tab === "settings" && btn !== current && isSettingsDirty();
      if (leavingDirtySettings) {
        const leave = confirm("You have unsaved changes in Settings. Leave without saving?");
        if (!leave) return;
      }
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
      if (btn.dataset.tab === "overview") loadOverview();
      if (btn.dataset.tab === "run") refreshRunStatus();
      if (btn.dataset.tab === "history") loadHistory();
      if (btn.dataset.tab === "restore") initRestoreTab();
    });
  });
}

// ----------------------------------------------------------- overview ----
async function loadOverview() {
  const summaryEl = document.getElementById("overview-summary");
  const appsEl = document.getElementById("overview-apps");
  summaryEl.textContent = "Loading...";
  appsEl.innerHTML = "";
  try {
    const [cfg, history, status] = await Promise.all([
      apiFetch("/api/config"),
      apiFetch("/api/history"),
      apiFetch("/api/backup/status"),
    ]);
    const allIds = Object.keys(cfg.apps || {});
    const enabledIds = allIds.filter((id) => cfg.apps[id].enabled);

    // "<app>: <error message>" strings, see run_backup() in backup.py.
    const failureByApp = {};
    (status.failed || []).forEach((entry) => {
      const idx = entry.indexOf(":");
      if (idx > -1) failureByApp[entry.slice(0, idx)] = entry.slice(idx + 1).trim();
    });

    summaryEl.innerHTML = "";
    const stats = [
      { label: "Enabled services", value: `${enabledIds.length} / ${allIds.length}` },
      { label: "Google Drive remote", value: cfg.rclone_remote || "Not configured" },
      { label: "Cron schedule", value: cfg.cron_schedule || "-" },
      { label: "Retention", value: `${cfg.retention_days || 14} days` },
    ];
    stats.forEach((s) => {
      const div = document.createElement("div");
      div.className = "overview-stat";
      const value = document.createElement("div");
      value.className = "stat-value";
      value.textContent = s.value;
      const label = document.createElement("div");
      label.className = "stat-label";
      label.textContent = s.label;
      div.append(value, label);
      summaryEl.appendChild(div);
    });

    if (!enabledIds.length) {
      appsEl.innerHTML = '<p class="overview-empty">No services enabled yet - head to Settings to add one.</p>';
      return;
    }

    enabledIds.forEach((appId) => {
      const meta = appMeta(appId);
      const latest = (history[appId] || [])[0];
      const failure = failureByApp[appId];

      const card = document.createElement("div");
      card.className = "overview-app-card";

      const head = document.createElement("div");
      head.className = "overview-app-head";
      if (meta) {
        const img = document.createElement("img");
        img.className = "app-icon";
        img.src = `/static/icons/${meta.icon}`;
        img.alt = "";
        head.appendChild(img);
      }
      const h3 = document.createElement("h3");
      h3.textContent = appLabel(appId);
      head.appendChild(h3);
      const dot = document.createElement("span");
      dot.className = "status-dot";
      dot.dataset.state = failure ? "fail" : latest ? "ok" : "idle";
      head.appendChild(dot);
      card.appendChild(head);

      const last = document.createElement("div");
      last.className = "overview-app-last";
      if (failure) {
        last.textContent = `Last run failed: ${failure}`;
      } else if (latest) {
        const summaryLine = document.createElement("span");
        summaryLine.textContent = `Last backup: ${fmtTime(latest.mod_time)} (${fmtBytes(latest.size)})`;
        const fn = document.createElement("span");
        fn.className = "filename";
        fn.textContent = latest.name;
        last.append(summaryLine, fn);
      } else {
        last.textContent = "No backups yet.";
      }
      card.appendChild(last);
      appsEl.appendChild(card);
    });
  } catch (e) {
    summaryEl.innerHTML = "";
    appsEl.innerHTML = `<p class="overview-empty">Error loading overview: ${e.message}</p>`;
  }
}

// ------------------------------------------------------------- layout ----
// Keeps the sticky Settings toolbar pinned just below the sticky topbar
// instead of overlapping it, whatever the topbar's actual rendered height
// (which varies with font size/zoom/wrapping).
function syncTopbarHeight() {
  const topbar = document.querySelector(".topbar");
  if (topbar) document.documentElement.style.setProperty("--topbar-h", `${topbar.getBoundingClientRect().height}px`);
}

// -------------------------------------------------------------- theme ----
function effectiveTheme() {
  const explicit = document.documentElement.getAttribute("data-theme");
  if (explicit === "light" || explicit === "dark") return explicit;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function applyThemeButton() {
  const btn = document.getElementById("theme-toggle");
  const theme = effectiveTheme();
  btn.textContent = theme === "dark" ? "☀" : "☽"; // sun / moon
  btn.setAttribute("aria-label", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
}

function initTheme() {
  applyThemeButton();
  document.getElementById("theme-toggle").addEventListener("click", () => {
    const next = effectiveTheme() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("arr-backup-theme", next);
    } catch (e) {}
    applyThemeButton();
  });
}

// ------------------------------------------------------------- settings ----
function appCard(appId) {
  return document.querySelector(`.app-card[data-app="${appId}"]`);
}

function setAppCardExpanded(card, expanded) {
  card.querySelector(".app-card-body").classList.toggle("hidden", !expanded);
}

function fillAppCard(appId, cfg) {
  const card = appCard(appId);
  if (!card) return;
  card.querySelector(".f-enabled").checked = !!cfg.enabled;
  card.querySelector(".f-url").value = cfg.url || "";
  card.querySelector(".f-api_key").value = cfg.api_key || "";
  card.querySelectorAll(".f-extra").forEach((input) => {
    input.value = cfg[input.dataset.field] || "";
  });
  setAppCardExpanded(card, !!cfg.enabled);
}

function readAppCard(appId) {
  const card = appCard(appId);
  const out = {
    enabled: card.querySelector(".f-enabled").checked,
    url: card.querySelector(".f-url").value.trim(),
    api_key: card.querySelector(".f-api_key").value.trim(),
  };
  card.querySelectorAll(".f-extra").forEach((input) => {
    out[input.dataset.field] = input.value;
  });
  return out;
}

async function loadConfig() {
  const cfg = await apiFetch("/api/config");
  document.getElementById("s-rclone_remote").value = cfg.rclone_remote || "";
  document.getElementById("s-retention_days").value = cfg.retention_days || 14;
  document.getElementById("s-cron_schedule").value = cfg.cron_schedule || "";
  document.getElementById("s-notify_url").value = cfg.notify_url || "";
  document.getElementById("s-bazarr_backup_dir").value = cfg.bazarr_backup_dir || "";
  Object.keys(cfg.apps || {}).forEach((appId) => fillAppCard(appId, cfg.apps[appId]));
  snapshotSettingsState();
  return cfg;
}

function collectConfig() {
  const apps = {};
  document.querySelectorAll(".app-card").forEach((card) => {
    apps[card.dataset.app] = readAppCard(card.dataset.app);
  });
  return {
    rclone_remote: document.getElementById("s-rclone_remote").value.trim(),
    retention_days: Number(document.getElementById("s-retention_days").value || 14),
    cron_schedule: document.getElementById("s-cron_schedule").value.trim(),
    notify_url: document.getElementById("s-notify_url").value.trim(),
    bazarr_backup_dir: document.getElementById("s-bazarr_backup_dir").value.trim(),
    apps,
  };
}

// Tracks whether the Settings form has unsaved edits, so switching tabs or
// closing the page can warn before silently discarding them.
function snapshotSettingsState() {
  SETTINGS_SNAPSHOT = JSON.stringify(collectConfig());
}

function isSettingsDirty() {
  if (SETTINGS_SNAPSHOT === null) return false;
  return JSON.stringify(collectConfig()) !== SETTINGS_SNAPSHOT;
}

async function saveSettings() {
  const resultEl = document.getElementById("save-result");
  resultEl.textContent = "Saving...";
  resultEl.className = "save-result";
  try {
    await apiFetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectConfig()),
    });
    resultEl.textContent = "Saved";
    resultEl.className = "save-result ok";
    snapshotSettingsState();
    toast("Settings saved", "ok");
  } catch (e) {
    resultEl.textContent = e.message;
    resultEl.className = "save-result fail";
    toast(`Save failed: ${e.message}`, "fail");
  }
}

async function testApp(appId) {
  const card = appCard(appId);
  const dot = card.querySelector(".status-dot");
  const resultEl = card.querySelector(".test-result");
  const btn = card.querySelector(".test-btn");
  const payload = readAppCard(appId);

  btn.disabled = true;
  resultEl.textContent = "Testing...";
  resultEl.className = "test-result";
  dot.dataset.state = "idle";
  try {
    const res = await apiFetch(`/api/test/${appId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      resultEl.textContent = res.message;
      resultEl.className = "test-result ok";
      dot.dataset.state = "ok";
    } else {
      resultEl.textContent = res.message;
      resultEl.className = "test-result fail";
      dot.dataset.state = "fail";
    }
  } catch (e) {
    resultEl.textContent = e.message;
    resultEl.className = "test-result fail";
    dot.dataset.state = "fail";
  } finally {
    btn.disabled = false;
  }
}

async function testRclone() {
  const resultEl = document.getElementById("test-rclone-result");
  const remote = document.getElementById("s-rclone_remote").value.trim();
  resultEl.textContent = "Testing...";
  resultEl.className = "test-result";
  try {
    const res = await apiFetch("/api/test-rclone", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rclone_remote: remote }),
    });
    resultEl.textContent = res.message;
    resultEl.className = `test-result ${res.ok ? "ok" : "fail"}`;
  } catch (e) {
    resultEl.textContent = e.message;
    resultEl.className = "test-result fail";
  }
}

function initSettingsEvents() {
  document.getElementById("save-btn").addEventListener("click", saveSettings);
  document.getElementById("test-rclone-btn").addEventListener("click", testRclone);
  document.querySelectorAll(".test-btn").forEach((btn) => {
    btn.addEventListener("click", () => testApp(btn.closest(".app-card").dataset.app));
  });
  document.querySelectorAll(".f-enabled").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      setAppCardExpanded(checkbox.closest(".app-card"), checkbox.checked);
    });
  });
  document.querySelectorAll(".chip[data-cron]").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.getElementById("s-cron_schedule").value = chip.dataset.cron;
    });
  });
}

// -------------------------------------------------------------- run tab ----
function renderRunState(state) {
  const summary = document.getElementById("run-summary");
  const logView = document.getElementById("run-log");
  const runBtn = document.getElementById("run-btn");

  runBtn.disabled = !!state.running;
  runBtn.textContent = state.running ? "Running..." : "Run backup now";

  summary.innerHTML = "";
  const addPart = (label, text, danger) => {
    const span = document.createElement("span");
    if (label) {
      const strong = document.createElement("strong");
      if (danger) strong.style.color = "var(--danger)";
      strong.textContent = label;
      span.appendChild(strong);
      span.appendChild(document.createTextNode(` ${text}`));
    } else {
      span.textContent = text;
    }
    summary.appendChild(span);
  };
  if (state.running) {
    addPart("Running", `since ${fmtTime(state.started_at)}`);
  } else if (state.finished_at) {
    addPart(null, `Last run finished ${fmtTime(state.finished_at)}`);
    addPart("OK:", (state.ok || []).join(", ") || "none");
    if ((state.failed || []).length) {
      addPart("Failed:", state.failed.join("; "), true);
    }
  }

  const lines = state.running ? state.log : state.log_tail;
  logView.textContent = (lines && lines.length) ? lines.join("\n") : "No log output yet.";
  logView.scrollTop = logView.scrollHeight;
}

async function refreshRunStatus() {
  try {
    const state = await apiFetch("/api/backup/status");
    renderRunState(state);
    if (state.running && !RUN_POLL_TIMER) {
      RUN_POLL_TIMER = setInterval(async () => {
        const s = await apiFetch("/api/backup/status");
        renderRunState(s);
        if (!s.running) {
          clearInterval(RUN_POLL_TIMER);
          RUN_POLL_TIMER = null;
        }
      }, 1500);
    }
  } catch (e) {
    document.getElementById("run-log").textContent = `Error loading status: ${e.message}`;
  }
}

async function runBackupNow() {
  try {
    await apiFetch("/api/backup/run", { method: "POST" });
    toast("Backup started", "ok");
    refreshRunStatus();
  } catch (e) {
    toast(`Could not start backup: ${e.message}`, "fail");
  }
}

function initRunEvents() {
  document.getElementById("run-btn").addEventListener("click", runBackupNow);
}

// ---------------------------------------------------------- history tab ----
function appMeta(appId) {
  return APP_META.find((m) => m.id === appId);
}

function appLabel(appId) {
  const m = appMeta(appId);
  return m ? m.label : appId;
}

async function deleteBackup(appId, filename, row) {
  if (!confirm(`Delete ${filename}? This can't be undone.`)) return;
  try {
    await apiFetch(`/api/history/${appId}/${encodeURIComponent(filename)}`, { method: "DELETE" });
    row.remove();
    toast(`Deleted ${filename}`, "ok");
  } catch (e) {
    toast(`Delete failed: ${e.message}`, "fail");
  }
}

async function loadHistory() {
  const root = document.getElementById("history-root");
  root.textContent = "Loading...";
  try {
    const history = await apiFetch("/api/history");
    root.innerHTML = "";
    const appIds = Object.keys(history).filter((appId) => (history[appId] || []).length > 0);
    if (!appIds.length) {
      root.innerHTML = '<p class="empty-note">No backups yet - set a Google Drive remote in Settings and run a backup.</p>';
      return;
    }
    appIds.forEach((appId) => {
      const entries = history[appId];
      const section = document.createElement("div");
      section.className = "history-app";
      const heading = document.createElement("h3");
      heading.textContent = appLabel(appId);
      section.appendChild(heading);

      const table = document.createElement("table");
      table.className = "history-table";
      table.innerHTML = "<thead><tr><th>File</th><th>Size</th><th>Modified</th><th></th></tr></thead>";
      const tbody = document.createElement("tbody");
      entries.forEach((e) => {
        const tr = document.createElement("tr");
        const nameTd = document.createElement("td");
        nameTd.textContent = e.name;
        const sizeTd = document.createElement("td");
        sizeTd.textContent = fmtBytes(e.size);
        const timeTd = document.createElement("td");
        timeTd.textContent = fmtTime(e.mod_time);
        const actionsTd = document.createElement("td");
        actionsTd.className = "actions-cell";
        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "icon-btn";
        delBtn.title = "Delete this backup";
        delBtn.textContent = "×";
        delBtn.addEventListener("click", () => deleteBackup(appId, e.name, tr));
        actionsTd.appendChild(delBtn);
        tr.append(nameTd, sizeTd, timeTd, actionsTd);
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      section.appendChild(table);
      root.appendChild(section);
    });
  } catch (e) {
    root.innerHTML = "";
    const p = document.createElement("p");
    p.className = "empty-note";
    p.textContent = `Error loading history: ${e.message}`;
    root.appendChild(p);
  }
}

// ---------------------------------------------------------- restore tab ----
let RESTORE_STATE = { app: null, file: null, servers: [] };

function initRestoreAppOptions() {
  const select = document.getElementById("r-app");
  if (select.options.length) return;
  APP_META.forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m.id;
    opt.textContent = m.label;
    select.appendChild(opt);
  });
}

async function loadRestoreFiles() {
  const appId = document.getElementById("r-app").value;
  const fileSelect = document.getElementById("r-file");
  fileSelect.innerHTML = "<option>Loading...</option>";
  document.getElementById("r-extra").innerHTML = "";
  try {
    const res = await apiFetch(`/api/restore/${appId}/backups`);
    fileSelect.innerHTML = "";
    if (!res.files.length) {
      fileSelect.innerHTML = "<option>No backups found</option>";
      return;
    }
    res.files.forEach((f) => {
      const opt = document.createElement("option");
      opt.value = f;
      opt.textContent = f;
      fileSelect.appendChild(opt);
    });
    renderRestoreExtra();
  } catch (e) {
    fileSelect.innerHTML = "";
    const opt = document.createElement("option");
    opt.textContent = "Error loading backups";
    fileSelect.appendChild(opt);
    toast(e.message, "fail");
  }
}

function renderRestoreExtra() {
  const appId = document.getElementById("r-app").value;
  const extraRoot = document.getElementById("r-extra");
  extraRoot.innerHTML = "";

  if (appId === "tdarr") {
    const block = document.createElement("div");
    block.className = "extra-field-block";
    block.innerHTML =
      '<strong style="color:var(--danger)">Destructive:</strong> this wipes each Tdarr database collection before repopulating it from the backup.';
    extraRoot.appendChild(block);
  } else if (appId === "bazarr") {
    const block = document.createElement("div");
    block.className = "extra-field-block";
    block.innerHTML =
      '<label class="field"><span class="field-label">Bazarr backup folder (local path, overrides Settings value)</span>' +
      '<input type="text" id="r-bazarr-dir" placeholder="/mnt/bazarr-backup"></label>';
    extraRoot.appendChild(block);
    document.getElementById("r-bazarr-dir").value = document.getElementById("s-bazarr_backup_dir").value;
  } else if (appId === "sabnzbd") {
    const block = document.createElement("div");
    block.className = "extra-field-block";
    block.id = "r-sabnzbd-block";
    block.innerHTML =
      '<button type="button" class="btn btn-ghost btn-sm" id="r-sabnzbd-preview-btn">Load Usenet servers</button>' +
      '<div id="r-sabnzbd-servers"></div>';
    extraRoot.appendChild(block);
    document.getElementById("r-sabnzbd-preview-btn").addEventListener("click", loadSabnzbdPreview);
  }
}

async function loadSabnzbdPreview() {
  const fileSelect = document.getElementById("r-file");
  const container = document.getElementById("r-sabnzbd-servers");
  container.textContent = "Loading...";
  try {
    const res = await apiFetch("/api/restore/sabnzbd/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: fileSelect.value }),
    });
    RESTORE_STATE.servers = res.servers;
    container.innerHTML = "";
    if (!res.servers.length) {
      container.innerHTML = '<p class="empty-note">No Usenet servers in this backup.</p>';
      return;
    }
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = "SABnzbd never returns real passwords over its API - enter each server's password to restore it, or leave blank to set it manually later.";
    container.appendChild(note);
    res.servers.forEach((s) => {
      const row = document.createElement("div");
      row.className = "server-pw-row";
      const label = document.createElement("span");
      label.className = "server-label";
      label.textContent = `${s.name} (${s.host || "?"})`;
      const input = document.createElement("input");
      input.type = "password";
      input.placeholder = "password (optional)";
      input.dataset.serverName = s.name;
      input.className = "r-sabnzbd-pw";
      row.append(label, input);
      container.appendChild(row);
    });
  } catch (e) {
    container.innerHTML = "";
    const p = document.createElement("p");
    p.className = "empty-note";
    p.textContent = e.message;
    container.appendChild(p);
  }
}

async function doRestore() {
  const appId = document.getElementById("r-app").value;
  const file = document.getElementById("r-file").value;
  const confirmed = document.getElementById("r-confirm").checked;
  const resultEl = document.getElementById("restore-result");

  if (!confirmed) {
    resultEl.textContent = "Check the confirmation box first.";
    resultEl.className = "save-result fail";
    return;
  }

  const payload = { file, confirm: true };
  if (appId === "bazarr") {
    const dirInput = document.getElementById("r-bazarr-dir");
    if (dirInput && dirInput.value.trim()) payload.bazarr_backup_dir = dirInput.value.trim();
  }
  if (appId === "sabnzbd") {
    const passwords = {};
    document.querySelectorAll(".r-sabnzbd-pw").forEach((input) => {
      if (input.value) passwords[input.dataset.serverName] = input.value;
    });
    payload.passwords = passwords;
  }

  resultEl.textContent = "Restoring...";
  resultEl.className = "save-result";
  try {
    const res = await apiFetch(`/api/restore/${appId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res.summary) {
      const s = res.summary;
      resultEl.textContent =
        `Restored ${s.servers_restored.length} server(s), ${s.misc_keys_restored.length} setting(s).` +
        (s.servers_missing_password.length ? ` No password set for: ${s.servers_missing_password.join(", ")}.` : "");
    } else {
      resultEl.textContent = res.message || "Restore complete.";
    }
    resultEl.className = "save-result ok";
    toast("Restore complete", "ok");
  } catch (e) {
    resultEl.textContent = e.message;
    resultEl.className = "save-result fail";
    toast(`Restore failed: ${e.message}`, "fail");
  }
}

function initRestoreTab() {
  initRestoreAppOptions();
  loadRestoreFiles();
}

function initRestoreEvents() {
  document.getElementById("r-app").addEventListener("change", loadRestoreFiles);
  document.getElementById("r-file").addEventListener("change", renderRestoreExtra);
  document.getElementById("restore-btn").addEventListener("click", doRestore);
}

// -------------------------------------------------------------- startup ----
async function init() {
  initTabs();
  initTheme();
  initSettingsEvents();
  initRunEvents();
  initRestoreEvents();
  syncTopbarHeight();
  window.addEventListener("resize", syncTopbarHeight);
  window.addEventListener("beforeunload", (e) => {
    if (isSettingsDirty()) {
      e.preventDefault();
      e.returnValue = "";
    }
  });
  try {
    APP_META = await apiFetch("/api/meta");
  } catch (e) {
    toast(`Could not load app metadata: ${e.message}`, "fail");
  }
  try {
    await loadConfig();
  } catch (e) {
    toast(`Could not load config: ${e.message}`, "fail");
  }
  refreshRunStatus();
  loadOverview();
}

document.addEventListener("DOMContentLoaded", init);
