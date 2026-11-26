// Theme toggle for the docs site. Follows the device setting until the
// visitor picks a theme; the choice is saved in localStorage as
// "backuparr-theme" and kept across pages and visits, like the web UI.
(function () {
  var STORAGE_KEY = "backuparr-theme";

  function savedChoice() {
    try {
      return window.localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      return null;
    }
  }

  function deviceTheme() {
    return window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }

  function effectiveTheme() {
    var saved = savedChoice();
    return saved === "dark" || saved === "light" ? saved : deviceTheme();
  }

  // Switch the dark stylesheet and the color-scheme meta to the saved
  // choice. With no saved choice the dark stylesheet keeps its
  // "(prefers-color-scheme: dark)" media query, so the site follows
  // the device.
  function applyTheme() {
    var saved = savedChoice();
    var darkCss = document.getElementById("theme-dark-css");
    var meta = document.querySelector('meta[name="color-scheme"]');
    if (darkCss && saved === "dark") {
      darkCss.media = "all";
    } else if (darkCss && saved === "light") {
      darkCss.media = "not all";
    }
    if (meta) {
      if (saved === "dark") {
        meta.content = "dark";
      } else if (saved === "light") {
        meta.content = "light";
      } else {
        meta.content = "light dark";
      }
    }
  }

  function applyThemeButton() {
    var button = document.getElementById("theme-toggle");
    if (!button) {
      return;
    }
    var theme = effectiveTheme();
    button.textContent = theme === "dark" ? "☀" : "☽";
    button.setAttribute(
      "aria-label",
      theme === "dark" ? "Switch to light theme" : "Switch to dark theme"
    );
    button.hidden = false;
  }

  // Apply the saved theme as early as possible, before the page paints.
  applyTheme();

  document.addEventListener("DOMContentLoaded", function () {
    applyThemeButton();
    var button = document.getElementById("theme-toggle");
    if (button) {
      button.addEventListener("click", function () {
        var next = effectiveTheme() === "dark" ? "light" : "dark";
        try {
          window.localStorage.setItem(STORAGE_KEY, next);
        } catch (e) {
          /* Private browsing: keep the choice for this visit only. */
        }
        applyTheme();
        applyThemeButton();
      });
    }
    // Follow the device setting while no choice is saved.
    window
      .matchMedia("(prefers-color-scheme: dark)")
      .addEventListener("change", function () {
        if (!savedChoice()) {
          applyThemeButton();
        }
      });
  });
})();
