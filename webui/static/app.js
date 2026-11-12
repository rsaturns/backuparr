"use strict";

const AUTH_DISABLED = document.body.dataset.authDisabled === "true";
const { apps: APP_META, categories: APP_CATEGORIES, destinations: DESTINATION_META, self_app: SELF_APP } = JSON.parse(document.body.dataset.uiMeta);
let RUN_POLL_TIMER = null;
let RESTORE_POLL_TIMER = null;
let SETTINGS_SNAPSHOT = null;
let DISCOVERY_RUNNING = false;
let PLEX_LOGIN = null;
const APP_CREDENTIAL_EDIT_REVISION = new Map();
const APP_DISCOVERY_UPDATES = new Map();
// Saved extra instances (radarr-4k -> "4K"), ones removed in Settings but
// not saved yet, and the ids of saved removals, never given out again.
const INSTANCE_NAMES = new Map();
const REMOVED_INSTANCES = new Set();
const RETIRED_INSTANCES = new Set();
// Enabled apps and instances as last saved: the Run tab can back up any one.
const ENABLED_APPS = new Set();
// Whether Backuparr's own backup is on in the saved settings (#165): its card
// counts and shows like an enabled app's, and Run & Status can run it alone.
let SELF_BACKUP_ON = true;

async function apiFetch(url, opts) {
  opts = { ...opts };
  if (opts.method && opts.method !== "GET" && opts.method !== "DELETE") {
    // The server only accepts JSON for state changes, which a form or
    // simple request from another site can't send.
    opts.headers = { "Content-Type": "application/json", ...opts.headers };
    if (opts.body === undefined) opts.body = "{}";
  }
  const res = await fetch(url, opts);
  if (res.status === 401 && !AUTH_DISABLED) {
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
    const error = new Error(message);
    error.status = res.status;
    error.body = body;
    throw error;
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

// Local storage can be a USB disk that spins down to save power: the server
// waits up to 60 s for it to wake before it answers. A Local request still
// unanswered after 2 s says so, as the backup log does.
const STORAGE_WAIT_TEXT = "Waiting for storage to be ready...";

async function whileStorageWakes(destId, request, show) {
  if (destId !== "local") return request;
  const timer = setTimeout(show, 2000);
  try {
    return await request;
  } finally {
    clearTimeout(timer);
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

// How many days a destination keeps backups - its own, or the global
// default. Matches config_store.retention_days().
function destRetentionDays(cfg, destId) {
  const own = ((cfg.destinations || {})[destId] || {}).retention_days;
  return Number.isInteger(own) && own >= 1 ? own : (cfg.retention_days || 7);
}

function daysText(days) {
  return `${days} ${days === 1 ? "day" : "days"}`;
}

// The Retention tile: the default, as before, while every destination uses
// it; once one has its own, each destination's icon and days, one per line (#133).
function retentionStat(cfg, destIds) {
  const div = document.createElement("div");
  div.className = "overview-stat";
  const days = destIds.map((id) => destRetentionDays(cfg, id));
  const value = document.createElement("div");
  if (!destIds.some((id) => Number.isInteger(((cfg.destinations || {})[id] || {}).retention_days))) {
    value.className = "stat-value";
    value.textContent = daysText(days.length ? days[0] : (cfg.retention_days || 7));
  } else {
    value.className = "overview-retention-list";
    destIds.forEach((id, i) => {
      const row = document.createElement("div");
      row.className = "overview-retention-row";
      const m = DESTINATION_META.find((d) => d.id === id);
      if (m && m.icon) {
        const img = document.createElement("img");
        img.className = "app-icon";
        img.src = `/static/icons/${m.icon}`;
        img.alt = SHORT_DEST_LABEL[id] || destLabel(id);
        img.title = img.alt;
        row.appendChild(img);
      }
      const span = document.createElement("span");
      span.textContent = daysText(days[i]);
      row.appendChild(span);
      value.appendChild(row);
    });
  }
  const label = document.createElement("div");
  label.className = "stat-label";
  label.textContent = "Retention";
  div.append(value, label);
  return div;
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

// ---------------------------------------------------------- encryption ----
// Whether a backup is encrypted (#147): a padlock and a word, so it reads at
// a glance, at small sizes and to screen readers. Used wherever a backup is
// listed - History, the Overview, and the Restore tab.
const LOCK_SHACKLE = {
  closed: "M8 11V7a4 4 0 0 1 8 0v4",
  open: "M8 11V7a4 4 0 0 1 7.75-1.4",
};

function lockIcon(closed) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", "enc-icon");
  const body = document.createElementNS(ns, "rect");
  Object.entries({ x: 5, y: 11, width: 14, height: 10, rx: 2 }).forEach(([k, v]) => body.setAttribute(k, v));
  if (closed) body.setAttribute("class", "enc-icon-fill");
  const shackle = document.createElementNS(ns, "path");
  shackle.setAttribute("d", closed ? LOCK_SHACKLE.closed : LOCK_SHACKLE.open);
  svg.append(body, shackle);
  return svg;
}

function encryptionBadge(encrypted) {
  const badge = document.createElement("span");
  badge.className = `enc-badge ${encrypted ? "enc-on" : "enc-off"}`;
  badge.append(lockIcon(encrypted), document.createTextNode(encrypted ? "Encrypted" : "Not encrypted"));
  return badge;
}

const DECRYPTED_DOWNLOAD_TEXT = "This download is decrypted. Anyone with access to this computer can read it.";

// An app's encrypted backup downloads decrypted; Backuparr's own stays as it
// is, for the setup screen's restore.
function warnWhenDownloadIsDecrypted(link, appId, entry) {
  if (entry.encrypted && appId !== "backuparr") link.addEventListener("click", () => toast(DECRYPTED_DOWNLOAD_TEXT, "warn"));
}

// The one-time notice for an install updated to 3.0, when backups became
// encrypted by default. A new install never sees it. While the first start's
// backup of Backuparr runs, it waits (up to 5 minutes) to say if it was made.
async function showEncryptionNotice() {
  let notice = await apiFetch("/api/encryption-notice");
  for (let tries = 0; notice.show && notice.self_backup_pending && tries < 100; tries++) {
    await new Promise((resolve) => setTimeout(resolve, 3000));
    notice = await apiFetch("/api/encryption-notice");
  }
  if (!notice.show) return;
  document.getElementById("encryption-notice-self-backup").classList.toggle("hidden", !notice.self_backup_made);
  const modal = document.getElementById("encryption-notice-modal");
  const ok = document.getElementById("encryption-notice-ok");
  modal.classList.remove("hidden");
  ok.focus();
  ok.addEventListener("click", async () => {
    ok.disabled = true;
    try {
      await apiFetch("/api/encryption-notice/acknowledge", { method: "POST" });
      modal.classList.add("hidden");
    } catch (e) {
      toast(sentence(e.message), "fail");
      ok.disabled = false;
    }
  }, { once: true });
}

// One Overview tile: an app's (or Backuparr's own) latest backup, or why its
// last run failed, with download and log links.
function overviewCard(appId, { label, icon, latest, failure, latestRun, idleText }) {
  const card = document.createElement("div");
  card.className = "overview-app-card";
  card.id = `overview-card-${appId}`; // the status bar's failure lines scroll here

  const head = document.createElement("div");
  head.className = "overview-app-head";
  if (icon) {
    const img = document.createElement("img");
    img.className = "app-icon";
    img.src = icon;
    img.alt = "";
    head.appendChild(img);
  }
  const h3 = document.createElement("h3");
  h3.textContent = label;
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
    failLine.textContent = `Last run failed: ${clause(failure)}`;
    last.appendChild(failLine);
    if (isCertificateFailure(failure)) last.appendChild(cardLink(appId));
  } else if (latest) {
    const summaryLine = document.createElement("span");
    summaryLine.textContent = `Last backup: ${fmtTime(latest.mod_time)} (${fmtBytes(latest.size)})`;
    const fn = document.createElement("span");
    fn.className = "filename";
    fn.textContent = latest.name;
    last.append(summaryLine, fn, encryptionBadge(latest.encrypted));
  } else {
    last.textContent = idleText;
  }
  card.appendChild(last);

  const links = document.createElement("div");
  links.className = "overview-links";
  if (latest) {
    const dlLink = document.createElement("a");
    dlLink.className = "overview-download-link";
    dlLink.textContent = "Download latest backup";
    dlLink.href = `/api/history/${latest.destId}/${appId}/${encodeURIComponent(latest.name)}/download`;
    warnWhenDownloadIsDecrypted(dlLink, appId, latest);
    links.appendChild(dlLink);
  }
  if (latestRun) links.appendChild(viewLogButton(appId, latestRun.id));
  if (links.childElementCount) card.appendChild(links);
  return card;
}

// ------------------------------------------------------- status bar (#167) ----
// One line for every app's latest backup, under At a Glance: each enabled app
// and instance, and Backuparr's own while it's on, judged like its card's dot.
let OVERVIEW_RUN_TIMER = null;

function statusIcon(ok) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("class", "status-icon");
  const circle = document.createElementNS(ns, "circle");
  Object.entries({ cx: 12, cy: 12, r: 11 }).forEach(([k, v]) => circle.setAttribute(k, v));
  const mark = document.createElementNS(ns, "path");
  mark.setAttribute("d", ok ? "M7 12.5l3.2 3.2L17 9" : "M8 8l8 8M16 8l-8 8");
  svg.append(circle, mark);
  return svg;
}

function overviewStatusState(item) {
  return item.failure ? "fail" : item.latest ? "ok" : "idle";
}

// While a backup runs the bar says so, and the Overview reloads when it ends.
async function reloadOverviewWhenRunEnds() {
  OVERVIEW_RUN_TIMER = null;
  if (!document.getElementById("tab-overview").classList.contains("active")) return;
  try {
    if (!(await apiFetch("/api/backup/status")).running) {
      loadOverview();
      return;
    }
  } catch (e) {
    // the container may be restarting; try again
  }
  OVERVIEW_RUN_TIMER = setTimeout(reloadOverviewWhenRunEnds, 5000);
}

function scrollToOverviewCard(appId) {
  const card = document.getElementById(`overview-card-${appId}`);
  if (!card) return;
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.remove("flash");
  void card.offsetWidth; // restart the highlight
  card.classList.add("flash");
  setTimeout(() => card.classList.remove("flash"), 1700);
}

function renderOverviewStatus(items, running) {
  const bar = document.getElementById("overview-status");
  bar.replaceChildren();
  clearTimeout(OVERVIEW_RUN_TIMER);
  OVERVIEW_RUN_TIMER = null;
  bar.classList.toggle("hidden", !items.length);
  if (!items.length) return;

  const head = document.createElement("div");
  head.className = "overview-status-head";
  bar.appendChild(head);
  if (running) {
    bar.dataset.state = "running";
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    const link = document.createElement("button");
    link.type = "button";
    link.className = "link-btn";
    link.textContent = "Run & Status";
    link.addEventListener("click", () => document.querySelector('.tab-btn[data-tab="run"]').click());
    head.append(spinner, document.createTextNode("Backing up... "), link);
    OVERVIEW_RUN_TIMER = setTimeout(reloadOverviewWhenRunEnds, 5000);
    return;
  }

  const failed = items.filter((item) => overviewStatusState(item) === "fail");
  const succeeded = items.filter((item) => overviewStatusState(item) === "ok").length;
  const notYet = items.length - failed.length - succeeded;
  bar.dataset.state = failed.length ? "fail" : "ok";
  const total = items.length;
  let text;
  if (!failed.length && !notYet) text = total === 1 ? "The backup succeeded" : `All ${total} backups succeeded`;
  else text = `${succeeded} of ${total} ${total === 1 ? "backup" : "backups"} succeeded`;
  const strong = document.createElement("span");
  strong.className = "overview-status-text";
  strong.textContent = text;
  head.append(statusIcon(!failed.length), strong);
  if (notYet) {
    const extra = document.createElement("span");
    extra.className = "overview-status-extra";
    extra.textContent = `${notYet} not backed up yet`;
    head.appendChild(extra);
  }

  if (!failed.length) return;
  const list = document.createElement("ul");
  list.className = "overview-status-list";
  failed.forEach((item) => {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "overview-status-item";
    btn.title = `Go to ${item.label}`;
    if (item.icon) {
      const img = document.createElement("img");
      img.className = "app-icon";
      img.src = item.icon;
      img.alt = "";
      btn.appendChild(img);
    }
    // One run of text, so a long error wraps under the name on a phone.
    const line = document.createElement("span");
    line.className = "overview-status-line";
    const name = document.createElement("strong");
    name.textContent = `${item.label}:`;
    const error = document.createElement("span");
    error.className = "overview-status-error";
    error.textContent = ` ${item.failure}`;
    line.append(name, error);
    btn.appendChild(line);
    btn.addEventListener("click", () => scrollToOverviewCard(item.id));
    li.appendChild(btn);
    if (isCertificateFailure(item.failure)) li.appendChild(cardLink(item.id));
    list.appendChild(li);
  });
  bar.appendChild(list);
}

// The scheduler skips a saved schedule that isn't valid, e.g. one an update
// of the cron library stopped accepting (#166) - say so instead of nothing.
function showScheduleWarning(elementId, valid) {
  const el = document.getElementById(elementId);
  el.textContent = valid === false ? "Your backup schedule isn't valid any more. Check it in Settings." : "";
  el.classList.toggle("hidden", valid !== false);
}

async function loadOverview() {
  const summaryEl = document.getElementById("overview-summary");
  const appsEl = document.getElementById("overview-apps");
  summaryEl.textContent = "Loading...";
  appsEl.innerHTML = "";
  renderOverviewStatus([], false); // nothing stale while it loads
  try {
    const cfg = await apiFetch("/api/config");
    const destIds = enabledDestinationIds(cfg);

    const [histories, latestRuns, runState] = await Promise.all([
      Promise.all(destIds.map((id) => whileStorageWakes(id, apiFetch(`/api/history/${id}`), () => {
        summaryEl.textContent = STORAGE_WAIT_TEXT;
      }).catch((e) => {
        toast(`${destLabel(id)}: ${clause(e.message)}`, "fail");
        return {};
      }))),
      apiFetch("/api/runlogs"),
      apiFetch("/api/backup/status").catch(() => null), // the status bar's "Backing up..."
    ]);
    // Per app, the single most recent backup across every enabled destination.
    const latestByApp = {};
    histories.forEach((history, i) => {
      const destId = destIds[i];
      Object.keys(history).forEach((appId) => {
        const entry = (history[appId] || []).find((e) => !e.missing);
        if (entry && (!latestByApp[appId] || entry.mod_time > latestByApp[appId].mod_time)) {
          latestByApp[appId] = { ...entry, destId };
        }
      });
    });

    const allIds = Object.keys(cfg.apps || {});
    const enabledIds = allIds.filter((id) => cfg.apps[id].enabled);

    // Each app's latest saved run (runlog.py), so its status survives a restart.
    const failureByApp = {};
    Object.entries(latestRuns || {}).forEach(([appId, run]) => {
      if (!run.ok) failureByApp[appId] = run.error || "unknown error";
    });

    summaryEl.innerHTML = "";
    const textStats = [
      { label: "Enabled services", value: `${enabledIds.length} / ${allIds.length}` },
      { label: "Cron schedule", value: cfg.cron_schedule || "-" },
    ];
    const [enabledStat, cronStat] = textStats.map((s) => {
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
    summaryEl.append(enabledStat, destinationsStat(destIds), cronStat, retentionStat(cfg, destIds));
    showScheduleWarning("overview-schedule-warning", cfg.schedule_valid);

    const statusItems = [
      ...(cfg.self_backup === false ? [] : ["backuparr"]),
      ...enabledIds,
    ].map((appId) => ({
      id: appId, label: appLabel(appId), latest: latestByApp[appId], failure: failureByApp[appId],
      icon: appId === "backuparr" ? "/static/logo.png" : appMeta(appId) ? `/static/icons/${appMeta(appId).icon}` : null,
    }));
    // With no app enabled the page says to add one; the bar has nothing to say.
    renderOverviewStatus(enabledIds.length ? statusItems : [], !!(runState && runState.running));

    // Backuparr's own backup leads, always there (#110); then each enabled app.
    appsEl.appendChild(overviewCard("backuparr", {
      label: "Backuparr", icon: "/static/logo.png", latest: latestByApp.backuparr,
      failure: failureByApp.backuparr, latestRun: (latestRuns || {}).backuparr,
      idleText: cfg.self_backup === false ? "Turned off in Settings." : "No backups yet.",
    }));
    if (!enabledIds.length) {
      const empty = document.createElement("p");
      empty.className = "overview-empty";
      empty.textContent = "No services enabled yet - head to Settings to add one.";
      appsEl.appendChild(empty);
      return;
    }
    enabledIds.forEach((appId) => {
      const meta = appMeta(appId);
      appsEl.appendChild(overviewCard(appId, {
        label: appLabel(appId), icon: meta ? `/static/icons/${meta.icon}` : null, latest: latestByApp[appId],
        failure: failureByApp[appId], latestRun: (latestRuns || {})[appId], idleText: "No backups yet.",
      }));
    });
  } catch (e) {
    summaryEl.innerHTML = "";
    const error = document.createElement("p");
    error.className = "overview-empty";
    error.textContent = `Error loading overview: ${clause(e.message)}`;
    appsEl.replaceChildren(error);
  }
}

// ------------------------------------------------------------ run log ----
// One app's log from one run, in a floating panel over the page.
let logPanelSeq = 0;

function destinationResultText(result) {
  if (!result) return "not uploaded";
  return result.ok ? "uploaded" : `failed - ${result.error}`;
}

async function openRunLog(appId, runId) {
  const panel = document.getElementById("log-panel");
  const meta = document.getElementById("log-panel-meta");
  const lines = document.getElementById("log-panel-lines");
  document.getElementById("log-panel-title").textContent = `${appLabel(appId)} - Run Log`;
  meta.textContent = "Loading...";
  lines.innerHTML = "";
  panel.classList.remove("hidden");
  const seq = ++logPanelSeq;
  let run;
  try {
    run = await apiFetch(`/api/runlogs/${encodeURIComponent(appId)}/${encodeURIComponent(runId)}`);
  } catch (e) {
    if (seq === logPanelSeq) meta.textContent = sentence(e.message);
    return;
  }
  if (seq !== logPanelSeq) return;

  meta.innerHTML = "";
  const when = document.createElement("p");
  when.textContent = `Run: ${fmtTime(run.started)}${run.app_version ? ` - ${appId === "backuparr" ? "Backuparr" : appMeta(appId)?.label || appId} ${run.app_version}` : ""}`;
  const result = document.createElement("p");
  result.className = run.ok ? "ok" : "fail";
  result.textContent = run.ok ? `Backed up: ${run.file}` : `Failed: ${clause(run.error || "unknown error")}`;
  meta.append(when, result);
  const destIds = Object.keys(run.destinations || {});
  if (destIds.length) {
    const list = document.createElement("ul");
    destIds.forEach((destId) => {
      const r = run.destinations[destId];
      const li = document.createElement("li");
      li.className = !r ? "muted" : r.ok ? "ok" : "fail";
      li.textContent = `${destLabel(destId)}: ${destinationResultText(r)}`;
      list.appendChild(li);
    });
    meta.appendChild(list);
  }

  (run.lines || []).forEach((line) => {
    const li = document.createElement("li");
    li.className = `log-${String(line.level || "").toLowerCase()}`;
    const time = document.createElement("span");
    time.className = "log-time";
    time.textContent = new Date(line.time).toLocaleTimeString();
    const text = document.createElement("span");
    text.textContent = line.message;
    li.append(time, text);
    lines.appendChild(li);
  });
  if (!(run.lines || []).length) lines.innerHTML = '<li class="log-time">No log lines were kept for this run.</li>';
}

function closeRunLog() {
  logPanelSeq++;
  document.getElementById("log-panel").classList.add("hidden");
}

function initRunLogPanel() {
  document.getElementById("log-panel-close").addEventListener("click", closeRunLog);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !document.getElementById("log-panel").classList.contains("hidden")) closeRunLog();
  });
  // A click anywhere outside the panel closes it too - except on a View log
  // button, which opens (or switches) the panel instead.
  document.addEventListener("click", (e) => {
    const panel = document.getElementById("log-panel");
    if (panel.classList.contains("hidden") || panel.contains(e.target) || e.target.closest(".view-log-btn")) return;
    closeRunLog();
  });
}

function viewLogButton(appId, runId) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "link-btn view-log-btn";
  btn.textContent = "View log";
  btn.addEventListener("click", () => openRunLog(appId, runId));
  return btn;
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

// ----------------------------------------------------------- user menu ----
// The username's menu in the top bar: Change password and Log out (#120).
// Not on the page when BACKUPARR_DISABLE_AUTH is set.
function initUserMenu() {
  const btn = document.getElementById("user-menu-btn");
  if (!btn) return;
  const list = document.getElementById("user-menu-list");
  const setOpen = (open) => {
    list.classList.toggle("hidden", !open);
    btn.setAttribute("aria-expanded", String(open));
  };
  btn.addEventListener("click", () => setOpen(list.classList.contains("hidden")));
  document.addEventListener("click", (e) => {
    if (!document.getElementById("user-menu").contains(e.target)) setOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !list.classList.contains("hidden")) {
      setOpen(false);
      btn.focus();
    }
  });
  list.addEventListener("click", () => setOpen(false));
  initChangePassword();
}

function initChangePassword() {
  const modal = document.getElementById("password-modal");
  const form = document.getElementById("password-form");
  const result = document.getElementById("pw-result");
  const fields = ["pw-current", "pw-new", "pw-confirm"].map((id) => document.getElementById(id));
  const close = () => {
    modal.classList.add("hidden");
    form.reset();
    result.textContent = "";
  };
  document.getElementById("change-password-btn").addEventListener("click", () => {
    form.reset();
    result.textContent = "";
    modal.classList.remove("hidden");
    fields[0].focus();
  });
  document.getElementById("password-modal-close").addEventListener("click", close);
  document.getElementById("pw-cancel-btn").addEventListener("click", close);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !modal.classList.contains("hidden")) close();
  });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const [current, next, confirmed] = fields.map((f) => f.value);
    if (next !== confirmed) {
      result.textContent = "The new passwords don't match";
      result.className = "save-result fail";
      return;
    }
    const submit = document.getElementById("pw-submit-btn");
    submit.disabled = true;
    try {
      await apiFetch("/api/password", {
        method: "POST",
        body: JSON.stringify({ current_password: current, new_password: next, confirm_password: confirmed }),
      });
      close();
      toast("Password changed - other browsers were signed out", "ok");
    } catch (err) {
      result.textContent = sentence(err.message);
      result.className = "save-result fail";
    } finally {
      submit.disabled = false;
    }
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
// Home Assistant's status token (#170): made, replaced and removed straight
// away. Only shown when made; config.json keeps its hash.
function showHaState(enabled, token) {
  document.getElementById("ha-enabled").checked = enabled;
  document.getElementById("ha-actions").classList.toggle("hidden", !enabled);
  document.getElementById("ha-token-field").classList.toggle("hidden", !token);
  document.getElementById("ha-token").value = token || "";
}

async function newHaToken(message) {
  const result = document.getElementById("ha-result");
  try {
    const { token } = await apiFetch("/api/ha/token", { method: "POST", body: "{}" });
    showHaState(true, token);
    result.textContent = message;
    result.className = "test-result ok";
  } catch (e) {
    showHaState(false, null);
    toast(`Could not turn on the integration: ${clause(e.message)}`, "fail");
  }
}

function initHaEvents() {
  const toggle = document.getElementById("ha-enabled");
  toggle.addEventListener("change", async () => {
    if (toggle.checked) {
      await newHaToken("Turned on.");
      return;
    }
    if (!confirm("Turn off the Home Assistant integration? Its token stops working.")) {
      toggle.checked = true;
      return;
    }
    try {
      await apiFetch("/api/ha/disable", { method: "POST", body: "{}" });
      showHaState(false, null);
      document.getElementById("ha-result").textContent = "";
    } catch (e) {
      toggle.checked = true;
      toast(`Could not turn off the integration: ${clause(e.message)}`, "fail");
    }
  });
  document.getElementById("ha-new-token-btn").addEventListener("click", async () => {
    if (!confirm("Make a new token? The current one stops working.")) return;
    await newHaToken("New token made.");
  });
  document.getElementById("ha-copy-btn").addEventListener("click", async () => {
    const input = document.getElementById("ha-token");
    try {
      await navigator.clipboard.writeText(input.value);
      toast("Copied", "ok");
    } catch (e) {
      input.select(); // no clipboard over plain HTTP
      toast("Couldn't copy automatically - the token is selected, so copy it by hand.", "fail");
    }
  });
}

// HTTPS (#177): the certificate in use, and HTTPS only, which closes plain
// HTTP straight away. It can only be turned on from a page that came in on
// the HTTPS port, so it can't lock anyone out.
let HTTPS_STATE = null;

async function loadHttps() {
  try {
    HTTPS_STATE = await apiFetch("/api/https");
  } catch (e) {
    return;
  }
  showHttpsState();
}

function showHttpsState() {
  const s = HTTPS_STATE;
  const until = new Date(s.expires).toLocaleDateString();
  document.getElementById("https-summary").textContent = s.own_certificate
    ? `HTTPS on port ${s.port}, with your certificate (until ${until}).`
    : `HTTPS on port ${s.port}, with a self-signed certificate (until ${until}).`;
  setFingerprintTip(document.getElementById("https-fingerprint"), s.fingerprint);
  const certError = document.getElementById("https-cert-error");
  certError.textContent = s.certificate_error ? `Your certificate isn't used: ${s.certificate_error}.` : "";
  certError.classList.toggle("hidden", !s.certificate_error);

  const toggle = document.getElementById("https-only");
  toggle.checked = s.https_only;
  toggle.disabled = s.https_only_locked || (!s.https_only && !s.on_https);
  const note = document.getElementById("https-note");
  note.replaceChildren();
  if (s.https_only_locked) {
    note.textContent = "Set by BACKUPARR_HTTPS_ONLY.";
  } else if (s.https_only || s.on_https) {
    note.textContent = `Closes HTTP on port ${s.http_port}.`;
  } else if (location.protocol === "http:") {
    // The container's port: the host may publish it as another.
    note.textContent = `To turn this on, open Backuparr over HTTPS (port ${s.port}).`;
  } else {
    note.textContent = `To turn this on, point your proxy at HTTPS port ${s.port}.`;
  }
}

function initHttpsEvents() {
  const toggle = document.getElementById("https-only");
  toggle.addEventListener("change", async () => {
    const enabled = toggle.checked;
    if (enabled && !confirm(`Close HTTP on port ${HTTPS_STATE.http_port}? Backuparr will only answer on HTTPS, port ${HTTPS_STATE.port}.`)) {
      toggle.checked = false;
      return;
    }
    const result = document.getElementById("https-result");
    try {
      await apiFetch("/api/https/only", { method: "POST", body: JSON.stringify({ enabled }) });
      HTTPS_STATE.https_only = enabled;
      showHttpsState();
      result.textContent = enabled ? "HTTP closed." : "HTTP open again.";
      result.className = "test-result ok";
    } catch (e) {
      toggle.checked = !enabled;
      toast(`Could not change HTTPS only: ${clause(e.message)}`, "fail");
    }
  });
}

// Backuparr's own card (#165): its body, like an app card's, shows while on.
function showSelfCardBody() {
  const card = document.querySelector(".self-card");
  card.querySelector(".app-card-body").classList.toggle("hidden", !document.getElementById("s-self_backup").checked);
}

function appCard(appId) {
  return document.querySelector(`.app-card[data-app="${appId}"]`);
}

function setAppCardExpanded(card, expanded) {
  const body = card.querySelector(".app-card-body");
  // A card holding discovery results stays open so its choices remain visible.
  if (body) body.classList.toggle("hidden", !expanded && !card.dataset.discovered);
}

function fillAppCard(appId, cfg) {
  if (appId === "plex" && PLEX_LOGIN) stopPlexLogin("Plex settings reloaded. Sign in again to get a token.");
  const card = appCard(appId);
  if (!card) return;
  card.querySelector(".f-enabled").checked = !!cfg.enabled;
  const urlInput = card.querySelector(".f-url");
  urlInput.value = cfg.url || "";
  const keyInput = card.querySelector(".f-api_key");
  if (keyInput) keyInput.value = cfg.api_key || ""; // absent for apps without an API key
  card.querySelectorAll(".f-extra").forEach((input) => {
    const value = cfg[input.dataset.field];
    if (input.type === "checkbox") input.checked = typeof value === "boolean" ? value : input.dataset.default === "true";
    else if (input.tagName === "SELECT") input.value = value || input.dataset.default || "";
    else input.value = value || "";
  });
  card.querySelectorAll(".app-list").forEach((list) => renderAppList(list, cfg[list.dataset.field] || [], null));
  showInstallOptions(card);
  const encryptInput = card.querySelector(".f-encrypt");
  if (encryptInput) encryptInput.checked = cfg.encrypt !== false; // absent on always-encrypted apps
  if (appMeta(appId)?.pins_certificate) card.dataset.trustedCert = cfg.trusted_cert ? JSON.stringify(cfg.trusted_cert) : "";
  card.querySelector(".cert-trust")?.remove();
  showLoginFields(card, Array.from(card.querySelectorAll(".login-field .f-extra")).some((input) => input.value));
  const nameInput = card.querySelector(".f-name");
  if (nameInput) {
    nameInput.value = cfg.name || "";
    updateInstanceCardLabel(card);
  }
  setAppCardExpanded(card, !!cfg.enabled);
  if (appId === "prowlarr") updateDiscoveryButton();
}

// Home Assistant's backup options (#155): the ones only some installs have
// show once a Test connection found that kind (its install_type), and the
// list of apps while Apps is "Chosen apps".
function showInstallOptions(card) {
  const install = card.querySelector('.f-extra[data-field="install_type"]')?.value || "";
  const choice = card.querySelector('select.f-extra[data-field="apps"]')?.value;
  card.querySelectorAll("[data-only-install]").forEach((el) => {
    let show = el.dataset.onlyInstall === install;
    if (el.classList.contains("app-list")) show = show && choice === "chosen";
    el.classList.toggle("hidden", !show);
  });
  // show_if (#161): [{field: [values]}, ...] - shown while every field of
  // any one condition has one of its values (UniFi's History: the
  // standalone app, or UniFi OS backing up Network only).
  card.querySelectorAll("[data-show-if]").forEach((el) => {
    let conditions = [];
    try { conditions = JSON.parse(el.dataset.showIf); } catch { /* malformed: stays hidden */ }
    const value = (name) => card.querySelector(`.f-extra[data-field="${name}"]`)?.value || "";
    const show = conditions.some((cond) => Object.entries(cond).every(([name, values]) => values.includes(value(name))));
    el.classList.toggle("hidden", !show);
  });
}

function readAppList(list) {
  return Array.from(list.querySelectorAll("input:checked"), (input) => ({ slug: input.value, name: input.dataset.name }));
}

// chosen: the apps picked ([{slug, name}]); installed: the apps a Test
// connection listed, or null before one - then the chosen ones are listed.
function renderAppList(list, chosen, installed) {
  const picked = new Set(chosen.map((app) => app.slug));
  const apps = installed || chosen;
  const items = list.querySelector(".app-list-items");
  items.replaceChildren(...apps.map((app) => {
    const row = document.createElement("label");
    row.className = "field toggle-field app-list-item";
    const toggle = document.createElement("span");
    toggle.className = "switch";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = app.slug;
    input.dataset.name = app.name;
    input.checked = picked.has(app.slug);
    const slider = document.createElement("span");
    slider.className = "slider";
    toggle.append(input, slider);
    const name = document.createElement("span");
    name.textContent = app.name;
    row.append(toggle, name);
    return row;
  }));
  const empty = list.querySelector(".app-list-empty");
  empty.textContent = installed ? "No apps are installed." : "Test the connection to list the installed apps.";
  empty.classList.toggle("hidden", apps.length > 0);
}

// What a Test connection found for the card (#155): Home Assistant's
// install type and its installed apps.
function applyCardInfo(card, info) {
  const install = card.querySelector('.f-extra[data-field="install_type"]');
  if (install && typeof info.install === "string") install.value = info.install;
  // Synology (#168): DSM remembered this device after a 2FA code - Save keeps
  // it. The code worked once and is cleared.
  const deviceToken = card.querySelector('.f-extra[data-field="device_token"]');
  if (deviceToken && typeof info.device_token === "string") {
    deviceToken.value = info.device_token;
    const code = card.querySelector('.f-extra[data-field="otp_code"]');
    if (code) code.value = "";
  }
  card.querySelectorAll(".app-list").forEach((list) => renderAppList(list, readAppList(list), info.apps || []));
  showInstallOptions(card);
}

// Radarr, Sonarr, Lidarr, Whisparr and Prowlarr cards keep their username
// and password hidden until a test finds the app asks for a login, or they
// hold a value; Synology's card its 2FA code until DSM asks for one.
function showLoginFields(card, show) {
  card.querySelectorAll(".login-field").forEach((field) => field.classList.toggle("hidden", !show));
}

function revealLoginFields(card) {
  const hidden = card.querySelector(".login-field.hidden");
  showLoginFields(card, true);
  if (hidden) card.querySelector(".login-field .f-extra")?.focus();
}

function prowlarrCredentials() {
  const card = appCard("prowlarr");
  return {
    url: card.querySelector(".f-url").value.trim(),
    api_key: card.querySelector(".f-api_key").value.trim(),
    username: card.querySelector('.f-extra[data-field="username"]').value.trim(),
    password: card.querySelector('.f-extra[data-field="password"]').value,
    request_headers: readRequestHeaders(),
  };
}

function updateDiscoveryButton() {
  const btn = document.getElementById("discover-btn");
  const { url, api_key } = prowlarrCredentials();
  btn.disabled = DISCOVERY_RUNNING || !url || !api_key;
  btn.textContent = DISCOVERY_RUNNING ? "Discovering..." : "Discover services";
}

function readAppCard(appId) {
  const card = appCard(appId);
  const urlInput = card.querySelector(".f-url");
  const keyInput = card.querySelector(".f-api_key");
  const out = {
    enabled: card.querySelector(".f-enabled").checked,
    url: urlInput.value.trim(),
    api_key: keyInput ? keyInput.value.trim() : "",
  };
  card.querySelectorAll(".f-extra").forEach((input) => {
    out[input.dataset.field] = input.type === "checkbox" ? input.checked : input.value;
  });
  card.querySelectorAll(".app-list").forEach((list) => {
    out[list.dataset.field] = readAppList(list);
  });
  const encryptInput = card.querySelector(".f-encrypt");
  if (encryptInput) out.encrypt = encryptInput.checked;
  const nameInput = card.querySelector(".f-name");
  if (nameInput) out.name = nameInput.value.trim();
  if (appMeta(appId)?.pins_certificate) out.trusted_cert = card.dataset.trustedCert ? JSON.parse(card.dataset.trustedCert) : null;
  return out;
}

// ------------------------------------------------------------ instances ----
// An extra Radarr, Sonarr, Lidarr or Whisparr instance gets a copy of its app's card, with a name
// field and a Remove button. Adding and removing take effect on Save.
function instanceCards(type) {
  return Array.from(document.querySelectorAll(".app-card[data-app]"))
    .filter((card) => instanceType(card.dataset.app) === type);
}

// The id names the instance's backup folder, so it's made once, from the
// name it's added with: "4K" -> radarr-4k. Renaming keeps it. A removed
// instance's id stays taken: its backups are kept.
function newInstanceId(type, name) {
  const taken = new Set([
    ...Array.from(document.querySelectorAll(".app-card[data-app]"), (card) => card.dataset.app),
    ...REMOVED_INSTANCES, ...RETIRED_INSTANCES,
  ]);
  const max = 40 - type.length - 1;
  let slug = name.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  if (slug.startsWith(`${type}-`)) slug = slug.slice(type.length + 1); // "Radarr 4K" -> 4k
  slug = slug.slice(0, max).replace(/-+$/, "") || "2";
  for (let n = 1, candidate = slug; ; n += 1, candidate = `${slug.slice(0, max - String(n).length - 1).replace(/-+$/, "")}-${n}`) {
    if (!taken.has(`${type}-${candidate}`)) return `${type}-${candidate}`;
  }
}

function updateInstanceCardLabel(card) {
  const meta = appMeta(card.dataset.app);
  const name = card.querySelector(".f-name").value.trim();
  card.querySelector("h3").textContent = `${meta ? meta.label : card.dataset.app} (${name || "unnamed"})`;
}

function addInstanceCard(appId, values = {}) {
  const type = instanceType(appId);
  const card = appCard(type).cloneNode(true);
  card.dataset.app = appId;
  card.classList.add("instance-card");
  delete card.dataset.discovered;
  card.querySelectorAll("input:not([disabled])").forEach((input) => {
    input.value = "";
    input.checked = false;
  });
  card.querySelectorAll(".app-list-items").forEach((items) => items.replaceChildren());
  card.querySelector(".status-dot").dataset.state = "idle";
  card.querySelector(".test-result").textContent = "";
  card.querySelector(".app-discovery")?.replaceChildren();
  card.querySelectorAll(".app-discovery, .discovery-origin, .discovery-key-status").forEach((el) => el.classList.add("hidden"));

  const nameField = document.createElement("label");
  nameField.className = "field";
  const nameLabel = document.createElement("span");
  nameLabel.className = "field-label";
  const folder = document.createElement("span");
  folder.className = "muted";
  folder.textContent = `(backups go in its ${appId} folder)`;
  nameLabel.append("Name ", folder);
  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.className = "f-name";
  nameInput.maxLength = 64;
  nameInput.autocomplete = "off";
  nameField.append(nameLabel, nameInput);
  card.querySelector(".f-url").closest(".field").before(nameField);

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "btn btn-ghost btn-sm remove-instance";
  remove.textContent = "Remove";
  card.querySelector(".test-btn").after(remove);

  document.querySelector(`.instance-add[data-instance-of="${type}"]`).before(card);
  bindAppCard(card);
  fillAppCard(appId, values);
  refreshHeaderScopes();
  return card;
}

function removeInstanceCard(card) {
  const appId = card.dataset.app;
  const label = card.querySelector("h3").textContent;
  // Like the server, refuse rather than quietly narrow a header's apps.
  const scoped = Array.from(document.querySelectorAll(".request-header-row")).find((row) =>
    (row.querySelector(".header-name").value.trim() || row.querySelector(".header-value").value)
    && !row.querySelector(".header-scope-all").checked
    && row.querySelector(`.header-scope-app[value="${appId}"]`).checked);
  if (scoped) {
    const name = scoped.querySelector(".header-name").value.trim() || "unnamed";
    toast(`The ${name} request header is limited to ${label} - change the apps it goes to first.`, "fail");
    return;
  }
  if (INSTANCE_NAMES.has(appId)) REMOVED_INSTANCES.add(appId);
  APP_DISCOVERY_UPDATES.delete(appId);
  card.remove();
  refreshHeaderScopes();
  toast(`${label} removed - save settings to apply. Its backups are kept.`, "ok");
}

function initInstanceAddRows() {
  document.querySelectorAll(".instance-add").forEach((row) => {
    const type = row.dataset.instanceOf;
    const btn = row.querySelector(".instance-add-btn");
    const form = row.querySelector(".instance-add-form");
    const input = row.querySelector(".instance-add-name");
    const error = row.querySelector(".instance-add-error");
    const close = () => {
      form.classList.add("hidden");
      btn.classList.remove("hidden");
      input.value = "";
      error.textContent = "";
    };
    btn.addEventListener("click", () => {
      btn.classList.add("hidden");
      form.classList.remove("hidden");
      input.focus();
    });
    row.querySelector(".instance-add-cancel").addEventListener("click", close);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const name = input.value.trim();
      if (!name) {
        error.className = "test-result instance-add-error fail";
        error.textContent = "Enter a name.";
        return;
      }
      const max = appMeta(type).max_extra_instances;
      if (instanceCards(type).length > max) {
        error.className = "test-result instance-add-error fail";
        error.textContent = `At most ${max} extra instances.`;
        return;
      }
      close();
      const card = addInstanceCard(newInstanceId(type, name), { name, enabled: true });
      card.querySelector(".f-url").focus();
    });
  });
}


const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const trimUrl = (url) => url.replace(/\/+$/, "");

// Plex credentials are entered only in Plex's own window. Keep unsaved form
// values here while polling, and never replace credentials edited meanwhile.
function forgetPlexLogin(jobId) {
  if (jobId) apiFetch(`/api/plex/auth/${jobId}`, { method: "DELETE" }).catch(() => {});
}

function stopPlexLogin(message) {
  const flow = PLEX_LOGIN;
  PLEX_LOGIN = null;
  if (flow) {
    forgetPlexLogin(flow.jobId);
    try { flow.popup?.close(); } catch (_) { /* The browser may sever the popup handle. */ }
  }
  document.getElementById("plex-token-btn").disabled = false;
  document.getElementById("plex-token-btn").textContent = "Get Plex token";
  const link = document.getElementById("plex-auth-link");
  link.classList.add("hidden");
  link.removeAttribute("href");
  document.getElementById("plex-auth-cancel").classList.add("hidden");
  document.getElementById("plex-server-field").classList.add("hidden");
  document.getElementById("plex-server-select").onchange = null;
  document.getElementById("plex-server-select").replaceChildren();
  document.getElementById("plex-auth-status").textContent = sentence(message);
}

function applyPlexToken(flow, server) {
  if (PLEX_LOGIN !== flow) return;
  const card = appCard("plex");
  if ((APP_CREDENTIAL_EDIT_REVISION.get("plex") || 0) !== flow.revision
      || card.querySelector(".f-api_key").value !== flow.originalKey) {
    stopPlexLogin("Plex details changed. Sign in again to get a token.");
    return;
  }
  // The server's own plex.direct address replaces an empty URL or one that
  // points at the same machine: Plex's certificate is issued for that name.
  const urlInput = card.querySelector(".f-url");
  let host = "";
  try { host = new URL(urlInput.value.trim()).hostname; } catch (_) { /* Empty or not a URL. */ }
  const useUrl = server.url && (!urlInput.value.trim() || host === server.address || host.endsWith(".plex.direct"));
  stopPlexLogin(useUrl
    ? `Token and URL filled for ${server.name}. Test the connection, then save settings.`
    : `Token filled for ${server.name}. Check the server URL${server.url ? ` (its secure local address is ${server.url})` : ""}, test the connection, then save settings.`);
  if (useUrl) {
    urlInput.value = server.url;
    urlInput.dispatchEvent(new Event("input", { bubbles: true }));
  }
  const input = card.querySelector(".f-api_key");
  input.value = server.api_key;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  card.querySelector(".status-dot").dataset.state = "idle";
  card.querySelector(".test-result").textContent = "";
}

async function startPlexLogin() {
  if (PLEX_LOGIN) return;
  const card = appCard("plex");
  const flow = {
    popup: window.open("about:blank", "_blank", "popup,width=600,height=720"),
    revision: APP_CREDENTIAL_EDIT_REVISION.get("plex") || 0,
    originalKey: card.querySelector(".f-api_key").value,
    jobId: null,
  };
  PLEX_LOGIN = flow;
  if (flow.popup) {
    flow.popup.opener = null;
    flow.popup.document.title = "Plex sign-in";
    flow.popup.document.body.textContent = "Opening Plex sign-in...";
  }
  document.getElementById("plex-token-btn").disabled = true;
  document.getElementById("plex-token-btn").textContent = "Waiting for Plex...";
  document.getElementById("plex-auth-cancel").classList.remove("hidden");
  const status = document.getElementById("plex-auth-status");
  status.textContent = "Opening Plex sign-in...";
  try {
    const login = await apiFetch("/api/plex/auth", { method: "POST" });
    flow.jobId = login.job_id;
    if (PLEX_LOGIN !== flow) { forgetPlexLogin(flow.jobId); return; }
    const link = document.getElementById("plex-auth-link");
    link.href = login.auth_url;
    link.classList.remove("hidden");
    try { if (flow.popup && !flow.popup.closed) flow.popup.location.href = login.auth_url; } catch (_) { /* Use the link instead. */ }
    status.textContent = "Sign in on Plex with the server owner's account, then return here. If no window opened, use Open Plex sign-in.";
    const deadline = Date.now() + login.expires_in * 1000;
    let failures = 0;
    while (PLEX_LOGIN === flow && Date.now() < deadline) {
      await sleep(2000);
      if (PLEX_LOGIN !== flow) return;
      let result;
      try {
        result = await apiFetch(`/api/plex/auth/${flow.jobId}`);
        failures = 0;
      } catch (e) {
        if (PLEX_LOGIN !== flow) return;
        if (e.status === 400 || e.status === 404 || e.status === 410 || ++failures >= 3) throw e;
        status.textContent = "Connection interrupted. Retrying Plex sign-in...";
        continue;
      }
      if (PLEX_LOGIN !== flow) return;
      if (result.state !== "completed") continue;
      // Once received, results only live in this unsaved form.
      forgetPlexLogin(flow.jobId);
      flow.jobId = null;
      if (result.servers.length === 1) {
        applyPlexToken(flow, result.servers[0]);
      } else {
        try { flow.popup?.close(); } catch (_) { /* May already be closed. */ }
        link.classList.add("hidden");
        link.removeAttribute("href");
        const select = document.getElementById("plex-server-select");
        select.replaceChildren(new Option("Choose your server", ""));
        result.servers.forEach((server, index) => select.add(new Option(server.name, String(index))));
        select.onchange = () => {
          if (select.value !== "") applyPlexToken(flow, result.servers[Number(select.value)]);
        };
        document.getElementById("plex-server-field").classList.remove("hidden");
        status.textContent = "Choose the Plex server matching the URL above to fill its token.";
        document.getElementById("plex-token-btn").textContent = "Choose your server";
      }
      return;
    }
    if (PLEX_LOGIN === flow) stopPlexLogin("Plex sign-in expired. Click Get Plex token to try again.");
  } catch (e) {
    if (PLEX_LOGIN === flow) stopPlexLogin(e.message);
  }
}

// NZBGet's discovered secret is its password; every other app's is an API key.
function secretField(candidate) {
  return "password" in candidate ? "password" : "api_key";
}

function secretLabel(candidate) {
  return secretField(candidate) === "password" ? "password" : "API key";
}

function secretUnavailable(candidate) {
  const reason = candidate[`${secretField(candidate)}_error`] || `Prowlarr's backup did not supply this ${secretLabel(candidate)}.`;
  return `Discovery ${secretLabel(candidate)} unavailable — ${reason}`;
}

function markDiscovered(appId) {
  const card = appCard(appId);
  card.dataset.discovered = "true";
  revealAppCard(card);
  card.querySelector(".discovery-origin").classList.remove("hidden");
  setAppCardExpanded(card, true);
}

function clearDiscoveryResults() {
  APP_DISCOVERY_UPDATES.clear();
  document.getElementById("discovery-results").replaceChildren();
  document.querySelectorAll(".app-discovery").forEach((panel) => {
    panel.replaceChildren();
    panel.classList.add("hidden");
    delete panel.closest(".app-card").dataset.discovered;
  });
  document.querySelectorAll(".discovery-origin, .discovery-key-status").forEach((note) => note.classList.add("hidden"));
  document.querySelectorAll(".instance-discovered").forEach((list) => {
    list.replaceChildren();
    list.classList.add("hidden");
  });
}

// Finding a service never enables it for backups. candidate.app is only the
// app type, so the card is named: it may be an extra instance's.
function applyDiscoveredService(appId, candidate, keyOnly = false) {
  const card = appCard(appId);
  if (!keyOnly) card.querySelector(".f-url").value = candidate.url;
  if (secretField(candidate) === "password") {
    const username = card.querySelector('.f-extra[data-field="username"]');
    if (!keyOnly || !username.value) username.value = candidate.username || "";
    card.querySelector('.f-extra[data-field="password"]').value = candidate.password || "";
  } else {
    card.querySelector(".f-api_key").value = candidate.api_key || "";
  }
  markDiscovered(appId);
  card.querySelector(".status-dot").dataset.state = "idle";
  card.querySelector(".test-result").textContent = "";
  pruneNewInstanceChoices();
}

// Fill an app's card automatically when there is exactly one candidate and the
// user has neither edited the card since discovery began nor entered a key.
function autoFillDiscovered(appId, candidates, original) {
  const single = candidates.length === 1 ? candidates[0] : null;
  const field = single ? secretField(single) : "api_key";
  const current = readAppCard(appId);
  const unedited = original && original.revision === (APP_CREDENTIAL_EDIT_REVISION.get(appId) || 0)
    && original.url === current.url && original[field] === current[field];
  const keyOnly = !!single && !!current.url && trimUrl(current.url) === trimUrl(single.url) && !!single[field];
  if (!single || !unedited || current[field] || (current.url && !keyOnly)) return;
  applyDiscoveredService(appId, single, keyOnly);
}

function renderInstanceChoice(appId, candidates) {
  const card = appCard(appId);
  const panel = card.querySelector(".app-discovery");
  const keyStatus = card.querySelector(".discovery-key-status");
  const description = document.createElement("p");
  description.className = "hint";
  panel.appendChild(description);

  let selected = candidates.length === 1 ? candidates[0] : null;
  let select = null;
  if (candidates.length > 1) {
    select = document.createElement("select");
    select.setAttribute("aria-label", `Choose ${appLabel(appId)} instance`);
    select.add(new Option("Choose an instance...", ""));
    candidates.forEach((candidate, index) => select.add(new Option(`${candidate.name} — ${candidate.url}`, String(index))));
    panel.appendChild(select);
  }
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn btn-ghost btn-sm";

  const update = () => {
    btn.disabled = !selected;
    if (!selected) {
      description.textContent = appMeta(appId).multi_instance
        ? "Several found in Prowlarr. Choose the one for this app; the others can be added as instances below."
        : "Multiple instances found in Prowlarr. Choose the one you want to configure.";
      keyStatus.classList.add("hidden");
      panel.classList.remove("hidden");
      btn.classList.remove("hidden");
      btn.textContent = "Use selected service";
      return;
    }
    const fields = readAppCard(appId);
    const field = secretField(selected);
    const sameService = trimUrl(fields.url) === trimUrl(selected.url);
    const inUse = sameService && (!selected[field] || fields[field] === selected[field]);
    if (candidates.length > 1) {
      description.textContent = inUse
        ? "Selected instance filled in. Review and save settings."
        : "Choose an instance and apply its settings below.";
    } else {
      description.textContent = sameService
        ? `Prowlarr found the ${secretLabel(selected)} for this URL. Your existing settings were kept.`
        : `${selected.name} found at ${selected.url}. Your existing settings were kept.`;
    }
    keyStatus.textContent = selected[field] ? "" : secretUnavailable(selected);
    keyStatus.classList.toggle("hidden", !!selected[field]);
    panel.classList.toggle("hidden", candidates.length === 1 && inUse);
    btn.classList.toggle("hidden", inUse);
    const replaces = (fields.url && !sameService) || (fields[field] && fields[field] !== selected[field]);
    btn.textContent = replaces ? `Replace URL and ${secretLabel(selected)}` : "Use this service";
  };

  if (select) select.addEventListener("change", () => {
    selected = select.value === "" ? null : candidates[Number(select.value)];
    update();
  });
  btn.addEventListener("click", () => {
    if (!selected) return;
    applyDiscoveredService(appId, selected);
    update();
  });
  APP_DISCOVERY_UPDATES.set(appId, update);  // refreshed when the card's fields are edited
  update();
  panel.appendChild(btn);
}

function renderDiscoveryNotes(result) {
  const keyErrors = new Set(result.candidates.map((candidate) => candidate[`${secretField(candidate)}_error`]).filter(Boolean));
  const root = document.getElementById("discovery-results");
  result.warnings
    .filter((warning) => !keyErrors.has(warning) && !warning.startsWith("Some API keys or passwords are unavailable."))
    .forEach((warning) => {
      const note = document.createElement("p");
      note.className = "discovery-warning";
      note.textContent = warning;
      root.appendChild(note);
    });
  const total = result.candidates.length;
  const keys = result.candidates.filter((candidate) => candidate[secretField(candidate)]).length;
  document.getElementById("discovery-status").textContent = total
    ? `Found ${total} service(s); API keys and passwords available: ${keys}/${total}. Review the details and save settings.`
    : "No supported services found in Prowlarr. Configure them manually below.";
}

function renderDiscoveryResults(result, initial) {
  clearDiscoveryResults();
  const groups = new Map();
  result.candidates.forEach((candidate) => {
    if (!groups.has(candidate.app)) groups.set(candidate.app, []);
    groups.get(candidate.app).push(candidate);
  });
  groups.forEach((candidates, type) => {
    // A card already holding a candidate's URL gets that candidate only (so
    // a missing key can be filled); the app's own card otherwise offers the
    // ones no other card holds.
    const unclaimed = candidates.filter((candidate) => !onInstanceCard(type, candidate.url));
    instanceCards(type).forEach((card) => {
      const appId = card.dataset.app;
      const url = trimUrl(readAppCard(appId).url);
      const own = candidates.filter((candidate) => url && trimUrl(candidate.url) === url);
      const offered = own.length ? own : appId === type ? unclaimed : [];
      if (!offered.length) return;
      markDiscovered(appId);
      autoFillDiscovered(appId, offered, initial[appId]);
      renderInstanceChoice(appId, offered);
    });
    if (appMeta(type).multi_instance) renderNewInstanceChoices(type, candidates);
  });
  renderDiscoveryNotes(result);
}

function onInstanceCard(type, url) {
  return instanceCards(type).some((card) => trimUrl(readAppCard(card.dataset.app).url) === trimUrl(url));
}

// Drops offers whose URL has since been filled in on a card.
function pruneNewInstanceChoices() {
  document.querySelectorAll(".instance-discovered").forEach((list) => {
    const type = list.closest(".instance-add").dataset.instanceOf;
    list.querySelectorAll(".instance-discovered-row").forEach((row) => {
      if (onInstanceCard(type, row.dataset.url)) row.remove();
    });
    if (!list.querySelector(".instance-discovered-row")) list.classList.add("hidden");
  });
}

// Candidates no card holds yet, each offered as a new extra instance.
function renderNewInstanceChoices(type, candidates) {
  const list = document.querySelector(`.instance-add[data-instance-of="${type}"] .instance-discovered`);
  const remaining = candidates.filter((candidate) => !onInstanceCard(type, candidate.url));
  // With the app's own card still empty, one candidate goes there instead.
  if (!remaining.length || (remaining.length === 1 && !readAppCard(type).url)) return;
  const intro = document.createElement("p");
  intro.className = "hint";
  intro.textContent = `Also found in Prowlarr - add as another ${appMeta(type).label}:`;
  list.replaceChildren(intro);
  remaining.forEach((candidate) => {
    const row = document.createElement("div");
    row.className = "instance-discovered-row";
    row.dataset.url = candidate.url;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-ghost btn-sm";
    btn.textContent = "Add as instance";
    btn.addEventListener("click", () => {
      if (onInstanceCard(type, candidate.url)) return pruneNewInstanceChoices();
      const appId = newInstanceId(type, candidate.name);
      // Prowlarr names it like "Radarr 4K"; the card already says Radarr.
      const name = candidate.name.replace(new RegExp(`^${appMeta(type).label}\\b[\\s:-]*`, "i"), "") || candidate.name;
      addInstanceCard(appId, { name, url: candidate.url, api_key: candidate.api_key || "" });
      markDiscovered(appId);
      if (!candidate.api_key) {
        const keyStatus = appCard(appId).querySelector(".discovery-key-status");
        keyStatus.textContent = secretUnavailable(candidate);
        keyStatus.classList.remove("hidden");
      }
      pruneNewInstanceChoices();
    });
    row.append(`${candidate.name} — ${candidate.url}`, btn);
    list.appendChild(row);
  });
  list.classList.remove("hidden");
}

async function pollDiscovery(jobId, status) {
  const deadline = Date.now() + 10 * 60 * 1000;
  let failures = 0;
  while (Date.now() < deadline) {
    try {
      const state = await apiFetch(`/api/discovery/prowlarr/${jobId}`);
      failures = 0;
      status.textContent = sentence(state.message);
      if (state.state !== "running") return state;
    } catch (e) {
      // Tolerate a few transient failures; a 4xx (other than 429) is final.
      if ((e.status && e.status < 500 && e.status !== 429) || ++failures > 5) throw e;
      status.textContent = "Connection interrupted. Waiting for Prowlarr discovery results...";
      await sleep(2000);
      continue;
    }
    await sleep(1000);
  }
  throw new Error("Discovery is taking longer than expected. Try again after Prowlarr finishes its backup.");
}

async function discoverServices() {
  const credentials = prowlarrCredentials();
  if (DISCOVERY_RUNNING || !credentials.url || !credentials.api_key) return;
  DISCOVERY_RUNNING = true;
  updateDiscoveryButton();
  const status = document.getElementById("discovery-status");
  status.textContent = "Connecting to Prowlarr...";
  clearDiscoveryResults();
  // Snapshot the cards so edits made while discovery runs are never overwritten.
  const initial = {};
  APP_META.filter((meta) => meta.discoverable).map((meta) => meta.id).flatMap(instanceCards).forEach((card) => {
    const appId = card.dataset.app;
    initial[appId] = { ...readAppCard(appId), revision: APP_CREDENTIAL_EDIT_REVISION.get(appId) || 0 };
  });
  let jobId = null;
  let finished = false;
  try {
    ({ job_id: jobId } = await apiFetch("/api/discovery/prowlarr", {
      method: "POST", body: JSON.stringify(credentials),
    }));
    const state = await pollDiscovery(jobId, status);
    finished = true;
    if (JSON.stringify(credentials) !== JSON.stringify(prowlarrCredentials())) {
      status.textContent = "Prowlarr details changed. Previous discovery results were discarded.";
      return;
    }
    if (state.state === "failed") throw new Error(state.message);
    if (state.result.needs_login) revealLoginFields(appCard("prowlarr"));
    renderDiscoveryResults(state.result, initial);
  } catch (e) {
    status.textContent = sentence(e.message);
  } finally {
    if (jobId && finished) await apiFetch(`/api/discovery/prowlarr/${jobId}`, { method: "DELETE" }).catch(() => {});
    DISCOVERY_RUNNING = false;
    updateDiscoveryButton();
  }
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
  const retention = card.querySelector(".d-retention");
  if (retention) retention.value = cfg.retention_days ?? "";
  const body = card.querySelector(".app-card-body");
  if (body) body.classList.toggle("hidden", !cfg.enabled);
  if (destId === "gdrive") updateGdriveUI(cfg);
  if (TOKEN_DESTS[destId]) updateTokenDestUI(destId, cfg);
}

function readDestCard(destId) {
  const card = destCard(destId);
  const out = { enabled: card.querySelector(".d-enabled").checked };
  card.querySelectorAll(".d-field").forEach((input) => {
    out[input.dataset.field] = input.value.trim();
  });
  // Blank (or not a number) leaves it to the default retention.
  const retention = card.querySelector(".d-retention");
  if (retention) out.retention_days = retention.value.trim() === "" || Number.isNaN(Number(retention.value)) ? null : Number(retention.value);
  return out;
}

// Each destination's blank retention field shows the default it falls back to.
function updateRetentionPlaceholders() {
  const days = document.getElementById("s-retention_days").value || 7;
  document.querySelectorAll(".d-retention").forEach((input) => { input.placeholder = `Default (${days})`; });
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

function readRequestHeaders() {
  return Array.from(document.querySelectorAll(".request-header-row"), (row) => {
    const header = {
      name: row.querySelector(".header-name").value.trim(),
      value: row.querySelector(".header-value").value,
    };
    // No "apps" key means every app, including ones added in later releases.
    if (!row.querySelector(".header-scope-all").checked) {
      header.apps = Array.from(row.querySelectorAll(".header-scope-app:checked"), (box) => box.value);
    }
    return header;
  }).filter(({ name, value }) => name || value);
}

function updateHeaderScopeSummary(row) {
  const all = row.querySelector(".header-scope-all").checked;
  row.querySelectorAll(".header-scope-app").forEach((box) => {
    box.disabled = all;
    if (all) box.checked = true;
  });
  const labels = Array.from(row.querySelectorAll(".header-scope-app:checked"), (box) => box.dataset.label);
  row.querySelector(".header-scope summary").textContent =
    `Apps: ${all ? "All apps" : labels.join(", ") || "none selected"}`;
}

function buildHeaderScope(apps) {
  const scope = document.createElement("details");
  scope.className = "header-scope";
  const options = document.createElement("div");
  options.className = "header-scope-options";
  const option = (className, value, label, checked) => {
    const wrap = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.className = className;
    box.value = value;
    box.dataset.label = label;
    box.checked = checked;
    wrap.append(box, ` ${label}`);
    options.append(wrap);
  };
  option("header-scope-all", "", "All apps", !apps);
  // Each app's extra instances (from the Settings cards) follow it.
  APP_META.forEach((m) => {
    option("header-scope-app", m.id, m.label, !apps || apps.includes(m.id));
    instanceCards(m.id).filter((card) => card.dataset.app !== m.id).forEach((card) => {
      option("header-scope-app", card.dataset.app, card.querySelector("h3").textContent, !apps || apps.includes(card.dataset.app));
    });
  });
  scope.append(document.createElement("summary"), options);
  return scope;
}

// Rebuilt when an instance is added, renamed or removed. An instance can
// only be removed while no header is limited to it.
function refreshHeaderScopes() {
  document.querySelectorAll(".request-header-row").forEach((row) => {
    const old = row.querySelector(".header-scope");
    const apps = old.querySelector(".header-scope-all").checked
      ? undefined : Array.from(old.querySelectorAll(".header-scope-app:checked"), (box) => box.value);
    const scope = buildHeaderScope(apps);
    scope.open = old.open;
    old.replaceWith(scope);
    updateHeaderScopeSummary(row);
  });
}

// A rename only changes the instance's label in each scope, so typing a
// name doesn't rebuild every header's checkboxes.
function relabelHeaderScopes(appId, label) {
  document.querySelectorAll(`.header-scope-app[value="${appId}"]`).forEach((box) => {
    box.dataset.label = label;
    box.nextSibling.textContent = ` ${label}`;
    updateHeaderScopeSummary(box.closest(".request-header-row"));
  });
}

function addRequestHeader(header = {}) {
  const row = document.createElement("div");
  row.className = "request-header-row";
  row.innerHTML = `<label class="field"><span class="field-label">Header name</span><input class="header-name" type="text" autocomplete="off" spellcheck="false" placeholder="X-Custom-Header"></label>
    <label class="field"><span class="field-label">Value</span><input class="header-value" type="password" autocomplete="off" spellcheck="false" placeholder="Header value"></label>
    <button type="button" class="btn btn-ghost btn-sm remove-header" aria-label="Remove header">Remove</button>`;
  row.querySelector(".header-name").value = header.name || "";
  row.querySelector(".header-value").value = header.value || "";
  row.querySelector(".header-value").type = document.getElementById("show-request-headers").checked ? "text" : "password";
  row.querySelector(".remove-header").addEventListener("click", () => row.remove());
  row.append(buildHeaderScope(header.apps));
  // On the row: refreshHeaderScopes() replaces the scope element.
  row.addEventListener("change", (event) => {
    if (event.target.closest(".header-scope")) updateHeaderScopeSummary(row);
  });
  updateHeaderScopeSummary(row);
  document.getElementById("request-headers-list").append(row);
  return row;
}

function updateBasicAuthHeaderOptions() {
  const names = new Map(["Authorization", "Proxy-Authorization"].map((name) => [name.toLowerCase(), name]));
  readRequestHeaders().forEach(({ name }) => {
    if (name) names.set(name.toLowerCase(), name);
  });
  document.getElementById("basic-auth-header-options").replaceChildren(...Array.from(names.values(), (name) => {
    const option = document.createElement("option");
    option.value = name;
    return option;
  }));
}

function applyBasicAuthHeader(event) {
  event.preventDefault();
  const status = document.getElementById("basic-auth-status");
  const username = document.getElementById("basic-auth-username");
  const password = document.getElementById("basic-auth-password");
  const name = document.getElementById("basic-auth-header").value.trim();
  const fail = (message) => {
    status.className = "test-result fail";
    status.textContent = sentence(message);
  };
  if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(name)) return fail("Enter a valid HTTP header name.");
  if (["host", "content-length", "transfer-encoding", "connection", "trailer", "te", "upgrade", "proxy-connection", "expect"].includes(name.toLowerCase())) {
    return fail("This header controls HTTP transport and cannot be customized.");
  }
  if (!username.value || username.value.includes(":")) return fail("Enter a username without a colon (:).");
  if (/[\x00-\x1f\x7f]/.test(username.value + password.value)) return fail("Username and password cannot contain control characters.");
  const rows = Array.from(document.querySelectorAll(".request-header-row"));
  const matches = rows.filter((row) => row.querySelector(".header-name").value.trim().toLowerCase() === name.toLowerCase());
  // Rows scoped to different apps may share a name: fill the one left empty for this credential.
  const empty = matches.filter((row) => !row.querySelector(".header-value").value);
  if (matches.length > 1 && empty.length !== 1) {
    return fail("Several rows use this header name. Clear the value of the row to replace, or add a row with this name and no value.");
  }
  const target = empty[0] || matches[0] || rows.find((row) => !row.querySelector(".header-name").value.trim() && !row.querySelector(".header-value").value);
  const otherRows = target ? rows.filter((row) => row !== target) : rows;
  const headers = otherRows.map((row) => ({ name: row.querySelector(".header-name").value.trim(), value: row.querySelector(".header-value").value }))
    .filter(({ name: other, value: otherValue }) => other || otherValue);
  if (headers.length >= 32) return fail("At most 32 headers can be configured. Remove a header first.");
  // Encode UTF-8 before Base64; btoa alone rejects non-Latin usernames/passwords.
  const bytes = new TextEncoder().encode(`${username.value}:${password.value}`);
  if (bytes.length > 12288) return fail("These credentials exceed the request header size limit.");
  const value = "Basic " + btoa(Array.from(bytes, (byte) => String.fromCharCode(byte)).join(""));
  const totalSize = headers.reduce((size, header) => size + header.name.length + header.value.length, name.length + value.length);
  if (totalSize > 16384) return fail("Request headers exceed the 16 KiB limit. Shorten the credentials or remove a header.");
  const row = target || addRequestHeader();
  row.querySelector(".header-name").value = name;
  row.querySelector(".header-value").value = value;
  row.querySelector(".header-value").dispatchEvent(new Event("input", { bubbles: true }));
  username.value = "";
  password.value = "";
  status.className = "test-result ok";
  status.textContent = "Header applied. Save settings to keep this change.";
  updateBasicAuthHeaderOptions();
}

async function loadConfig() {
  const cfg = await apiFetch("/api/config");
  document.getElementById("s-retention_days").value = cfg.retention_days || 7;
  updateRetentionPlaceholders();
  SELF_BACKUP_ON = cfg.self_backup !== false;
  document.getElementById("s-self_backup").checked = SELF_BACKUP_ON;
  showSelfCardBody();
  // A reload keeps a token just made in view; the next page load won't have it.
  showHaState(!!cfg.ha_enabled, cfg.ha_enabled ? document.getElementById("ha-token").value : null);
  loadHttps();
  document.getElementById("s-cron_schedule").value = cfg.cron_schedule || "";
  initSchedulePicker(cfg.cron_schedule);
  document.getElementById("s-notify_url").value = cfg.notify_url || "";
  document.getElementById("s-bazarr_backup_dir").value = cfg.bazarr_backup_dir || "";
  document.getElementById("request-headers-list").replaceChildren();
  document.querySelectorAll(".instance-card").forEach((card) => card.remove());
  REMOVED_INSTANCES.clear();
  RETIRED_INSTANCES.clear();
  (cfg.removed_instances || []).forEach((appId) => RETIRED_INSTANCES.add(appId));
  rememberSavedInstances(cfg.apps || {});
  rememberEnabledApps(cfg.apps || {});
  INSTANCE_NAMES.forEach((name, appId) => addInstanceCard(appId, cfg.apps[appId]));
  (cfg.request_headers || []).forEach(addRequestHeader);
  Object.keys(cfg.apps || {}).forEach((appId) => fillAppCard(appId, cfg.apps[appId]));
  Object.keys(cfg.destinations || {}).forEach((destId) => fillDestCard(destId, cfg.destinations[destId]));
  sortAppCards();
  snapshotSettingsState();
  return cfg;
}

function rememberSavedInstances(apps) {
  INSTANCE_NAMES.clear();
  Object.keys(apps).sort().forEach((appId) => {
    if (apps[appId] && isExtraInstance(appId)) INSTANCE_NAMES.set(appId, apps[appId].name || "");
  });
}

// ---------------------------------------------------------- categories ----
// The apps are grouped by category (#148). Which sections are open is kept in
// this browser, per tab, so a page comes back the way it was left.
const LAYOUT_KEY = { settings: "backuparr.layout.settings", history: "backuparr.layout.history" };

function savedLayout(page) {
  try {
    const value = JSON.parse(localStorage.getItem(LAYOUT_KEY[page]) || "{}");
    return value && typeof value === "object" ? value : {};
  } catch (e) {
    return {}; // storage blocked or garbled: every section takes its default
  }
}

function rememberOpen(page, id, open) {
  const layout = savedLayout(page);
  layout[id] = open;
  try {
    localStorage.setItem(LAYOUT_KEY[page], JSON.stringify(layout));
  } catch (e) {
    // Private window or storage turned off: the layout just isn't kept.
  }
}

function setOpen(button, body, open) {
  button.setAttribute("aria-expanded", String(open));
  body.classList.toggle("hidden", !open);
}

function isOpen(button) {
  return button.getAttribute("aria-expanded") === "true";
}

function openCategory(section, open) {
  setOpen(section.querySelector(".category-head"), section.querySelector(".category-body"), open);
  rememberOpen("settings", section.dataset.category, open);
}

// A card filled in by discovery, or otherwise brought up, opens its category.
function revealAppCard(card) {
  const section = card.closest(".app-category");
  if (section && !isOpen(section.querySelector(".category-head"))) openCategory(section, true);
}

// Each category's apps in the order the page first lists them (Prowlarr and
// the apps it discovers first), kept to go back to if every app is disabled.
let FRESH_APP_ORDER = null;
// Whether each category had an enabled app at the last sort: the first one
// set up in a category opens it.
let CATEGORY_HAD_ENABLED = null;

// Once an app is enabled in the saved settings, each category lists Prowlarr
// first if it's there and enabled, then its enabled apps and then the rest,
// each A-Z. Runs on load and after Save only, so switching an app on never
// moves a card mid-edit. An app moves with its extra instances and "Add
// another" row, and counts as enabled if any of its cards is.
function sortAppCards() {
  const enabledTypes = new Set(Array.from(ENABLED_APPS, (appId) => instanceType(appId) || appId));
  const firstSort = !FRESH_APP_ORDER;
  if (firstSort) FRESH_APP_ORDER = {};
  const layout = savedLayout("settings");
  const hadEnabled = CATEGORY_HAD_ENABLED;
  CATEGORY_HAD_ENABLED = {};
  document.querySelectorAll(".app-category").forEach((section) => {
    const id = section.dataset.category;
    const body = section.querySelector(".category-body");
    const blocks = new Map();
    let block = null;
    Array.from(body.children).forEach((el) => {
      if (el.matches(".app-card[data-app]:not(.instance-card)")) {
        block = [];
        blocks.set(el.dataset.app, block);
      }
      if (block) block.push(el);
    });
    if (firstSort) FRESH_APP_ORDER[id] = Array.from(blocks.keys());

    const label = (type) => blocks.get(type)[0].querySelector("h3").textContent.trim();
    const byLabel = (a, b) => label(a).localeCompare(label(b), undefined, { sensitivity: "base" });
    const types = FRESH_APP_ORDER[id].filter((type) => blocks.has(type));
    const enabled = types.filter((type) => enabledTypes.has(type));
    let order = types;
    if (enabledTypes.size) {
      const pinned = enabled.includes("prowlarr") ? ["prowlarr"] : [];
      order = [...pinned, ...enabled.filter((type) => !pinned.includes(type)).sort(byLabel),
        ...types.filter((type) => !enabledTypes.has(type)).sort(byLabel)];
    }
    body.append(...order.flatMap((type) => blocks.get(type)));
    // Backuparr's own card heads its category and counts like an app (#165).
    const self = body.querySelector(".self-card") ? 1 : 0;
    const selfOn = self && SELF_BACKUP_ON ? 1 : 0;
    section.querySelector(".category-count").textContent = `${enabled.length + selfOn} of ${types.length + self} enabled`;

    // Closed on a fresh install; open once one of its apps is set up; after
    // that, as the user left it.
    const has = enabled.length + selfOn > 0;
    CATEGORY_HAD_ENABLED[id] = has;
    if (hadEnabled && has && !hadEnabled[id]) {
      openCategory(section, true);
    } else {
      setOpen(section.querySelector(".category-head"), body, id in layout ? layout[id] === true : has);
    }
  });
  refreshCategoryStatus();
}

const CATEGORY_STATUS_TEXT = {
  fail: "An app here failed its last backup",
  ok: "Every app here backed up on its last run",
  idle: "No backups yet",
};

// The header's dot: the worst last run of the category's enabled apps, so a
// failure shows while the category is closed.
async function refreshCategoryStatus() {
  let runs;
  try {
    runs = await apiFetch("/api/runlogs");
  } catch (e) {
    return; // the dots stay as they were; the Overview has the detail
  }
  // Backuparr's card has no Test connection: its dot is its last run.
  const selfRun = SELF_BACKUP_ON ? runs[SELF_APP.id] : null;
  const selfDot = document.querySelector(".self-card .status-dot");
  if (selfDot) selfDot.dataset.state = selfRun ? (selfRun.ok ? "ok" : "fail") : "idle";
  document.querySelectorAll(".app-category").forEach((section) => {
    const results = [...ENABLED_APPS, ...(SELF_BACKUP_ON ? [SELF_APP.id] : [])]
      .filter((appId) => appMeta(appId)?.category === section.dataset.category)
      .map((appId) => runs[appId])
      .filter(Boolean);
    const state = results.some((run) => !run.ok) ? "fail" : results.length ? "ok" : "idle";
    const dot = section.querySelector(".category-status");
    dot.dataset.state = state;
    dot.title = CATEGORY_STATUS_TEXT[state];
    dot.setAttribute("role", "img");
    dot.setAttribute("aria-label", CATEGORY_STATUS_TEXT[state]);
  });
}

function initCategoryEvents() {
  document.querySelectorAll(".app-category").forEach((section) => {
    const head = section.querySelector(".category-head");
    head.addEventListener("click", () => openCategory(section, !isOpen(head)));
  });
  const toggle = document.getElementById("destinations-toggle");
  const body = document.getElementById("destinations-body");
  setOpen(toggle, body, savedLayout("settings").destinations !== false);
  toggle.addEventListener("click", () => {
    const open = !isOpen(toggle);
    setOpen(toggle, body, open);
    rememberOpen("settings", "destinations", open);
  });
}

// A <select>'s apps under one labelled group per category (the Run & Status
// and Restore pickers), headed by its name alone: an <optgroup> label can't
// hold a category's icon, so none shows an emoji either (#189).
function addCategoryOptions(select, appIds) {
  APP_CATEGORIES.forEach((category) => {
    const ids = appIds.filter((appId) => appMeta(appId)?.category === category.id);
    if (!ids.length) return;
    const group = document.createElement("optgroup");
    group.label = category.label;
    ids.forEach((appId) => group.appendChild(new Option(appLabel(appId), appId)));
    select.appendChild(group);
  });
}

function rememberEnabledApps(apps) {
  ENABLED_APPS.clear();
  Object.keys(apps).forEach((appId) => {
    if (apps[appId] && apps[appId].enabled) ENABLED_APPS.add(appId);
  });
  initRunAppOptions();
}

function collectConfig() {
  const apps = {};
  document.querySelectorAll(".app-card[data-app]").forEach((card) => {
    apps[card.dataset.app] = readAppCard(card.dataset.app);
  });
  REMOVED_INSTANCES.forEach((appId) => { apps[appId] = null; }); // null removes it
  const destinations = {};
  document.querySelectorAll(".dest-card[data-dest]").forEach((card) => {
    destinations[card.dataset.dest] = readDestCard(card.dataset.dest);
  });
  return {
    retention_days: Number(document.getElementById("s-retention_days").value || 7),
    self_backup: document.getElementById("s-self_backup").checked,
    cron_schedule: document.getElementById("s-cron_schedule").value.trim(),
    notify_url: document.getElementById("s-notify_url").value.trim(),
    bazarr_backup_dir: document.getElementById("s-bazarr_backup_dir").value.trim(),
    request_headers: readRequestHeaders(),
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
    const cfg = collectConfig();
    await apiFetch("/api/config", {
      method: "POST",
      body: JSON.stringify(cfg),
    });
    REMOVED_INSTANCES.forEach((appId) => RETIRED_INSTANCES.add(appId));
    REMOVED_INSTANCES.clear();
    rememberSavedInstances(cfg.apps);
    SELF_BACKUP_ON = cfg.self_backup;
    rememberEnabledApps(cfg.apps);
    sortAppCards();
    resultEl.textContent = "Saved";
    resultEl.className = "save-result ok";
    snapshotSettingsState();
    toast("Settings saved", "ok");
  } catch (e) {
    resultEl.textContent = sentence(e.message);
    resultEl.className = "save-result fail";
    toast(`Save failed: ${clause(e.message)}`, "fail");
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
    resultEl.textContent = sentence(res.message);
    resultEl.className = `test-result ${res.ok ? "ok" : "fail"}`;
    if (dot) dot.dataset.state = res.ok ? "ok" : "fail";
    return res;
  } catch (e) {
    resultEl.textContent = sentence(e.message);
    resultEl.className = "test-result fail";
    if (dot) dot.dataset.state = "fail";
  } finally {
    btn.disabled = false;
  }
}

async function testApp(appId) {
  const card = appCard(appId);
  card.querySelector(".cert-trust")?.remove();
  const payload = readAppCard(appId);
  payload.request_headers = readRequestHeaders();
  const res = await runTest({
    btn: card.querySelector(".test-btn"),
    resultEl: card.querySelector(".test-result"),
    dot: card.querySelector(".status-dot"),
    pendingText: "Testing...",
    request: () =>
      apiFetch(`/api/test/${appId}`, {
        method: "POST",
        body: JSON.stringify(payload),
      }),
  });
  if (res?.needs_login) revealLoginFields(card);
  if (res?.card) applyCardInfo(card, res.card);
  if (res && "certificate" in res) renderCertTrust(card, appId, res);
}

// "AB:CD:..." - easier to compare with the app's own display.
function formatFingerprint(sha256) {
  return sha256.toUpperCase().match(/.{2}/g).join(":");
}

// A SHA-256 fingerprint sits in a tooltip on its "?" (#189): shown on hover,
// or on focus (a tap on a phone), and read out in full by a screen reader.
// The tooltip shows it as four rows of 8 bytes, easy to compare in chunks.
function setFingerprintTip(tip, fingerprint) {
  tip.dataset.tip = fingerprint.match(/(?:[0-9A-F]{2}:?){1,8}/gi).join("\n");
  tip.setAttribute("aria-label", `SHA-256 fingerprint ${fingerprint}`);
}

function fingerprintTip(fingerprint) {
  const tip = document.createElement("span");
  tip.className = "help-tip help-tip-fingerprint";
  tip.tabIndex = 0;
  tip.setAttribute("role", "img");
  tip.textContent = "?";
  setFingerprintTip(tip, fingerprint);
  return tip;
}

function certificateDetails(title, cert) {
  const box = document.createElement("div");
  box.className = "cert-details";
  const heading = document.createElement("strong");
  heading.textContent = title;
  box.appendChild(heading);
  const rows = [["Name", cert.subject], ["Expires", new Date(cert.not_after).toLocaleDateString()]];
  rows.forEach(([label, value]) => {
    const line = document.createElement("div");
    const name = document.createElement("span");
    name.className = "muted";
    name.textContent = `${label}: `;
    const text = document.createElement("span");
    text.textContent = value;
    line.append(name, text);
    box.appendChild(line);
  });
  const line = document.createElement("div");
  line.className = "cert-fingerprint";
  const name = document.createElement("span");
  name.className = "muted";
  name.textContent = "SHA-256";
  line.append(name, fingerprintTip(formatFingerprint(cert.sha256)));
  box.appendChild(line);
  return box;
}

// A self-signed certificate (#153): what the app presents now, the one
// trusted until then if it changed, and Trust. Trust checks it's still the
// same certificate; the card keeps it, and Save stores it.
function renderCertTrust(card, appId, res) {
  const block = document.createElement("div");
  block.className = "cert-trust";
  if (!res.certificate) {
    const note = document.createElement("p");
    note.className = "hint";
    note.textContent = "Backuparr couldn't read the certificate.";
    block.appendChild(note);
  } else {
    block.appendChild(certificateDetails(res.certificate_changed ? "New certificate" : "Certificate", res.certificate));
    if (res.trusted_certificate) block.appendChild(certificateDetails("Trusted until now", res.trusted_certificate));
    const row = document.createElement("div");
    row.className = "app-card-actions";
    const trust = document.createElement("button");
    trust.type = "button";
    trust.className = "btn btn-primary btn-sm";
    trust.textContent = "Trust";
    const result = document.createElement("span");
    result.className = "test-result";
    row.append(trust, result);
    block.appendChild(row);
    trust.addEventListener("click", async () => {
      trust.disabled = true;
      try {
        const { trusted_cert: trusted } = await apiFetch(`/api/trust-certificate/${appId}`, {
          method: "POST",
          body: JSON.stringify({ url: card.querySelector(".f-url").value.trim(), sha256: res.certificate.sha256 }),
        });
        card.dataset.trustedCert = JSON.stringify(trusted);
        await testApp(appId);
        toast("Certificate trusted. Save to keep it.", "ok");
      } catch (e) {
        result.textContent = sentence(e.message);
        result.className = "test-result fail";
        trust.disabled = false;
      }
    });
  }
  card.querySelector(".app-card-actions").after(block);
}

// A backup that stopped over the app's certificate (#153) is fixed on its
// card: these failures link there.
function isCertificateFailure(message) {
  return /certificate/i.test(message || "") && /trust (it|the new one)/i.test(message || "");
}

function goToAppCard(appId) {
  document.querySelector('.tab-btn[data-tab="settings"]').click();
  const card = appCard(appId);
  if (!card) return;
  revealAppCard(card);
  setAppCardExpanded(card, true);
  card.scrollIntoView({ behavior: "smooth", block: "center" });
  card.classList.remove("flash");
  void card.offsetWidth; // restart the highlight
  card.classList.add("flash");
  setTimeout(() => card.classList.remove("flash"), 1700);
}

function cardLink(appId) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "link-btn card-link";
  btn.textContent = "Check settings";
  btn.addEventListener("click", (event) => {
    event.stopPropagation();
    goToAppCard(appId);
  });
  return btn;
}

async function testDestination(destId) {
  const card = destCard(destId);
  const payload = readDestCard(destId);
  // Pasted but not yet connected: lets the server say "click Connect first".
  const pasteBox = document.getElementById(`${destId}-token-input`);
  if (pasteBox && pasteBox.value.trim()) payload.token_pasted = true;
  await runTest({
    btn: card.querySelector(".test-dest-btn"),
    resultEl: card.querySelector(".test-result"),
    dot: card.querySelector(".status-dot"),
    pendingText: "Testing...",
    request: () =>
      whileStorageWakes(destId, apiFetch(`/api/test-destination/${destId}`, {
        method: "POST",
        body: JSON.stringify(payload),
      }), () => {
        card.querySelector(".test-result").textContent = STORAGE_WAIT_TEXT;
      }),
  });
}

async function testNotifyUrl() {
  const btn = document.getElementById("notify-test-btn");
  const resultEl = document.getElementById("notify-test-result");
  const notifyUrl = document.getElementById("s-notify_url").value.trim();
  if (!notifyUrl) {
    resultEl.textContent = "Enter a Notify URL first.";
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
      body: JSON.stringify(collectConfig()),
    });
    snapshotSettingsState();
    window.location.href = "/api/destinations/gdrive/oauth/start";
  } catch (e) {
    resultEl.textContent = sentence(e.message);
    resultEl.className = "save-result fail";
    toast(`Could not save before connecting: ${clause(e.message)}`, "fail");
  }
}

// Google's script is loaded only to choose a Drive folder, not on every page.
let GOOGLE_API_SCRIPT = null;

function loadGoogleApiScript() {
  if (!GOOGLE_API_SCRIPT) {
    GOOGLE_API_SCRIPT = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "https://apis.google.com/js/api.js";
      script.onload = resolve;
      script.onerror = () => {
        GOOGLE_API_SCRIPT = null; // try again next time
        script.remove();
        reject(new Error("Google API script did not load"));
      };
      document.head.appendChild(script);
    });
  }
  return GOOGLE_API_SCRIPT;
}

async function loadGooglePickerApi() {
  if (window.google && window.google.picker) return;
  await loadGoogleApiScript();
  await new Promise((resolve, reject) => {
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
    toast(`Could not open the folder picker: ${clause(e.message)}`, "fail");
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
      body: JSON.stringify({ folder_id: folder.id, folder_name: folder.name }),
    });
    toast(`Backing up to "${folder.name}"`, "ok");
    await loadConfig();
  } catch (e) {
    toast(`Could not save the selected folder: ${clause(e.message)}`, "fail");
  }
}

async function disconnectGdrive() {
  if (!confirm("Disconnect Google Drive? Existing backups already there are left untouched.")) return;
  try {
    await apiFetch("/api/destinations/gdrive/disconnect", { method: "POST" });
    toast("Google Drive disconnected", "ok");
    await loadConfig();
  } catch (e) {
    toast(`Could not disconnect: ${clause(e.message)}`, "fail");
  }
}

// ------------------------------------------- token-paste destinations ----
// OneDrive and Dropbox both connect by pasting what `rclone authorize <id>`
// prints; ids match the element ids in index.html and /api/destinations/<id>/.
const TOKEN_DESTS = {
  onedrive: { label: "OneDrive", connected: "Connected - backing up to your OneDrive app folder (Apps/Backuparr)." },
  dropbox: { label: "Dropbox", connected: "Connected - backing up to the Backuparr folder in your Dropbox." },
};

function updateTokenDestUI(destId, cfg) {
  const statusEl = document.getElementById(`${destId}-status`);
  const connectBtn = document.getElementById(`${destId}-connect-btn`);
  const disconnectBtn = document.getElementById(`${destId}-disconnect-btn`);
  const pasteField = document.getElementById(`${destId}-paste-field`);
  if (!statusEl) return;
  const connected = !!cfg.token;
  connectBtn.classList.toggle("hidden", connected);
  disconnectBtn.classList.toggle("hidden", !connected);
  if (pasteField) pasteField.classList.toggle("hidden", connected);
  statusEl.textContent = connected ? TOKEN_DESTS[destId].connected : "Not connected yet.";
}

async function connectTokenDest(destId) {
  const { label } = TOKEN_DESTS[destId];
  const input = document.getElementById(`${destId}-token-input`);
  const btn = document.getElementById(`${destId}-connect-btn`);
  const tokenBlob = input.value.trim();
  if (!tokenBlob) {
    toast(`Paste the token \`rclone authorize ${destId}\` printed first.`, "fail");
    return;
  }
  btn.disabled = true;
  try {
    await apiFetch(`/api/destinations/${destId}/connect`, {
      method: "POST",
      body: JSON.stringify({ token_blob: tokenBlob }),
    });
    input.value = "";
    toast(`${label} connected`, "ok");
    await loadConfig();
  } catch (e) {
    toast(`Could not connect: ${clause(e.message)}`, "fail");
  } finally {
    btn.disabled = false;
  }
}

async function disconnectTokenDest(destId) {
  const { label } = TOKEN_DESTS[destId];
  if (!confirm(`Disconnect ${label}? Existing backups already there are left untouched.`)) return;
  try {
    await apiFetch(`/api/destinations/${destId}/disconnect`, { method: "POST" });
    toast(`${label} disconnected`, "ok");
    await loadConfig();
  } catch (e) {
    toast(`Could not disconnect: ${clause(e.message)}`, "fail");
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
      toast("Couldn't copy automatically - the text is selected, so copy it by hand.", "fail");
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

// Also run for each extra instance's card, a copy made after startup.
function bindAppCard(card) {
  card.querySelectorAll('.f-url, .f-api_key, .f-extra[data-field="username"], .f-extra[data-field="password"]').forEach((input) => {
    input.addEventListener("input", () => {
      const appId = card.dataset.app;
      if (appId === "plex" && PLEX_LOGIN) stopPlexLogin("Plex details changed. Sign in again to get a token.");
      APP_CREDENTIAL_EDIT_REVISION.set(appId, (APP_CREDENTIAL_EDIT_REVISION.get(appId) || 0) + 1);
      const refresh = APP_DISCOVERY_UPDATES.get(appId);
      if (refresh) refresh();
      pruneNewInstanceChoices();
      card.querySelector(".discovery-origin")?.classList.add("hidden");
      card.querySelector(".discovery-key-status")?.classList.add("hidden");
    });
  });
  card.querySelector(".test-btn")?.addEventListener("click", () => testApp(card.dataset.app));
  card.querySelectorAll("select.f-extra").forEach((select) => select.addEventListener("change", () => showInstallOptions(card)));
  card.querySelector(".f-enabled").addEventListener("change", (event) => setAppCardExpanded(card, event.target.checked));
  card.querySelector(".f-name")?.addEventListener("input", () => {
    updateInstanceCardLabel(card);
    relabelHeaderScopes(card.dataset.app, card.querySelector("h3").textContent);
  });
  card.querySelector(".remove-instance")?.addEventListener("click", () => removeInstanceCard(card));
}

function initSettingsEvents() {
  document.getElementById("save-btn").addEventListener("click", saveSettings);
  initCategoryEvents();
  document.getElementById("plex-token-btn").addEventListener("click", startPlexLogin);
  document.getElementById("plex-auth-cancel").addEventListener("click", () => stopPlexLogin("Plex sign-in cancelled."));
  document.getElementById("basic-auth-form").addEventListener("submit", applyBasicAuthHeader);
  document.getElementById("basic-auth-helper").addEventListener("toggle", updateBasicAuthHeaderOptions);
  document.getElementById("basic-auth-header").addEventListener("focus", updateBasicAuthHeaderOptions);
  document.getElementById("basic-auth-form").addEventListener("input", () => {
    document.getElementById("basic-auth-status").textContent = "";
  });
  document.getElementById("add-request-header").addEventListener("click", () => addRequestHeader().querySelector(".header-name").focus());
  document.getElementById("show-request-headers").addEventListener("change", (event) => {
    document.querySelectorAll(".header-value").forEach((input) => { input.type = event.target.checked ? "text" : "password"; });
  });
  document.getElementById("discover-btn").addEventListener("click", () => discoverServices());
  appCard("prowlarr").querySelectorAll(".f-url, .f-api_key").forEach((input) => {
    input.addEventListener("input", updateDiscoveryButton);
  });
  document.querySelectorAll(".app-card[data-app]").forEach(bindAppCard);
  document.getElementById("s-self_backup").addEventListener("change", showSelfCardBody);
  initHaEvents();
  initHttpsEvents();
  initInstanceAddRows();
  document.getElementById("notify-test-btn").addEventListener("click", testNotifyUrl);
  initScheduleEvents();

  document.querySelectorAll(".test-dest-btn").forEach((btn) => {
    btn.addEventListener("click", () => testDestination(btn.closest(".dest-card").dataset.dest));
  });
  document.getElementById("s-retention_days").addEventListener("input", updateRetentionPlaceholders);
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

  Object.keys(TOKEN_DESTS).forEach((destId) => {
    const connectBtn = document.getElementById(`${destId}-connect-btn`);
    if (connectBtn) connectBtn.addEventListener("click", () => connectTokenDest(destId));
    const disconnectBtn = document.getElementById(`${destId}-disconnect-btn`);
    if (disconnectBtn) disconnectBtn.addEventListener("click", () => disconnectTokenDest(destId));
  });
}

// -------------------------------------------------------------- run tab ----
function renderRunState(state) {
  const summary = document.getElementById("run-summary");
  const logView = document.getElementById("run-log");
  const runBtn = document.getElementById("run-btn");
  const cancelBtn = document.getElementById("cancel-run-btn");
  const progress = document.getElementById("run-progress");
  const progressBar = document.getElementById("run-progress-bar");

  showScheduleWarning("run-schedule-warning", state.schedule_valid);
  runBtn.disabled = !!state.running;
  document.getElementById("run-app").disabled = !!state.running;
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
      addPart(null, `- backing up ${appLabel(state.current_app)} (${state.current_index} of ${state.total_apps})`);
      const spinner = document.createElement("span");
      spinner.className = "spinner";
      summary.appendChild(spinner);
    }
  } else if (state.finished_at) {
    addPart(null, `Last run finished ${fmtTime(state.finished_at)}`);
    addPart("OK:", (state.ok || []).map(appLabel).join(", ") || "none");
    if ((state.failed || []).length) {
      addPart("Failed:", state.failed.map(namedFailure).join("; "), true);
      state.failed.filter(isCertificateFailure).forEach((entry) => summary.appendChild(cardLink(entry.split(": ")[0])));
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
// then - if it's running and no poll is already active - keep polling,
// rendering each tick, until the status reports not-running. The next tick is
// only scheduled once one has rendered, so a slow response can't overlap or
// land out of order; a failed tick (the container restarting) is reported
// and retried.
async function pollWhileRunning({ getTimer, setTimer, fetchStatus, render, intervalMs, onStopped, onError }) {
  try {
    const state = await fetchStatus();
    render(state);
    if (!state.running || getTimer()) return;
  } catch (e) {
    if (onError) onError(e);
    return;
  }
  const tick = async () => {
    try {
      const s = await fetchStatus();
      render(s);
      if (!s.running) {
        setTimer(null);
        if (onStopped) onStopped(s);
        return;
      }
    } catch (e) {
      if (onError) onError(e);
    }
    setTimer(setTimeout(tick, intervalMs));
  };
  setTimer(setTimeout(tick, intervalMs));
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
      document.getElementById("run-log").textContent = `Error loading status: ${clause(e.message)}`;
    },
  });
}

// "All apps", then every enabled app with its saved instances after it, and
// Backuparr itself while its card is on.
function initRunAppOptions() {
  const select = document.getElementById("run-app");
  const previous = select.value;
  select.replaceChildren(new Option("All apps", ""));
  addCategoryOptions(select, [...(SELF_BACKUP_ON ? [SELF_APP.id] : []), ...APP_META.flatMap((m) =>
    [m.id, ...Array.from(INSTANCE_NAMES.keys()).filter((appId) => instanceType(appId) === m.id)])
    .filter((appId) => ENABLED_APPS.has(appId))]);
  if (Array.from(select.options).some((opt) => opt.value === previous)) select.value = previous;
}

async function runBackupNow() {
  const appId = document.getElementById("run-app").value;
  try {
    await apiFetch("/api/backup/run", { method: "POST", body: JSON.stringify(appId ? { app: appId } : {}) });
    toast(appId ? `Backup of ${appLabel(appId)} started` : "Backup started", "ok");
    refreshRunStatus();
  } catch (e) {
    toast(`Could not start backup: ${clause(e.message)}`, "fail");
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
    toast(`Could not cancel: ${clause(e.message)}`, "fail");
  }
}

function initRunEvents() {
  document.getElementById("run-btn").addEventListener("click", runBackupNow);
  document.getElementById("cancel-run-btn").addEventListener("click", cancelBackupRun);
}

// ---------------------------------------------------------- history tab ----
// An app id, or an extra instance's "<app>-<suffix>" id (see config_store.py).
function instanceType(appId) {
  if (APP_META.some((m) => m.id === appId)) return appId;
  const match = /^([a-z]+)-[a-z0-9]+(?:-[a-z0-9]+)*$/.exec(appId || "");
  return match && APP_META.some((m) => m.id === match[1] && m.multi_instance) ? match[1] : null;
}

function isExtraInstance(appId) {
  const type = instanceType(appId);
  return !!type && type !== appId;
}

function appMeta(appId) {
  if (appId === SELF_APP.id) return SELF_APP;
  const type = instanceType(appId);
  return APP_META.find((m) => m.id === type);
}

// "Radarr (4K)" for an extra instance; a removed one's id stands in for its name.
// A run's failure is logged as "<app id>: why" ("radarr-4k: ..."); the page
// names the app as its card does: "Radarr (4K): why" (#191).
function namedFailure(entry) {
  const [appId, ...why] = entry.split(": ");
  return why.length && appMeta(appId) ? `${appLabel(appId)}: ${why.join(": ")}` : entry;
}

function appLabel(appId) {
  if (appId === "backuparr") return "Backuparr";
  const m = appMeta(appId);
  if (!m) return appId;
  return isExtraInstance(appId) ? `${m.label} (${INSTANCE_NAMES.get(appId) || appId.slice(m.id.length + 1)})` : m.label;
}

// A run that left no file on this destination: greyed, "Backup failed" in
// place of a version and size, and its log, which says why.
function missingHistoryRow(appId, e) {
  const tr = document.createElement("tr");
  tr.className = "history-missing";
  const cells = [e.name || "No backup file", "Backup failed", fmtTime(e.mod_time)].map((text) => {
    const td = document.createElement("td");
    td.textContent = text;
    return td;
  });
  cells[1].className = "history-reason";
  cells[1].colSpan = 2;
  const actionsTd = document.createElement("td");
  actionsTd.className = "actions-cell";
  actionsTd.appendChild(viewLogButton(appId, e.run_id));
  tr.append(...cells, actionsTd);
  return tr;
}

async function deleteBackup(destId, appId, filename, row) {
  if (!confirm(`Delete ${filename}? This can't be undone.`)) return;
  try {
    await whileStorageWakes(destId, apiFetch(`/api/history/${destId}/${appId}/${encodeURIComponent(filename)}`, { method: "DELETE" }),
      () => toast(STORAGE_WAIT_TEXT));
    row.remove();
    toast(`Deleted ${filename}`, "ok");
  } catch (e) {
    toast(`Delete failed: ${clause(e.message)}`, "fail");
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
  document.getElementById("history-select-btn").addEventListener("click", () =>
    setHistorySelecting(!document.getElementById("history-root").classList.contains("selecting")));
  document.getElementById("history-bulk-download").addEventListener("click", downloadPicked);
  document.getElementById("history-bulk-delete").addEventListener("click", deletePicked);
  document.getElementById("history-bulk-cancel").addEventListener("click", () => setHistorySelecting(false));
}

async function loadHistory() {
  const root = document.getElementById("history-root");
  const destId = document.getElementById("h-destination").value;
  clearHistorySelection();
  root.textContent = "Loading...";
  if (!destId) {
    root.innerHTML = '<p class="empty-note">No destinations enabled - turn one on in Settings first.</p>';
    return;
  }
  try {
    const history = await whileStorageWakes(destId, apiFetch(`/api/history/${destId}`), () => {
      root.textContent = STORAGE_WAIT_TEXT;
    });
    renderHistory(root, destId, history);
  } catch (e) {
    root.innerHTML = "";
    const p = document.createElement("p");
    p.className = "empty-note";
    p.textContent = `Error loading history: ${clause(e.message)}`;
    root.appendChild(p);
  }
}

// Each category's apps under its own collapsible header (#148), open unless
// closed here before; Backuparr's own backups head Server (#165).
function renderHistory(root, destId, history) {
  root.innerHTML = "";
  const appIds = Object.keys(history).filter((appId) => (history[appId] || []).length > 0);
  if (!appIds.length) {
    root.innerHTML = '<p class="empty-note">No backups yet on this destination - run a backup first.</p>';
    return;
  }
  const layout = savedLayout("history");
  APP_CATEGORIES.forEach((category) => {
    const ids = appIds.filter((appId) => appMeta(appId)?.category === category.id);
    if (!ids.length) return;
    const group = document.createElement("section");
    group.className = "history-category";
    const head = categoryHead(category, `history-category-${category.id}`);
    const body = document.createElement("div");
    body.className = "category-body";
    body.id = `history-category-${category.id}`;
    ids.sort((a, b) => (b === SELF_APP.id) - (a === SELF_APP.id));
    ids.forEach((appId) => body.appendChild(historyAppSection(destId, appId, history[appId])));
    setOpen(head, body, layout[category.id] !== false);
    head.addEventListener("click", () => {
      const open = !isOpen(head);
      setOpen(head, body, open);
      rememberOpen("history", category.id, open);
    });
    group.append(head, body);
    root.appendChild(group);
  });
}

function categoryHead(category, controls) {
  const head = document.createElement("button");
  head.type = "button";
  head.className = "category-head";
  head.setAttribute("aria-controls", controls);
  const emoji = document.createElement("span");
  emoji.setAttribute("aria-hidden", "true");
  if (category.icon) {
    emoji.className = `category-icon category-icon-${category.icon}`;
  } else {
    emoji.className = "category-emoji";
    emoji.textContent = category.emoji;
  }
  const name = document.createElement("span");
  name.className = "category-name";
  name.textContent = category.label;
  const chevron = document.createElement("span");
  chevron.className = "category-chevron";
  chevron.setAttribute("aria-hidden", "true");
  head.append(emoji, name, chevron);
  return head;
}

function historyAppSection(destId, appId, entries) {
  const section = document.createElement("div");
  section.className = "history-app";
  const heading = document.createElement("h3");
  const all = document.createElement("input");
  all.type = "checkbox";
  all.className = "history-select history-select-all";
  all.setAttribute("aria-label", `Select every ${appLabel(appId)} backup`);
  all.addEventListener("change", () => {
    section.querySelectorAll("tbody .history-select").forEach((box) => {
      box.checked = all.checked;
      setHistoryPicked(box, all.checked);
    });
    updateHistoryBulkBar();
  });
  heading.append(all, appLabel(appId));
  section.appendChild(heading);

  const table = document.createElement("table");
  table.className = "history-table";
  table.innerHTML = "<thead><tr><th>File</th><th>Version</th><th>Size</th><th>Modified</th><th></th></tr></thead>";
  const tbody = document.createElement("tbody");
  entries.forEach((e) => {
    if (e.missing) {
      const row = missingHistoryRow(appId, e);
      if (e.run_id) row.cells[0].prepend(historyCheckbox({ kind: "run", app: appId, run_id: e.run_id }, e.name || "this failed run"));
      tbody.appendChild(row);
      return;
    }
    const tr = document.createElement("tr");
    const nameTd = document.createElement("td");
    const nameText = document.createElement("span");
    nameText.className = "history-file";
    nameText.textContent = e.name;
    nameTd.append(historyCheckbox({ kind: "backup", app: appId, file: e.name, encrypted: e.encrypted }, e.name),
      nameText, encryptionBadge(e.encrypted));
    const versionTd = document.createElement("td");
    versionTd.textContent = e.app_version || "-";
    if (!e.app_version) {
      versionTd.title = e.recorded ? "This backup doesn't record the app's version" : "Not checked yet";
    }
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
    dlLink.href = `/api/history/${encodeURIComponent(destId)}/${encodeURIComponent(appId)}/${encodeURIComponent(e.name)}/download`;
    warnWhenDownloadIsDecrypted(dlLink, appId, e);
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "icon-btn";
    delBtn.title = "Delete this backup";
    delBtn.textContent = "×";
    delBtn.addEventListener("click", () => deleteBackup(destId, appId, e.name, tr));
    if (e.run_id) actionsTd.append(viewLogButton(appId, e.run_id));
    actionsTd.append(dlLink, delBtn);
    tr.append(nameTd, versionTd, sizeTd, timeTd, actionsTd);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  section.appendChild(table);
  return section;
}

// ------------------------------------------------- picking several (#148) ----
// Select shows a box on every row; the bar at the bottom downloads or deletes
// what's picked. Keyed so a row picked stays picked until it's gone.
const HISTORY_PICKED = new Map();

function historyKey(item) {
  return item.kind === "run" ? `run|${item.app}|${item.run_id}` : `backup|${item.app}|${item.file}`;
}

function historyCheckbox(item, name) {
  const box = document.createElement("input");
  box.type = "checkbox";
  box.className = "history-select";
  box.setAttribute("aria-label", `Select ${name}`);
  box.dataset.item = JSON.stringify(item);
  box.addEventListener("change", () => {
    setHistoryPicked(box, box.checked);
    updateHistoryBulkBar();
  });
  return box;
}

function setHistoryPicked(box, picked) {
  const item = JSON.parse(box.dataset.item);
  if (picked) HISTORY_PICKED.set(historyKey(item), item);
  else HISTORY_PICKED.delete(historyKey(item));
}

function setHistorySelecting(on) {
  document.getElementById("history-root").classList.toggle("selecting", on);
  document.getElementById("history-bulk-bar").classList.toggle("hidden", !on);
  document.getElementById("history-select-btn").setAttribute("aria-pressed", String(on));
  if (!on) clearHistorySelection();
  updateHistoryBulkBar();
}

function clearHistorySelection() {
  HISTORY_PICKED.clear();
  document.querySelectorAll("#history-root .history-select").forEach((box) => {
    box.checked = false;
    box.indeterminate = false;
  });
  updateHistoryBulkBar();
}

function updateHistoryBulkBar() {
  showHistoryBulkError(null);
  const items = Array.from(HISTORY_PICKED.values());
  const backups = items.filter((item) => item.kind === "backup");
  document.getElementById("history-bulk-count").textContent = `${items.length} selected`;
  document.getElementById("history-bulk-download").disabled = !backups.length;
  document.getElementById("history-bulk-delete").disabled = !items.length;
  // Each app's "every backup" box: ticked, empty or in between.
  document.querySelectorAll("#history-root .history-app").forEach((section) => {
    const boxes = Array.from(section.querySelectorAll("tbody .history-select"));
    const all = section.querySelector(".history-select-all");
    const ticked = boxes.filter((box) => box.checked).length;
    all.checked = boxes.length > 0 && ticked === boxes.length;
    all.indeterminate = ticked > 0 && ticked < boxes.length;
  });
}

// While several backups are deleted or prepared for download (#164): the
// bar shows a spinner and a count, and nothing in History can change the
// selection meanwhile.
let HISTORY_BUSY = false;

function setHistoryBusy(text) {
  HISTORY_BUSY = !!text;
  const count = document.getElementById("history-bulk-count");
  if (text) {
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    spinner.setAttribute("aria-hidden", "true");
    count.replaceChildren(spinner, document.createTextNode(` ${text}`));
  }
  ["history-bulk-download", "history-bulk-delete", "history-bulk-cancel", "history-select-btn", "h-destination"]
    .forEach((id) => { document.getElementById(id).disabled = HISTORY_BUSY; });
  document.querySelectorAll("#history-root .history-select").forEach((box) => { box.disabled = HISTORY_BUSY; });
  if (!text) updateHistoryBulkBar();
}

function downloadUrl(url) {
  const link = document.createElement("a");
  link.href = url;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function downloadPicked() {
  const destId = document.getElementById("h-destination").value;
  const backups = Array.from(HISTORY_PICKED.values()).filter((item) => item.kind === "backup");
  if (!backups.length || HISTORY_BUSY) return;
  if (backups.some((b) => b.encrypted && b.app !== "backuparr")) toast(DECRYPTED_DOWNLOAD_TEXT, "warn");
  const dest = encodeURIComponent(destId);
  if (backups.length === 1) {
    downloadUrl(`/api/history/${dest}/${encodeURIComponent(backups[0].app)}/${encodeURIComponent(backups[0].file)}/download`);
    return;
  }
  // Several come as one zip, built on the server first.
  setHistoryBusy(`Preparing 0 of ${backups.length}...`);
  try {
    const { job_id: jobId } = await apiFetch(`/api/history/${dest}/download`, {
      method: "POST", body: JSON.stringify({ backups: backups.map(({ app, file }) => ({ app, file })) }),
    });
    const job = `/api/history/${dest}/download/${encodeURIComponent(jobId)}`;
    for (;;) {
      await sleep(1000);
      const status = await apiFetch(job);
      if (status.state === "failed") throw new Error(status.message);
      setHistoryBusy(`Preparing ${status.done} of ${status.total}...`);
      if (status.state === "ready") {
        downloadUrl(`${job}/file`);
        break;
      }
    }
  } catch (e) {
    toast(`Download failed: ${clause(e.message)}`, "fail");
  } finally {
    setHistoryBusy(null);
  }
}

function countText(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

async function deletePicked() {
  const destId = document.getElementById("h-destination").value;
  const items = Array.from(HISTORY_PICKED.values());
  const backups = items.filter((item) => item.kind === "backup");
  const runs = items.filter((item) => item.kind === "run");
  const what = [backups.length && countText(backups.length, "backup", "backups"),
    runs.length && countText(runs.length, "failed run", "failed runs")].filter(Boolean).join(" and ");
  if (!what || HISTORY_BUSY || !confirm(`Delete ${what} from ${destLabel(destId)}? This can't be undone.`)) return;
  // One per request, so the count goes up by one; each is one delete either way.
  const failed = [];
  let deleted = 0;
  for (const [index, item] of items.entries()) {
    setHistoryBusy(`Deleting ${index + 1} of ${items.length}...`);
    const body = item.kind === "backup"
      ? { backups: [{ app: item.app, file: item.file }], runs: [] }
      : { backups: [], runs: [{ app: item.app, run_id: item.run_id }] };
    try {
      const request = apiFetch(`/api/history/${destId}/delete`, { method: "POST", body: JSON.stringify(body) });
      const result = await (index === 0 ? whileStorageWakes(destId, request, () => toast(STORAGE_WAIT_TEXT)) : request);
      deleted += result.deleted.length;
      result.failed.forEach((f) => failed.push({ item, error: f.error }));
    } catch (e) {
      failed.push({ item, error: e.message });
    }
  }
  setHistoryBusy(null);
  await loadHistory();
  if (!failed.length) {
    toast(`Deleted ${countText(deleted, "item", "items")}.`, "ok");
    setHistorySelecting(false);
    return;
  }
  // The ones that couldn't be deleted stay picked (if still listed), with why, in the bar.
  const keep = new Set(failed.map(({ item }) => historyKey(item)));
  HISTORY_PICKED.clear();
  document.querySelectorAll("#history-root tbody .history-select").forEach((box) => {
    const item = JSON.parse(box.dataset.item);
    box.checked = keep.has(historyKey(item));
    if (box.checked) HISTORY_PICKED.set(historyKey(item), item);
  });
  updateHistoryBulkBar();
  document.getElementById("history-bulk-count").textContent =
    `Deleted ${deleted}; ${failed.length} couldn't be deleted`;
  showHistoryBulkError(clause(failed[0].error));
}

function showHistoryBulkError(text) {
  const error = document.getElementById("history-bulk-error");
  error.textContent = text || "";
  error.classList.toggle("hidden", !text);
}

// ---------------------------------------------------------- restore tab ----
let RESTORE_STATE = { app: null, file: null, servers: [] };

function initRestoreAppOptions() {
  const select = document.getElementById("r-app");
  const previous = select.value;
  select.replaceChildren();
  // restore_supported defaults to true; an app restored by hand (manual_restore,
  // #151) is listed for its download and steps. Saved extra instances follow their app.
  addCategoryOptions(select, APP_META.filter((m) => m.restore_supported !== false || m.manual_restore).flatMap((m) =>
    [m.id, ...Array.from(INSTANCE_NAMES.keys()).filter((appId) => instanceType(appId) === m.id)]));
  if (Array.from(select.options).some((opt) => opt.value === previous)) select.value = previous;
}

// <label class="field">: a caption, a muted "(hint)" if any, and an input
// with the given properties. Text goes in as text, never as markup.
function overrideField(caption, hint, inputProps) {
  const label = document.createElement("label");
  label.className = "field";
  const text = document.createElement("span");
  text.className = "field-label";
  text.textContent = caption;
  if (hint) {
    const muted = document.createElement("span");
    muted.className = "muted";
    muted.textContent = `(${hint})`;
    text.append(" ", muted);
  }
  const input = Object.assign(document.createElement("input"), { autocomplete: "off" }, inputProps);
  label.append(text, input);
  return label;
}

function renderRestoreOverride() {
  const appId = document.getElementById("r-app").value;
  const meta = appMeta(appId);
  const root = document.getElementById("r-override");
  root.innerHTML = "";
  if (!meta) return;

  const block = document.createElement("div");
  block.className = "extra-field-block";

  block.appendChild(overrideField("URL", "", { type: "text", id: "r-ovr-url", placeholder: meta.url_placeholder || "" }));
  if (!meta.no_api_key) {
    const hint = meta.key_required ? meta.key_help : `optional${meta.key_help ? ` \u2014 ${meta.key_help}` : ""}`;
    block.appendChild(overrideField(meta.key_label || "API key", hint, { type: "password", id: "r-ovr-api_key" }));
  }
  // A restore never needs the app's web login (login_only fields).
  (meta.extra_fields || []).filter((f) => !f.login_only && !f.option).forEach((f) => {
    const field = overrideField(f.label, f.help, { type: f.type, className: "r-ovr-extra", placeholder: f.placeholder || "" });
    field.querySelector("input").dataset.field = f.name;
    block.appendChild(field);
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
    api_key: document.getElementById("r-ovr-api_key")?.value.trim() || "",
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
  showRestoreKeyField(false);
  const manual = Boolean(appMeta(appId)?.manual_restore);
  document.getElementById("r-auto").classList.toggle("hidden", manual);
  document.getElementById("r-manual").classList.toggle("hidden", !manual);
  document.getElementById("r-manual").replaceChildren();
  if (!destId) {
    fileSelect.innerHTML = "<option>No destinations enabled</option>";
    if (manual) renderManualRestore();
    return;
  }
  try {
    const res = await whileStorageWakes(destId, apiFetch(`/api/restore/${destId}/${appId}/backups`), () => {
      fileSelect.options[0].textContent = STORAGE_WAIT_TEXT;
    });
    fileSelect.innerHTML = "";
    if (!res.files.length) {
      fileSelect.innerHTML = '<option value="">No backups found</option>';
      setRestoreReady(false);
      if (manual) renderManualRestore();
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
    toast(sentence(e.message), "fail");
  }
}

// Restore stays disabled until the backup's version has been checked
// against the installed app - first, while the app is still running, so
// Emby, Plex, Seerr, LazyLibrarian and Vaultwarden are only asked to stop
// after it.
let restoreCheckSeq = 0;

function setRestoreReady(ready) {
  document.getElementById("restore-btn").disabled = !ready;
}

// Asked for only when a backup turns out to be made with another key (#147).
function showRestoreKeyField(show) {
  const field = document.getElementById("r-key-field");
  field.classList.toggle("hidden", !show);
  if (!show) document.getElementById("r-key").value = "";
}

function restoreKey() {
  return document.getElementById("r-key").value.trim();
}

// An app Backuparr can't restore itself (#151): the picked backup's download
// (decrypted, like History's) and the steps for putting it back by hand.
function renderManualRestore() {
  const destId = document.getElementById("r-destination").value;
  const appId = document.getElementById("r-app").value;
  const file = document.getElementById("r-file").value;
  const meta = appMeta(appId);
  const root = document.getElementById("r-manual");
  root.replaceChildren();
  const intro = document.createElement("p");
  intro.className = "hint";
  intro.textContent = `Backuparr can't restore ${meta.label} itself. Download the backup and follow these steps.`;
  root.appendChild(intro);
  if (destId && file) {
    const row = document.createElement("div");
    row.className = "save-row";
    const link = document.createElement("a");
    link.className = "btn btn-primary";
    link.href = `/api/history/${encodeURIComponent(destId)}/${encodeURIComponent(appId)}/${encodeURIComponent(file)}/download`;
    link.textContent = "Download";
    row.appendChild(link);
    root.appendChild(row);
    if (file.endsWith(".enc")) {
      const note = document.createElement("p");
      note.className = "hint";
      note.textContent = DECRYPTED_DOWNLOAD_TEXT;
      root.appendChild(note);
    }
  }
  meta.manual_restore.steps.forEach(({ title, steps }) => {
    const heading = document.createElement("h3");
    heading.className = "restore-steps-title";
    heading.textContent = title;
    const list = document.createElement("ol");
    list.className = "restore-steps";
    steps.forEach((step) => {
      const item = document.createElement("li");
      item.textContent = step;
      list.appendChild(item);
    });
    root.append(heading, list);
  });
  const docs = document.createElement("a");
  docs.href = meta.manual_restore.docs;
  docs.target = "_blank";
  docs.rel = "noopener noreferrer";
  docs.textContent = "More in the docs";
  const more = document.createElement("p");
  more.appendChild(docs);
  root.appendChild(more);
}

async function renderRestoreExtra() {
  const destId = document.getElementById("r-destination").value;
  const appId = document.getElementById("r-app").value;
  if (appMeta(appId)?.manual_restore) {
    renderManualRestore();
    return;
  }
  const file = document.getElementById("r-file").value;
  const extraRoot = document.getElementById("r-extra");
  extraRoot.innerHTML = "";
  setRestoreReady(false);
  if (!destId || !file) return;

  const block = document.createElement("div");
  block.className = "extra-field-block restore-version";
  block.textContent = "Checking the backup's version against the installed one...";
  extraRoot.appendChild(block);
  const seq = ++restoreCheckSeq;
  const body = { file };
  const override = readRestoreOverride();
  if (override) body.override = override;
  let result;
  try {
    result = await whileStorageWakes(destId, apiFetch(`/api/restore/${destId}/${appId}/check`, {
      method: "POST", body: JSON.stringify(body),
    }), () => {
      if (seq === restoreCheckSeq) block.textContent = STORAGE_WAIT_TEXT;
    });
  } catch (e) {
    if (seq !== restoreCheckSeq) return;
    showRestoreCheckFailure(block, e.message);
    return;
  }
  if (seq !== restoreCheckSeq) return;
  if (result.status === "refused") {
    showRestoreCheckFailure(block, result.message);
    return;
  }
  block.textContent = sentence(result.message);
  block.classList.add(result.status === "match" ? "ok" : "warn");
  renderRestoreDetails(appId, extraRoot);
  setRestoreReady(true);
}

function showRestoreCheckFailure(block, message) {
  block.textContent = "";
  block.classList.add("fail");
  const again = document.createElement("button");
  again.type = "button";
  again.className = "btn btn-ghost btn-sm";
  again.textContent = "Check again";
  again.addEventListener("click", renderRestoreExtra);
  block.append(sentence(message), " ", again);
}

function renderRestoreDetails(appId, extraRoot) {
  const warning = appMeta(appId)?.restore_warning;
  if (warning) {
    const block = document.createElement("div");
    block.className = "extra-field-block";
    const lead = document.createElement("strong");
    if (warning.danger) lead.className = "danger-text";
    lead.textContent = warning.lead;
    block.append(lead, ` ${warning.text}`);
    extraRoot.appendChild(block);
  }
  if (appId === "bazarr") {
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
    const body = { file: fileSelect.value };
    if (restoreKey()) body.key = restoreKey();
    const res = await whileStorageWakes(destId, apiFetch(`/api/restore/${destId}/sabnzbd/preview`, {
      method: "POST",
      body: JSON.stringify(body),
    }), () => {
      container.textContent = STORAGE_WAIT_TEXT;
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
    p.textContent = sentence(e.message);
    container.appendChild(p);
    if (e.body && e.body.key_needed) {
      showRestoreKeyField(true);
      const again = document.createElement("button");
      again.type = "button";
      again.className = "btn btn-ghost btn-sm";
      again.textContent = "Try again";
      again.addEventListener("click", loadSabnzbdPreview);
      p.append(" ", again);
    }
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
  if (restoreKey()) payload.key = restoreKey();
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
    await whileStorageWakes(destId, apiFetch(`/api/restore/${destId}/${appId}`, {
      method: "POST",
      body: JSON.stringify(payload),
    }), () => {
      resultEl.textContent = STORAGE_WAIT_TEXT;
    });
    refreshRestoreStatus();
  } catch (e) {
    resultEl.textContent = sentence(e.message);
    resultEl.className = "save-result fail";
    toast(`Restore failed: ${clause(e.message)}`, "fail");
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
    if (state.key_needed) showRestoreKeyField(true);
    if (state.error) {
      resultEl.textContent = sentence(state.error);
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
        if (s.config && !s.config_staged) parts.push("config restored");
        resultEl.textContent = parts.length ? parts.join(", ") + "." : "";
        if (s.config_staged) resultEl.textContent += `${parts.length ? " " : ""}Config not applied yet: ${s.config_staged}`;
      } else {
        resultEl.textContent = sentence(state.message || "Restore complete.");
      }
      resultEl.className = `save-result ${(s && s.config_staged) || state.warning ? "warn" : "ok"}`;
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
    onStopped: (s) => toast(s.error ? `Restore failed: ${clause(s.error)}` : "Restore complete", s.error ? "fail" : "ok"),
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
  // The check runs against the restore's target, so a new target needs a new check.
  document.getElementById("r-override-toggle").addEventListener("change", renderRestoreExtra);
  document.getElementById("restore-btn").addEventListener("click", doRestore);
}

// -------------------------------------------------------------- startup ----
function handleGdriveRedirect() {
  const params = new URLSearchParams(window.location.search);
  const error = params.get("gdrive_error");
  const connected = params.get("gdrive");
  if (!error && !connected) return;

  if (error) {
    toast(`Google Drive: ${clause(error)}`, "fail"); // URLSearchParams has already decoded it
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

// Lazily fetches the last 5 GitHub Releases and shows them in a modal. Only
// reachable via the "What's Changed" button, which lives inside #version-check
// and is therefore only visible once checkForUpdate() has already confirmed
// GitHub is reachable - no extra eager fetch just to decide whether to show it.
async function openWhatsChanged() {
  const body = document.getElementById("whats-changed-body");
  body.innerHTML = "";
  document.getElementById("whats-changed-modal").classList.remove("hidden");
  try {
    const res = await fetch("https://api.github.com/repos/rsaturns/backuparr/releases?per_page=5");
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
    err.textContent = `Could not load release notes: ${clause(e.message)}`;
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

function initFeedbackMenu() {
  const btn = document.getElementById("feedback-btn");
  const menu = document.getElementById("feedback-menu");
  const setOpen = (open) => {
    menu.classList.toggle("hidden", !open);
    btn.setAttribute("aria-expanded", String(open));
  };
  btn.addEventListener("click", () => setOpen(menu.classList.contains("hidden")));
  document.addEventListener("click", (e) => {
    if (!document.getElementById("feedback").contains(e.target)) setOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !menu.classList.contains("hidden")) {
      setOpen(false);
      btn.focus();
    }
  });
  // A link click opens a new tab; close so the menu isn't left open behind it.
  menu.addEventListener("click", () => setOpen(false));
}

// ------------------------------------------------- restore Backuparr ----
function initBackuparrRestore() {
  if (AUTH_DISABLED) document.getElementById("br-password-field").classList.add("hidden");
  document.getElementById("br-submit-btn").addEventListener("click", async () => {
    const resultEl = document.getElementById("br-result");
    const btn = document.getElementById("br-submit-btn");
    if (!document.getElementById("br-confirm").checked) {
      resultEl.textContent = "Check the confirmation box first.";
      resultEl.className = "save-result fail";
      return;
    }
    btn.disabled = true;
    resultEl.textContent = "Restoring...";
    resultEl.className = "save-result";
    try {
      const warnings = await restoreBackuparr("/api/backuparr/restore", document.getElementById("br-file").files[0],
        document.getElementById("br-key").value,
        { confirm: true, password: document.getElementById("br-password").value });
      SETTINGS_SNAPSHOT = null; // the settings on screen are gone; don't warn about them
      const note = warnings.length ? ` ${warnings.join(" ")}` : "";
      resultEl.textContent = `Restored - everyone is signed out.${note}`;
      resultEl.className = warnings.length ? "save-result warn" : "save-result ok";
      const next = document.createElement("a");
      next.className = "btn btn-primary";
      next.href = AUTH_DISABLED ? "/" : "/login";
      next.textContent = AUTH_DISABLED ? "Reload" : "Sign in again";
      btn.replaceWith(next);
    } catch (e) {
      resultEl.textContent = sentence(e.message);
      resultEl.className = "save-result fail";
      btn.disabled = false;
    }
  });
}

async function init() {
  // Until the encryption key is saved, the server refuses everything else.
  try {
    if (await showKeyPrompt(document.getElementById("key-modal"), () => window.location.reload())) return;
  } catch (e) {
    toast(`Could not check the encryption key: ${clause(e.message)}`, "fail");
  }
  initTabs();
  initFeedbackMenu();
  initTheme();
  initUserMenu();
  initLogout();
  initWhatsChangedEvents();
  initRunLogPanel();
  initBackuparrRestore();
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
    await loadConfig();
  } catch (e) {
    toast(`Could not load config: ${clause(e.message)}`, "fail");
  }
  showEncryptionNotice().catch(() => {}); // shown again next time if it couldn't be checked
  refreshRunStatus();
  loadOverview();
  handleGdriveRedirect();
  checkForUpdate();
}

document.addEventListener("DOMContentLoaded", init);
