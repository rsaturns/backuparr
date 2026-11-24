"""Every form field in the web UI has a name a screen reader can announce,
names start with the text they show, and muted text keeps WCAG AA contrast
(#261). These are the problems an axe-core check of 3.0.0 found."""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from conftest import PASSWORD

STYLE = Path(__file__).resolve().parent.parent / "webui" / "static" / "style.css"
_UNNAMED_INPUT_TYPES = {"hidden", "submit", "button", "reset", "image"}


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class _Page(HTMLParser):
    """Collects the form fields and what labels them, the text inside each
    element with an id, and buttons and links named with aria-label beside
    the text they show. Text inside a <select>, an <svg> or anything
    aria-hidden isn't counted: it's not a label."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields = []      # (description, attrs, [enclosing <label> entries])
        self.label_for = {}   # id -> text of <label for=id>
        self.text_by_id = {}  # id -> text inside the element
        self.aria_named = []  # (tag, aria-label, visible text)
        self._open = []       # open elements: {"tag", "attrs", "text", "hidden"}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if (tag in ("input", "select", "textarea") and a.get("type", "text") not in _UNNAMED_INPUT_TYPES
                and a.get("aria-hidden") != "true"):  # e.g. the username kept for password managers
            what = f"<{tag} {' '.join(f'{k}={v!r}' for k, v in attrs)}>"
            self.fields.append((what, a, [e for e in self._open if e["tag"] == "label"]))
        if tag in _VOID:
            return
        hidden = a.get("aria-hidden") == "true" or tag in ("select", "svg")
        self._open.append({"tag": tag, "attrs": a, "text": [], "hidden": hidden})

    def handle_endtag(self, tag):
        found = [i for i, e in enumerate(self._open) if e["tag"] == tag]
        if not found:
            return
        closed, self._open = self._open[found[-1]:], self._open[:found[-1]]
        for e in reversed(closed):
            text = "".join(e["text"]).strip()
            if e["attrs"].get("id"):
                self.text_by_id[e["attrs"]["id"]] = text
            if e["tag"] == "label" and e["attrs"].get("for"):
                self.label_for[e["attrs"]["for"]] = text
            if e["tag"] in ("button", "a") and e["attrs"].get("aria-label"):
                self.aria_named.append((e["tag"], e["attrs"]["aria-label"], text))

    def handle_data(self, data):
        if any(e["hidden"] for e in self._open):
            return
        for e in self._open:
            e["text"].append(data)


def _parse(html):
    page = _Page()
    page.feed(html)
    return page


def _unnamed(page):
    missing = []
    for what, a, labels in page.fields:
        named = (
            a.get("aria-label", "").strip()
            or any(page.text_by_id.get(i) for i in a.get("aria-labelledby", "").split())
            or page.label_for.get(a.get("id") or "")
            or any("".join(label["text"]).strip() for label in labels)
            or a.get("title", "").strip()
        )
        if not named:
            missing.append(what)
    return missing


def _setup_html(isolated_webui):
    return isolated_webui.app.test_client().get("/setup").get_data(as_text=True)


def test_every_field_on_the_main_page_has_a_name(authed_client):
    page = _parse(authed_client.get("/").get_data(as_text=True))
    assert len(page.fields) > 100  # every app's and destination's card is there
    unnamed = _unnamed(page)
    assert not unnamed, "\n".join(unnamed)


def test_every_field_on_the_setup_and_login_pages_has_a_name(isolated_webui):
    setup = _parse(_setup_html(isolated_webui))
    assert setup.fields and not _unnamed(setup), "\n".join(_unnamed(setup))
    client = isolated_webui.app.test_client()
    assert client.post("/api/setup", json={"username": "admin", "password": PASSWORD}).status_code == 200
    login = _parse(isolated_webui.app.test_client().get("/login").get_data(as_text=True))
    assert login.fields and not _unnamed(login), "\n".join(_unnamed(login))


def test_the_card_switches_say_what_they_switch(authed_client):
    html = authed_client.get("/").get_data(as_text=True)
    assert 'class="f-enabled" aria-label="Back up Radarr"' in html
    assert 'class="d-enabled" aria-label="Upload to Google Drive"' in html


def test_names_start_with_the_text_they_show(authed_client):
    """WCAG 2.5.3 Label in Name: someone using voice control says what they
    see. Text without letters (the cards' "?") is a symbol, not a label."""
    page = _parse(authed_client.get("/").get_data(as_text=True))
    checked = 0
    for tag, label, visible in page.aria_named:
        if not re.search(r"[A-Za-z]", visible):
            continue
        checked += 1
        assert label.lower().startswith(visible.lower()), (tag, label, visible)
    assert checked  # the account button, at least


def _colours():
    """{theme: {token: hex}} from the stylesheet's light and dark blocks."""
    css = STYLE.read_text()
    light = re.search(r"^:root \{(.*?)^\}", css, re.S | re.M).group(1)
    dark = re.search(r'^:root\[data-theme="dark"\] \{(.*?)^\}', css, re.S | re.M).group(1)
    tokens = lambda block: dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})", block))  # noqa: E731
    return {"light": tokens(light), "dark": tokens(dark)}


def _contrast(fg, bg):
    def luminance(colour):
        channels = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        channels = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
    hi, lo = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("text", ["text", "text-muted", "accent", "danger", "ok", "warn"])
@pytest.mark.parametrize("background", ["bg", "bg-elevated"])
def test_text_meets_wcag_aa_contrast(theme, text, background):
    """Each of these colours is used for text on the page or on a card."""
    colours = _colours()[theme]
    assert _contrast(colours[text], colours[background]) >= 4.5


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("button", ["accent", "danger"])
def test_button_text_meets_wcag_aa_contrast(theme, button):
    colours = _colours()[theme]
    assert _contrast(colours[f"{button}-contrast"], colours[button]) >= 4.5
