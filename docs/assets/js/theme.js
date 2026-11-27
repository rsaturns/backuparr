// The docs' light/dark button (#272), working as the web UI's does: the site
// follows the device's setting until the button is clicked, then the choice
// is kept in this browser ("backuparr-theme"), across pages and visits.
//
// head_custom.html loads this in the <head>. It writes the dark stylesheet's
// <link> there with the choice already in its media, so the browser waits
// for that stylesheet before painting when it applies: a page never shows
// the other theme first. Changing the media of a link already in the page
// doesn't do that - Chromium paints with the light theme while the dark
// stylesheet loads. Without JavaScript head_custom.html's noscript copy
// follows the device, and the button stays hidden.
(function () {
  "use strict";

  var KEY = "backuparr-theme";
  var script = document.currentScript;
  var href = script && script.getAttribute("data-dark-css");
  // The dark device's media query, the same one the preload and the
  // noscript copy use (head_custom.html). It also tells the button whether
  // the device's setting is dark.
  var deviceMedia = script && script.getAttribute("data-dark-media");
  if (!href || !deviceMedia) return;
  var meta = document.querySelector('meta[name="color-scheme"]');
  var device = window.matchMedia ? window.matchMedia(deviceMedia) : null;

  function saved() {
    try {
      var t = localStorage.getItem(KEY);
      return t === "light" || t === "dark" ? t : null;
    } catch (e) {
      return null;
    }
  }

  // Kept here too, so the button still works where storage is blocked.
  var choice = saved();

  function current() {
    return choice || (device && device.matches ? "dark" : "light");
  }

  // The dark stylesheet applies always, never, or on a dark device - on
  // screen only, so a page prints light.
  function media() {
    return choice === "dark" ? "screen" : choice === "light" ? "not all" : deviceMedia;
  }

  // The two values come from head_custom.html's attributes, so they're
  // escaped before they go into the page as HTML.
  function attr(value) {
    return value.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
  }

  document.write('<link rel="stylesheet" href="' + attr(href) + '" id="theme-dark" media="' + attr(media()) + '">');
  var sheet = document.getElementById("theme-dark");
  if (!sheet) return;

  // The color-scheme meta matches the theme, for scroll bars and form
  // fields. The button's icon comes from the stylesheet that applies
  // (custom.scss).
  function apply() {
    sheet.media = media();
    if (meta) meta.content = choice || "light dark";
    var btn = document.getElementById("theme-toggle");
    if (btn) btn.setAttribute("aria-label", current() === "dark" ? "Switch to light theme" : "Switch to dark theme");
  }

  // Another tab's choice, or this page coming back from the back/forward
  // cache after a choice on another page.
  function sync() {
    var t = saved();
    if (t && t !== choice) {
      choice = t;
      apply();
    }
  }

  document.documentElement.classList.add("has-theme-toggle");
  apply();

  if (device && device.addEventListener) device.addEventListener("change", apply);
  window.addEventListener("storage", sync);
  window.addEventListener("pageshow", sync);

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;
    btn.addEventListener("click", function () {
      choice = current() === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(KEY, choice);
      } catch (e) {}
      apply();
    });
    apply();
  });
})();
