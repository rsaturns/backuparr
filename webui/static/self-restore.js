"use strict";

// Restoring Backuparr from its own backup (#111), shared by the setup page
// and Settings. The file goes as base64 inside JSON: the server only accepts
// JSON for changes, which another site can't send.
const SELF_RESTORE_MAX_BYTES = 48 * 1024 * 1024;

function readFileBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).replace(/^data:[^,]*,/, ""));
    reader.onerror = () => reject(new Error("The file couldn't be read."));
    reader.readAsDataURL(file);
  });
}

// Returns the server's warnings; throws with its error.
async function restoreBackuparr(url, file, key, extra) {
  if (!file) throw new Error("Choose Backuparr's backup file");
  if (!key.trim()) throw new Error("Enter the encryption key.");
  if (file.size > SELF_RESTORE_MAX_BYTES) throw new Error("That file is too large to be Backuparr's own backup.");
  const archive = await readFileBase64(file);
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ archive, key: key.trim(), ...extra }),
  });
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new Error((body && body.error) || `HTTP ${res.status}`);
  return body.warnings || [];
}

function renderRestoreWarnings(container, warnings) {
  container.innerHTML = "";
  warnings.forEach((text) => {
    const p = document.createElement("p");
    p.className = "key-warning";
    p.textContent = sentence(text);
    container.appendChild(p);
  });
}
