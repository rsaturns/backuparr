"use strict";

async function submitSetup() {
  const username = document.getElementById("su-username").value.trim();
  const password = document.getElementById("su-password").value;
  const password2 = document.getElementById("su-password2").value;
  const keyEl = document.getElementById("su-key");
  const resultEl = document.getElementById("setup-result");
  const btn = document.getElementById("setup-submit-btn");

  if (!username || !password) {
    resultEl.textContent = "Username and password are required";
    resultEl.className = "save-result fail";
    return;
  }
  if (keyEl && !keyEl.value.trim()) {
    resultEl.textContent = "Enter your encryption key";
    resultEl.className = "save-result fail";
    return;
  }
  if (password !== password2) {
    resultEl.textContent = "Passwords don't match";
    resultEl.className = "save-result fail";
    return;
  }

  btn.disabled = true;
  resultEl.textContent = "Creating account...";
  resultEl.className = "save-result";
  try {
    const res = await fetch("/api/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(keyEl ? { username, password, key: keyEl.value.trim() } : { username, password }),
    });
    const body = await res.json().catch(() => null);
    if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
    document.removeEventListener("keydown", submitOnEnter);
    document.getElementById("account-step").classList.add("hidden");
    const shown = await showKeyPrompt(document.getElementById("key-step"), () => { window.location.href = "/"; });
    if (!shown) window.location.href = "/";
  } catch (e) {
    resultEl.textContent = e.message;
    resultEl.className = "save-result fail";
    btn.disabled = false;
  }
}

function submitOnEnter(e) {
  if (e.key === "Enter") submitSetup();
}

function showStep(id) {
  ["choice-step", "restore-step", "restored-step", "account-step"].forEach((step) => {
    document.getElementById(step).classList.toggle("hidden", step !== id);
  });
}

async function submitRestore() {
  const resultEl = document.getElementById("sr-result");
  const btn = document.getElementById("sr-submit-btn");
  btn.disabled = true;
  resultEl.textContent = "Restoring...";
  resultEl.className = "save-result";
  try {
    const warnings = await restoreBackuparr("/api/setup/restore", document.getElementById("sr-file").files[0],
      document.getElementById("sr-key").value);
    renderRestoreWarnings(document.getElementById("sr-warnings"), warnings);
    showStep("restored-step");
  } catch (e) {
    resultEl.textContent = e.message;
    resultEl.className = "save-result fail";
    btn.disabled = false;
  }
}

function startAccountStep() {
  showStep("account-step");
  document.addEventListener("keydown", submitOnEnter);
  (document.getElementById("su-key") || document.getElementById("su-username")).focus();
}

document.getElementById("choice-new-btn").addEventListener("click", startAccountStep);
// Settings are already here and only the login is missing (#123): straight to a new login.
if (document.body.dataset.needsKey) startAccountStep();
document.getElementById("choice-restore-btn").addEventListener("click", () => showStep("restore-step"));
document.getElementById("sr-back-btn").addEventListener("click", () => showStep("choice-step"));
document.getElementById("sr-submit-btn").addEventListener("click", submitRestore);
document.getElementById("setup-submit-btn").addEventListener("click", submitSetup);
