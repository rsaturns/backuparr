"use strict";

// The encryption key prompt (#110), shared by the setup page and the main
// page: shows the key from /api/secrets-key until the user confirms they've
// saved it, and calls done() after that.
async function showKeyPrompt(container, done) {
  const res = await fetch("/api/secrets-key");
  const body = await res.json().catch(() => null);
  if (!res.ok || !body) throw new Error((body && body.error) || `HTTP ${res.status}`);
  if (body.acknowledged) return false;

  const keyEl = container.querySelector("#key-value");
  keyEl.textContent = body.key;
  container.querySelector("#key-env-note").classList.toggle("hidden", !body.from_environment);
  container.classList.remove("hidden");

  container.querySelector("#key-copy-btn").addEventListener("click", async () => {
    const result = container.querySelector("#key-result");
    try {
      await navigator.clipboard.writeText(body.key);
      result.textContent = "Copied";
      result.className = "save-result ok";
    } catch (e) {
      // No clipboard over plain HTTP: select it for the user to copy.
      const range = document.createRange();
      range.selectNodeContents(keyEl);
      window.getSelection().removeAllRanges();
      window.getSelection().addRange(range);
      result.textContent = "Selected - copy it with Ctrl+C or Cmd+C";
      result.className = "save-result";
    }
  });

  const ackBtn = container.querySelector("#key-ack-btn");
  ackBtn.addEventListener("click", async () => {
    const result = container.querySelector("#key-result");
    ackBtn.disabled = true;
    try {
      const ack = await fetch("/api/secrets-key/acknowledge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      });
      const ackBody = await ack.json().catch(() => null);
      if (!ack.ok) throw new Error((ackBody && ackBody.error) || `HTTP ${ack.status}`);
      done();
    } catch (e) {
      result.textContent = sentence(e.message);
      result.className = "save-result fail";
      ackBtn.disabled = false;
    }
  });
  return true;
}
