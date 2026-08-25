"use strict";

async function submitSetup() {
  const username = document.getElementById("su-username").value.trim();
  const password = document.getElementById("su-password").value;
  const password2 = document.getElementById("su-password2").value;
  const resultEl = document.getElementById("setup-result");
  const btn = document.getElementById("setup-submit-btn");

  if (!username || !password) {
    resultEl.textContent = "Username and password are required";
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

document.getElementById("setup-submit-btn").addEventListener("click", submitSetup);
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter") submitSetup();
});
