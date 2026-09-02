"use strict";

let APP_META = [];
let DESTINATION_META = [];
let RUN_POLL_TIMER = null;
let RESTORE_POLL_TIMER = null;
let SETTINGS_SNAPSHOT = null;

async function apiFetch(url, opts) {
  const res = await fetch(url, opts);
  if (res.status === 401) {
    // Session expired - bounce to login instead of surfacing a raw 401 toast.
    window.location.href = "/login";
    return new Promise(() => {}); // navigation in flight, never resolve
  }
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
      if (btn.dataset.tab === "history") initHistoryTab();
      if (btn.dataset.tab === "restore") initRestoreTab();
    });
  });
}

// ----------------------------------------------------------- overview ----
function destLabel(destId) {
  const m = DESTINATION_META.find((d) => d.id === destId);
  return m ? m.label : destId;
}

// Short labels for the small icon row - destLabel()'s full names are too long there.
const SHORT_DEST_LABEL = { local: "Local", gdrive: "Google Drive", onedrive: "OneDrive", dropbox: "Dropbox" };

function enabledDestinationIds(cfg) {
  // Order from DESTINATION_META, not Object.keys - jsonify() sorts keys
  // alphabetically, which would put "gdrive" before "local".
  const ids = DESTINATION_META.length ? DESTINATION_META.map((m) => m.id) : Object.keys(cfg.destinations || {});
  return ids.filter((id) => (cfg.destinations || {})[id] && cfg.destinations[id].enabled);
}

function destinationsStat(destIds) {
  const div = document.createElement("div");
  div.className = "overview-stat overview-stat-dest";
  const row = document.createElement("div");
  row.className = "overview-dest-icons";
  if (!destIds.length) {
    row.textContent = "None configured";
  } else {
    destIds.forEach((id) => {
      const m = DESTINATION_META.find((d) => d.id === id);
      const item = document.createElement("div");
      item.className = "overview-dest-icon";
      if (m && m.icon) {
        const img = document.createElement("img");
        img.className = "app-icon";
        img.src = `/static/icons/${m.icon}`;
        img.alt = "";
        item.appendChild(img);
      }
      const span = document.createElement("span");
      span.textContent = SHORT_DEST_LABEL[id] || destLabel(id);
      item.appendChild(span);
      row.appendChild(item);
    });
  }
  const label = document.createElement("div");
  label.className = "stat-label";
  label.textContent = "Destinations";
  div.append(row, label);
  return div;
}

async function loadOverview() {
  const summaryEl = document.getElementById("overview-summary");
  const appsEl = document.getElementById("overview-apps");
  summaryEl.textContent = "Loading...";
  appsEl.innerHTML = "";
  try {
    const cfg = await apiFetch("/api/config");
    const destIds = enabledDestinationIds(cfg);

    const [histories, status] = await Promise.all([
      Promise.all(destIds.map((id) => apiFetch(`/api/history/${id}`).catch(() => ({})))),
      apiFetch("/api/backup/status"),
    ]);
    // Per app, the single most recent backup across every enabled destination.
    const latestByApp = {};
    histories.forEach((history, i) => {
      const destId = destIds[i];
      Object.keys(history).forEach((appId) => {
        const entry = (history[appId] || [])[0];
        if (entry && (!latestByApp[appId] || entry.mod_time > latestByApp[appId].mod_time)) {
          latestByApp[appId] = { ...entry, destId };
        }
      });
    });

    const allIds = Object.keys(cfg.apps || {});
    const enabledIds = allIds.filter((id) => cfg.apps[id].enabled);

    // "<app>: <error message>" strings, see run_backup() in backup.py.
    const failureByApp = {};
    (status.failed || []).forEach((entry) => {
      const idx = entry.indexOf(":");
      if (idx > -1) failureByApp[entry.slice(0, idx)] = entry.slice(idx + 1).trim();
    });

    summaryEl.innerHTML = "";
    const textStats = [
      { label: "Enabled services", value: `${enabledIds.length} / ${allIds.length}` },
      { label: "Cron schedule", value: cfg.cron_schedule || "-" },
      { label: "Retention", value: `${cfg.retention_days || 7} days` },
    ];
    const [enabledStat, cronStat, retentionStat] = textStats.map((s) => {
      const div = document.createElement("div");
      div.className = "overview-stat";
      const value = document.createElement("div");
      value.className = "stat-value";
      value.textContent = s.value;
      const label = document.createElement("div");
      label.className = "stat-label";
      label.textContent = s.label;
      div.append(value, label);
      return div;
    });
    summaryEl.append(enabledStat, destinationsStat(destIds), cronStat, retentionStat);

    if (!enabledIds.length) {
      appsEl.innerHTML = '<p class="overview-empty">No services enabled yet - head to Settings to add one.</p>';
      return;
    }

    enabledIds.forEach((appId) => {
      const meta = appMeta(appId);
      const latest = latestByApp[appId];
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
        const failLine = document.createElement("span");
        failLine.textContent = `Last run failed: ${failure}`;
        last.appendChild(failLine);
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

      if (latest) {
        const dlLink = document.createElement("a");
        dlLink.className = "overview-download-link";
        dlLink.textContent = "Download latest backup";
        dlLink.href = `/api/history/${latest.destId}/${appId}/${encodeURIComponent(latest.name)}/download`;
        card.appendChild(dlLink);
      }

      appsEl.appendChild(card);
    });
  } catch (e) {
    summaryEl.innerHTML = "";
    appsEl.innerHTML = `<p class="overview-empty">Error loading overview: ${e.message}</p>`;
  }
}

// ------------------------------------------------------------- layout ----
// Keeps the sticky Settings toolbar pinned below the sticky topbar at any
// topbar height (varies with font size/zoom/wrapping).
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
      localStorage.setItem("backuparr-theme", next);
    } catch (e) {}
    applyThemeButton();
  });
}

// -------------------------------------------------------------- logout ----
function initLogout() {
  const btn = document.getElementById("logout-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    if (!confirm("Log out?")) return;
    try {
      await apiFetch("/api/logout", { method: "POST" });
    } catch (e) {
      // Best-effort - redirecting to /login is correct either way.
    }
    window.location.href = "/login";
  });
}

// ------------------------------------------------------------- settings ----
function appCard(appId) {
  return document.querySelector(`.app-card[data-app="${appId}"]`);
}

function setAppCardExpanded(card, expanded) {
  const body = card.querySelector(".app-card-body");
  if (body) body.classList.toggle("hidden", !expanded);
}

function fillAppCard(appId, cfg) {
  const card = appCard(appId);
  if (!card) return;
  card.querySelector(".f-enabled").checked = !!cfg.enabled;
  const urlInput = card.querySelector(".f-url");
  if (!urlInput) return; // coming_soon app - no body/fields rendered
  urlInput.value = cfg.url || "";
  card.querySelector(".f-api_key").value = cfg.api_key || "";
  card.querySelectorAll(".f-extra").forEach((input) => {
    input.value = cfg[input.dataset.field] || "";
  });
  setAppCardExpanded(card, !!cfg.enabled);
}

function readAppCard(appId) {
  const card = appCard(appId);
  const urlInput = card.querySelector(".f-url");
  if (!urlInput) return { enabled: false }; // coming_soon app
  const out = {
    enabled: card.querySelector(".f-enabled").checked,
    url: urlInput.value.trim(),
    api_key: card.querySelector(".f-api_key").value.trim(),
  };
  card.querySelectorAll(".f-extra").forEach((input) => {
    out[input.dataset.field] = input.value;
  });
  return out;
}

// ---------------------------------------------------------- destinations ----
function destCard(destId) {
  return document.querySelector(`.dest-card[data-dest="${destId}"]`);
}

function fillDestCard(destId, cfg) {
  const card = destCard(destId);
  if (!card) return;
  card.querySelector(".d-enabled").checked = !!cfg.enabled;
  card.querySelectorAll(".d-field").forEach((input) => {
    input.value = cfg[input.dataset.field] || "";
  });
  const body = card.querySelector(".app-card-body");
  if (body) body.classList.toggle("hidden", !cfg.enabled);
  if (destId === "gdrive") updateGdriveUI(cfg);
  if (destId === "onedrive") updateOnedriveUI(cfg);
}

function readDestCard(destId) {
  const card = destCard(destId);
  const out = { enabled: card.querySelector(".d-enabled").checked };
  card.querySelectorAll(".d-field").forEach((input) => {
    out[input.dataset.field] = input.value.trim();
  });
  return out;
}

function updateGdriveUI(gdriveCfg) {
  const statusEl = document.getElementById("gdrive-status");
  const connectBtn = document.getElementById("gdrive-connect-btn");
  const folderBtn = document.getElementById("gdrive-folder-btn");
  const disconnectBtn = document.getElementById("gdrive-disconnect-btn");
  if (!statusEl) return;
  const connected = !!gdriveCfg.refresh_token;
  connectBtn.classList.toggle("hidden", connected);
  folderBtn.classList.toggle("hidden", !connected);
  disconnectBtn.classList.toggle("hidden", !connected);
  if (connected) {
    statusEl.textContent = `Connected - backing up to "${gdriveCfg.folder_name || "My Drive (root)"}".`;
  } else {
    statusEl.textContent = "Not connected yet.";
  }
}

async function loadConfig() {
  const cfg = await apiFetch("/api/config");
  document.getElementById("s-retention_days").value = cfg.retention_days || 7;
  document.getElementById("s-cron_schedule").value = cfg.cron_schedule || "";
  initSchedulePicker(cfg.cron_schedule);
  document.getElementById("s-notify_url").value = cfg.notify_url || "";
  document.getElementById("s-bazarr_backup_dir").value = cfg.bazarr_backup_dir || "";
  Object.keys(cfg.apps || {}).forEach((appId) => fillAppCard(appId, cfg.apps[appId]));
  Object.keys(cfg.destinations || {}).forEach((destId) => fillDestCard(destId, cfg.destinations[destId]));
  snapshotSettingsState();
  return cfg;
}

function collectConfig() {
  const apps = {};
  document.querySelectorAll(".app-card[data-app]").forEach((card) => {
    apps[card.dataset.app] = readAppCard(card.dataset.app);
  });
  const destinations = {};
  document.querySelectorAll(".dest-card[data-dest]").forEach((card) => {
    destinations[card.dataset.dest] = readDestCard(card.dataset.dest);
  });
  return {
    retention_days: Number(document.getElementById("s-retention_days").value || 7),
    cron_schedule: document.getElementById("s-cron_schedule").value.trim(),
    notify_url: document.getElementById("s-notify_url").value.trim(),
    bazarr_backup_dir: document.getElementById("s-bazarr_backup_dir").value.trim(),
    apps,
    destinations,
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

// Shared by testApp/testDestination/testNotifyUrl: disable the button, show
// a pending message, run `request()`, render ok/fail into resultEl (+ a
// status dot's state where the card has one), then re-enable the button.
async function runTest({ btn, resultEl, dot, pendingText, request }) {
  btn.disabled = true;
  resultEl.textContent = pendingText;
  resultEl.className = "test-result";
  if (dot) dot.dataset.state = "idle";
  try {
    const res = await request();
    resultEl.textContent = res.message;
    resultEl.className = `test-result ${res.ok ? "ok" : "fail"}`;
    if (dot) dot.dataset.state = res.ok ? "ok" : "fail";
  } catch (e) {
    resultEl.textContent = e.message;
    resultEl.className = "test-result fail";
    if (dot) dot.dataset.state = "fail";
  } finally {
    btn.disabled = false;
  }
}

async function testApp(appId) {
  const card = appCard(appId);
  const payload = readAppCard(appId);
  await runTest({
    btn: card.querySelector(".test-btn"),
    resultEl: card.querySelector(".test-result"),
    dot: card.querySelector(".status-dot"),
    pendingText: "Testing...",
    request: () =>
      apiFetch(`/api/test/${appId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
  });
}

async function testDestination(destId) {
  const card = destCard(destId);
  const payload = readDestCard(destId);
  await runTest({
    btn: card.querySelector(".test-dest-btn"),
    resultEl: card.querySelector(".test-result"),
    dot: card.querySelector(".status-dot"),
    pendingText: "Testing...",
    request: () =>
      apiFetch(`/api/test-destination/${destId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
  });
}

async function testNotifyUrl() {
  const btn = document.getElementById("notify-test-btn");
  const resultEl = document.getElementById("notify-test-result");
  const notifyUrl = document.getElementById("s-notify_url").value.trim();
  if (!notifyUrl) {
    resultEl.textContent = "Enter a Notify URL first";
    resultEl.className = "test-result fail";
    return;
  }
  await runTest({
    btn,
    resultEl,
    pendingText: "Sending...",
    request: () =>
      apiFetch("/api/test-notify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ notify_url: notifyUrl }),
      }),
  });
}

// ------------------------------------------------------------ gdrive ----
function gdriveRedirectUri() {
  return `${window.location.origin}/api/destinations/gdrive/oauth/callback`;
}

async function connectGdrive() {
  // Client ID/secret must be saved before the OAuth redirect can use them.
  const resultEl = document.getElementById("save-result");
  resultEl.textContent = "Saving before connecting...";
  resultEl.className = "save-result";
  try {
    await apiFetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectConfig()),
    });
    snapshotSettingsState();
    window.location.href = "/api/destinations/gdrive/oauth/start";
  } catch (e) {
    resultEl.textContent = e.message;
    resultEl.className = "save-result fail";
    toast(`Could not save before connecting: ${e.message}`, "fail");
  }
}

function loadGooglePickerApi() {
  return new Promise((resolve, reject) => {
    if (window.google && window.google.picker) return resolve();
    if (!window.gapi) return reject(new Error("Google API script did not load"));
    window.gapi.load("picker", { callback: resolve, onerror: () => reject(new Error("could not load Google Picker")) });
  });
}

// OAuth client IDs are "<cloud project number>-xxxx.apps.googleusercontent.com" -
// the numeric prefix is the same project number Picker's setAppId wants, so
// it doesn't need its own field.
function gdriveAppIdFromClientId(clientId) {
  const match = /^(\d+)-/.exec(clientId || "");
  return match ? match[1] : null;
}

async function openGooglePicker() {
  const btn = document.getElementById("gdrive-folder-btn");
  btn.disabled = true;
  try {
    const gdriveCfg = readDestCard("gdrive");
    if (!gdriveCfg.developer_key) {
      throw new Error('Add an API key in the Google Drive settings first (see "Setup guide")');
    }
    const [{ access_token: accessToken }] = await Promise.all([
      apiFetch("/api/destinations/gdrive/access-token", { method: "POST" }),
      loadGooglePickerApi(),
    ]);
    const view = new google.picker.DocsView(google.picker.ViewId.FOLDERS)
      .setSelectFolderEnabled(true)
      .setIncludeFolders(true);
    const builder = new google.picker.PickerBuilder()
      .addView(view)
      .setTitle("Choose a folder for Backuparr's backups")
      .setOAuthToken(accessToken)
      .setDeveloperKey(gdriveCfg.developer_key)
      .setCallback(gdrivePickerCallback);
    const appId = gdriveAppIdFromClientId(gdriveCfg.client_id);
    if (appId) builder.setAppId(appId);
    builder.build().setVisible(true);
  } catch (e) {
    toast(`Could not open the folder picker: ${e.message}`, "fail");
  } finally {
    btn.disabled = false;
  }
}

async function gdrivePickerCallback(data) {
  if (data.action !== google.picker.Action.PICKED) return;
  const folder = data.docs[0];
  try {
    await apiFetch("/api/destinations/gdrive/folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_id: folder.id, folder_name: folder.name }),
    });
    toast(`Backing up to "${folder.name}"`, "ok");
    await loadConfig();
  } catch (e) {
    toast(`Could not save the selected folder: ${e.message}`, "fail");
  }
}

async function disconnectGdrive() {
  if (!confirm("Disconnect Google Drive? Existing backups already there are left untouched.")) return;
  try {
    await apiFetch("/api/destinations/gdrive/disconnect", { method: "POST" });
    toast("Google Drive disconnected", "ok");
    await loadConfig();
  } catch (e) {
    toast(`Could not disconnect: ${e.message}`, "fail");
  }
}

// ----------------------------------------------------------- onedrive ----
function updateOnedriveUI(onedriveCfg) {
  const statusEl = document.getElementById("onedrive-status");
  const connectBtn = document.getElementById("onedrive-connect-btn");
  const disconnectBtn = document.getElementById("onedrive-disconnect-btn");
  const pasteField = document.getElementById("onedrive-paste-field");
  if (!statusEl) return;
  const connected = !!onedriveCfg.token;
  connectBtn.classList.toggle("hidden", connected);
  disconnectBtn.classList.toggle("hidden", !connected);
  if (pasteField) pasteField.classList.toggle("hidden", connected);
  statusEl.textContent = connected
    ? "Connected - backing up to your OneDrive app folder (Apps/Backuparr)."
    : "Not connected yet.";
}

async function connectOnedrive() {
  const input = document.getElementById("onedrive-token-input");
  const btn = document.getElementById("onedrive-connect-btn");
  const tokenBlob = input.value.trim();
  if (!tokenBlob) {
    toast("Paste the token `rclone authorize onedrive` printed first", "fail");
    return;
  }
  btn.disabled = true;
  try {
    await apiFetch("/api/destinations/onedrive/connect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token_blob: tokenBlob }),
    });
    input.value = "";
    toast("OneDrive connected", "ok");
    await loadConfig();
  } catch (e) {
    toast(`Could not connect: ${e.message}`, "fail");
  } finally {
    btn.disabled = false;
  }
}

async function disconnectOnedrive() {
  if (!confirm("Disconnect OneDrive? Existing backups already there are left untouched.")) return;
  try {
    await apiFetch("/api/destinations/onedrive/disconnect", { method: "POST" });
    toast("OneDrive disconnected", "ok");
    await loadConfig();
  } catch (e) {
    toast(`Could not disconnect: ${e.message}`, "fail");
  }
}

// ------------------------------------------------------------ setup guide ----
function openSetupGuide(destId) {
  const meta = DESTINATION_META.find((m) => m.id === destId);
  if (!meta || !meta.setup_help) return;
  const help = meta.setup_help;

  document.getElementById("setup-guide-title").textContent = help.title;
  document.getElementById("setup-guide-intro").textContent = help.intro || "";

  const redirectBlock = document.getElementById("setup-guide-redirect");
  redirectBlock.classList.add("hidden");

  const stepsEl = document.getElementById("setup-guide-steps");
  stepsEl.innerHTML = "";
  (help.steps || []).forEach((step) => {
    const li = document.createElement("li");
    const text = typeof step === "string" ? step : step.text;
    li.appendChild(document.createTextNode(text));
    const code = typeof step === "object" ? step.code : null;
    if (code) {
      const pre = document.createElement("pre");
      pre.className = "guide-step-code";
      const codeEl = document.createElement("code");
      codeEl.textContent = code;
      pre.appendChild(codeEl);
      li.appendChild(pre);
    }
    const checklist = typeof step === "object" ? step.checklist : null;
    if (checklist && checklist.length) {
      const ul = document.createElement("ul");
      ul.className = "guide-step-checklist";
      checklist.forEach((item) => {
        const itemLi = document.createElement("li");
        itemLi.textContent = item;
        ul.appendChild(itemLi);
      });
      li.appendChild(ul);
    }
    if (typeof step === "object" && step.redirect_uri) {
      document.getElementById("setup-guide-redirect-input").value = gdriveRedirectUri();
      li.appendChild(redirectBlock);
      redirectBlock.classList.remove("hidden");
    }
    const link = typeof step === "object" ? step.link : null;
    if (link) {
      const a = document.createElement("a");
      a.className = "guide-step-link";
      a.href = link.url;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      a.textContent = `${link.label} ↗`;
      li.appendChild(a);
    }
    stepsEl.appendChild(li);
  });

  const linksEl = document.getElementById("setup-guide-links");
  linksEl.innerHTML = "";
  (help.links || []).forEach((link) => {
    const a = document.createElement("a");
    a.href = link.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = `${link.label} ↗`;
    linksEl.appendChild(a);
  });

  document.getElementById("setup-guide-modal").classList.remove("hidden");
}

function closeSetupGuide() {
  document.getElementById("setup-guide-modal").classList.add("hidden");
}

function initSetupGuideEvents() {
  document.getElementById("setup-guide-close").addEventListener("click", closeSetupGuide);
  document.getElementById("setup-guide-modal").addEventListener("click", (e) => {
    if (e.target.id === "setup-guide-modal") closeSetupGuide();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSetupGuide();
  });
  document.getElementById("setup-guide-copy-btn").addEventListener("click", async () => {
    const input = document.getElementById("setup-guide-redirect-input");
    try {
      await navigator.clipboard.writeText(input.value);
      toast("Copied", "ok");
    } catch (e) {
      input.select();
      toast("Couldn't copy automatically - text is selected, copy manually", "fail");
    }
  });
}

// -------------------------------------------------------- schedule picker ----
// Writes into #s-cron_schedule, same as if typed directly.
function schedulePickerVisibility() {
  const freq = document.getElementById("sched-frequency").value;
  document.getElementById("sched-time").classList.toggle("hidden", freq === "hourly");
  document.getElementById("sched-weekday").classList.toggle("hidden", freq !== "weekly");
  document.getElementById("sched-interval-hours").classList.toggle("hidden", freq !== "hourly");
}

function applyPickerToCron() {
  const freq = document.getElementById("sched-frequency").value;
  let cron;
  if (freq === "hourly") {
    cron = `0 */${document.getElementById("sched-interval-hours").value} * * *`;
  } else {
    const [hh, mm] = (document.getElementById("sched-time").value || "03:00").split(":").map((n) => parseInt(n, 10));
    cron = freq === "weekly" ? `${mm} ${hh} * * ${document.getElementById("sched-weekday").value}` : `${mm} ${hh} * * *`;
  }
  document.getElementById("s-cron_schedule").value = cron;
}

// Only recognizes shapes the picker itself produces (daily/weekly at HH:MM,
// or every-N-hours); anything else is left untouched.
function parseCronIntoPicker(cron) {
  const parts = (cron || "").trim().split(/\s+/);
  if (parts.length !== 5) return false;
  const [min, hour, dom, mon, dow] = parts;
  if (dom !== "*" || mon !== "*") return false;

  const hourlyMatch = /^\*\/(\d{1,2})$/.exec(hour);
  if (min === "0" && dow === "*" && hourlyMatch) {
    const select = document.getElementById("sched-interval-hours");
    if (![...select.options].some((o) => o.value === hourlyMatch[1])) return false;
    document.getElementById("sched-frequency").value = "hourly";
    select.value = hourlyMatch[1];
    return true;
  }

  if (!/^\d{1,2}$/.test(hour) || !/^\d{1,2}$/.test(min)) return false;
  const time = `${hour.padStart(2, "0")}:${min.padStart(2, "0")}`;

  if (dow === "*") {
    document.getElementById("sched-frequency").value = "daily";
    document.getElementById("sched-time").value = time;
    return true;
  }
  if (/^[0-6]$/.test(dow)) {
    document.getElementById("sched-frequency").value = "weekly";
    document.getElementById("sched-time").value = time;
    document.getElementById("sched-weekday").value = dow;
    return true;
  }
  return false;
}

function setCronAdvancedExpanded(expanded) {
  document.getElementById("cron-advanced-body").classList.toggle("hidden", !expanded);
  document.getElementById("cron-advanced-toggle").setAttribute("aria-expanded", String(expanded));
}

function initSchedulePicker(cronValue) {
  const matched = parseCronIntoPicker(cronValue);
  if (!matched) {
    document.getElementById("sched-frequency").value = "daily";
    if (!document.getElementById("sched-time").value) document.getElementById("sched-time").value = "03:00";
  }
  schedulePickerVisibility();
  // Auto-expand advanced when the schedule doesn't match a simple picker shape.
  setCronAdvancedExpanded(!matched);
}

function initScheduleEvents() {
  ["sched-frequency", "sched-time", "sched-weekday", "sched-interval-hours"].forEach((id) => {
    document.getElementById(id).addEventListener("change", () => {
      schedulePickerVisibility();
      applyPickerToCron();
    });
  });
  document.getElementById("cron-advanced-toggle").addEventListener("click", () => {
    const expanded = document.getElementById("cron-advanced-toggle").getAttribute("aria-expanded") === "true";
    setCronAdvancedExpanded(!expanded);
  });
}

function initSettingsEvents() {
  document.getElementById("save-btn").addEventListener("click", saveSettings);
  document.querySelectorAll(".test-btn").forEach((btn) => {
    btn.addEventListener("click", () => testApp(btn.closest(".app-card").dataset.app));
  });
  document.querySelectorAll(".f-enabled").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      setAppCardExpanded(checkbox.closest(".app-card"), checkbox.checked);
    });
  });
  document.getElementById("notify-test-btn").addEventListener("click", testNotifyUrl);
  initScheduleEvents();

  document.querySelectorAll(".test-dest-btn").forEach((btn) => {
    btn.addEventListener("click", () => testDestination(btn.closest(".dest-card").dataset.dest));
  });
  document.querySelectorAll(".d-enabled").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      const card = checkbox.closest(".dest-card");
      const body = card.querySelector(".app-card-body");
      if (body) body.classList.toggle("hidden", !checkbox.checked);
    });
  });
  document.querySelectorAll(".setup-guide-btn").forEach((btn) => {
    btn.addEventListener("click", () => openSetupGuide(btn.dataset.dest));
  });
  initSetupGuideEvents();

  const gdriveConnectBtn = document.getElementById("gdrive-connect-btn");
  if (gdriveConnectBtn) gdriveConnectBtn.addEventListener("click", connectGdrive);
  const gdriveFolderBtn = document.getElementById("gdrive-folder-btn");
  if (gdriveFolderBtn) gdriveFolderBtn.addEventListener("click", openGooglePicker);
  const gdriveDisconnectBtn = document.getElementById("gdrive-disconnect-btn");
  if (gdriveDisconnectBtn) gdriveDisconnectBtn.addEventListener("click", disconnectGdrive);

  const onedriveConnectBtn = document.getElementById("onedrive-connect-btn");
  if (onedriveConnectBtn) onedriveConnectBtn.addEventListener("click", connectOnedrive);
  const onedriveDisconnectBtn = document.getElementById("onedrive-disconnect-btn");
  if (onedriveDisconnectBtn) onedriveDisconnectBtn.addEventListener("click", disconnectOnedrive);
}

// -------------------------------------------------------------- run tab ----
function renderRunState(state) {
  const summary = document.getElementById("run-summary");
  const logView = document.getElementById("run-log");
  const runBtn = document.getElementById("run-btn");
  const cancelBtn = document.getElementById("cancel-run-btn");
  const progress = document.getElementById("run-progress");
  const progressBar = document.getElementById("run-progress-bar");

  runBtn.disabled = !!state.running;
  runBtn.textContent = state.running ? "Running..." : "Run backup now";

  cancelBtn.classList.toggle("hidden", !state.running);
  cancelBtn.disabled = !!state.cancel_requested;
  cancelBtn.textContent = state.cancel_requested ? "Cancelling..." : "Cancel";

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
    if (state.total_apps) {
      addPart(null, `- backing up ${state.current_app} (${state.current_index} of ${state.total_apps})`);
      const spinner = document.createElement("span");
      spinner.className = "spinner";
      summary.appendChild(spinner);
    }
  } else if (state.finished_at) {
    addPart(null, `Last run finished ${fmtTime(state.finished_at)}`);
    addPart("OK:", (state.ok || []).join(", ") || "none");
    if ((state.failed || []).length) {
      addPart("Failed:", state.failed.join("; "), true);
    }
  }

  if (state.running && state.total_apps) {
    progress.classList.remove("hidden");
    progressBar.style.width = `${((state.current_index - 1) / state.total_apps) * 100}%`;
  } else {
    progress.classList.add("hidden");
  }

  const lines = state.running ? state.log : state.log_tail;
  logView.textContent = (lines && lines.length) ? lines.join("\n") : "No log output yet.";
  logView.scrollTop = logView.scrollHeight;
}

// Shared by refreshRunStatus/refreshRestoreStatus: fetch+render status once,
// then - if it's running and no poll is already active - keep polling on an
// interval, rendering each tick, until the status reports not-running.
async function pollWhileRunning({ getTimer, setTimer, fetchStatus, render, intervalMs, onStopped, onError }) {
  try {
    const state = await fetchStatus();
    render(state);
    if (state.running && !getTimer()) {
      setTimer(
        setInterval(async () => {
          const s = await fetchStatus();
          render(s);
          if (!s.running) {
            clearInterval(getTimer());
            setTimer(null);
            if (onStopped) onStopped(s);
          }
        }, intervalMs)
      );
    }
  } catch (e) {
    if (onError) onError(e);
  }
}

async function refreshRunStatus() {
  await pollWhileRunning({
    getTimer: () => RUN_POLL_TIMER,
    setTimer: (t) => {
      RUN_POLL_TIMER = t;
    },
    fetchStatus: () => apiFetch("/api/backup/status"),
    render: renderRunState,
    intervalMs: 1500,
    onError: (e) => {
      document.getElementById("run-log").textContent = `Error loading status: ${e.message}`;
    },
  });
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

async function cancelBackupRun() {
  const cancelBtn = document.getElementById("cancel-run-btn");
  cancelBtn.disabled = true;
  cancelBtn.textContent = "Cancelling...";
  try {
    await apiFetch("/api/backup/cancel", { method: "POST" });
    toast("Cancelling after the current step finishes...", "ok");
    refreshRunStatus();
  } catch (e) {
    toast(`Could not cancel: ${e.message}`, "fail");
  }
}

function initRunEvents() {
  document.getElementById("run-btn").addEventListener("click", runBackupNow);
  document.getElementById("cancel-run-btn").addEventListener("click", cancelBackupRun);
}

// ---------------------------------------------------------- history tab ----
function appMeta(appId) {
  return APP_META.find((m) => m.id === appId);
}

function appLabel(appId) {
  const m = appMeta(appId);
  return m ? m.label : appId;
}

async function deleteBackup(destId, appId, filename, row) {
  if (!confirm(`Delete ${filename}? This can't be undone.`)) return;
  try {
    await apiFetch(`/api/history/${destId}/${appId}/${encodeURIComponent(filename)}`, { method: "DELETE" });
    row.remove();
    toast(`Deleted ${filename}`, "ok");
  } catch (e) {
    toast(`Delete failed: ${e.message}`, "fail");
  }
}

async function populateDestinationSelect(select) {
  const previous = select.value;
  try {
    const cfg = await apiFetch("/api/config");
    const destIds = enabledDestinationIds(cfg);
    select.innerHTML = "";
    if (!destIds.length) {
      select.innerHTML = "<option>No destinations enabled</option>";
      return [];
    }
    destIds.forEach((id) => {
      const opt = document.createElement("option");
      opt.value = id;
      opt.textContent = destLabel(id);
      select.appendChild(opt);
    });
    if (destIds.includes(previous)) select.value = previous;
    return destIds;
  } catch (e) {
    select.innerHTML = "<option>Error loading destinations</option>";
    return [];
  }
}

async function initHistoryTab() {
  const select = document.getElementById("h-destination");
  await populateDestinationSelect(select);
  loadHistory();
}

function initHistoryEvents() {
  document.getElementById("history-refresh-btn").addEventListener("click", loadHistory);
  document.getElementById("h-destination").addEventListener("change", loadHistory);
}

async function loadHistory() {
  const root = document.getElementById("history-root");
  const destId = document.getElementById("h-destination").value;
  root.textContent = "Loading...";
  if (!destId) {
    root.innerHTML = '<p class="empty-note">No destinations enabled - turn one on in Settings first.</p>';
    return;
  }
  try {
    const history = await apiFetch(`/api/history/${destId}`);
    root.innerHTML = "";
    const appIds = Object.keys(history).filter((appId) => (history[appId] || []).length > 0);
    if (!appIds.length) {
      root.innerHTML = '<p class="empty-note">No backups yet on this destination - run a backup first.</p>';
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
        const dlLink = document.createElement("a");
        dlLink.className = "icon-btn";
        dlLink.title = "Download this backup";
        dlLink.textContent = "↓";
        dlLink.href = `/api/history/${destId}/${appId}/${encodeURIComponent(e.name)}/download`;
        const delBtn = document.createElement("button");
        delBtn.type = "button";
        delBtn.className = "icon-btn";
        delBtn.title = "Delete this backup";
        delBtn.textContent = "×";
        delBtn.addEventListener("click", () => deleteBackup(destId, appId, e.name, tr));
        actionsTd.append(dlLink, delBtn);
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
  // restore_supported defaults to true; only explicit opt-outs (e.g. Profilarr) are excluded.
  APP_META.filter((m) => m.restore_supported !== false).forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m.id;
    opt.textContent = m.label;
    select.appendChild(opt);
  });
}

function renderRestoreOverride() {
  const appId = document.getElementById("r-app").value;
  const meta = APP_META.find((m) => m.id === appId);
  const root = document.getElementById("r-override");
  root.innerHTML = "";
  if (!meta) return;

  const block = document.createElement("div");
  block.className = "extra-field-block";

  const urlLabel = document.createElement("label");
  urlLabel.className = "field";
  urlLabel.innerHTML = `<span class="field-label">URL</span><input type="text" id="r-ovr-url" placeholder="${meta.url_placeholder || ""}" autocomplete="off">`;
  block.appendChild(urlLabel);

  const keyLabel = document.createElement("label");
  keyLabel.className = "field";
  const keyHint = !meta.key_required ? `<span class="muted">(optional${meta.key_help ? ` &mdash; ${meta.key_help}` : ""})</span>` : "";
  keyLabel.innerHTML = `<span class="field-label">API key ${keyHint}</span><input type="password" id="r-ovr-api_key" autocomplete="off">`;
  block.appendChild(keyLabel);

  (meta.extra_fields || []).forEach((f) => {
    const label = document.createElement("label");
    label.className = "field";
    const help = f.help ? `<span class="muted">(${f.help})</span>` : "";
    label.innerHTML = `<span class="field-label">${f.label} ${help}</span><input type="${f.type}" class="r-ovr-extra" data-field="${f.name}" autocomplete="off">`;
    block.appendChild(label);
  });

  root.appendChild(block);
}

function updateRestoreOverrideVisibility() {
  const checked = document.getElementById("r-override-toggle").checked;
  document.getElementById("r-override").classList.toggle("hidden", !checked);
  if (checked) renderRestoreOverride();
}

function readRestoreOverride() {
  if (!document.getElementById("r-override-toggle").checked) return null;
  const override = {
    url: document.getElementById("r-ovr-url").value.trim(),
    api_key: document.getElementById("r-ovr-api_key").value.trim(),
  };
  document.querySelectorAll("#r-override .r-ovr-extra").forEach((input) => {
    override[input.dataset.field] = input.value.trim();
  });
  return override;
}

async function loadRestoreFiles() {
  const destId = document.getElementById("r-destination").value;
  const appId = document.getElementById("r-app").value;
  const fileSelect = document.getElementById("r-file");
  fileSelect.innerHTML = "<option>Loading...</option>";
  document.getElementById("r-extra").innerHTML = "";
  if (!destId) {
    fileSelect.innerHTML = "<option>No destinations enabled</option>";
    return;
  }
  try {
    const res = await apiFetch(`/api/restore/${destId}/${appId}/backups`);
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
    block.innerHTML = '<div id="r-sabnzbd-servers">Loading Usenet servers...</div>';
    extraRoot.appendChild(block);
    loadSabnzbdPreview();
  }
}

async function loadSabnzbdPreview() {
  const destId = document.getElementById("r-destination").value;
  const fileSelect = document.getElementById("r-file");
  const container = document.getElementById("r-sabnzbd-servers");
  container.textContent = "Loading...";
  try {
    const res = await apiFetch(`/api/restore/${destId}/sabnzbd/preview`, {
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
  const destId = document.getElementById("r-destination").value;
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
  const override = readRestoreOverride();
  if (override) {
    if (!override.url) {
      resultEl.textContent = "Enter a target URL for the override.";
      resultEl.className = "save-result fail";
      return;
    }
    payload.override = override;
  }
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

  resultEl.textContent = "";
  resultEl.className = "save-result";
  document.getElementById("restore-log").textContent = "";
  document.getElementById("restore-log").classList.add("hidden");
  try {
    await apiFetch(`/api/restore/${destId}/${appId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    refreshRestoreStatus();
  } catch (e) {
    resultEl.textContent = e.message;
    resultEl.className = "save-result fail";
    toast(`Restore failed: ${e.message}`, "fail");
  }
}

function renderRestoreState(state) {
  const resultEl = document.getElementById("restore-result");
  const summaryEl = document.getElementById("restore-summary");
  const logEl = document.getElementById("restore-log");
  const btn = document.getElementById("restore-btn");

  btn.disabled = !!state.running;
  btn.textContent = state.running ? "Restoring..." : "Restore";

  summaryEl.classList.toggle("hidden", !state.running);
  summaryEl.innerHTML = "";
  if (state.running) {
    summaryEl.appendChild(document.createTextNode(`Restoring ${state.app || ""}...`));
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    summaryEl.appendChild(spinner);
  }

  logEl.classList.toggle("hidden", !(state.log && state.log.length));
  if (state.log && state.log.length) {
    logEl.textContent = state.log.join("\n");
    logEl.scrollTop = logEl.scrollHeight;
  }

  if (!state.running && state.finished_at) {
    if (state.error) {
      resultEl.textContent = state.error;
      resultEl.className = "save-result fail";
    } else {
      const s = state.summary;
      if (s && Array.isArray(s.servers_restored)) {
        resultEl.textContent =
          `Restored ${s.servers_restored.length} server(s), ${s.misc_keys_restored.length} setting(s).` +
          (s.servers_missing_password.length ? ` No password set for: ${s.servers_missing_password.join(", ")}.` : "");
      } else if (s && (s.database || s.database_skipped || s.config)) {
        const parts = [];
        if (s.database) parts.push("database restored");
        if (s.database_skipped) parts.push(`database not restored (${s.database_skipped})`);
        if (s.config) parts.push("config restored");
        resultEl.textContent = parts.join(", ") + ".";
      } else {
        resultEl.textContent = state.message || "Restore complete.";
      }
      resultEl.className = "save-result ok";
    }
  }
}

async function refreshRestoreStatus() {
  await pollWhileRunning({
    getTimer: () => RESTORE_POLL_TIMER,
    setTimer: (t) => {
      RESTORE_POLL_TIMER = t;
    },
    fetchStatus: () => apiFetch("/api/restore/status"),
    render: renderRestoreState,
    intervalMs: 1000,
    onStopped: (s) => toast(s.error ? `Restore failed: ${s.error}` : "Restore complete", s.error ? "fail" : "ok"),
    // no onError - a transient poll error just waits for the next tick (or tab visit) to retry
  });
}

async function initRestoreTab() {
  initRestoreAppOptions();
  await populateDestinationSelect(document.getElementById("r-destination"));
  loadRestoreFiles();
  refreshRestoreStatus();
}

function initRestoreEvents() {
  document.getElementById("r-destination").addEventListener("change", loadRestoreFiles);
  document.getElementById("r-app").addEventListener("change", loadRestoreFiles);
  document.getElementById("r-app").addEventListener("change", updateRestoreOverrideVisibility);
  document.getElementById("r-file").addEventListener("change", renderRestoreExtra);
  document.getElementById("r-override-toggle").addEventListener("change", updateRestoreOverrideVisibility);
  document.getElementById("restore-btn").addEventListener("click", doRestore);
}

// -------------------------------------------------------------- startup ----
function handleGdriveRedirect() {
  const params = new URLSearchParams(window.location.search);
  const error = params.get("gdrive_error");
  const connected = params.get("gdrive");
  if (!error && !connected) return;

  if (error) {
    toast(`Google Drive: ${decodeURIComponent(error)}`, "fail");
  } else if (connected === "connected") {
    toast("Google Drive connected - choose a folder in Settings", "ok");
  }
  window.history.replaceState({}, "", window.location.pathname);
  document.querySelector('.tab-btn[data-tab="settings"]').click();
}

// Compares the running version against GitHub's latest published Release.
// Plain fetch (not apiFetch) - this is an external, unauthenticated call,
// not one of Backuparr's own API routes. Fails silently (leaves the
// footer's version-check dot hidden) on any error - offline, GitHub
// unreachable, rate-limited, or the repo not being public yet.
async function checkForUpdate() {
  const versionEl = document.querySelector(".site-footer-version");
  let latest;
  try {
    const res = await fetch("https://api.github.com/repos/rsaturns/backuparr/releases/latest");
    if (!res.ok) return;
    const data = await res.json();
    latest = (data.tag_name || "").replace(/^v/, "");
  } catch (e) {
    return;
  }
  if (!latest) return;

  const dot = document.getElementById("version-check-dot");
  const text = document.getElementById("version-check-text");
  if (latest === versionEl.dataset.version) {
    dot.dataset.state = "ok";
    text.className = "version-check-text ok";
    text.textContent = "Current Version";
  } else {
    dot.dataset.state = "fail";
    text.className = "version-check-text fail";
    text.innerHTML = "";
    const link = document.createElement("a");
    link.href = "https://github.com/rsaturns/backuparr";
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "Update Available";
    text.appendChild(link);
  }
  document.getElementById("version-check").classList.remove("hidden");
}

// ------------------------------------------------------------ what's changed ----
// Renders the small subset of Markdown CHANGELOG.md actually uses (##/###
// headers, "- " bullets whose text wraps onto indented continuation lines,
// inline `code`, **bold**) into HTML - not general-purpose Markdown, no
// library needed for content this project controls itself.
function renderChangelogMarkdown(md) {
  const escapeHtml = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inlineFormat = (s) =>
    escapeHtml(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");

  const html = [];
  let list = null; // accumulated <li> strings for the list currently being built
  let item = null; // text of the list item currently accumulating continuation lines

  const flushItem = () => {
    if (item !== null) {
      list.push(`<li>${inlineFormat(item.trim())}</li>`);
      item = null;
    }
  };
  const flushList = () => {
    flushItem();
    if (list) {
      html.push(`<ul>${list.join("")}</ul>`);
      list = null;
    }
  };

  for (const line of md.split("\n")) {
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushList();
      const level = heading[1].length;
      html.push(`<h${level}>${inlineFormat(heading[2])}</h${level}>`);
      continue;
    }
    const bullet = line.match(/^-\s+(.*)$/);
    if (bullet) {
      flushItem();
      if (!list) list = [];
      item = bullet[1];
      continue;
    }
    if (item !== null && /^\s+\S/.test(line)) {
      item += " " + line.trim();
      continue;
    }
    if (line.trim() === "") {
      flushList();
      continue;
    }
    flushList();
    html.push(`<p>${inlineFormat(line.trim())}</p>`);
  }
  flushList();
  return html.join("");
}

// Lazily fetches the last 3 GitHub Releases and shows them in a modal. Only
// reachable via the "What's Changed" button, which lives inside #version-check
// and is therefore only visible once checkForUpdate() has already confirmed
// GitHub is reachable - no extra eager fetch just to decide whether to show it.
async function openWhatsChanged() {
  const body = document.getElementById("whats-changed-body");
  body.innerHTML = "";
  document.getElementById("whats-changed-modal").classList.remove("hidden");
  try {
    const res = await fetch("https://api.github.com/repos/rsaturns/backuparr/releases?per_page=3");
    if (!res.ok) throw new Error(`GitHub returned ${res.status}`);
    const releases = await res.json();
    if (!releases.length) {
      body.innerHTML = '<p class="hint">No releases found.</p>';
      return;
    }
    const list = document.createElement("div");
    list.className = "release-list";
    releases.forEach((release) => {
      const item = document.createElement("div");
      item.className = "release-item";

      const head = document.createElement("div");
      head.className = "release-head";
      const tag = document.createElement("span");
      tag.className = "release-tag";
      tag.textContent = release.tag_name || release.name || "Untitled release";
      const date = document.createElement("span");
      date.className = "release-date";
      date.textContent = release.published_at ? new Date(release.published_at).toLocaleDateString() : "-";
      head.appendChild(tag);
      head.appendChild(date);
      item.appendChild(head);

      const notes = document.createElement("div");
      notes.className = "release-body";
      notes.innerHTML = release.body ? renderChangelogMarkdown(release.body) : "<p>No release notes provided.</p>";
      item.appendChild(notes);

      list.appendChild(item);
    });
    body.appendChild(list);
  } catch (e) {
    body.innerHTML = "";
    const err = document.createElement("p");
    err.className = "whats-changed-error";
    err.textContent = `Could not load release notes: ${e.message}`;
    body.appendChild(err);
  }
}

function closeWhatsChanged() {
  document.getElementById("whats-changed-modal").classList.add("hidden");
}

function initWhatsChangedEvents() {
  document.getElementById("whats-changed-btn").addEventListener("click", openWhatsChanged);
  document.getElementById("whats-changed-close").addEventListener("click", closeWhatsChanged);
  document.getElementById("whats-changed-modal").addEventListener("click", (e) => {
    if (e.target.id === "whats-changed-modal") closeWhatsChanged();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeWhatsChanged();
  });
}

async function init() {
  initTabs();
  initTheme();
  initLogout();
  initWhatsChangedEvents();
  initSettingsEvents();
  initRunEvents();
  initHistoryEvents();
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
    DESTINATION_META = await apiFetch("/api/destinations");
  } catch (e) {
    toast(`Could not load destination metadata: ${e.message}`, "fail");
  }
  try {
    await loadConfig();
  } catch (e) {
    toast(`Could not load config: ${e.message}`, "fail");
  }
  refreshRunStatus();
  loadOverview();
  handleGdriveRedirect();
  checkForUpdate();
}

document.addEventListener("DOMContentLoaded", init);
