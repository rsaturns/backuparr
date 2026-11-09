// Backuparr's messages are written log-style ("radarr: couldn't connect -
// check the URL"), since they also go to the log and notifications. The
// page shows them as sentences: loaded before every other script.

function endsLikeASentence(text) {
  return /[.!?…]$/.test(text);
}

// A message shown on its own: a capital first letter and a full stop.
function sentence(text) {
  const s = String(text ?? "").trim();
  if (!s) return s;
  const capped = s.charAt(0).toUpperCase() + s.slice(1);
  return endsLikeASentence(capped) ? capped : `${capped}.`;
}

// A message after a label ("Restore failed: ..."): lower case after the
// colon, as written, but ending like a sentence.
function clause(text) {
  const s = String(text ?? "").trim();
  if (!s) return s;
  return endsLikeASentence(s) ? s : `${s}.`;
}
