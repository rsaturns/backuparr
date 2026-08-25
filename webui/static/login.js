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
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter") submitLogin();
});
