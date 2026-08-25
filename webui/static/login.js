"use strict";

async function submitLogin() {
  const username = document.getElementById("li-username").value.trim();
  const password = document.getElementById("li-password").value;
  const resultEl = document.getElementById("login-result");
  const btn = document.getElementById("login-submit-btn");

  if (!username || !password) {
    resultEl.textContent = "Username and password are required";
    resultEl.className = "save-result fail";
    return;
  }

  btn.disabled = true;
  resultEl.textContent = "Logging in...";
  resultEl.className = "save-result";
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    const body = await res.json().catch(() => null);
    if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
    window.location.href = "/";
  } catch (e) {
    resultEl.textContent = e.message;
    resultEl.className = "save-result fail";
    btn.disabled = false;
  }
}

document.getElementById("login-submit-btn").addEventListener("click", submitLogin);

// ---------------------------------------------------------------- reset ----
const RESET_PHRASE = "i-want-to-reset-and-delete-files";

function resetModalOpen() {
  return !document.getElementById("reset-modal").classList.contains("hidden");
}

function showResetStep(step) {
  document.getElementById("reset-step-warning").classList.toggle("hidden", step !== "warning");
  document.getElementById("reset-step-confirm").classList.toggle("hidden", step !== "confirm");
}

function openResetModal() {
  document.getElementById("reset-modal").classList.remove("hidden");
  showResetStep("warning");
}

function closeResetModal() {
  document.getElementById("reset-modal").classList.add("hidden");
  const input = document.getElementById("reset-confirm-input");
  input.value = "";
  document.getElementById("reset-confirm-btn").disabled = true;
  const resultEl = document.getElementById("reset-result");
  resultEl.textContent = "";
  resultEl.className = "save-result";
}

async function submitReset() {
  const input = document.getElementById("reset-confirm-input");
  const btn = document.getElementById("reset-confirm-btn");
  const resultEl = document.getElementById("reset-result");
  if (input.value !== RESET_PHRASE) return;

  btn.disabled = true;
  resultEl.textContent = "Resetting...";
  resultEl.className = "save-result";
  try {
    const res = await fetch("/api/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: input.value }),
    });
    const body = await res.json().catch(() => null);
    if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
    window.location.href = "/setup";
  } catch (e) {
    resultEl.textContent = e.message;
    resultEl.className = "save-result fail";
    btn.disabled = false;
  }
}

document.getElementById("reset-open-btn").addEventListener("click", openResetModal);
document.getElementById("reset-modal-close").addEventListener("click", closeResetModal);
document.getElementById("reset-cancel-btn-1").addEventListener("click", closeResetModal);
document.getElementById("reset-cancel-btn-2").addEventListener("click", closeResetModal);
document.getElementById("reset-continue-btn").addEventListener("click", () => showResetStep("confirm"));
document.getElementById("reset-confirm-input").addEventListener("input", (e) => {
  document.getElementById("reset-confirm-btn").disabled = e.target.value !== RESET_PHRASE;
});
document.getElementById("reset-confirm-btn").addEventListener("click", submitReset);
document.getElementById("reset-modal").addEventListener("click", (e) => {
  if (e.target.id === "reset-modal") closeResetModal();
});

document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  if (resetModalOpen()) {
    if (!document.getElementById("reset-confirm-btn").disabled) submitReset();
    return;
  }
  submitLogin();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && resetModalOpen()) closeResetModal();
});
